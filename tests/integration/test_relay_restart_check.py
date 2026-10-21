"""The ruling 18 check of ``test_relay_restart_no_faults``, on synthetic events.

Runs on the Mac (no target): the container test only feeds it real events.
"""

import pytest

from integration.test_container import assert_relay_restart_clean

Event = dict[str, object]
EDGE = "controller -> relay.rep"


def finding(code: str, element: str) -> dict[str, object]:
    return {"code": code, "element": element, "units": ["u.service"], "message": "m"}


def ev(kind: str, element: str, new: str | None, code: str | None = None) -> Event:
    return {
        "kind": kind,
        "element": element,
        "old": None,
        "new": new,
        "text": f"{kind} {element} {new}",
        "finding": finding(code, element) if code else None,
    }


def transient_run() -> list[Event]:
    """A relay restart in which the controller's REQ edge drops for ~0.1 s."""
    return [
        ev("stack", "stack", "stopping"),
        ev("service", "relay", "waiting"),
        ev("stack", "stack", "starting"),
        # The element event, with or without the finding attached.
        ev("edge", EDGE, "fault"),
        ev("edge", EDGE, "fault", "edge_missing"),
        ev("finding", EDGE, "edge_missing", "edge_missing"),
        ev("stack", "stack", "fault"),
        ev("cleared", EDGE, None, "edge_missing"),
        ev("edge", EDGE, "ready"),
        ev("stack", "stack", "starting"),
        ev("service", "relay", "ready"),
        ev("stack", "stack", "ready"),
    ]


def test_cleared_transient_and_stack_fault_pass() -> None:
    assert_relay_restart_clean(transient_run())


def test_clean_restart_passes() -> None:
    events = [e for e in transient_run() if EDGE not in str(e["element"])]
    assert_relay_restart_clean([e for e in events if e.get("new") != "fault"])


@pytest.mark.parametrize(
    "extra",
    [
        ev("service", "sink@1", "fault", "unit_failed"),
        ev("finding", "sink@1", "unit_failed", "unit_failed"),
        ev("edge", "relay -> sink@1.stats", "fault", "edge_missing"),
        ev("finding", "relay -> sink@1.stats", "edge_missing", "edge_missing"),
        ev("finding", EDGE, "edge_server_missing", "edge_server_missing"),
    ],
    ids=[
        "sink-fault",
        "sink-finding",
        "other-edge",
        "other-edge-finding",
        "other-code",
    ],
)
def test_any_other_fault_fails(extra: Event) -> None:
    events = transient_run()
    events.insert(4, extra)
    with pytest.raises(AssertionError):
        assert_relay_restart_clean(events)


def test_uncleared_transient_fails() -> None:
    events = [e for e in transient_run() if e["kind"] != "cleared"]
    with pytest.raises(AssertionError, match="never cleared"):
        assert_relay_restart_clean(events)


def test_must_end_ready() -> None:
    events = [*transient_run(), ev("stack", "stack", "starting")]
    with pytest.raises(AssertionError):
        assert_relay_restart_clean(events)
