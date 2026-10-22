"""``qd-generate-polkit``: print the polkit rule for the tool's user."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from qd_service_control.cli.common import load_spec
from qd_service_control.polkit import polkit_rule


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="qd-generate-polkit",
        description="Print the polkit rule for the tool's user.",
    )
    parser.add_argument("config", type=Path)
    parser.add_argument("--user", required=True, help="the user the tool runs as")
    args = parser.parse_args(argv)
    print(polkit_rule(load_spec(args.config), args.user), end="")
