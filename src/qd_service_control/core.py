"""Wire the observers to the evaluator on asyncio and publish its events.

``Core.run`` is one TaskGroup of the systemd observer (unit snapshots, event
driven), the poll loop (sockets, CPU and file probes every
``poll_interval``) and the journal follower. Each new input goes through
``Evaluator.update``; the resulting events go to every subscriber.

``Core.control`` runs one start/stop/restart job on a configured unit or
group and waits for its ``JobRemoved``; the job's events go to the same
subscribers.

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
from qd_service_control.model.types import (
    Event,
    JobOutcome,
    StackView,
    UnitSnapshot,
    Verb,
)
from qd_service_control.observe.cpu import CpuMeter
from qd_service_control.observe.files import probe_file
from qd_service_control.observe.journal import JournalLine, JournalTail
from qd_service_control.observe.procfs import SocketTable, read_cpu_usec, scan_sockets
from qd_service_control.observe.systemd import (
    DEFAULT_BACKOFF,
    SystemdBus,
    SystemdJobError,
    SystemdObserver,
    SystemdReplyError,
    connect_system_bus,
)

log = logging.getLogger(__name__)

CGROUP_ROOT: Final = Path("/sys/fs/cgroup")
FATAL_LOAD_STATES: Final = frozenset({"not-found", "error", "bad-setting"})
WARN_LOAD_STATES: Final = frozenset({"masked"})
STACK_TARGET: Final = "stack"
CALL_TIMEOUT: Final = 25.0  # s, for systemd (and polkit) to answer a job call
FINISHED_TTL: Final = 60.0  # s, to keep a JobRemoved nobody was waiting for yet
POLKIT_ERRORS: Final = frozenset(
    {
        "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired",
        "org.freedesktop.DBus.Error.AccessDenied",
    }
)


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
            on_job_removed=self._on_job_removed,
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
        self._bus_tried = False  # a connection attempt has failed
        self._primed = False  # the first update has run
        self._scanned = False  # a poll has finished, successfully or not
        self._sockets: SocketTable | None = None
        self._scan_time: float | None = None  # of self._sockets, if it is a table
        self._scan_failing = False
        self._cpu: dict[str, float | None] = dict.fromkeys(service_units)
        self._files: dict[str, bool] = {}
        self._preflight_done = False
        self._preflight_failed = asyncio.Event()
        self._unit_update: asyncio.Handle | None = None
        self._last_now = float("-inf")  # the evaluator's time never goes back
        # Jobs: waiters by job path (a None result means the bus dropped), and
        # results that came before their waiter (JobRemoved can arrive before
        # the job call returns), with the monotonic time they arrived.
        self._job_waiters: dict[str, asyncio.Future[str | None]] = {}
        self._finished: dict[str, tuple[str, float]] = {}

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
        finally:
            # Nothing is evaluated or published once run() has returned.
            if self._unit_update is not None:
                self._unit_update.cancel()
                self._unit_update = None
            self._fail_job_waiters()  # the bus went with the observer

    async def _exit_on_preflight_failure(self) -> None:
        await self._preflight_failed.wait()
        raise _PreflightFailed

    async def _connect_and_preflight(self) -> SystemdBus:
        """The observer's ``connect``: the first connection also runs the preflight."""
        try:
            bus = await self._connect()
        except Exception:
            # Only a failure lets the first update go ahead without the bus; a
            # success waits for on_connection(True), after the units are read.
            self._bus_tried = True
            self._update(poll_time=None)  # show the units as unknown meanwhile
            raise
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
        """Check every unit's load state (spec Section 9.1); False if any is fatal.

        An alias is fatal too: systemd answers with the canonical name, and its
        signals and jobs name that unit, so the alias would never be observed.
        """
        rows = await bus.list_units_by_names(self._units)
        if len(rows) != len(self._units):
            raise SystemdReplyError(
                f"ListUnitsByNames returned {len(rows)} rows for {len(self._units)} units"
            )
        ok = True
        for unit, (name, load, _active, _sub) in zip(self._units, rows, strict=True):
            if load in FATAL_LOAD_STATES:
                log.error("unit %s: load state %s", unit, load)
                ok = False
            elif name != unit:
                log.error(
                    "unit %s: is an alias of %s, configure the canonical name",
                    unit,
                    name,
                )
                ok = False
            elif load in WARN_LOAD_STATES:
                log.warning("unit %s: load state %s", unit, load)
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
        self._scan_time = now if poll.sockets is not None else None
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

    # -- control -------------------------------------------------------------

    def _control_unit(self, target: str) -> str:
        """The unit behind a control target: a service, a group or the stack."""
        spec = self._spec
        if target in spec.services:
            return spec.services[target].unit
        if target in spec.groups:
            return spec.groups[target].unit
        if target == STACK_TARGET:
            if spec.stack_group is None:
                raise ValueError("no stack_group configured for the stack target")
            return spec.groups[spec.stack_group].unit
        raise ValueError(f"unknown control target {target!r}")

    def _event_now(self) -> float:
        now = max(self._clock(), self._last_now)
        self._last_now = now
        return now

    async def control(self, target: str, verb: Verb) -> JobOutcome:
        """Run ``verb`` on ``target`` as one systemd job and return how it ended.

        ``target`` is a service id, a group id or ``"stack"`` (the
        ``stack_group``); anything else raises ``ValueError``. Every failure
        to run the job is an outcome, not an exception. The queued and
        finished events (and a ``job_failed`` finding) go to the subscribers.
        """
        unit = self._control_unit(target)
        loop = asyncio.get_running_loop()
        started = loop.time()
        result, detail = await self._run_job(target, unit, verb)
        outcome = JobOutcome(target, unit, verb, result, detail, loop.time() - started)
        for event in self._evaluator.job_finished(outcome, self._event_now()):
            self._publish(event)
        return outcome

    async def _run_job(self, target: str, unit: str, verb: Verb) -> tuple[str, str]:
        """Call systemd and wait for the job's result: ``(result, detail)``."""
        bus = self._observer.bus
        if bus is None:
            return "not-connected", "no D-Bus connection"
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                path = await bus.unit_job(verb, unit)
        except SystemdJobError as err:
            if err.name in POLKIT_ERRORS:
                return "error", "not authorised (polkit)"
            return "error", f"{err.name}: {err.text}"
        except TimeoutError:
            return "error", "no reply from systemd"
        except (OSError, EOFError):  # dbus-fast: the connection dropped mid-call
            return "error", "bus connection lost"
        for event in self._evaluator.job_queued(target, unit, verb, self._event_now()):
            self._publish(event)
        finished = self._finished.pop(path, None)
        if finished is not None:
            return finished[0], ""
        if self._observer.bus is not bus:
            # The connection dropped during the call: its JobRemoved is lost.
            return "error", "bus connection lost"
        waiter: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._job_waiters[path] = waiter
        try:
            async with asyncio.timeout(self._spec.defaults.job_timeout):
                result = await waiter
        except TimeoutError:
            return "no-result", ""
        finally:
            self._job_waiters.pop(path, None)
        if result is None:
            return "error", "bus connection lost"
        return result, ""

    def _on_job_removed(self, job_path: str, result: str) -> None:
        """The observer's feed of every ``JobRemoved``."""
        waiter = self._job_waiters.pop(job_path, None)
        if waiter is not None:
            if not waiter.done():
                waiter.set_result(result)
            return
        now = asyncio.get_running_loop().time()
        self._finished = {
            path: entry
            for path, entry in self._finished.items()
            if now - entry[1] <= FINISHED_TTL
        }
        self._finished[job_path] = (result, now)

    def _fail_job_waiters(self) -> None:
        waiters, self._job_waiters = self._job_waiters, {}
        for waiter in waiters.values():
            if not waiter.done():
                waiter.set_result(None)

    # -- observer callbacks --------------------------------------------------

    def _on_snapshot(self, snapshot: UnitSnapshot) -> None:
        self._snapshots[snapshot.name] = snapshot
        if self._bus_connected:  # the initial ones wait for on_connection(True)
            self._unit_changed()

    def _on_connection(self, up: bool) -> None:
        self._bus_connected = up
        if not up:
            self._fail_job_waiters()  # their JobRemoved went with the connection
            # Also after a session that failed before going live: the units
            # are unknown, and the first update must not wait for the bus.
            self._bus_tried = True
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
        if self._unit_update is None:
            loop = asyncio.get_running_loop()
            self._unit_update = loop.call_soon(self._apply_unit_change)

    def _apply_unit_change(self) -> None:
        self._unit_update = None
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
        if not self._primed:
            if not (self._scanned and (self._bus_connected or self._bus_tried)):
                return
            self._primed = True  # from now on every change is evaluated
            if poll_time is None:
                # The bus came last: the scan this update waited for has not
                # been evaluated yet, so it is still fresh. Without it the
                # gate tracker has no first poll, and skips the check of a
                # start seen before the next one.
                poll_time = self._scan_time
        now = max(self._clock() if now is None else now, self._last_now)
        self._last_now = now
        inputs = Inputs(
            now=now,
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
