"""Run ``ioc_child.py`` per test and talk to it over loopback Channel Access.

The CA variables are set here, at import, so they are in ``os.environ`` before
the first aioca call (libca reads them once per process) and every child
inherits the same loopback-only settings on one free port.
"""

import asyncio
import contextlib
import json
import os
import secrets
import socket
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path

import aioca
import pytest_asyncio
from aioca.types import Datatype

from service_control_ioc.config.spec import StackSpec
from service_control_ioc.pvnames import PvNames, pv_names

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/, for fakes

from fakes import scdemo_spec

CHILD = Path(__file__).with_name("ioc_child.py")
START_TIMEOUT = 30.0  # s; the first softioc import in a fresh process is slow
STOP_TIMEOUT = 10.0  # s, after SIGTERM, before SIGKILL
WAIT = 5.0  # s, for a control call or a monitored value


def _free_port() -> int:
    """A loopback port free for both TCP and UDP (CA serves on both)."""
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                try:
                    udp.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port


_PORT = str(_free_port())
CA_ENV = {
    "EPICS_CA_ADDR_LIST": "127.0.0.1",
    "EPICS_CA_AUTO_ADDR_LIST": "NO",
    "EPICS_CAS_INTF_ADDR_LIST": "127.0.0.1",
    "EPICS_CA_SERVER_PORT": _PORT,
    "EPICS_CAS_SERVER_PORT": _PORT,
}
os.environ.update(CA_ENV)


class IocChild:
    """A running ``ioc_child.py``: send it commands, read its control calls."""

    def __init__(self, proc: asyncio.subprocess.Process, spec: StackSpec) -> None:
        self.proc = proc
        self.spec = spec
        self.names: PvNames = pv_names(spec)
        self.controls: asyncio.Queue[list[str]] = asyncio.Queue()
        self.ready = asyncio.Event()
        self.closed = False  # stdout reached EOF: the child has gone

    def pv(self, base: str, field: str) -> str:
        return self.names.pv(base, field)

    async def send(self, *commands: dict[str, object]) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write("".join(json.dumps(c) + "\n" for c in commands).encode())
        await stdin.drain()

    async def next_control(self) -> list[str]:
        async with asyncio.timeout(WAIT):
            return await self.controls.get()

    async def wait_for(
        self,
        pv: str,
        predicate: Callable[[object], bool],
        *,
        datatype: Datatype = None,
    ) -> object:
        """The first monitored value of ``pv`` for which ``predicate`` holds."""
        found: asyncio.Future[object] = asyncio.get_running_loop().create_future()

        def update(value: object) -> None:
            if not found.done() and predicate(value):
                found.set_result(value)

        sub = aioca.camonitor(pv, update, datatype=datatype)
        try:
            async with asyncio.timeout(WAIT):
                return await found
        finally:
            sub.close()

    async def read_stdout(self) -> None:
        stdout = self.proc.stdout
        assert stdout is not None
        while line := await stdout.readline():
            text = line.decode().strip()
            if text == "ready":
                self.ready.set()
            else:
                self.controls.put_nowait(json.loads(text)["control"])
        self.closed = True
        self.ready.set()  # wake a start that is still waiting: it fails at once


@pytest_asyncio.fixture
async def ioc() -> AsyncIterator[IocChild]:
    prefix = f"SCIOCT{secrets.token_hex(4).upper()}:SC"
    spec = replace(scdemo_spec(), prefix=prefix)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(CHILD),
        prefix,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env={**os.environ, **CA_ENV, "PYTHONUNBUFFERED": "1"},
    )
    child = IocChild(proc, spec)
    reader = asyncio.create_task(child.read_stdout())
    early_exit: int | None = None
    try:
        async with asyncio.timeout(START_TIMEOUT):
            await child.ready.wait()
        if child.closed:
            raise RuntimeError("ioc_child.py exited before serving (see its stderr)")
        yield child
    finally:
        aioca.purge_channel_caches()
        early_exit = proc.returncode  # set: the child died during the test
        if proc.returncode is None:
            proc.terminate()
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        if proc.stdin is not None:
            proc.stdin.close()
    assert early_exit is None, f"ioc_child.py died during the test: rc {early_exit}"
    assert proc.returncode == 0, (
        f"ioc_child.py did not stop on SIGTERM: rc {proc.returncode}"
    )
