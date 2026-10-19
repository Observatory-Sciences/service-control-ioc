"""``scdemo-controller``: configures the relay, then polls it.

After ``delay_connect`` it connects four edges to the relay: ``req`` (REQ to
``rep``), ``dealer`` (DEALER to ``router``), ``sub`` (SUB to ``pub``, every
topic) and ``http`` (one keep-alive connection to ``http``). It configures the
relay over REQ, retrying on a fresh REQ socket after a timeout, and announces
ready once configured. It then polls every second, and configures again when
the relay's boot id changes or three DEALER status requests in a row time out.
The controller has no listeners.
"""

import asyncio
import contextlib
import json
import os
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Final

import zmq
import zmq.asyncio
from zmq.utils.monitor import parse_monitor_message

from scdemo.env import ConfigError, Env, exit_on_config_error
from scdemo.faults import Faults, announce_ready, run_fault_timers
from scdemo.http import HttpClient, HttpError
from scdemo.logs import log_event, setup_logging
from scdemo.protocol import ports
from scdemo.service import run_service

EDGES: Final = frozenset({"req", "dealer", "sub", "http"})
CONFIG_TIMEOUT: Final = 3.0
CONFIG_RETRY: Final = 2.0
POLL_PERIOD: Final = 1.0
STATUS_TIMEOUT: Final = 1.0  # per DEALER or HTTP status request
MAX_STATUS_TIMEOUTS: Final = 3  # in a row, then configure again
STATUS_LOG_PERIOD: Final = 5.0
_MONITOR_EVENTS: Final = zmq.EVENT_CONNECTED | zmq.EVENT_DISCONNECTED
# ValueError: an over-long line from a peer that is not speaking our HTTP.
_LINK_ERRORS: Final = (
    OSError,
    HttpError,
    TimeoutError,
    asyncio.IncompleteReadError,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class Config:
    port_base: int
    relay_host: str
    rate_hz: int
    faults: Faults


def load_config(environ: dict[str, str]) -> Config:
    env = Env(environ)
    rate_hz = env.int("SCDEMO_RATE_HZ", 10)
    if rate_hz < 1:
        raise ConfigError(f"SCDEMO_RATE_HZ: must be >= 1: {rate_hz}")
    return Config(
        port_base=env.int("SCDEMO_PORT_BASE", 20000),
        relay_host=env.str("SCDEMO_RELAY_HOST", "127.0.0.1"),
        rate_hz=rate_hz,
        faults=Faults.from_env(env, frozenset(), EDGES),
    )


class ZmqEdge:
    """One outgoing ZMQ edge: a socket, its monitor and whether it is connected.

    ``connected``/``disconnected`` come from the socket monitor. ``reset``
    closes the socket and connects a fresh one (a stuck REQ needs this).
    """

    def __init__(
        self,
        name: str,
        kind: int,
        ctx: zmq.asyncio.Context,
        endpoint: tuple[str, int],
        tg: asyncio.TaskGroup,
    ) -> None:
        self.name = name
        self.kind = kind
        self.ctx = ctx
        self.host, self.port = endpoint
        self.tg = tg
        self.connected = False
        self._sock: zmq.asyncio.Socket | None = None
        self._monitor: zmq.asyncio.Socket | None = None
        self._watch: asyncio.Task[None] | None = None

    @property
    def sock(self) -> zmq.asyncio.Socket:
        assert self._sock is not None, "open() first"
        return self._sock

    def open(self) -> zmq.asyncio.Socket:
        log_event(
            "connecting", name=self.name, proto="tcp", host=self.host, port=self.port
        )
        sock = self.ctx.socket(self.kind)
        sock.setsockopt(zmq.LINGER, 0)
        if self.kind == zmq.SUB:
            sock.subscribe(b"")
        if self.kind == zmq.DEALER:
            # Queue nothing for a relay that is not there: a send then fails
            # at once (and counts as a status timeout) instead of piling up.
            sock.setsockopt(zmq.IMMEDIATE, 1)
        monitor = sock.get_monitor_socket(_MONITOR_EVENTS)
        sock.connect(f"tcp://{self.host}:{self.port}")
        self._sock, self._monitor = sock, monitor
        self._watch = self.tg.create_task(self._watch_monitor(monitor))
        return sock

    async def _watch_monitor(self, monitor: zmq.asyncio.Socket) -> None:
        while True:
            try:
                msg = parse_monitor_message(await monitor.recv_multipart())
            except zmq.ZMQError:
                return  # the monitor was closed under us by close()
            if msg["event"] == zmq.EVENT_CONNECTED and not self.connected:
                self.connected = True
                log_event("connected", name=self.name)
            elif msg["event"] == zmq.EVENT_DISCONNECTED and self.connected:
                self.connected = False
                log_event("disconnected", name=self.name)

    def close(self) -> None:
        """Close the socket and its monitor (linger 0)."""
        if self._watch is not None:
            self._watch.cancel()  # runs before the closed monitor wakes it
            self._watch = None
        if self._sock is not None:
            self._sock.disable_monitor()
        if self._monitor is not None:
            self._monitor.close(linger=0)
        if self._sock is not None:
            self._sock.close(linger=0)
        self._sock = self._monitor = None
        if self.connected:
            self.connected = False
            log_event("disconnected", name=self.name)

    def reset(self) -> zmq.asyncio.Socket:
        self.close()
        return self.open()


def _parse(raw: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _udp_count(status: dict[str, object]) -> int:
    udp = status.get("udp")
    count = udp.get("count") if isinstance(udp, dict) else None
    return count if isinstance(count, int) else 0


@dataclass(slots=True)
class Controller:
    """Configure state shared by the configure and poll tasks."""

    cfg: Config
    boot_id: str | None = None  # set while configured, None while configuring
    last_boot_id: str | None = None
    retired: set[str] = field(default_factory=set)  # boot ids of dead relays
    reason: str = "startup"
    need_configure: asyncio.Event = field(default_factory=asyncio.Event)
    ready_sent: bool = False
    status_timeouts: int = 0
    ticks: int = 0
    dealer_seq: int = 0
    http_connected: bool = False
    http_opened: int = 0
    last_status_log: float | None = None

    # -- configure ------------------------------------------------------

    async def run_configure(self, req: ZmqEdge) -> None:
        sock = req.open()
        while True:
            await self._configure(req, sock)
            await self.need_configure.wait()
            self.need_configure.clear()
            sock = req.reset()  # the relay may be new: start a clean REQ

    async def _configure(self, req: ZmqEdge, sock: zmq.asyncio.Socket) -> None:
        """Send ``configure`` until the relay accepts it."""
        request = json.dumps({"cmd": "configure", "rate_hz": self.cfg.rate_hz})
        while True:
            log_event("configuring", reason=self.reason, rate_hz=self.cfg.rate_hz)
            try:
                async with asyncio.timeout(CONFIG_TIMEOUT):
                    await sock.send(request.encode())
                    raw = await sock.recv()
            except TimeoutError:
                log_event("config_timeout", timeout=CONFIG_TIMEOUT)
                sock = req.reset()  # a REQ waiting for a reply is stuck for good
                await asyncio.sleep(CONFIG_RETRY)
                self.reason = "retry"
                continue
            reply = _parse(raw)
            boot_id = None if reply is None else reply.get("boot_id")
            if (
                reply is None
                or reply.get("ok") is not True
                or not isinstance(boot_id, str)
            ):
                # Rejected or garbled: the REQ is back in step, so just retry.
                log_event("config_timeout", error="bad reply")
                await asyncio.sleep(CONFIG_RETRY)
                self.reason = "retry"
                continue
            break
        if self.last_boot_id is not None and self.last_boot_id != boot_id:
            self.retired.add(self.last_boot_id)
        self.last_boot_id = self.boot_id = boot_id
        self.status_timeouts = 0
        log_event("configured", boot_id=boot_id, rate_hz=self.cfg.rate_hz)
        if not self.ready_sent:
            self.ready_sent = True
            await announce_ready(self.cfg.faults)

    def reconfigure(self, reason: str) -> None:
        """Ask the configure task to configure again (once per episode)."""
        if self.boot_id is None:
            return  # not configured yet, or already configuring
        self.boot_id = None
        self.reason = reason
        self.need_configure.set()

    def _check_boot(self, boot_id: object) -> None:
        if (
            isinstance(boot_id, str)
            and self.boot_id is not None
            and boot_id != self.boot_id
            and boot_id not in self.retired
        ):
            self.reconfigure("boot_id")

    # -- polling --------------------------------------------------------

    async def run_poll(
        self,
        dealer: ZmqEdge | None,
        sub: ZmqEdge | None,
        http: HttpClient | None,
    ) -> None:
        dealer_sock = None if dealer is None else dealer.open()
        sub_sock = None if sub is None else sub.open()
        loop = asyncio.get_running_loop()
        while True:
            status = None
            if dealer_sock is not None:
                status = await self._poll_dealer(dealer_sock)
            if http is not None:
                http_status = await self._poll_http(http)
                status = status or http_status
            if sub_sock is not None:
                await self._drain_sub(sub_sock)
            if status is not None:
                self._check_boot(status.get("boot_id"))
                now = loop.time()
                last = self.last_status_log
                if last is None or now - last >= STATUS_LOG_PERIOD:
                    self.last_status_log = now
                    log_event(
                        "status",
                        count=_udp_count(status),
                        boot_id=str(status.get("boot_id")),
                        ticks=self.ticks,
                        configured=int(self.boot_id is not None),
                    )
            await asyncio.sleep(POLL_PERIOD)

    async def _poll_dealer(self, sock: zmq.asyncio.Socket) -> dict[str, object] | None:
        """One ``status`` request; the reply, or None after a timeout.

        A sequence number in the envelope lets late replies to earlier
        (timed-out) requests be skipped.
        """
        self.dealer_seq += 1
        seq = str(self.dealer_seq).encode()
        try:
            async with asyncio.timeout(STATUS_TIMEOUT):
                await sock.send_multipart([seq, b"", b"status"], zmq.NOBLOCK)
                while True:
                    frames = await sock.recv_multipart()
                    if frames[0] == seq:
                        break
        except (TimeoutError, zmq.Again):
            self.status_timeouts += 1
            if self.status_timeouts >= MAX_STATUS_TIMEOUTS:
                self.status_timeouts = 0
                self.reconfigure("status_timeout")
            return None
        self.status_timeouts = 0
        return _parse(frames[-1])

    async def _poll_http(self, http: HttpClient) -> dict[str, object] | None:
        """``GET /status`` on the kept connection; log when it opens or closes."""
        try:
            async with asyncio.timeout(STATUS_TIMEOUT):
                status = await http.get_json("/status")
        except _LINK_ERRORS:
            if self.http_connected:
                self.http_connected = False
                log_event("disconnected", name="http")
            await http.close()
            return None
        if http.connections_opened != self.http_opened:
            # A new TCP connection: the old one (if any) has gone.
            self.http_opened = http.connections_opened
            if self.http_connected:
                log_event("disconnected", name="http")
            self.http_connected = True
            log_event("connected", name="http")
        return status

    async def _drain_sub(self, sock: zmq.asyncio.Socket) -> None:
        """Read every queued ``tick <n> <boot_id>``."""
        while True:
            try:
                message = await sock.recv(zmq.NOBLOCK)
            except zmq.Again:
                return
            parts = message.split()
            if len(parts) == 3 and parts[0] == b"tick":
                self.ticks += 1
                self._check_boot(parts[2].decode(errors="replace"))


@contextlib.contextmanager
def _zmq_context() -> Generator[zmq.asyncio.Context]:
    ctx = zmq.asyncio.Context()
    ctx.linger = 0
    try:
        yield ctx
    finally:
        ctx.term()


async def _run(cfg: Config) -> None:
    log_event("starting", port_base=cfg.port_base)
    faults = cfg.faults
    p = ports(cfg.port_base)
    host = cfg.relay_host
    ctrl = Controller(cfg)
    edges: list[ZmqEdge] = []
    http: HttpClient | None = None
    with _zmq_context() as ctx:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(run_fault_timers(faults))
                await asyncio.sleep(faults.delay_connect)

                def edge(name: str, kind: int, port: int) -> ZmqEdge | None:
                    target = faults.connect_port(name, port)
                    if target is None:
                        log_event("connect_skipped", name=name)
                        return None
                    zmq_edge = ZmqEdge(name, kind, ctx, (host, target), tg)
                    edges.append(zmq_edge)
                    return zmq_edge

                req = edge("req", zmq.REQ, p.rep)
                dealer = edge("dealer", zmq.DEALER, p.router)
                sub = edge("sub", zmq.SUB, p.pub)
                http_port = faults.connect_port("http", p.http)
                if http_port is None:
                    log_event("connect_skipped", name="http")
                else:
                    log_event(
                        "connecting",
                        name="http",
                        proto="http",
                        host=host,
                        port=http_port,
                    )
                    http = HttpClient(host, http_port)
                if req is not None:
                    tg.create_task(ctrl.run_configure(req))
                tg.create_task(ctrl.run_poll(dealer, sub, http))
        finally:
            # Every task has finished, so nothing is waiting on these sockets.
            for zmq_edge in edges:
                zmq_edge.close()
            if http is not None:
                await http.close()


@exit_on_config_error
def main() -> None:
    cfg = load_config(dict(os.environ))
    setup_logging("controller")
    run_service(lambda: _run(cfg))


if __name__ == "__main__":
    main()
