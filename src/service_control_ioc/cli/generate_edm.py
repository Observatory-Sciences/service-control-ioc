"""``service-control-generate-edm``: write the EDM screens."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from service_control_ioc.cli.common import EXIT_BAD_CONFIG, load_spec
from service_control_ioc.edm.screens import (
    DEFAULT_BUDGET,
    Link,
    Size,
    default_name,
    generate,
)


def _format_size(size: Size) -> str:
    return f"{size.w}x{size.h}"


def parse_size(text: str) -> Size:
    """``"1900x1040"`` as a ``Size``; both sides must be positive integers."""
    width, sep, height = text.partition("x")
    if not (sep and width.isdecimal() and height.isdecimal()):
        raise argparse.ArgumentTypeError(f"{text!r} is not <width>x<height>")
    size = Size(int(width), int(height))
    if size.w <= 0 or size.h <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} must be larger than 0 x 0")
    return size


def parse_link(text: str) -> Link:
    """``"Wiring=wiring.edl"`` as a ``Link``: a label and an ``.edl`` file name."""
    label, sep, file = text.partition("=")
    label = label.strip()
    if not sep or not label:
        raise argparse.ArgumentTypeError(f"{text!r} is not <label>=<file>.edl")
    if not file.endswith(".edl") or "/" in file:
        raise argparse.ArgumentTypeError(
            f"{file!r} must be a .edl file name in the same directory"
        )
    if not (label + file).isascii():
        raise argparse.ArgumentTypeError(
            f"{text!r}: the label and file name must be ASCII (EDM's X fonts)"
        )
    return Link(label, file)


def generate_edm(
    config: Path,
    outdir: Path,
    name: str | None,
    budget: Size = DEFAULT_BUDGET,
    links: Sequence[Link] = (),
) -> None:
    spec = load_spec(config)
    try:
        files = generate(
            spec, name or default_name(spec.prefix), budget=budget, links=links
        )
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
    parser.add_argument(
        "--max-size",
        type=parse_size,
        default=DEFAULT_BUDGET,
        metavar="WxH",
        help=f"largest screen in pixels (default: {_format_size(DEFAULT_BUDGET)})",
    )
    parser.add_argument(
        "--link",
        type=parse_link,
        action="append",
        metavar="LABEL=FILE.edl",
        help="add a header button opening FILE.edl; repeat for more",
    )
    args = parser.parse_args(argv)
    generate_edm(args.config, args.outdir, args.name, args.max_size, args.link or ())
