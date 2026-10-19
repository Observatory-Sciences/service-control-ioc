"""Wire the observers to the evaluator on asyncio and publish its events.

``Core.run`` is one TaskGroup of the systemd observer (unit snapshots, event
driven), the poll loop (sockets, CPU and file probes every
``poll_interval``) and the journal follower. Each new input goes through
``Evaluator.update``; the resulting events go to every subscriber.

The first evaluator update waits until there is a socket scan and the bus
has either connected or failed once, so a tool started after the stack goes
straight to ``ready`` instead of passing through ``starting``.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from qd_service_control.config.spec import StackSpec
from qd_service_control.model.deps import DependencyGraph
from qd_service_control.model.evaluator import Evaluator, Inputs
from qd_service_control.model.types import Event, StackView, UnitSnapshot
from qd_service_control.observe.cpu import CpuMeter
from qd_service_control.observe.files import probe_file
from qd_service_control.observe.journal import JournalLine, JournalTail
from qd_service_control.observe.procfs import SocketTable, read_cpu_usec, scan_sockets
from qd_service_control.observe.systemd import (
    DEFAULT_BACKOFF,
    SystemdBus,
    SystemdObserver,
    connect_system_bus,
)

log = logging.getLogger(__name__)

CGROUP_ROOT: Final = Path("/sys/fs/cgroup")
FATAL_LOAD_STATES: Final = frozenset({"not-found", "error", "bad-setting"})
WARN_LOAD_STATES: Final = frozenset({"masked"})


def read_cgroup_cpu(control_group: str) -> int | None:
    """``usage_usec`` of a unit's cgroup under ``/sys/fs/cgroup``."""
    return read_cpu_usec(CGROUP_ROOT, control_group)


class _PreflightFailed(Exception):
    """A configured unit has a load state the tool cannot work with."""


@dataclass(frozen=True, slots=True)
class _Poll:
    """What one poll read in the worker thread."""

    sockets: SocketTable | None
    error: OSError | None
    usage: dict[str, int | None]  # unit -> usage_usec
    files: dict[str, bool]  # FileSpec id -> probe result


def _dedup(names: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


class Core:
    """The EPICS-free core: observe, evaluate, publish."""

    def __init__(
        self,
        spec: StackSpec,
        *,
        connect: Callable[[], Awaitable[SystemdBus]] = connect_system_bus,
        scan: Callable[[], SocketTable] = scan_sockets,
        read_cpu: Callable[[str], int | None] = read_cgroup_cpu,
        probe: Callable[[str, bool], bool] = probe_file,
        journalctl: Sequence[str] = ("journalctl",),
        clock: Callable[[], float] = time.time,
        bus_backoff: Sequence[float] = DEFAULT_BACKOFF,
    ) -> None:
        self._spec = spec
        self._connect = connect
        self._scan = scan
        self._read_cpu = read_cpu
        self._probe = probe
        self._clock = clock
        service_units = _dedup([s.unit for s in spec.services.values()])
        self._group_units = frozenset(g.unit for g in spec.groups.values()) - set(
            service_units
        )
        self._units = _dedup([*service_units, *sorted(self._group_units)])
        self._service_units = service_units

        self._evaluator = Evaluator(spec, self._graph({}), started_at=clock())
        self._observer = SystemdObserver(
            self._units,
            self._connect_and_preflight,
            self._on_snapshot,
            self._on_connection,
            backoff=bus_backoff,
        )
        defaults = spec.defaults
        self._journal = JournalTail(
            service_units,
            self._on_journal_line,
            lines=defaults.journal_lines,
            max_bytes=defaults.journal_bytes,
            journalctl=journalctl,
        )
        self._meters = {
            u: CpuMeter(defaults.cpu_threshold, defaults.cpu_window)
            for u in service_units
        }

        self._subscribers: list[Callable[[Event], None]] = []
        self._journal_subscribers: list[Callable[[JournalLine], None]] = []
        # Latest observations.
        self._snapshots: dict[str, UnitSnapshot] = {}
        self._bus_connected = False
        self._bus_tried = False  # a connection attempt has finished either way
        self._scanned = False  # a poll has finished, successfully or not
        self._sockets: SocketTable | None = None
        self._scan_failing = False
        self._cpu: dict[str, float | None] = dict.fromkeys(service_units)
        self._files: dict[str, bool] = {}
        self._preflight_done = False
        self._preflight_failed = asyncio.Event()
        self._unit_update_pending = False

    @property
    def spec(self) -> StackSpec:
        return self._spec

    # -- subscribers ---------------------------------------------------------

    def subscribe(self, cb: Callable[[Event], None]) -> Callable[[], None]:
        """Call ``cb`` with every event; returns a function that unsubscribes."""
        self._subscribers.append(cb)

        def unsubscribe() -> None:
            if cb in self._subscribers:
                self._subscribers.remove(cb)

        return unsubscribe

    def subscribe_journal(
        self, cb: Callable[[JournalLine], None]
    ) -> Callable[[], None]:
        """Call ``cb`` with every journal line; returns a function that unsubscribes."""
        self._journal_subscribers.append(cb)

        def unsubscribe() -> None:
            if cb in self._journal_subscribers:
                self._journal_subscribers.remove(cb)

        return unsubscribe

    def view(self) -> StackView:
        return self._evaluator.view()

    def journal(self, service_id: str) -> tuple[str, ...]:
        """The service's recent journal lines, newest last (KeyError if unknown)."""
        return self._journal.lines(self._spec.services[service_id].unit)

    # -- running -------------------------------------------------------------

    async def run(self) -> None:
        """Observe and evaluate until cancelled.

        Raises ``SystemExit(2)`` if the unit preflight finds a unit that is
        not found, failed to load or has a bad setting.
        """
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._observer.run())
                tg.create_task(self._poll_loop())
                tg.create_task(self._journal.run())
                tg.create_task(self._exit_on_preflight_failure())
        except* _PreflightFailed:
            # Already logged per unit; the group adds nothing for the caller.
            raise SystemExit(2) from None

    async def _exit_on_preflight_failure(self) -> None:
        await self._preflight_failed.wait()
        raise _PreflightFailed

    async def _connect_and_preflight(self) -> SystemdBus:
        """The observer's ``connect``: the first connection also runs the preflight."""
        try:
            bus = await self._connect()
        except Exception:
            self._bus_tried = True
            self._update(poll_time=None)  # show the units as unknown meanwhile
            raise
        self._bus_tried = True
        if self._preflight_done:
            return bus
        try:
            ok = await self._preflight(bus)
        except BaseException:
            bus.disconnect()
            raise
        if not ok:
            bus.disconnect()
            self._preflight_failed.set()
            # run() now cancels the TaskGroup, and with it this call.
            await asyncio.Event().wait()
        self._preflight_done = True
        return bus

    async def _preflight(self, bus: SystemdBus) -> bool:
        """Check every unit's load state (spec Section 9.1); False if any is fatal."""
        ok = True
        for name, load, _active, _sub in await bus.list_units_by_names(self._units):
            if load in FATAL_LOAD_STATES:
                log.error("unit %s: load state %s", name, load)
                ok = False
            elif load in WARN_LOAD_STATES:
                log.warning("unit %s: load state %s", name, load)
        return ok

    async def _poll_loop(self) -> None:
        interval = self._spec.defaults.poll_interval
        while True:
            await self._poll_once()
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        now = self._clock()  # before the scan: what it sees is no older than this
        groups = {
            unit: snap.control_group
            for unit, snap in self._snapshots.items()
            if unit in self._meters and snap.control_group
        }
        poll = await asyncio.to_thread(self._read_inputs, groups)
        if poll.error is not None:
            if not self._scan_failing:
                log.warning("socket scan failed: %s", poll.error)
            self._scan_failing = True
        elif self._scan_failing:
            log.info("socket scan recovered")
            self._scan_failing = False
        self._sockets = poll.sockets
        for unit, meter in self._meters.items():
            self._cpu[unit] = meter.sample(now, poll.usage.get(unit))
        self._files = poll.files
        self._scanned = True
        self._update(poll_time=now if poll.sockets is not None else None, now=now)

    def _read_inputs(self, control_groups: dict[str, str]) -> _Poll:
        """Runs in a worker thread: the socket scan, CPU counters and file probes."""
        sockets: SocketTable | None = None
        error: OSError | None = None
        try:
            sockets = self._scan()
        except OSError as err:
            error = err
        usage = {unit: self._read_cpu(cg) for unit, cg in control_groups.items()}
        files = {
            fid: self._probe(fs.path, fs.non_empty)
            for fid, fs in self._spec.files.items()
        }
        return _Poll(sockets, error, usage, files)

    # -- observer callbacks --------------------------------------------------

    def _on_snapshot(self, snapshot: UnitSnapshot) -> None:
        self._snapshots[snapshot.name] = snapshot
        if self._bus_connected:  # the initial ones wait for on_connection(True)
            self._unit_changed()

    def _on_connection(self, up: bool) -> None:
        self._bus_connected = up
        if not up:
            self._snapshots.clear()  # re-read in full on the next connection
        self._unit_changed()

    def _unit_changed(self) -> None:
        """Evaluate a unit or connection change in this loop iteration.

        systemd announces one transaction (say, starting the whole stack) as a
        burst of PropertiesChanged signals, one per unit, which dbus-fast
        dispatches back to back. Evaluating once after the burst, instead of
        after each signal, avoids transient findings such as ``unit_stopped``
        for units whose job is queued but not yet announced.
        """
        if not self._unit_update_pending:
            self._unit_update_pending = True
            asyncio.get_running_loop().call_soon(self._apply_unit_change)

    def _apply_unit_change(self) -> None:
        self._unit_update_pending = False
        self._evaluator.set_graph(self._graph(self._observer.after_graph()))
        self._update(poll_time=None)

    def _on_journal_line(self, line: JournalLine) -> None:
        for cb in list(self._journal_subscribers):
            try:
                cb(line)
            except Exception:  # a subscriber's bug must not stop the core
                log.exception("journal subscriber %r failed", cb)

    # -- evaluation ----------------------------------------------------------

    def _graph(self, after: Mapping[str, Iterable[str]]) -> DependencyGraph:
        """Ordering between configured units; groups never count as dependents."""
        return DependencyGraph(after, self._units, exclude=self._group_units)

    def _update(self, poll_time: float | None, now: float | None = None) -> None:
        """Evaluate the latest inputs; ``poll_time`` only for a fresh scan."""
        if not (self._scanned and (self._bus_connected or self._bus_tried)):
            return
        inputs = Inputs(
            now=self._clock() if now is None else now,
            bus_connected=self._bus_connected,
            units=dict(self._snapshots),
            sockets=self._sockets,
            poll_time=poll_time,
            cpu=dict(self._cpu),
            spinning=frozenset(u for u, m in self._meters.items() if m.spinning()),
            files=dict(self._files),
        )
        for event in self._evaluator.update(inputs):
            self._publish(event)

    def _publish(self, event: Event) -> None:
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:  # a subscriber's bug must not stop the core
                log.exception("event subscriber %r failed", cb)
