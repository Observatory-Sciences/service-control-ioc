import asyncio
from collections.abc import Callable, Sequence

import pytest

from fakes import SERVICE_IFACE, UNIT_IFACE, FakeBus, FakeSystemd, unit_props
from qd_service_control.model.types import UnitSnapshot
from qd_service_control.observe.systemd import (
    SystemdObserver,
    snapshot_from_props,
    unit_path,
)


class Recorder:
    def __init__(self) -> None:
        self.snapshots: list[UnitSnapshot] = []
        self.connection: list[bool] = []
        self._changed = asyncio.Event()

    def on_snapshot(self, snapshot: UnitSnapshot) -> None:
        self.snapshots.append(snapshot)
        self._changed.set()

    def on_connection(self, up: bool) -> None:
        self.connection.append(up)
        self._changed.set()

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(2):
            while not predicate():
                self._changed.clear()
                await self._changed.wait()


UNITS = ("a.service", "b.service")


@pytest.fixture
def systemd() -> FakeSystemd:
    return FakeSystemd(
        {
            "a.service": unit_props(),
            "b.service": unit_props(MainPID=200, After=["a.service"]),
        }
    )


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def make_observer(
    systemd: FakeSystemd, recorder: Recorder, units: Sequence[str] = UNITS
) -> SystemdObserver:
    return SystemdObserver(
        units,
        systemd.connect,
        recorder.on_snapshot,
        recorder.on_connection,
        backoff=(0,),
    )


async def started(
    observer: SystemdObserver, recorder: Recorder
) -> "asyncio.Task[None]":
    task = asyncio.create_task(observer.run())
    await recorder.wait_for(lambda: recorder.connection == [True])
    return task


async def stop(task: "asyncio.Task[None]") -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_unit_path_escaping() -> None:
    assert (
        unit_path("qd-frame-receiver@1.service")
        == "/org/freedesktop/systemd1/unit/qd_2dframe_2dreceiver_401_2eservice"
    )
    assert unit_path("1a.target") == "/org/freedesktop/systemd1/unit/_31a_2etarget"


def test_snapshot_from_props() -> None:
    props = unit_props(
        ActiveState="activating",
        SubState="auto-restart",
        Result="exit-code",
        Job=[7, "/org/freedesktop/systemd1/job/7"],
        NRestarts=3,
        MainPID=0,
        ExecMainCode=1,
        ExecMainStatus=2,
        After=["network.target", "a.service"],
    )
    assert snapshot_from_props("x.service", props) == UnitSnapshot(
        name="x.service",
        load_state="loaded",
        active_state="activating",
        sub_state="auto-restart",
        result="exit-code",
        job_pending=True,
        n_restarts=3,
        main_pid=0,
        exec_main_code=1,
        exec_main_status=2,
        exec_main_start_us=1_000_000,
        active_enter_us=1_500_000,
        control_group="/system.slice/a.service",
        invocation_id="00112233445566778899aabbccddeeff",
        after=("network.target", "a.service"),
    )


def test_snapshot_from_props_idle_job_and_target() -> None:
    snap = snapshot_from_props(
        "t.target",
        {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "active",
            "Job": [0, "/"],
            "InvocationID": b"",
        },
    )
    assert not snap.job_pending
    assert snap.invocation_id == ""
    assert snap.main_pid == 0
    assert snap.after == ()


@pytest.mark.asyncio
async def test_initial_snapshot_then_change(
    systemd: FakeSystemd, recorder: Recorder
) -> None:
    observer = make_observer(systemd, recorder)
    task = await started(observer, recorder)
    try:
        assert systemd.bus.subscribed
        assert [s.name for s in recorder.snapshots] == list(UNITS)
        assert recorder.snapshots[1].main_pid == 200

        systemd.bus.fire_properties_changed(
            "a.service", SERVICE_IFACE, {"MainPID": 0, "Result": "exit-code"}
        )
        assert len(recorder.snapshots) == 2

        systemd.bus.fire_properties_changed(
            "a.service",
            UNIT_IFACE,
            {"ActiveState": "activating", "SubState": "auto-restart"},
        )
        assert len(recorder.snapshots) == 3
        snap = recorder.snapshots[-1]
        assert snap.name == "a.service"
        assert (snap.active_state, snap.sub_state) == ("activating", "auto-restart")
        assert (snap.main_pid, snap.result) == (0, "exit-code")
        assert observer.after_graph() == {
            "a.service": ("basic.target",),
            "b.service": ("a.service",),
        }
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_duplicate_unit_signal_ignored(
    systemd: FakeSystemd, recorder: Recorder
) -> None:
    task = await started(make_observer(systemd, recorder), recorder)
    try:
        change = {"ActiveState": "deactivating", "SubState": "stop-sigterm"}
        systemd.bus.fire_properties_changed("a.service", UNIT_IFACE, change)
        systemd.bus.fire_properties_changed("a.service", UNIT_IFACE, change)
        # Same key, other properties differ: still a duplicate.
        systemd.bus.fire_properties_changed(
            "a.service", UNIT_IFACE, {**change, "ActiveEnterTimestamp": 9}
        )
        assert len(recorder.snapshots) == 3
        # A Job change alone is a new key.
        systemd.bus.fire_properties_changed(
            "a.service", UNIT_IFACE, {"Job": [5, "/org/freedesktop/systemd1/job/5"]}
        )
        assert len(recorder.snapshots) == 4
        assert recorder.snapshots[-1].job_pending
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_reloading_rereads(systemd: FakeSystemd, recorder: Recorder) -> None:
    observer = make_observer(systemd, recorder, (*UNITS, "c.service"))
    task = await started(observer, recorder)
    try:
        assert recorder.snapshots[2].load_state == "not-found"
        bus = systemd.bus
        reads = len(bus.reads)

        systemd.units["b.service"]["After"] = ["c.service"]  # never signalled
        systemd.units["c.service"] = unit_props(After=[])
        bus.fire_reloading(True)
        bus.fire_reloading(False)
        await recorder.wait_for(lambda: len(recorder.snapshots) == 5)

        assert len(bus.reads) > reads
        assert {s.name for s in recorder.snapshots[3:]} == {"b.service", "c.service"}
        assert observer.after_graph()["b.service"] == ("c.service",)
        assert observer.after_graph()["c.service"] == ()
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_disconnect_reconnects_and_resnapshots(
    systemd: FakeSystemd, recorder: Recorder
) -> None:
    task = await started(make_observer(systemd, recorder), recorder)
    try:
        first = systemd.bus
        systemd.units["a.service"]["NRestarts"] = 1  # changed during the gap
        first.drop()
        await recorder.wait_for(lambda: recorder.connection == [True, False, True])

        assert len(systemd.buses) == 2
        assert systemd.bus.subscribed
        assert [s.name for s in recorder.snapshots] == [*UNITS, *UNITS]
        assert recorder.snapshots[2].n_restarts == 1
        # Signals arrive on the new connection.
        systemd.bus.fire_properties_changed("a.service", UNIT_IFACE, {"MainPID": 7})
        assert recorder.snapshots[-1].main_pid == 7
    finally:
        await stop(task)
    assert systemd.bus._user_disconnect  # the observer closed its bus on cancel


@pytest.mark.asyncio
async def test_connect_failure_backs_off_and_retries(recorder: Recorder) -> None:
    systemd = FakeSystemd({"a.service": unit_props()})
    attempts = 0

    async def flaky_connect() -> FakeBus:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionRefusedError
        return await systemd.connect()

    observer = SystemdObserver(
        ["a.service"],
        flaky_connect,
        recorder.on_snapshot,
        recorder.on_connection,
        backoff=(0,),
    )
    task = await started(observer, recorder)
    try:
        assert attempts == 3
        assert recorder.connection == [True]
    finally:
        await stop(task)
