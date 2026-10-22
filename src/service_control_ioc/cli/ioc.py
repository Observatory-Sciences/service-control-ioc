"""``service-control-ioc``: observe the stack, serve it as PVs, log its events."""

import argparse
import asyncio
import signal
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Final

from service_control_ioc.cli.common import load_spec
from service_control_ioc.core import Core
from service_control_ioc.logs import LogFormat, log_event, setup_logging

_STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)


class _Stop(Exception):
    """SIGTERM or SIGINT arrived."""


async def serve(
    core: Core, *, ioc: Callable[[Core], Awaitable[None]] | None = None
) -> None:
    """Run ``core`` (and ``ioc(core)``, if given) until SIGTERM or SIGINT.

    Both run in one TaskGroup, so a stop signal cancels and awaits them, and
    a failure in ``ioc`` stops the core (an ``ExceptionGroup`` propagates).
    ``SystemExit`` from ``Core.run`` (a failed unit preflight) propagates.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in _STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)

    async def stopper() -> None:
        await stop.wait()
        raise _Stop

    async def extra(run_ioc: Callable[[Core], Awaitable[None]]) -> None:
        await run_ioc(core)  # tasks need a coroutine, the signature takes any awaitable

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(stopper())
            if ioc is not None:
                tg.create_task(extra(ioc))
            await core.run()
    except* _Stop:
        pass  # graceful: the TaskGroup has cancelled and awaited the core
    finally:
        for sig in _STOP_SIGNALS:
            loop.remove_signal_handler(sig)


def run(path: Path, log_format: LogFormat, log_level: str, *, no_ioc: bool) -> None:
    spec = load_spec(path)
    setup_logging(log_format, log_level)
    core = Core(spec)
    core.subscribe(log_event)
    if no_ioc:
        asyncio.run(serve(core))
        return
    # Imported here so that the other tools never load EPICS.
    from service_control_ioc.ioc.app import serve_ioc

    asyncio.run(serve(core, ioc=serve_ioc))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="service-control-ioc",
        description="Observe the stack, serve it as PVs and log its events.",
    )
    parser.add_argument("config", type=Path)
    parser.add_argument("--log-format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--log-level",
        default="INFO",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument(
        "--no-ioc",
        action="store_true",
        help="observe and log only; do not serve PVs",
    )
    args = parser.parse_args(argv)
    run(args.config, args.log_format, args.log_level, no_ioc=args.no_ioc)
