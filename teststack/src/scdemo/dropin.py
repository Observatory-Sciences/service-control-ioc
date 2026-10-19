"""``scdemo-dropin``: write systemd drop-ins that gate startup or inject faults."""

import argparse
import contextlib
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from scdemo.env import ConfigError, exit_on_config_error

GATING_STYLES: Final = ("none", "sleep", "waitport", "notify")

DEFAULT_ROOT: Final = "/etc/systemd/system"
GATING_FILE: Final = "50-gating.conf"
FAULTS_FILE: Final = "60-faults.conf"
_FAULT_KEY: Final = re.compile(r"SCDEMO_(FAULT|DELAY)_[A-Z0-9_]+")
# Only scdemo services (templates and instances too). This also keeps NUL,
# newlines and "/" out of the drop-in path.
_UNIT_NAME: Final = re.compile(r"scdemo-[a-z]+(@[0-9]*)?\.service")
_CONTROLLER_UNIT: Final = "scdemo-controller.service"
_WAIT_FOR_PORT: Final = (
    "/opt/scdemo/bin/scdemo-wait-for-port ${SCDEMO_GATE_PROTO} ${SCDEMO_GATE_PORT}"
)


def render_gating(style: str, sleep_s: float = 5.0) -> str:
    """The text of ``50-gating.conf`` for a gating style."""
    if style == "none":
        return "[Service]\nType=simple\n"
    if style == "sleep":
        return f"[Service]\nType=simple\nExecStartPre=/bin/sleep {sleep_s}\n"
    if style == "waitport":
        return f"[Service]\nType=simple\nExecStartPost={_WAIT_FOR_PORT}\n"
    if style == "notify":
        return "[Service]\nType=notify\nNotifyAccess=main\nTimeoutStartSec=30\n"
    raise ValueError(
        f"unknown gating style {style!r} (allowed: {', '.join(GATING_STYLES)})"
    )


def _quote(value: str) -> str:
    """Escape a value for systemd's ``Environment="K=V"`` quoting."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def render_faults(settings: Mapping[str, str]) -> str:
    """The text of ``60-faults.conf``: one ``Environment=`` line per setting."""
    lines = ["[Service]"]
    for key, value in settings.items():
        if not _FAULT_KEY.fullmatch(key):
            raise ValueError(
                f"{key!r}: only SCDEMO_FAULT_* and SCDEMO_DELAY_* may be set"
                " (upper case letters, digits and _)"
            )
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key}: value must not contain a newline")
        lines.append(f'Environment="{key}={_quote(value)}"')
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _clear(dropin_dir: Path) -> None:
    for name in (GATING_FILE, FAULTS_FILE):
        with contextlib.suppress(FileNotFoundError):  # clearing nothing is fine
            (dropin_dir / name).unlink()
    # rmdir only succeeds on an empty directory, so other drop-ins are never touched.
    with contextlib.suppress(OSError):  # missing or not empty
        dropin_dir.rmdir()


def _reload() -> None:
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
    except FileNotFoundError as err:
        raise ConfigError("systemctl not found; use --no-reload") from err
    except subprocess.CalledProcessError as err:
        raise ConfigError(
            f"systemctl daemon-reload failed with exit code {err.returncode}"
        ) from err


def normalise_unit(unit: str) -> str:
    """``unit`` with ``.service`` added when it has no suffix; ConfigError unless
    it is then an scdemo service name."""
    full = unit if "." in unit else f"{unit}.service"
    if not _UNIT_NAME.fullmatch(full):
        raise ConfigError(f"{unit!r}: not an scdemo unit (scdemo-<name>[@<n>].service)")
    return full


def _parse_settings(items: Sequence[str]) -> dict[str, str]:
    if not items:
        raise ConfigError("fault: give at least one K=V setting")
    settings: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise ConfigError(f"{item!r}: expected K=V")
        settings[key] = value
    return settings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scdemo-dropin", description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(DEFAULT_ROOT), help="drop-in root"
    )
    parser.add_argument(
        "--no-reload", action="store_true", help="skip systemctl daemon-reload"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    gating = sub.add_parser("gating", help="write 50-gating.conf")
    gating.add_argument("unit")
    gating.add_argument("style", choices=GATING_STYLES)
    gating.add_argument("--sleep", type=float, default=5.0, metavar="S")
    fault = sub.add_parser("fault", help="write 60-faults.conf")
    fault.add_argument("unit")
    fault.add_argument("settings", nargs="*", metavar="K=V")
    clear = sub.add_parser("clear", help="remove both drop-ins")
    clear.add_argument("unit")
    return parser


@exit_on_config_error
def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    unit = normalise_unit(args.unit)
    dropin_dir: Path = args.root / f"{unit}.d"
    if args.command == "gating":
        if unit == _CONTROLLER_UNIT and args.style == "waitport":
            raise ConfigError("waitport: scdemo-controller has no listener to wait for")
        if not math.isfinite(args.sleep) or args.sleep < 0:
            raise ConfigError(f"--sleep: must be a non-negative number: {args.sleep}")
        _write_atomic(dropin_dir / GATING_FILE, render_gating(args.style, args.sleep))
    elif args.command == "fault":
        try:
            text = render_faults(_parse_settings(args.settings))
        except ValueError as err:
            raise ConfigError(str(err)) from err
        _write_atomic(dropin_dir / FAULTS_FILE, text)
    else:
        _clear(dropin_dir)
    if not args.no_reload:
        _reload()
