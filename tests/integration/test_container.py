"""The core against the real scdemo stack in the Rocky 9 systemd container.

Run with ``QDSC_TARGET=container uv run pytest -m container -v``. The ``container``
fixture (``conftest.py``) gives a stopped stack with no drop-ins and stops the
tool, the stack and the drop-ins again afterwards.
"""

import time
from collections.abc import Callable, Sequence

import pytest

pytestmark = pytest.mark.container

Event = dict[str, object]


def finding(event: Event) -> dict[str, object] | None:
    value = event.get("finding")
    return value if isinstance(value, dict) else None


def code(event: Event) -> str | None:
    f = finding(event)
    return str(f["code"]) if f is not None else None


def units(event: Event) -> list[str]:
    f = finding(event)
    value = f.get("units") if f is not None else None
    return [str(u) for u in value] if isinstance(value, list) else []


def stack_events(events: Sequence[Event], new: str) -> list[Event]:
    return [e for e in events if e.get("kind") == "stack" and e.get("new") == new]


def has_stack(new: str) -> Callable[[list[Event]], bool]:
    return lambda events: bool(stack_events(events, new))


def findings_with(events: Sequence[Event], wanted: str, element: str) -> list[Event]:
    return [
        e
        for e in events
        if e.get("kind") in {"finding", "gate"}
        and code(e) == wanted
        and e.get("element") == element
    ]


def texts(events: Sequence[Event]) -> str:
    return "\n".join(str(e.get("text")) for e in events)


def start_tool_and_wait_stopped(container) -> None:
    container.start_tool()
    container.wait_for(has_stack("stopped"), 30, "initial 'stack stopped' event")


def clean_gating(container) -> None:
    container.gating(sinks="waitport", relay="waitport", controller="notify")


def test_clean_start_ready_no_faults(container) -> None:
    clean_gating(container)
    start_tool_and_wait_stopped(container)
    container.start_stack()

    events = container.wait_for(
        lambda ev: (
            bool(stack_events(ev, "ready")) and bool(stack_events(ev, "post-ready"))
        ),
        60,
        "'stack ready' and 'stack post-ready'",
    )
    assert any(str(e.get("text")).startswith("stack post-ready") for e in events)
    bad = [e for e in events if finding(e) and code(e) != "gate_violation_possible"]
    assert not bad, texts(bad)


def test_listener_missing_names_server(container) -> None:
    container.fault("scdemo-relay.service", "SCDEMO_FAULT_NO_BIND=rep")
    start_tool_and_wait_stopped(container)
    container.start_stack()

    events = container.wait_for(
        lambda ev: bool(findings_with(ev, "listener_missing", "relay.rep")),
        45,
        "listener_missing for relay.rep",
    )
    (event,) = findings_with(events, "listener_missing", "relay.rep")
    assert "scdemo-relay.service" in units(event)
    assert not stack_events(events, "ready"), texts(events)


def test_manual_stop_is_fault(container) -> None:
    clean_gating(container)
    start_tool_and_wait_stopped(container)
    container.start_stack()
    container.wait_for(has_stack("ready"), 60, "'stack ready'")

    container.sh("systemctl stop scdemo-sink@2.service")
    edge = "relay -> sink@2.stats"
    events = container.wait_for(
        lambda ev: (
            bool(stack_events(ev, "fault"))
            and bool(findings_with(ev, "edge_server_missing", edge))
        ),
        10,
        f"'stack fault' and edge_server_missing for {edge}",
    )
    (event,) = findings_with(events, "edge_server_missing", edge)
    assert "scdemo-sink@2.service" in units(event)


def test_gate_violation_detected(container) -> None:
    container.gating(sinks="none", relay="none", controller="none")
    container.fault("scdemo-relay.service", "SCDEMO_DELAY_BIND=3")
    start_tool_and_wait_stopped(container)
    container.start_stack()

    def violation(ev: list[Event]) -> list[Event]:
        found = []
        for e in ev:
            f = finding(e)
            if (
                f is not None
                and f.get("code") == "gate_violation"
                and "scdemo-controller.service" in units(e)
                and "relay.rep" in str(f.get("message"))
            ):
                found.append(e)
        return found

    events = container.wait_for(
        lambda ev: bool(violation(ev)), 45, "gate_violation for the controller"
    )
    assert violation(events)


def test_tool_started_after_stack(container) -> None:
    clean_gating(container)
    container.start_stack(block=True)
    time.sleep(20)
    container.start_tool()

    events = container.wait_for(has_stack("ready"), 10, "'stack ready'")
    # Let the first few polls run, then check nothing was raised.
    time.sleep(3)
    events = container.tool_events()
    bad = [e for e in events if e.get("kind") in {"finding", "gate"}]
    assert not bad, texts(bad)


# -- restarts from ready ---------------------------------------------------------

ALLOWED_FINDINGS = {"gate_violation_possible"}


def ready_stack(container) -> int:
    """Start the stack cleanly with the tool running; return the event count."""
    clean_gating(container)
    start_tool_and_wait_stopped(container)
    container.start_stack()
    container.wait_for(
        lambda ev: (
            bool(stack_events(ev, "ready")) and bool(stack_events(ev, "post-ready"))
        ),
        60,
        "'stack ready' and 'stack post-ready'",
    )
    time.sleep(2)  # let the post-ready polls settle
    return len(container.tool_events())


def restart_and_wait_ready(container, unit: str, timeout: float) -> list[Event]:
    """Restart ``unit``; return the events after it once the stack is ready again."""
    before = ready_stack(container)
    container.sh(f"systemctl restart {unit}", timeout=120)
    container.wait_for(
        lambda ev: bool(stack_events(ev[before:], "ready")),
        timeout,
        f"'stack ready' after restarting {unit}",
    )
    time.sleep(3)  # a few more polls: nothing may be raised late
    return container.tool_events()[before:]


def assert_no_faults(events: Sequence[Event]) -> None:
    bad = [
        e
        for e in events
        if (finding(e) and code(e) not in ALLOWED_FINDINGS) or e.get("new") == "fault"
    ]
    assert not bad, texts(bad)


def overall(events: Sequence[Event]) -> list[str]:
    return [
        str(e.get("new"))
        for e in events
        if e.get("kind") == "stack" and "post" not in str(e.get("new"))
    ]


def test_controller_restart_no_faults_and_no_early_ready(container) -> None:
    events = restart_and_wait_ready(container, "scdemo-controller.service", 30)
    assert_no_faults(events)
    assert overall(events)[0] != "ready", texts(events)  # it left ready first
    assert overall(events)[-1] == "ready", texts(events)
    # The final 'stack ready' only after every controller edge was seen again
    # in a scan of the new invocation (no ready from the old scan).
    last_ready = max(
        i
        for i, e in enumerate(events)
        if e.get("kind") == "stack" and e.get("new") == "ready"
    )
    for name in ("rep", "router", "pub", "http"):
        element = f"controller -> relay.{name}"
        ready_at = [
            i
            for i, e in enumerate(events)
            if e.get("element") == element and e.get("new") == "ready"
        ]
        assert ready_at, f"{element} never ready again:\n{texts(events)}"
        assert ready_at[-1] < last_ready, texts(events)
    # No 'stack ready' while the controller's edges were not ready.
    edges_down = False
    for e in events:
        if str(e.get("element", "")).startswith("controller -> "):
            edges_down = e.get("new") != "ready"
        if e.get("kind") == "stack" and e.get("new") == "ready":
            assert not edges_down, texts(events)


def test_relay_restart_no_faults(container) -> None:
    events = restart_and_wait_ready(container, "scdemo-relay.service", 45)
    assert_no_faults(events)
    assert overall(events)[-1] == "ready", texts(events)


def test_whole_stack_restart_no_faults(container) -> None:
    events = restart_and_wait_ready(container, "scdemo-all.service", 90)
    assert_no_faults(events)
    assert overall(events)[-1] == "ready", texts(events)
