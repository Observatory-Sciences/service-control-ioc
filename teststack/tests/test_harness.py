import asyncio
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from harness import ServiceProc, find_port_base, range_is_free

HERE = str(Path(__file__).parent)


def probe(port_base: int, **env: str) -> ServiceProc:
    return ServiceProc(
        "probe", port_base, module="_probe_service", PYTHONPATH=HERE, **env
    )


def test_port_base_range_is_free(port_base: int) -> None:
    assert 30000 <= port_base <= 59000
    for offset in (0, 105, 210):
        with socket.socket() as s:
            s.bind(("127.0.0.1", port_base + offset))


@pytest.mark.asyncio
async def test_wait_event_and_env(port_base: int) -> None:
    async with probe(port_base) as proc:
        event = await proc.wait_event("starting", svc="probe")
        assert event["hw_host"] == "127.0.0.1"
        assert event["base"] == str(port_base)
        ready = await proc.wait_event("ready")
        assert ready["note"] == "two words"
        assert [e["event"] for e in proc.events] == ["starting", "ready"]


@pytest.mark.asyncio
async def test_env_override_beats_default(port_base: int) -> None:
    async with probe(port_base, SCDEMO_HW_HOST="127.0.0.2") as proc:
        assert (await proc.wait_event("starting"))["hw_host"] == "127.0.0.2"


@pytest.mark.asyncio
async def test_wait_event_times_out(port_base: int) -> None:
    async with probe(port_base) as proc:
        await proc.wait_event("ready")
        with pytest.raises(TimeoutError, match="nothing"):
            await proc.wait_event("nothing", timeout=0.3)
        with pytest.raises(TimeoutError):
            await proc.wait_event("ready", timeout=0.3, svc="other")


@pytest.mark.asyncio
async def test_sigterm_logs_shutdown_and_exits_zero(port_base: int) -> None:
    async with probe(port_base) as proc:
        await proc.wait_event("ready")
        async with asyncio.timeout(2):
            code = await proc.terminate()
        assert code == 0
        assert proc.events[-1]["event"] == "shutdown"
        assert proc.events[-1]["svc"] == "probe"


@pytest.mark.asyncio
async def test_exit_code_is_returned(port_base: int) -> None:
    async with probe(port_base, SCDEMO_PROBE_MODE="exit3") as proc:
        await proc.wait_event("ready")
        assert await proc.wait_exit() == 3


@pytest.mark.asyncio
async def test_exit_kills_a_running_process(port_base: int) -> None:
    async with probe(port_base) as proc:
        await proc.wait_event("ready")
    assert proc.returncode is not None
    assert proc.returncode != 0


@pytest.mark.asyncio
async def test_default_module_is_scdemo_service(port_base: int) -> None:
    # No scdemo.nosuch module: python exits 1 and the harness reports it.
    async with ServiceProc("nosuch", port_base) as proc:
        assert await proc.wait_exit() == 1
        assert any("nosuch" in line for line in proc.stderr_lines)


@pytest.mark.asyncio
async def test_terminate_returns_after_all_output_is_read(port_base: int) -> None:
    async with probe(port_base, SCDEMO_PROBE_MODE="burst") as proc:
        await proc.wait_event("ready")
        assert await proc.terminate() == 0
        bursts = [e for e in proc.events if e["event"] == "burst"]
        assert len(bursts) == 500
        assert proc.events[-1]["event"] == "shutdown"


def test_find_port_base_gives_up() -> None:
    with pytest.raises(RuntimeError, match="3 attempts"):
        find_port_base(attempts=3, is_free=lambda _base: False)


def test_range_is_free_sees_a_used_port(port_base: int) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", port_base + 50))
        assert range_is_free(port_base) is False
    assert range_is_free(port_base) is True


def test_find_port_base_survives_a_tiny_fd_limit() -> None:
    # Holding all sockets at once used to hit EMFILE and spin forever.
    code = (
        "import resource\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (24, 24))\n"
        "from harness import find_port_base\n"
        "assert 30000 <= find_port_base() <= 59000\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={"PYTHONPATH": HERE},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
