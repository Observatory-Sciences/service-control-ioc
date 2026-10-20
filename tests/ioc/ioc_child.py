"""One soft IOC over a scripted core, run by the IOC tests as a child process.

Usage: ``ioc_child.py <prefix>``. It serves ``teststack/config/scdemo.yaml``
with ``prefix`` replaced, through ``serve_ioc`` over a ``ScriptedApi``.

stdin takes one JSON command per line:

- ``{"view": "ready" | "initial" | "fault"}``: feed a real ``Evaluator``
  (every unit active with every element up, or ``scdemo-relay.service``
  failed), or start again from a fresh one;
- ``{"event": text}``: publish an event with that text;
- ``{"journal": [unit, text]}``: publish a journal line;
- ``{"control_raises": bool}``: make ``control`` raise instead of returning.

stdout carries only the protocol: ``ready`` once the IOC is serving (when it
subscribes), then ``{"control": [target, verb]}`` per control call. Anything
else written to fd 1 (by EPICS or Python) goes to stderr. The child exits on
SIGTERM or when stdin closes.
"""

import asyncio
import json
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from qd_service_control.config.spec import StackSpec
from qd_service_control.ioc.app import serve_ioc
from qd_service_control.model.deps import DependencyGraph
from qd_service_control.model.evaluator import Evaluator, Inputs
from qd_service_control.model.types import Event, JobOutcome, StackView, Verb
from qd_service_control.observe.journal import JournalLine
from qd_service_control.observe.systemd import snapshot_from_props

# tests/ is not a package: put it on sys.path, then import the shared fakes.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes import everything, scdemo_spec, scdemo_table, unit_props

FAILED_UNIT = "scdemo-relay.service"
FAILED_PROPS = {
    "ActiveState": "failed",
    "SubState": "failed",
    "Result": "exit-code",
    "ExecMainCode": 1,
    "ExecMainStatus": 1,
    "MainPID": 0,
}


class ScriptedApi:
    """A ``CoreApi`` whose view, events and journal follow stdin commands."""

    def __init__(self, spec: StackSpec, out: TextIO) -> None:
        self._spec = spec
        self._out = out
        self._units = [
            *(s.unit for s in spec.services.values()),
            *(g.unit for g in spec.groups.values()),
        ]
        self._evaluator = self._fresh()
        self._subscribers: list[Callable[[Event], None]] = []
        self._journal_subscribers: list[Callable[[JournalLine], None]] = []
        self._lines: dict[str, deque[str]] = {}
        self.control_raises = False

    def _fresh(self) -> Evaluator:
        groups = {g.unit for g in self._spec.groups.values()}
        graph = DependencyGraph({}, self._units, exclude=groups)
        return Evaluator(self._spec, graph, started_at=time.time())

    @property
    def spec(self) -> StackSpec:
        return self._spec

    def view(self) -> StackView:
        return self._evaluator.view()

    def subscribe(self, cb: Callable[[Event], None]) -> Callable[[], None]:
        self._subscribers.append(cb)
        self._say("ready")  # the IOC subscribes once it is serving
        return lambda: self._subscribers.remove(cb)

    def subscribe_journal(
        self, cb: Callable[[JournalLine], None]
    ) -> Callable[[], None]:
        self._journal_subscribers.append(cb)
        return lambda: self._journal_subscribers.remove(cb)

    def journal(self, service_id: str) -> tuple[str, ...]:
        return tuple(self._lines.get(service_id, ()))

    async def control(self, target: str, verb: Verb) -> JobOutcome:
        self._say(json.dumps({"control": [target, verb]}))
        if self.control_raises:
            raise RuntimeError("scripted control failure")
        return JobOutcome(target, "", verb, "done", "", 0.0)

    # -- commands ------------------------------------------------------------

    def handle(self, command: dict[str, object]) -> None:
        if "view" in command:
            self._set_view(str(command["view"]))
        elif "event" in command:
            text = str(command["event"])
            self._publish(Event(time.time(), "stack", "stack", None, None, text, None))
        elif "journal" in command:
            unit, text = command["journal"]  # ty: ignore[not-iterable] - test JSON
            self._journal_line(str(unit), str(text))
        elif "control_raises" in command:
            self.control_raises = bool(command["control_raises"])

    def _set_view(self, name: str) -> None:
        if name == "initial":
            self._evaluator = self._fresh()
            self._publish(Event(time.time(), "stack", "stack", None, None, "", None))
            return
        failed = {FAILED_UNIT} if name == "fault" else set()
        snaps = {}
        for unit in self._units:
            props = unit_props(ControlGroup=f"/system.slice/{unit}")
            if unit in failed:
                props.update(FAILED_PROPS)
            snaps[unit] = snapshot_from_props(unit, props)
        relay = "relay" if failed else None
        up = [e for e in everything(self._spec) if relay is None or relay not in e]
        now = time.time()
        inputs = Inputs(
            now=now,
            bus_connected=True,
            units=snaps,
            sockets=scdemo_table(self._spec, up),
            poll_time=now,
            cpu=dict.fromkeys(self._units, 1.5),
            spinning=frozenset(),
            files={},
        )
        for event in self._evaluator.update(inputs):
            self._publish(event)

    def _journal_line(self, unit: str, text: str) -> None:
        service = self._spec.service_by_unit(unit)
        if service is not None:
            lines = self._lines.setdefault(service.id, deque(maxlen=20))
            lines.append(text)
        line = JournalLine(unit, time.time(), text)
        for cb in list(self._journal_subscribers):
            cb(line)

    def _publish(self, event: Event) -> None:
        for cb in list(self._subscribers):
            cb(event)

    def _say(self, line: str) -> None:
        self._out.write(line + "\n")
        self._out.flush()


async def read_commands(api: ScriptedApi) -> None:
    """Apply stdin commands until stdin closes."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    while line := await reader.readline():
        api.handle(json.loads(line))


async def main(prefix: str, out: TextIO) -> None:
    spec = replace(scdemo_spec(), prefix=prefix)
    api = ScriptedApi(spec, out)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    async with asyncio.TaskGroup() as tg:
        ioc = tg.create_task(serve_ioc(api))
        commands = tg.create_task(read_commands(api))
        commands.add_done_callback(lambda _: stop.set())
        await stop.wait()
        ioc.cancel()
        commands.cancel()


if __name__ == "__main__":
    # The protocol keeps the real stdout; everything else written to fd 1
    # (EPICS's iocInit banner, for one) goes to stderr.
    protocol = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    asyncio.run(main(sys.argv[1], protocol))
