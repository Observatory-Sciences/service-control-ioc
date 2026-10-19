"""Core wiring against fakes: FakeSystemd on D-Bus, a scripted scan, a fake clock."""

import asyncio
import itertools
import json
import logging
import os
import signal
import sys
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from fakes import (
    IDLE_JOB,
    UNIT_IFACE,
    FakeClock,
    FakeSystemd,
    ScriptedScan,
    Waiter,
    everything,
    scdemo_spec,
    scdemo_table,
    unit_props,
)
from qd_service_control.cli import serve
from qd_service_control.config.spec import StackSpec
from qd_service_control.core import Core
from qd_service_control.model.types import ElementState, Event
from qd_service_control.observe.journal import JournalLine

SPEC = scdemo_spec()
UNITS = {sid: s.unit for sid, s in SPEC.services.items()}
ALL_UNIT = "scdemo-all.service"
AFTER = {
    UNITS["controller"]: [UNITS["relay"]],
    UNITS["relay"]: [UNITS["sink@1"], UNITS["sink@2"]],
    ALL_UNIT: list(UNITS.values()),
}
T = 1_700_000_000.0
EVERYTHING = everything(SPEC)
SINKS_UP = ("sink@1.udp", "sink@1.stats", "sink@2.udp", "sink@2.stats")
CONTROLLER_EDGES = tuple(e for e in SPEC.edges if e.startswith("controller -> "))
RELAY_UP = tuple(x for x in EVERYTHING if x not in CONTROLLER_EDGES)
SLEEPER = (sys.executable, "-c", "import time; time.sleep(60)")
FAKE_JOURNALCTL = Path(__file__).parent / "observe" / "fake_journalctl.py"
JOB = (1, "/org/freedesktop/systemd1/job/1")


def us(t: float) -> int:
    return int(t * 1_000_000)


def props(
    unit: str, state: str, start: float = T - 100, **extra: object
) -> dict[str, object]:
    """Unit properties in a state: stopped, queued, activating or active."""
    active, sub, job, pid = {
        "stopped": ("inactive", "dead", IDLE_JOB, 0),
        "queued": ("inactive", "dead", JOB, 0),
        "activating": ("activating", "start", IDLE_JOB, 100),
        "active": ("active", "running", IDLE_JOB, 100),
    }[state]
    base = unit_props(
        ActiveState=active,
        SubState=sub,
        Job=job,
        MainPID=pid,
        ExecMainStartTimestamp=us(start) if pid else 0,
        ActiveEnterTimestamp=us(start) if state == "active" else 0,
        ControlGroup=f"/system.slice/{unit}",
        InvocationID=unit.encode(),
        After=AFTER.get(unit, []),
    )
    base.update(extra)
    return base


def stack(
    state: str = "active", overrides: Mapping[str, dict[str, object]] | None = None
) -> FakeSystemd:
    units = {u: props(u, state) for u in (*UNITS.values(), ALL_UNIT)}
    units.update(overrides or {})
    return FakeSystemd(units)


class Harness:
    def __init__(
        self,
        systemd: FakeSystemd,
        scan: ScriptedScan,
        clock: FakeClock,
        *,
        spec: StackSpec = SPEC,
        read_cpu: Callable[[str], int | None] = lambda cg: None,
        journalctl: Iterable[str] = SLEEPER,
    ) -> None:
        self.systemd = systemd
        self.scan = scan
        self.clock = clock
        self.events: list[Event] = []
        self.waiter = Waiter()
        self.core = Core(
            spec,
            connect=systemd.connect,
            scan=scan,
            read_cpu=read_cpu,
            probe=lambda path, non_empty: True,
            journalctl=tuple(journalctl),
            clock=clock,
            bus_backoff=(0.001,),
        )
        self.unsubscribe = self.core.subscribe(self._on_event)

    def _on_event(self, event: Event) -> None:
        self.events.append(event)
        self.waiter.poke()

    def texts(self) -> list[str]:
        return [e.text for e in self.events]

    def count(self, prefix: str) -> int:
        return sum(1 for t in self.texts() if t.startswith(prefix))

    async def wait_text(self, prefix: str, times: int = 1) -> None:
        await self.waiter.wait_for(lambda: self.count(prefix) >= times)

    def faults(self) -> list[str]:
        return [
            e.text
            for e in self.events
            if e.kind in ("finding", "gate") or e.new == "fault"
        ]

    def change(self, unit: str, state: str, start: float, **extra: object) -> None:
        """A unit changes state on the bus (one Unit PropertiesChanged)."""
        changed = props(unit, state, start, **extra)
        self.systemd.bus.fire_properties_changed(unit, UNIT_IFACE, changed)


@asynccontextmanager
async def running(core: Core) -> AsyncGenerator["asyncio.Task[None]"]:
    task = asyncio.create_task(core.run())
    try:
        yield task
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise


def states(core: Core, kind: str) -> set[ElementState]:
    return {v.state for v in core.view().elements.values() if v.kind == kind}


@pytest.mark.asyncio
async def test_core_end_to_end_with_fakes() -> None:
    clock = FakeClock(T)
    h = Harness(stack("stopped"), ScriptedScan(), clock)
    sinks = (UNITS["sink@1"], UNITS["sink@2"])
    async with running(h.core):
        await h.wait_text("stack stopped (+0.0 s)")
        clock.now = T + 1
        for unit in (*UNITS.values(), ALL_UNIT):
            h.change(unit, "queued", T + 1)
        await h.wait_text("stack starting (+0.0 s)")

        clock.now = T + 2
        for unit in sinks:
            h.change(unit, "activating", T + 2)
        clock.now = T + 3
        for unit in sinks:
            h.change(unit, "active", T + 3, ExecMainStartTimestamp=us(T + 2))
        clock.now = T + 4
        h.scan.table = scdemo_table(SPEC, SINKS_UP)
        await h.wait_text("listener sink@2.stats ready")

        clock.now = T + 5
        h.change(UNITS["relay"], "activating", T + 5)
        clock.now = T + 6
        h.change(UNITS["relay"], "active", T + 6, ExecMainStartTimestamp=us(T + 5))
        await h.wait_text("service relay ready")
        await h.scan.polls()
        clock.now = T + 7
        h.scan.table = scdemo_table(SPEC, RELAY_UP)
        await h.wait_text("edge relay -> sink@2.stats ready")

        clock.now = T + 8
        h.change(UNITS["controller"], "active", T + 8)
        h.change(ALL_UNIT, "active", T + 8)
        await h.wait_text("service controller ready")
        await h.scan.polls()
        clock.now = T + 9
        h.scan.table = scdemo_table(SPEC, EVERYTHING)
        await h.wait_text("stack ready (+8.0 s)")

    assert h.faults() == []
    # The post edges are all there once the relay's scan is: before ready.
    assert "stack post-ready (+6.0 s)" in h.texts()
    ready = [f"{e.kind} {e.element}" for e in h.events if e.new == "ready"]
    stages = [
        [f"service {s}" for s in ("sink@1", "sink@2")],
        [f"listener {x}" for x in SINKS_UP],
        ["service relay"],
        [f"listener relay.{n}" for n in ("rep", "router", "pub", "http", "udp")]
        + ["edge relay -> sink@1.stats", "edge relay -> sink@2.stats"],
        ["service controller"],
        [f"edge {e}" for e in CONTROLLER_EDGES],
        ["stack stack"],
    ]
    for earlier, later in itertools.pairwise(stages):
        assert max(ready.index(x) for x in earlier) < min(ready.index(x) for x in later)
    stack_states = [
        e.new for e in h.events if e.kind == "stack" and "post" not in e.text
    ]
    assert stack_states == ["stopped", "starting", "ready"]


@pytest.mark.asyncio
async def test_tool_started_with_stack_up_goes_straight_to_ready() -> None:
    h = Harness(stack(), ScriptedScan(scdemo_table(SPEC, EVERYTHING)), FakeClock(T))
    async with running(h.core):
        await h.wait_text("stack post-ready")
    assert h.faults() == []
    kinds = [e.kind for e in h.events]
    n_listeners = sum(1 for x in SPEC.listeners.values() if not x.on_host)
    assert kinds == (
        ["service"] * len(SPEC.services)
        + ["listener"] * n_listeners
        + ["edge"] * len(SPEC.edges)
        + ["stack", "stack"]
    )
    assert all(e.new == "ready" for e in h.events if e.kind != "stack")
    assert h.texts()[-2:] == ["stack ready (+0.0 s)", "stack post-ready (+0.0 s)"]
    view = h.core.view()
    assert view.ready
    assert view.findings == ()


@pytest.mark.asyncio
async def test_slow_bus_reads_still_go_straight_to_ready() -> None:
    # The bus connects at once but reading the units takes a while; polls
    # finish meanwhile. Nothing may be published until the units are read.
    systemd = stack()
    systemd.reads_open.clear()
    h = Harness(systemd, ScriptedScan(scdemo_table(SPEC, EVERYTHING)), FakeClock(T))
    async with running(h.core):
        await h.scan.polls()
        assert systemd.buses  # connected, units not read yet
        assert h.events == []
        systemd.reads_open.set()
        await h.wait_text("stack post-ready")
    assert h.faults() == []
    assert not [e for e in h.events if e.new in ("unknown", "starting")]
    assert h.texts()[-2:] == ["stack ready (+0.0 s)", "stack post-ready (+0.0 s)"]


@pytest.mark.asyncio
async def test_staged_start_split_burst_has_no_faults() -> None:
    # scdemo-all and the sinks are queued in one read, the rest in a later one.
    clock = FakeClock(T)
    h = Harness(stack("stopped"), ScriptedScan(), clock)
    first = (ALL_UNIT, UNITS["sink@1"], UNITS["sink@2"])
    async with running(h.core):
        await h.wait_text("stack stopped")
        clock.now = T + 1
        for unit in first:
            h.change(unit, "queued", T + 1)
        await h.wait_text("stack starting")
        await h.scan.polls()
        for unit in (UNITS["relay"], UNITS["controller"]):
            h.change(unit, "queued", T + 1)
        await h.wait_text("service controller waiting")
        await h.scan.polls()
    assert h.faults() == []
    assert [e.new for e in h.events if e.kind == "stack"] == ["stopped", "starting"]


@pytest.mark.asyncio
async def test_orderly_stop_has_no_faults() -> None:
    # systemctl stop scdemo-all: stop jobs on every unit, then reverse order.
    clock = FakeClock(T)
    h = Harness(stack(), ScriptedScan(scdemo_table(SPEC, EVERYTHING)), clock)
    stop_job = (2, "/org/freedesktop/systemd1/job/2")
    async with running(h.core):
        await h.wait_text("stack post-ready")
        clock.now = T + 1
        for unit in (*UNITS.values(), ALL_UNIT):
            h.change(unit, "active", T - 100, Job=stop_job)
        await h.scan.polls()
        clock.now = T + 2
        h.change(ALL_UNIT, "stopped", T - 100)
        h.change(UNITS["controller"], "stopped", T - 100)
        h.scan.table = scdemo_table(SPEC, RELAY_UP)
        await h.wait_text("edge controller -> relay.rep waiting")
        await h.scan.polls()
        clock.now = T + 3
        h.change(UNITS["relay"], "stopped", T - 100)
        h.scan.table = scdemo_table(SPEC, SINKS_UP)
        await h.wait_text("service relay stopped")
        await h.scan.polls()
        clock.now = T + 4
        for unit in (UNITS["sink@1"], UNITS["sink@2"]):
            h.change(unit, "stopped", T - 100)
        h.scan.table = scdemo_table(SPEC, ())
        await h.wait_text("stack stopped")
    assert h.faults() == []
    stack_states = [
        e.new for e in h.events if e.kind == "stack" and "post" not in e.text
    ]
    assert stack_states == ["ready", "starting", "stopped"]


@pytest.mark.asyncio
async def test_event_time_never_goes_back() -> None:
    clock = FakeClock(T)
    h = Harness(stack(), ScriptedScan(scdemo_table(SPEC, EVERYTHING)), clock)
    async with running(h.core):
        await h.wait_text("stack post-ready")
        clock.now = T + 5
        h.change(UNITS["controller"], "activating", T + 5)
        await h.wait_text("service controller waiting")
        clock.now = T + 2  # the wall clock steps back
        h.change(UNITS["controller"], "active", T + 5)
        await h.wait_text("service controller ready", times=2)
    assert h.texts()[-1].endswith("(+5.0 s)")
    assert not [t for t in h.texts() if "(+-" in t]


@pytest.mark.asyncio
async def test_core_subscriber_exception_does_not_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    systemd = stack()
    scan = ScriptedScan(scdemo_table(SPEC, EVERYTHING))
    calls: list[Event] = []

    def broken(event: Event) -> None:
        calls.append(event)
        raise RuntimeError("subscriber bug")

    clock = FakeClock(T)
    h = Harness(systemd, scan, clock)
    h.unsubscribe()
    unsubscribe_broken = h.core.subscribe(broken)
    h.core.subscribe(h._on_event)
    async with running(h.core):
        await h.wait_text("stack ready")
        assert len(calls) == len(h.events)  # both got every event
        await h.wait_text("stack post-ready")
        assert any(
            r.levelno == logging.ERROR and r.exc_info is not None
            for r in caplog.records
        )
        unsubscribe_broken()
        seen = len(calls)
        clock.now = T + 1
        h.change(UNITS["controller"], "activating", T + 1)
        await h.wait_text("service controller waiting")
        assert len(calls) == seen


@pytest.mark.asyncio
async def test_scan_error_makes_elements_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="qd_service_control")
    h = Harness(stack(), ScriptedScan(scdemo_table(SPEC, EVERYTHING)), FakeClock(T))
    async with running(h.core):
        await h.wait_text("stack ready")
        h.scan.error = PermissionError(13, "Permission denied", "/proc/net/tcp")
        await h.wait_text("listener relay.rep unknown: no socket scan")
        await h.scan.polls()
        assert states(h.core, "listener") == {ElementState.UNKNOWN}
        assert states(h.core, "edge") == {ElementState.UNKNOWN}
        assert states(h.core, "service") == {ElementState.READY}
        assert not h.core.view().ready
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "socket scan failed" in warnings[0].getMessage()

        h.scan.error = None
        await h.wait_text("stack ready", times=2)
    assert h.faults() == []


@pytest.mark.asyncio
async def test_bus_drop_makes_services_unknown_then_recovers() -> None:
    systemd = stack()
    h = Harness(systemd, ScriptedScan(scdemo_table(SPEC, EVERYTHING)), FakeClock(T))
    async with running(h.core):
        await h.wait_text("stack ready")
        systemd.bus.drop()
        await h.wait_text("service relay unknown: bus disconnected")
        await h.wait_text("stack ready", times=2)
        assert len(systemd.buses) == 2
        assert states(h.core, "service") == {ElementState.READY}
    texts = h.texts()
    dropped = texts.index("service relay unknown: bus disconnected (+0.0 s)")
    assert "stack starting (+0.0 s)" in texts[dropped:]
    assert h.faults() == []


@pytest.mark.asyncio
async def test_preflight_not_found_unit_exits_2(
    caplog: pytest.LogCaptureFixture,
) -> None:
    systemd = stack()
    del systemd.units[UNITS["relay"]]
    h = Harness(systemd, ScriptedScan(), FakeClock(T))
    with pytest.raises(SystemExit) as exc_info:
        async with asyncio.timeout(2):
            await h.core.run()
    assert exc_info.value.code == 2
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["unit scdemo-relay.service: load state not-found"]


@pytest.mark.asyncio
async def test_preflight_masked_unit_warns_and_carries_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    masked = props(UNITS["relay"], "stopped", LoadState="masked")
    h = Harness(stack(overrides={UNITS["relay"]: masked}), ScriptedScan(), FakeClock(T))
    async with running(h.core):
        await h.wait_text("stack ")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "unit scdemo-relay.service: load state masked" in warnings


@pytest.mark.asyncio
async def test_bus_unreachable_at_startup_keeps_retrying() -> None:
    systemd = stack()
    systemd.refuse = True
    h = Harness(systemd, ScriptedScan(scdemo_table(SPEC, EVERYTHING)), FakeClock(T))
    async with running(h.core):
        await h.wait_text("service relay unknown: bus disconnected")
        while systemd.attempts < 3:
            await h.scan.polls(1)
        assert not h.core.view().ready
        systemd.refuse = False
        await h.wait_text("stack ready")


@pytest.mark.asyncio
async def test_unreachable_then_not_found_exits_2() -> None:
    systemd = stack()
    del systemd.units[ALL_UNIT]
    systemd.refuse = True
    h = Harness(systemd, ScriptedScan(), FakeClock(T))

    async def bus_comes_up() -> None:
        await h.wait_text("service relay unknown: bus disconnected")
        systemd.refuse = False

    # SystemExit escapes a Task's event loop, so run the core in this task.
    helper = asyncio.create_task(bus_comes_up())
    with pytest.raises(SystemExit) as exc_info:
        async with asyncio.timeout(2):
            await serve(h.core)
    await helper
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_cpu_per_unit_from_control_group() -> None:
    usage: dict[str, int] = {}
    clock = FakeClock(T)
    h = Harness(
        stack(),
        ScriptedScan(scdemo_table(SPEC, EVERYTHING)),
        clock,
        read_cpu=lambda cg: usage.get(cg),
    )
    relay_cg = f"/system.slice/{UNITS['relay']}"
    usage[relay_cg] = 1_000_000

    def tick() -> None:  # each scan: 1 s passes, the relay uses 0.5 s of CPU
        clock.now += 1
        usage[relay_cg] += 500_000

    h.scan.on_scan = tick
    async with running(h.core):
        await h.wait_text("stack ready")
        await h.scan.polls()
        cpu = h.core.view().cpu
    assert cpu[UNITS["relay"]] == pytest.approx(50.0)
    assert cpu[UNITS["controller"]] is None


@pytest.mark.asyncio
async def test_journal_lines_reach_subscribers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = {
        "_SYSTEMD_UNIT": UNITS["relay"],
        "MESSAGE": "relay bound",
        "__REALTIME_TIMESTAMP": "1700000000000000",
        "__CURSOR": "c1",
    }
    journal = tmp_path / "journal.json"
    journal.write_text(json.dumps(entry) + "\n")
    monkeypatch.setenv("FAKE_JOURNAL", str(journal))
    h = Harness(
        stack(),
        ScriptedScan(),
        FakeClock(T),
        journalctl=(sys.executable, str(FAKE_JOURNALCTL)),
    )
    lines: list[JournalLine] = []

    def on_line(line: JournalLine) -> None:
        lines.append(line)
        h.waiter.poke()

    h.core.subscribe_journal(on_line)
    async with running(h.core):
        await h.waiter.wait_for(lambda: bool(lines))
    assert lines[0].unit == UNITS["relay"]
    assert h.core.journal("relay") == (lines[0].text,)
    assert lines[0].text.endswith(" relay bound")
    assert h.core.journal("controller") == ()


@pytest.mark.asyncio
async def test_serve_stops_on_sigterm() -> None:
    h = Harness(stack(), ScriptedScan(scdemo_table(SPEC, EVERYTHING)), FakeClock(T))
    task = asyncio.create_task(serve(h.core))
    await h.wait_text("stack ready")
    os.kill(os.getpid(), signal.SIGTERM)
    async with asyncio.timeout(2):
        await task  # returns normally: a graceful stop
