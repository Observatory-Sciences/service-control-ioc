"""``qd-check-config``: validate and summarise a config."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from qd_service_control.cli.common import load_spec
from qd_service_control.config.spec import StackSpec
from qd_service_control.pvnames import pv_names


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


def check_config(path: Path, *, pvs: bool = False) -> None:
    spec = load_spec(path)
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


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="qd-check-config", description="Validate and summarise a config."
    )
    parser.add_argument("config", type=Path)
    parser.add_argument(
        "--pvs", action="store_true", help="also print every full PV name"
    )
    args = parser.parse_args(argv)
    check_config(args.config, pvs=args.pvs)
