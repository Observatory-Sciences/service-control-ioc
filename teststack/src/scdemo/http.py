"""A minimal HTTP/1.1 JSON server and client, both holding keep-alive connections.

Only what the test stack needs: ``GET`` requests, JSON bodies framed by
``Content-Length``, and persistent connections. Not a general HTTP library.
"""

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Final

MAX_HEADERS: Final = 100
MAX_BODY: Final = 64 * 1024
_REASONS: Final = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
}


class HttpError(Exception):
    """A response was malformed or did not have status 200."""


class _BadRequest(ValueError):
    """The client sent something that is not a request we understand."""


def _response(status: int, body: dict[str, object], *, close: bool) -> bytes:
    payload = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: {'close' if close else 'keep-alive'}\r\n"
        "\r\n"
    )
    return head.encode("ascii") + payload


async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str] | None:
    """Header lines up to the blank line, names lower-cased; None on EOF.

    Raise ``ValueError`` (over-long line, from the stream limit) or
    ``_BadRequest`` for anything malformed.
    """
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADERS + 1):
        line = await reader.readline()
        if not line.endswith(b"\n"):
            return None  # EOF part-way through
        text = line.decode("ascii").strip()  # UnicodeDecodeError is a ValueError
        if not text:
            return headers
        name, sep, value = text.partition(":")
        if not sep or not name.strip():
            raise _BadRequest(f"bad header line: {text!r}")
        headers[name.strip().lower()] = value.strip()
    raise _BadRequest("too many headers")


def _content_length(headers: dict[str, str]) -> int:
    raw = headers.get("content-length", "0")
    if not raw.isdigit() or int(raw) > MAX_BODY:
        raise _BadRequest(f"bad Content-Length: {raw!r}")
    return int(raw)


def _wants_close(version: str, headers: dict[str, str]) -> bool:
    connection = headers.get("connection", "").lower()
    if version == "HTTP/1.0":
        return connection != "keep-alive"
    return connection == "close"


async def _serve_one(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handler: Callable[[str], dict[str, object]],
) -> bool:
    """Answer one request. Return False when the connection should close."""
    line = await reader.readline()
    if not line.endswith(b"\n"):
        return False  # EOF: the client is done
    parts = line.decode("ascii").split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise _BadRequest(f"bad request line: {line[:80]!r}")
    method, path, version = parts
    headers = await _read_headers(reader)
    if headers is None:
        return False
    body_length = _content_length(headers)
    if body_length:
        await reader.readexactly(body_length)  # GET has no use for a body
    close = _wants_close(version, headers)
    if method != "GET":
        writer.write(_response(405, {"error": "only GET"}, close=close))
    else:
        try:
            body = handler(path)
        except LookupError:
            writer.write(_response(404, {"error": "not found"}, close=close))
        else:
            writer.write(_response(200, body, close=close))
    await writer.drain()
    return not close


async def serve_http(
    host: str,
    port: int,
    handler: Callable[[str], dict[str, object]],
    on_connect: Callable[[], None],
    *,
    clients: set[asyncio.StreamWriter] | None = None,
) -> asyncio.Server:
    """Serve ``GET <path>`` as ``handler(path)`` JSON on keep-alive connections.

    ``on_connect`` is called once per accepted TCP connection. A handler that
    raises ``LookupError`` gives a 404. Malformed or over-long requests get a
    400 and the connection is closed; clients that reset are dropped quietly.
    If ``clients`` is given, open connections are kept in it, so the caller
    can close them on shutdown (``Server.close`` leaves them open).
    """
    live = clients if clients is not None else set()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        live.add(writer)
        on_connect()
        try:
            while await _serve_one(reader, writer, handler):
                pass
        except ValueError:
            # A malformed request, or a line over the stream limit (64 KiB).
            with contextlib.suppress(ConnectionError):
                writer.write(_response(400, {"error": "bad request"}, close=True))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # the client went away mid-request
        finally:
            live.discard(writer)
            writer.close()

    return await asyncio.start_server(handle, host, port)


class HttpClient:
    """GET JSON over one keep-alive connection, reopened only once it is closed.

    ``connections_opened`` counts TCP connections made. Calls are serialised,
    so one connection is never shared by two requests at once.
    """

    connections_opened: int

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.connections_opened = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    def _is_open(self) -> bool:
        return (
            self._reader is not None
            and self._writer is not None
            and not self._writer.is_closing()
            and not self._reader.at_eof()
        )

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self._drop()
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self.connections_opened += 1
        self._reader, self._writer = reader, writer
        return reader, writer

    def _drop(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = self._writer = None

    async def get_json(self, path: str) -> dict[str, object]:
        """``GET path`` and return the JSON object body.

        Raise ``HttpError`` for a non-200 or malformed response, and
        ``OSError`` if the server cannot be reached. A request on a reused
        connection that the server had closed is retried once on a new one.
        """
        async with self._lock:
            status, body = await self._exchange(path)
        if status != 200:
            raise HttpError(f"GET {path}: HTTP {status}")
        try:
            data = json.loads(body)
        except ValueError as err:
            raise HttpError(f"GET {path}: body is not JSON") from err
        if not isinstance(data, dict):
            raise HttpError(f"GET {path}: body is not a JSON object")
        return data

    async def _exchange(self, path: str) -> tuple[int, bytes]:
        """One request/response on the kept connection: ``(status, body)``."""
        reused = self._is_open()
        if reused:
            assert self._reader is not None
            assert self._writer is not None
            conn = (self._reader, self._writer)
        else:
            conn = await self._open()
        try:
            return await self._request(*conn, path)
        except (ConnectionError, asyncio.IncompleteReadError):
            self._drop()
            if not reused:
                raise
        except BaseException:
            self._drop()  # a timeout or bad response leaves the stream unusable
            raise
        # The server closed the idle connection under us: one fresh try.
        conn = await self._open()
        try:
            return await self._request(*conn, path)
        except BaseException:
            self._drop()
            raise

    async def _request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str
    ) -> tuple[int, bytes]:
        writer.write(
            f"GET {path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n\r\n".encode()
        )
        await writer.drain()
        status_line = await reader.readline()
        if not status_line:
            raise asyncio.IncompleteReadError(b"", None)
        parts = status_line.decode("ascii", errors="replace").split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise HttpError(f"bad status line: {status_line[:80]!r}")
        try:
            headers = await _read_headers(reader)
        except ValueError as err:
            raise HttpError(f"bad response headers: {err}") from err
        if headers is None:
            raise asyncio.IncompleteReadError(b"", None)
        raw_length = headers.get("content-length")
        if raw_length is None or not raw_length.isdigit():
            raise HttpError(f"missing or bad Content-Length: {raw_length!r}")
        body = await reader.readexactly(int(raw_length))
        if headers.get("connection", "").lower() == "close":
            self._drop()
        return int(parts[1]), body

    async def close(self) -> None:
        """Close the connection, if open."""
        writer = self._writer
        self._drop()
        if writer is not None:
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
