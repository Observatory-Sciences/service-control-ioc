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

from dbus_fast import BusType
from dbus_fast.aio import MessageBus, ProxyInterface
from dbus_fast.introspection import Node

from qd_service_control.model.types import UnitSnapshot
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
DEFAULT_BACKOFF: Final = (0.5, 1.0, 2.0, 5.0)

PropertiesCallback = Callable[[str, dict[str, object]], None]


class SystemdBus(Protocol):
    """The systemd D-Bus calls the observer needs, on one connection."""

    async def subscribe(self) -> None: ...

    async def list_units_by_names(
        self, names: Sequence[str]
    ) -> list[tuple[str, str, str, str]]:
        """One ``(name, load, active, sub)`` row per name, ``not-found`` if missing."""
        ...

    async def get_unit_properties(self, unit: str) -> dict[str, object]:
        """Unit (and, for a service, Service) properties, variants unpacked."""
        ...

    def on_properties_changed(self, unit: str, cb: PropertiesCallback) -> None:
        """Call ``cb(interface, changed)`` on each PropertiesChanged of ``unit``."""
        ...

    def on_reloading(self, cb: Callable[[bool], None]) -> None: ...

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
    )


def _dedup_key(props: Mapping[str, object]) -> tuple[object, ...]:
    return (
        props.get("ActiveState"),
        props.get("SubState"),
        _job(props),
        _invocation_id(props),
        props.get("NRestarts"),
        props.get("MainPID"),
    )


class SystemdObserver:
    """Supervises a D-Bus connection and reports unit snapshots.

    Each connection: register handlers, ``Subscribe``, read every unit, emit
    every snapshot, then ``on_connection(True)``. Afterwards a snapshot is
    emitted when a Unit-interface PropertiesChanged changes the dedup key,
    and after ``Reloading(False)`` for each unit whose snapshot changed. On
    a disconnect: ``on_connection(False)``, back off, start again.
    """

    def __init__(
        self,
        units: Sequence[str],
        connect: Callable[[], Awaitable[SystemdBus]],
        on_snapshot: Callable[[UnitSnapshot], None],
        on_connection: Callable[[bool], None],
        backoff: Sequence[float] = DEFAULT_BACKOFF,
    ) -> None:
        self._units = tuple(units)
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
        # Survives reconnects.
        self._after: dict[str, tuple[str, ...]] = {}

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
                    bus.disconnect()
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
        reload_done = asyncio.Event()
        for unit in self._units:
            bus.on_properties_changed(unit, partial(self._properties_changed, unit))
        bus.on_reloading(lambda active: None if active else reload_done.set())
        await bus.subscribe()

        await self._read_all(bus)
        for unit in self._units:
            self._emit(unit)
        self._live = True
        self._connected = True
        self._on_connection(True)

        async with asyncio.TaskGroup() as tg:
            reread = tg.create_task(self._reread_on_reload(bus, reload_done))
            try:
                await bus.wait_for_disconnect()
            except EOFError:
                log.warning("system bus connection lost")
            reread.cancel()
        return True

    async def _read_all(self, bus: SystemdBus) -> None:
        rows = await bus.list_units_by_names(self._units)
        for name, load, active, sub in rows:
            if load == "not-found":
                props: dict[str, object] = {
                    "LoadState": load,
                    "ActiveState": active,
                    "SubState": sub,
                }
            else:
                props = await bus.get_unit_properties(name)
            self._props[name] = props
            self._after[name] = _after(props)

    async def _reread_on_reload(
        self, bus: SystemdBus, reload_done: asyncio.Event
    ) -> None:
        while True:
            await reload_done.wait()
            reload_done.clear()
            await self._read_all(bus)
            for unit in self._units:
                if snapshot_from_props(unit, self._props[unit]) != self._emitted.get(
                    unit
                ):
                    self._emit(unit)

    def _properties_changed(
        self, unit: str, interface: str, changed: Mapping[str, object]
    ) -> None:
        props = self._props.get(unit)
        if props is None:  # not read yet on this connection; the read wins
            return
        props.update(changed)
        if "After" in changed:
            self._after[unit] = _after(props)
        if (
            self._live
            and interface == UNIT_IFACE
            and _dedup_key(props) != self._keys.get(unit)
        ):
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

    async def wait_for_disconnect(self) -> None:
        await self._bus.wait_for_disconnect()

    def disconnect(self) -> None:
        self._bus.disconnect()


async def connect_system_bus() -> SystemdBus:
    """Connect to the system bus (inside the running loop)."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    return DbusFastBus(bus)
