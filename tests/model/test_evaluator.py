"""Evaluator rules: element states, faults, findings, the overall state, events."""

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from service_control_ioc.config.load import load_config
from service_control_ioc.config.spec import FileSpec, StackSpec
from service_control_ioc.model.deps import DependencyGraph
from service_control_ioc.model.evaluator import Evaluator, Inputs
from service_control_ioc.model.types import (
    ElementState,
    Event,
    FindingCode,
    JobOutcome,
    OverallState,
    UnitSnapshot,
)
from service_control_ioc.observe.procfs import (
    TCP_ESTABLISHED,
    TCP_LISTEN,
    UDP_ESTABLISHED,
    UDP_UNCONN,
    OwnedSock,
    Sock,
    SocketTable,
)

SPEC = load_config(Path(__file__).parents[2] / "teststack" / "config" / "scdemo.yaml")
UNITS = {sid: s.unit for sid, s in SPEC.services.items()}
ALL_UNIT = "scdemo-all.service"
AFTER = {
    UNITS["controller"]: [UNITS["relay"]],
    UNITS["relay"]: [UNITS["sink@1"], UNITS["sink@2"]],
    ALL_UNIT: list(UNITS.values()),
}
T = 1_700_000_000.0
LISTENERS = tuple(lid for lid, ls in SPEC.listeners.items() if not ls.on_host)
EVERYTHING = LISTENERS + tuple(SPEC.edges)
SINK2_GONE = (
    "sink@2.udp",
    "sink@2.stats",
    "relay -> sink@2.stats",
    "relay -> sink@2.udp",
    "sink@2 -> relay.pull",
)

ready = ElementState.READY
fault = ElementState.FAULT
waiting = ElementState.WAITING
stopped = ElementState.STOPPED
unknown = ElementState.UNKNOWN


def us(t: float) -> int:
    return int(t * 1_000_000)


def graph(after: Mapping[str, Iterable[str]] = AFTER) -> DependencyGraph:
    return DependencyGraph(after, [*UNITS.values(), ALL_UNIT], exclude={ALL_UNIT})


def make(
    spec: StackSpec = SPEC, after: Mapping[str, Iterable[str]] = AFTER
) -> Evaluator:
    return Evaluator(spec, graph(after), started_at=T)


# shorthand -> (active_state, sub_state, job_pending, main process running)
_SHORT = {
    "active": ("active", "running", False, True),
    "stopped": ("inactive", "dead", False, False),
    "queued": ("inactive", "dead", True, False),
    "activating": ("activating", "start", False, True),
    "deactivating": ("deactivating", "stop-sigterm", False, True),
    "failed": ("failed", "failed", False, False),
    "auto-restart": ("activating", "auto-restart", False, False),
}


def snap(
    sid: str,
    state: str = "active",
    *,
    since: float = T,
    run: int = 1,
    **changes: object,
) -> UnitSnapshot:
    """A unit snapshot; ``run`` > 1 gives a new invocation id (a later start)."""
    active, sub, job, running = _SHORT[state]
    failed = state in ("failed", "auto-restart")
    base = UnitSnapshot(
        name=UNITS[sid],
        load_state="loaded",
        active_state=active,
        sub_state=sub,
        result="exit-code" if failed else "success",
        job_pending=job,
        n_restarts=0,
        main_pid=1000 if running else 0,
        exec_main_code=1 if failed else 0,
        exec_main_status=1 if failed else 0,
        exec_main_start_us=us(since),
        active_enter_us=us(since) if state in ("active", "deactivating") else 0,
        control_group=f"/system.slice/{UNITS[sid]}",
        invocation_id=f"inv-{sid}" if run == 1 else f"inv-{sid}-run{run}",
        after=(),
    )
    return replace(base, **changes)


def _sockets(eid: str, i: int) -> list[OwnedSock]:
    if eid in SPEC.listeners:
        ls = SPEC.listeners[eid]
        state = TCP_LISTEN if ls.proto == "tcp" else UDP_UNCONN
        local = ls.address or "0.0.0.0"
        sock = Sock(ls.proto, local, ls.port, "0.0.0.0", 0, state, i)
        return [OwnedSock(sock, 1, UNITS[ls.owner])]
    edge = SPEC.edges[eid]
    server = SPEC.listeners[edge.server]
    if edge.sender == "remote":
        return []
    ip = server.address or "127.0.0.1"
    if edge.proto == "tcp":
        sock = Sock("tcp", "127.0.0.1", 40000 + i, ip, server.port, TCP_ESTABLISHED, i)
    elif edge.sender == "connected":
        sock = Sock("udp", "127.0.0.1", 41000 + i, ip, server.port, UDP_ESTABLISHED, i)
    else:
        sock = Sock("udp", "0.0.0.0", 45000 + i, "0.0.0.0", 0, UDP_UNCONN, i)
    return [OwnedSock(sock, 1, UNITS[edge.client])]


def scenario(
    now: float,
    *,
    state: str = "active",
    since: float = T,
    run: int = 1,
    units: Mapping[str, str | UnitSnapshot | None] | None = None,
    up: Iterable[str] = (),
    extra: Iterable[OwnedSock] = (),
    bus: bool = True,
    scan: bool = True,
    poll: bool = True,
    spinning: Iterable[str] = (),
    files: Mapping[str, bool] | None = None,
) -> Inputs:
    """Build Inputs: every service in ``state`` unless ``units`` says otherwise
    (None = no snapshot), and a socket table where the ``up`` elements are present.
    ``poll=False`` means the table is the previous scan (a unit-change update)."""
    snaps: dict[str, UnitSnapshot] = {}
    for sid in SPEC.services:
        value = (units or {}).get(sid, state)
        if value is None:
            continue
        if isinstance(value, UnitSnapshot):
            s = value
        else:
            s = snap(sid, value, since=since, run=run)
        snaps[s.name] = s
    entries = list(extra)
    for i, eid in enumerate(up):
        entries.extend(_sockets(eid, i + 1))
    return Inputs(
        now=now,
        bus_connected=bus,
        units=snaps,
        sockets=SocketTable(tuple(entries)) if scan else None,
        poll_time=now if scan and poll else None,
        cpu={u: None for u in snaps},
        spinning=frozenset(UNITS[s] for s in spinning),
        files=files or {},
    )


def without(*ids: str) -> tuple[str, ...]:
    return tuple(x for x in EVERYTHING if x not in ids)


def running(
    spec: StackSpec = SPEC, after: Mapping[str, Iterable[str]] = AFTER
) -> Evaluator:
    """An evaluator that has seen the whole stack up at T+1."""
    ev = make(spec, after)
    ev.update(scenario(T + 1, up=EVERYTHING, files=dict.fromkeys(spec.files, True)))
    assert ev.view().overall is OverallState.READY
    return ev


def st(ev: Evaluator, eid: str) -> ElementState:
    return ev.view().elements[eid].state


def codes(ev: Evaluator) -> dict[str, FindingCode]:
    return {f.element: f.code for f in ev.view().findings}


def texts(events: Sequence[Event], kind: str | None = None) -> list[str]:
    return [e.text for e in events if kind is None or e.kind == kind]


def with_file(spec: StackSpec = SPEC) -> StackSpec:
    f = FileSpec("relay:file0", "relay", "/run/relay.ready", False, "pre", True, 30.0)
    relay = replace(spec.services["relay"], files=(f.id,))
    return replace(spec, files={f.id: f}, services={**spec.services, "relay": relay})


# -- starting ----------------------------------------------------------------


def test_tool_starts_after_stack_up() -> None:
    ev = make()
    events = ev.update(scenario(T + 100, up=EVERYTHING))
    view = ev.view()
    assert {e.state for e in view.elements.values()} == {ready}
    assert view.overall is OverallState.READY
    assert view.ready
    assert view.post_ready
    assert view.findings == ()
    assert view.t0 == T
    assert not [e for e in events if e.kind == "finding" or e.new == "fault"]
    assert "stack ready (+100.0 s)" in texts(events)


def test_clean_start_goes_ready_without_faults() -> None:
    ev = make()
    sinks_up = ("sink@1.udp", "sink@1.stats", "sink@2.udp", "sink@2.stats")
    relay_up = (
        *sinks_up,
        *without(
            "controller -> relay.rep",
            "controller -> relay.router",
            "controller -> relay.pub",
            "controller -> relay.http",
        ),
    )
    sinks = {
        "sink@1": snap("sink@1", since=T + 3),
        "sink@2": snap("sink@2", since=T + 3),
    }
    relay = {**sinks, "relay": snap("relay", since=T + 6)}
    steps = [
        scenario(T, state="stopped"),
        scenario(T + 1, state="queued", poll=False),
        scenario(
            T + 2,
            state="queued",
            units={"sink@1": "activating", "sink@2": "activating"},
            since=T + 2,
            poll=False,
        ),
        scenario(T + 3, state="queued", units=sinks, poll=False),
        scenario(T + 4, state="queued", units=sinks, up=sinks_up),
        scenario(
            T + 5,
            state="queued",
            units={**sinks, "relay": snap("relay", "activating", since=T + 5)},
            up=sinks_up,
            poll=False,
        ),
        scenario(T + 6, state="queued", units=relay, up=sinks_up, poll=False),
        scenario(T + 7, state="queued", units=relay, up=relay_up),
        scenario(
            T + 8,
            units={**relay, "controller": snap("controller", since=T + 8)},
            up=relay_up,
            poll=False,
        ),
        scenario(
            T + 9,
            units={**relay, "controller": snap("controller", since=T + 8)},
            up=EVERYTHING,
        ),
    ]
    events = [e for inputs in steps for e in ev.update(inputs)]
    assert not [e for e in events if e.kind in ("finding", "gate") or e.new == "fault"]
    assert [e.new for e in events if e.kind == "stack" and "post" not in e.text] == [
        "stopped",
        "starting",
        "ready",
    ]
    assert ev.view().t0 == T + 1
    assert ev.view().ready


# -- services ----------------------------------------------------------------


def test_service_unknown_without_bus_or_snapshot() -> None:
    ev = make()
    ev.update(scenario(T + 1, up=EVERYTHING, bus=False))
    assert {st(ev, sid) for sid in SPEC.services} == {unknown}
    ev = make()
    ev.update(scenario(T + 1, up=EVERYTHING, units={"relay": None}))
    assert st(ev, "relay") is unknown
    assert st(ev, "controller") is ready


def test_unit_not_found() -> None:
    ev = make()
    ev.update(
        scenario(
            T + 1,
            up=EVERYTHING,
            units={"relay": snap("relay", "stopped", load_state="not-found")},
        )
    )
    assert st(ev, "relay") is fault
    assert codes(ev)["relay"] is FindingCode.UNIT_NOT_FOUND
    assert ev.view().overall is OverallState.FAULT


def test_unit_failed() -> None:
    ev = running()
    ev.update(
        scenario(
            T + 5,
            up=without(*SINK2_GONE),
            units={"sink@2": snap("sink@2", "failed", exec_main_status=3)},
        )
    )
    assert st(ev, "sink@2") is fault
    finding = next(f for f in ev.view().findings if f.element == "sink@2")
    assert finding.code is FindingCode.UNIT_FAILED
    assert "exited with status 3" in finding.message
    assert finding.units == (UNITS["sink@2"],)


def test_auto_restart_is_fault() -> None:
    ev = running()
    restarting = snap("sink@1", "auto-restart", exec_main_code=2, exec_main_status=9)
    ev.update(scenario(T + 5, up=EVERYTHING, units={"sink@1": restarting}))
    assert st(ev, "sink@1") is fault
    finding = next(f for f in ev.view().findings if f.element == "sink@1")
    assert finding.code is FindingCode.UNIT_FAILED
    assert "killed by signal SIGKILL" in finding.message


def test_restart_loop() -> None:
    ev = make()
    for i in range(4):
        ev.update(
            scenario(
                T + 1 + i, up=EVERYTHING, units={"sink@1": snap("sink@1", n_restarts=i)}
            )
        )
    assert st(ev, "sink@1") is ready  # three rises: at the limit, not over it
    events = ev.update(
        scenario(T + 5, up=EVERYTHING, units={"sink@1": snap("sink@1", n_restarts=4)})
    )
    assert st(ev, "sink@1") is fault
    assert codes(ev)["sink@1"] is FindingCode.UNIT_RESTART_LOOP
    assert any(t.startswith("FAULT unit_restart_loop sink@1: ") for t in texts(events))
    events = ev.update(
        scenario(T + 100, up=EVERYTHING, units={"sink@1": snap("sink@1", n_restarts=4)})
    )
    assert st(ev, "sink@1") is ready
    assert "CLEARED unit_restart_loop sink@1 (+100.0 s)" in texts(events)


def test_cpu_spin() -> None:
    ev = running()
    events = ev.update(scenario(T + 5, up=EVERYTHING, spinning=["relay"]))
    assert st(ev, "relay") is fault
    assert codes(ev)["relay"] is FindingCode.CPU_SPIN
    assert ev.view().overall is OverallState.FAULT
    assert any(t.startswith("FAULT cpu_spin relay: ") for t in texts(events))
    events = ev.update(scenario(T + 6, up=EVERYTHING))
    assert st(ev, "relay") is ready
    assert "CLEARED cpu_spin relay (+6.0 s)" in texts(events)


def test_service_ready_when_active() -> None:
    ev = make()
    ev.update(scenario(T + 1, up=EVERYTHING))
    assert st(ev, "relay") is ready
    assert ev.view().elements["relay"].detail == "active/running"


def test_service_waiting_when_activating_deactivating_or_job() -> None:
    ev = make()
    ev.update(
        scenario(
            T + 1,
            units={"sink@1": "activating", "sink@2": "deactivating", "relay": "queued"},
            state="queued",
        )
    )
    assert st(ev, "sink@1") is waiting
    assert st(ev, "sink@2") is waiting
    assert st(ev, "relay") is waiting
    assert codes(ev) == {}


def test_unit_start_timeout() -> None:
    ev = make()
    ev.update(scenario(T, state="queued", units={"relay": "activating"}))
    ev.update(scenario(T + 30, state="queued", units={"relay": "activating"}))
    assert st(ev, "relay") is waiting
    ev.update(scenario(T + 31, state="queued", units={"relay": "activating"}))
    assert st(ev, "relay") is fault
    assert codes(ev)["relay"] is FindingCode.UNIT_START_TIMEOUT


def test_whole_stack_stop_is_grey() -> None:
    ev = running()
    events = ev.update(scenario(T + 200, state="stopped"))
    view = ev.view()
    assert {e.state for e in view.elements.values()} == {stopped}
    assert view.overall is OverallState.STOPPED
    assert view.findings == ()
    assert "stack stopped (+200.0 s)" in texts(events)


def no_faults(events: Sequence[Event]) -> bool:
    return not [e for e in events if e.kind in ("finding", "gate") or e.new == "fault"]


def stopping(sid: str) -> UnitSnapshot:
    """Still running, with its stop job queued."""
    return snap(sid, job_pending=True, job_type="stop")


def test_orderly_whole_stack_stop_has_no_faults() -> None:
    # systemctl stop scdemo-all: every unit gets a stop job, then they stop in
    # reverse After= order. The stack is in transition until the last job ends.
    ev = running()
    steps = [
        scenario(
            T + 200,
            up=EVERYTHING,
            units={sid: stopping(sid) for sid in UNITS},
            poll=False,
        ),
        scenario(
            T + 201,
            up=without(*(e for e in EVERYTHING if e.startswith("controller -> "))),
            units={
                "controller": "stopped",
                "relay": stopping("relay"),
                "sink@1": stopping("sink@1"),
                "sink@2": stopping("sink@2"),
            },
        ),
        scenario(
            T + 202,
            up=("sink@1.udp", "sink@1.stats", "sink@2.udp", "sink@2.stats"),
            units={
                "controller": "stopped",
                "relay": "stopped",
                "sink@1": stopping("sink@1"),
                "sink@2": stopping("sink@2"),
            },
        ),
        scenario(
            T + 203,
            units={
                "controller": "stopped",
                "relay": "stopped",
                "sink@1": snap("sink@1", "deactivating", job_pending=True),
                "sink@2": "stopped",
            },
        ),
    ]
    events = []
    for inputs in steps:
        events += ev.update(inputs)
        assert ev.view().overall is not OverallState.FAULT
        assert ev.view().findings == ()
    assert ev.view().overall is OverallState.STOPPING
    assert st(ev, "controller") is stopped
    assert st(ev, "controller -> relay.rep") is waiting
    events += ev.update(scenario(T + 204, state="stopped"))
    assert no_faults(events)
    assert ev.view().overall is OverallState.STOPPED


def test_staged_start_split_delivery_has_no_faults() -> None:
    # The start jobs are announced over two reads: the sinks are queued while
    # the relay and controller still look stopped with no job.
    ev = make()
    events = ev.update(scenario(T, state="stopped"))
    events += ev.update(
        scenario(
            T + 1,
            state="stopped",
            units={"sink@1": "queued", "sink@2": "queued"},
            poll=False,
        )
    )
    assert ev.view().overall is OverallState.STARTING
    assert st(ev, "relay") is stopped
    events += ev.update(scenario(T + 1, state="queued", poll=False))
    assert no_faults(events)
    assert ev.view().overall is OverallState.STARTING


def test_manual_stop_waits_during_its_job_then_faults() -> None:
    ev = running()
    events = ev.update(
        scenario(
            T + 50,
            up=without(*SINK2_GONE),
            units={"sink@2": snap("sink@2", "deactivating", job_pending=True)},
        )
    )
    assert no_faults(events)
    assert st(ev, "sink@2.stats") is waiting
    assert st(ev, "relay -> sink@2.stats") is waiting
    ev.update(scenario(T + 51, up=without(*SINK2_GONE), units={"sink@2": "stopped"}))
    assert codes(ev)["sink@2"] is FindingCode.UNIT_STOPPED
    assert st(ev, "sink@2.stats") is fault
    assert ev.view().overall is OverallState.FAULT


SINK2_STOPPED_UP = without(*SINK2_GONE)


def sink2_stopped(now: float, controller: UnitSnapshot) -> Inputs:
    """sink@2 stopped (its job done) while the controller has a job pending."""
    return scenario(
        now, up=SINK2_STOPPED_UP, units={"sink@2": "stopped", "controller": controller}
    )


def assert_sink2_faults(ev: Evaluator) -> None:
    assert codes(ev)["sink@2"] is FindingCode.UNIT_STOPPED
    assert st(ev, "sink@2.stats") is fault
    assert st(ev, "relay -> sink@2.stats") is fault
    assert ev.view().overall is OverallState.FAULT


@pytest.mark.parametrize(
    "controller",
    [snap("controller", "queued"), stopping("controller")],
    ids=["queued", "active-with-job"],
)
def test_stuck_job_stops_masking_after_its_timeout(controller: UnitSnapshot) -> None:
    timeout = SPEC.services["controller"].timeout
    ev = running()
    events = ev.update(sink2_stopped(T + 50, controller))
    events += ev.update(sink2_stopped(T + 50 + timeout - 1, controller))
    assert no_faults(events)  # within the bound: still a transition
    # A queued start reads starting; a stop job on a running unit reads stopping.
    expected = (
        OverallState.STOPPING
        if controller.active_state == "active"
        else OverallState.STARTING
    )
    assert ev.view().overall is expected
    ev.update(sink2_stopped(T + 50 + timeout + 1, controller))
    assert_sink2_faults(ev)
    ev.update(sink2_stopped(T + 5000, controller))
    assert_sink2_faults(ev)


def test_job_timer_resets_when_the_job_clears() -> None:
    timeout = SPEC.services["controller"].timeout
    busy = snap("controller", job_pending=True)
    ev = running()
    events = ev.update(sink2_stopped(T + 50, busy))
    events += ev.update(sink2_stopped(T + 70, snap("controller")))  # job done
    assert codes(ev)["sink@2"] is FindingCode.UNIT_STOPPED  # no transition now
    events = ev.update(sink2_stopped(T + 75, busy))  # a new job: timer restarts
    events += ev.update(sink2_stopped(T + 75 + timeout - 1, busy))
    assert ev.view().findings == ()  # the new job masks again, within its bound
    ev.update(sink2_stopped(T + 75 + timeout + 1, busy))
    assert_sink2_faults(ev)


def test_manual_stop_of_one_unit_is_fault() -> None:
    ev = running()
    ev.update(scenario(T + 50, up=without(*SINK2_GONE), units={"sink@2": "stopped"}))
    view = ev.view()
    assert st(ev, "sink@2") is stopped
    assert codes(ev)["sink@2"] is FindingCode.UNIT_STOPPED
    assert st(ev, "sink@2.stats") is fault
    assert st(ev, "relay -> sink@2.stats") is fault
    edge_finding = view.elements["relay -> sink@2.stats"].finding
    assert edge_finding is not None
    assert edge_finding.code is FindingCode.EDGE_SERVER_MISSING
    assert edge_finding.units == (UNITS["sink@2"],)
    assert view.overall is OverallState.FAULT
    assert not view.ready


# -- listener, file and edge states -----------------------------------------


def test_unknown_without_socket_scan() -> None:
    ev = running(with_file())
    events = ev.update(scenario(T + 5, scan=False, files={"relay:file0": True}))
    view = ev.view()
    for eid, element in view.elements.items():
        expected = ready if element.kind == "service" else unknown
        assert element.state is expected, eid
    assert not view.ready
    assert view.overall is OverallState.STARTING
    assert not [e for e in events if e.kind == "finding"]


def test_unknown_when_owner_unknown() -> None:
    ev = running()
    ev.update(scenario(T + 5, up=EVERYTHING, units={"relay": None}))
    assert st(ev, "relay.rep") is unknown
    assert st(ev, "controller -> relay.rep") is unknown  # server's service unknown
    assert st(ev, "relay -> hw.ctrl") is unknown  # host server: only the client counts
    assert st(ev, "sink@1.stats") is ready


def test_stopped_when_whole_stack_stopped() -> None:
    ev = make()
    ev.update(scenario(T + 1, state="stopped"))
    assert st(ev, "relay.rep") is stopped
    assert st(ev, "controller -> relay.rep") is stopped


def test_rule3_dropped_after_ready_is_fault() -> None:
    ev = running(after={})
    ev.update(scenario(T + 5, up=without("relay.pull")))
    assert st(ev, "relay.pull") is fault  # post, no timeout: only rule 3 applies
    assert codes(ev)["relay.pull"] is FindingCode.LISTENER_MISSING


def test_restart_resets_was_ready() -> None:
    gone = without("relay.pull", "sink@1 -> relay.pull", "sink@2 -> relay.pull")
    ev = running(after={})
    # A new invocation starts; the last scan (T+1) still shows the old sockets.
    ev.update(
        scenario(
            T + 10,
            up=EVERYTHING,
            poll=False,
            units={
                "relay": snap(
                    "relay", "activating", since=T + 10, invocation_id="inv-2"
                )
            },
        )
    )
    ev.update(
        scenario(
            T + 11,
            up=gone,
            units={"relay": snap("relay", since=T + 10, invocation_id="inv-2")},
        )
    )
    assert st(ev, "relay.pull") is waiting
    assert st(ev, "sink@1 -> relay.pull") is waiting
    # Same invocation: the drop is a fault.
    ev = running(after={})
    ev.update(scenario(T + 11, up=gone))
    assert st(ev, "relay.pull") is fault
    assert st(ev, "sink@1 -> relay.pull") is fault


def test_rule1_dependent_up_makes_listener_fault() -> None:
    ev = make()
    ev.update(scenario(T + 5, up=without("relay.rep")))
    assert st(ev, "relay.rep") is fault
    finding = ev.view().elements["relay.rep"].finding
    assert finding is not None
    assert finding.code is FindingCode.LISTENER_MISSING
    assert finding.units == (UNITS["relay"],)
    assert UNITS["controller"] in finding.message


def test_rule1_dependent_main_pid_counts() -> None:
    ev = make()
    ev.update(
        scenario(T + 5, up=without("relay.rep"), units={"controller": "activating"})
    )
    assert st(ev, "relay.rep") is fault


def test_rule1_needs_a_scan_after_the_dependent_started() -> None:
    controller = snap("controller", "activating", since=T + 5)
    ev = make()
    ev.update(scenario(T + 4, up=without("relay.rep"), units={"controller": "queued"}))
    ev.update(
        scenario(
            T + 5, up=without("relay.rep"), units={"controller": controller}, poll=False
        )
    )
    assert st(ev, "relay.rep") is waiting
    ev.update(
        scenario(T + 6, up=without("relay.rep"), units={"controller": controller})
    )
    assert st(ev, "relay.rep") is fault


def test_rule1_not_for_post() -> None:
    ev = make()
    ev.update(
        scenario(
            T + 5,
            up=without("relay.pull", "sink@1 -> relay.pull", "sink@2 -> relay.pull"),
        )
    )
    assert st(ev, "relay.pull") is waiting


def test_rule1_ignores_owner_in_cycle() -> None:
    ev = make(after={UNITS["relay"]: [UNITS["relay"]]})
    ev.update(scenario(T + 5, up=without("relay.rep")))
    assert st(ev, "relay.rep") is waiting


def test_rule1_ignores_edge_endpoints() -> None:
    ev = make()
    ev.update(scenario(T + 0.5, up=without("controller -> relay.rep")))
    assert st(ev, "relay.rep") is ready
    assert st(ev, "controller -> relay.rep") is waiting


def test_rule2_timeout() -> None:
    ev = make(after={})
    ev.update(scenario(T + 30, up=without("relay.rep", "controller -> relay.router")))
    assert st(ev, "relay.rep") is waiting
    assert st(ev, "controller -> relay.router") is waiting
    ev.update(scenario(T + 31, up=without("relay.rep", "controller -> relay.router")))
    assert st(ev, "relay.rep") is fault
    assert st(ev, "controller -> relay.router") is fault
    assert codes(ev)["controller -> relay.router"] is FindingCode.EDGE_MISSING


def test_rule2_counts_from_owner_active_enter() -> None:
    ev = make(after={})
    ev.update(
        scenario(
            T + 31,
            up=without("relay.rep"),
            units={"relay": snap("relay", since=T + 10)},
        )
    )
    assert st(ev, "relay.rep") is waiting


def test_post_edge_waits_until_top_timeout() -> None:
    edge = replace(SPEC.edges["relay -> sink@1.udp"], timeout=5.0)
    spec = replace(SPEC, edges={**SPEC.edges, edge.id: edge})
    up = without(edge.id)
    top = {"controller": snap("controller", since=T + 10)}
    ev = make(spec)
    ev.update(scenario(T + 14, up=up, units=top))
    assert st(ev, edge.id) is waiting
    ev.update(scenario(T + 16, up=up, units=top))
    assert st(ev, edge.id) is fault
    assert codes(ev)[edge.id] is FindingCode.EDGE_MISSING
    # Without a timeout a post edge waits for ever.
    ev = make()
    ev.update(scenario(T + 1000, up=up, units=top))
    assert st(ev, edge.id) is waiting


def test_waiting_otherwise() -> None:
    ev = make(after={})
    ev.update(scenario(T + 1, up=()))
    assert st(ev, "relay.rep") is waiting
    assert st(ev, "relay -> sink@1.stats") is waiting
    assert ev.view().overall is OverallState.STARTING


# -- findings ----------------------------------------------------------------


def test_port_taken() -> None:
    other = OwnedSock(
        Sock("tcp", "127.0.0.1", 20100, "0.0.0.0", 0, TCP_LISTEN, 99),
        7,
        "other.service",
    )
    ev = make(after={})
    ev.update(scenario(T + 31, up=without("relay.rep"), extra=[other]))
    finding = ev.view().elements["relay.rep"].finding
    assert finding is not None
    assert finding.code is FindingCode.LISTENER_PORT_TAKEN
    assert finding.message == "port held by other.service"
    assert finding.units == (UNITS["relay"], "other.service")


def test_listener_uses_unit_finding_when_unit_not_active() -> None:
    ev = running()
    events = ev.update(
        scenario(T + 5, up=without(*SINK2_GONE), units={"sink@2": "failed"})
    )
    listener = ev.view().elements["sink@2.stats"]
    assert listener.state is fault
    assert listener.finding is not None
    assert listener.finding.code is FindingCode.UNIT_FAILED
    assert listener.finding.element == "sink@2"
    assert len([t for t in texts(events) if t.startswith("FAULT unit_failed")]) == 1
    assert FindingCode.LISTENER_MISSING not in codes(ev).values()


def test_listener_missing_when_unit_has_no_finding() -> None:
    ev = running()
    ev.update(
        scenario(T + 5, up=without(*SINK2_GONE), units={"sink@2": "deactivating"})
    )
    assert st(ev, "sink@2") is waiting
    assert codes(ev)["sink@2.stats"] is FindingCode.LISTENER_MISSING


def test_edge_missing_names_client() -> None:
    ev = running()
    ev.update(scenario(T + 5, up=without("controller -> relay.rep")))
    finding = ev.view().elements["controller -> relay.rep"].finding
    assert finding is not None
    assert finding.code is FindingCode.EDGE_MISSING
    assert finding.message == "listener present, connection missing"
    assert finding.units == (UNITS["controller"],)


def test_edge_server_missing_names_server() -> None:
    ev = running(after={})
    ev.update(scenario(T + 5, up=without("relay.pull", "sink@1 -> relay.pull")))
    finding = ev.view().elements["sink@1 -> relay.pull"].finding
    assert finding is not None
    assert finding.code is FindingCode.EDGE_SERVER_MISSING
    assert finding.units == (UNITS["relay"],)


def test_connected_udp_edge_with_receiver_gone_is_server_missing() -> None:
    """Ruling 20: the relay's connected socket to sink@1.udp outlives the
    receiver; the edge reports the missing server, like any other edge."""
    ev = running(after={})
    ev.update(scenario(T + 5, up=without("sink@1.udp")))  # the edge socket stays
    edge = ev.view().elements["relay -> sink@1.udp"]
    assert edge.state is fault
    assert edge.detail == "server sink@1.udp missing"
    assert edge.finding is not None
    assert edge.finding.code is FindingCode.EDGE_SERVER_MISSING
    assert edge.finding.units == (UNITS["sink@1"],)


def test_host_client_edge_looks_at_server() -> None:
    ev = running(after={})
    ev.update(scenario(T + 5, up=without("relay.udp")))
    finding = ev.view().elements["hw -> relay.udp"].finding
    assert finding is not None
    assert finding.code is FindingCode.EDGE_SERVER_MISSING
    assert finding.units == (UNITS["relay"],)


def test_file_missing() -> None:
    spec = with_file()
    ev = running(spec)
    assert st(ev, "relay:file0") is ready
    ev.update(scenario(T + 5, up=EVERYTHING, files={"relay:file0": False}))
    assert st(ev, "relay:file0") is fault
    finding = ev.view().elements["relay:file0"].finding
    assert finding is not None
    assert finding.code is FindingCode.FILE_MISSING
    assert finding.units == (UNITS["relay"],)
    assert "/run/relay.ready" in finding.message
    ev.update(scenario(T + 6, up=EVERYTHING))
    assert st(ev, "relay:file0") is unknown  # no probe result


def test_fault_and_cleared_events() -> None:
    ev = running()
    events = ev.update(scenario(T + 5, up=without("controller -> relay.rep")))
    raised = [e for e in events if e.kind == "finding"]
    assert len(raised) == 1
    assert raised[0].finding is not None
    assert raised[0].new == "edge_missing"
    events = ev.update(scenario(T + 6, up=EVERYTHING))
    cleared = [e for e in events if e.kind == "cleared"]
    assert [e.element for e in cleared] == ["controller -> relay.rep"]
    assert cleared[0].old == "edge_missing"


# -- overall and events ------------------------------------------------------


def test_overall_states() -> None:
    ev = make()
    ev.update(scenario(T, state="stopped"))
    assert ev.view().overall is OverallState.STOPPED
    ev.update(scenario(T + 1, state="queued", units={"sink@1": "activating"}))
    assert ev.view().overall is OverallState.STARTING
    ev.update(scenario(T + 2, up=EVERYTHING))
    assert ev.view().overall is OverallState.READY
    ev.update(scenario(T + 3, up=without("relay.rep")))
    assert ev.view().overall is OverallState.FAULT


def test_post_elements_do_not_count_for_ready() -> None:
    post = [eid for eid, e in ev_elements() if e == "post"]
    ev = make(after={})
    ev.update(scenario(T + 1, up=without(*post)))
    assert ev.view().overall is OverallState.READY
    assert not ev.view().post_ready
    events = ev.update(scenario(T + 2, up=EVERYTHING))
    assert "stack post-ready (+2.0 s)" in texts(events)
    events = ev.update(scenario(T + 3, up=without("relay.pull")))
    assert st(ev, "relay.pull") is fault
    assert ev.view().overall is OverallState.READY
    assert "stack post-not-ready (+3.0 s)" in texts(events)


def ev_elements() -> list[tuple[str, str]]:
    return [(lid, ls.phase) for lid, ls in SPEC.listeners.items() if not ls.on_host] + [
        (eid, e.phase) for eid, e in SPEC.edges.items()
    ]


def test_bus_disconnect_unknown_then_recovers() -> None:
    ev = running()
    events = ev.update(scenario(T + 5, up=EVERYTHING, bus=False))
    view = ev.view()
    assert {st(ev, sid) for sid in SPEC.services} == {unknown}
    assert {e.state for e in view.elements.values()} == {unknown}
    assert not view.ready
    assert not [e for e in events if e.kind == "finding"]
    events = ev.update(scenario(T + 6, up=EVERYTHING))
    assert ev.view().ready
    assert not [e for e in events if e.kind == "finding" or e.new == "fault"]


def test_no_events_for_unchanged_elements() -> None:
    ev = running()
    assert ev.update(scenario(T + 2, up=EVERYTHING)) == []


def test_set_graph() -> None:
    ev = make()
    ev.set_graph(graph({}))
    ev.update(scenario(T + 5, up=without("relay.rep")))
    assert st(ev, "relay.rep") is waiting


def test_event_text_formats() -> None:
    ev = make()
    events = ev.update(scenario(T + 2.5, up=EVERYTHING))
    lines = texts(events)
    assert "service relay ready: active/running (+2.5 s)" in lines
    assert "listener relay.rep ready: 127.0.0.1:20100/tcp listening (+2.5 s)" in lines
    assert "edge controller -> relay.rep ready: established (+2.5 s)" in lines
    assert "stack ready (+2.5 s)" in lines
    assert "stack post-ready (+2.5 s)" in lines
    events = ev.update(scenario(T + 4, up=without("relay.rep")))
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T + 4 - 5))
    assert texts(events) == [
        "listener relay.rep fault: 20100/tcp not bound (+4.0 s)",
        (
            "FAULT listener_missing relay.rep: 20100/tcp not bound (dropped after"
            " being ready), look at scdemo-relay.service"
            f' (journalctl -u scdemo-relay.service --since "{since}")'
        ),
        "stack fault (+4.0 s)",
    ]
    events = ev.update(scenario(T + 5, up=EVERYTHING))
    assert texts(events) == [
        "listener relay.rep ready: 127.0.0.1:20100/tcp listening (+5.0 s)",
        "CLEARED listener_missing relay.rep (+5.0 s)",
        "stack ready (+5.0 s)",
    ]


# -- fix round 1 regressions -------------------------------------------------


def test_host_server_edge_drop_names_client() -> None:
    ev = running()
    events = ev.update(scenario(T + 5, up=without("relay -> hw.ctrl")))
    finding = ev.view().elements["relay -> hw.ctrl"].finding
    assert finding is not None
    assert finding.code is FindingCode.EDGE_MISSING
    assert finding.units == (UNITS["relay"],)
    assert any(
        t.startswith("FAULT edge_missing relay -> hw.ctrl: ") for t in texts(events)
    )


def test_stop_then_start_has_no_faults() -> None:
    ev = running()
    ev.update(scenario(T + 10, state="stopped"))
    assert ev.view().overall is OverallState.STOPPED
    sinks_up = ("sink@1.udp", "sink@1.stats", "sink@2.udp", "sink@2.stats")
    controller_edges = tuple(e for e in SPEC.edges if e.startswith("controller -> "))
    relay_up = (*sinks_up, *without(*controller_edges))

    def new(sid: str, state: str, since: float) -> UnitSnapshot:
        return snap(sid, state, since=since, run=2)

    sinks = {
        "sink@1": new("sink@1", "active", T + 21),
        "sink@2": new("sink@2", "active", T + 21),
    }
    relay = {**sinks, "relay": new("relay", "active", T + 24)}
    controller = {**relay, "controller": new("controller", "active", T + 26)}
    starting = {
        "sink@1": new("sink@1", "activating", T + 20.5),
        "sink@2": new("sink@2", "activating", T + 20.5),
    }
    steps = [
        # Start jobs queued: every unit still carries its old invocation id.
        scenario(T + 20, state="queued"),
        scenario(T + 20.5, state="queued", units=starting, poll=False),
        scenario(T + 21, state="queued", units=sinks, poll=False),
        scenario(T + 22, state="queued", units=sinks, up=sinks_up),
        scenario(
            T + 23,
            state="queued",
            units={**sinks, "relay": new("relay", "activating", T + 23)},
            up=sinks_up,
            poll=False,
        ),
        scenario(T + 24, state="queued", units=relay, up=sinks_up, poll=False),
        scenario(T + 25, state="queued", units=relay, up=relay_up),
        scenario(T + 26, units=controller, up=relay_up, poll=False),
        scenario(T + 27, units=controller, up=EVERYTHING),
    ]
    events = [e for inputs in steps for e in ev.update(inputs)]
    assert not [e for e in events if e.kind in ("finding", "gate") or e.new == "fault"]
    assert [e.new for e in events if e.kind == "stack" and "post" not in e.text] == [
        "starting",
        "ready",
    ]
    assert ev.view().t0 == T + 20


def test_optional_elements_are_shown_but_not_counted() -> None:
    controller = replace(SPEC.services["controller"], required=False)
    http = replace(SPEC.listeners["relay.http"], required=False)
    edge = replace(SPEC.edges["controller -> relay.http"], required=False)
    spec = replace(
        SPEC,
        services={**SPEC.services, controller.id: controller},
        listeners={**SPEC.listeners, http.id: http},
        edges={**SPEC.edges, edge.id: edge},
    )
    ev = make(spec, after={})
    ev.update(
        scenario(
            T + 1, units={"controller": "activating"}, up=without("relay.http", edge.id)
        )
    )
    view = ev.view()
    assert view.elements["controller"].state is waiting
    assert not view.elements["controller"].required
    assert view.elements["relay.http"].state is waiting
    assert view.elements[edge.id].state is waiting
    assert view.ready
    assert view.overall is OverallState.READY


# -- single-unit restarts ------------------------------------------------------


def restart_steps(
    sid: str, at: float, missing: Sequence[str], files: Mapping[str, bool] | None = None
) -> list[Inputs]:
    """``systemctl restart <sid>`` that completes between two polls (at+0.2 ..
    at+0.6), then a poll before the unit has re-created ``missing``, then a
    poll with everything back."""

    def unit(state: str, run: int, since: float, job: bool = False) -> UnitSnapshot:
        return snap(sid, state, since=since, run=run, job_pending=job)

    new = unit("active", 2, at + 0.6)
    return [
        scenario(at + 0.2, units={sid: unit("active", 1, T, True)}, up=EVERYTHING,
                 poll=False, files=files),
        scenario(at + 0.3, units={sid: unit("deactivating", 1, T, True)},
                 up=EVERYTHING, poll=False, files=files),
        scenario(at + 0.5, units={sid: unit("activating", 2, at + 0.5, True)},
                 up=EVERYTHING, poll=False, files=files),
        scenario(at + 0.6, units={sid: new}, up=EVERYTHING, poll=False, files=files),
        scenario(at + 1.0, units={sid: new}, up=without(*missing),
                 files={k: False for k in files or {}}),
        scenario(at + 2.0, units={sid: new}, up=EVERYTHING, files=files),
    ]  # fmt: skip


CONTROLLER_EDGES = tuple(e for e in EVERYTHING if e.startswith("controller -> "))
RELAY_GONE = tuple(
    x for x in EVERYTHING if x.startswith("relay") or " relay" in x or "-> relay" in x
)


def test_restart_between_polls_is_not_ready_from_the_old_scan() -> None:
    # C-1: the unit-change update after the restart reuses the last scan,
    # taken before the new invocation started: its sockets prove nothing.
    ev = running()
    steps = restart_steps("controller", T + 1, CONTROLLER_EDGES)
    events: list[Event] = []
    for inputs in steps[:4]:
        events += ev.update(inputs)
    assert not ev.view().ready
    assert st(ev, "controller") is ready
    assert st(ev, "controller -> relay.rep") is waiting
    assert "awaiting a scan" in ev.view().elements["controller -> relay.rep"].detail
    events += ev.update(steps[4])
    assert not ev.view().ready
    assert "stack ready" not in " ".join(texts(events))
    events += ev.update(steps[5])
    assert ev.view().ready
    assert no_faults(events)
    assert [e.new for e in events if e.kind == "stack" and "post" not in e.text] == [
        "stopping",
        "starting",
        "ready",
    ]


def test_restart_between_polls_is_not_ready_from_an_old_file_probe() -> None:
    ev = running(with_file())
    files = {"relay:file0": True}
    for inputs in restart_steps("relay", T + 1, RELAY_GONE, files)[:4]:
        ev.update(inputs)
    assert st(ev, "relay:file0") is waiting


def test_single_dependency_restart_has_no_faults() -> None:
    # I-5: systemctl restart scdemo-relay while the controller (Wants+After)
    # stays up. Before the relay binds again, rule 1 must not blame the
    # controller, which predates the relay's new start.
    ev = running()
    events: list[Event] = []
    for inputs in restart_steps("relay", T + 1, RELAY_GONE):
        events += ev.update(inputs)
        assert ev.view().overall is not OverallState.FAULT
    assert no_faults(events)
    assert ev.view().ready


def test_rule1_still_fires_for_a_dependent_started_after_the_owner() -> None:
    # The relay restarts at T+10; the controller is (re)started at T+11, after
    # it, while relay.rep is not there yet: a real gate failure.
    relay = snap("relay", since=T + 10, run=2)
    controller = snap("controller", since=T + 11, run=2)
    ev = running()
    ev.update(
        scenario(
            T + 12,
            up=without("relay.rep", "controller -> relay.rep"),
            units={"relay": relay, "controller": controller},
        )
    )
    assert st(ev, "relay.rep") is fault
    finding = ev.view().elements["relay.rep"].finding
    assert finding is not None
    assert UNITS["controller"] in finding.message


def test_view_not_evaluated_before_first_update() -> None:
    ev = make()
    assert not ev.view().evaluated
    ev.update(scenario(T + 1, up=EVERYTHING))
    assert ev.view().evaluated


def test_overall_stopping_during_stop_jobs() -> None:
    ev = running()
    ev.update(
        scenario(T + 200, units={sid: stopping(sid) for sid in UNITS}, poll=False)
    )
    assert ev.view().overall is OverallState.STOPPING
    ev.update(
        scenario(
            T + 201,
            units={
                "controller": "stopped",
                "relay": stopping("relay"),
                "sink@1": stopping("sink@1"),
                "sink@2": stopping("sink@2"),
            },
            poll=False,
        )
    )
    assert st(ev, "controller") is stopped
    assert ev.view().overall is OverallState.STOPPING


def test_start_job_on_an_active_unit_is_not_stop_evidence() -> None:
    # The stack starts: the controller is already active, but its start job
    # (Type=notify) has not been removed yet, and its edges are not up. That
    # is a start in progress, never ``stopping``.
    ev = make()
    controller = snap("controller", since=T + 0.2, job_pending=True, job_type="start")
    ev.update(
        scenario(
            T + 0.3,
            up=without(*(e for e in EVERYTHING if e.startswith("controller -> "))),
            units={"controller": controller},
        )
    )
    assert ev.view().overall is OverallState.STARTING


def test_stop_job_on_an_active_unit_is_stop_evidence() -> None:
    ev = running()
    ev.update(
        scenario(
            T + 200,
            up=without(*(e for e in EVERYTHING if e.startswith("controller -> "))),
            units={"controller": "stopped", "relay": stopping("relay")},
            poll=False,
        )
    )
    assert ev.view().overall is OverallState.STOPPING
    restart = snap("relay", job_pending=True, job_type="restart")
    ev.update(
        scenario(
            T + 201,
            up=without(*(e for e in EVERYTHING if e.startswith("controller -> "))),
            units={"controller": "stopped", "relay": restart},
            poll=False,
        )
    )
    assert ev.view().overall is OverallState.STOPPING


def test_stop_job_on_an_inactive_unit_is_not_start_evidence() -> None:
    # A stop job still pending on a unit that has already gone inactive (its
    # job not removed yet) is part of the stop, not a start.
    ev = running()
    ev.update(
        scenario(
            T + 200,
            up=without(*(e for e in EVERYTHING if e.startswith("controller -> "))),
            units={
                "controller": snap("controller", "queued", job_type="stop"),
                "relay": stopping("relay"),
            },
            poll=False,
        )
    )
    assert ev.view().overall is OverallState.STOPPING


def test_overall_starting_not_stopping_on_restart_start_phase() -> None:
    ev = running()
    ev.update(scenario(T + 200, state="queued", poll=False))
    assert ev.view().overall is OverallState.STARTING


# -- unit jobs ---------------------------------------------------------------


def test_job_queued_and_done_events() -> None:
    ev = running()
    relay = UNITS["relay"]
    queued = ev.job_queued("relay", relay, "restart", T + 2)
    assert [(e.kind, e.element, e.new) for e in queued] == [("job", "relay", "queued")]
    assert texts(queued) == [f"JOB restart relay {relay}: queued (+2.0 s)"]
    done = ev.job_finished(
        JobOutcome("relay", relay, "restart", "done", "", 1.25), T + 3
    )
    assert texts(done) == [f"JOB restart relay {relay}: done (1.2 s) (+3.0 s)"]
    assert [(e.kind, e.new) for e in done] == [("job", "done")]
    assert ev.view().findings == ()


def test_job_failure_is_finding_until_next_ok_job() -> None:
    ev = running()
    relay = UNITS["relay"]
    failed = JobOutcome(
        "relay", relay, "restart", "error", "not authorised (polkit)", 0.5
    )
    events = ev.job_finished(failed, T + 2)
    (finding,) = ev.view().findings
    assert finding.key == "job_failed:relay"
    assert finding.element == "relay"
    assert finding.units == (relay,)
    assert finding.message == f"restart {relay}: error: not authorised (polkit)"
    assert finding.since == T + 2
    assert texts(events) == [
        f"JOB restart relay {relay}: error: not authorised (polkit) (0.5 s) (+2.0 s)",
        f"FAULT job_failed relay: {finding.look}",
    ]
    assert events[1].kind == "finding"
    assert events[1].new == "job_failed"
    assert events[1].finding == finding

    # Job findings never change states, and survive evaluator updates.
    ev.update(scenario(T + 3, up=EVERYTHING))
    view = ev.view()
    assert view.overall is OverallState.READY
    assert st(ev, "relay") is ready
    assert [f.key for f in view.findings] == ["job_failed:relay"]

    # An ok job on another target leaves it; the next ok one on relay clears it.
    ev.job_finished(JobOutcome("stack", ALL_UNIT, "start", "done", "", 0.1), T + 4)
    assert [f.key for f in ev.view().findings] == ["job_failed:relay"]
    events = ev.job_finished(
        JobOutcome("relay", relay, "restart", "skipped", "", 0.1), T + 5
    )
    assert texts(events)[1:] == ["CLEARED job_failed relay (+5.0 s)"]
    assert events[1].kind == "cleared"
    assert ev.view().findings == ()


def test_job_outcome_ok() -> None:
    def outcome(result: str) -> JobOutcome:
        return JobOutcome("relay", "u", "start", result, "", 0.0)

    assert outcome("done").ok
    assert outcome("skipped").ok
    for result in ("failed", "canceled", "timeout", "error", "not-connected"):
        assert not outcome(result).ok
