"""Test doubles shared by the observer and core tests.

``FakeSystemd``/``FakeBus`` stand in for systemd on D-Bus; the scdemo helpers
build socket tables for the test stack's config; ``FakeClock`` and
``ScriptedScan`` let a test drive the core's time and socket scans.
"""

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from qd_service_control.config.load import load_config
from qd_service_control.config.spec import StackSpec
from qd_service_control.observe.procfs import (
    TCP_ESTABLISHED,
    TCP_LISTEN,
    UDP_ESTABLISHED,
    UDP_UNCONN,
    OwnedSock,
    Sock,
    SocketTable,
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

    A unit missing from ``units`` is reported ``not-found``. While ``refuse``
    is set, ``connect`` fails like an unreachable bus.
    """

    def __init__(self, units: Mapping[str, dict[str, object]]) -> None:
        self.units = {name: dict(props) for name, props in units.items()}
        self.buses: list[FakeBus] = []
        self.connected = asyncio.Event()
        self.refuse = False
        self.attempts = 0
        self.reads_open = asyncio.Event()  # clear it to hold GetAll reads
        self.reads_open.set()

    async def connect(self) -> "FakeBus":
        self.attempts += 1
        if self.refuse:
            raise ConnectionRefusedError("system bus unreachable")
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
        self._job_new_handlers: list[Callable[[int, str, str], None]] = []
        self._job_removed_handlers: list[Callable[[int, str, str, str], None]] = []
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
        await self.systemd.reads_open.wait()
        self.reads.append(unit)
        return dict(self.systemd.units[unit])

    def on_properties_changed(
        self, unit: str, cb: Callable[[str, dict[str, object]], None]
    ) -> None:
        self._prop_handlers.setdefault(unit, []).append(cb)

    def on_reloading(self, cb: Callable[[bool], None]) -> None:
        self._reload_handlers.append(cb)

    def on_job_new(self, cb: Callable[[int, str, str], None]) -> None:
        self._job_new_handlers.append(cb)

    def on_job_removed(self, cb: Callable[[int, str, str, str], None]) -> None:
        self._job_removed_handlers.append(cb)

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

    def fire_job_new(self, job_id: int, job_path: str, unit: str) -> None:
        """Manager ``JobNew``; the unit's ``Job`` changes, without a
        PropertiesChanged (systemd may send that one much later)."""
        if unit in self.systemd.units:
            self.systemd.units[unit]["Job"] = (job_id, job_path)
        for cb in self._job_new_handlers:
            cb(job_id, job_path, unit)

    def fire_job_removed(
        self, job_id: int, job_path: str, unit: str, result: str = "done"
    ) -> None:
        props = self.systemd.units.get(unit)
        if props is not None and props["Job"] in (
            (job_id, job_path),
            [job_id, job_path],
        ):
            props["Job"] = IDLE_JOB
        for cb in self._job_removed_handlers:
            cb(job_id, job_path, unit, result)

    def fire_reloading(self, active: bool) -> None:
        for cb in self._reload_handlers:
            cb(active)

    def drop(self) -> None:
        """The bus connection drops (wait_for_disconnect raises EOFError)."""
        self._dropped.set()


# -- the scdemo test stack -----------------------------------------------------

SCDEMO = Path(__file__).resolve().parents[1] / "teststack" / "config" / "scdemo.yaml"


def scdemo_spec(poll_interval: float = 0.001) -> StackSpec:
    """The scdemo spec, polled fast so tests need no real waits."""
    spec = load_config(SCDEMO)
    return replace(spec, defaults=replace(spec.defaults, poll_interval=poll_interval))


def _sockets(spec: StackSpec, eid: str, i: int) -> list[OwnedSock]:
    """The sockets that make one listener or edge present."""
    units = {sid: s.unit for sid, s in spec.services.items()}
    if eid in spec.listeners:
        ls = spec.listeners[eid]
        state = TCP_LISTEN if ls.proto == "tcp" else UDP_UNCONN
        sock = Sock(ls.proto, ls.address or "0.0.0.0", ls.port, "0.0.0.0", 0, state, i)
        return [OwnedSock(sock, 1, units[ls.owner])]
    edge = spec.edges[eid]
    server = spec.listeners[edge.server]
    if edge.sender == "remote":
        return []
    ip = server.address or "127.0.0.1"
    if edge.proto == "tcp":
        sock = Sock("tcp", "127.0.0.1", 40000 + i, ip, server.port, TCP_ESTABLISHED, i)
    elif edge.sender == "connected":
        sock = Sock("udp", "127.0.0.1", 41000 + i, ip, server.port, UDP_ESTABLISHED, i)
    else:
        sock = Sock("udp", "0.0.0.0", 45000 + i, "0.0.0.0", 0, UDP_UNCONN, i)
    return [OwnedSock(sock, 1, units[edge.client])]


def scdemo_table(spec: StackSpec, up: Iterable[str]) -> SocketTable:
    """A socket table in which the ``up`` listeners and edges are present."""
    entries: list[OwnedSock] = []
    for i, eid in enumerate(up):
        entries.extend(_sockets(spec, eid, i + 1))
    return SocketTable(tuple(entries))


def everything(spec: StackSpec) -> tuple[str, ...]:
    """Every observable listener and every edge."""
    listeners = tuple(lid for lid, ls in spec.listeners.items() if not ls.on_host)
    return listeners + tuple(spec.edges)


class FakeClock:
    """A wall clock the test sets."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ScriptedScan:
    """A ``scan`` callable returning whatever table (or error) the test set.

    It runs in a worker thread, so it reports each finished scan to the loop
    with ``call_soon_threadsafe``.
    """

    def __init__(self, table: SocketTable | None = None) -> None:
        self.table = table if table is not None else SocketTable(())
        self.error: OSError | None = None
        self.on_scan: Callable[[], None] | None = None  # runs in the worker thread
        self.count = 0
        self._loop = asyncio.get_running_loop()
        self._changed = asyncio.Event()

    def __call__(self) -> SocketTable:
        table, error = self.table, self.error
        if self.on_scan is not None:
            self.on_scan()
        self._loop.call_soon_threadsafe(self._scanned)
        if error is not None:
            raise error
        return table

    def _scanned(self) -> None:
        self.count += 1
        self._changed.set()

    async def polls(self, n: int = 3) -> None:
        """Wait until the core has applied at least one poll of the current script.

        A scan in flight may predate the change, the next one sees it, and the
        one after that starts only once its update was applied: hence 3.
        """
        target = self.count + n
        async with asyncio.timeout(2):
            while self.count < target:
                self._changed.clear()
                await self._changed.wait()


class Waiter:
    """Collects items and lets a test wait for a condition on them."""

    def __init__(self) -> None:
        self._changed = asyncio.Event()

    def poke(self) -> None:
        self._changed.set()

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(2):
            while not predicate():
                self._changed.clear()
                await self._changed.wait()
