"""Observe systemd units over D-Bus and turn them into ``UnitSnapshot``s.

``SystemdObserver`` talks to a small ``SystemdBus`` protocol, so tests use an
in-memory fake. ``DbusFastBus`` is the real adapter over dbus-fast on the
system bus.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import partial
from typing import Any, Final, Protocol

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus, ProxyInterface
from dbus_fast.introspection import Node

from qd_service_control.model.types import UnitSnapshot, Verb
from qd_service_control.observe.systemd_xml import MANAGER_XML, PROPERTIES_XML

log = logging.getLogger(__name__)

SYSTEMD: Final = "org.freedesktop.systemd1"
MANAGER_PATH: Final = "/org/freedesktop/systemd1"
MANAGER_IFACE: Final = "org.freedesktop.systemd1.Manager"
UNIT_IFACE: Final = "org.freedesktop.systemd1.Unit"
SERVICE_IFACE: Final = "org.freedesktop.systemd1.Service"
PROPERTIES_IFACE: Final = "org.freedesktop.DBus.Properties"
UNIT_PATH_PREFIX: Final = "/org/freedesktop/systemd1/unit/"
IDLE_JOB: Final = (0, "/")
JOB_MODE: Final = "replace"
_JOB_METHODS: Final[dict[Verb, str]] = {
    "start": "call_start_unit",
    "stop": "call_stop_unit",
    "restart": "call_restart_unit",
}
DEFAULT_BACKOFF: Final = (0.5, 1.0, 2.0, 5.0)
MAX_REREADS: Final = 5  # reads of a unit that changes during each read
JOB_IFACE: Final = "org.freedesktop.systemd1.Job"
JOB_TYPE: Final = "JobType"  # set on a unit's props from its job object

PropertiesCallback = Callable[[str, dict[str, object]], None]


class SystemdReplyError(Exception):
    """systemd answered in a shape the observer cannot use."""


class SystemdJobError(Exception):
    """systemd refused a job call: the D-Bus error name and its message."""

    def __init__(self, name: str, text: str) -> None:
        super().__init__(f"{name}: {text}")
        self.name = name
        self.text = text


JobNewCallback = Callable[[int, str, str], None]  # id, job path, unit
JobRemovedCallback = Callable[[int, str, str, str], None]  # ... and result


class SystemdBus(Protocol):
    """The systemd D-Bus calls the observer needs, on one connection."""

    async def subscribe(self) -> None: ...

    async def unit_job(self, verb: Verb, unit: str) -> str:
        """``StartUnit``/``StopUnit``/``RestartUnit(unit, "replace")``: the job path.

        Raises ``SystemdJobError`` if systemd (or polkit) refuses the call.
        """
        ...

    async def list_units_by_names(
        self, names: Sequence[str]
    ) -> list[tuple[str, str, str, str]]:
        """One ``(name, load, active, sub)`` row per name, ``not-found`` if missing."""
        ...

    async def get_unit_properties(self, unit: str) -> dict[str, object]:
        """Unit (and, for a service, Service) properties, variants unpacked."""
        ...

    async def job_type(self, job_path: str) -> str:
        """The job's ``JobType`` (``start``, ``stop``, ...); "" if it is gone."""
        ...

    def on_properties_changed(self, unit: str, cb: PropertiesCallback) -> None:
        """Call ``cb(interface, changed)`` on each PropertiesChanged of ``unit``."""
        ...

    def on_reloading(self, cb: Callable[[bool], None]) -> None: ...

    def on_job_new(self, cb: JobNewCallback) -> None:
        """Call ``cb(id, job_path, unit)`` on each Manager ``JobNew``."""
        ...

    def on_job_removed(self, cb: JobRemovedCallback) -> None:
        """Call ``cb(id, job_path, unit, result)`` on each Manager ``JobRemoved``."""
        ...

    async def wait_for_disconnect(self) -> None:
        """Return or raise ``EOFError`` when the connection drops."""
        ...

    def disconnect(self) -> None: ...


def unit_path(name: str) -> str:
    """The unit's object path (systemd's bus_label_escape, not systemd-escape)."""
    return UNIT_PATH_PREFIX + "".join(
        c
        if c.isascii() and (c.isalpha() or (c.isdigit() and i > 0))
        else f"_{ord(c):02x}"
        for i, c in enumerate(name)
    )


def _str(props: Mapping[str, object], key: str) -> str:
    value = props.get(key, "")
    return value if isinstance(value, str) else str(value)


def _int(props: Mapping[str, object], key: str) -> int:
    value = props.get(key, 0)
    return value if isinstance(value, int) else 0


def _job(props: Mapping[str, object]) -> tuple[object, ...]:
    """``Job`` as a tuple; unpacked D-Bus structs arrive as lists."""
    value = props.get("Job", IDLE_JOB)
    return tuple(value) if isinstance(value, list | tuple) else IDLE_JOB


def _invocation_id(props: Mapping[str, object]) -> str:
    value = props.get("InvocationID", b"")
    if isinstance(value, bytes | bytearray):
        return value.hex()
    if isinstance(value, list):  # an ``ay`` that was not turned into bytes
        return bytes(v for v in value if isinstance(v, int)).hex()
    return ""


def _after(props: Mapping[str, object]) -> tuple[str, ...]:
    value = props.get("After", ())
    if not isinstance(value, list | tuple):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def snapshot_from_props(name: str, props: Mapping[str, object]) -> UnitSnapshot:
    """Map unpacked Unit/Service properties to a snapshot; missing ones default."""
    return UnitSnapshot(
        name=name,
        load_state=_str(props, "LoadState"),
        active_state=_str(props, "ActiveState"),
        sub_state=_str(props, "SubState"),
        result=_str(props, "Result"),
        job_pending=_job(props) != IDLE_JOB,
        n_restarts=_int(props, "NRestarts"),
        main_pid=_int(props, "MainPID"),
        exec_main_code=_int(props, "ExecMainCode"),
        exec_main_status=_int(props, "ExecMainStatus"),
        exec_main_start_us=_int(props, "ExecMainStartTimestamp"),
        active_enter_us=_int(props, "ActiveEnterTimestamp"),
        control_group=_str(props, "ControlGroup"),
        invocation_id=_invocation_id(props),
        after=_after(props),
        job_type=_str(props, JOB_TYPE) if _job(props) != IDLE_JOB else "",
    )


def _dedup_key(props: Mapping[str, object]) -> tuple[object, ...]:
    return (
        props.get("ActiveState"),
        props.get("SubState"),
        _job(props),
        _invocation_id(props),
        props.get("NRestarts"),
        props.get("MainPID"),
        props.get(JOB_TYPE),
    )


def _cgroup_key(props: Mapping[str, object]) -> tuple[object, ...]:
    """What a unit's ``ControlGroup`` is re-read for when it changes."""
    return (_invocation_id(props), props.get("MainPID"))


class SystemdObserver:
    """Supervises a D-Bus connection and reports unit snapshots.

    Each connection: register handlers, ``Subscribe``, read every unit, emit
    every snapshot, then ``on_connection(True)``. Afterwards a snapshot is
    emitted when a Unit-interface PropertiesChanged changes the dedup key,
    and after ``Reloading(False)`` for each unit whose snapshot changed. On
    a disconnect: ``on_connection(False)``, back off, start again.

    ``JobNew`` and ``JobRemoved`` also update a unit's ``Job``. systemd sends
    them at once, but may hold back a queued unit's PropertiesChanged until
    the jobs it waits for are done: without them, a unit queued behind
    another would look stopped and idle for that long.

    Every ``JobRemoved``, for any unit, also goes to ``on_job_removed`` as
    ``(job_path, result)``, so a caller can match the jobs it queued.

    systemd never signals ``ControlGroup``, so a unit read while stopped
    would keep an empty one. When a unit's ``InvocationID`` or ``MainPID``
    changes, its properties are read again and a changed ``ControlGroup`` is
    emitted in a new snapshot.

    Neither ``JobNew`` nor the unit's ``Job`` says what kind of job it is, so
    each new job's ``JobType`` is read from the job object and emitted with
    the unit's next snapshot ("" until then, or if the job is already gone).
    """

    def __init__(
        self,
        units: Sequence[str],
        connect: Callable[[], Awaitable[SystemdBus]],
        on_snapshot: Callable[[UnitSnapshot], None],
        on_connection: Callable[[bool], None],
        backoff: Sequence[float] = DEFAULT_BACKOFF,
        on_job_removed: Callable[[str, str], None] | None = None,
    ) -> None:
        self._units = tuple(units)
        self._on_job_removed = on_job_removed
        self._bus: SystemdBus | None = None  # the live session's connection
        self._connect = connect
        self._on_snapshot = on_snapshot
        self._on_connection = on_connection
        self._backoff = tuple(backoff) or DEFAULT_BACKOFF
        self._connected = False
        # Per connection: merged properties, and the last emitted key/snapshot.
        self._props: dict[str, dict[str, object]] = {}
        self._keys: dict[str, tuple[object, ...]] = {}
        self._emitted: dict[str, UnitSnapshot] = {}
        self._live = False
        # unit -> signals seen while its read is in flight (only during reads)
        self._in_flight: dict[str, list[dict[str, object]]] = {}
        # unit -> (InvocationID, MainPID) its ControlGroup was last read for
        self._cgroup_keys: dict[str, tuple[object, ...]] = {}
        self._cgroup_due: set[str] = set()  # units whose ControlGroup to re-read
        self._cgroup_wake = asyncio.Event()  # also wakes the job-type reads
        self._job_paths: dict[str, str] = {}  # unit -> job path seen last
        self._job_due: dict[str, str] = {}  # unit -> job path to read the type of
        # Survives reconnects.
        self._after: dict[str, tuple[str, ...]] = {}

    @property
    def bus(self) -> SystemdBus | None:
        """The live session's bus (for job calls), else None."""
        return self._bus

    def after_graph(self) -> Mapping[str, tuple[str, ...]]:
        """Unit -> its ``After`` list, as last read, for every observed unit."""
        return dict(self._after)

    async def run(self) -> None:
        """Observe forever, reconnecting with backoff; returns only by cancel."""
        failures = 0
        while True:
            try:
                bus = await self._connect()
            except Exception:  # supervisor: any connect failure means retry
                log.exception("cannot connect to the system bus")
            else:
                try:
                    if await self._session(bus):
                        failures = 0
                except Exception:  # supervisor: a failed session means reconnect
                    log.exception("systemd D-Bus session failed")
                finally:
                    self._live = False
                    self._bus = None
                    bus.disconnect()
                if not self._connected:
                    # It never went live: still tell the owner, so the units
                    # read as unknown instead of never being evaluated.
                    self._on_connection(False)
            if self._connected:
                self._connected = False
                self._on_connection(False)
            await asyncio.sleep(self._backoff[min(failures, len(self._backoff) - 1)])
            failures += 1

    async def _session(self, bus: SystemdBus) -> bool:
        """Run one connection until it drops; True if it got as far as live."""
        self._props.clear()
        self._keys.clear()
        self._emitted.clear()
        self._cgroup_keys.clear()
        self._cgroup_due.clear()
        self._job_paths.clear()
        self._job_due.clear()
        reload_done = asyncio.Event()
        for unit in self._units:
            bus.on_properties_changed(unit, partial(self._properties_changed, unit))
        bus.on_reloading(lambda active: None if active else reload_done.set())
        bus.on_job_new(self._job_new)
        bus.on_job_removed(self._job_removed)
        await bus.subscribe()

        await self._read_all(bus)
        for unit in self._units:
            self._cgroup_keys[unit] = _cgroup_key(self._props[unit])
            self._track_job(unit)
            self._emit(unit)
        self._live = True
        self._bus = bus
        self._connected = True
        self._on_connection(True)

        async with asyncio.TaskGroup() as tg:
            reread = tg.create_task(self._reread_on_reload(bus, reload_done))
            cgroups = tg.create_task(self._read_details(bus))
            try:
                await bus.wait_for_disconnect()
            except EOFError:
                log.warning("system bus connection lost")
            reread.cancel()
            cgroups.cancel()
        return True

    async def _read_all(self, bus: SystemdBus) -> None:
        """Read every unit. Rows are paired with the configured names.

        A signal for a unit (PropertiesChanged, JobNew, JobRemoved) that
        arrives while its read is in flight may be newer than the reply, so
        that unit is read again; a unit that keeps changing gets the signals
        seen during its last read applied on top of the reply.
        """
        self._in_flight = {unit: [] for unit in self._units}
        try:
            rows = await bus.list_units_by_names(self._units)
            if len(rows) != len(self._units):
                raise SystemdReplyError(
                    f"ListUnitsByNames returned {len(rows)} rows"
                    f" for {len(self._units)} units"
                )
            for unit, (_name, load, active, sub) in zip(self._units, rows, strict=True):
                if load == "not-found":
                    props: dict[str, object] = {
                        "LoadState": load,
                        "ActiveState": active,
                        "SubState": sub,
                    }
                else:
                    props = await self._read_unit(bus, unit)
                self._props[unit] = props
                self._after[unit] = _after(props)
        finally:
            self._in_flight = {}

    async def _read_unit(self, bus: SystemdBus, unit: str) -> dict[str, object]:
        for _ in range(MAX_REREADS):
            self._in_flight[unit] = []
            props = await bus.get_unit_properties(unit)
            if not self._in_flight[unit]:
                return props
        for changed in self._in_flight[unit]:  # still busy: newest signals win
            props.update(changed)
        return props

    async def _reread_on_reload(
        self, bus: SystemdBus, reload_done: asyncio.Event
    ) -> None:
        while True:
            await reload_done.wait()
            reload_done.clear()
            await self._read_all(bus)
            for unit in self._units:
                self._job_paths.pop(unit, None)  # the read replaced its props
                self._track_job(unit)
                if snapshot_from_props(unit, self._props[unit]) != self._emitted.get(
                    unit
                ):
                    self._emit(unit)

    async def _read_details(self, bus: SystemdBus) -> None:
        """Read what systemd never signals: new jobs' types, and ``ControlGroup``
        again for units with a new invocation or MainPID."""
        while True:
            await self._cgroup_wake.wait()
            self._cgroup_wake.clear()
            while self._job_due:
                unit, path = self._job_due.popitem()
                job_type = await bus.job_type(path)
                props = self._props.get(unit)
                if props is not None and _job(props)[1:] == (path,):
                    props[JOB_TYPE] = job_type
                    self._emit_if_changed(unit)
            while self._cgroup_due:
                unit = self._cgroup_due.pop()
                fresh = await bus.get_unit_properties(unit)
                props = self._props.get(unit)
                if props is None:
                    continue
                control_group = fresh.get("ControlGroup", "")
                if control_group != props.get("ControlGroup", ""):
                    props["ControlGroup"] = control_group
                    self._emit(unit)

    def _track_job(self, unit: str) -> None:
        """On a new job for ``unit``: forget the old type, queue a type read."""
        props = self._props.get(unit)
        if props is None:
            return
        job = _job(props)
        path = str(job[1]) if len(job) > 1 else "/"
        if self._job_paths.get(unit) == path:
            return
        self._job_paths[unit] = path
        props.pop(JOB_TYPE, None)
        self._job_due.pop(unit, None)
        if job != IDLE_JOB:
            self._job_due[unit] = path
            self._cgroup_wake.set()

    def _note_in_flight(self, unit: str, changed: Mapping[str, object]) -> None:
        """Remember a signal that arrived while ``unit``'s read was in flight."""
        pending = self._in_flight.get(unit)
        if pending is not None:
            pending.append(dict(changed))

    def _properties_changed(
        self, unit: str, interface: str, changed: Mapping[str, object]
    ) -> None:
        self._note_in_flight(unit, changed)
        props = self._props.get(unit)
        if props is None:  # not read yet on this connection; the read wins
            return
        props.update(changed)
        if "After" in changed:
            self._after[unit] = _after(props)
        if self._live:
            self._track_job(unit)
        cgroup_key = _cgroup_key(props)
        if self._live and cgroup_key != self._cgroup_keys.get(unit):
            self._cgroup_keys[unit] = cgroup_key
            self._cgroup_due.add(unit)
            self._cgroup_wake.set()
        if (
            self._live
            and interface == UNIT_IFACE
            and _dedup_key(props) != self._keys.get(unit)
        ):
            self._emit(unit)

    def _job_new(self, job_id: int, job_path: str, unit: str) -> None:
        self._note_in_flight(unit, {"Job": (job_id, job_path)})
        props = self._props.get(unit)
        if props is None:  # not ours, or not read yet (the read has the job)
            return
        props["Job"] = (job_id, job_path)
        if self._live:
            self._track_job(unit)
        self._emit_if_changed(unit)

    def _job_removed(self, job_id: int, job_path: str, unit: str, result: str) -> None:
        del job_id  # only the path identifies a job
        if self._on_job_removed is not None:
            self._on_job_removed(job_path, result)
        self._note_in_flight(unit, {})  # re-read: the reply may predate it
        props = self._props.get(unit)
        if props is None or _job(props)[1:] != (job_path,):
            return  # not ours, or the unit already has another job (or none)
        props["Job"] = IDLE_JOB
        if self._live:
            self._track_job(unit)
        self._emit_if_changed(unit)

    def _emit_if_changed(self, unit: str) -> None:
        if self._live and _dedup_key(self._props[unit]) != self._keys.get(unit):
            self._emit(unit)

    def _emit(self, unit: str) -> None:
        props = self._props[unit]
        snapshot = snapshot_from_props(unit, props)
        self._keys[unit] = _dedup_key(props)
        self._emitted[unit] = snapshot
        self._on_snapshot(snapshot)


def _member(iface: ProxyInterface, name: str) -> Callable[..., Any]:
    """A ``call_*``/``on_*`` member that dbus-fast generates from the XML."""
    return getattr(iface, name)


class DbusFastBus:
    """``SystemdBus`` over a dbus-fast ``MessageBus``."""

    _MANAGER_NODE: Final = Node.parse(MANAGER_XML)
    _PROPERTIES_NODE: Final = Node.parse(PROPERTIES_XML)

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._manager = bus.get_proxy_object(
            SYSTEMD, MANAGER_PATH, self._MANAGER_NODE
        ).get_interface(MANAGER_IFACE)
        self._properties: dict[str, ProxyInterface] = {}

    def _unit_properties(self, unit: str) -> ProxyInterface:
        iface = self._properties.get(unit)
        if iface is None:
            iface = self._bus.get_proxy_object(
                SYSTEMD, unit_path(unit), self._PROPERTIES_NODE
            ).get_interface(PROPERTIES_IFACE)
            self._properties[unit] = iface
        return iface

    async def subscribe(self) -> None:
        await _member(self._manager, "call_subscribe")()

    async def unit_job(self, verb: Verb, unit: str) -> str:
        call = _member(self._manager, _JOB_METHODS[verb])
        try:
            path = await call(unit, JOB_MODE)
        except DBusError as err:
            raise SystemdJobError(err.type, err.text) from err
        if not isinstance(path, str):
            raise SystemdReplyError(f"{verb} {unit}: job path {path!r}")
        return path

    async def list_units_by_names(
        self, names: Sequence[str]
    ) -> list[tuple[str, str, str, str]]:
        rows = await _member(self._manager, "call_list_units_by_names")(list(names))
        # (name, description, load, active, sub, followed, path, job id/type/path)
        return [(row[0], row[2], row[3], row[4]) for row in rows]

    async def get_unit_properties(self, unit: str) -> dict[str, object]:
        get_all = _member(self._unit_properties(unit), "call_get_all")
        props: dict[str, object] = dict(await get_all(UNIT_IFACE, unpack_variants=True))
        if unit.endswith(".service"):
            props.update(await get_all(SERVICE_IFACE, unpack_variants=True))
        return props

    async def job_type(self, job_path: str) -> str:
        props = self._bus.get_proxy_object(
            SYSTEMD, job_path, self._PROPERTIES_NODE
        ).get_interface(PROPERTIES_IFACE)
        try:
            value = await _member(props, "call_get")(
                JOB_IFACE, JOB_TYPE, unpack_variants=True
            )
        except DBusError:
            return ""  # the job finished before we asked (UnknownObject)
        return value if isinstance(value, str) else ""

    def on_properties_changed(self, unit: str, cb: PropertiesCallback) -> None:
        def handler(
            interface: str, changed: dict[str, object], invalidated: list[str]
        ) -> None:
            del invalidated  # systemd sends values, not invalidations, for these
            cb(interface, changed)

        on = _member(self._unit_properties(unit), "on_properties_changed")
        on(handler, unpack_variants=True)

    def on_reloading(self, cb: Callable[[bool], None]) -> None:
        _member(self._manager, "on_reloading")(cb)

    def on_job_new(self, cb: JobNewCallback) -> None:
        _member(self._manager, "on_job_new")(cb)

    def on_job_removed(self, cb: JobRemovedCallback) -> None:
        _member(self._manager, "on_job_removed")(cb)

    async def wait_for_disconnect(self) -> None:
        await self._bus.wait_for_disconnect()

    def disconnect(self) -> None:
        self._bus.disconnect()


async def connect_system_bus() -> SystemdBus:
    """Connect to the system bus (inside the running loop)."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    return DbusFastBus(bus)
