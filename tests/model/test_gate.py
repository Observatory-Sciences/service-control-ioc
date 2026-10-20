"""Gate violations: a dependent started before a dependency was ready (spec 5.4)."""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from qd_service_control.config.load import load_config
from qd_service_control.model.deps import DependencyGraph
from qd_service_control.model.evaluator import Evaluator, Inputs
from qd_service_control.model.types import (
    ElementState,
    Event,
    Finding,
    FindingCode,
    OverallState,
    UnitSnapshot,
)
from qd_service_control.observe.procfs import (
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
CONTROLLER = UNITS["controller"]
RELAY = UNITS["relay"]
AFTER = {
    CONTROLLER: [RELAY],
    RELAY: [UNITS["sink@1"], UNITS["sink@2"]],
}
T = 1_700_000_000.0
LISTENERS = tuple(lid for lid, ls in SPEC.listeners.items() if not ls.on_host)
EVERYTHING = LISTENERS + tuple(SPEC.edges)
CONTROLLER_EDGES = tuple(e for e in SPEC.edges if e.startswith("controller -> "))
GATES = (FindingCode.GATE_VIOLATION, FindingCode.GATE_VIOLATION_POSSIBLE)


def us(t: float) -> int:
    return round(t * 1_000_000)


def make() -> Evaluator:
    graph = DependencyGraph(AFTER, UNITS.values())
    return Evaluator(SPEC, graph, started_at=T)


def snap(
    sid: str, state: str = "active", *, since: float = T, **changes: object
) -> UnitSnapshot:
    active, sub, job, running = {
        "active": ("active", "running", False, True),
        "queued": ("inactive", "dead", True, False),
        "activating": ("activating", "start", False, True),
    }[state]
    base = UnitSnapshot(
        name=UNITS[sid],
        load_state="loaded",
        active_state=active,
        sub_state=sub,
        result="success",
        job_pending=job,
        n_restarts=0,
        main_pid=1000 if running else 0,
        exec_main_code=0,
        exec_main_status=0,
        exec_main_start_us=us(since),
        active_enter_us=us(since) if state == "active" else 0,
        control_group=f"/system.slice/{UNITS[sid]}",
        invocation_id=f"inv-{sid}-{since}",
        after=(),
    )
    return replace(base, **changes)


def _sockets(eid: str, i: int) -> list[OwnedSock]:
    if eid in SPEC.listeners:
        ls = SPEC.listeners[eid]
        state = TCP_LISTEN if ls.proto == "tcp" else UDP_UNCONN
        sock = Sock(ls.proto, ls.address or "0.0.0.0", ls.port, "0.0.0.0", 0, state, i)
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


def poll(
    now: float,
    *,
    up: Iterable[str] = EVERYTHING,
    units: Mapping[str, UnitSnapshot] | None = None,
) -> Inputs:
    """A fresh socket poll at ``now``; services active since T unless overridden."""
    snaps = {s.name: s for s in (snap(sid) for sid in SPEC.services)}
    for s in (units or {}).values():
        snaps[s.name] = s
    entries = [e for i, eid in enumerate(up) for e in _sockets(eid, i + 1)]
    return Inputs(
        now=now,
        bus_connected=True,
        units=snaps,
        sockets=SocketTable(tuple(entries)),
        poll_time=now,
        cpu={u: None for u in snaps},
        spinning=frozenset(),
        files={},
    )


def without(*ids: str) -> tuple[str, ...]:
    return tuple(x for x in EVERYTHING if x not in ids)


def gates(ev: Evaluator) -> list[Finding]:
    return [f for f in ev.view().findings if f.code in GATES]


def gate_texts(events: Iterable[Event]) -> list[str]:
    return [e.text for e in events if e.kind == "gate"]


def hms(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


QUEUED = {"controller": snap("controller", "queued")}
STARTED = {"controller": snap("controller", since=T + 10)}


def test_definite_gate_violation() -> None:
    # relay.rep is still missing at polls after the controller's start, and
    # binds later: definite, raised when it becomes ready (spec 5.4).
    ev = make()
    missing = without("relay.rep", *CONTROLLER_EDGES)
    ev.update(poll(T + 9, up=missing, units=QUEUED))
    events = ev.update(poll(T + 10.3, up=missing, units=STARTED))
    assert gates(ev) == []  # not ready yet: the check waits for it
    events = ev.update(poll(T + 11.3, up=missing, units=STARTED))
    assert gates(ev) == []
    events = ev.update(poll(T + 12.3, up=without(*CONTROLLER_EDGES), units=STARTED))
    [finding] = gates(ev)
    assert finding.code is FindingCode.GATE_VIOLATION
    assert finding.units == (CONTROLLER, RELAY)
    assert finding.element == "controller"
    assert "relay.rep" in finding.message
    [text] = gate_texts(events)
    assert text.startswith(
        f"GATE gate_violation: {CONTROLLER} started at {hms(T + 10)} before relay"
        f" was ready (relay.rep), look at {CONTROLLER}, {RELAY}"
        f" (journalctl -u {CONTROLLER} --since "
    )
    [event] = [e for e in events if e.kind == "gate"]
    assert event.finding == finding
    assert event.new == "gate_violation"
    # Raised once, held while this invocation lasts.
    events = ev.update(poll(T + 13.3, units=STARTED))
    assert gate_texts(events) == []
    assert gates(ev) == [finding]
    # The controller's next invocation clears it.
    ev.update(poll(T + 19, units=STARTED))
    again = {"controller": snap("controller", since=T + 20)}
    events = ev.update(poll(T + 20.5, units=again))
    assert gates(ev) == []
    cleared = [e for e in events if e.kind == "cleared"]
    assert [(e.element, e.old) for e in cleared] == [("controller", "gate_violation")]
    assert f"CLEARED gate_violation controller after {RELAY} (+20.5 s)" in [
        e.text for e in cleared
    ]


def test_never_ready_listener_raises_no_gate_finding() -> None:
    # relay.rep never binds: that is listener_missing's job, not a gate
    # violation (ruling 14), however long the check waits.
    ev = make()
    missing = without("relay.rep", *CONTROLLER_EDGES)
    ev.update(poll(T + 9, up=missing, units=QUEUED))
    events: list[Event] = []
    for i in range(60):
        events += ev.update(poll(T + 10.3 + i, up=missing, units=STARTED))
    assert gates(ev) == []
    assert gate_texts(events) == []
    # The controller's next invocation drops the wait; relay.rep binding
    # afterwards, before that start, is no violation of either invocation.
    again = {"controller": snap("controller", since=T + 80)}
    ev.update(poll(T + 79.9, up=missing, units=STARTED))
    events = ev.update(poll(T + 80.3, up=missing, units=again))
    events += ev.update(poll(T + 81.3, up=without(*CONTROLLER_EDGES), units=again))
    # relay.rep became ready after the second start: definite for that one.
    [finding] = gates(ev)
    assert finding.code is FindingCode.GATE_VIOLATION
    assert hms(T + 80) in finding.message


def test_check_dropped_when_dependent_stops() -> None:
    # The controller starts too early, then stops while relay.rep is missing.
    # relay.rep binding while the controller is down is no gate violation.
    ev = make()
    missing = without("relay.rep", *CONTROLLER_EDGES)
    ev.update(poll(T + 9, up=missing, units=QUEUED))
    ev.update(poll(T + 10.3, up=missing, units=STARTED))
    stopped = {
        "controller": snap(
            "controller",
            since=T + 10,
            active_state="inactive",
            sub_state="dead",
            main_pid=0,
        )
    }
    ev.update(poll(T + 11.3, up=missing, units=stopped))
    events = ev.update(poll(T + 12.3, up=without(*CONTROLLER_EDGES), units=stopped))
    assert gates(ev) == []
    assert gate_texts(events) == []


def test_fault_element_raises_no_gate_finding() -> None:
    # relay.rep stays missing long enough to be a fault: still no gate finding.
    ev = make()
    missing = without("relay.rep", *CONTROLLER_EDGES)
    ev.update(poll(T + 9, up=missing, units=QUEUED))
    events: list[Event] = []
    for i in range(40):
        events += ev.update(poll(T + 10.3 + i, up=missing, units=STARTED))
    assert ev.view().elements["relay.rep"].state is ElementState.FAULT
    assert gate_texts(events) == []


def test_possible_gate_violation() -> None:
    # The relay unit did not become active before the controller's start
    # (same microsecond), so its exact timestamp vouches for nothing.
    ev = make()
    relay = snap("relay", active_enter_us=us(T + 10))
    ev.update(
        poll(
            T + 9.6,
            up=without("relay.rep", *CONTROLLER_EDGES),
            units={**QUEUED, "relay": snap("relay", "activating", since=T + 9)},
        )
    )
    events = ev.update(poll(T + 10.6, units={**STARTED, "relay": relay}))
    [finding] = gates(ev)
    assert finding.code is FindingCode.GATE_VIOLATION_POSSIBLE
    assert finding.units == (CONTROLLER, RELAY)
    assert "(relay.rep)" in finding.message
    assert [t.split(":")[0] for t in gate_texts(events)] == [
        "GATE gate_violation_possible"
    ]
    # Gate findings change no element state and not the overall state.
    view = ev.view()
    assert {e.state for e in view.elements.values()} == {ElementState.READY}
    assert view.overall is OverallState.READY
    assert view.ready


def test_no_violation_when_ready_before_start() -> None:
    ev = make()
    ev.update(poll(T + 9, up=without(*CONTROLLER_EDGES), units=QUEUED))
    events = ev.update(poll(T + 10.3, units=STARTED))
    assert gates(ev) == []
    assert gate_texts(events) == []


def test_no_check_without_earlier_evidence() -> None:
    ev = make()
    events = ev.update(
        poll(T + 10.3, up=without("relay.rep", *CONTROLLER_EDGES), units=STARTED)
    )
    assert gates(ev) == []
    assert gate_texts(events) == []
    events = ev.update(poll(T + 11.3, units=STARTED))
    assert gates(ev) == []


def test_post_elements_not_checked() -> None:
    gone = ("relay.pull", "sink@1 -> relay.pull", "sink@2 -> relay.pull")
    ev = make()
    ev.update(poll(T + 9, up=without(*gone, *CONTROLLER_EDGES), units=QUEUED))
    events = ev.update(poll(T + 10.3, up=without(*gone), units=STARTED))
    assert gates(ev) == []
    assert gate_texts(events) == []


def test_unit_level_violation() -> None:
    ev = make()
    relay_starting = snap("relay", "activating", since=T + 8)
    ev.update(
        poll(
            T + 9,
            up=without(*CONTROLLER_EDGES),
            units={**QUEUED, "relay": relay_starting},
        )
    )
    relay_late = snap("relay", since=T + 8, active_enter_us=us(T + 10.2))
    events = ev.update(poll(T + 10.3, units={**STARTED, "relay": relay_late}))
    [finding] = gates(ev)
    assert finding.code is FindingCode.GATE_VIOLATION
    assert finding.units == (CONTROLLER, RELAY)
    assert "before relay was ready (relay)" in finding.message
    assert len(gate_texts(events)) == 1
    assert ev.view().elements["relay"].state is ElementState.READY


def test_definite_wins_one_finding_per_dependency() -> None:
    # relay.rep appears at the check poll (possible), relay.router is still
    # missing and binds later (definite): the possible finding is raised
    # first, then replaced by one definite finding naming relay.router. The
    # relay unit became active at the controller's start, not before it.
    ev = make()
    relay = {"relay": snap("relay", active_enter_us=us(T + 10))}
    ev.update(
        poll(
            T + 9.6,
            up=without("relay.rep", "relay.router", *CONTROLLER_EDGES),
            units={**QUEUED, "relay": snap("relay", "activating", since=T + 9)},
        )
    )
    ev.update(poll(T + 10.6, up=without("relay.router"), units={**STARTED, **relay}))
    [finding] = gates(ev)
    assert finding.code is FindingCode.GATE_VIOLATION_POSSIBLE
    events = ev.update(poll(T + 11.6, units={**STARTED, **relay}))
    [finding] = gates(ev)
    assert finding.code is FindingCode.GATE_VIOLATION
    assert "(relay.router)" in finding.message
    assert [e.old for e in events if e.kind == "cleared" and e.old in GATES] == [
        "gate_violation_possible"
    ]
    assert [t.split(":")[0] for t in gate_texts(events)] == ["GATE gate_violation"]


def test_gate_cleared_text_names_dependency() -> None:
    ev = make()
    missing = without("relay.rep", *CONTROLLER_EDGES)
    ev.update(poll(T + 9, up=missing, units=QUEUED))
    ev.update(poll(T + 10.3, up=missing, units=STARTED))
    ev.update(poll(T + 19, units=STARTED))
    assert [f.code for f in gates(ev)] == [FindingCode.GATE_VIOLATION]
    again = {"controller": snap("controller", since=T + 20)}
    events = ev.update(poll(T + 20.5, units=again))
    assert [e.text for e in events if e.kind == "cleared"] == [
        f"CLEARED gate_violation controller after {RELAY} (+20.5 s)"
    ]


# -- fast polls while units start (ruling 13) ---------------------------------


def chain(
    sink_ready: float,
    relay_start: float,
    up_until: Iterable[tuple[float, bool]],
    sink_active: float = T,
) -> list[Finding]:
    """Polls every 100 ms; sink@1.udp/stats appear at ``sink_ready``.

    ``up_until`` overrides: (poll time, sink listeners up) pairs;
    ``sink_active`` is sink@1's exact ActiveEnterTimestamp.
    """
    ev = make()
    sinks = ("sink@1.udp", "sink@1.stats")
    relay_side = tuple(
        x
        for x in EVERYTHING
        if x.startswith(("relay", "controller")) and x not in sinks
    )
    sink = snap("sink@1", active_enter_us=us(sink_active))
    queued = {
        "sink@1": sink,
        "relay": snap("relay", "queued"),
        "controller": snap("controller", "queued"),
    }
    started = {
        "sink@1": sink,
        "relay": snap("relay", since=relay_start),
        "controller": snap("controller", "queued"),
    }
    overrides = dict(up_until)
    for i in range(20):
        t = T + i / 10
        sinks_up = overrides.get(round(t - T, 1), t >= sink_ready)
        up = [x for x in EVERYTHING if x not in relay_side]
        if not sinks_up:
            up = [x for x in up if x not in sinks]
        ev.update(poll(t, up=up, units=started if t >= relay_start else queued))
    return gates(ev)


def test_chain_seen_ready_one_fast_poll_before_start_is_clean() -> None:
    # Sinks seen ready at the T+0.2 poll, the relay starts at T+0.3.
    assert chain(T + 0.2, T + 0.3, ()) == []


def test_start_inside_one_fast_interval_is_possible() -> None:
    # Not ready at T+0.2, ready at T+0.3; the relay started at T+0.25, and
    # sink@1 had not become active before it (no evidence from systemd).
    [finding] = chain(T + 0.3, T + 0.25, (), sink_active=T + 0.25)
    assert finding.code is FindingCode.GATE_VIOLATION_POSSIBLE
    assert finding.units == (RELAY, UNITS["sink@1"])


def test_dependency_active_before_start_vouches_for_its_elements() -> None:
    # Like G1 (notify): sink@1 binds, sends READY=1 (active at T+0.24) and
    # systemd starts the relay at T+0.25, inside one fast interval. The exact
    # ActiveEnterTimestamp precedes the start, so no possible finding.
    assert chain(T + 0.3, T + 0.25, (), sink_active=T + 0.24) == []


def test_dependency_active_before_start_still_definite_when_late() -> None:
    # Like G4: sink@1 active at T (no gate), its listeners still missing at
    # polls after the relay's start: definite despite the timestamp.
    [finding] = chain(T + 0.5, T + 0.25, (), sink_active=T)
    assert finding.code is FindingCode.GATE_VIOLATION


def test_listener_missing_after_start_is_definite() -> None:
    # The relay started at T+0.25; the T+0.3 and T+0.4 polls still see the
    # sink's listeners missing, and they appear at T+0.5.
    [finding] = chain(T + 0.5, T + 0.25, ())
    assert finding.code is FindingCode.GATE_VIOLATION
    assert finding.units == (RELAY, UNITS["sink@1"])
