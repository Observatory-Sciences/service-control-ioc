"""Command line entry point."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from qd_service_control.config.load import ConfigFileError, load_config
from qd_service_control.config.spec import StackSpec

_EXIT_BAD_CONFIG = 2


def _check_config(path: Path) -> None:
    try:
        spec = load_config(path)
    except ConfigFileError as err:
        for issue in err.issues:
            print(f"{err.file}: {issue}", file=sys.stderr)
        sys.exit(_EXIT_BAD_CONFIG)
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


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="qd-service-control")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-config", help="validate and summarise a config")
    check.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    if args.command == "check-config":
        _check_config(args.config)
