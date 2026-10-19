"""Log every event, one line each, on stdout (journald picks it up under systemd).

Text: ``<ISO local time, ms> <LEVEL> <text>``. JSON (``--log-format json``):
one object per line. An event gives exactly
``{"ts", "kind", "element", "old", "new", "text", "finding"}``; any other
log record gives ``{"ts", "kind": "log", "level", "logger", "text"}``, so
every stdout line stays parseable.
"""

import json
import logging
import sys
from datetime import datetime
from typing import Final, Literal, TypedDict

from qd_service_control.model.types import Event, Finding

LogFormat = Literal["text", "json"]

EVENT_LOGGER: Final = "qd_service_control.events"
_WARNING_KINDS: Final = frozenset({"finding", "gate"})
_HANDLER_MARK: Final = "_qd_service_control"

log = logging.getLogger(EVENT_LOGGER)


class FindingJson(TypedDict):
    code: str
    element: str
    units: list[str]
    message: str
    since: float
    journal: str


class EventJson(TypedDict):
    ts: float
    kind: str
    element: str
    old: str | None
    new: str | None
    text: str
    finding: FindingJson | None


def _finding_json(finding: Finding) -> FindingJson:
    return {
        "code": finding.code.value,
        "element": finding.element,
        "units": list(finding.units),
        "message": finding.message,
        "since": finding.since,
        "journal": finding.journal_ref,
    }


def event_json(event: Event) -> EventJson:
    """The JSON object for one event."""
    return {
        "ts": event.ts,
        "kind": event.kind,
        "element": event.element,
        "old": event.old,
        "new": event.new,
        "text": event.text,
        "finding": _finding_json(event.finding) if event.finding else None,
    }


def _event_of(record: logging.LogRecord) -> Event | None:
    event = getattr(record, "event", None)
    return event if isinstance(event, Event) else None


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).isoformat(
            timespec="milliseconds"
        )
        line = f"{stamp} {record.levelname} {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = _event_of(record)
        if event is not None:
            return json.dumps(event_json(event))
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return json.dumps(
            {
                "ts": record.created,
                "kind": "log",
                "level": record.levelname,
                "logger": record.name,
                "text": text,
            }
        )


def setup_logging(fmt: LogFormat, level: str) -> None:
    """Send all logging to stdout in ``fmt``, replacing a handler set up before."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if fmt == "json" else _TextFormatter())
    setattr(handler, _HANDLER_MARK, True)
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level.upper())


def log_event(event: Event) -> None:
    """Log one event: findings and gate violations at WARNING, the rest at INFO."""
    level = logging.WARNING if event.kind in _WARNING_KINDS else logging.INFO
    log.log(level, "%s", event.text, extra={"event": event})
