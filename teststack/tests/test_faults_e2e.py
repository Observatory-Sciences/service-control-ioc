"""Timer and delay faults end to end, on the sink (the cheapest service)."""

import asyncio

import pytest

from harness import ServiceProc

DELAY = 0.6
MARGIN = 0.1  # pipe and scheduling jitter on the measured interval


def _sink(port_base: int, **env: str) -> ServiceProc:
    return ServiceProc("sink", port_base, SCDEMO_INSTANCE="1", **env)


@pytest.mark.asyncio
async def test_crash_after_logs_crash_and_exits_3(port_base: int) -> None:
    async with _sink(port_base, SCDEMO_FAULT_CRASH_AFTER="0.3") as sink:
        await sink.wait_event("ready")
        await sink.wait_event("crash", timeout=3)
        assert await sink.wait_exit() == 3
        assert sink.events[-1]["event"] == "crash"
        assert not any(e["event"] == "shutdown" for e in sink.events)


@pytest.mark.asyncio
async def test_spin_after_still_shuts_down_on_sigterm(port_base: int) -> None:
    async with _sink(port_base, SCDEMO_FAULT_SPIN_AFTER="0.2") as sink:
        await sink.wait_event("spinning", timeout=3)
        async with asyncio.timeout(2):
            assert await sink.terminate() == 0
        assert sink.events[-1]["event"] == "shutdown"


async def _interval(proc: ServiceProc, event: str) -> float:
    """Seconds between ``starting`` and ``event`` arriving, as seen here."""
    loop = asyncio.get_running_loop()
    await proc.wait_event("starting")
    start = loop.time()
    await proc.wait_event(event, timeout=5)
    return loop.time() - start


@pytest.mark.asyncio
async def test_delay_ready_holds_ready_back(port_base: int) -> None:
    async with _sink(port_base, SCDEMO_DELAY_READY=str(DELAY)) as sink:
        assert await _interval(sink, "ready") >= DELAY - MARGIN
        bound = [e["name"] for e in sink.events if e["event"] == "bound"]
        assert bound == ["udp", "stats"]  # binds were not delayed


@pytest.mark.asyncio
async def test_delay_bind_holds_each_bind_back(port_base: int) -> None:
    async with _sink(port_base, SCDEMO_DELAY_BIND=str(DELAY)) as sink:
        loop = asyncio.get_running_loop()
        await sink.wait_event("starting")
        start = loop.time()
        await sink.wait_event("bound", name="udp", timeout=5)
        assert loop.time() - start >= DELAY - MARGIN
        # The stats bind waits its own delay after the udp bind.
        await sink.wait_event("bound", name="stats", timeout=5)
        assert loop.time() - start >= 2 * DELAY - MARGIN


@pytest.mark.asyncio
async def test_delay_connect_holds_push_back(port_base: int) -> None:
    async with _sink(port_base, SCDEMO_DELAY_CONNECT=str(DELAY)) as sink:
        loop = asyncio.get_running_loop()
        await sink.wait_event("ready")
        ready = loop.time()
        await sink.wait_event("connecting", name="push", timeout=5)
        assert loop.time() - ready >= DELAY - MARGIN
