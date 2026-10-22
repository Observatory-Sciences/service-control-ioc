"""``service-control-generate-edm``: write the EDM screens."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from service_control_ioc.cli.common import EXIT_BAD_CONFIG, load_spec
from service_control_ioc.edm.screens import default_name, generate


def generate_edm(config: Path, outdir: Path, name: str | None) -> None:
    spec = load_spec(config)
    try:
        files = generate(spec, name or default_name(spec.prefix))
    except ValueError as err:
        print(f"{config}: {err}", file=sys.stderr)
        sys.exit(EXIT_BAD_CONFIG)
    outdir.mkdir(parents=True, exist_ok=True)
    for file_name, content in files.items():
        path = outdir / file_name
        path.write_text(content)
        print(path)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="service-control-generate-edm", description="Write the EDM screens."
    )
    parser.add_argument("config", type=Path)
    parser.add_argument("outdir", type=Path)
    parser.add_argument("--name", help="screen name (default: derived from the prefix)")
    args = parser.parse_args(argv)
    generate_edm(args.config, args.outdir, args.name)
