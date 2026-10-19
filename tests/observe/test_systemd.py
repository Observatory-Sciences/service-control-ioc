import asyncio
from collections.abc import Callable, Mapping, Sequence

import pytest

from qd_service_control.model.types import UnitSnapshot
from qd_service_control.observe.systemd import (
    SystemdObserver,
    snapshot_from_props,
    unit_path,
)

UNIT_IFACE = "org.freedesktop.systemd1.Unit"
SERVICE_IFACE = "org.freedesktop.systemd1.Service"
IDLE_JOB = (0, "/")


def unit_props(**overrides: object) -> dict[str, object]:
    """Unit + Service properties of a running unit, as GetAll unpacks them."""
    props: dict[str, object] = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "Result": "success",
        "Job": IDLE_JOB,
        "NRestarts": 0,
        "MainPID": 100,
        "ExecMainCode": 0,
        "ExecMainStatus": 0,
        "ExecMainStartTimestamp": 1_000_000,
        "ActiveEnterTimestamp": 1_500_000,
        "ControlGroup": "/system.slice/a.service",
        "InvocationID": bytes.fromhex("00112233445566778899aabbccddeeff"),
        "After": ["basic.target"],
    }
    props.update(overrides)
    return props


class FakeSystemd:
    """In-memory systemd: unit properties shared by every FakeBus connection.

    A unit missing from ``units`` is reported ``not-found``.
    """

    def __init__(self, units: Mapping[str, dict[str, object]]) -> None:
        self.units = {name: dict(props) for name, props in units.items()}
        self.buses: list[FakeBus] = []
        self.connected = asyncio.Event()

    async def connect(self) -> "FakeBus":
        bus = FakeBus(self)
        self.buses.append(bus)
        self.connected.set()
        return bus

    @property
    def bus(self) -> "FakeBus":
        return self.buses[-1]


class FakeBus:
    """One connection to a FakeSystemd; implements ``SystemdBus``."""

    def __init__(self, systemd: FakeSystemd) -> None:
        self.systemd = systemd
        self.subscribed = False
        self.reads: list[str] = []
        self._prop_handlers: dict[
            str, list[Callable[[str, dict[str, object]], None]]
        ] = {}
        self._reload_handlers: list[Callable[[bool], None]] = []
        self._dropped = asyncio.Event()
        self._user_disconnect = False

    async def subscribe(self) -> None:
        self.subscribed = True

    async def list_units_by_names(
        self, names: Sequence[str]
    ) -> list[tuple[str, str, str, str]]:
        rows = []
        for name in names:
            props = self.systemd.units.get(name)
            if props is None:
                rows.append((name, "not-found", "inactive", "dead"))
            else:
                rows.append(
                    (
                        name,
                        str(props["LoadState"]),
                        str(props["ActiveState"]),
                        str(props["SubState"]),
                    )
                )
        return rows

    async def get_unit_properties(self, unit: str) -> dict[str, object]:
        self.reads.append(unit)
        return dict(self.systemd.units[unit])

    def on_properties_changed(
        self, unit: str, cb: Callable[[str, dict[str, object]], None]
    ) -> None:
        self._prop_handlers.setdefault(unit, []).append(cb)

    def on_reloading(self, cb: Callable[[bool], None]) -> None:
        self._reload_handlers.append(cb)

    async def wait_for_disconnect(self) -> None:
        await self._dropped.wait()
        if not self._user_disconnect:
            raise EOFError

    def disconnect(self) -> None:
        self._user_disconnect = True
        self._dropped.set()

    # Test controls

    def fire_properties_changed(
        self, unit: str, interface: str, changed: Mapping[str, object]
    ) -> None:
        self.systemd.units[unit].update(changed)
        for cb in self._prop_handlers.get(unit, []):
            cb(interface, dict(changed))

    def fire_reloading(self, active: bool) -> None:
        for cb in self._reload_handlers:
            cb(active)

    def drop(self) -> None:
        """The bus connection drops (wait_for_disconnect raises EOFError)."""
        self._dropped.set()


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
