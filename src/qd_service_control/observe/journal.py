"""Follow the journal of the managed units with ``journalctl -f -o json``.

This is the project's only subprocess. One follower covers every unit; entries
are demultiplexed by unit into small per-unit ring buffers. If journalctl
exits (for instance because the service user cannot read the journal), the
reason is logged once and the follower is restarted with backoff.
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

log = logging.getLogger(__name__)

READER_LIMIT: Final = 1 << 20  # journal entries can exceed asyncio's 64 KiB default
OUTPUT_FIELDS: Final = (
    "MESSAGE,_SYSTEMD_UNIT,UNIT,OBJECT_SYSTEMD_UNIT,__REALTIME_TIMESTAMP"
)
KILL_AFTER: Final = 5.0  # seconds to wait for SIGTERM before SIGKILL


@dataclass(frozen=True, slots=True)
class JournalLine:
    unit: str
    ts: float
    text: str  # "HH:MM:SS " + message


def decode_message(value: object) -> str:
    """``MESSAGE`` as text: a str, a list of byte values, or None (dropped)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return bytes(v for v in value if isinstance(v, int) and 0 <= v < 256).decode(
            "utf-8", errors="replace"
        )
    return ""


def _timestamp(entry: Mapping[str, object]) -> float:
    raw = entry.get("__REALTIME_TIMESTAMP")
    try:
        return int(str(raw)) / 1e6
    except ValueError:
        return time.time()


def demux_entry(
    entry: Mapping[str, object], units: frozenset[str]
) -> JournalLine | None:
    """Pick the managed unit an entry belongs to, or None to drop it.

    A unit's own lines carry ``_SYSTEMD_UNIT``. systemd's lines about a unit
    come from ``init.scope`` and name it in ``UNIT`` or ``OBJECT_SYSTEMD_UNIT``.
    """
    for key in ("_SYSTEMD_UNIT", "UNIT", "OBJECT_SYSTEMD_UNIT"):
        value = entry.get(key)
        if isinstance(value, str) and value in units:
            ts = _timestamp(entry)
            stamp = time.strftime("%H:%M:%S", time.localtime(ts))
            return JournalLine(
                value, ts, f"{stamp} {decode_message(entry.get('MESSAGE'))}"
            )
    return None


class _Ring:
    def __init__(self, lines: int, max_bytes: int) -> None:
        self._lines: deque[str] = deque()
        self._max_lines = lines
        self._max_bytes = max_bytes
        self._bytes = 0  # total of the lines' UTF-8 sizes, without separators

    def _size(self) -> int:
        return self._bytes + max(len(self._lines) - 1, 0)

    def append(self, text: str) -> None:
        data = text.encode()
        if len(data) > self._max_bytes:
            text = data[: self._max_bytes].decode("utf-8", errors="ignore")
            data = text.encode()
        self._lines.append(text)
        self._bytes += len(data)
        while len(self._lines) > self._max_lines or self._size() > self._max_bytes:
            self._bytes -= len(self._lines.popleft().encode())

    def snapshot(self) -> tuple[str, ...]:
        return tuple(self._lines)


class JournalTail:
    def __init__(
        self,
        units: Sequence[str],
        on_line: Callable[[JournalLine], None],
        lines: int = 20,
        max_bytes: int = 5120,
        journalctl: Sequence[str] = ("journalctl",),
        backlog: int = 2000,
        backoff: Sequence[float] = (1, 2, 5, 10, 30),
    ) -> None:
        self._units = tuple(units)
        self._unit_set = frozenset(units)
        self._on_line = on_line
        self._journalctl = tuple(journalctl)
        self._backlog = backlog
        self._backoff = tuple(backoff) or (1.0,)
        self._rings = {u: _Ring(lines, max_bytes) for u in units}
        self._cursor: str | None = None

    def lines(self, unit: str) -> tuple[str, ...]:
        """The unit's recent lines, newest last."""
        ring = self._rings.get(unit)
        return ring.snapshot() if ring else ()

    def feed(self, line: JournalLine) -> None:
        """Store a line in its unit's ring and pass it to the callback."""
        ring = self._rings.get(line.unit)
        if ring is None:
            return
        ring.append(line.text)
        try:
            self._on_line(line)
        except Exception:
            log.exception("journal line callback failed for %s", line.unit)

    def _command(self) -> list[str]:
        cmd = [
            *self._journalctl,
            "-f", "-o", "json", "-q", "--no-pager",
            "-n", str(self._backlog),
            f"--output-fields={OUTPUT_FIELDS}",
            *(f"--unit={u}" for u in self._units),
        ]  # fmt: skip
        if self._cursor is not None:
            cmd.append(f"--after-cursor={self._cursor}")
        return cmd

    def _handle(self, raw: bytes) -> None:
        try:
            entry = json.loads(raw)
        except ValueError:
            log.debug("journal: skipping invalid JSON: %r", raw[:200])
            return
        if not isinstance(entry, dict):
            return
        cursor = entry.get("__CURSOR")
        if isinstance(cursor, str):
            self._cursor = cursor
        line = demux_entry(entry, self._unit_set)
        if line is not None:
            self.feed(line)

    async def run(self) -> None:
        """Follow the journal until cancelled, restarting journalctl on exit."""
        failures = 0
        logged: set[str] = set()
        while True:
            got_lines, message = await self._run_once()
            if got_lines:  # it was healthy, so a new failure deserves a new log
                failures = 0
                logged.clear()
            if message in logged:
                log.debug("journal follower: %s", message)
            else:
                logged.add(message)
                log.error("%s", message)
            await asyncio.sleep(self._backoff[min(failures, len(self._backoff) - 1)])
            failures += 1

    async def _run_once(self) -> tuple[bool, str]:
        """Run one journalctl to its end; return (got entries, failure message)."""
        proc = await asyncio.create_subprocess_exec(
            *self._command(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=READER_LIMIT,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())
        got = False
        try:
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError:
                    log.warning("journal: entry over %d bytes skipped", READER_LIMIT)
                    continue
                if not raw:
                    break
                got = True
                self._handle(raw)
            rc = await proc.wait()
            err = (await stderr_task).decode(errors="replace").strip()
        finally:
            await self._reap(proc)
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        first = err.splitlines()[0] if err else ""
        return got, f"journal follower exited rc={rc}: {first}"

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process) -> None:
        """Make sure the child is gone and awaited (also on cancellation)."""
        if proc.returncode is None:
            proc.terminate()
            try:
                async with asyncio.timeout(KILL_AFTER):
                    await proc.wait()
            except TimeoutError:
                proc.kill()
                await proc.wait()
