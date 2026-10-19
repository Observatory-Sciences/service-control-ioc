import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import pytest

from qd_service_control.observe.journal import (
    JournalLine,
    JournalTail,
    decode_message,
    demux_entry,
)

FAKE = Path(__file__).parent / "fake_journalctl.py"
UNITS = frozenset({"scdemo-relay.service", "scdemo-sink.service"})
PERMISSION = "No journal files were opened due to insufficient permissions."


def hhmmss(us: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(us / 1e6))


def test_demux_rules() -> None:
    us = 1_700_000_000_000_000
    own = {
        "_SYSTEMD_UNIT": "scdemo-sink.service",
        "UNIT": "scdemo-relay.service",
        "MESSAGE": "hello",
        "__REALTIME_TIMESTAMP": str(us),
    }
    line = demux_entry(own, UNITS)
    assert line == JournalLine("scdemo-sink.service", us / 1e6, f"{hhmmss(us)} hello")

    from_init = {
        "_SYSTEMD_UNIT": "init.scope",
        "UNIT": "scdemo-relay.service",
        "MESSAGE": "Started.",
        "__REALTIME_TIMESTAMP": str(us),
    }
    got = demux_entry(from_init, UNITS)
    assert got is not None
    assert got.unit == "scdemo-relay.service"

    obj = {
        "_SYSTEMD_UNIT": "init.scope",
        "OBJECT_SYSTEMD_UNIT": "scdemo-sink.service",
        "MESSAGE": "x",
        "__REALTIME_TIMESTAMP": str(us),
    }
    got = demux_entry(obj, UNITS)
    assert got is not None
    assert got.unit == "scdemo-sink.service"

    assert (
        demux_entry({"_SYSTEMD_UNIT": "other.service", "MESSAGE": "x"}, UNITS) is None
    )


def test_decode_message() -> None:
    assert decode_message("plain") == "plain"
    assert decode_message(list("héllo".encode())) == "héllo"
    assert decode_message([0x61, 0xFF, 0x62]) == "a�b"
    assert decode_message(None) == ""


def test_rings_trim_by_lines_and_bytes() -> None:
    got: list[JournalLine] = []
    tail = JournalTail(["a.service"], got.append, lines=3, max_bytes=20)
    for i in range(5):
        tail.feed(JournalLine("a.service", 0.0, f"line{i}"))
    assert tail.lines("a.service") == ("line2", "line3", "line4")
    for i in range(3):
        tail.feed(JournalLine("a.service", 0.0, "x" * 8))
    lines = tail.lines("a.service")
    assert len("\n".join(lines).encode()) <= 20
    assert lines == ("x" * 8, "x" * 8)
    tail.feed(JournalLine("a.service", 0.0, "y" * 50))
    assert len("\n".join(tail.lines("a.service")).encode()) <= 20
    assert tail.lines("unknown.service") == ()


def entry(unit: str, msg: str | list[int], us: int, cursor: str) -> dict[str, object]:
    return {
        "_SYSTEMD_UNIT": unit,
        "MESSAGE": msg,
        "__REALTIME_TIMESTAMP": str(us),
        "__CURSOR": cursor,
    }


def write_journal(tmp_path: Path, us: int) -> Path:
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        "\n".join(
            [
                json.dumps(entry("scdemo-relay.service", "one", us, "c1")),
                "not json",
                json.dumps(entry("scdemo-sink.service", "two", us + 1, "c2")),
                json.dumps(entry("scdemo-relay.service", list(b"three"), us + 2, "c3")),
                json.dumps(entry("unrelated.service", "nope", us + 3, "c4")),
            ]
        )
        + "\n"
    )
    return journal


@pytest.mark.asyncio
async def test_tail_reads_fake_journalctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    us = 1_700_000_000_000_000
    monkeypatch.setenv("FAKE_JOURNAL", str(write_journal(tmp_path, us)))
    seen: list[JournalLine] = []
    three = asyncio.Event()

    def on_line(line: JournalLine) -> None:
        seen.append(line)
        if len(seen) == 3:
            three.set()

    tail = JournalTail(
        sorted(UNITS),
        on_line,
        journalctl=[sys.executable, str(FAKE)],
        backoff=(60,),
    )
    caplog.set_level(logging.DEBUG, logger="qd_service_control.observe.journal")
    task = asyncio.create_task(tail.run())
    try:
        async with asyncio.timeout(5):
            await three.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(seen) == 3
    assert tail.lines("scdemo-relay.service") == (
        f"{hhmmss(us)} one",
        f"{hhmmss(us + 2)} three",
    )
    assert tail.lines("scdemo-sink.service") == (f"{hhmmss(us + 1)} two",)
    assert any("invalid JSON" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_restart_resumes_after_last_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FAKE_JOURNAL", str(write_journal(tmp_path, 1_700_000_000_000_000))
    )
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args"))
    tail = JournalTail(
        sorted(UNITS),
        lambda _line: None,
        journalctl=[sys.executable, str(FAKE)],
        backoff=(0.05,),
    )
    task = asyncio.create_task(tail.run())
    await asyncio.sleep(0.7)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    args = (tmp_path / "args").read_text().splitlines()
    assert len(args) >= 2
    assert "--unit=scdemo-relay.service" in args[0]
    assert "-o json" in args[0]
    assert "--after-cursor" not in args[0]
    assert "--after-cursor=c4" in args[1]


@pytest.mark.asyncio
async def test_permission_failure_logs_once_and_backs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FAKE_RC", "1")
    monkeypatch.setenv("FAKE_STDERR", PERMISSION)
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args"))
    tail = JournalTail(
        ["a.service"],
        lambda _line: None,
        journalctl=[sys.executable, str(FAKE)],
        backoff=(0.05,),
    )
    caplog.set_level(logging.DEBUG, logger="qd_service_control.observe.journal")
    task = asyncio.create_task(tail.run())
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len((tmp_path / "args").read_text().splitlines()) >= 3
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert PERMISSION in errors[0].getMessage()
    assert "rc=1" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_cancel_terminates_child(tmp_path: Path) -> None:
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\nprint('x', flush=True)\ntime.sleep(60)\n")
    tail = JournalTail(
        ["a.service"], lambda _line: None, journalctl=[sys.executable, str(sleeper)]
    )
    task = asyncio.create_task(tail.run())
    await asyncio.sleep(0.3)
    task.cancel()
    async with asyncio.timeout(5):
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_spawn_failure_logs_once_and_retries(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tail = JournalTail(
        ["a.service"],
        lambda _line: None,
        journalctl=[str(tmp_path / "nonexistent-journalctl")],
        backoff=(0.05,),
    )
    attempts = 0
    original = tail._command

    def counting() -> list[str]:
        nonlocal attempts
        attempts += 1
        return original()

    tail._command = counting  # ty: ignore[invalid-assignment] - test spy
    caplog.set_level(logging.DEBUG, logger="qd_service_control.observe.journal")
    task = asyncio.create_task(tail.run())
    await asyncio.sleep(0.5)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts >= 2
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "failed to start" in errors[0].getMessage()
