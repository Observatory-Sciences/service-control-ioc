"""The soft IOC: serve the core's model as PVs and run jobs from control PVs.

This is the only module that imports ``softioc``. ``Ioc.create_records``
builds every record named by ``pv_names`` and sets it from the current view,
so the IOC comes up with defined values (state records INVALID and ``READY``
0 until the core's first update). ``Ioc.run`` then keeps the records current:

- each core event schedules one coalesced refresh (``loop.call_soon``) that
  reads ``api.view()`` and sets only the records whose value or severity
  changed, and appends the event's text to the ``EVENTS`` ring;
- each journal line marks its service's ``LOG`` dirty;
- the flush loop writes dirty ``LOG`` and ``EVENTS`` records at most once per
  ``flush_interval``, so a unit logging hundreds of lines a second costs one
  update per interval.

A non-zero write to ``START``, ``STOP`` or ``RESTART`` awaits
``api.control``, sets ``LASTJOB`` and resets the record to 0. The records
are blocking, so a ``caput -c`` returns when the job has ended.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol

import numpy
from softioc import alarm, asyncio_dispatcher, builder, softioc

from qd_service_control.config.spec import StackSpec
from qd_service_control.core import STACK_TARGET
from qd_service_control.ioc.mapping import (
    ELEMENT_STATES,
    OVERALL_STATES,
    Severity,
    describe,
    element_values,
    fit,
    group_values,
    service_values,
    stack_values,
    text_waveform,
)
from qd_service_control.model.types import (
    Event,
    JobOutcome,
    StackView,
    Verb,
)
from qd_service_control.observe.journal import JournalLine
from qd_service_control.pvnames import PvNames, pv_names

log = logging.getLogger(__name__)

STRING_BYTES: Final = 39  # stringIn: 40 bytes with the NUL
MSG_LENGTH: Final = 512  # FAULT_MSG, FINDING
UNIT_LENGTH: Final = 128
TEXT_LENGTH: Final = 256  # element DESC, DETAIL
# LOG and EVENTS hold the newest whole lines that fit in 999 bytes plus a NUL
# (ruling 19): EDM's multiLineTextUpdate reads at most 1000 bytes and prints
# junk after them unless a NUL comes first (MAX_TEXT_LENGTH in edm diamondlib).
EDM_TEXT_LENGTH: Final = 1000
LOG_LENGTH: Final = EDM_TEXT_LENGTH
EVENTS_LENGTH: Final = EDM_TEXT_LENGTH
EVENTS_RING: Final = 20
VERBS: Final[tuple[tuple[str, Verb], ...]] = (
    ("START", "start"),
    ("STOP", "stop"),
    ("RESTART", "restart"),
)
_SEVERITY_NAMES: Final = ("NO_ALARM", "MINOR", "MAJOR", "INVALID")
# The alarm status that goes with each severity.
_STATUS: Final = {
    0: alarm.NO_ALARM,
    1: alarm.STATE_ALARM,
    2: alarm.STATE_ALARM,
    3: alarm.UDF_ALARM,
}
_READY_NAMES: Final = {"ZNAM": "NOT READY", "ONAM": "READY"}


class CoreApi(Protocol):
    """What the IOC uses of ``Core``."""

    @property
    def spec(self) -> StackSpec: ...
    def view(self) -> StackView: ...
    def subscribe(self, cb: Callable[[Event], None]) -> Callable[[], None]: ...
    def subscribe_journal(
        self, cb: Callable[[JournalLine], None]
    ) -> Callable[[], None]: ...
    def journal(self, service_id: str) -> tuple[str, ...]: ...
    async def control(self, target: str, verb: Verb) -> JobOutcome: ...


class Record(Protocol):
    """A softioc record, as far as this module uses it."""

    def set(self, value: Any, *args: Any, **kwargs: Any) -> None: ...


def _mbb_options(states: tuple[tuple[str, Severity], ...]) -> list[tuple[str, str]]:
    return [(str(state), _SEVERITY_NAMES[sev]) for state, sev in states]


class Ioc:
    """The records of one stack, and the tasks that keep them current."""

    def __init__(self, api: CoreApi, *, flush_interval: float = 0.5) -> None:
        self._api = api
        self._spec = api.spec
        self._names: PvNames = pv_names(self._spec)
        self._flush_interval = flush_interval
        self._records: dict[str, Record] = {}  # by full PV name
        self._last: dict[str, tuple[object, Severity]] = {}  # last value set
        self._services_of_unit: dict[str, list[str]] = {}
        for sid, service in self._spec.services.items():
            self._services_of_unit.setdefault(service.unit, []).append(sid)
        self._refresh: asyncio.Handle | None = None
        self._events: deque[str] = deque(maxlen=EVENTS_RING)
        self._dirty_logs: set[str] = set()  # service ids
        self._events_dirty = False
        self._dirty = asyncio.Event()  # something for the flush loop

    # -- records -------------------------------------------------------------

    def create_records(self) -> None:
        """Build every record and set it from the current view.

        Values set now are applied when ``iocInit`` processes the records, so
        no client ever sees an undefined value.
        """
        names = self._names
        builder.SetDeviceName(names.prefix)
        overall = _mbb_options(OVERALL_STATES)
        element = _mbb_options(ELEMENT_STATES)

        stack = self._add_stack_records(overall)
        if names.stack_controls:
            self._add_controls("", STACK_TARGET)
        for sid, base in names.services.items():
            self._mbb(names.pv(base, "STATE"), element)
            self._long_string(names.pv(base, "UNIT"), UNIT_LENGTH)
            self._string(names.pv(base, "ACTIVE"))
            self._string(names.pv(base, "EXIT"))
            self._bool(names.pv(base, "READY"))
            self._long(names.pv(base, "NRESTARTS"))
            self._add(
                names.pv(base, "CPU"),
                builder.aIn(self._rel(names.pv(base, "CPU")), EGU="%", PREC=1),
            )
            log_name = names.pv(base, "LOG")
            self._add(
                log_name,
                builder.WaveformIn(
                    self._rel(log_name), length=LOG_LENGTH, datatype=numpy.uint8
                ),
            )
            self._long_string(names.pv(base, "FINDING"), MSG_LENGTH)
            self._add_controls(base, sid)
        for eid, base in names.elements.items():
            self._mbb(names.pv(base, "STATE"), element)
            desc = fit(describe(self._spec, eid), TEXT_LENGTH - 1)
            desc_name = names.pv(base, "DESC")
            self._add(
                desc_name,
                builder.longStringIn(
                    self._rel(desc_name), length=TEXT_LENGTH, initial_value=desc
                ),
            )
            self._long_string(names.pv(base, "DETAIL"), TEXT_LENGTH)
            self._string(names.pv(base, "SINCE"))
        for gid, base in names.groups.items():
            self._mbb(names.pv(base, "STATE"), element)
            self._add_controls(base, gid)

        self._publish(force=True)
        self._set_events(stack)
        for sid in names.services:
            self._set_log(sid)

    def _add_stack_records(self, overall: list[tuple[str, str]]) -> str:
        """The stack records; returns the ``EVENTS`` name."""
        names = self._names
        self._bool(names.pv("", "READY"))
        self._bool(names.pv("", "POST_READY"))
        self._mbb(names.pv("", "STATE"), overall)
        self._long(names.pv("", "FAULTS"))
        self._long_string(names.pv("", "FAULT_MSG"), MSG_LENGTH)
        events = names.pv("", "EVENTS")
        self._long_string(events, EVENTS_LENGTH)
        return events

    def _rel(self, name: str) -> str:
        """``name`` without the device prefix, as the builder takes it."""
        return name.removeprefix(f"{self._names.prefix}:")

    def _add(self, name: str, record: Record) -> None:
        self._records[name] = record

    def _bool(self, name: str) -> None:
        self._add(name, builder.boolIn(self._rel(name), **_READY_NAMES))

    def _long(self, name: str) -> None:
        self._add(name, builder.longIn(self._rel(name)))

    def _string(self, name: str) -> None:
        self._add(name, builder.stringIn(self._rel(name)))

    def _long_string(self, name: str, length: int) -> None:
        self._add(name, builder.longStringIn(self._rel(name), length=length))

    def _mbb(self, name: str, options: list[tuple[str, str]]) -> None:
        self._add(name, builder.mbbIn(self._rel(name), *options))

    def _add_controls(self, base: str, target: str) -> None:
        lastjob = self._names.pv(base, "LASTJOB")
        self._string(lastjob)
        for field, verb in VERBS:
            name = self._names.pv(base, field)
            record = builder.longOut(
                self._rel(name),
                initial_value=0,
                blocking=True,
                on_update=self._control_handler(name, lastjob, target, verb),
            )
            self._add(name, record)

    # -- control -------------------------------------------------------------

    def _control_handler(
        self, name: str, lastjob: str, target: str, verb: Verb
    ) -> Callable[[int], Awaitable[None]]:
        async def on_update(value: int) -> None:
            if value == 0:
                return
            try:
                text = await self._run_control(target, verb)
                # Forced: every job posts an update, even with the same text.
                self._put(lastjob, fit(text, STRING_BYTES), force=True)
            finally:
                self._records[name].set(0, process=False)

        return on_update

    async def _run_control(self, target: str, verb: Verb) -> str:
        """``"<verb>: <result>"``; an exception is logged and gives ``error``."""
        try:
            outcome = await self._api.control(target, verb)
        except Exception:  # PV handler boundary: log, report "error", reset the PV
            log.exception("%s of %s failed", verb, target)
            return f"{verb}: error"
        return f"{verb}: {outcome.result}"

    # -- publishing ----------------------------------------------------------

    def _put(
        self, name: str, value: object, severity: Severity = 0, *, force: bool = False
    ) -> None:
        """Set ``name`` if its value or severity changed (or ``force``)."""
        if not force and self._last.get(name) == (value, severity):
            return
        self._last[name] = (value, severity)
        self._records[name].set(value, severity=severity, alarm=_STATUS[severity])

    def _publish(self, *, force: bool = False) -> None:
        """Set every view-driven record from ``api.view()``."""
        view = self._api.view()
        names = self._names
        spec = self._spec

        def put(name: str, value: object, severity: Severity = 0) -> None:
            self._put(name, value, severity, force=force)

        stack = stack_values(view)
        put(names.pv("", "STATE"), stack.state, stack.severity)
        put(names.pv("", "READY"), int(stack.ready))
        put(names.pv("", "POST_READY"), int(stack.post_ready))
        put(names.pv("", "FAULTS"), stack.faults)
        put(names.pv("", "FAULT_MSG"), fit(stack.fault_msg, MSG_LENGTH - 1))
        for sid, base in names.services.items():
            sv = service_values(spec, view, sid)
            put(names.pv(base, "STATE"), sv.state, sv.severity)
            put(names.pv(base, "UNIT"), fit(sv.unit, UNIT_LENGTH - 1))
            put(names.pv(base, "ACTIVE"), fit(sv.active, STRING_BYTES))
            put(names.pv(base, "EXIT"), fit(sv.exit, STRING_BYTES))
            put(names.pv(base, "READY"), int(sv.ready))
            put(names.pv(base, "NRESTARTS"), sv.nrestarts)
            if sv.cpu is None:
                put(names.pv(base, "CPU"), 0.0, 3)
            else:
                put(names.pv(base, "CPU"), sv.cpu)
            put(names.pv(base, "FINDING"), fit(sv.finding, MSG_LENGTH - 1))
        for eid, base in names.elements.items():
            ev = element_values(view, eid)
            put(names.pv(base, "STATE"), ev.state, ev.severity)
            put(names.pv(base, "DETAIL"), fit(ev.detail, TEXT_LENGTH - 1))
            put(names.pv(base, "SINCE"), fit(ev.since, STRING_BYTES))
        for gid, base in names.groups.items():
            gv = group_values(spec, view, gid)
            put(names.pv(base, "STATE"), gv.state, gv.severity)

    def _set_log(self, sid: str) -> None:
        name = self._names.pv(self._names.services[sid], "LOG")
        data = text_waveform(self._api.journal(sid), LOG_LENGTH)
        self._records[name].set(numpy.frombuffer(data, dtype=numpy.uint8))

    def _set_events(self, name: str) -> None:
        data = text_waveform(list(self._events), EVENTS_LENGTH)
        text = data.rstrip(b"\0").removesuffix(b"\n").decode()
        self._records[name].set(text)

    # -- running -------------------------------------------------------------

    async def run(self) -> None:
        """Subscribe, publish everything once, then flush until cancelled."""
        unsubscribe = self._api.subscribe(self._on_event)
        unsubscribe_journal: Callable[[], None] | None = None
        try:
            unsubscribe_journal = self._api.subscribe_journal(self._on_journal)
            self._publish(force=True)
            self._dirty_logs.update(self._names.services)  # lines before subscribe
            self._dirty.set()
            await self._flush_loop()
        finally:
            unsubscribe()
            if unsubscribe_journal is not None:
                unsubscribe_journal()
            if self._refresh is not None:
                self._refresh.cancel()
                self._refresh = None

    def _on_event(self, event: Event) -> None:
        if event.text:
            self._events.append(event.text)
            self._events_dirty = True
            self._dirty.set()
        if self._refresh is None:
            self._refresh = asyncio.get_running_loop().call_soon(self._apply_refresh)

    def _apply_refresh(self) -> None:
        self._refresh = None
        self._publish()

    def _on_journal(self, line: JournalLine) -> None:
        sids = self._services_of_unit.get(line.unit)
        if sids:
            self._dirty_logs.update(sids)
            self._dirty.set()

    async def _flush_loop(self) -> None:
        """Write dirty ``LOG`` and ``EVENTS`` records, at most once per interval."""
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            self._flush()
            await asyncio.sleep(self._flush_interval)

    def _flush(self) -> None:
        dirty, self._dirty_logs = self._dirty_logs, set()
        for sid in sorted(dirty):
            self._set_log(sid)
        if self._events_dirty:
            self._events_dirty = False
            self._set_events(self._names.pv("", "EVENTS"))


async def serve_ioc(api: CoreApi, *, flush_interval: float = 0.5) -> None:
    """Serve the IOC over Channel Access (no PVA) on the running loop until cancelled.

    A process can hold only one IOC: softioc refuses a second ``iocInit``.
    """
    dispatcher = asyncio_dispatcher.AsyncioDispatcher(loop=asyncio.get_running_loop())
    ioc = Ioc(api, flush_interval=flush_interval)
    ioc.create_records()
    builder.LoadDatabase()
    softioc.iocInit(dispatcher, enable_pva=False)
    await ioc.run()
