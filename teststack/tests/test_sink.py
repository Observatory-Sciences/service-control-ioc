import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncGenerator

import pytest
import zmq
import zmq.asyncio

from harness import ServiceProc
from scdemo.protocol import pack_datagram


@pytest.mark.asyncio
async def test_sink_reports_first_datagram_and_gap(port_base: int) -> None:
    async with ServiceProc("sink", port_base, SCDEMO_INSTANCE="1") as sink:
        await sink.wait_event("ready")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            for seq in (0, 1, 2, 5):
                tx.sendto(pack_datagram(1, seq), ("127.0.0.1", port_base + 11))
        await sink.wait_event("first_datagram", seq="0")
        gap = await sink.wait_event("gap_detected")
        assert (gap["expected"], gap["got"], gap["missing"]) == ("3", "5", "2")


@pytest.mark.asyncio
async def test_sink_stats_ping(port_base: int) -> None:
    async with ServiceProc("sink", port_base, SCDEMO_INSTANCE="2") as sink:
        await sink.wait_event("bound", name="stats", port=str(port_base + 22))
        r, w = await asyncio.open_connection("127.0.0.1", port_base + 22)
        try:
            w.write(b"PING\n")
            async with asyncio.timeout(5):
                assert (await r.readline()).startswith(b"PONG ")
            await sink.wait_event("connected", name="stats-client")
        finally:
            w.close()
            await w.wait_closed()


@pytest.mark.asyncio
async def test_sink_no_bind_keeps_running(port_base: int) -> None:
    async with ServiceProc(
        "sink", port_base, SCDEMO_INSTANCE="1", SCDEMO_FAULT_NO_BIND="udp"
    ) as sink:
        await sink.wait_event("bind_skipped", name="udp")
        await sink.wait_event("ready")


@pytest.mark.asyncio
async def test_sink_sigterm_clean_exit(port_base: int) -> None:
    async with ServiceProc("sink", port_base, SCDEMO_INSTANCE="1") as sink:
        await sink.wait_event("ready")
        await sink.wait_event("connecting", name="push")
        async with asyncio.timeout(2):
            assert await sink.terminate() == 0
        assert sink.events[-1]["event"] == "shutdown"


@pytest.mark.asyncio
@pytest.mark.parametrize("instance", [None, "0", "abc"])
async def test_sink_bad_instance_exits_2(port_base: int, instance: str | None) -> None:
    env = {} if instance is None else {"SCDEMO_INSTANCE": instance}
    async with ServiceProc("sink", port_base, **env) as sink:
        assert await sink.wait_exit() == 2
        assert any("SCDEMO_INSTANCE" in line for line in sink.stderr_lines)
        assert not any("Traceback" in line for line in sink.stderr_lines)


@pytest.mark.asyncio
async def test_sink_overlong_line_drops_client_quietly(port_base: int) -> None:
    async with ServiceProc("sink", port_base, SCDEMO_INSTANCE="1") as sink:
        await sink.wait_event("bound", name="stats")
        r, w = await asyncio.open_connection("127.0.0.1", port_base + 21)
        try:
            w.write(b"x" * (128 * 1024))
            async with asyncio.timeout(5):
                await w.drain()
                assert await r.read() == b""  # sink closed the connection
        except ConnectionError:
            pass  # reset instead of a clean close also means it was dropped
        finally:
            w.close()
            with contextlib.suppress(ConnectionError):
                await w.wait_closed()
        r, w = await asyncio.open_connection("127.0.0.1", port_base + 21)
        try:
            w.write(b"PING\n")
            async with asyncio.timeout(5):
                assert (await r.readline()).startswith(b"PONG ")
        finally:
            w.close()
            await w.wait_closed()
        # asyncio logs unhandled callback errors through logging, to stdout.
        noise = [*sink.stderr_lines, *sink.other_stdout]
        assert not any("Traceback" in line or "Unhandled" in line for line in noise)
        assert sink.returncode is None


@contextlib.asynccontextmanager
async def _pull(port: int) -> AsyncGenerator[zmq.asyncio.Socket]:
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.PULL)
    sock.setsockopt(zmq.LINGER, 0)
    sock.bind(f"tcp://127.0.0.1:{port}")
    try:
        yield sock
    finally:
        sock.close(linger=0)
        ctx.term()


@pytest.mark.asyncio
async def test_sink_pushes_stats_and_logs_connected(port_base: int) -> None:
    async with (
        _pull(port_base + 103) as pull,
        ServiceProc("sink", port_base, SCDEMO_INSTANCE="1") as sink,
    ):
        await sink.wait_event("connected", name="push")
        async with asyncio.timeout(5):
            msg = json.loads(await pull.recv())
        assert msg == {"inst": 1, "count": 0, "gaps": 0, "missing": 0}


@pytest.mark.asyncio
async def test_sink_sigterm_after_push_and_peer_gone(port_base: int) -> None:
    async with ServiceProc("sink", port_base, SCDEMO_INSTANCE="1") as sink:
        async with _pull(port_base + 103) as pull:
            await sink.wait_event("connected", name="push")
            async with asyncio.timeout(5):
                await pull.recv()
        await sink.wait_event("disconnected", name="push")
        async with asyncio.timeout(2):
            assert await sink.terminate() == 0
        assert sink.events[-1]["event"] == "shutdown"
