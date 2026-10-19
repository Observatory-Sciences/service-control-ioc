"""Test harness: run an scdemo service as a subprocess and watch its events."""

import asyncio
import contextlib
import errno
import json
import os
import random
import signal
import socket
import sys
from collections.abc import AsyncGenerator, Callable, Mapping
from types import TracebackType
from typing import Final, Self

import zmq
import zmq.asyncio

from scdemo.logs import parse_logfmt
from scdemo.protocol import ports

EXIT_TIMEOUT: Final = 10
PORT_SPAN: Final = 211  # offsets +0 .. +210
# Bases stay in 10000-32000 with room for the +1000 fault ports: below Linux's
# ephemeral range (32768-60999), so a client socket's local port can never
# collide with a test listener.
PORT_BASE_MIN: Final = 10000
PORT_BASE_MAX: Final = 32000 - 1000 - PORT_SPAN
_NOT_FREE: Final = (errno.EADDRINUSE, errno.EACCES)


def range_is_free(base: int) -> bool:
    """True if every port in base..base+PORT_SPAN-1 is free (TCP and UDP) on 127.0.0.1.

    Binds one socket at a time so the fd limit never matters. Only "address in
    use" and "permission denied" mean not free; any other OSError propagates.
    """
    for port in range(base, base + PORT_SPAN):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(socket.AF_INET, kind) as s:
                try:
                    s.bind(("127.0.0.1", port))
                except OSError as err:
                    if err.errno in _NOT_FREE:
                        return False
                    raise
    return True


def find_port_base(
    attempts: int = 50, is_free: Callable[[int], bool] = range_is_free
) -> int:
    """A random base in PORT_BASE_MIN..PORT_BASE_MAX whose port range is free.

    RuntimeError if none is found in ``attempts`` tries.
    """
    for _ in range(attempts):
        base = random.randint(PORT_BASE_MIN, PORT_BASE_MAX)
        if is_free(base):
            return base
    raise RuntimeError(f"no free port range found in {attempts} attempts")


async def zmq_req(
    endpoint: str,
    msg: dict[str, object],
    timeout: float = 5,  # noqa: ASYNC109 - the plan fixes this signature
) -> dict[str, object]:
    """Send ``msg`` as JSON on a fresh REQ socket and return the JSON reply.

    The socket is closed (linger 0) afterwards, so a timed-out request leaves
    nothing behind. Raise TimeoutError if no reply arrives in ``timeout`` s.
    """
    ctx = zmq.asyncio.Context()
    ctx.linger = 0
    sock = ctx.socket(zmq.REQ)
    try:
        sock.connect(endpoint)
        async with asyncio.timeout(timeout):
            await sock.send(json.dumps(msg).encode())
            raw = await sock.recv()
    finally:
        sock.close(linger=0)
        ctx.term()
    reply = json.loads(raw)
    if not isinstance(reply, dict):
        raise TypeError(f"reply is not a JSON object: {raw!r}")
    return reply


class ServiceProc:
    """Run ``python -m scdemo.<svc>`` and collect its logfmt stdout events.

    ``module`` overrides the module run (for harness tests). Extra keyword
    arguments are environment variables for the child and win over the
    defaults. Inherited ``SCDEMO_*`` variables are dropped so a developer's
    shell cannot leak into a test.
    """

    def __init__(
        self, svc: str, port_base: int, *, module: str | None = None, **env: str
    ) -> None:
        self.svc = svc
        self.module = module or f"scdemo.{svc}"
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("SCDEMO_")},
            "SCDEMO_PORT_BASE": str(port_base),
            "SCDEMO_HW_HOST": "127.0.0.1",
            **env,
        }
        self.events: list[dict[str, str]] = []
        self.stderr_lines: list[str] = []
        self.other_stdout: list[str] = []  # stdout lines that are not events
        self._proc: asyncio.subprocess.Process | None = None
        self._changed = asyncio.Condition()
        self._readers: asyncio.TaskGroup | None = None
        self._eof = (asyncio.Event(), asyncio.Event())

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.returncode

    async def __aenter__(self) -> Self:
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            self.module,
            env=self.env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        self._readers = asyncio.TaskGroup()
        await self._readers.__aenter__()
        self._readers.create_task(
            self._read(self._proc.stdout, self._on_event, self._eof[0])
        )
        self._readers.create_task(
            self._read(self._proc.stderr, self._on_stderr, self._eof[1])
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._proc is not None
        assert self._readers is not None
        try:
            if self._proc.returncode is None:
                self._proc.kill()
            await self._proc.wait()
        finally:
            # The pipes hit EOF once the process is dead, so the readers end.
            await self._readers.__aexit__(exc_type, exc, tb)

    async def _read(
        self,
        stream: asyncio.StreamReader,
        handle: Callable[[str], None],
        eof: asyncio.Event,
    ) -> None:
        try:
            async for raw in stream:
                handle(raw.decode(errors="replace").rstrip("\n"))
                async with self._changed:
                    self._changed.notify_all()
        finally:
            eof.set()

    def _on_event(self, line: str) -> None:
        event = parse_logfmt(line)
        if "event" in event:
            self.events.append(event)
        else:
            self.other_stdout.append(line)

    def _on_stderr(self, line: str) -> None:
        self.stderr_lines.append(line)

    def _find(
        self, event: str, match: dict[str, str], start: int
    ) -> dict[str, str] | None:
        for candidate in self.events[start:]:
            if candidate["event"] == event and all(
                candidate.get(k) == v for k, v in match.items()
            ):
                return candidate
        return None

    async def wait_event(
        self,
        event: str,
        timeout: float = 10,  # noqa: ASYNC109 - the plan fixes this signature
        *,
        after: dict[str, str] | None = None,
        **match: str,
    ) -> dict[str, str]:
        """Return the first event named ``event`` whose fields equal ``match``.

        Events already received count. With ``after`` (an event dict this
        process returned earlier, compared by identity), only events logged
        after it count. Raise TimeoutError if none arrives.
        """
        start = 0
        if after is not None:
            index = next((i for i, e in enumerate(self.events) if e is after), None)
            if index is None:
                raise ValueError(f"{self.svc}: after= is not one of its events")
            start = index + 1
        found: dict[str, str] | None = None

        def seen() -> bool:
            nonlocal found
            found = self._find(event, match, start)
            return found is not None

        try:
            async with asyncio.timeout(timeout):
                async with self._changed:
                    await self._changed.wait_for(seen)
        except TimeoutError as err:
            raise TimeoutError(
                f"{self.svc}: no event {event!r} {match} within {timeout}s;"
                f" saw {[e['event'] for e in self.events]},"
                f" stderr {self.stderr_lines[-3:]}"
            ) from err
        assert found is not None
        return found

    async def wait_exit(self) -> int:
        """Wait (at most 10 s) for exit and for both pipes to reach EOF.

        Every line the process wrote is in ``events`` when this returns.
        """
        assert self._proc is not None
        async with asyncio.timeout(EXIT_TIMEOUT):
            code = await self._proc.wait()
            for eof in self._eof:
                await eof.wait()
        return code

    async def terminate(self) -> int:
        """Send SIGTERM and return the exit code."""
        assert self._proc is not None
        self._proc.send_signal(signal.SIGTERM)
        return await self.wait_exit()


STACK_ORDER: Final = ("sink1", "sink2", "hwsim", "relay", "controller")
_STACK_SERVICES: Final = {
    "sink1": ("sink", {"SCDEMO_INSTANCE": "1"}),
    "sink2": ("sink", {"SCDEMO_INSTANCE": "2"}),
    "hwsim": ("hwsim", {}),
    "relay": ("relay", {}),
    "controller": ("controller", {}),
}
READY_TIMEOUT: Final = 15
RELEASE_TIMEOUT: Final = 5
RELEASE_POLL: Final = 0.05


def _tcp_port_bindable(port: int) -> bool:
    """True if ``port`` binds with SO_REUSEADDR, as the services bind it."""
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))  # where the services bind
        except OSError as err:
            if err.errno in _NOT_FREE:
                return False
            raise
    return True


class Stack:
    """The running services of one local stack, by name (see ``STACK_ORDER``)."""

    def __init__(self, port_base: int, env: Mapping[str, Mapping[str, str]]) -> None:
        unknown = sorted(set(env) - set(STACK_ORDER))
        if unknown:
            raise TypeError(f"stack(): unknown service {', '.join(unknown)}")
        self.port_base = port_base
        self.env = env
        self.procs: dict[str, ServiceProc] = {}

    @property
    def sink1(self) -> ServiceProc:
        return self.procs["sink1"]

    @property
    def sink2(self) -> ServiceProc:
        return self.procs["sink2"]

    @property
    def hwsim(self) -> ServiceProc:
        return self.procs["hwsim"]

    @property
    def relay(self) -> ServiceProc:
        return self.procs["relay"]

    @property
    def controller(self) -> ServiceProc:
        return self.procs["controller"]

    async def start(self, name: str) -> ServiceProc:
        """Start ``name``; wait for ``ready`` except for the controller.

        The controller is only ready once configured, which some tests
        prevent on purpose, so callers wait for it themselves.
        """
        svc, defaults = _STACK_SERVICES[name]
        env = {**defaults, **self.env.get(name, {})}
        proc = ServiceProc(svc, self.port_base, **env)
        await proc.__aenter__()
        self.procs[name] = proc
        if name != "controller":
            await proc.wait_event("ready", timeout=READY_TIMEOUT)
        return proc

    async def stop(self, name: str) -> None:
        """SIGTERM ``name`` (if still running) and wait for it to exit."""
        proc = self.procs.pop(name)
        try:
            if proc.returncode is None:
                await proc.terminate()
        finally:
            await proc.__aexit__(None, None, None)

    async def restart(self, name: str) -> ServiceProc:
        """Stop ``name``, wait until its ports are free again, start it afresh."""
        await self.stop(name)
        if name == "relay":
            p = ports(self.port_base)
            tcp = (p.rep, p.router, p.pub, p.http)
            # The kernel gives no event when a dead process's sockets are
            # released, so poll the binds (normally free at the first try).
            async with asyncio.timeout(RELEASE_TIMEOUT):
                while True:
                    if all(_tcp_port_bindable(port) for port in tcp):
                        break
                    await asyncio.sleep(RELEASE_POLL)
        return await self.start(name)


@contextlib.asynccontextmanager
async def stack(
    port_base: int, **per_service_env: Mapping[str, str]
) -> AsyncGenerator[Stack]:
    """Run sink1, sink2, hwsim, relay and controller, started in that order.

    ``per_service_env`` gives extra environment per service, e.g.
    ``stack(base, relay={"SCDEMO_FAULT_IGNORE_CONFIG": "1"})``. On exit every
    service still running is killed.
    """
    s = Stack(port_base, per_service_env)
    try:
        for name in STACK_ORDER:
            await s.start(name)
        yield s
    finally:
        async with contextlib.AsyncExitStack() as procs:
            for proc in s.procs.values():
                procs.push_async_exit(proc)
