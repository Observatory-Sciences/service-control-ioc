"""Pure record-value mapping: no softioc involved."""

import time
from dataclasses import replace

from fakes import scdemo_spec
from qd_service_control.ioc.mapping import (
    ELEMENT_STATES,
    OVERALL_STATES,
    describe,
    element_values,
    fit,
    group_values,
    latest_finding,
    service_values,
    since_text,
    stack_values,
    text_waveform,
)
from qd_service_control.model.deps import DependencyGraph
from qd_service_control.model.evaluator import Evaluator
from qd_service_control.model.types import (
    ElementState,
    ElementView,
    Finding,
    FindingCode,
    OverallState,
    StackView,
    UnitSnapshot,
)

SPEC = scdemo_spec()
T = 1_700_000_000.0


def snapshot(unit: str, **changes: object) -> UnitSnapshot:
    base = UnitSnapshot(
        name=unit,
        load_state="loaded",
        active_state="active",
        sub_state="running",
        result="success",
        job_pending=False,
        n_restarts=0,
        main_pid=1,
        exec_main_code=0,
        exec_main_status=0,
        exec_main_start_us=0,
        active_enter_us=0,
        control_group="",
        invocation_id="",
        after=(),
    )
    return replace(base, **changes)


def finding(
    element: str = "relay",
    unit: str = "scdemo-relay.service",
    since: float = T,
    code: FindingCode = FindingCode.UNIT_FAILED,
) -> Finding:
    return Finding(code, element, (unit,), "relay failed", since)


def element(eid: str, state: ElementState, found: Finding | None = None) -> ElementView:
    return ElementView(eid, "service", state, T, "detail", "pre", True, found)


def view(**changes: object) -> StackView:
    base = StackView(
        overall=OverallState.READY,
        ready=True,
        post_ready=True,
        since=T,
        t0=T,
        elements={},
        findings=(),
        units={},
        cpu={},
        evaluated=True,
    )
    return replace(base, **changes)


def test_fit_cuts_on_char_boundary() -> None:
    assert fit("aé", 2) == "a"
    assert fit("aé", 3) == "aé"
    assert fit("abc", 10) == "abc"


def test_text_waveform_newest_last_and_bounded() -> None:
    lines = [f"{i:03d}" + "x" * 297 for i in range(30)]
    data = text_waveform(lines, 5120)
    assert len(data) == 5120
    assert data[5119] == 0
    text = data.rstrip(b"\0").decode()
    assert text.endswith(lines[-1] + "\n")
    assert lines[0] not in text
    assert len(text.encode()) <= 5119


def test_text_waveform_single_long_line_keeps_tail() -> None:
    data = text_waveform(["a" * 100 + "TAIL"], 20)
    assert len(data) == 20
    assert data[19] == 0
    assert data.rstrip(b"\0") == b"a" * 14 + b"TAIL\n"


def test_text_waveform_utf8_safe() -> None:
    data = text_waveform(["é" * 50], 20)
    text = data.rstrip(b"\0").decode("utf-8")  # strict: no split character
    assert text == "é" * 9 + "\n"
    many = text_waveform(["é" * 7] * 10, 40)
    many.rstrip(b"\0").decode("utf-8")


def test_text_waveform_empty() -> None:
    assert text_waveform([], 8) == bytes(8)


def test_stack_values_initial_view_invalid() -> None:
    ev = Evaluator(SPEC, DependencyGraph({}, []), started_at=T)
    values = stack_values(ev.view())
    assert values.severity == 3
    assert values.ready is False
    assert values.post_ready is False
    assert values.fault_msg == ""


def test_stack_values_evaluated() -> None:
    f = finding()
    values = stack_values(view(overall=OverallState.FAULT, ready=False, findings=(f,)))
    assert values.state == OVERALL_STATES.index((OverallState.FAULT, 2))
    assert values.severity == 2
    assert values.faults == 1
    assert values.fault_msg == f.look
    ok = stack_values(view())
    assert (ok.state, ok.severity, ok.ready, ok.post_ready) == (3, 0, True, True)


def test_service_values_fault_and_exit() -> None:
    unit = SPEC.services["relay"].unit
    f = finding()
    snap = snapshot(
        unit,
        active_state="activating",
        sub_state="auto-restart",
        result="exit-code",
        n_restarts=4,
        exec_main_code=1,
        exec_main_status=3,
    )
    v = view(
        elements={"relay": element("relay", ElementState.FAULT, f)},
        units={unit: snap},
        cpu={unit: 12.5},
    )
    values = service_values(SPEC, v, "relay")
    assert values.state == ELEMENT_STATES.index((ElementState.FAULT, 2))
    assert values.severity == 2
    assert values.unit == unit
    assert values.active == "activating/auto-restart"
    assert values.ready is False
    assert values.nrestarts == 4
    assert values.exit == "exited with status 3 (result exit-code)"
    assert values.cpu == 12.5
    assert values.finding == f.look


def test_service_values_unit_never_exited_and_unit_finding() -> None:
    unit = SPEC.services["relay"].unit
    other = finding(element="relay.pull", unit=unit, since=T)
    v = view(
        elements={"relay": element("relay", ElementState.READY)},
        units={unit: snapshot(unit)},
        findings=(finding(unit="scdemo-other.service"), other),
    )
    values = service_values(SPEC, v, "relay")
    assert values.exit == ""
    assert values.ready is True
    assert values.cpu is None
    assert values.finding == other.look
    none = service_values(SPEC, view(), "controller")
    assert none.state == 0
    assert none.severity == 3
    assert none.active == "unknown"
    assert none.finding == ""
    assert none.nrestarts == 0


def test_element_values_unknown_when_missing() -> None:
    values = element_values(view(), "relay.pull")
    assert values.state == ELEMENT_STATES.index((ElementState.UNKNOWN, 3))
    assert values.severity == 3
    assert values.detail == ""
    assert values.since == ""


def test_element_values_present() -> None:
    v = view(elements={"relay": element("relay", ElementState.WAITING)})
    values = element_values(v, "relay")
    assert (values.state, values.severity) == (2, 1)
    assert values.detail == "detail"
    assert values.since == since_text(T)
    assert values.since == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T))


def test_group_values_from_snapshot() -> None:
    unit = SPEC.groups["all"].unit
    expected = {
        "active": (ElementState.READY, 0),
        "reloading": (ElementState.READY, 0),
        "inactive": (ElementState.STOPPED, 0),
        "activating": (ElementState.WAITING, 1),
        "deactivating": (ElementState.WAITING, 1),
        "failed": (ElementState.FAULT, 2),
    }
    for active, pair in expected.items():
        v = view(units={unit: snapshot(unit, active_state=active)})
        values = group_values(SPEC, v, "all")
        assert (values.state, values.severity) == (ELEMENT_STATES.index(pair), pair[1])
    missing = group_values(SPEC, view(), "all")
    assert (missing.state, missing.severity) == (0, 3)


def test_describe_scdemo() -> None:
    assert describe(SPEC, "relay.pull") == "relay :20103/tcp (127.0.0.1)"
    assert describe(SPEC, "relay -> sink@1.stats") == "relay -> sink@1:20021/tcp"
    assert describe(SPEC, "relay -> hw.ctrl") == "relay -> hw 127.0.0.2:20200/tcp"


def test_describe_file_and_no_address() -> None:
    from qd_service_control.config.spec import FileSpec

    spec = replace(
        SPEC,
        files={
            "relay.f": FileSpec("relay.f", "relay", "/x/y", True, "pre", True, None)
        },
        listeners={
            **SPEC.listeners,
            "relay.pull": replace(SPEC.listeners["relay.pull"], address=None),
        },
    )
    assert describe(spec, "relay.f") == "relay file /x/y"
    assert describe(spec, "relay.pull") == "relay :20103/tcp"


def test_latest_finding_tie_breaks_on_key() -> None:
    a = finding(element="a", since=T)
    b = finding(element="b", since=T)
    old = finding(element="z", since=T - 10)
    assert latest_finding([a, old, b]) == b
    assert latest_finding([b, a]) == b
    assert latest_finding([]) is None
