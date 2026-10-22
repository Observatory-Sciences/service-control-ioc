import time

from service_control_ioc.model.types import Finding, FindingCode, OverallState


def test_journal_ref_uses_local_time_minus_five_seconds() -> None:
    since = time.mktime((2026, 10, 19, 10, 41, 7, 0, 0, -1))
    finding = Finding(
        FindingCode.UNIT_FAILED,
        "relay",
        ("scdemo-relay.service",),
        "failed",
        since,
    )
    assert finding.journal_ref == (
        'journalctl -u scdemo-relay.service --since "2026-10-19 10:41:02"'
    )


def test_finding_code_values() -> None:
    assert FindingCode.GATE_VIOLATION_POSSIBLE == "gate_violation_possible"


def test_finding_key_plain() -> None:
    finding = Finding(
        FindingCode.UNIT_FAILED, "relay", ("scdemo-relay.service",), "failed", 0.0
    )
    assert finding.key == "unit_failed:relay"


def test_finding_key_gate_includes_dependency() -> None:
    against_relay = Finding(
        FindingCode.GATE_VIOLATION,
        "controller",
        ("scdemo-controller.service", "scdemo-relay.service"),
        "started before relay was ready",
        0.0,
    )
    against_sink = Finding(
        FindingCode.GATE_VIOLATION_POSSIBLE,
        "controller",
        ("scdemo-controller.service", "scdemo-sink@1.service"),
        "started before sink@1 was ready",
        0.0,
    )
    assert against_relay.element == against_sink.element
    assert against_relay.key == "gate_violation:controller<scdemo-relay.service"
    assert against_sink.key == (
        "gate_violation_possible:controller<scdemo-sink@1.service"
    )
    assert against_relay.key != against_sink.key


def test_finding_look_text() -> None:
    since = time.mktime((2026, 10, 19, 10, 41, 7, 0, 0, -1))
    finding = Finding(
        FindingCode.UNIT_FAILED, "relay", ("scdemo-relay.service",), "failed", since
    )
    assert finding.look == (
        f"failed, look at scdemo-relay.service ({finding.journal_ref})"
    )


def test_overall_state_has_stopping() -> None:
    assert OverallState.STOPPING == "stopping"
    assert [s.value for s in OverallState] == [
        "stopped",
        "starting",
        "stopping",
        "ready",
        "fault",
    ]
