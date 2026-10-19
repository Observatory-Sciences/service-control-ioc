import time

from qd_service_control.model.types import Finding, FindingCode


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
