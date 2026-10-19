"""Immutable value types shared by the evaluator, observers and IOC."""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from qd_service_control.config.spec import Phase


class ElementState(StrEnum):
    UNKNOWN = "unknown"
    STOPPED = "stopped"
    WAITING = "waiting"
    READY = "ready"
    FAULT = "fault"


class OverallState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    FAULT = "fault"


class FindingCode(StrEnum):
    UNIT_NOT_FOUND = "unit_not_found"
    UNIT_FAILED = "unit_failed"
    UNIT_RESTART_LOOP = "unit_restart_loop"
    UNIT_START_TIMEOUT = "unit_start_timeout"
    UNIT_STOPPED = "unit_stopped"
    LISTENER_MISSING = "listener_missing"
    LISTENER_PORT_TAKEN = "listener_port_taken"
    EDGE_MISSING = "edge_missing"
    EDGE_SERVER_MISSING = "edge_server_missing"
    FILE_MISSING = "file_missing"
    CPU_SPIN = "cpu_spin"
    GATE_VIOLATION = "gate_violation"
    GATE_VIOLATION_POSSIBLE = "gate_violation_possible"


@dataclass(frozen=True)
class UnitSnapshot:
    name: str
    load_state: str
    active_state: str
    sub_state: str
    result: str
    job_pending: bool
    n_restarts: int
    main_pid: int
    exec_main_code: int
    exec_main_status: int
    exec_main_start_us: int  # realtime microseconds, 0 = never
    active_enter_us: int  # realtime microseconds, 0 = never
    control_group: str
    invocation_id: str
    after: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    code: FindingCode
    element: str
    units: tuple[str, ...]
    message: str
    since: float

    @property
    def journal_ref(self) -> str:
        """A journalctl command starting 5 s before the finding (local time)."""
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since - 5))
        return f'journalctl -u {self.units[0]} --since "{start}"'


@dataclass(frozen=True)
class ElementView:
    id: str
    kind: Literal["service", "listener", "file", "edge"]
    state: ElementState
    since: float
    detail: str
    phase: Phase
    required: bool
    finding: Finding | None


@dataclass(frozen=True)
class StackView:
    overall: OverallState
    ready: bool
    post_ready: bool
    since: float
    t0: float
    elements: Mapping[str, ElementView]
    findings: tuple[Finding, ...]
    units: Mapping[str, UnitSnapshot]
    cpu: Mapping[str, float | None]


@dataclass(frozen=True)
class Event:
    ts: float
    kind: Literal[
        "service", "listener", "file", "edge", "stack", "finding", "cleared", "gate"
    ]
    element: str
    old: str | None
    new: str | None
    text: str
    finding: Finding | None
