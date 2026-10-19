"""Command line entry point."""

import argparse
import asyncio
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from qd_service_control.config.load import ConfigFileError, load_config
from qd_service_control.config.spec import StackSpec
from qd_service_control.core import Core
from qd_service_control.logs import LogFormat, log_event, setup_logging

_EXIT_BAD_CONFIG: Final = 2
_STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)


def _load(path: Path) -> StackSpec:
    """Load the config, or print its issues to stderr and exit 2."""
    try:
        return load_config(path)
    except ConfigFileError as err:
        for issue in err.issues:
            print(f"{err.file}: {issue}", file=sys.stderr)
        sys.exit(_EXIT_BAD_CONFIG)


def _check_config(path: Path) -> None:
    spec = _load(path)
    print(_summary(spec))
    listeners = spec.listeners.values()
    lines = (
        [f"service {i}" for i in spec.services]
        + [f"listener {x.id}" for x in listeners if not x.on_host]
        + [f"host-listener {x.id}" for x in listeners if x.on_host]
        + [f"file {i}" for i in spec.files]
        + [f"host {i}" for i in spec.hosts]
        + [f"edge {i}" for i in spec.edges]
        + [f"group {i}" for i in spec.groups]
    )
    print("\n".join(lines))


def _summary(spec: StackSpec) -> str:
    observable = sum(1 for x in spec.listeners.values() if not x.on_host)
    pre = sum(1 for e in spec.edges.values() if e.phase == "pre")
    post = len(spec.edges) - pre
    return (
        f"services: {len(spec.services)}, listeners: {observable}, "
        f"files: {len(spec.files)}, hosts: {len(spec.hosts)}, "
        f"edges: {len(spec.edges)} (pre {pre}, post {post}), "
        f"groups: {len(spec.groups)}"
    )


class _Stop(Exception):
    """SIGTERM or SIGINT arrived."""


async def serve(core: Core) -> None:
    """Run ``core`` until SIGTERM or SIGINT, then cancel it and return.

    ``SystemExit`` from ``Core.run`` (a failed unit preflight) propagates.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in _STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)

    async def stopper() -> None:
        await stop.wait()
        raise _Stop

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(stopper())
            await core.run()
    except* _Stop:
        pass  # graceful: the TaskGroup has cancelled and awaited the core
    finally:
        for sig in _STOP_SIGNALS:
            loop.remove_signal_handler(sig)


def _run(path: Path, log_format: LogFormat, log_level: str) -> None:
    spec = _load(path)
    setup_logging(log_format, log_level)
    core = Core(spec)
    core.subscribe(log_event)
    asyncio.run(serve(core))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="qd-service-control")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-config", help="validate and summarise a config")
    check.add_argument("config", type=Path)
    run = commands.add_parser("run", help="observe the stack and log its events")
    run.add_argument("config", type=Path)
    run.add_argument("--log-format", choices=("text", "json"), default="text")
    run.add_argument(
        "--log-level",
        default="INFO",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    args = parser.parse_args(argv)
    if args.command == "check-config":
        _check_config(args.config)
    elif args.command == "run":
        _run(args.config, args.log_format, args.log_level)
