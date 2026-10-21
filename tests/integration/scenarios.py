"""The acceptance scenarios (spec 10.4) as data, and the runner that checks them.

A scenario is a gating choice, fault drop-ins, a few steps through the PVs
and an expected outcome. ``run_scenario`` runs one against a prepared target
(``conftest.Host``), checks it and writes a JSON evidence file
(``evidence.py``). ``scenario_session`` holds a scenario's state without the
checks, so screenshots can be taken while it lasts (Task 5).

The runner:

1. ``clear()``.
2. Assert every ``scdemo-*`` unit (and the tool) is inactive.
3. Set the gating and faults, and start the tool.
4. Wait until ``STATE`` connects and reads ``stopped``.
5. Run the steps. A ``Put`` directly followed by a ``Wait`` subscribes to the
   waited PV before the put, so a transient value (``stopping``) is caught.
6. Wait for ``expect.state`` with ``READY``, check the timings against the
   tool's own event times, check that the state holds, snapshot the PVs and
   events, and write the evidence.
7. ``clear()`` in ``finally``.

Times: ``t`` is the target clock just before the last action (a put, or the
tool restart), and the final time is the ``ts`` of the tool's last
``stack <state>`` event, so ``time_to_final_s`` is measured on one clock.
"""

import json
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Final, Protocol

from integration import evidence

P: Final = "SCDEMO:SC"
Gating = tuple[str, str, str]  # sinks, relay, controller: notify|waitport|sleep|none
BASE: Final[Gating] = ("waitport", "waitport", "notify")  # QD-like gating
GATE_CODES: Final = frozenset({"gate_violation", "gate_violation_possible"})
FINDING_KINDS: Final = frozenset({"finding", "gate"})
# The units that must be inactive before a scenario: the stack, the packaged
# tool and the dev tool. scdemo-hwsim (the "hardware") stays up throughout.
SCENARIO_UNITS: Final = (
    "scdemo-all.service",
    "scdemo-controller.service",
    "scdemo-relay.service",
    "scdemo-sink@1.service",
    "scdemo-sink@2.service",
    "scdemo-sc.service",
    "qdsc-dev.service",
)
_BOOL_NAMES: Final = {"0": "NOT READY", "1": "READY"}  # READY/POST_READY bi records
STOPPED_TIMEOUT: Final = 30.0  # step 4
WAIT_TIMEOUT: Final = 120.0  # a Wait step's default
WAIT_SLACK: Final = 5.0  # extra CA wait time; the event times decide `within`
EVENT_POLL: Final = 1.0  # how often a state_seen run re-reads the tool's events
READY_TIMEOUT: Final = 10.0  # READY follows STATE in the same update
PUT_TIMEOUT: Final = 10.0
PUT_WAIT_TIMEOUT: Final = 150.0  # a blocking put returns when the job ends

Event = dict[str, object]


# -- the data ------------------------------------------------------------------


@dataclass(frozen=True)
class Put:
    """Write ``value`` to ``<P>:<pv>``; ``wait`` waits for the blocking record
    (the job) to finish."""

    pv: str
    value: str | int = 1
    wait: bool = False


@dataclass(frozen=True)
class Wait:
    """Monitor ``<P>:<pv>`` until it reads ``value`` (text, or 0/1 for READY)."""

    pv: str
    value: str
    timeout: float = WAIT_TIMEOUT


@dataclass(frozen=True)
class Sh:
    cmd: str


@dataclass(frozen=True)
class Sleep:
    seconds: float


@dataclass(frozen=True)
class RestartTool:
    """Stop and start ``qdsc-dev`` under whatever the stack is doing."""


Step = Put | Wait | Sh | Sleep | RestartTool


@dataclass(frozen=True)
class Expect:
    state: str  # final SCDEMO:SC:STATE
    ready: int  # final SCDEMO:SC:READY
    post_ready: int | None = None
    # extra PV suffix -> value, e.g. {"RELAY:HTTP:STATE": "fault"}
    pvs: Mapping[str, str] = field(default_factory=dict)
    raised: tuple[str, ...] = ()  # finding keys that must be raised; "a|b" = either
    allowed: tuple[str, ...] = ()  # fnmatch patterns of other findings that may appear
    cleared: bool = False  # every raised finding has a CLEARED by the end
    # fnmatch patterns of findings that may appear only as transients: each
    # one raised must be CLEARED after its last raise (ruling 21).
    transient: tuple[str, ...] = ()
    within: float = 120.0  # seconds to reach the final state
    min_time: float = 0.0  # final state must NOT be reached before this
    hold: float = 5.0  # final state must then hold this long
    # Additions for C1: the overall states must end with ``tail`` and never
    # include any of ``forbidden`` (post-ready changes are not counted).
    tail: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    # Ruling 12 ("fault seen, not held"): STATE must have read ``state`` at
    # some point after the last action, with the ``raised`` findings; READY,
    # the hold and the final PV values are not asserted.
    state_seen: bool = False
    # The final state may be reached, break during its hold and be reached
    # again (a restart loop cycles through it). Without this, any broken hold
    # fails the scenario even if a later hold succeeds. Only F5 and F4 opt in.
    holds_may_break: bool = False


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    gating: Gating = BASE
    # unit (template for sinks) -> "SCDEMO_X=v" settings
    faults: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    steps: tuple[Step, ...] = (Put("START"),)
    expect: Expect = field(default_factory=lambda: Expect(state="ready", ready=1))


_SINKS: Final = "scdemo-sink@.service"
_RELAY: Final = "scdemo-relay.service"
_CONTROLLER: Final = "scdemo-controller.service"
_TO_READY: Final = (Put("START"), Wait("READY", "1"))
_CLEAN: Final = Expect(state="ready", ready=1, post_ready=1)

SCENARIOS: Final[tuple[Scenario, ...]] = (
    Scenario(
        "G1", "Clean start, all notify", ("notify", "notify", "notify"), expect=_CLEAN
    ),
    Scenario(
        "G2",
        "Clean start, waitport and no gate",
        ("waitport", "waitport", "none"),
        expect=_CLEAN,
    ),
    Scenario(
        "G3",
        "Clean start, all sleep",
        ("sleep", "sleep", "sleep"),
        expect=Expect("ready", 1, post_ready=1, allowed=("gate_violation_possible:*",)),
    ),
    Scenario(
        "G4",
        "No gating with a slow sink bind: gate violation",
        ("none", "none", "none"),
        {_SINKS: ("SCDEMO_DELAY_BIND=3",)},
        expect=Expect(
            "ready",
            1,
            raised=("gate_violation:relay<scdemo-sink@1.service",),
            allowed=("gate_violation*:*", "listener_missing:*", "edge_*:*"),
        ),
    ),
    Scenario(
        "F1",
        "Sink skips its stats bind",
        faults={_SINKS: ("SCDEMO_FAULT_NO_BIND=stats",)},
        expect=Expect(
            "fault",
            0,
            pvs={"SINK1:STATS:STATE": "fault"},
            raised=("listener_missing:sink@1.stats",),
            allowed=("*sink@1*", "*sink@2.stats*"),
        ),
    ),
    Scenario(
        "F2",
        "Relay binds http on the wrong port",
        faults={_RELAY: ("SCDEMO_FAULT_WRONG_PORT=http",)},
        expect=Expect(
            "fault",
            0,
            pvs={"RELAY:HTTP:STATE": "fault"},
            raised=("listener_missing:relay.http",),
            allowed=("edge_*:controller -> relay.http",),
        ),
    ),
    Scenario(
        "F3",
        "Controller never connects http",
        faults={_CONTROLLER: ("SCDEMO_FAULT_NO_CONNECT=http",)},
        expect=Expect(
            "fault",
            0,
            pvs={"E:CONTROLLER-RELAY-HTTP:STATE": "fault"},
            raised=("edge_missing:controller -> relay.http",),
            within=90,
        ),
    ),
    Scenario(
        "F4",
        "Controller connects dealer to the wrong port",
        faults={_CONTROLLER: ("SCDEMO_FAULT_WRONG_CONNECT=dealer",)},
        # The controller's REQ socket can drop for ~0.1 s at start (ruling
        # 21, as in C2): a cleared rep transient and the fault/starting/fault
        # flap it causes are allowed; the router fault must then hold.
        expect=Expect(
            "fault",
            0,
            raised=("edge_missing:controller -> relay.router",),
            transient=("edge_missing:controller -> relay.rep",),
            within=90,
            holds_may_break=True,
        ),
    ),
    Scenario(
        "F5",
        "Relay crashes after 5 s: restart loop",
        faults={_RELAY: ("SCDEMO_FAULT_CRASH_AFTER=5",)},
        expect=Expect(
            "fault",
            0,
            raised=("unit_restart_loop:relay",),
            allowed=("*",),
            within=150,
            holds_may_break=True,  # the relay cycles through fault until the loop
        ),
    ),
    Scenario(
        "F6",
        "Relay never sends READY=1",
        ("waitport", "notify", "notify"),
        {_RELAY: ("SCDEMO_FAULT_NO_READY=1",)},
        expect=Expect(
            "fault",
            0,
            state_seen=True,
            raised=("unit_start_timeout:relay|unit_failed:relay",),
            allowed=("*",),
            within=120,
        ),
    ),
    Scenario(
        "F7",
        "Relay ignores the configuration",
        faults={_RELAY: ("SCDEMO_FAULT_IGNORE_CONFIG=1",)},
        expect=Expect(
            "fault",
            0,
            state_seen=True,
            raised=("unit_start_timeout:controller|unit_failed:controller",),
            allowed=("*controller*", "*post*"),
            within=120,
        ),
    ),
    Scenario(
        "F8",
        "Relay stops sending: invisible at transport level",
        faults={_RELAY: ("SCDEMO_FAULT_STOP_SENDING_AFTER=5",)},
        steps=(Put("START"), Sleep(20)),
        expect=Expect("ready", 1, post_ready=1, hold=20),
    ),
    Scenario(
        "F9",
        "Sink 2 spins at 100% CPU",
        faults={"scdemo-sink@2.service": ("SCDEMO_FAULT_SPIN_AFTER=5",)},
        expect=Expect(
            "fault",
            0,
            pvs={"SINK2:STATE": "fault"},
            raised=("cpu_spin:sink@2",),
            within=60,
        ),
    ),
    Scenario(
        "F10",
        "Slow sink binds: transient fault, then ready",
        faults={_SINKS: ("SCDEMO_DELAY_BIND=10",)},
        # Each listener is delayed 10 s (udp at 10 s, stats at 20 s); the
        # transient fault and the stats findings are allowed (ruling 11).
        # waitport gates only udp, so relay and controller start before stats
        # binds: those definite gate violations are required, and no other
        # gate finding may appear (ruling 17).
        expect=Expect(
            "ready",
            1,
            min_time=20,
            raised=(
                "gate_violation:relay<scdemo-sink@1.service",
                "gate_violation:relay<scdemo-sink@2.service",
                "gate_violation:controller<scdemo-relay.service",
                "gate_violation:controller<scdemo-sink@1.service",
                "gate_violation:controller<scdemo-sink@2.service",
            ),
            allowed=("*stats*",),
            cleared=True,
        ),
    ),
    Scenario(
        "C1",
        "Start, restart and stop through the PVs",
        steps=(
            *_TO_READY,
            Put("RESTART"),
            Wait("STATE", "stopping", 30),
            Wait("READY", "1", 90),
            Put("STOP"),
        ),
        expect=Expect("stopped", 0, tail=("stopping", "stopped"), forbidden=("fault",)),
    ),
    Scenario(
        "C2",
        "Restart one unit through its PV",
        steps=(*_TO_READY, Put("RELAY:RESTART", wait=True)),
        # The controller's edges to relay may drop for ~0.1 s while it
        # reconnects: allowed if cleared, nothing may remain (ruling 18).
        expect=Expect(
            "ready",
            1,
            pvs={"RELAY:LASTJOB": "restart: done"},
            allowed=("edge_missing:controller -> relay.*",),
            cleared=True,
        ),
    ),
    Scenario(
        "C3",
        "Stop one unit, then start it again",
        steps=(
            *_TO_READY,
            Put("SINK1:STOP"),
            Wait("STATE", "fault", 30),
            Put("SINK1:START"),
        ),
        expect=Expect(
            "ready",
            1,
            raised=("unit_stopped:sink@1",),
            allowed=("*sink@1*",),
            cleared=True,
        ),
    ),
    Scenario(
        "C4",
        "Tool restarted under a running stack",
        steps=(*_TO_READY, RestartTool()),
        expect=Expect("ready", 1),
    ),
)


# -- the host ------------------------------------------------------------------


class _Named(Protocol):
    @property
    def name(self) -> str: ...


class ScenarioHost(Protocol):
    """What the runner needs from ``conftest.Host`` (a fake in the Mac tests)."""

    @property
    def target(self) -> _Named: ...

    @property
    def invocation(self) -> str | None: ...

    def clear(self) -> None: ...
    def sh(self, cmd: str, timeout: float = 120) -> str: ...
    def gating(self, sinks: str, relay: str, controller: str) -> None: ...
    def fault(self, unit: str, *settings: str) -> None: ...
    def start_tool(self) -> None: ...
    def stop_tool(self) -> None: ...
    def tool_events(self, invocation: str | None = None) -> list[Event]: ...
    def unit_states(self, units: Sequence[str]) -> dict[str, str]: ...
    def arch(self) -> str: ...
    def now(self) -> float: ...
    def ca(
        self, *args: str, stdin_cmd: str | None = None, check: bool = True
    ) -> Any: ...


class ScenarioIsolationError(AssertionError):
    """The target was not clean (a unit still active) before a scenario."""


class ScenarioSetupError(AssertionError):
    """The tool did not come up reading ``stopped`` before the steps."""


# -- helpers -------------------------------------------------------------------


def _pv(suffix: str) -> str:
    return f"{P}:{suffix}"


def _ca_text(suffix: str, value: str | int) -> str:
    """The value as ``ca_client.py`` reads it (a READY bi as its state name)."""
    text = str(value)
    if suffix.split(":")[-1] in {"READY", "POST_READY"} and text in _BOOL_NAMES:
        return _BOOL_NAMES[text]
    return text


def _num(value: float) -> str:
    return f"{value:g}"


def _reply(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"ca_client.py gave {value!r}, expected an object")
    return value


def finding_of(event: Event) -> Mapping[str, object] | None:
    value = event.get("finding")
    return value if isinstance(value, dict) else None


def finding_key(finding: Mapping[str, object]) -> str:
    """``<code>:<element>``, plus ``<<dependency unit>`` for gates (``Finding.key``)."""
    code, element = str(finding["code"]), str(finding["element"])
    if code in GATE_CODES:
        units = finding["units"]
        assert isinstance(units, list), finding
        assert len(units) > 1, finding
        return f"{code}:{element}<{units[1]}"
    return f"{code}:{element}"


def stack_states(events: Sequence[Event]) -> list[str]:
    """The overall states in order (post-ready changes left out)."""
    return [
        str(e.get("new"))
        for e in events
        if e.get("kind") == "stack" and not str(e.get("new")).startswith("post-")
    ]


def raised_keys(events: Sequence[Event]) -> list[str]:
    """Keys of every FAULT and GATE event, first-raised order, no repeats."""
    keys: list[str] = []
    for e in events:
        f = finding_of(e)
        if e.get("kind") in FINDING_KINDS and f is not None:
            key = finding_key(f)
            if key not in keys:
                keys.append(key)
    return keys


def cleared_keys(events: Sequence[Event]) -> list[str]:
    keys: list[str] = []
    for e in events:
        f = finding_of(e)
        if e.get("kind") == "cleared" and f is not None:
            key = finding_key(f)
            if key not in keys:
                keys.append(key)
    return keys


# -- the session ---------------------------------------------------------------


@dataclass
class FinalObservation:
    """What runner step 6 saw, before any check."""

    state: dict[str, Any]  # the STATE wait reply
    ready: dict[str, Any]  # the READY wait reply
    post_ready: dict[str, Any] | None
    hold: dict[str, Any]  # the last hold check
    broken_holds: list[dict[str, Any]]  # earlier holds that broke (retried)
    pvs: dict[str, str | None]
    events: list[Event]
    ref_t: float
    final_t: float | None  # ts of the last `stack <state>` event
    time_to_final_s: float | None


class Session:
    """A scenario's running state: the tool's invocations and the last action."""

    def __init__(self, host: ScenarioHost, sc: Scenario) -> None:
        self.host = host
        self.sc = sc
        self.invocations: list[str] = []
        self.ref_t: float | None = None
        self.step_failures: list[str] = []

    def setup(self) -> None:
        """Runner steps 2-4 (step 1, ``clear()``, is the caller's)."""
        states = self.host.unit_states(SCENARIO_UNITS)
        busy = {u: s for u, s in states.items() if s != "inactive"}
        if busy:
            raise ScenarioIsolationError(
                f"{self.sc.id}: units not inactive after clear(): {busy}"
            )
        self.host.gating(*self.sc.gating)
        for unit, settings in self.sc.faults.items():
            self.host.fault(unit, *settings)
        self._start_tool()
        reply = _reply(
            self.host.ca(
                "wait",
                _pv("STATE"),
                "stopped",
                "--timeout",
                _num(STOPPED_TIMEOUT),
                check=False,
            )
        )
        if not reply.get("ok"):
            raise ScenarioSetupError(
                f"{self.sc.id}: STATE did not read 'stopped' within"
                f" {_num(STOPPED_TIMEOUT)} s of starting the tool: {reply}"
            )

    def _start_tool(self) -> None:
        self.host.start_tool()
        invocation = self.host.invocation
        assert invocation, "start_tool() set no invocation"
        self.invocations.append(invocation)

    def events(self) -> list[Event]:
        """Every non-log event of every tool invocation in this session."""
        events: list[Event] = []
        for invocation in self.invocations:
            events.extend(
                e for e in self.host.tool_events(invocation) if e.get("kind") != "log"
            )
        return events

    # runner step 5
    def run_steps(self) -> list[str]:
        """Run the steps; return (and keep) the failures of puts and waits."""
        steps = self.sc.steps
        i = 0
        while i < len(steps):
            step = steps[i]
            following = steps[i + 1] if i + 1 < len(steps) else None
            if isinstance(step, Put) and isinstance(following, Wait) and not step.wait:
                self._put_wait(step, following)
                i += 2
                continue
            self._run(step)
            i += 1
        return list(self.step_failures)

    def _run(self, step: Step) -> None:
        host = self.host
        if isinstance(step, Put):
            args = ["put", _pv(step.pv), str(step.value)]
            limit = PUT_WAIT_TIMEOUT if step.wait else PUT_TIMEOUT
            args += [*(["--wait"] if step.wait else []), "--timeout", _num(limit)]
            reply = _reply(host.ca(*args, check=False))
            self.ref_t = float(reply["t"])
            if not reply.get("ok"):
                self.step_failures.append(f"Put {step.pv} failed: {reply.get('error')}")
        elif isinstance(step, Wait):
            want = _ca_text(step.pv, step.value)
            reply = _reply(
                host.ca(
                    "wait",
                    _pv(step.pv),
                    want,
                    "--timeout",
                    _num(step.timeout),
                    check=False,
                )
            )
            if not reply.get("ok"):
                self.step_failures.append(
                    f"Wait {step.pv} {step.value} ({_num(step.timeout)} s) timed out:"
                    f" last value {reply.get('value')!r}"
                )
        elif isinstance(step, Sh):
            host.sh(step.cmd)
        elif isinstance(step, Sleep):
            time.sleep(step.seconds)
        else:
            self.ref_t = host.now()
            host.stop_tool()
            self._start_tool()

    def _put_wait(self, put: Put, wait: Wait) -> None:
        want = _ca_text(wait.pv, wait.value)
        reply = _reply(
            self.host.ca(
                "putwait",
                _pv(put.pv),
                str(put.value),
                _pv(wait.pv),
                want,
                "--timeout",
                _num(wait.timeout),
                check=False,
            )
        )
        self.ref_t = float(reply["t"])
        if not reply.get("ok"):
            self.step_failures.append(
                f"Put {put.pv} then Wait {wait.pv} {wait.value} ({_num(wait.timeout)} s)"
                f" failed: put_ok={reply.get('put_ok')} last value"
                f" {reply.get('value')!r} {reply.get('error') or ''}".rstrip()
            )

    def _wait(self, suffix: str, value: str, limit: float) -> dict[str, Any]:
        return _reply(
            self.host.ca(
                "wait", _pv(suffix), value, "--timeout", _num(limit), check=False
            )
        )

    # runner step 6, without the checks
    def wait_final(self) -> FinalObservation:
        """Wait for the final state and check it holds. A state that is reached
        but does not hold (a transient ``fault`` before a unit restarts) is not
        the final one: wait for it again while ``within`` has time left."""
        host, exp = self.host, self.sc.expect
        ref = self.ref_t if self.ref_t is not None else host.now()
        if exp.state_seen:
            return self._wait_seen(ref)

        def left() -> float:
            return exp.within - (host.now() - ref)

        ready_text = _ca_text("READY", exp.ready)
        pairs = [f"{_pv('STATE')}={exp.state}", f"{_pv('READY')}={ready_text}"]
        if exp.post_ready is not None:
            pairs.append(
                f"{_pv('POST_READY')}={_ca_text('POST_READY', exp.post_ready)}"
            )
        broken_holds: list[dict[str, Any]] = []
        while True:
            state = self._wait("STATE", exp.state, max(left(), 1.0) + WAIT_SLACK)
            ready = self._wait("READY", ready_text, READY_TIMEOUT)
            post_ready = None
            if exp.post_ready is not None:
                post_text = _ca_text("POST_READY", exp.post_ready)
                post_ready = self._wait(
                    "POST_READY", post_text, max(left(), 1.0) + WAIT_SLACK
                )
            hold = _reply(host.ca("hold", "--for", _num(exp.hold), *pairs, check=False))
            if hold.get("ok") or not state.get("ok") or left() <= 0:
                break
            broken_holds.append(hold)
        names = ["STATE", "READY", "POST_READY", "FAULTS", *exp.pvs]
        pvs = _reply(host.ca("get", *(_pv(n) for n in names), check=False))
        events = self.events()
        final_t = None
        for e in events:
            if e.get("kind") == "stack" and e.get("new") == exp.state:
                final_t = float(str(e["ts"]))
        time_to_final = None if final_t is None else max(0.0, final_t - ref)
        return FinalObservation(
            state=state,
            ready=ready,
            post_ready=post_ready,
            hold=hold,
            broken_holds=broken_holds,
            pvs={str(k): None if v is None else str(v) for k, v in pvs.items()},
            events=events,
            ref_t=ref,
            final_t=final_t,
            time_to_final_s=time_to_final,
        )

    def _wait_seen(self, ref: float) -> FinalObservation:
        """Re-read the tool's events until STATE has read ``expect.state`` after
        ``ref`` and every ``raised`` finding is there, or ``within`` runs out."""
        host, exp = self.host, self.sc.expect
        deadline = time.monotonic() + max(exp.within - (host.now() - ref), 0.0)
        while True:
            events = self.events()
            first = _first_state_after(events, exp.state, ref)
            if first is not None and not _missing_raised(exp, raised_keys(events)):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(EVENT_POLL)
        names = ["STATE", "READY", "POST_READY", "FAULTS", *exp.pvs]
        pvs = _reply(host.ca("get", *(_pv(n) for n in names), check=False))
        return FinalObservation(
            state={"ok": first is not None, "value": pvs.get(_pv("STATE"))},
            ready={"ok": True},
            post_ready=None,
            hold={"ok": True},
            broken_holds=[],
            pvs={str(k): None if v is None else str(v) for k, v in pvs.items()},
            events=events,
            ref_t=ref,
            final_t=first,
            time_to_final_s=None if first is None else max(0.0, first - ref),
        )


def _first_state_after(events: Sequence[Event], state: str, ref: float) -> float | None:
    """``ts`` of the first ``stack <state>`` event at or after ``ref``."""
    for e in events:
        if e.get("kind") == "stack" and e.get("new") == state:
            ts = float(str(e["ts"]))
            if ts >= ref:
                return ts
    return None


def _missing_raised(exp: Expect, keys: Sequence[str]) -> list[str]:
    """The ``raised`` specs with no alternative among ``keys``."""
    return [s for s in exp.raised if not any(a in keys for a in s.split("|"))]


@contextmanager
def scenario_session(host: ScenarioHost, sc: Scenario) -> Generator[Session]:
    """Runner steps 1-4 on entry, ``clear()`` on exit (however it exits)."""
    session = Session(host, sc)
    host.clear()
    try:
        session.setup()
        yield session
    finally:
        host.clear()


# -- the checks ----------------------------------------------------------------


def check(
    sc: Scenario, final: FinalObservation, step_failures: Sequence[str]
) -> list[str]:
    """Every way the observation differs from ``sc.expect``."""
    exp = sc.expect
    failures = list(step_failures)
    if exp.state_seen and not final.state.get("ok"):
        failures.append(
            f"STATE never read {exp.state!r} within {_num(exp.within)} s"
            f" (now {final.state.get('value')!r})"
        )
    elif not final.state.get("ok"):
        failures.append(
            f"STATE did not reach {exp.state!r} within {_num(exp.within)} s"
            f" (last value {final.state.get('value')!r})"
        )
    if not final.ready.get("ok"):
        failures.append(
            f"READY did not read {exp.ready} with STATE {exp.state!r}"
            f" (last value {final.ready.get('value')!r})"
        )
    if final.post_ready is not None and not final.post_ready.get("ok"):
        failures.append(
            f"POST_READY did not read {exp.post_ready} within {_num(exp.within)} s"
            f" (last value {final.post_ready.get('value')!r})"
        )
    t = final.time_to_final_s
    if t is None:
        failures.append(f"the tool logged no 'stack {exp.state}' event")
    else:
        if t > exp.within:
            failures.append(
                f"reached {exp.state} after {t:.1f} s, not within {_num(exp.within)} s"
            )
        if t < exp.min_time:
            failures.append(
                f"reached {exp.state} after {t:.1f} s, before min_time"
                f" {_num(exp.min_time)} s"
            )
    if not final.hold.get("ok"):
        broken = final.hold.get("broken") or {}
        failures.append(
            f"the final state did not hold for {_num(exp.hold)} s:"
            f" {broken.get('pv')} read {broken.get('value')!r}"
        )
    if not exp.holds_may_break:
        for hold in final.broken_holds:
            broken = hold.get("broken") or {}
            failures.append(
                f"the final state {exp.state!r} was reached but did not hold:"
                f" {broken.get('pv')} read {broken.get('value')!r}"
                " (opt in with holds_may_break)"
            )

    wanted: dict[str, str] = {}
    if not exp.state_seen:
        wanted = {"STATE": exp.state, "READY": _ca_text("READY", exp.ready)}
    if exp.post_ready is not None:
        wanted["POST_READY"] = _ca_text("POST_READY", exp.post_ready)
    wanted.update(exp.pvs)
    for suffix, want in wanted.items():
        got = final.pvs.get(_pv(suffix))
        if got != want:
            failures.append(f"{_pv(suffix)} is {got!r}, expected {want!r}")

    failures += _check_findings(exp, final.events)

    states = stack_states(final.events)
    if exp.tail and tuple(states[-len(exp.tail) :]) != exp.tail:
        failures.append(f"stack states {states} do not end with tail {list(exp.tail)}")
    for state in exp.forbidden:
        if state in states:
            failures.append(f"stack state {state!r} is forbidden here: {states}")
    return failures


def _check_findings(exp: Expect, events: Sequence[Event]) -> list[str]:
    failures: list[str] = []
    keys = raised_keys(events)
    alternatives = {alt for spec in exp.raised for alt in spec.split("|")}
    for spec in _missing_raised(exp, keys):
        failures.append(f"expected finding {spec} was not raised")
    texts = {}
    for e in events:
        f = finding_of(e)
        if e.get("kind") in FINDING_KINDS and f is not None:
            texts.setdefault(finding_key(f), str(e.get("text")))
    for key in keys:
        if key in alternatives:
            continue
        if any(fnmatchcase(key, pattern) for pattern in exp.allowed):
            continue
        if any(fnmatchcase(key, pattern) for pattern in exp.transient):
            continue  # must clear, checked below
        failures.append(f"unexpected finding {key}: {texts[key]}")
    must_clear = [
        key
        for key in raised_keys([e for e in events if e.get("kind") == "finding"])
        # FAULT findings only: a GATE finding is an event about a start that
        # has happened (spec 5.4), cleared only when the dependent restarts.
        if exp.cleared
        or (
            key not in alternatives
            and any(fnmatchcase(key, pattern) for pattern in exp.transient)
        )
    ]
    for key in must_clear:
        last_raised = max(
            i
            for i, e in enumerate(events)
            if e.get("kind") in FINDING_KINDS
            and (f := finding_of(e)) is not None
            and finding_key(f) == key
        )
        if not any(
            i > last_raised
            and e.get("kind") == "cleared"
            and (f := finding_of(e)) is not None
            and finding_key(f) == key
            for i, e in enumerate(events)
        ):
            failures.append(f"finding {key} never cleared")
    return failures


# -- the runner ----------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioResult:
    id: str
    passed: bool
    failures: tuple[str, ...]
    evidence: Path
    time_to_final_s: float | None


def _evidence(
    sc: Scenario,
    target: str,
    arch: str,
    started: str,
    failures: Sequence[str],
    final: FinalObservation | None,
) -> evidence.EvidenceJson:
    events = final.events if final is not None else []
    observed: evidence.ObservedJson | None = None
    if final is not None:
        observed = {
            "pvs": final.pvs,
            "findings": raised_keys(events),
            "cleared": cleared_keys(events),
            "stack_states": stack_states(events),
            "time_to_final_s": final.time_to_final_s,
        }
    return {
        "scenario": sc.id,
        "title": sc.title,
        "target": target,
        "arch": arch,
        "started": started,
        "passed": not failures,
        "failures": list(failures),
        # JSON round trip: tuples become lists, the Mapping a dict.
        "expect": json.loads(json.dumps(asdict(sc.expect))),
        "observed": observed,
        "gate": evidence.gate_entries(events),
        "broken_holds": final.broken_holds if final is not None else [],
        "events": events,
    }


def run_scenario(
    host: ScenarioHost, sc: Scenario, evidence_dir: Path
) -> ScenarioResult:
    """Run, check and record one scenario (runner steps 1-7)."""
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    target = host.target.name
    arch = host.arch()
    path = evidence_dir / target / f"{sc.id}.json"
    try:
        with scenario_session(host, sc) as session:
            step_failures = session.run_steps()
            final = session.wait_final()
    except Exception as err:  # record the broken run, then re-raise
        failed = [f"{type(err).__name__}: {err}"]
        evidence.write(path, _evidence(sc, target, arch, started, failed, None))
        raise
    failures = check(sc, final, step_failures)
    evidence.write(path, _evidence(sc, target, arch, started, failures, final))
    return ScenarioResult(
        id=sc.id,
        passed=not failures,
        failures=tuple(failures),
        evidence=path,
        time_to_final_s=final.time_to_final_s,
    )
