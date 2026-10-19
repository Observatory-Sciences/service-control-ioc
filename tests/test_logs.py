"""Event log lines: text and JSON formats on stdout."""

import json
import logging
import re
from collections.abc import Iterator

import pytest

from qd_service_control.logs import log_event, setup_logging
from qd_service_control.model.types import Event, Finding, FindingCode

T = 1_700_000_000.25
STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


FINDING = Finding(
    FindingCode.LISTENER_MISSING,
    "relay.rep",
    ("scdemo-relay.service",),
    "127.0.0.1:20100/tcp not listening (not there 30 s after start)",
    T,
)
READY = Event(
    T,
    "service",
    "relay",
    "waiting",
    "ready",
    "service relay ready: active/running (+1.2 s)",
    None,
)
FAULT = Event(
    T,
    "finding",
    "relay.rep",
    None,
    "listener_missing",
    "FAULT listener_missing relay.rep: ...",
    FINDING,
)
GATE = Event(T, "gate", "controller", None, "gate_violation", "GATE ...", FINDING)
CLEARED = Event(
    T,
    "cleared",
    "relay.rep",
    "listener_missing",
    None,
    "CLEARED listener_missing relay.rep (+3.0 s)",
    FINDING,
)


def test_log_event_text_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("text", "INFO")
    for event in (READY, FAULT, GATE, CLEARED):
        log_event(event)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4
    assert re.fullmatch(
        f"{STAMP} INFO service relay ready: active/running \\(\\+1.2 s\\)", lines[0]
    )
    assert re.fullmatch(
        f"{STAMP} WARNING FAULT listener_missing relay.rep: ...", lines[1]
    )
    assert re.fullmatch(f"{STAMP} WARNING GATE ...", lines[2])
    assert re.fullmatch(
        f"{STAMP} INFO CLEARED listener_missing relay.rep \\(\\+3.0 s\\)", lines[3]
    )

    setup_logging("json", "INFO")
    log_event(READY)
    log_event(FAULT)
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in lines] == [
        {
            "ts": T,
            "kind": "service",
            "element": "relay",
            "old": "waiting",
            "new": "ready",
            "text": "service relay ready: active/running (+1.2 s)",
            "finding": None,
        },
        {
            "ts": T,
            "kind": "finding",
            "element": "relay.rep",
            "old": None,
            "new": "listener_missing",
            "text": "FAULT listener_missing relay.rep: ...",
            "finding": {
                "code": "listener_missing",
                "element": "relay.rep",
                "units": ["scdemo-relay.service"],
                "message": FINDING.message,
                "since": T,
                "journal": FINDING.journal_ref,
            },
        },
    ]


def test_json_format_keeps_other_log_lines_parseable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("json", "INFO")
    logging.getLogger("qd_service_control.test").warning("bus %s", "lost")
    logging.getLogger("qd_service_control.test").debug("hidden")
    (line,) = capsys.readouterr().out.splitlines()
    record = json.loads(line)
    assert record["kind"] == "log"
    assert record["level"] == "WARNING"
    assert record["logger"] == "qd_service_control.test"
    assert record["text"] == "bus lost"
    assert isinstance(record["ts"], float)


def test_setup_logging_replaces_its_own_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("text", "INFO")
    setup_logging("text", "INFO")
    log_event(READY)
    assert len(capsys.readouterr().out.splitlines()) == 1
