"""Gate violations (spec Section 5.4): a dependent started before a dependency was ready.

Used by ``Evaluator``. When a configured service D gets a new
``exec_main_start_us`` (``t_s``) after the tracker's first socket poll, a
check is queued. It runs at the first poll with ``poll_time >= t_s`` and looks
at each service O that D is ordered after: O's unit state plus its ``pre``
listeners, ``pre`` files and ``pre`` edges where O is the client. There is one
finding per (D invocation, O); definite wins over possible. The findings never
change element states; they clear when D's next invocation starts.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from qd_service_control.config.spec import StackSpec
from qd_service_control.model.deps import DependencyGraph
from qd_service_control.model.types import (
    ElementState,
    Finding,
    FindingCode,
    UnitSnapshot,
)

_US: Final = 1_000_000

_Verdict = Literal["definite", "possible"] | None


@dataclass(slots=True)
class _History:
    """What the socket polls saw of one element."""

    last_not_ready: float | None = None  # last poll where it was not ready
    first_ready: float | None = None  # first ready poll since then


@dataclass(frozen=True, slots=True)
class GateUpdate:
    raised: list[Finding]
    cleared: list[Finding]


def _clock(t: float) -> str:
    """Local wall-clock time as ``HH:MM:SS.mmm``."""
    return datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


def _checked_elements(spec: StackSpec) -> dict[str, tuple[str, ...]]:
    """Per service id: the elements a gate check looks at (the unit first)."""
    checked: dict[str, list[str]] = {sid: [sid] for sid in spec.services}
    for lid, ls in spec.listeners.items():
        if not ls.on_host and ls.phase == "pre":
            checked[ls.owner].append(lid)
    for fid, fs in spec.files.items():
        if fs.phase == "pre":
            checked[fs.owner].append(fid)
    for eid, edge in spec.edges.items():
        if not edge.client_is_host and edge.phase == "pre":
            checked[edge.client].append(eid)
    return {sid: tuple(ids) for sid, ids in checked.items()}


class GateTracker:
    """Queues and runs gate checks; holds the resulting findings."""

    def __init__(self, spec: StackSpec) -> None:
        self._spec = spec
        self._sid_of = {s.unit: sid for sid, s in spec.services.items()}
        self._checked = _checked_elements(spec)
        self._first_poll: float | None = None
        self._starts: dict[str, int] = {}  # sid -> last exec_main_start_us seen
        self._pending: dict[str, int] = {}  # sid -> exec_main_start_us to check
        self._history: dict[str, _History] = {}
        self._findings: dict[tuple[str, str], Finding] = {}  # (D sid, O sid)

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(self._findings.values())

    def update(
        self,
        now: float,
        units: Mapping[str, UnitSnapshot] | None,
        poll: float | None,
        states: Mapping[str, ElementState],
        graph: DependencyGraph,
    ) -> GateUpdate:
        """Advance with one evaluator update.

        ``units`` is None while D-Bus is disconnected; ``poll`` is the time of a
        fresh socket scan (None when there is none); ``states`` are this
        update's element states.
        """
        if poll is not None and self._first_poll is None:
            self._first_poll = poll
        cleared = self._observe_starts(units) if units is not None else []
        if poll is not None:
            self._record(poll, states)
        raised: list[Finding] = []
        if poll is not None and units is not None:
            for sid, start_us in list(self._pending.items()):
                if poll >= start_us / _US:
                    del self._pending[sid]
                    raised.extend(self._check(sid, start_us, units, states, graph, now))
        return GateUpdate(raised, cleared)

    def _observe_starts(self, units: Mapping[str, UnitSnapshot]) -> list[Finding]:
        """Queue a check for each new invocation; clear the previous one's findings."""
        cleared: list[Finding] = []
        for sid, svc in self._spec.services.items():
            snap = units.get(svc.unit)
            if snap is None or not snap.exec_main_start_us:
                continue
            start_us = snap.exec_main_start_us
            if self._starts.get(sid) == start_us:
                continue
            self._starts[sid] = start_us
            self._pending.pop(sid, None)
            for key in [k for k in self._findings if k[0] == sid]:
                cleared.append(self._findings.pop(key))
            # Only a start after the first poll has evidence from before it.
            if self._first_poll is not None and self._first_poll < start_us / _US:
                self._pending[sid] = start_us
        return cleared

    def _record(self, poll: float, states: Mapping[str, ElementState]) -> None:
        """Per-element poll history: last not-ready poll, first ready poll since."""
        for eid, state in states.items():
            if state is ElementState.UNKNOWN:
                continue  # no evidence either way
            history = self._history.setdefault(eid, _History())
            if state is ElementState.READY:
                if history.first_ready is None:
                    history.first_ready = poll
            else:
                history.last_not_ready = poll
                history.first_ready = None

    def _check(
        self,
        sid: str,
        start_us: int,
        units: Mapping[str, UnitSnapshot],
        states: Mapping[str, ElementState],
        graph: DependencyGraph,
        now: float,
    ) -> list[Finding]:
        unit = self._spec.services[sid].unit
        start = start_us / _US
        raised: list[Finding] = []
        for dep_unit in sorted(graph.dependencies_of(unit)):
            dep = self._sid_of.get(dep_unit)
            if dep is None or dep == sid:  # unconfigured, or a cycle back to D
                continue
            definite: list[str] = []
            possible: list[str] = []
            for eid in self._checked[dep]:
                verdict = self._verdict(eid, dep, start_us, units, states)
                if verdict == "definite":
                    definite.append(eid)
                elif verdict == "possible":
                    possible.append(eid)
            if definite:
                code, elements = FindingCode.GATE_VIOLATION, definite
            elif possible:
                code, elements = FindingCode.GATE_VIOLATION_POSSIBLE, possible
            else:
                continue
            message = (
                f"{unit} started at {_clock(start)} before {dep} was ready"
                f" ({', '.join(elements)})"
            )
            finding = Finding(code, sid, (unit, dep_unit), message, now)
            self._findings[(sid, dep)] = finding
            raised.append(finding)
        return raised

    def _verdict(
        self,
        eid: str,
        dep: str,
        start_us: int,
        units: Mapping[str, UnitSnapshot],
        states: Mapping[str, ElementState],
    ) -> _Verdict:
        """``definite``, ``possible`` or None for one element at the check poll."""
        state = states.get(eid, ElementState.UNKNOWN)
        if state is ElementState.UNKNOWN:
            return None  # no evidence
        if state is not ElementState.READY:
            return "definite"
        if eid == dep:
            # The unit: its own timestamp is exact, no poll uncertainty.
            snap = units.get(self._spec.services[dep].unit)
            late = snap is not None and snap.active_enter_us > start_us
            return "definite" if late else None
        history = self._history.get(eid)
        if history is None or history.last_not_ready is None:
            return None  # never seen not ready
        start = start_us / _US
        if history.last_not_ready >= start:
            return "definite"  # a poll after the start still saw it missing
        if history.first_ready is not None and history.first_ready >= start:
            return "possible"  # became ready within the poll interval around t_s
        return None
