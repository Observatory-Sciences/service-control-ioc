#!/usr/bin/env python3
"""Drive EDM under Xvfb on the test VM: screenshots, pixels and button clicks.

Stdlib only. Runs on the VM as root (``python3.11 edmshot.py <command>``), and
so does EDM. Every command prints one JSON value on stdout.

- ``start [FILE...]``: start ``Xvfb :99`` if it is not running (pid in
  ``/run/edmshot.pid``), stop any EDM this script started, and start
  ``edm -x -noautomsg FILE...`` (the top screen if no file is given). It waits
  until every window is mapped.
- ``open FILE``: open another display (another ``edm`` process).
- ``windows``: ``[{"id", "title", "x", "y", "w", "h"}]`` from
  ``xwininfo -root -tree``.
- ``shot TITLE OUT.png [--ppm OUT.ppm]``: capture the window with ``xwd -id``
  and convert it with netpbm (``convert`` if netpbm is missing).
- ``pixel OUT.ppm X Y``: ``[r, g, b]`` at that point (P6 parsed here).
- ``click TITLE EDL PV``: click the centre of the ``activeMessageButtonClass``
  in ``EDL`` whose ``controlPv`` is ``PV``.
- ``stop``: stop every EDM this script started, and Xvfb.

EDM sees the scdemo IOCs only: the QD EDM environment, then the scdemo
loopback CA settings on top (port 5064, not the QD stack's 6064).
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TypedDict

DISPLAY: Final = ":99"
SCREEN: Final = "1600x1200x24"
XVFB_PID: Final = Path("/run/edmshot.pid")
EDM_PIDS: Final = Path("/run/edmshot-edm.pids")
EDM_LOG: Final = Path("/run/edmshot-edm.log")
EDM_ENV: Final = "/opt/QD/env/qd-epics-edm.sh"
CA_ENV: Final = "/etc/scdemo/ca.env"
SCREENS: Final = "/opt/scdemo/edm"
TOP: Final = "scdemo-sc.edl"
# On top of ca.env: qd-epics.sh exports the QD stack's 6064/6065.
CA_PORTS: Final = {"EPICS_CA_SERVER_PORT": "5064", "EPICS_CA_REPEATER_PORT": "5065"}
START_TIMEOUT: Final = 30.0
_WINDOW: Final = re.compile(
    r'^\s*(0x[0-9a-f]+) "(.*)": \(.*\)\s+(\d+)x(\d+)\+-?\d+\+-?\d+\s+\+(-?\d+)\+(-?\d+)\s*$'
)


class Window(TypedDict):
    id: str
    title: str
    x: int
    y: int
    w: int
    h: int


class EdmShotError(RuntimeError):
    """A step failed; the message says which and why."""


# -- processes ------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_pids(path: Path) -> list[int]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    return [int(p) for p in text.split()]


def _kill(pids: Sequence[int], timeout: float = 10.0) -> None:
    live = [p for p in pids if _alive(p)]
    for pid in live:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while any(_alive(p) for p in live):
        if time.monotonic() > deadline:
            for pid in live:
                if _alive(pid):
                    os.kill(pid, signal.SIGKILL)
            break
        time.sleep(0.1)
    # Reap our own children (a no-op for processes started by another run).
    for pid in live:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _x_env() -> dict[str, str]:
    return {**os.environ, "DISPLAY": DISPLAY}


def _start_xvfb() -> int:
    """Start Xvfb unless the recorded one is still alive; return its pid."""
    pids = _read_pids(XVFB_PID)
    if pids and _alive(pids[0]):
        return pids[0]
    proc = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", SCREEN, "-nolisten", "tcp"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    XVFB_PID.write_text(f"{proc.pid}\n")
    deadline = time.monotonic() + START_TIMEOUT
    while True:
        ok = subprocess.run(
            ["xwininfo", "-root"],
            env=_x_env(),
            capture_output=True,
            check=False,
        )
        if ok.returncode == 0:
            return proc.pid
        if proc.poll() is not None or time.monotonic() > deadline:
            raise EdmShotError(f"Xvfb {DISPLAY} did not start (rc {proc.poll()})")
        time.sleep(0.2)


def _edm_command(files: Sequence[str]) -> str:
    """The bash command that sets EDM's environment and execs it."""
    exports = " ".join(f"{k}={v}" for k, v in CA_PORTS.items())
    return (
        f". {EDM_ENV}\n"
        f"set -a; . {CA_ENV}; set +a\n"
        f"export {exports} EDMDATAFILES={SCREENS} DISPLAY={DISPLAY}\n"
        f"cd {SCREENS}\n"
        f"exec edm -x -noautomsg {shlex.join(files)}"
    )


def _spawn_edm(files: Sequence[str]) -> int:
    with EDM_LOG.open("ab") as log:
        proc = subprocess.Popen(
            ["bash", "-c", _edm_command(files)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    with EDM_PIDS.open("a") as out:
        out.write(f"{proc.pid}\n")
    return proc.pid


def _screen_title(file: str) -> str:
    """The ``title`` screen property of an ``.edl`` file."""
    path = Path(file) if os.path.isabs(file) else Path(SCREENS) / file
    match = re.search(r'^title "((?:[^"\\]|\\.)*)"$', path.read_text(), re.MULTILINE)
    if match is None:
        raise EdmShotError(f"{path} has no title")
    return match.group(1).replace('\\"', '"').replace("\\\\", "\\")


def _wait_windows(titles: Sequence[str], pid: int) -> list[Window]:
    """Wait for the windows, then move each to the top left corner: a screen's
    own position (``x 100 y 100``) would push a tall one past the bottom of
    the 1200-line Xvfb screen, and xwd cannot capture what is off screen."""
    deadline = time.monotonic() + START_TIMEOUT
    while True:
        found = [_find(t, required=False) for t in titles]
        if all(found):
            for window in found:
                assert window is not None  # all() above
                subprocess.run(
                    ["xdotool", "windowmove", window["id"], "0", "0"],
                    env=_x_env(),
                    check=True,
                )
            return [_window(t) for t in titles]
        if not _alive(pid) or time.monotonic() > deadline:
            tail = EDM_LOG.read_text(errors="replace")[-2000:]
            raise EdmShotError(f"EDM windows {list(titles)} did not appear:\n{tail}")
        time.sleep(0.3)


# -- windows ----------------------------------------------------------------------


def windows() -> list[Window]:
    out = subprocess.run(
        ["xwininfo", "-root", "-tree"],
        env=_x_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found: list[Window] = []
    for line in out.splitlines():
        m = _WINDOW.match(line)
        if m is None:
            continue
        wid, title, w, h, x, y = m.groups()
        found.append(
            Window(id=wid, title=title, x=int(x), y=int(y), w=int(w), h=int(h))
        )
    return found


def _find(title: str, *, required: bool = True) -> Window | None:
    """The largest mapped window called ``title``."""
    matches = [w for w in windows() if w["title"] == title]
    if not matches:
        if required:
            raise EdmShotError(f"no window titled {title!r}")
        return None
    return max(matches, key=lambda w: w["w"] * w["h"])


def _window(title: str) -> Window:
    window = _find(title)
    assert window is not None  # required=True raises instead
    return window


# -- images -----------------------------------------------------------------------


def _ppm_of(xwd_file: Path, ppm: Path) -> None:
    if shutil.which("xwdtopnm"):
        with ppm.open("wb") as out:
            subprocess.run(
                ["xwdtopnm", str(xwd_file)],
                stdout=out,
                stderr=subprocess.DEVNULL,
                check=True,
            )
    else:
        subprocess.run(["convert", f"xwd:{xwd_file}", f"ppm:{ppm}"], check=True)


def _png_of(ppm: Path, png: Path) -> None:
    if shutil.which("pnmtopng"):
        with png.open("wb") as out:
            subprocess.run(
                ["pnmtopng", str(ppm)],
                stdout=out,
                stderr=subprocess.DEVNULL,
                check=True,
            )
    else:
        subprocess.run(["convert", f"ppm:{ppm}", f"png:{png}"], check=True)


def shot(title: str, png: Path, ppm: Path | None) -> dict[str, object]:
    window = _window(title)
    png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        xwd_file = Path(tmp) / "shot.xwd"
        subprocess.run(
            ["xwd", "-silent", "-id", window["id"], "-out", str(xwd_file)],
            env=_x_env(),
            check=True,
        )
        target = ppm if ppm is not None else Path(tmp) / "shot.ppm"
        target.parent.mkdir(parents=True, exist_ok=True)
        _ppm_of(xwd_file, target)
        _png_of(target, png)
    return {"window": window, "png": str(png), "ppm": None if ppm is None else str(ppm)}


def read_ppm(path: Path) -> tuple[int, int, bytes]:
    """Width, height and the RGB bytes of a binary (P6, maxval 255) PPM."""
    data = path.read_bytes()
    fields: list[bytes] = []
    i = 0
    while len(fields) < 4:
        while data[i : i + 1].isspace():
            i += 1
        if data[i : i + 1] == b"#":
            i = data.index(b"\n", i) + 1
            continue
        start = i
        while not data[i : i + 1].isspace():
            i += 1
        fields.append(data[start:i])
    i += 1  # the single whitespace byte after maxval
    if fields[0] != b"P6" or int(fields[3]) != 255:
        raise EdmShotError(f"{path}: not a P6 PPM with maxval 255")
    w, h = int(fields[1]), int(fields[2])
    pixels = data[i : i + 3 * w * h]
    if len(pixels) != 3 * w * h:
        raise EdmShotError(f"{path}: truncated ({len(pixels)} of {3 * w * h} bytes)")
    return w, h, pixels


def pixel(path: Path, x: int, y: int) -> list[int]:
    w, h, pixels = read_ppm(path)
    if not (0 <= x < w and 0 <= y < h):
        raise EdmShotError(f"({x}, {y}) is outside {path} ({w}x{h})")
    at = 3 * (y * w + x)
    return list(pixels[at : at + 3])


# -- buttons ----------------------------------------------------------------------


def edl_objects(text: str) -> list[dict[str, str]]:
    """Each object's class and its single-line properties (``x``, ``controlPv``)."""
    objects: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if line.startswith("object "):
            current = {"class": line.split(None, 1)[1]}
        elif line == "endObjectProperties" and current is not None:
            objects.append(current)
            current = None
        elif current is not None and " " in line:
            key, value = line.split(" ", 1)
            current.setdefault(key, value.strip('"'))
    return objects


def button_centre(edl: Path, pv: str) -> tuple[int, int]:
    buttons = [
        o
        for o in edl_objects(edl.read_text())
        if o["class"] == "activeMessageButtonClass" and o.get("controlPv") == pv
    ]
    if len(buttons) != 1:
        raise EdmShotError(f"{edl}: {len(buttons)} message buttons for {pv}")
    b = buttons[0]
    return int(b["x"]) + int(b["w"]) // 2, int(b["y"]) + int(b["h"]) // 2


def click(title: str, edl: Path, pv: str) -> dict[str, object]:
    window = _window(title)
    cx, cy = button_centre(edl, pv)
    subprocess.run(
        [
            "xdotool",
            "mousemove",
            "--window",
            window["id"],
            str(cx),
            str(cy),
            "click",
            "1",
        ],
        env=_x_env(),
        check=True,
    )
    return {"window": window, "pv": pv, "x": cx, "y": cy}


# -- commands ---------------------------------------------------------------------


def start(files: Sequence[str]) -> dict[str, object]:
    files = list(files) or [TOP]
    xvfb = _start_xvfb()
    _kill(_read_pids(EDM_PIDS))
    EDM_PIDS.write_text("")
    EDM_LOG.write_text("")
    pid = _spawn_edm(files)
    found = _wait_windows([_screen_title(f) for f in files], pid)
    return {"xvfb": xvfb, "edm": pid, "windows": found}


def open_display(file: str) -> dict[str, object]:
    pid = _spawn_edm([file])
    return {"edm": pid, "windows": _wait_windows([_screen_title(file)], pid)}


def stop() -> dict[str, object]:
    edm = _read_pids(EDM_PIDS)
    xvfb = _read_pids(XVFB_PID)
    _kill(edm)
    _kill(xvfb)
    for path in (EDM_PIDS, XVFB_PID, EDM_LOG):
        path.unlink(missing_ok=True)
    return {"stopped": {"edm": edm, "xvfb": xvfb}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("start")
    p.add_argument("files", nargs="*")
    p = sub.add_parser("open")
    p.add_argument("file")
    sub.add_parser("windows")
    p = sub.add_parser("shot")
    p.add_argument("title")
    p.add_argument("png", type=Path)
    p.add_argument("--ppm", type=Path)
    p = sub.add_parser("pixel")
    p.add_argument("ppm", type=Path)
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p = sub.add_parser("click")
    p.add_argument("title")
    p.add_argument("edl", type=Path)
    p.add_argument("pv")
    sub.add_parser("stop")
    args = parser.parse_args(argv)

    result: object
    try:
        if args.command == "start":
            result = start(args.files)
        elif args.command == "open":
            result = open_display(args.file)
        elif args.command == "windows":
            result = windows()
        elif args.command == "shot":
            result = shot(args.title, args.png, args.ppm)
        elif args.command == "pixel":
            result = pixel(args.ppm, args.x, args.y)
        elif args.command == "click":
            result = click(args.title, args.edl, args.pv)
        else:
            result = stop()
    except (EdmShotError, subprocess.CalledProcessError, OSError) as err:
        print(json.dumps({"error": f"{type(err).__name__}: {err}"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
