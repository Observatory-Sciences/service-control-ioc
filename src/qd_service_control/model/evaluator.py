"""Turn the stack spec plus observations into element states, findings and events.

Pure: time only ever comes from ``Inputs.now``. ``update`` composes small rule
functions (spec Sections 5.1-5.3, 5.5); the result of each rule is a
``_Result`` whose optional ``_Claim`` becomes a held ``Finding``. Gate
violations (spec Section 5.4) come from ``GateTracker`` in ``model.gate``:
they are findings and ``gate`` events but never change a state.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from qd_service_control.config.spec import (
    EdgeSpec,
    FileSpec,
    ListenerSpec,
    Phase,
    ServiceSpec,
    StackSpec,
)
from qd_service_control.model.deps import DependencyGraph
from qd_service_control.model.gate import GateTracker, GateUpdate
from qd_service_control.model.types import (
    ElementState,
    ElementView,
    Event,
    Finding,
    FindingCode,
    OverallState,
    StackView,
    UnitSnapshot,
)
from qd_service_control.observe.match import (
    EdgeFact,
    ListenerFact,
    match_edge,
    match_listener,
)
from qd_service_control.observe.procfs import SocketTable

Kind = Literal["service", "listener", "file", "edge"]

_US: Final = 1_000_000
# si_code values in ExecMainCode (Linux <bits/siginfo-consts.h>).
_CLD_EXITED: Final = 1
_CLD_KILLED: Final = 2
_CLD_DUMPED: Final = 3
# Linux numbering; the host's ``signal`` module differs on macOS.
_LINUX_SIGNALS: Final = {
    1: "SIGHUP",
    2: "SIGINT",
    3: "SIGQUIT",
    4: "SIGILL",
    6: "SIGABRT",
    7: "SIGBUS",
    8: "SIGFPE",
    9: "SIGKILL",
    10: "SIGUSR1",
    11: "SIGSEGV",
    12: "SIGUSR2",
    13: "SIGPIPE",
    14: "SIGALRM",
    15: "SIGTERM",
}
_UNIT_CODES: Final = frozenset(
    {
        FindingCode.UNIT_NOT_FOUND,
        FindingCode.UNIT_FAILED,
        FindingCode.UNIT_RESTART_LOOP,
        FindingCode.UNIT_START_TIMEOUT,
        FindingCode.UNIT_STOPPED,
        FindingCode.CPU_SPIN,
    }
)

_UNKNOWN: Final = ElementState.UNKNOWN
_STOPPED: Final = ElementState.STOPPED
_WAITING: Final = ElementState.WAITING
_READY: Final = ElementState.READY
_FAULT: Final = ElementState.FAULT


@dataclass(frozen=True)
class Inputs:
    now: float
    bus_connected: bool
    units: Mapping[str, UnitSnapshot]  # by unit name; missing = unknown
    sockets: SocketTable | None  # None until the first scan, or after a scan error
    poll_time: float | None  # set when `sockets` is a fresh scan taken at this time
    cpu: Mapping[str, float | None]  # by unit name
    spinning: frozenset[str]  # unit names whose CpuMeter.spinning() is true
    files: Mapping[str, bool]  # FileSpec id -> probe result


@dataclass(frozen=True, slots=True)
class _Claim:
    """A finding a rule wants raised; held as a ``Finding`` with a stable start."""

    code: FindingCode
    element: str
    units: tuple[str, ...]  # never empty: Finding.journal_ref uses units[0]
    message: str


@dataclass(frozen=True, slots=True)
class _Result:
    state: ElementState
    detail: str
    claim: _Claim | None = None


@dataclass(frozen=True, slots=True)
class _Meta:
    kind: Kind
    phase: Phase
    required: bool


def _start_s(snap: UnitSnapshot) -> float:
    """When the unit's current invocation started (0 = unknown, long ago)."""
    return (snap.exec_main_start_us or snap.active_enter_us) / _US


def _exit_text(snap: UnitSnapshot) -> str:
    status = snap.exec_main_status
    if snap.exec_main_code == _CLD_EXITED:
        what = f"exited with status {status}"
    elif snap.exec_main_code in (_CLD_KILLED, _CLD_DUMPED):
        what = f"killed by signal {_LINUX_SIGNALS.get(status, str(status))}"
        if snap.exec_main_code == _CLD_DUMPED:
            what += " (core dumped)"
    else:
        what = "failed"
    if snap.result in ("", "success"):
        return what
    return f"{what} (result {snap.result})"


def _edge_missing_detail(edge: EdgeSpec, fact: EdgeFact) -> str:
    if not fact.server_present:
        return f"server {edge.server} missing"
    if edge.proto == "tcp":
        return "not established"
    if edge.sender == "connected":
        return "not connected"
    return "no sender socket"


def _look(finding: Finding) -> str:
    """``<message>, look at <units> (journalctl ...)`` for FAULT and GATE lines."""
    units = ", ".join(finding.units)
    return f"{finding.message}, look at {units} ({finding.journal_ref})"


def _cleared_event(finding: Finding, now: float, t: str) -> Event:
    text = f"CLEARED {finding.code} {finding.element} {t}"
    return Event(
        now, "cleared", finding.element, finding.code.value, None, text, finding
    )


class Evaluator:
    """Applies the state, fault and finding rules to each new set of inputs."""

    def __init__(
        self, spec: StackSpec, graph: DependencyGraph, started_at: float
    ) -> None:
        self._spec = spec
        self._graph = graph
        self._t0 = started_at
        self._overall: OverallState | None = None
        self._overall_since = started_at
        self._ready = False
        self._post_ready = False
        self._elements: dict[str, ElementView] = {}
        self._findings: dict[tuple[FindingCode, str], Finding] = {}
        self._units: Mapping[str, UnitSnapshot] = {}
        self._cpu: Mapping[str, float | None] = {}
        # History kept between updates.
        self._scan_time: float | None = None
        self._invocations: dict[str, str] = {}  # unit -> last non-empty id
        self._restarts: dict[str, int] = {}  # unit -> last NRestarts
        self._rises: dict[str, list[float]] = {}  # unit -> times NRestarts rose
        self._activating_since: dict[str, float] = {}
        self._was_ready: dict[str, tuple[str, ...]] = {}  # id -> invocation key
        self._meta = self._build_meta(spec)
        self._gate = GateTracker(spec)

    @staticmethod
    def _build_meta(spec: StackSpec) -> dict[str, _Meta]:
        meta = {
            sid: _Meta("service", "pre", s.required) for sid, s in spec.services.items()
        }
        for lid, ls in spec.listeners.items():
            if not ls.on_host:
                meta[lid] = _Meta("listener", ls.phase, ls.required)
        for fid, fs in spec.files.items():
            meta[fid] = _Meta("file", fs.phase, fs.required)
        for eid, edge in spec.edges.items():
            meta[eid] = _Meta("edge", edge.phase, edge.required)
        return meta

    def set_graph(self, graph: DependencyGraph) -> None:
        self._graph = graph

    def update(self, inputs: Inputs) -> list[Event]:
        """Apply ``inputs`` and return the events for whatever changed."""
        self._observe(inputs)
        results = self._evaluate(inputs)
        self._remember_ready(results, inputs)
        claims = [r.claim for r in results.values() if r.claim is not None]
        findings = self._hold(claims, inputs.now)
        gate = self._gate.update(
            inputs.now,
            inputs.units if inputs.bus_connected else None,
            inputs.poll_time if inputs.sockets is not None else None,
            {eid: r.state for eid, r in results.items()},
            self._graph,
        )
        return self._publish(results, findings, gate, inputs)

    def view(self) -> StackView:
        return StackView(
            overall=self._overall or OverallState.STOPPED,
            ready=self._ready,
            post_ready=self._post_ready,
            since=self._overall_since,
            t0=self._t0,
            elements=dict(self._elements),
            findings=(*self._findings.values(), *self._gate.findings),
            units=self._units,
            cpu=self._cpu,
        )

    # -- history -------------------------------------------------------------

    def _observe(self, inputs: Inputs) -> None:
        now = inputs.now
        if inputs.poll_time is not None and inputs.sockets is not None:
            self._scan_time = inputs.poll_time
        if inputs.bus_connected:
            for unit, snap in inputs.units.items():
                self._observe_unit(unit, snap, now)
        window = self._spec.defaults.restart_window
        for unit, times in self._rises.items():
            self._rises[unit] = [t for t in times if t > now - window]

    def _observe_unit(self, unit: str, snap: UnitSnapshot, now: float) -> None:
        if snap.invocation_id:
            self._invocations[unit] = snap.invocation_id
        last = self._restarts.get(unit)
        if last is not None and snap.n_restarts > last:
            self._rises.setdefault(unit, []).extend([now] * (snap.n_restarts - last))
        self._restarts[unit] = snap.n_restarts
        if snap.active_state == "activating" and snap.sub_state != "auto-restart":
            self._activating_since.setdefault(unit, now)
        else:
            self._activating_since.pop(unit, None)

    def _remember_ready(self, results: Mapping[str, _Result], inputs: Inputs) -> None:
        """Arm rule 3 for elements seen ready in a scan of the owners' invocation."""
        for eid, result in results.items():
            if result.state is not _READY or self._meta[eid].kind == "service":
                continue
            owners = self._owner_units(eid)
            if self._seen_this_invocation(owners, inputs):
                self._was_ready[eid] = self._invocation_key(owners)

    def _seen_this_invocation(self, owners: Iterable[str], inputs: Inputs) -> bool:
        if self._scan_time is None:
            return False
        scan = self._scan_time
        return all(
            (snap := inputs.units.get(unit)) is not None and _start_s(snap) <= scan
            for unit in owners
        )

    def _invocation_key(self, owners: Iterable[str]) -> tuple[str, ...]:
        return tuple(self._invocations.get(unit, "") for unit in owners)

    # -- rules ---------------------------------------------------------------

    def _evaluate(self, inputs: Inputs) -> dict[str, _Result]:
        spec = self._spec
        services = {sid: self._service(s, inputs) for sid, s in spec.services.items()}
        stack_stopped = all(r.state is _STOPPED for r in services.values())
        if stack_stopped:
            # The next start is a fresh one: queued units keep their old
            # invocation ids, which must not count as "was ready" (rule 3).
            self._was_ready.clear()
        else:
            services = {
                sid: self._unit_stopped(spec.services[sid], r)
                for sid, r in services.items()
            }
        results: dict[str, _Result] = dict(services)
        facts: dict[str, ListenerFact] = {}
        if inputs.sockets is not None:
            facts = {
                lid: match_listener(ls, spec.services[ls.owner].unit, inputs.sockets)
                for lid, ls in spec.listeners.items()
                if not ls.on_host
            }
        for lid, ls in spec.listeners.items():
            if not ls.on_host:
                results[lid] = self._listener(
                    ls, facts, services, inputs, stack_stopped
                )
        for fid, fs in spec.files.items():
            results[fid] = self._file(fs, services, inputs, stack_stopped)
        for eid, edge in spec.edges.items():
            results[eid] = self._edge(edge, facts, services, inputs, stack_stopped)
        return results

    def _service(self, svc: ServiceSpec, inputs: Inputs) -> _Result:
        if not inputs.bus_connected:
            return _Result(_UNKNOWN, "bus disconnected")
        snap = inputs.units.get(svc.unit)
        if snap is None:
            return _Result(_UNKNOWN, "no unit state")
        detail = f"{snap.active_state}/{snap.sub_state}"

        def unit_fault(code: FindingCode, message: str) -> _Result:
            claim = _Claim(code, svc.id, (svc.unit,), message)
            return _Result(_FAULT, f"{detail}, {message}", claim)

        defaults = self._spec.defaults
        if snap.load_state == "not-found":
            return unit_fault(FindingCode.UNIT_NOT_FOUND, "unit not found")
        if len(self._rises.get(svc.unit, ())) > defaults.restart_limit:
            return unit_fault(
                FindingCode.UNIT_RESTART_LOOP,
                f"restarted more than {defaults.restart_limit} times"
                f" in {defaults.restart_window:g} s",
            )
        if snap.active_state == "failed" or snap.sub_state == "auto-restart":
            return unit_fault(FindingCode.UNIT_FAILED, _exit_text(snap))
        if svc.unit in inputs.spinning:
            return unit_fault(
                FindingCode.CPU_SPIN,
                f"CPU above {defaults.cpu_threshold:g}%"
                f" for more than {defaults.cpu_window:g} s",
            )
        if snap.active_state == "active":
            return _Result(_READY, detail)
        if snap.active_state == "activating":
            since = self._activating_since.get(svc.unit, inputs.now)
            if inputs.now - since > svc.timeout:
                return unit_fault(
                    FindingCode.UNIT_START_TIMEOUT,
                    f"activating for more than {svc.timeout:g} s",
                )
            return _Result(_WAITING, detail)
        if snap.active_state == "inactive" and not snap.job_pending:
            return _Result(_STOPPED, detail)
        return _Result(_WAITING, detail)  # deactivating, queued job, reloading

    @staticmethod
    def _unit_stopped(svc: ServiceSpec, result: _Result) -> _Result:
        """A unit stopped while the rest of the stack is not: still grey, plus a finding."""
        if result.state is not _STOPPED:
            return result
        message = "stopped while the rest of the stack is not"
        claim = _Claim(FindingCode.UNIT_STOPPED, svc.id, (svc.unit,), message)
        return _Result(_STOPPED, f"{result.detail}, {message}", claim)

    def _owner_services(self, eid: str) -> tuple[str, ...]:
        """Service ids whose units own the element (hosts own nothing)."""
        spec = self._spec
        if eid in spec.listeners:
            return (spec.listeners[eid].owner,)
        if eid in spec.files:
            return (spec.files[eid].owner,)
        edge = spec.edges[eid]
        server = spec.listeners[edge.server]
        owners: list[str] = []
        if not edge.client_is_host:
            owners.append(edge.client)
        if not server.on_host:
            owners.append(server.owner)
        return tuple(owners)

    def _owner_units(self, eid: str) -> tuple[str, ...]:
        return tuple(self._spec.services[sid].unit for sid in self._owner_services(eid))

    def _unobservable(
        self, eid: str, services: Mapping[str, _Result], inputs: Inputs
    ) -> _Result | None:
        """``unknown`` when there is no scan or an owning unit is unknown."""
        if inputs.sockets is None:
            return _Result(_UNKNOWN, "no socket scan")
        if any(services[sid].state is _UNKNOWN for sid in self._owner_services(eid)):
            return _Result(_UNKNOWN, "unit state unknown")
        return None

    def _missing(
        self,
        eid: str,
        timeout: float | None,
        inputs: Inputs,
        stack_stopped: bool,
    ) -> tuple[ElementState, str]:
        """State of a missing element, with the fault reason (empty if no fault)."""
        if stack_stopped:
            return _STOPPED, ""
        owners = self._owner_units(eid)
        if self._was_ready.get(eid) == self._invocation_key(owners):
            return _FAULT, "dropped after being ready"
        phase = self._meta[eid].phase
        if phase == "pre":
            dependent = self._dependent_up(owners, inputs)
            if dependent is not None:
                return _FAULT, f"{dependent} is up"
        if timeout is not None:
            clocks = (
                owners
                if phase == "pre"
                else (self._spec.services[self._spec.top].unit,)
            )
            start = self._timer_start(clocks, inputs)
            if start is not None and inputs.now - start > timeout:
                return _FAULT, f"not there {timeout:g} s after start"
        return _WAITING, ""

    def _dependent_up(self, owners: tuple[str, ...], inputs: Inputs) -> str | None:
        """Rule 1: a dependent of an owner is up, as seen by a scan after it started."""
        if self._scan_time is None:
            return None
        dependents: set[str] = set()
        for unit in owners:
            dependents |= self._graph.dependents_of(unit)
        dependents -= set(owners)  # cycles, and the edge's own client and server
        for unit in sorted(dependents):
            snap = inputs.units.get(unit)
            if snap is None:
                continue
            up = snap.active_state == "active" or snap.main_pid != 0
            if up and _start_s(snap) < self._scan_time:
                return unit
        return None

    @staticmethod
    def _timer_start(units: Iterable[str], inputs: Inputs) -> float | None:
        """Rule 2 clock: when the last of ``units`` became active, if all are."""
        starts: list[float] = []
        for unit in units:
            snap = inputs.units.get(unit)
            if (
                snap is None
                or snap.active_state != "active"
                or not snap.active_enter_us
            ):
                return None
            starts.append(snap.active_enter_us / _US)
        return max(starts, default=None)

    def _listener(
        self,
        ls: ListenerSpec,
        facts: Mapping[str, ListenerFact],
        services: Mapping[str, _Result],
        inputs: Inputs,
        stack_stopped: bool,
    ) -> _Result:
        unobservable = self._unobservable(ls.id, services, inputs)
        if unobservable is not None:
            return unobservable
        fact = facts[ls.id]
        if fact.present:
            return _Result(_READY, fact.detail)
        state, reason = self._missing(ls.id, ls.timeout, inputs, stack_stopped)
        if state is not _FAULT:
            return _Result(state, fact.detail)
        svc = self._spec.services[ls.owner]
        if fact.taken_by is not None:
            units = (svc.unit,)
            if fact.taken_by.endswith(".service") and fact.taken_by != svc.unit:
                units = (svc.unit, fact.taken_by)
            message = f"port held by {fact.taken_by}"
            claim = _Claim(FindingCode.LISTENER_PORT_TAKEN, ls.id, units, message)
            return _Result(_FAULT, fact.detail, claim)
        snap = inputs.units.get(svc.unit)
        unit_claim = services[svc.id].claim
        if (
            snap is not None
            and snap.active_state != "active"
            and unit_claim is not None
        ):
            return _Result(_FAULT, fact.detail, unit_claim)
        message = f"{fact.detail} ({reason})"
        claim = _Claim(FindingCode.LISTENER_MISSING, ls.id, (svc.unit,), message)
        return _Result(_FAULT, fact.detail, claim)

    def _file(
        self,
        fs: FileSpec,
        services: Mapping[str, _Result],
        inputs: Inputs,
        stack_stopped: bool,
    ) -> _Result:
        unobservable = self._unobservable(fs.id, services, inputs)
        if unobservable is not None:
            return unobservable
        probe = inputs.files.get(fs.id)
        if probe is None:
            return _Result(_UNKNOWN, "not probed")
        if probe:
            return _Result(
                _READY, f"{fs.path} {'non-empty' if fs.non_empty else 'present'}"
            )
        detail = f"{fs.path} {'missing or empty' if fs.non_empty else 'missing'}"
        state, reason = self._missing(fs.id, fs.timeout, inputs, stack_stopped)
        if state is not _FAULT:
            return _Result(state, detail)
        unit = self._spec.services[fs.owner].unit
        claim = _Claim(FindingCode.FILE_MISSING, fs.id, (unit,), f"{detail} ({reason})")
        return _Result(_FAULT, detail, claim)

    def _edge(
        self,
        edge: EdgeSpec,
        facts: Mapping[str, ListenerFact],
        services: Mapping[str, _Result],
        inputs: Inputs,
        stack_stopped: bool,
    ) -> _Result:
        unobservable = self._unobservable(edge.id, services, inputs)
        if unobservable is not None or inputs.sockets is None:
            return unobservable or _Result(_UNKNOWN, "no socket scan")
        fact = match_edge(edge, self._spec, inputs.sockets, facts)
        if fact.present:
            return _Result(_READY, fact.detail)
        detail = _edge_missing_detail(edge, fact)
        state, reason = self._missing(edge.id, edge.timeout, inputs, stack_stopped)
        if state is not _FAULT:
            return _Result(state, detail)
        spec = self._spec
        server = spec.listeners[edge.server]
        # Hosts have no unit: look at whichever side is a service.
        client_unit = None if edge.client_is_host else spec.services[edge.client].unit
        server_unit = None if server.on_host else spec.services[server.owner].unit
        if not fact.server_present:
            code = FindingCode.EDGE_SERVER_MISSING
            message = f"server listener {edge.server} missing ({reason})"
            look = server_unit or client_unit
        else:
            code = FindingCode.EDGE_MISSING
            message = "listener present, connection missing"
            look = client_unit or server_unit
        # A fault needs an observable side, so at least one end is a service.
        assert look is not None
        return _Result(_FAULT, detail, _Claim(code, edge.id, (look,), message))

    # -- findings, overall state and events ---------------------------------

    def _hold(
        self, claims: Iterable[_Claim], now: float
    ) -> dict[tuple[FindingCode, str], Finding]:
        """Keep a raised finding (and its start) while some rule still claims it."""
        held: dict[tuple[FindingCode, str], Finding] = {}
        for claim in claims:
            key = (claim.code, claim.element)
            if key in held:
                continue
            old = self._findings.get(key)
            held[key] = old or Finding(
                claim.code, claim.element, claim.units, claim.message, now
            )
        return held

    def _publish(
        self,
        results: Mapping[str, _Result],
        findings: Mapping[tuple[FindingCode, str], Finding],
        gate: GateUpdate,
        inputs: Inputs,
    ) -> list[Event]:
        now = inputs.now
        views = self._views(results, findings, now)
        overall, ready, post_ready = self._overall_of(views, findings.values())
        if (
            self._overall is OverallState.STOPPED
            and overall is not OverallState.STOPPED
        ):
            self._t0 = now
        t = f"(+{now - self._t0:.1f} s)"

        events: list[Event] = []
        for eid, view in views.items():
            prev = self._elements.get(eid)
            if prev is not None and (prev.state, prev.detail) == (
                view.state,
                view.detail,
            ):
                continue
            text = f"{view.kind} {eid} {view.state}: {view.detail} {t}"
            old = prev.state.value if prev is not None else None
            events.append(
                Event(now, view.kind, eid, old, view.state.value, text, view.finding)
            )
        gone = [f for key, f in self._findings.items() if key not in findings]
        events.extend(_cleared_event(f, now, t) for f in (*gone, *gate.cleared))
        for key, finding in findings.items():
            if key not in self._findings:
                text = f"FAULT {finding.code} {finding.element}: {_look(finding)}"
                events.append(
                    Event(
                        now,
                        "finding",
                        finding.element,
                        None,
                        finding.code.value,
                        text,
                        finding,
                    )
                )
        for finding in gate.raised:
            text = f"GATE {finding.code}: {_look(finding)}"
            events.append(
                Event(
                    now,
                    "gate",
                    finding.element,
                    None,
                    finding.code.value,
                    text,
                    finding,
                )
            )
        if overall is not self._overall:
            old = self._overall.value if self._overall is not None else None
            events.append(
                Event(
                    now,
                    "stack",
                    "stack",
                    old,
                    overall.value,
                    f"stack {overall} {t}",
                    None,
                )
            )
            self._overall_since = now
        if post_ready != self._post_ready:
            old_post, new_post = (
                ("post-ready", "post-not-ready")
                if self._post_ready
                else ("post-not-ready", "post-ready")
            )
            events.append(
                Event(
                    now,
                    "stack",
                    "stack",
                    old_post,
                    new_post,
                    f"stack {new_post} {t}",
                    None,
                )
            )

        self._overall = overall
        self._ready = ready
        self._post_ready = post_ready
        self._elements = views
        self._findings = dict(findings)
        self._units = dict(inputs.units)
        self._cpu = dict(inputs.cpu)
        return events

    def _views(
        self,
        results: Mapping[str, _Result],
        findings: Mapping[tuple[FindingCode, str], Finding],
        now: float,
    ) -> dict[str, ElementView]:
        views: dict[str, ElementView] = {}
        for eid, result in results.items():
            meta = self._meta[eid]
            prev = self._elements.get(eid)
            since = (
                prev.since if prev is not None and prev.state is result.state else now
            )
            claim = result.claim
            finding = (
                findings[(claim.code, claim.element)] if claim is not None else None
            )
            views[eid] = ElementView(
                eid,
                meta.kind,
                result.state,
                since,
                result.detail,
                meta.phase,
                meta.required,
                finding,
            )
        return views

    @staticmethod
    def _overall_of(
        views: Mapping[str, ElementView], findings: Iterable[Finding]
    ) -> tuple[OverallState, bool, bool]:
        services = [v for v in views.values() if v.kind == "service"]
        counted = [v for v in views.values() if v.required and v.phase == "pre"]
        post = [
            v
            for v in views.values()
            if v.required and v.phase == "post" and v.kind != "service"
        ]
        ready = all(v.state is _READY for v in counted)
        post_ready = all(v.state is _READY for v in post)
        if all(v.state is _STOPPED for v in services):
            return OverallState.STOPPED, ready, post_ready
        if ready:
            return OverallState.READY, ready, post_ready
        if any(v.state is _FAULT for v in counted) or any(
            f.code in _UNIT_CODES for f in findings
        ):
            return OverallState.FAULT, ready, post_ready
        return OverallState.STARTING, ready, post_ready
