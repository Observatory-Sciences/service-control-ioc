"""``service-control-generate-polkit``: print the polkit rule for the tool's user."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from service_control_ioc.cli.common import load_spec
from service_control_ioc.polkit import polkit_rule


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="service-control-generate-polkit",
        description="Print the polkit rule for the tool's user.",
    )
    parser.add_argument("config", type=Path)
    parser.add_argument("--user", required=True, help="the user the tool runs as")
    args = parser.parse_args(argv)
    print(polkit_rule(load_spec(args.config), args.user), end="")
