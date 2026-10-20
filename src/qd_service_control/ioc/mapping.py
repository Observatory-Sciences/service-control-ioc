"""Turn a ``StackView`` into the plain values the IOC puts in records.

Pure: this module never imports ``softioc``, so it is tested on any host. Only
the byte helpers (``fit``, ``text_waveform``) apply record limits here; the IOC
fits the other texts to their record lengths.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from qd_service_control.config.spec import StackSpec
from qd_service_control.model.evaluator import exit_text
from qd_service_control.model.types import (
    ElementState,
    Finding,
    OverallState,
    StackView,
)

Severity = Literal[0, 1, 2, 3]  # NO_ALARM, MINOR, MAJOR, INVALID

_INVALID: Final[Severity] = 3

# The tuple index is the mbbi state number; the severity is the state's alarm.
ELEMENT_STATES: Final[tuple[tuple[ElementState, Severity], ...]] = (
    (ElementState.UNKNOWN, 3),
    (ElementState.STOPPED, 0),
    (ElementState.WAITING, 1),
    (ElementState.READY, 0),
    (ElementState.FAULT, 2),
)
OVERALL_STATES: Final[tuple[tuple[OverallState, Severity], ...]] = (
    (OverallState.STOPPED, 0),
    (OverallState.STARTING, 1),
    (OverallState.STOPPING, 1),
    (OverallState.READY, 0),
    (OverallState.FAULT, 2),
)
# A group unit's ActiveState as an element state; no snapshot is UNKNOWN.
GROUP_STATE_OF_ACTIVE: Final = {
    "active": ElementState.READY,
    "reloading": ElementState.READY,
    "inactive": ElementState.STOPPED,
    "activating": ElementState.WAITING,
    "deactivating": ElementState.WAITING,
    "failed": ElementState.FAULT,
}

_ELEMENT_INDEX: Final = {state: i for i, (state, _) in enumerate(ELEMENT_STATES)}
_OVERALL_INDEX: Final = {state: i for i, (state, _) in enumerate(OVERALL_STATES)}
_CONTINUATION_MASK: Final = 0xC0  # UTF-8 continuation bytes are 0b10xxxxxx
_CONTINUATION: Final = 0x80


@dataclass(frozen=True)
class StackValues:
    state: int
    severity: Severity
    ready: bool
    post_ready: bool
    faults: int
    fault_msg: str


@dataclass(frozen=True)
class ServiceValues:
    state: int
    severity: Severity
    unit: str
    active: str
    ready: bool
    nrestarts: int
    exit: str
    cpu: float | None  # None: the IOC marks the record INVALID
    finding: str


@dataclass(frozen=True)
class ElementValues:
    state: int
    severity: Severity
    detail: str
    since: str


@dataclass(frozen=True)
class GroupValues:
    state: int
    severity: Severity


def fit(text: str, max_bytes: int) -> str:
    """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes, on a character boundary."""
    data = text.encode()
    if len(data) <= max_bytes:
        return text
    # "ignore" drops the partial character a byte cut can leave at the end.
    return data[:max_bytes].decode("utf-8", errors="ignore")


def _tail(data: bytes, limit: int) -> bytes:
    """The last ``limit`` bytes of ``data``, starting on a character boundary."""
    cut = len(data) - limit
    while cut < len(data) and data[cut] & _CONTINUATION_MASK == _CONTINUATION:
        cut += 1
    return data[cut:]


def text_waveform(lines: Sequence[str], length: int) -> bytes:
    """Newline-terminated ``lines`` as a NUL-padded buffer of ``length`` bytes.

    The oldest lines go first so the total is at most ``length - 1`` (a final
    NUL always remains). A single line that is still too long keeps its tail.
    """
    limit = length - 1
    encoded = [(line + "\n").encode() for line in lines]
    total = sum(len(line) for line in encoded)
    first = 0
    while total > limit and first < len(encoded) - 1:
        total -= len(encoded[first])
        first += 1
    data = b"".join(encoded[first:])
    if len(data) > limit:
        data = _tail(data, limit)
    return data.ljust(length, b"\0")


def since_text(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def describe(spec: StackSpec, eid: str) -> str:
    """A one-line description of a listener, edge or file element."""
    if eid in spec.listeners:
        ls = spec.listeners[eid]
        where = f":{ls.port}/{ls.proto}"
        if ls.on_host:
            return f"{ls.owner} {ls.address}{where}"
        suffix = f" ({ls.address})" if ls.address else ""
        return f"{ls.owner} {where}{suffix}"
    if eid in spec.edges:
        edge = spec.edges[eid]
        server = spec.listeners[edge.server]
        where = f"{server.port}/{server.proto}"
        if server.on_host:
            return f"{edge.client} -> {server.owner} {server.address}:{where}"
        return f"{edge.client} -> {server.owner}:{where}"
    file = spec.files[eid]
    return f"{file.owner} file {file.path}"


def latest_finding(findings: Sequence[Finding]) -> Finding | None:
    """The finding raised last; ties go to the greater key."""
    if not findings:
        return None
    return max(findings, key=lambda f: (f.since, f.key))


def stack_values(view: StackView) -> StackValues:
    latest = latest_finding(view.findings)
    index = _OVERALL_INDEX[view.overall]
    return StackValues(
        state=index,
        severity=OVERALL_STATES[index][1] if view.evaluated else _INVALID,
        ready=view.ready and view.evaluated,
        post_ready=view.post_ready and view.evaluated,
        faults=len(view.findings),
        fault_msg=latest.look if latest else "",
    )


def _state_pair(state: ElementState) -> tuple[int, Severity]:
    index = _ELEMENT_INDEX[state]
    return index, ELEMENT_STATES[index][1]


def service_values(spec: StackSpec, view: StackView, sid: str) -> ServiceValues:
    unit = spec.services[sid].unit
    elem = view.elements.get(sid)
    state = elem.state if elem else ElementState.UNKNOWN
    index, severity = _state_pair(state)
    snap = view.units.get(unit)
    finding = elem.finding if elem else None
    if finding is None:
        finding = latest_finding([f for f in view.findings if f.units[0] == unit])
    return ServiceValues(
        state=index,
        severity=severity,
        unit=unit,
        active=f"{snap.active_state}/{snap.sub_state}" if snap else "unknown",
        ready=state is ElementState.READY,
        nrestarts=snap.n_restarts if snap else 0,
        exit=exit_text(snap) if snap and snap.exec_main_code != 0 else "",
        cpu=view.cpu.get(unit),
        finding=finding.look if finding else "",
    )


def element_values(view: StackView, eid: str) -> ElementValues:
    elem = view.elements.get(eid)
    if elem is None:
        index, severity = _state_pair(ElementState.UNKNOWN)
        return ElementValues(index, severity, "", "")
    index, severity = _state_pair(elem.state)
    return ElementValues(index, severity, elem.detail, since_text(elem.since))


def group_values(spec: StackSpec, view: StackView, gid: str) -> GroupValues:
    snap = view.units.get(spec.groups[gid].unit)
    state = (
        GROUP_STATE_OF_ACTIVE.get(snap.active_state, ElementState.UNKNOWN)
        if snap
        else ElementState.UNKNOWN
    )
    index, severity = _state_pair(state)
    return GroupValues(index, severity)
