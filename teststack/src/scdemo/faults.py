"""Fault injection driven by ``SCDEMO_FAULT_*`` and ``SCDEMO_DELAY_*`` variables."""

import asyncio
import math
import os
import sys
import threading
from dataclasses import dataclass

from scdemo import notify
from scdemo.env import ConfigError, Env
from scdemo.logs import log_event

_WRONG_PORT_OFFSET = 1000


def _checked(name: str, value: float | None) -> float | None:
    if value is not None and (not math.isfinite(value) or value < 0):
        raise ConfigError(f"{name}: must be a finite number >= 0: {value!r}")
    return value


def _delay(env: Env, name: str) -> float:
    value = _checked(name, env.float(name, 0.0))
    assert value is not None
    return value


def _timer(env: Env, name: str) -> float | None:
    return _checked(name, env.float_or_none(name))


@dataclass(frozen=True, slots=True)
class Faults:
    delay_bind: float
    delay_connect: float
    delay_ready: float
    crash_after: float | None
    stop_sending_after: float | None
    spin_after: float | None
    no_ready: bool
    ignore_config: bool
    no_bind: frozenset[str] = frozenset()
    wrong_port: frozenset[str] = frozenset()
    no_connect: frozenset[str] = frozenset()
    wrong_connect: frozenset[str] = frozenset()

    @classmethod
    def from_env(
        cls, env: Env, listeners: frozenset[str], edges: frozenset[str]
    ) -> "Faults":
        return cls(
            delay_bind=_delay(env, "SCDEMO_DELAY_BIND"),
            delay_connect=_delay(env, "SCDEMO_DELAY_CONNECT"),
            delay_ready=_delay(env, "SCDEMO_DELAY_READY"),
            crash_after=_timer(env, "SCDEMO_FAULT_CRASH_AFTER"),
            stop_sending_after=_timer(env, "SCDEMO_FAULT_STOP_SENDING_AFTER"),
            spin_after=_timer(env, "SCDEMO_FAULT_SPIN_AFTER"),
            no_ready=env.flag("SCDEMO_FAULT_NO_READY"),
            ignore_config=env.flag("SCDEMO_FAULT_IGNORE_CONFIG"),
            no_bind=env.names("SCDEMO_FAULT_NO_BIND", listeners),
            wrong_port=env.names("SCDEMO_FAULT_WRONG_PORT", listeners),
            no_connect=env.names("SCDEMO_FAULT_NO_CONNECT", edges),
            wrong_connect=env.names("SCDEMO_FAULT_WRONG_CONNECT", edges),
        )

    def bind_port(self, name: str, port: int) -> int | None:
        """The port to bind, or ``None`` to skip the bind."""
        if name in self.no_bind:
            return None
        if name in self.wrong_port:
            return port + _WRONG_PORT_OFFSET
        return port

    def connect_port(self, name: str, port: int) -> int | None:
        """The port to connect to, or ``None`` to never connect."""
        if name in self.no_connect:
            return None
        if name in self.wrong_connect:
            return port + _WRONG_PORT_OFFSET
        return port


def _spin() -> None:
    while True:
        pass


async def _crash_after(seconds: float) -> None:
    await asyncio.sleep(seconds)
    log_event("crash")
    sys.stdout.flush()
    os._exit(3)


async def _spin_after(seconds: float) -> None:
    await asyncio.sleep(seconds)
    log_event("spinning")
    threading.Thread(target=_spin, daemon=True).start()


async def run_fault_timers(faults: Faults) -> None:
    """Run the crash and spin timers. Never returns; cancel it to stop."""
    async with asyncio.TaskGroup() as tg:
        if faults.crash_after is not None:
            tg.create_task(_crash_after(faults.crash_after))
        if faults.spin_after is not None:
            tg.create_task(_spin_after(faults.spin_after))
        await asyncio.Event().wait()


async def announce_ready(faults: Faults) -> None:
    """After ``delay_ready``, log ``ready`` and send ``READY=1`` unless ``no_ready``."""
    await asyncio.sleep(faults.delay_ready)
    if faults.no_ready:
        log_event("ready", skipped=1)
        return
    log_event("ready")
    notify.sd_notify("READY=1")
