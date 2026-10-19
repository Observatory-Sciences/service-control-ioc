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
    ev = make()
    missing = without("relay.rep", *CONTROLLER_EDGES)
    ev.update(poll(T + 9, up=missing, units=QUEUED))
    events = ev.update(poll(T + 10.3, up=missing, units=STARTED))
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
    events = ev.update(poll(T + 11.3, up=missing, units=STARTED))
    assert gate_texts(events) == []
    assert gates(ev) == [finding]
    # The controller's next invocation clears it (relay.rep is back by then).
    ev.update(poll(T + 19, units=STARTED))
    again = {"controller": snap("controller", since=T + 20)}
    events = ev.update(poll(T + 20.5, units=again))
    assert gates(ev) == []
    cleared = [e for e in events if e.kind == "cleared"]
    assert [(e.element, e.old) for e in cleared] == [("controller", "gate_violation")]
    assert "CLEARED gate_violation controller (+20.5 s)" in [e.text for e in cleared]


def test_possible_gate_violation() -> None:
    ev = make()
    ev.update(poll(T + 9.6, up=without("relay.rep", *CONTROLLER_EDGES), units=QUEUED))
    events = ev.update(poll(T + 10.6, units=STARTED))
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
    # missing (definite): one finding for relay, definite, naming the definite one.
    ev = make()
    ev.update(
        poll(
            T + 9.6,
            up=without("relay.rep", "relay.router", *CONTROLLER_EDGES),
            units=QUEUED,
        )
    )
    ev.update(poll(T + 10.6, up=without("relay.router"), units=STARTED))
    [finding] = gates(ev)
    assert finding.code is FindingCode.GATE_VIOLATION
    assert "(relay.router)" in finding.message
