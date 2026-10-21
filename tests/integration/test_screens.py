"""The generated EDM screens, live on the test VM (spec 10.6, plan 4 Task 5).

EDM runs under Xvfb on the VM, driven by ``teststack/screen/edmshot.py``
(as root, with the scdemo loopback CA). One module fixture holds scenario
states with ``scenario_session``, takes the screenshots of the plan's table
and clicks the screen's buttons; the tests then check the pictures and the
control evidence:

- the overall LED's centre pixel has the colour of the state;
- no widget in a state column has EDM's disconnected colour (index 0 of the
  VM's ``colors.list``, ``"Disconn/Invalid" { 65535 65535 65535 }``);
- the relay detail's LOG pane shows text;
- Start, a relay Restart and Stop clicked on the screen work, checked through
  the PVs and the tool's ``JOB`` events;
- every screenshot is fetched back into ``docs/acceptance/images``.

Run with ``QDSC_TARGET=vm uv run pytest -m screens
tests/integration/test_screens.py -v``.
"""

import hashlib
import json
import os
import re
import shlex
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest

from integration.conftest import Host
from integration.evidence import evidence_dir
from integration.scenarios import (
    SCENARIOS,
    Put,
    Scenario,
    Session,
    Wait,
    scenario_session,
)
from qd_service_control.edm.edl import OVERALL_COLOURS
from qd_service_control.ioc.mapping import OVERALL_STATES

pytestmark = [
    pytest.mark.screens,
    pytest.mark.skipif(
        os.environ.get("QDSC_TARGET") != "vm",
        reason="the EDM screens are viewed on the VM (QDSC_TARGET=vm)",
    ),
]

REPO: Final = Path(__file__).resolve().parents[2]
IMAGES: Final = REPO / "docs" / "acceptance" / "images"
P: Final = "SCDEMO:SC"
SCREENS: Final = "/opt/scdemo/edm"
REMOTE: Final = "/var/tmp/edmshot"
TOP: Final = "scdemo-sc.edl"
RELAY: Final = "scdemo-sc-relay.edl"
SINK1: Final = "scdemo-sc-sink1.edl"
SINK2: Final = "scdemo-sc-sink2.edl"
REDRAW: Final = 2.0  # seconds for EDM to redraw after the PVs have settled
TOLERANCE: Final = 8  # per channel, 8-bit
DISCONNECTED: Final = (255, 255, 255)  # colors.list index 0, "Disconn/Invalid"
SPIN_CPU: Final = 90.0
BORDER: Final = 4  # a multiLineTextUpdate's 3D border, px
TEXT_GREEN: Final = (0, 224, 0)  # colors.list 16 "Monitor: NORMAL", the text
MIN_TEXT_PIXELS: Final = 1000  # ~10 lines of courier 12 (seen: ~13000)
TABLE: Final = (
    "edm-top-ready",
    "edm-relay-ready",
    "edm-top-starting",
    "edm-top-manual-stop",
    "edm-top-fault-nobind",
    "edm-sink1-fault-nobind",
    "edm-sink2-spin",
    "edm-top-stopped",
    "edm-control-start-before",
    "edm-control-start-after",
    "edm-control-restart-before",
    "edm-control-restart-after",
    "edm-control-stop-before",
    "edm-control-stop-after",
)
# The overall state each top-screen shot must show.
TOP_STATES: Final = {
    "edm-top-ready": "ready",
    "edm-top-starting": "starting",
    "edm-top-manual-stop": "fault",
    "edm-top-fault-nobind": "fault",
    "edm-top-stopped": "stopped",
    "edm-control-start-before": "stopped",
    "edm-control-start-after": "ready",
    "edm-control-restart-after": "ready",
    "edm-control-stop-after": "stopped",
}
JOBS: Final = {
    "start": "JOB start stack scdemo-all.service: done",
    "restart": "JOB restart relay scdemo-relay.service: done",
    "stop": "JOB stop stack scdemo-all.service: done",
}

Rgb = tuple[int, int, int]


# -- pictures and screens ---------------------------------------------------------


@dataclass(frozen=True)
class Image:
    w: int
    h: int
    pixels: bytes

    def at(self, x: int, y: int) -> Rgb:
        i = 3 * (y * self.w + x)
        r, g, b = self.pixels[i : i + 3]
        return r, g, b


def read_ppm(path: Path) -> Image:
    """A binary (P6, maxval 255) PPM as written by ``xwdtopnm``."""
    data = path.read_bytes()
    header = re.match(rb"P6\s+(?:#[^\n]*\n\s*)*(\d+)\s+(\d+)\s+(\d+)\s", data)
    assert header is not None, f"{path} is not a P6 PPM"
    w, h, maxval = (int(v) for v in header.groups())
    assert maxval == 255, path
    pixels = data[header.end() : header.end() + 3 * w * h]
    assert len(pixels) == 3 * w * h, f"{path} is truncated"
    return Image(w, h, pixels)


def edl_objects(text: str) -> list[dict[str, str]]:
    """Each object's class and its one-line properties."""
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


def box(obj: Mapping[str, str]) -> tuple[int, int, int, int]:
    return int(obj["x"]), int(obj["y"]), int(obj["w"]), int(obj["h"])


def screen_title(text: str) -> str:
    match = re.search(r'^title "(.*)"$', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def near(a: Rgb, b: Rgb) -> bool:
    return all(abs(x - y) <= TOLERANCE for x, y in zip(a, b, strict=True))


def overall_rgb(state: str) -> Rgb:
    index = next(i for i, (s, _) in enumerate(OVERALL_STATES) if s == state)
    r, g, b = OVERALL_COLOURS[index]
    return r >> 8, g >> 8, b >> 8


# -- the capture ------------------------------------------------------------------


@dataclass
class Shot:
    name: str
    edl: str
    png: Path
    ppm: Path
    remote_ppm: str
    pvs: dict[str, Any]
    sha256: str  # of the PNG fetched in this run


@dataclass
class Captures:
    edl: dict[str, str] = field(default_factory=dict)  # file -> text
    shots: dict[str, Shot] = field(default_factory=dict)
    control: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    screen: "Screen | None" = None
    started: float = field(default_factory=time.time)  # Mac clock, fixture start


class Screen:
    """edmshot.py on the VM, and the screenshots fetched back."""

    def __init__(self, host: Host, local: Path, caps: Captures) -> None:
        self.host = host
        self.local = local
        self.caps = caps
        self.script = f"python3.11 {host.target.src}/teststack/screen/edmshot.py"
        self.title = ""
        self.file = ""

    def run(self, *args: str) -> Any:
        out = self.host.sh(f"{self.script} {shlex.join(args)}", timeout=120)
        return json.loads(out.strip().splitlines()[-1])

    def edl(self, file: str) -> str:
        if file not in self.caps.edl:
            self.caps.edl[file] = self.host.sh(f"cat {SCREENS}/{file}")
        return self.caps.edl[file]

    def show(self, file: str) -> None:
        """Restart EDM with just ``file`` open, so no stale window remains."""
        self.run("start", file)
        self.file = file
        self.title = screen_title(self.edl(file))
        time.sleep(REDRAW)  # connect and draw

    def shot(self, name: str, *pv_suffixes: str) -> Shot:
        time.sleep(REDRAW)
        png, ppm = f"{REMOTE}/{name}.png", f"{REMOTE}/{name}.ppm"
        self.run("shot", self.title, png, "--ppm", ppm)
        names = [f"{P}:{s}" for s in ("STATE", "READY", *pv_suffixes)]
        pvs = self.host.ca("get", *names, check=False)
        IMAGES.mkdir(parents=True, exist_ok=True)
        local_png, local_ppm = IMAGES / f"{name}.png", self.local / f"{name}.ppm"
        self.host.target.fetch(png, local_png)
        self.host.target.fetch(ppm, local_ppm)
        digest = hashlib.sha256(local_png.read_bytes()).hexdigest()
        shot = Shot(name, self.file, local_png, local_ppm, ppm, pvs, digest)
        self.caps.shots[name] = shot
        return shot

    def click(self, pv_suffix: str) -> Any:
        return self.run(
            "click", self.title, f"{SCREENS}/{self.file}", f"{P}:{pv_suffix}"
        )


def wait(host: Host, caps: Captures, suffix: str, value: str, timeout: float) -> bool:
    reply = host.ca(
        "wait", f"{P}:{suffix}", value, "--timeout", f"{timeout:g}", check=False
    )
    if not reply.get("ok"):
        caps.failures.append(
            f"{suffix} did not read {value!r} in {timeout:g} s: {reply.get('value')!r}"
        )
    return bool(reply.get("ok"))


def expect_pv(caps: Captures, shot: Shot, suffix: str, value: str) -> None:
    got = shot.pvs.get(f"{P}:{suffix}")
    if got != value:
        caps.failures.append(f"{shot.name}: {suffix} is {got!r}, expected {value!r}")


def by_id(sc_id: str) -> Scenario:
    return next(s for s in SCENARIOS if s.id == sc_id)


def jobs(session: Session) -> list[str]:
    return [str(e.get("text")) for e in session.events() if e.get("kind") == "job"]


def capture_ready(host: Host, screen: Screen, caps: Captures) -> None:
    sc = Scenario("SHOT-READY", "BASE clean start, then STOP", steps=(Put("START"),))
    with scenario_session(host, sc) as session:
        screen.show(TOP)
        session.run_steps()
        wait(host, caps, "READY", "READY", 120)
        shot = screen.shot("edm-top-ready")
        expect_pv(caps, shot, "STATE", "ready")
        screen.show(RELAY)
        wait(host, caps, "RELAY:STATE", "ready", 10)
        shot = screen.shot("edm-relay-ready", "RELAY:LOG")
        expect_pv(caps, shot, "STATE", "ready")
        screen.show(TOP)
        host.ca("put", f"{P}:STOP", "1", "--timeout", "10")
        wait(host, caps, "STATE", "stopped", 60)
        shot = screen.shot("edm-top-stopped")
        expect_pv(caps, shot, "STATE", "stopped")


def capture_starting(host: Host, screen: Screen, caps: Captures) -> None:
    with scenario_session(host, by_id("F10")) as session:
        screen.show(TOP)
        session.run_steps()  # START; every sink listener binds 10 s late
        wait(host, caps, "STATE", "starting", 10)
        shot = screen.shot("edm-top-starting")
        expect_pv(caps, shot, "STATE", "starting")


def capture_manual_stop(host: Host, screen: Screen, caps: Captures) -> None:
    sc = Scenario(
        "SHOT-C3",
        "C3 up to SINK1:STOP",
        steps=(Put("START"), Wait("READY", "1"), Put("SINK1:STOP")),
    )
    with scenario_session(host, sc) as session:
        screen.show(TOP)
        session.run_steps()
        wait(host, caps, "STATE", "fault", 30)
        shot = screen.shot("edm-top-manual-stop", "SINK1:STATE")
        expect_pv(caps, shot, "STATE", "fault")


def capture_nobind(host: Host, screen: Screen, caps: Captures) -> None:
    with scenario_session(host, by_id("F1")) as session:
        screen.show(TOP)
        session.run_steps()
        final = session.wait_final()
        if not final.hold.get("ok"):
            caps.failures.append(f"F1 final state did not hold: {final.hold}")
        shot = screen.shot("edm-top-fault-nobind", "SINK1:STATS:STATE")
        expect_pv(caps, shot, "STATE", "fault")
        expect_pv(caps, shot, "SINK1:STATS:STATE", "fault")
        screen.show(SINK1)
        wait(host, caps, "SINK1:STATS:STATE", "fault", 10)
        shot = screen.shot("edm-sink1-fault-nobind", "SINK1:FINDING")
        if not shot.pvs.get(f"{P}:SINK1:FINDING"):
            caps.failures.append("edm-sink1-fault-nobind: SINK1:FINDING is empty")


def capture_spin(host: Host, screen: Screen, caps: Captures) -> None:
    with scenario_session(host, by_id("F9")) as session:
        screen.show(SINK2)
        session.run_steps()
        final = session.wait_final()
        if not final.hold.get("ok"):
            caps.failures.append(f"F9 final state did not hold: {final.hold}")
        wait(host, caps, "SINK2:STATE", "fault", 10)
        shot = screen.shot("edm-sink2-spin", "SINK2:STATE", "SINK2:CPU")
        expect_pv(caps, shot, "STATE", "fault")
        expect_pv(caps, shot, "SINK2:STATE", "fault")
        cpu = float(str(shot.pvs.get(f"{P}:SINK2:CPU") or "0").split()[0])
        if cpu <= SPIN_CPU:
            caps.failures.append(f"edm-sink2-spin: SINK2:CPU {cpu} is not > {SPIN_CPU}")


def capture_control(host: Host, screen: Screen, caps: Captures) -> None:
    """Start, a relay Restart and Stop, each clicked on the top screen."""
    sc = Scenario("SHOT-CONTROL", "control from the screen", steps=())
    control: dict[str, Any] = {}
    with scenario_session(host, sc) as session:
        screen.show(TOP)
        screen.shot("edm-control-start-before", "LASTJOB")
        before = jobs(session)
        control["start"] = {"click": screen.click("START"), "jobs_before": before}
        wait(host, caps, "READY", "READY", 120)
        shot = screen.shot("edm-control-start-after", "LASTJOB")
        control["start"]["pvs"] = shot.pvs
        control["start"]["jobs"] = jobs(session)

        screen.shot("edm-control-restart-before", "RELAY:LASTJOB")
        before = jobs(session)
        control["restart"] = {
            "click": screen.click("RELAY:RESTART"),
            "jobs_before": before,
        }
        wait(host, caps, "RELAY:LASTJOB", "restart: done", 60)
        wait(host, caps, "READY", "READY", 60)
        shot = screen.shot("edm-control-restart-after", "RELAY:LASTJOB")
        control["restart"]["pvs"] = shot.pvs
        control["restart"]["jobs"] = jobs(session)

        screen.shot("edm-control-stop-before", "LASTJOB")
        before = jobs(session)
        control["stop"] = {"click": screen.click("STOP"), "jobs_before": before}
        wait(host, caps, "STATE", "stopped", 60)
        shot = screen.shot("edm-control-stop-after", "LASTJOB")
        control["stop"]["pvs"] = shot.pvs
        control["stop"]["jobs"] = jobs(session)
    caps.control = control


def write_evidence(caps: Captures) -> None:
    path = evidence_dir() / "vm" / "screens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "shots": {
            n: {"edl": s.edl, "png": str(s.png.relative_to(REPO)), "pvs": s.pvs}
            for n, s in caps.shots.items()
        },
        "control": caps.control,
        "failures": caps.failures,
    }
    path.write_text(json.dumps(data, indent=2) + "\n")


@pytest.fixture(scope="module")
def caps(
    container_session: Host, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Captures]:
    host = container_session
    captures = Captures()
    screen = Screen(host, tmp_path_factory.mktemp("ppm"), captures)
    captures.screen = screen
    host.sh(f"rm -rf {REMOTE} && mkdir -p {REMOTE}")
    try:
        capture_ready(host, screen, captures)
        capture_starting(host, screen, captures)
        capture_manual_stop(host, screen, captures)
        capture_nobind(host, screen, captures)
        capture_spin(host, screen, captures)
        capture_control(host, screen, captures)
        for file in (TOP, RELAY, SINK1, SINK2):
            screen.edl(file)
        yield captures
    finally:
        screen.run("stop")  # also removes /run/edmshot*
        host.sh(f"rm -rf {REMOTE}")
        write_evidence(captures)


# -- the tests --------------------------------------------------------------------


def test_capture_conditions_met(caps: Captures) -> None:
    """Every screenshot was taken in the state its table row names."""
    assert caps.failures == []


def led_centre(caps: Captures) -> tuple[int, int]:
    leds = [
        o
        for o in edl_objects(caps.edl[TOP])
        if o["class"] == "activeRectangleClass" and o.get("visPv") == f"{P}:STATE"
    ]
    assert len(leds) == len(OVERALL_STATES), "one rectangle per overall state"
    x, y, w, h = box(leds[0])
    return x + w // 2, y + h // 2


@pytest.mark.parametrize("name", ["edm-top-ready", "edm-top-fault-nobind"])
def test_led_pixel_matches_state(caps: Captures, name: str) -> None:
    """The overall LED's centre pixel, read on the VM with ``edmshot pixel``."""
    x, y = led_centre(caps)
    assert caps.screen is not None
    r, g, b = caps.screen.run("pixel", caps.shots[name].remote_ppm, str(x), str(y))
    state = TOP_STATES[name]
    assert near((r, g, b), overall_rgb(state)), (name, state, (r, g, b))
    # The fetched copy has the same pixel (the other tests read it locally).
    assert read_ppm(caps.shots[name].ppm).at(x, y) == (r, g, b)


def test_led_switches_with_every_top_state(caps: Captures) -> None:
    x, y = led_centre(caps)
    for name, state in TOP_STATES.items():
        got = read_ppm(caps.shots[name].ppm).at(x, y)
        assert near(got, overall_rgb(state)), (name, state, got, overall_rgb(state))


def state_boxes(text: str) -> list[tuple[int, int, int, int]]:
    """Every LED and state text of a screen: the state columns."""
    return [
        box(o)
        for o in edl_objects(text)
        if (o.get("controlPv") or o.get("visPv") or "").endswith(":STATE")
    ]


def test_no_white_disconnected_widgets(caps: Captures) -> None:
    for name, shot in caps.shots.items():
        image = read_ppm(shot.ppm)
        for x0, y0, w, h in state_boxes(caps.edl[shot.edl]):
            white = [
                (x, y)
                for y in range(y0, y0 + h)
                for x in range(x0, x0 + w)
                if near(image.at(x, y), DISCONNECTED)
            ]
            assert not white, f"{name}: disconnected colour at {white[:5]}"


def test_log_waveform_shows_text(caps: Captures) -> None:
    """The relay detail's LOG pane has text-coloured pixels and RELAY:LOG is
    not empty. It does not prove that the pixels spell RELAY:LOG's lines, nor
    that the newest line is the one drawn last (no OCR): that is checked by eye
    and by the IOC tests of the 999-byte cap (ruling 19)."""
    shot = caps.shots["edm-relay-ready"]
    (log,) = [
        o
        for o in edl_objects(caps.edl[RELAY])
        if o["class"] == "multiLineTextUpdateClass"
        and o.get("controlPv") == f"{P}:RELAY:LOG"
    ]
    x0, y0, w, h = box(log)
    image = read_ppm(shot.ppm)
    # Inside the pane's 3D border (its two shadow colours), so an empty pane
    # is one colour.
    inside = [
        image.at(x, y)
        for y in range(y0 + BORDER, y0 + h - BORDER)
        for x in range(x0 + BORDER, x0 + w - BORDER)
    ]
    assert len(set(inside)) > 1, f"LOG region is uniform: {set(inside)}"
    text = sum(near(c, TEXT_GREEN) for c in inside)
    assert text >= MIN_TEXT_PIXELS, f"only {text} text pixels in the LOG pane"
    assert str(shot.pvs.get(f"{P}:RELAY:LOG") or "").strip(), "RELAY:LOG is empty"


@pytest.mark.parametrize("action", ["start", "restart", "stop"])
def test_control_from_screen(caps: Captures, action: str) -> None:
    step = caps.control[action]
    # The job ran because of the click: absent before it, done after it.
    assert JOBS[action] not in "\n".join(step["jobs_before"]), step["jobs_before"]
    assert JOBS[action] in "\n".join(step["jobs"]), step["jobs"]
    pvs = step["pvs"]
    if action == "start":
        assert pvs[f"{P}:READY"] == "READY"
        assert pvs[f"{P}:LASTJOB"] == "start: done"
    elif action == "restart":
        assert pvs[f"{P}:RELAY:LASTJOB"] == "restart: done"
        assert pvs[f"{P}:READY"] == "READY"
    else:
        assert pvs[f"{P}:STATE"] == "stopped"
        assert pvs[f"{P}:LASTJOB"] == "stop: done"


def test_screenshots_written(caps: Captures) -> None:
    """Every row's PNG was captured and fetched in this run: not a stale file
    left in docs/acceptance/images by an earlier run."""
    for name in TABLE:
        assert name in caps.shots, f"{name} was not captured in this run"
        png = IMAGES / f"{name}.png"
        data = png.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", png
        assert hashlib.sha256(data).hexdigest() == caps.shots[name].sha256, png
        assert png.stat().st_mtime >= caps.started, f"{png} predates this run"
