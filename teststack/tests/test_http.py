import asyncio
import contextlib
import logging
import socket
import struct
from collections.abc import AsyncGenerator

import pytest

from scdemo.http import HttpClient, HttpError, serve_http


def _status_handler(path: str) -> dict[str, object]:
    if path == "/missing":
        raise LookupError(path)
    return {"path": path}


@contextlib.asynccontextmanager
async def _raw(
    port: int,
) -> AsyncGenerator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        yield r, w
    finally:
        w.close()
        with contextlib.suppress(ConnectionError):
            await w.wait_closed()


async def _read_response(r: asyncio.StreamReader) -> tuple[str, bytes]:
    """Status line and body of one response (Content-Length framed)."""
    async with asyncio.timeout(5):
        status = (await r.readline()).decode().strip()
        length = 0
        while (line := await r.readline()) not in (b"\r\n", b""):
            name, _, value = line.decode().partition(":")
            if name.lower() == "content-length":
                length = int(value)
        return status, await r.readexactly(length)


@pytest.mark.asyncio
async def test_http_client_reuses_connection(port_base):
    accepted = []
    server = await serve_http(
        "127.0.0.1",
        port_base + 104,
        lambda path: {"path": path},
        lambda: accepted.append(1),
    )
    async with server:
        client = HttpClient("127.0.0.1", port_base + 104)
        for _ in range(3):
            assert (await client.get_json("/status"))["path"] == "/status"
        assert client.connections_opened == 1
        assert len(accepted) == 1
        await client.close()


@pytest.mark.asyncio
async def test_http_server_keep_alive_on_one_connection(port_base):
    accepted = []
    server = await serve_http(
        "127.0.0.1", port_base + 104, _status_handler, lambda: accepted.append(1)
    )
    async with server, _raw(port_base + 104) as (r, w):
        for n in range(3):
            w.write(f"GET /s{n} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            status, body = await _read_response(r)
            assert status == "HTTP/1.1 200 OK"
            assert body == f'{{"path": "/s{n}"}}'.encode()
        assert len(accepted) == 1


@pytest.mark.asyncio
async def test_http_unknown_path_is_404_and_connection_kept(port_base):
    server = await serve_http(
        "127.0.0.1", port_base + 104, _status_handler, lambda: None
    )
    async with server, _raw(port_base + 104) as (r, w):
        w.write(b"GET /missing HTTP/1.1\r\n\r\n")
        assert (await _read_response(r))[0] == "HTTP/1.1 404 Not Found"
        w.write(b"GET /ok HTTP/1.1\r\n\r\n")
        assert (await _read_response(r))[0] == "HTTP/1.1 200 OK"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_bytes",
    [
        b"garbage\r\n\r\n",
        b"GET /x\r\n\r\n",
        b"GET /x HTTP/1.1\r\nno-colon-header\r\n\r\n",
        b"GET /x HTTP/1.1\r\nContent-Length: abc\r\n\r\n",
        b"GET /\xff HTTP/1.1\r\n\r\n",
        b"GET /" + b"x" * (70 * 1024) + b" HTTP/1.1\r\n\r\n",
        b"GET /x HTTP/1.1\r\nX: " + b"y" * (70 * 1024) + b"\r\n\r\n",
    ],
    ids=[
        "garbage",
        "no-version",
        "bad-header",
        "bad-length",
        "non-ascii",
        "long-line",
        "long-header",
    ],
)
async def test_http_bad_request_gets_400_and_close(
    port_base, request_bytes, caplog: pytest.LogCaptureFixture
):
    server = await serve_http(
        "127.0.0.1", port_base + 104, _status_handler, lambda: None
    )
    async with server:
        async with _raw(port_base + 104) as (r, w):
            w.write(request_bytes)
            assert (await _read_response(r))[0] == "HTTP/1.1 400 Bad Request"
            async with asyncio.timeout(5):
                assert await r.read() == b""  # server closed it
        # The server still serves new clients.
        client = HttpClient("127.0.0.1", port_base + 104)
        assert (await client.get_json("/ok"))["path"] == "/ok"
        await client.close()
    assert [rec for rec in caplog.records if rec.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_http_too_many_headers_gets_400(port_base):
    server = await serve_http(
        "127.0.0.1", port_base + 104, _status_handler, lambda: None
    )
    headers = b"".join(f"X-{n}: 1\r\n".encode() for n in range(200))
    async with server, _raw(port_base + 104) as (r, w):
        w.write(b"GET /x HTTP/1.1\r\n" + headers + b"\r\n")
        assert (await _read_response(r))[0] == "HTTP/1.1 400 Bad Request"


@pytest.mark.asyncio
async def test_http_connection_close_is_honoured(port_base):
    server = await serve_http(
        "127.0.0.1", port_base + 104, _status_handler, lambda: None
    )
    async with server, _raw(port_base + 104) as (r, w):
        w.write(b"GET /x HTTP/1.1\r\nConnection: close\r\n\r\n")
        assert (await _read_response(r))[0] == "HTTP/1.1 200 OK"
        async with asyncio.timeout(5):
            assert await r.read() == b""


@pytest.mark.asyncio
async def test_http_client_reset_is_quiet(port_base, caplog: pytest.LogCaptureFixture):
    accepted = asyncio.Event()
    server = await serve_http(
        "127.0.0.1", port_base + 104, _status_handler, accepted.set
    )
    async with server:
        with socket.create_connection(("127.0.0.1", port_base + 104)) as c:
            c.sendall(b"GET /x HTTP/1.1\r\n")  # half a request, then RST
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        async with asyncio.timeout(5):
            await accepted.wait()
        client = HttpClient("127.0.0.1", port_base + 104)
        assert (await client.get_json("/ok"))["path"] == "/ok"
        await client.close()
    assert [rec for rec in caplog.records if rec.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_http_client_reconnects_after_server_closes(port_base):
    clients: set[asyncio.StreamWriter] = set()
    server = await serve_http(
        "127.0.0.1", port_base + 104, _status_handler, lambda: None, clients=clients
    )
    async with server:
        client = HttpClient("127.0.0.1", port_base + 104)
        await client.get_json("/a")
        assert len(clients) == 1
        for writer in list(clients):
            writer.close()
            await writer.wait_closed()
        assert (await client.get_json("/b"))["path"] == "/b"
        assert client.connections_opened == 2
        await client.close()


@pytest.mark.asyncio
async def test_http_client_raises_on_error_status(port_base):
    server = await serve_http(
        "127.0.0.1", port_base + 104, _status_handler, lambda: None
    )
    async with server:
        client = HttpClient("127.0.0.1", port_base + 104)
        with pytest.raises(HttpError, match="404"):
            await client.get_json("/missing")
        assert (await client.get_json("/ok"))["path"] == "/ok"
        assert client.connections_opened == 1
        await client.close()


@pytest.mark.asyncio
async def test_http_client_connect_refused(port_base):
    client = HttpClient("127.0.0.1", port_base + 104)
    with pytest.raises(ConnectionRefusedError):
        await client.get_json("/status")
    await client.close()


@pytest.mark.asyncio
async def test_http_client_overlong_status_line_is_http_error(port_base):
    """A peer streaming bytes with no newline: HttpError, not ValueError."""

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readline()
        w.write(b"x" * (128 * 1024))  # over the 64 KiB stream limit, no newline
        with contextlib.suppress(ConnectionError):
            await w.drain()
            await r.read()
        w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port_base + 104)
    async with server:
        client = HttpClient("127.0.0.1", port_base + 104)
        with pytest.raises(HttpError, match="status line"):
            await client.get_json("/status")
        await client.close()
