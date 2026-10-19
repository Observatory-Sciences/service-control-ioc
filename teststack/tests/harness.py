"""Test harness: run an scdemo service as a subprocess and watch its events."""

import asyncio
import os
import signal
import sys
from collections.abc import Callable
from types import TracebackType
from typing import Final, Self

from scdemo.logs import parse_logfmt

EXIT_TIMEOUT: Final = 10


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
        self._proc: asyncio.subprocess.Process | None = None
        self._changed = asyncio.Condition()
        self._readers: asyncio.TaskGroup | None = None

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
        self._readers.create_task(self._read(self._proc.stdout, self._on_event))
        self._readers.create_task(self._read(self._proc.stderr, self._on_stderr))
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
    ) -> None:
        async for raw in stream:
            handle(raw.decode(errors="replace").rstrip("\n"))
            async with self._changed:
                self._changed.notify_all()
        async with self._changed:  # EOF: wake waiters so they can see the exit
            self._changed.notify_all()

    def _on_event(self, line: str) -> None:
        event = parse_logfmt(line)
        if "event" in event:
            self.events.append(event)

    def _on_stderr(self, line: str) -> None:
        self.stderr_lines.append(line)

    def _find(self, event: str, match: dict[str, str]) -> dict[str, str] | None:
        for candidate in self.events:
            if candidate["event"] == event and all(
                candidate.get(k) == v for k, v in match.items()
            ):
                return candidate
        return None

    async def wait_event(
        self,
        event: str,
        timeout: float = 10,  # noqa: ASYNC109 - the plan fixes this signature
        **match: str,
    ) -> dict[str, str]:
        """Return the first event named ``event`` whose fields equal ``match``.

        Events already received count. Raise TimeoutError if none arrives.
        """
        found: dict[str, str] | None = None

        def seen() -> bool:
            nonlocal found
            found = self._find(event, match)
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
        """Wait (at most 10 s) for the process to exit; return its exit code."""
        assert self._proc is not None
        async with asyncio.timeout(EXIT_TIMEOUT):
            return await self._proc.wait()

    async def terminate(self) -> int:
        """Send SIGTERM and return the exit code."""
        assert self._proc is not None
        self._proc.send_signal(signal.SIGTERM)
        return await self.wait_exit()
