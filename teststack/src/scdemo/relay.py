"""``scdemo-relay``: the hub. ZMQ REP/ROUTER/PUB/PULL, HTTP, TCP and UDP links.

Before configure it binds ``rep``, ``router``, ``pub``, ``http`` and ``udp``,
connects ``stats@1..N`` to the sinks, then announces ready. A ``configure``
request on REP binds ``pull``, starts UDP to the sinks (``udp@1`` connected,
``udp@2..N`` unconnected ``sendto``) and connects ``hw`` (retried every second,
never waited for).
"""

import asyncio
import contextlib
import json
import os
import socket
import uuid
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Final

import zmq
import zmq.asyncio

from scdemo.env import ConfigError, Env, exit_on_config_error
from scdemo.faults import Faults, announce_ready, run_fault_timers
from scdemo.http import serve_http
from scdemo.logs import log_event, setup_logging
from scdemo.protocol import SeqTracker, pack_datagram, ports, unpack_datagram
from scdemo.service import run_service

LISTENERS: Final = frozenset({"rep", "router", "pub", "pull", "http", "udp"})
BIND_HOST: Final = "127.0.0.1"  # loopback only: configure is unauthenticated
SINK_HOST: Final = "127.0.0.1"  # sinks run on the relay's host
RETRY_PERIOD: Final = 1.0
PING_PERIOD: Final = 1.0
PONG_TIMEOUT: Final = 3.0
TICK_PERIOD: Final = 1.0
MAX_RATE_HZ: Final = 10_000
MAX_LAG: Final = 1.0  # seconds behind schedule before pacing resets, not bursts


def edge_names(sink_count: int) -> frozenset[str]:
    """``stats@n`` and ``udp@n`` for every sink, plus ``hw``."""
    sinks = range(1, sink_count + 1)
    return frozenset(
        {"hw", *(f"stats@{n}" for n in sinks), *(f"udp@{n}" for n in sinks)}
    )


@dataclass(frozen=True, slots=True)
class Config:
    port_base: int
    sink_count: int
    hw_host: str
    faults: Faults


def load_config(environ: dict[str, str]) -> Config:
    env = Env(environ)
    sink_count = env.int("SCDEMO_SINK_COUNT", 2)
    if sink_count < 1:
        raise ConfigError(f"SCDEMO_SINK_COUNT: must be >= 1: {sink_count}")
    return Config(
        port_base=env.int("SCDEMO_PORT_BASE", 20000),
        sink_count=sink_count,
        hw_host=env.str("SCDEMO_HW_HOST", "127.0.0.2"),
        faults=Faults.from_env(env, LISTENERS, edge_names(sink_count)),
    )


class _UdpReceiver(asyncio.DatagramProtocol):
    def __init__(self, tracker: SeqTracker) -> None:
        self.tracker = tracker

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        try:
            _stream, seq = unpack_datagram(data)
        except ValueError:
            return  # not one of ours
        first = self.tracker.count == 0
        restarts = self.tracker.restarts
        missing = self.tracker.feed(seq)
        if first:
            log_event("first_datagram", name="udp", seq=seq)
        elif self.tracker.restarts != restarts:
            log_event("first_datagram", name="udp", seq=seq, restart=1)
        if missing is not None:
            log_event(
                "gap_detected",
                name="udp",
                expected=seq - missing,
                got=seq,
                missing=missing,
            )


class _UdpSender(asyncio.DatagramProtocol):
    def error_received(self, exc: Exception) -> None:
        # A connected UDP socket reports ICMP port unreachable (the sink is
        # down) as ConnectionRefusedError on a later send. Expected while a
        # sink is absent: keep sending, the sink picks up when it is back.
        pass


@dataclass(slots=True)
class _Counters:
    udp: SeqTracker = field(default_factory=SeqTracker)
    sent: dict[str, int] = field(default_factory=dict)
    pull: int = 0
    sinks: dict[str, object] = field(default_factory=dict)
    pongs: dict[str, int] = field(default_factory=dict)
    ticks: int = 0
    hw_connected: bool = False


class Relay:
    """The relay's sockets, counters and configure state."""

    def __init__(self, cfg: Config, ctx: zmq.asyncio.Context) -> None:
        self.cfg = cfg
        self.ports = ports(cfg.port_base)
        self.ctx = ctx
        self.boot_id = uuid.uuid4().hex
        self.counters = _Counters()
        self.rate_hz: int | None = None
        self.sockets: list[zmq.asyncio.Socket] = []
        self.closers: list[asyncio.BaseTransport | asyncio.Server] = []
        self.http_clients: set[asyncio.StreamWriter] = set()
        self._configure_lock = asyncio.Lock()

    # -- status ---------------------------------------------------------

    def status(self) -> dict[str, object]:
        c = self.counters
        return {
            "boot_id": self.boot_id,
            "configured": self.rate_hz is not None,
            "rate_hz": self.rate_hz,
            "udp": {
                "count": c.udp.count,
                "gaps": c.udp.gaps,
                "missing": c.udp.missing,
                "late": c.udp.late,
            },
            "sent": dict(c.sent),
            "pull": c.pull,
            "sinks": dict(c.sinks),
            "stats": dict(c.pongs),
            "ticks": c.ticks,
            "hw": c.hw_connected,
        }

    def _http_status(self, path: str) -> dict[str, object]:
        if path != "/status":
            raise LookupError(path)
        return self.status()

    # -- binds ----------------------------------------------------------

    async def _bind_port(self, name: str, proto: str, port: int) -> int | None:
        faults = self.cfg.faults
        await asyncio.sleep(faults.delay_bind)
        log_event("binding", name=name, proto=proto, port=port)
        bind = faults.bind_port(name, port)
        if bind is None:
            log_event("bind_skipped", name=name, proto=proto, port=port)
        return bind

    async def bind_zmq(
        self, name: str, kind: int, port: int
    ) -> zmq.asyncio.Socket | None:
        bind = await self._bind_port(name, "zmq", port)
        if bind is None:
            return None
        sock = self.ctx.socket(kind)
        self.sockets.append(sock)
        sock.bind(f"tcp://{BIND_HOST}:{bind}")
        log_event("bound", name=name, proto="zmq", port=bind)
        return sock

    async def bind_http(self) -> None:
        bind = await self._bind_port("http", "http", self.ports.http)
        if bind is None:
            return
        server = await serve_http(
            BIND_HOST,
            bind,
            self._http_status,
            lambda: log_event("connected", name="http-client"),
            clients=self.http_clients,
        )
        self.closers.append(server)
        log_event("bound", name="http", proto="http", port=bind)

    async def bind_udp(self) -> None:
        bind = await self._bind_port("udp", "udp", self.ports.relay_udp)
        if bind is None:
            return
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpReceiver(self.counters.udp), local_addr=(BIND_HOST, bind)
        )
        self.closers.append(transport)
        log_event("bound", name="udp", proto="udp", port=bind)

    # -- pre links ------------------------------------------------------

    async def run_stats(self, n: int, handled: asyncio.Event) -> None:
        """Keep a TCP link to sink ``n``'s stats port, pinging every second.

        ``handled`` is set once the link first connects, or straight away
        when a fault means it is never meant to (so ready is not held up).
        """
        name = f"stats@{n}"
        faults = self.cfg.faults
        await asyncio.sleep(faults.delay_connect)
        target = faults.connect_port(name, self.ports.sink_stats(n))
        if target is None:
            log_event("connect_skipped", name=name)
            handled.set()
            return
        if name in faults.wrong_connect:
            handled.set()
        announce = True
        while True:
            if announce:
                log_event(
                    "connecting", name=name, proto="tcp", host=SINK_HOST, port=target
                )
            try:
                reader, writer = await asyncio.open_connection(SINK_HOST, target)
            except OSError:
                announce = False
                await asyncio.sleep(RETRY_PERIOD)
                continue
            log_event("connected", name=name)
            handled.set()
            try:
                await self._ping_loop(name, reader, writer)
            except (OSError, ValueError):
                # The sink went away or stalled (OSError covers ConnectionError,
                # TimeoutError and stray EHOSTUNREACH/ETIMEDOUT), or sent an
                # over-long line (ValueError).
                pass
            finally:
                writer.close()
            log_event("disconnected", name=name)
            announce = True
            await asyncio.sleep(RETRY_PERIOD)

    async def _ping_loop(
        self, name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """PING every second and record PONG counts; return on EOF."""
        while True:
            writer.write(b"PING\n")
            await writer.drain()
            async with asyncio.timeout(PONG_TIMEOUT):
                line = await reader.readline()
            if not line:
                return
            parts = line.split()
            if len(parts) == 2 and parts[0] == b"PONG" and parts[1].isdigit():
                self.counters.pongs[name] = int(parts[1])
            await asyncio.sleep(PING_PERIOD)

    async def run_router(self, sock: zmq.asyncio.Socket) -> None:
        """Answer ``status`` (plain or ``{"cmd": "status"}``) from DEALER/REQ peers."""
        while True:
            frames = await sock.recv_multipart()
            if len(frames) < 2:
                continue
            ident, envelope, body = frames[0], frames[1:-1], frames[-1]
            if _command(body) == "status":
                reply = self.status()
            else:
                reply = {
                    "ok": False,
                    "error": "unknown command",
                    "boot_id": self.boot_id,
                }
            await sock.send_multipart([ident, *envelope, json.dumps(reply).encode()])

    async def run_pub(self, sock: zmq.asyncio.Socket) -> None:
        """Publish ``tick <n> <boot_id>`` every second."""
        while True:
            await asyncio.sleep(TICK_PERIOD)
            self.counters.ticks += 1
            message = f"tick {self.counters.ticks} {self.boot_id}".encode()
            with contextlib.suppress(zmq.Again):  # PUB drops, never blocks
                await sock.send(message, zmq.NOBLOCK)

    async def run_rep(self, sock: zmq.asyncio.Socket, tg: asyncio.TaskGroup) -> None:
        while True:
            body = await sock.recv()
            request = _parse(body)
            cmd = request.get("cmd") if request is not None else None
            if cmd == "configure" and self.cfg.faults.ignore_config:
                log_event("config_ignored")
                # A REP socket cannot take another request before replying,
                # so with this fault the REP side is stuck for good.
                await asyncio.Event().wait()
            if request is None:
                reply = self._error("request is not a JSON object")
            elif cmd == "configure":
                reply = await self.configure(request.get("rate_hz"), tg)
            elif cmd == "status":
                reply = self.status()
            else:
                reply = self._error(f"unknown cmd: {cmd!r}")
            await sock.send(json.dumps(reply).encode())

    def _error(self, message: str) -> dict[str, object]:
        return {"ok": False, "error": message, "boot_id": self.boot_id}

    # -- configure and post links ----------------------------------------

    async def configure(self, rate: object, tg: asyncio.TaskGroup) -> dict[str, object]:
        """Start the post links (once) and return the reply.

        Returns once ``pull`` is bound and UDP sending has started; ``hw`` is
        connected in the background and never waited for.
        """
        if isinstance(rate, bool) or not isinstance(rate, int):
            return self._error("rate_hz must be an integer")
        if not 1 <= rate <= MAX_RATE_HZ:
            return self._error(f"rate_hz must be 1..{MAX_RATE_HZ}")
        log_event("configuring", rate_hz=rate)
        async with self._configure_lock:
            if self.rate_hz is None:
                pull = await self.bind_zmq("pull", zmq.PULL, self.ports.pull)
                if pull is not None:
                    tg.create_task(self.run_pull(pull))
                started = [asyncio.Event() for _ in range(self.cfg.sink_count)]
                for n, event in enumerate(started, start=1):
                    tg.create_task(self.send_udp(n, rate, event))
                tg.create_task(self.run_hw(rate))
                for event in started:
                    await event.wait()
                self.rate_hz = rate
        log_event("configured", rate_hz=self.rate_hz)
        return {"ok": True, "rate_hz": self.rate_hz, "boot_id": self.boot_id}

    async def run_pull(self, sock: zmq.asyncio.Socket) -> None:
        """Count sink stats messages and keep the latest from each sink."""
        while True:
            message = _parse(await sock.recv())
            self.counters.pull += 1
            if message is not None and "inst" in message:
                self.counters.sinks[str(message["inst"])] = message

    async def send_udp(self, n: int, rate: int, started: asyncio.Event) -> None:
        """Send datagrams to sink ``n`` at ``rate`` Hz; set ``started`` first."""
        name = f"udp@{n}"
        faults = self.cfg.faults
        target = faults.connect_port(name, self.ports.sink_udp(n))
        if target is None:
            log_event("connect_skipped", name=name)
            started.set()
            return
        loop = asyncio.get_running_loop()
        addr: tuple[str, int] | None
        if n == 1:  # connected: the kernel fixes the peer, sendto has no address
            transport, _ = await loop.create_datagram_endpoint(
                _UdpSender, remote_addr=(SINK_HOST, target)
            )
            addr = None
        else:  # unconnected: every sendto names the peer
            transport, _ = await loop.create_datagram_endpoint(
                _UdpSender, family=socket.AF_INET
            )
            addr = (SINK_HOST, target)
        self.counters.sent[name] = 0
        log_event(
            "sending",
            name=name,
            proto="udp",
            host=SINK_HOST,
            port=target,
            connected=int(addr is None),
        )
        started.set()
        period = 1.0 / rate
        try:
            try:
                async with asyncio.timeout(faults.stop_sending_after):
                    seq = 0
                    due = loop.time()
                    while True:
                        transport.sendto(pack_datagram(n, seq), addr)
                        self.counters.sent[name] = seq = seq + 1
                        due += period
                        now = loop.time()
                        if now - due > MAX_LAG:
                            due = now
                        await asyncio.sleep(max(0.0, due - now))
            except TimeoutError:
                # The stop_sending_after fault: only the traffic stops. The
                # socket stays open (and connected, for udp@1) until shutdown.
                log_event("sending_stopped", name=name, reason="fault")
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                log_event("sending_stopped", name=name, reason="shutdown")
                raise
        finally:
            transport.close()

    async def run_hw(self, rate: int) -> None:
        """Connect to hwsim (retrying every second), send ``start <rate>``."""
        faults = self.cfg.faults
        await asyncio.sleep(faults.delay_connect)
        target = faults.connect_port("hw", self.ports.hw_ctrl)
        if target is None:
            log_event("connect_skipped", name="hw")
            return
        host = self.cfg.hw_host
        announce = True
        while True:
            if announce:
                log_event("connecting", name="hw", proto="tcp", host=host, port=target)
            try:
                reader, writer = await asyncio.open_connection(host, target)
            except OSError:
                announce = False
                await asyncio.sleep(RETRY_PERIOD)
                continue
            log_event("connected", name="hw")
            self.counters.hw_connected = True
            try:
                writer.write(f"start {rate}\n".encode())
                await writer.drain()
                await reader.read()  # hwsim sends nothing; wait for EOF
            except OSError:
                pass  # hwsim went away (reset, EHOSTUNREACH, ETIMEDOUT, ...)
            finally:
                self.counters.hw_connected = False
                writer.close()
            log_event("disconnected", name="hw")
            announce = True
            await asyncio.sleep(RETRY_PERIOD)

    # -- shutdown -------------------------------------------------------

    def close(self) -> None:
        for writer in list(self.http_clients):
            writer.close()
        for closer in self.closers:
            closer.close()
        for sock in self.sockets:
            sock.close(linger=0)


def _parse(body: bytes) -> dict[str, object] | None:
    """``body`` as a JSON object, or None."""
    try:
        value = json.loads(body)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _command(body: bytes) -> object:
    """The command in a ROUTER request: plain text, or ``{"cmd": ...}``."""
    if body.strip() == b"status":
        return "status"
    request = _parse(body)
    return None if request is None else request.get("cmd")


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
    with _zmq_context() as ctx:
        relay = Relay(cfg, ctx)
        p = relay.ports
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(run_fault_timers(cfg.faults))
                rep = await relay.bind_zmq("rep", zmq.REP, p.rep)
                router = await relay.bind_zmq("router", zmq.ROUTER, p.router)
                pub = await relay.bind_zmq("pub", zmq.PUB, p.pub)
                await relay.bind_http()
                await relay.bind_udp()
                if rep is not None:
                    tg.create_task(relay.run_rep(rep, tg))
                if router is not None:
                    tg.create_task(relay.run_router(router))
                if pub is not None:
                    tg.create_task(relay.run_pub(pub))
                handled = [asyncio.Event() for _ in range(cfg.sink_count)]
                for n, event in enumerate(handled, start=1):
                    tg.create_task(relay.run_stats(n, event))
                for event in handled:
                    await event.wait()
                await announce_ready(cfg.faults)
        finally:
            relay.close()


@exit_on_config_error
def main() -> None:
    cfg = load_config(dict(os.environ))
    setup_logging("relay")
    run_service(lambda: _run(cfg))


if __name__ == "__main__":
    main()
