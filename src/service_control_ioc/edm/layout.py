"""Sizes, file names and row helpers shared by the top and the other screens."""

import re
from typing import Final, NamedTuple

from service_control_ioc.edm import edl
from service_control_ioc.pvnames import PvNames


class Size(NamedTuple):
    w: int
    h: int


class Link(NamedTuple):
    """A button on the top screen that opens another screen."""

    label: str
    file: str


# Every generated screen must fit this (``--max-size`` overrides it).
DEFAULT_BUDGET: Final = Size(1900, 1040)
ROW: Final = 24
MARGIN: Final = 10
GAP: Final = 8  # between sections
LED: Final = 16
TEXT_H: Final = 20
STATE_W: Final = 90
EXIT_W: Final = 60
# A multiLineTextUpdate pane that shows a whole 20-line ring (a unit's journal
# tail, the events): courier 12 lines are 19 px apart, plus the margins.
_LINE_H: Final = 19
_PANE_MARGINS: Final = 20
_PANE_LINES: Final = 20
PANE_HEIGHT: Final = _PANE_LINES * _LINE_H + _PANE_MARGINS
# FAULT_MSG and FINDING wrap over three lines, so the journalctl reference at
# the end of a finding stays visible.
FINDING_HEIGHT: Final = 3 * _LINE_H + _PANE_MARGINS
CONTROLS: Final = (
    ("START", "Start", 60),
    ("STOP", "Stop", 60),
    ("RESTART", "Restart", 70),
)
_BUTTON_GAP: Final = 5
_UNSAFE: Final = re.compile(r"[^a-z0-9_-]+")


def default_name(prefix: str) -> str:
    """``SCDEMO:SC`` -> ``scdemo-sc``."""
    return _UNSAFE.sub("-", prefix.lower()).strip("-")


def detail_file(name: str, base: str) -> str:
    """The detail screen of the service with PV base ``base``."""
    return f"{name}-{_UNSAFE.sub('-', base.lower())}.edl"


def edges_file(name: str) -> str:
    """The screen listing every edge."""
    return f"{name}-edges.edl"


def controls(names: PvNames, base: str, x: int, y: int) -> list[str]:
    """Start, Stop and Restart buttons from ``x``."""
    objects = []
    for field, label, width in CONTROLS:
        objects.append(edl.button(x, y, width, TEXT_H, names.pv(base, field), label))
        x += width + _BUTTON_GAP
    return objects


def led_state(names: PvNames, base: str, x: int, y: int) -> list[str]:
    """A state LED at ``x`` and the STATE text right of it."""
    return [
        edl.led(x, y + 2, LED, LED, names.pv(base, "STATE"), edl.STATE_COLOURS),
        edl.update(x + LED + 4, y, STATE_W, TEXT_H, names.pv(base, "STATE")),
    ]
