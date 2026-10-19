"""``scdemo-hwsim``: simulated detector hardware, started over TCP, sending UDP."""

import asyncio
import contextlib
import math
import os
import socket
from dataclasses import dataclass
from typing import Final, Literal

from scdemo.env import Env, exit_on_config_error
from scdemo.faults import Faults, announce_ready, run_fault_timers
from scdemo.logs import log_event, setup_logging
from scdemo.protocol import pack_datagram, ports
from scdemo.service import run_service

LISTENERS: Final = frozenset({"ctrl"})
EDGES: Final = frozenset({"udp"})
MAX_RATE_HZ: Final = 10_000.0
MAX_LAG: Final = 1.0  # seconds behind schedule before pacing resets, not bursts


@dataclass(frozen=True, slots=True)
class Config:
    port_base: int
    hw_host: str
    relay_host: str
    faults: Faults


StopReason = Literal["fault", "stop", "disconnect", "shutdown"]


@dataclass(slots=True)
class _Stopping:
    """Why the sender is being cancelled; ``shutdown`` unless a client said."""

    reason: StopReason = "shutdown"


def load_config(environ: dict[str, str]) -> Config:
    env = Env(environ)
    return Config(
        port_base=env.int("SCDEMO_PORT_BASE", 20000),
        hw_host=env.str("SCDEMO_HW_HOST", "127.0.0.2"),
        relay_host=env.str("SCDEMO_RELAY_HOST", "127.0.0.1"),
        faults=Faults.from_env(env, LISTENERS, EDGES),
    )


def parse_start(line: str) -> float | None:
    """The rate in Hz from a ``start <rate_hz>`` line, or ``None`` if invalid."""
    parts = line.split()
    if len(parts) != 2 or parts[0] != "start":
        return None
    try:
        rate = float(parts[1])
    except ValueError:
        return None
    if not math.isfinite(rate) or rate <= 0 or rate > MAX_RATE_HZ:
        return None
    return rate


async def _send_udp(cfg: Config, rate: float, stopping: _Stopping) -> None:
    """Send datagrams at ``rate`` Hz until cancelled or ``stop_sending_after``.

    ``sending_stopped`` carries ``reason=``: ``fault`` for stop_sending_after,
    otherwise ``stopping.reason`` as set by whoever cancelled the sender.
    """
    faults = cfg.faults
    target = faults.connect_port("udp", ports(cfg.port_base).relay_udp)
    if target is None:
        log_event("connect_skipped", name="udp")
        return
    loop = asyncio.get_running_loop()
    period = 1.0 / rate
    # Unconnected, so an absent relay (ICMP port unreachable) never raises.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((cfg.hw_host, 0))
        sock.setblocking(False)
        log_event("sending", name="udp", proto="udp", host=cfg.relay_host, port=target)
        try:
            async with asyncio.timeout(faults.stop_sending_after):
                seq = 0
                due = loop.time()
                while True:
                    with contextlib.suppress(OSError):  # relay absent or buffer full
                        await loop.sock_sendto(
                            sock, pack_datagram(0, seq), (cfg.relay_host, target)
                        )
                    seq += 1
                    due += period
                    now = loop.time()
                    if now - due > MAX_LAG:
                        due = now
                    await asyncio.sleep(max(0.0, due - now))
        except TimeoutError:
            stopping.reason = "fault"  # the stop_sending_after fault fired
        finally:
            log_event("sending_stopped", name="udp", reason=stopping.reason)


async def _serve_client(cfg: Config, reader: asyncio.StreamReader) -> None:
    sender: asyncio.Task[None] | None = None
    stopping = _Stopping()
    async with asyncio.TaskGroup() as tg:
        try:
            try:
                async for raw in reader:
                    line = raw.decode(errors="replace").strip()
                    if line == "stop":
                        if sender is not None:
                            stopping.reason = "stop"
                            sender.cancel()
                            sender = None
                    elif line.startswith("start"):
                        rate = parse_start(line)
                        if rate is not None and (sender is None or sender.done()):
                            stopping = _Stopping()
                            sender = tg.create_task(_send_udp(cfg, rate, stopping))
            except (ConnectionError, ValueError):
                # Caught here, inside the TaskGroup body: raised past the group
                # it would arrive wrapped in an ExceptionGroup. ConnectionError
                # is the client going away; ValueError is an over-long line
                # (stream limit), after which the client is dropped.
                pass
            # Reached only when the client is gone (EOF, reset or dropped);
            # cancellation by SIGTERM skips this and keeps reason=shutdown.
            stopping.reason = "disconnect"
        finally:
            if sender is not None:
                sender.cancel()


async def _bind_ctrl(cfg: Config, busy: asyncio.Lock) -> asyncio.Server | None:
    faults = cfg.faults
    await asyncio.sleep(faults.delay_bind)
    port = ports(cfg.port_base).hw_ctrl
    log_event("binding", name="ctrl", proto="tcp", port=port)
    bind = faults.bind_port("ctrl", port)
    if bind is None:
        log_event("bind_skipped", name="ctrl", proto="tcp", port=port)
        return None

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if busy.locked():  # one client at a time: turn the others away
            writer.close()
            return
        async with busy:
            log_event("connected", name="ctrl-client")
            try:
                await _serve_client(cfg, reader)
            finally:
                log_event("disconnected", name="ctrl-client")
                writer.close()

    server = await asyncio.start_server(handle, cfg.hw_host, bind)
    log_event("bound", name="ctrl", proto="tcp", host=cfg.hw_host, port=bind)
    return server


async def _run(cfg: Config) -> None:
    log_event("starting", port_base=cfg.port_base)
    server: asyncio.Server | None = None
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(run_fault_timers(cfg.faults))
            server = await _bind_ctrl(cfg, asyncio.Lock())
            await announce_ready(cfg.faults)
    finally:
        if server is not None:
            server.close()


@exit_on_config_error
def main() -> None:
    cfg = load_config(dict(os.environ))
    setup_logging("hwsim")
    run_service(lambda: _run(cfg))


if __name__ == "__main__":
    main()
