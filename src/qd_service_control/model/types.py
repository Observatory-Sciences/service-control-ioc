"""Immutable value types shared by the evaluator, observers and IOC."""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from qd_service_control.config.spec import Phase

Verb = Literal["start", "stop", "restart"]

_OK_RESULTS: Final = frozenset({"done", "skipped"})


class ElementState(StrEnum):
    UNKNOWN = "unknown"
    STOPPED = "stopped"
    WAITING = "waiting"
    READY = "ready"
    FAULT = "fault"


class OverallState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
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
    JOB_FAILED = "job_failed"


_GATE_CODES: Final = frozenset(
    {FindingCode.GATE_VIOLATION, FindingCode.GATE_VIOLATION_POSSIBLE}
)


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
    job_type: str = (
        ""  # the pending job's JobType ("start", "stop", ...); "" = none/unknown
    )


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

    @property
    def key(self) -> str:
        """A stable identity: ``<code>:<element>``, plus the dependency for gates.

        A service can have gate findings against several dependencies, so for
        those the dependency unit (``units[1]``) is part of the key.
        """
        if self.code in _GATE_CODES:
            return f"{self.code}:{self.element}<{self.units[1]}"
        return f"{self.code}:{self.element}"

    @property
    def look(self) -> str:
        """``<message>, look at <units> (journalctl ...)`` for FAULT and GATE lines."""
        units = ", ".join(self.units)
        return f"{self.message}, look at {units} ({self.journal_ref})"


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
    # False until the evaluator's first update; ``overall`` is then a placeholder.
    evaluated: bool


@dataclass(frozen=True)
class Event:
    ts: float
    kind: Literal[
        "service",
        "listener",
        "file",
        "edge",
        "stack",
        "finding",
        "cleared",
        "gate",
        "job",
    ]
    element: str
    old: str | None
    new: str | None
    text: str
    finding: Finding | None


@dataclass(frozen=True)
class JobOutcome:
    """How one start/stop/restart job ended, as ``Core.control`` returns it."""

    target: str  # service id, group id, or "stack"
    unit: str
    verb: Verb
    # systemd's result ("done", "skipped", "failed", "canceled", "timeout",
    # "dependency", ...), or "error" / "not-connected" / "no-result"
    result: str
    detail: str  # "" on success
    duration: float  # seconds from the call to the result

    @property
    def ok(self) -> bool:
        return self.result in _OK_RESULTS
