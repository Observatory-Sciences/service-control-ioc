"""``scdemo-sink``: receives UDP datagrams, answers TCP pings, pushes stats over ZMQ."""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Final

import zmq
import zmq.asyncio
from zmq.utils.monitor import parse_monitor_message

from scdemo.env import ConfigError, Env, exit_on_config_error
from scdemo.faults import Faults, announce_ready, run_fault_timers
from scdemo.logs import log_event, setup_logging
from scdemo.protocol import SeqTracker, ports, unpack_datagram
from scdemo.service import run_service

LISTENERS: Final = frozenset({"udp", "stats"})
EDGES: Final = frozenset({"push"})
STATS_PERIOD: Final = 1.0
BIND_HOST: Final = "0.0.0.0"
_MONITOR_EVENTS: Final = zmq.EVENT_CONNECTED | zmq.EVENT_DISCONNECTED


@dataclass(frozen=True, slots=True)
class Config:
    inst: int
    port_base: int
    relay_host: str
    faults: Faults


def load_config(environ: dict[str, str]) -> Config:
    env = Env(environ)
    if "SCDEMO_INSTANCE" not in environ:
        raise ConfigError("SCDEMO_INSTANCE: required")
    inst = env.int("SCDEMO_INSTANCE", 0)
    if inst < 1:
        raise ConfigError(f"SCDEMO_INSTANCE: must be >= 1: {inst}")
    return Config(
        inst=inst,
        port_base=env.int("SCDEMO_PORT_BASE", 20000),
        relay_host=env.str("SCDEMO_RELAY_HOST", "127.0.0.1"),
        faults=Faults.from_env(env, LISTENERS, EDGES),
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
        missing = self.tracker.feed(seq)
        if first:
            log_event("first_datagram", seq=seq)
        if missing is not None:
            log_event("gap_detected", expected=seq - missing, got=seq, missing=missing)


async def _bind_udp(
    cfg: Config, tracker: SeqTracker
) -> asyncio.DatagramTransport | None:
    faults = cfg.faults
    await asyncio.sleep(faults.delay_bind)
    port = ports(cfg.port_base).sink_udp(cfg.inst)
    log_event("binding", name="udp", proto="udp", port=port)
    bind = faults.bind_port("udp", port)
    if bind is None:
        log_event("bind_skipped", name="udp", proto="udp", port=port)
        return None
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _UdpReceiver(tracker), local_addr=(BIND_HOST, bind)
    )
    log_event("bound", name="udp", proto="udp", port=bind)
    return transport


async def _bind_stats(
    cfg: Config, tracker: SeqTracker, writers: set[asyncio.StreamWriter]
) -> asyncio.Server | None:
    faults = cfg.faults
    await asyncio.sleep(faults.delay_bind)
    port = ports(cfg.port_base).sink_stats(cfg.inst)
    log_event("binding", name="stats", proto="tcp", port=port)
    bind = faults.bind_port("stats", port)
    if bind is None:
        log_event("bind_skipped", name="stats", proto="tcp", port=port)
        return None

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writers.add(writer)
        log_event("connected", name="stats-client")
        try:
            async for line in reader:
                if line.strip() == b"PING":
                    writer.write(f"PONG {tracker.count}\n".encode())
                    await writer.drain()
        except ConnectionError:
            pass  # the client went away; nothing to answer
        finally:
            writers.discard(writer)
            writer.close()

    server = await asyncio.start_server(handle, BIND_HOST, bind)
    log_event("bound", name="stats", proto="tcp", port=bind)
    return server


async def _watch_monitor(monitor: zmq.asyncio.Socket) -> None:
    while True:
        msg = parse_monitor_message(await monitor.recv_multipart())
        if msg["event"] == zmq.EVENT_CONNECTED:
            log_event("connected", name="push")
        elif msg["event"] == zmq.EVENT_DISCONNECTED:
            log_event("disconnected", name="push")


async def _push_stats(
    cfg: Config, tracker: SeqTracker, sock: zmq.asyncio.Socket
) -> None:
    while True:
        await asyncio.sleep(STATS_PERIOD)
        payload = {
            "inst": cfg.inst,
            "count": tracker.count,
            "gaps": tracker.gaps,
            "missing": tracker.missing,
        }
        with contextlib.suppress(zmq.Again):  # no peer yet and the queue is full
            await sock.send(json.dumps(payload).encode(), zmq.NOBLOCK)


@contextlib.asynccontextmanager
async def _zmq_push(
    endpoint: str,
) -> AsyncGenerator[tuple[zmq.asyncio.Socket, zmq.asyncio.Socket]]:
    """A PUSH socket connected to ``endpoint`` and its monitor, closed with linger 0."""
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.PUSH)
    sock.setsockopt(zmq.LINGER, 0)
    monitor = sock.get_monitor_socket(_MONITOR_EVENTS)
    try:
        sock.connect(endpoint)
        yield sock, monitor
    finally:
        sock.disable_monitor()
        monitor.close(linger=0)
        sock.close(linger=0)
        ctx.term()


async def _connect_and_push(cfg: Config, tracker: SeqTracker) -> None:
    faults = cfg.faults
    await asyncio.sleep(faults.delay_connect)
    port = ports(cfg.port_base).pull
    log_event("connecting", name="push", proto="tcp", host=cfg.relay_host, port=port)
    target = faults.connect_port("push", port)
    if target is None:
        log_event("connect_skipped", name="push")
        await asyncio.Event().wait()
        return
    async with (
        _zmq_push(f"tcp://{cfg.relay_host}:{target}") as (sock, monitor),
        asyncio.TaskGroup() as tg,
    ):
        tg.create_task(_watch_monitor(monitor))
        tg.create_task(_push_stats(cfg, tracker, sock))


async def _run(cfg: Config) -> None:
    log_event("starting", port_base=cfg.port_base)
    tracker = SeqTracker()
    writers: set[asyncio.StreamWriter] = set()
    transport: asyncio.DatagramTransport | None = None
    server: asyncio.Server | None = None
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(run_fault_timers(cfg.faults))
            transport = await _bind_udp(cfg, tracker)
            server = await _bind_stats(cfg, tracker, writers)
            await announce_ready(cfg.faults)
            tg.create_task(_connect_and_push(cfg, tracker))
    finally:
        if transport is not None:
            transport.close()
        if server is not None:
            server.close()
        for writer in list(writers):
            writer.close()


@exit_on_config_error
def main() -> None:
    cfg = load_config(dict(os.environ))
    setup_logging("sink", cfg.inst)
    run_service(lambda: _run(cfg))


if __name__ == "__main__":
    main()
