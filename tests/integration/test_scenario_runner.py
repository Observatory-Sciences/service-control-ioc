"""The scenario runner against a fake ``Host`` (no target needed).

The fake scripts what the target would say: the ``ca_client.py`` replies, the
tool's JSON events (with target-clock ``ts``), the unit states and the clock.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from integration.scenarios import (
    Expect,
    Put,
    Scenario,
    ScenarioIsolationError,
    Sh,
    Wait,
    run_scenario,
    scenario_session,
)
from integration.summarise import render, render_gate

P = "SCDEMO:SC"
T0 = 1000.0  # target clock at the last action

Event = dict[str, object]


def finding(
    code: str,
    element: str,
    units: Sequence[str] = ("scdemo-x.service",),
    message: str | None = None,
) -> dict[str, object]:
    return {
        "code": code,
        "element": element,
        "units": list(units),
        "message": message or f"{code} on {element}",
        "since": T0,
        "journal": "journalctl -u x",
    }


def stack(ts: float, new: str, old: str | None = None) -> Event:
    return {
        "ts": ts,
        "kind": "stack",
        "element": "stack",
        "old": old,
        "new": new,
        "text": f"stack {new}",
        "finding": None,
    }


def fault(
    ts: float,
    code: str,
    element: str,
    units: Sequence[str] = ("u",),
    message: str | None = None,
) -> Event:
    kind = "gate" if code.startswith("gate_violation") else "finding"
    word = "GATE" if kind == "gate" else "FAULT"
    f = finding(code, element, units, message)
    return {
        "ts": ts,
        "kind": kind,
        "element": element,
        "old": None,
        "new": code,
        "text": f"{word} {code} {element}: {f['message']}",
        "finding": f,
    }


def cleared(ts: float, code: str, element: str, units: Sequence[str] = ("u",)) -> Event:
    return {
        "ts": ts,
        "kind": "cleared",
        "element": element,
        "old": code,
        "new": None,
        "text": f"CLEARED {code} {element}",
        "finding": finding(code, element, units),
    }


class FakeHost:
    """A scripted target. ``events`` are what the tool logs; ``pvs`` the final
    PV values; ``wait_ok``/``hold_ok`` how ``ca_client.py`` waits end."""

    def __init__(
        self,
        events: Sequence[Event] = (),
        pvs: Mapping[str, str] | None = None,
        *,
        wait_ok: Mapping[str, bool] | None = None,
        hold_ok: bool | Sequence[bool] = True,
        active_after_clear: Sequence[str] = (),
        failing_sh: str | None = None,
    ) -> None:
        self.events = list(events)
        self.pvs = dict(pvs or {f"{P}:STATE": "ready", f"{P}:READY": "READY"})
        self.wait_ok = dict(wait_ok or {})
        self.holds = [hold_ok] if isinstance(hold_ok, bool) else list(hold_ok)
        self.active_after_clear = list(active_after_clear)
        self.failing_sh = failing_sh
        self.invocation: str | None = None
        self.calls: list[str] = []
        self.clock = T0

    class _Target:
        name = "fake"

    @property
    def target(self) -> "FakeHost._Target":
        return FakeHost._Target()

    def clear(self) -> None:
        self.calls.append("clear")

    def sh(self, cmd: str, timeout: float = 120) -> str:
        self.calls.append(f"sh {cmd}")
        if self.failing_sh is not None and cmd == self.failing_sh:
            raise AssertionError(f"{cmd} exited 1")
        return ""

    def gating(self, sinks: str, relay: str, controller: str) -> None:
        self.calls.append(f"gating {sinks} {relay} {controller}")

    def fault(self, unit: str, *settings: str) -> None:
        self.calls.append(f"fault {unit} {' '.join(settings)}")

    def start_tool(self) -> None:
        self.calls.append("start_tool")
        self.invocation = "inv1"

    def stop_tool(self) -> None:
        self.calls.append("stop_tool")

    def tool_events(self, invocation: str | None = None) -> list[Event]:
        return list(self.events)

    def unit_states(self, units: Sequence[str]) -> dict[str, str]:
        active = set(self.active_after_clear)
        return {u: "active" if u in active else "inactive" for u in units}

    def arch(self) -> str:
        return "aarch64"

    def now(self) -> float:
        return self.clock

    def ca(self, *args: str, stdin_cmd: str | None = None, check: bool = True) -> Any:
        self.calls.append("ca " + " ".join(args))
        cmd = args[0]
        if cmd == "put":
            return {"pv": args[1], "ok": True, "error": None, "t": self.clock}
        if cmd == "putwait":
            pv, value = args[3], args[4]
            ok = self.wait_ok.get(pv, True)
            return {
                "pv": pv,
                "ok": ok,
                "value": value if ok else None,
                "t": self.clock,
                "put_ok": True,
                "error": None,
            }
        if cmd == "wait":
            pv, value = args[1], args[2]
            # The tool always comes up stopped (runner step 4).
            ok = value == "stopped" or self.wait_ok.get(pv, True)
            return {"pv": pv, "ok": ok, "value": value if ok else "other"}
        if cmd == "hold":
            self.clock += float(args[2]) + 60  # time passes; retries run out
            ok = self.holds.pop(0) if len(self.holds) > 1 else self.holds[0]
            broken = None if ok else {"pv": f"{P}:STATE", "value": "starting"}
            return {"ok": ok, "broken": broken}
        if cmd == "get":
            names = [a for a in args[1:] if not a.startswith("--")]
            return {pv: self.pvs.get(pv) for pv in names}
        raise AssertionError(f"unexpected ca call {args}")


READY_RUN: list[Event] = [stack(T0 + 1, "starting", "stopped"), stack(T0 + 5, "ready")]


def ready_scenario(**expect: Any) -> Scenario:
    return Scenario("T1", "test", expect=Expect(state="ready", ready=1, **expect))


def test_runner_passes_matching_run(tmp_path: Path) -> None:
    host = FakeHost(READY_RUN)
    result = run_scenario(host, ready_scenario(), tmp_path)
    assert result.passed, result.failures
    assert result.time_to_final_s == pytest.approx(5.0)
    assert result.evidence == tmp_path / "fake" / "T1.json"
    assert host.calls[0] == "clear"
    assert host.calls[-1] == "clear"
    assert "gating waitport waitport notify" in host.calls
    assert f"ca put {P}:START 1 --timeout 10" in host.calls


def test_runner_fails_on_unexpected_finding(tmp_path: Path) -> None:
    events = [*READY_RUN, fault(T0 + 2, "listener_missing", "relay.http")]
    result = run_scenario(FakeHost(events), ready_scenario(), tmp_path)
    assert not result.passed
    assert any("listener_missing:relay.http" in f for f in result.failures)


def test_runner_fails_on_timeout_even_if_state_matches(tmp_path: Path) -> None:
    # The wait timed out, yet the PV snapshot afterwards reads ready.
    host = FakeHost(READY_RUN, wait_ok={f"{P}:STATE": False})
    result = run_scenario(host, ready_scenario(), tmp_path)
    assert not result.passed
    assert any("STATE" in f and "within" in f for f in result.failures)

    # The wait matched, but the tool reached ready later than `within`.
    late = [stack(T0 + 1, "starting"), stack(T0 + 130, "ready")]
    result = run_scenario(FakeHost(late), ready_scenario(within=120), tmp_path)
    assert not result.passed
    assert any("130.0 s" in f and "within 120" in f for f in result.failures)


def test_runner_fails_if_final_state_too_early(tmp_path: Path) -> None:
    result = run_scenario(FakeHost(READY_RUN), ready_scenario(min_time=10), tmp_path)
    assert not result.passed
    assert any("min_time 10" in f for f in result.failures)


def test_runner_fails_if_state_does_not_hold(tmp_path: Path) -> None:
    host = FakeHost(READY_RUN, hold_ok=False)
    result = run_scenario(host, ready_scenario(hold=7), tmp_path)
    assert not result.passed
    assert any("did not hold for 7" in f for f in result.failures)
    assert any(c.startswith("ca hold --for 7") for c in host.calls)


def test_transient_final_state_is_waited_for_again(tmp_path: Path) -> None:
    # STATE reads fault, then a unit restarts (starting) before it settles.
    events = [
        stack(T0 + 6, "fault"),
        stack(T0 + 11, "starting"),
        stack(T0 + 40, "fault"),
    ]
    pvs = {f"{P}:STATE": "fault", f"{P}:READY": "NOT READY"}
    # Only a scenario that opts in (F5, the restart loop) may break a hold.
    sc = Scenario(
        "T9", "retry", expect=Expect("fault", 0, within=150, holds_may_break=True)
    )
    host = FakeHost(events, pvs, hold_ok=[False, True])
    result = run_scenario(host, sc, tmp_path)
    assert result.passed, result.failures
    assert result.time_to_final_s == pytest.approx(40.0)
    assert sum(c.startswith(f"ca wait {P}:STATE fault") for c in host.calls) == 2
    data = json.loads(result.evidence.read_text())
    assert data["broken_holds"] == [
        {"ok": False, "broken": {"pv": f"{P}:STATE", "value": "starting"}}
    ]


def test_broken_hold_fails_by_default(tmp_path: Path) -> None:
    # Reached ready, broke (fault) during the hold, reached ready again and held.
    events = [
        stack(T0 + 2, "ready"),
        stack(T0 + 4, "fault"),
        stack(T0 + 9, "ready"),
    ]
    host = FakeHost(events, hold_ok=[False, True])
    result = run_scenario(host, ready_scenario(), tmp_path)
    assert not result.passed
    broke = (
        "the final state 'ready' was reached but did not hold:"
        " SCDEMO:SC:STATE read 'starting' (opt in with holds_may_break)"
    )
    assert result.failures == (broke,)
    # The retry still ran: the final state was waited for twice.
    assert sum(c.startswith(f"ca wait {P}:STATE ready") for c in host.calls) == 2


def test_state_seen_needs_no_hold(tmp_path: Path) -> None:
    # The unit retries: STATE passes through fault, then reads starting again.
    events = [
        stack(T0 + 1, "starting"),
        fault(T0 + 31, "unit_start_timeout", "relay"),
        stack(T0 + 31, "fault"),
        stack(T0 + 36, "starting"),
    ]
    pvs = {f"{P}:STATE": "starting", f"{P}:READY": "NOT READY"}
    expect = Expect(
        "fault",
        0,
        state_seen=True,
        raised=("unit_start_timeout:relay|unit_failed:relay",),
        allowed=("*",),
    )
    host = FakeHost(events, pvs)
    result = run_scenario(host, Scenario("T10", "seen", expect=expect), tmp_path)
    assert result.passed, result.failures
    assert result.time_to_final_s == pytest.approx(31.0)
    assert not any(
        c.startswith(("ca hold", "ca wait SCDEMO:SC:READY")) for c in host.calls
    )

    # Never seen (the run gives up at once: within 0).
    never = [stack(T0 + 1, "starting")]
    seen_never = Expect("fault", 0, state_seen=True, allowed=("*",), within=0)
    result = run_scenario(
        FakeHost(never, pvs), Scenario("T11", "never", expect=seen_never), tmp_path
    )
    assert not result.passed
    assert any("never read 'fault'" in f for f in result.failures)

    # Seen, but a required finding is missing.
    missing = Expect(
        "fault", 0, state_seen=True, raised=("unit_failed:relay",), within=0
    )
    result = run_scenario(
        FakeHost(events, pvs), Scenario("T12", "missing", expect=missing), tmp_path
    )
    assert not result.passed
    assert any("unit_failed:relay was not raised" in f for f in result.failures)


def test_alternatives_and_patterns(tmp_path: Path) -> None:
    events = [
        stack(T0 + 1, "starting"),
        fault(T0 + 30, "unit_failed", "relay"),
        fault(T0 + 31, "edge_missing", "controller -> relay.http"),
        fault(
            T0 + 2,
            "gate_violation",
            "relay",
            ("scdemo-relay.service", "scdemo-sink@1.service"),
        ),
        stack(T0 + 30, "fault"),
    ]
    pvs = {f"{P}:STATE": "fault", f"{P}:READY": "NOT READY"}
    expect = Expect(
        state="fault",
        ready=0,
        raised=(
            "unit_start_timeout:relay|unit_failed:relay",
            "gate_violation:relay<scdemo-sink@1.service",
        ),
        allowed=("*controller*",),
    )
    sc = Scenario("T2", "alt", expect=expect)
    result = run_scenario(FakeHost(events, pvs), sc, tmp_path)
    assert result.passed, result.failures

    # Neither alternative raised: a failure naming the expectation.
    missing = [e for e in events if "unit_failed" not in str(e["text"])]
    result = run_scenario(FakeHost(missing, pvs), sc, tmp_path)
    assert not result.passed
    assert any(
        "unit_start_timeout:relay|unit_failed:relay" in f for f in result.failures
    )

    # The pattern does not cover a finding outside it.
    extra = [*events, fault(T0 + 32, "cpu_spin", "sink@2")]
    result = run_scenario(FakeHost(extra, pvs), sc, tmp_path)
    assert not result.passed
    assert any("cpu_spin:sink@2" in f for f in result.failures)


def test_cleared_and_extra_pvs(tmp_path: Path) -> None:
    events = [
        stack(T0 - 20, "ready"),
        fault(T0 - 10, "unit_stopped", "sink@1"),
        stack(T0 - 10, "fault"),
        stack(T0 + 4, "ready"),
    ]
    pvs = {f"{P}:STATE": "ready", f"{P}:READY": "READY", f"{P}:SINK1:STATE": "ready"}
    expect = Expect(
        state="ready",
        ready=1,
        pvs={"SINK1:STATE": "ready"},
        raised=("unit_stopped:sink@1",),
        cleared=True,
    )
    sc = Scenario("T3", "cleared", expect=expect)
    result = run_scenario(FakeHost(events, pvs), sc, tmp_path)
    assert not result.passed  # raised but never cleared
    assert any("never cleared" in f for f in result.failures)

    events.append(cleared(T0 + 3, "unit_stopped", "sink@1"))
    # A GATE finding is an event, not a state: `cleared` does not ask for it.
    gate = fault(
        T0 - 30, "gate_violation_possible", "relay", ("a.service", "b.service")
    )
    sc = Scenario(
        "T3",
        "cleared",
        expect=Expect(
            state="ready",
            ready=1,
            pvs={"SINK1:STATE": "ready"},
            raised=("unit_stopped:sink@1",),
            allowed=("gate_violation_possible:*",),
            cleared=True,
        ),
    )
    result = run_scenario(FakeHost([gate, *events], pvs), sc, tmp_path)
    assert result.passed, result.failures
    assert result.time_to_final_s == pytest.approx(4.0)

    pvs[f"{P}:SINK1:STATE"] = "fault"
    result = run_scenario(FakeHost(events, pvs), sc, tmp_path)
    assert not result.passed
    assert any("SINK1:STATE" in f for f in result.failures)


def test_stack_state_tail(tmp_path: Path) -> None:
    events = [
        stack(T0 - 9, "starting"),
        stack(T0 - 5, "ready"),
        stack(T0 + 1, "stopping"),
        stack(T0 + 2, "stopped"),
    ]
    pvs = {f"{P}:STATE": "stopped", f"{P}:READY": "NOT READY"}
    expect = Expect(
        state="stopped", ready=0, tail=("stopping", "stopped"), forbidden=("fault",)
    )
    sc = Scenario("T4", "tail", steps=(Put("STOP"),), expect=expect)
    assert run_scenario(FakeHost(events, pvs), sc, tmp_path).passed

    bad = [*events[:2], stack(T0 - 3, "fault"), *events[2:]]
    result = run_scenario(FakeHost(bad, pvs), sc, tmp_path)
    assert not result.passed
    assert any("fault" in f and "forbidden" in f for f in result.failures)

    direct = [*events[:2], events[3]]
    result = run_scenario(FakeHost(direct, pvs), sc, tmp_path)
    assert not result.passed
    assert any("tail" in f for f in result.failures)


def test_put_then_wait_is_armed_before_the_put(tmp_path: Path) -> None:
    sc = Scenario(
        "T5",
        "putwait",
        steps=(
            Put("START"),
            Wait("READY", "1"),
            Put("RESTART"),
            Wait("STATE", "stopping", 30),
        ),
        expect=Expect(state="ready", ready=1),
    )
    host = FakeHost(READY_RUN)
    assert run_scenario(host, sc, tmp_path).passed
    assert f"ca putwait {P}:START 1 {P}:READY READY --timeout 120" in host.calls
    assert f"ca putwait {P}:RESTART 1 {P}:STATE stopping --timeout 30" in host.calls

    host = FakeHost(READY_RUN, wait_ok={f"{P}:STATE": False})
    result = run_scenario(host, sc, tmp_path)
    assert not result.passed
    assert any("Wait STATE stopping" in f for f in result.failures)


def test_scenario_isolation(tmp_path: Path) -> None:
    # A step raising still clears the target (finally), and the error surfaces.
    sc = Scenario("T6", "boom", steps=(Put("START"), Sh("false")))
    host = FakeHost(READY_RUN, failing_sh="false")
    with pytest.raises(AssertionError, match="false exited 1"):
        run_scenario(host, sc, tmp_path)
    assert host.calls[0] == "clear"
    assert host.calls[-1] == "clear"
    evidence = json.loads((tmp_path / "fake" / "T6.json").read_text())
    assert evidence["passed"] is False
    assert "false exited 1" in evidence["failures"][0]

    # The next run refuses to start while a unit is still active after clear().
    host = FakeHost(READY_RUN, active_after_clear=["scdemo-relay.service"])
    with pytest.raises(ScenarioIsolationError, match="scdemo-relay.service"):
        run_scenario(host, ready_scenario(), tmp_path)
    assert "start_tool" not in host.calls
    assert not any(c.startswith("gating") for c in host.calls)
    assert host.calls.count("clear") == 2

    # The session alone does steps 1-4 on entry and clear() on exit.
    host = FakeHost(READY_RUN)
    with scenario_session(host, ready_scenario()) as session:
        assert host.calls.count("clear") == 1
        assert "start_tool" in host.calls
        assert f"ca wait {P}:STATE stopped --timeout 30" in host.calls
        session.run_steps()
        final = session.wait_final()
        assert final.time_to_final_s == pytest.approx(5.0)
    assert host.calls[-1] == "clear"


def test_faults_and_gating_applied(tmp_path: Path) -> None:
    sc = Scenario(
        "T7",
        "faults",
        gating=("none", "none", "none"),
        faults={
            "scdemo-sink@.service": ("SCDEMO_DELAY_BIND=3",),
            "scdemo-sink@2.service": ("SCDEMO_FAULT_SPIN_AFTER=5",),
        },
    )
    host = FakeHost(READY_RUN)
    run_scenario(host, sc, tmp_path)
    assert "gating none none none" in host.calls
    assert "fault scdemo-sink@.service SCDEMO_DELAY_BIND=3" in host.calls
    assert "fault scdemo-sink@2.service SCDEMO_FAULT_SPIN_AFTER=5" in host.calls
    assert host.calls.index("fault scdemo-sink@.service SCDEMO_DELAY_BIND=3") < (
        host.calls.index("start_tool")
    )


def test_evidence_json_shape(tmp_path: Path) -> None:
    gate = fault(
        T0 + 2,
        "gate_violation",
        "relay",
        ("scdemo-relay.service", "scdemo-sink@1.service"),
        "scdemo-relay.service started at 12:00:01.250 before scdemo-sink@1.service"
        " was ready (sink@1.udp)",
    )
    log_line: Event = {
        "ts": T0,
        "kind": "log",
        "level": "INFO",
        "logger": "x",
        "text": "hi",
    }
    events = [*READY_RUN, gate, log_line]
    expect = Expect(state="ready", ready=1, allowed=("gate_violation*:*",))
    run_scenario(FakeHost(events), Scenario("T8", "shape", expect=expect), tmp_path)

    data = json.loads((tmp_path / "fake" / "T8.json").read_text())
    assert set(data) >= {
        "scenario",
        "title",
        "target",
        "arch",
        "started",
        "passed",
        "failures",
        "expect",
        "observed",
        "gate",
        "events",
    }
    assert data["scenario"] == "T8"
    assert data["target"] == "fake"
    assert data["arch"] == "aarch64"
    assert data["passed"] is True
    assert data["expect"]["state"] == "ready"
    assert data["expect"]["allowed"] == ["gate_violation*:*"]
    observed = data["observed"]
    assert set(observed) == {
        "pvs",
        "findings",
        "cleared",
        "stack_states",
        "time_to_final_s",
    }
    assert observed["pvs"][f"{P}:STATE"] == "ready"
    assert observed["findings"] == ["gate_violation:relay<scdemo-sink@1.service"]
    assert observed["stack_states"] == ["starting", "ready"]
    assert observed["time_to_final_s"] == pytest.approx(5.0)
    (g,) = data["gate"]
    assert g["dependent"] == "scdemo-relay.service"
    assert g["dependency"] == "scdemo-sink@1.service"
    assert g["dependent_start"] == "12:00:01.250"
    assert g["dependency_start"] is None
    assert g["text"].startswith("GATE gate_violation relay")
    assert all(e["kind"] != "log" for e in data["events"])
    assert len(data["events"]) == 3


def test_summarise_renders_tables(tmp_path: Path) -> None:
    for target, passed, t in (("container", True, 5.25), ("vm", False, 61.0)):
        for sid in ("G1", "F1"):
            folder = tmp_path / target
            folder.mkdir(exist_ok=True)
            data = {
                "scenario": sid,
                "title": f"title {sid}",
                "target": target,
                "arch": "x86_64" if target == "vm" else "aarch64",
                "passed": passed or sid == "G1",
                "failures": [] if passed or sid == "G1" else ["boom"],
                "observed": {"time_to_final_s": t},
            }
            (folder / f"{sid}.json").write_text(json.dumps(data))
    text = render(tmp_path)
    lines = text.splitlines()
    assert lines[0] == "| id | title | container (aarch64) | vm (x86_64) |"
    assert lines[1].startswith("|---")
    assert "| G1 | title G1 | pass (5.2 s) | pass (61.0 s) |" in lines
    assert "| F1 | title F1 | pass (5.2 s) | FAIL (61.0 s) |" in lines
    assert lines.index("| G1 | title G1 | pass (5.2 s) | pass (61.0 s) |") < (
        lines.index("| F1 | title F1 | pass (5.2 s) | FAIL (61.0 s) |")
    )


def test_summarise_lists_gate_findings(tmp_path: Path) -> None:
    entry = {
        "text": "GATE gate_violation: a.service started at 12:00:01.250 before b was"
        " ready (b.one, b.two), look at a.service, b.service (journalctl)",
        "code": "gate_violation",
        "dependent": "a.service",
        "dependency": "b.service",
        "dependent_start": "12:00:01.250",
        "dependency_start": None,
    }
    for target, gate in (("container", [entry]), ("vm", [])):
        folder = tmp_path / target
        folder.mkdir()
        data = {"scenario": "G4", "title": "t", "passed": True, "gate": gate}
        (folder / "G4.json").write_text(json.dumps(data))
    text = render_gate(tmp_path)
    assert (
        "| G4 | gate_violation | a.service | b.service | 12:00:01.250 | b.one, b.two |"
        in text
    )
    assert "- container: 1 x gate_violation" in text
    assert "- vm: no gate findings" in text
