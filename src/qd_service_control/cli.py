"""Command line entry point."""

import argparse
import asyncio
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Final

from qd_service_control.config.load import ConfigFileError, load_config
from qd_service_control.config.spec import StackSpec
from qd_service_control.core import Core
from qd_service_control.edm.screens import default_name, generate
from qd_service_control.logs import LogFormat, log_event, setup_logging
from qd_service_control.polkit import polkit_rule
from qd_service_control.pvnames import pv_names

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


def _check_config(path: Path, *, pvs: bool = False) -> None:
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
    if pvs:
        print("\n".join(pv_names(spec).all()))


def _generate_edm(config: Path, outdir: Path, name: str | None) -> None:
    spec = _load(config)
    try:
        files = generate(spec, name or default_name(spec.prefix))
    except ValueError as err:
        print(f"{config}: {err}", file=sys.stderr)
        sys.exit(_EXIT_BAD_CONFIG)
    outdir.mkdir(parents=True, exist_ok=True)
    for file_name, content in files.items():
        path = outdir / file_name
        path.write_text(content)
        print(path)


def _generate_polkit(config: Path, user: str) -> None:
    print(polkit_rule(_load(config), user), end="")


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


def _run(path: Path, log_format: LogFormat, log_level: str, *, no_ioc: bool) -> None:
    spec = _load(path)
    setup_logging(log_format, log_level)
    core = Core(spec)
    core.subscribe(log_event)
    if no_ioc:
        asyncio.run(serve(core))
        return
    # Imported here so that check-config and the generators never load EPICS.
    from qd_service_control.ioc.app import serve_ioc

    asyncio.run(serve(core, ioc=serve_ioc))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="qd-service-control")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-config", help="validate and summarise a config")
    check.add_argument("config", type=Path)
    check.add_argument(
        "--pvs", action="store_true", help="also print every full PV name"
    )
    edm = commands.add_parser("generate-edm", help="write the EDM screens")
    edm.add_argument("config", type=Path)
    edm.add_argument("outdir", type=Path)
    edm.add_argument("--name", help="screen name (default: derived from the prefix)")
    rule = commands.add_parser(
        "generate-polkit", help="print the polkit rule for the tool's user"
    )
    rule.add_argument("config", type=Path)
    rule.add_argument("--user", required=True, help="the user the tool runs as")
    run = commands.add_parser(
        "run", help="observe the stack, serve it as PVs and log its events"
    )
    run.add_argument("config", type=Path)
    run.add_argument("--log-format", choices=("text", "json"), default="text")
    run.add_argument(
        "--log-level",
        default="INFO",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    run.add_argument(
        "--no-ioc",
        action="store_true",
        help="observe and log only; do not serve PVs",
    )
    args = parser.parse_args(argv)
    if args.command == "check-config":
        _check_config(args.config, pvs=args.pvs)
    elif args.command == "generate-edm":
        _generate_edm(args.config, args.outdir, args.name)
    elif args.command == "generate-polkit":
        _generate_polkit(args.config, args.user)
    elif args.command == "run":
        _run(args.config, args.log_format, args.log_level, no_ioc=args.no_ioc)
