"""Scenario evidence: one JSON file per scenario and target.

``SCIOC_EVIDENCE`` (default ``docs/acceptance/evidence``) ``/<target>/<id>.json``
holds ``scenario``, ``title``, ``target``, ``arch``, ``started``, ``passed``,
``failures``, ``expect``, ``observed`` (``pvs``, ``findings``, ``cleared``,
``stack_states``, ``time_to_final_s``), ``gate`` (each gate event's text, its
two units and the start times its message gives), ``broken_holds`` (each time
the final state was reached but did not hold, before it finally did or
``within`` ran out) and ``events`` (every tool event except plain log lines).
"""

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, TypedDict

REPO: Final = Path(__file__).resolve().parents[2]
DEFAULT_DIR: Final = REPO / "docs" / "acceptance" / "evidence"
_DEPENDENT_START: Final = re.compile(r" started at (\S+) before ")

Event = dict[str, object]


class GateJson(TypedDict):
    text: str
    code: str
    dependent: str | None
    dependency: str | None
    dependent_start: str | None  # from the message, local time of day
    dependency_start: str | None  # the message does not give it


class ObservedJson(TypedDict):
    pvs: dict[str, str | None]
    findings: list[str]
    cleared: list[str]
    stack_states: list[str]
    time_to_final_s: float | None


class EvidenceJson(TypedDict):
    scenario: str
    title: str
    target: str
    arch: str
    started: str
    passed: bool
    failures: list[str]
    expect: dict[str, object]
    observed: ObservedJson | None
    gate: list[GateJson]
    broken_holds: list[dict[str, object]]  # final states reached that did not hold
    events: list[Event]


def evidence_dir() -> Path:
    """The evidence root: ``$SCIOC_EVIDENCE`` or ``docs/acceptance/evidence``."""
    value = os.environ.get("SCIOC_EVIDENCE")
    return Path(value) if value else DEFAULT_DIR


def gate_entries(events: Sequence[Event]) -> list[GateJson]:
    """One entry per gate event, with the times its message gives."""
    entries: list[GateJson] = []
    for e in events:
        finding = e.get("finding")
        if e.get("kind") != "gate" or not isinstance(finding, Mapping):
            continue
        units = finding.get("units")
        units = [str(u) for u in units] if isinstance(units, list) else []
        match = _DEPENDENT_START.search(str(finding.get("message", "")))
        entries.append(
            {
                "text": str(e.get("text")),
                "code": str(finding.get("code")),
                "dependent": units[0] if units else None,
                "dependency": units[1] if len(units) > 1 else None,
                "dependent_start": match.group(1) if match else None,
                "dependency_start": None,
            }
        )
    return entries


def write(path: Path, data: EvidenceJson) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
