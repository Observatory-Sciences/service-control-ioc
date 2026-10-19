"""logfmt event lines on stdout: ``event=<name> svc=<service> [inst=<n>] k=v ...``."""

import io
import logging
import re
import sys
from typing import Final

_log = logging.getLogger("scdemo")
_context: dict[str, str | int | None] = {"svc": None, "inst": None}

_PAIR: Final = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S*)')
_NEEDS_QUOTES: Final = re.compile(r'[\s="]|^$')


def _quote(value: str) -> str:
    if not _NEEDS_QUOTES.search(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_event(
    event: str, svc: str, inst: int | None = None, **fields: str | float
) -> str:
    parts = [("event", event), ("svc", svc)]
    if inst is not None:
        parts.append(("inst", str(inst)))
    parts.extend((key, str(value)) for key, value in fields.items())
    return " ".join(f"{key}={_quote(value)}" for key, value in parts)


def parse_logfmt(line: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, raw in _PAIR.findall(line):
        if raw.startswith('"'):
            raw = re.sub(r"\\(.)", r"\1", raw[1:-1])
        result[key] = raw
    return result


def setup_logging(svc: str, inst: int | None = None) -> None:
    """Send bare messages to line-buffered stdout and remember who is logging."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    _context["svc"] = svc
    _context["inst"] = inst


def log_event(event: str, **fields: str | float) -> None:
    svc = _context["svc"]
    if svc is None:
        raise RuntimeError("setup_logging() must be called before log_event()")
    inst = _context["inst"]
    _log.info(
        format_event(event, str(svc), int(inst) if inst is not None else None, **fields)
    )
