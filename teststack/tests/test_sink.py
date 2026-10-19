import asyncio
import socket

import pytest

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
