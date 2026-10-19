import asyncio
import contextlib
import socket
import struct
from collections.abc import AsyncGenerator, Iterator

import pytest

from harness import ServiceProc
from scdemo.protocol import unpack_datagram


@pytest.fixture
def relay_rx(port_base: int) -> Iterator[socket.socket]:
    """A UDP receiver standing in for the relay's ``udp`` listener."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", port_base + 105))
    rx.setblocking(False)
    try:
        yield rx
    finally:
        rx.close()


async def _recv(rx: socket.socket, wait: float = 2) -> bytes:
    async with asyncio.timeout(wait):
        return await asyncio.get_running_loop().sock_recv(rx, 64)


async def _quiet(rx: socket.socket, period: float = 0.4) -> bool:
    """True if nothing arrives within ``period`` (after draining the backlog)."""
    while True:
        try:
            rx.recv(64)
        except BlockingIOError:
            break
    try:
        await _recv(rx, period)
    except TimeoutError:
        return True
    return False


@contextlib.asynccontextmanager
async def _client(
    port_base: int,
) -> AsyncGenerator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    r, w = await asyncio.open_connection("127.0.0.1", port_base + 200)
    try:
        yield r, w
    finally:
        w.close()
        with contextlib.suppress(ConnectionError):
            await w.wait_closed()


@pytest.mark.asyncio
async def test_hwsim_sends_after_start(port_base: int, relay_rx: socket.socket) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"start 50\n")
            await hw.wait_event("sending", name="udp")
            data = await _recv(relay_rx)
            assert unpack_datagram(data)[0] == 0
            seqs = [unpack_datagram(await _recv(relay_rx))[1] for _ in range(3)]
            assert seqs == sorted(seqs)
            await hw.wait_event("connected", name="ctrl-client")


@pytest.mark.asyncio
async def test_hwsim_stop_command_stops_datagrams(
    port_base: int, relay_rx: socket.socket
) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"start 100\n")
            await _recv(relay_rx)
            w.write(b"stop\n")
            await hw.wait_event("sending_stopped", reason="stop")
            assert await _quiet(relay_rx)


@pytest.mark.asyncio
async def test_hwsim_client_disconnect_stops_sending(
    port_base: int, relay_rx: socket.socket
) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"start 100\n")
            await _recv(relay_rx)
        await hw.wait_event("disconnected", name="ctrl-client")
        await hw.wait_event("sending_stopped", reason="disconnect")
        assert await _quiet(relay_rx)


@pytest.mark.asyncio
async def test_hwsim_sigterm_clean_exit_while_sending(
    port_base: int, relay_rx: socket.socket
) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"start 100\n")
            await _recv(relay_rx)
            async with asyncio.timeout(2):
                assert await hw.terminate() == 0
    assert hw.events[-1]["event"] == "shutdown"
    stopped = [e for e in hw.events if e["event"] == "sending_stopped"]
    assert [e["reason"] for e in stopped] == ["shutdown"]
    assert hw.other_stdout == []
    assert hw.stderr_lines == []


@pytest.mark.asyncio
async def test_hwsim_stop_sending_after_fault(
    port_base: int, relay_rx: socket.socket
) -> None:
    async with ServiceProc(
        "hwsim", port_base, SCDEMO_FAULT_STOP_SENDING_AFTER="0.3"
    ) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"start 100\n")
            await _recv(relay_rx)
            await hw.wait_event("sending_stopped", reason="fault")
            assert await _quiet(relay_rx)


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["abc", "0", "-5", "nan", "inf", "10001", ""])
async def test_hwsim_bad_start_is_ignored(
    port_base: int, relay_rx: socket.socket, arg: str
) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(f"start {arg}\n".encode())
            assert await _quiet(relay_rx)
            w.write(b"start 100\n")  # same connection still works
            await hw.wait_event("sending", name="udp")
            await _recv(relay_rx)
        assert hw.returncode is None


@pytest.mark.asyncio
async def test_hwsim_keeps_running_without_relay(port_base: int) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"start 200\n")
            await hw.wait_event("sending", name="udp")
            await asyncio.sleep(0.5)
            assert hw.returncode is None
        assert hw.stderr_lines == []
        assert hw.other_stdout == []


@pytest.mark.asyncio
async def test_hwsim_second_client_is_rejected(port_base: int) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r1, _w1):
            await hw.wait_event("connected", name="ctrl-client")
            async with _client(port_base) as (r2, _w2):
                async with asyncio.timeout(2):
                    assert await r2.read() == b""  # closed by hwsim
            assert hw.returncode is None


@pytest.mark.asyncio
async def test_hwsim_no_connect_fault_sends_nothing(
    port_base: int, relay_rx: socket.socket
) -> None:
    async with ServiceProc("hwsim", port_base, SCDEMO_FAULT_NO_CONNECT="udp") as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"start 100\n")
            await hw.wait_event("connect_skipped", name="udp")
            assert await _quiet(relay_rx)


async def _assert_clean_exit(hw: ServiceProc) -> None:
    """SIGTERM, then check exit 0 and no traceback or stray output (after EOF)."""
    assert await hw.terminate() == 0
    assert hw.other_stdout == []
    assert hw.stderr_lines == []
    assert hw.events[-1]["event"] == "shutdown"


@pytest.mark.asyncio
async def test_hwsim_overlong_line_no_traceback(port_base: int) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"x" * (70 * 1024))
            await w.drain()
            await hw.wait_event("disconnected", name="ctrl-client")
            await _assert_clean_exit(hw)


@pytest.mark.asyncio
async def test_hwsim_overlong_line_while_sending_stops_sender(
    port_base: int, relay_rx: socket.socket
) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"start 100\n")
            await _recv(relay_rx)
            w.write(b"x" * (70 * 1024))
            await hw.wait_event("sending_stopped", reason="disconnect")
            await hw.wait_event("disconnected", name="ctrl-client")
            assert await _quiet(relay_rx)
            await _assert_clean_exit(hw)


@pytest.mark.asyncio
async def test_hwsim_client_reset_no_traceback(
    port_base: int, relay_rx: socket.socket
) -> None:
    async with ServiceProc("hwsim", port_base) as hw:
        await hw.wait_event("ready")
        with socket.create_connection(("127.0.0.1", port_base + 200)) as c:
            c.sendall(b"start 100\n")
            await _recv(relay_rx)
            # Linger 0: close() sends RST instead of FIN.
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        await hw.wait_event("disconnected", name="ctrl-client")
        await hw.wait_event("sending_stopped", reason="disconnect")
        await _assert_clean_exit(hw)


@pytest.mark.asyncio
async def test_hwsim_wrong_connect_fault_uses_wrong_port(
    port_base: int, relay_rx: socket.socket
) -> None:
    async with ServiceProc("hwsim", port_base, SCDEMO_FAULT_WRONG_CONNECT="udp") as hw:
        await hw.wait_event("ready")
        async with _client(port_base) as (_r, w):
            w.write(b"start 100\n")
            await hw.wait_event("sending", name="udp", port=str(port_base + 1105))
            assert await _quiet(relay_rx)
