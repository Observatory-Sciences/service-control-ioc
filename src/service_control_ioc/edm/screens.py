"""The top, edges and per-service detail screens, from a ``StackSpec``."""

import math
import re
from collections.abc import Sequence
from typing import Final, NamedTuple

from service_control_ioc.config.spec import ServiceSpec, StackSpec
from service_control_ioc.edm import edl
from service_control_ioc.pvnames import PvNames, pv_names


class Size(NamedTuple):
    w: int
    h: int


class Link(NamedTuple):
    """A button on the top screen that opens another screen."""

    label: str
    file: str


# Every generated screen must fit this (``--max-size`` overrides it).
DEFAULT_BUDGET: Final = Size(1900, 1040)
WIDTH: Final = 900
ROW: Final = 24
_MARGIN: Final = 10
_LED: Final = 16
# A multiLineTextUpdate pane that shows a whole 20-line ring (a unit's journal
# tail, the events): courier 12 lines are 19 px apart, plus the margins.
_LINE_H: Final = 19
_PANE_MARGINS: Final = 20
_PANE_LINES: Final = 20
_PANE_HEIGHT: Final = _PANE_LINES * _LINE_H + _PANE_MARGINS
# FAULT_MSG and FINDING wrap over three lines, so the journalctl reference at
# the end of a finding stays visible.
_FINDING_HEIGHT: Final = 3 * _LINE_H + _PANE_MARGINS
# The events pane is a column right of the rows, so the top screen stays within
# a 1600x1200 display (below the rows it would be 1270 px tall).
_EVENTS_W: Final = 680
TOP_WIDTH: Final = WIDTH + _EVENTS_W + _MARGIN
_TEXT_H: Final = 20
_CONTROLS: Final = (
    ("START", "Start", 60),
    ("STOP", "Stop", 60),
    ("RESTART", "Restart", 70),
)
# The edges screen: LED, DESC, DETAIL and SINCE, a column every _EDGE_COLUMN px.
_EDGE_COLUMN: Final = 700
_EDGE_DESC_X: Final = _LED + 4
_EDGE_DESC_W: Final = 330
_EDGE_DETAIL_X: Final = _EDGE_DESC_X + _EDGE_DESC_W + 5
_EDGE_DETAIL_W: Final = 200
_EDGE_SINCE_X: Final = _EDGE_DETAIL_X + _EDGE_DETAIL_W + 5
_EDGE_SINCE_W: Final = 130
_EXIT_W: Final = 60
_UNSAFE: Final = re.compile(r"[^a-z0-9_-]+")
# A service row on the top screen: ACTIVE has room for "activating/start-post".
_ACTIVE_X: Final = 260
_ACTIVE_W: Final = 160
_NRESTARTS_X: Final = _ACTIVE_X + _ACTIVE_W + 5
_CPU_X: Final = _NRESTARTS_X + 45
_ROW_CONTROLS_X: Final = _CPU_X + 75


def default_name(prefix: str) -> str:
    """``SCDEMO:SC`` -> ``scdemo-sc``."""
    return _UNSAFE.sub("-", prefix.lower()).strip("-")


def detail_file(name: str, base: str) -> str:
    """The detail screen of the service with PV base ``base``."""
    return f"{name}-{_UNSAFE.sub('-', base.lower())}.edl"


def edges_file(name: str) -> str:
    """The screen listing every edge."""
    return f"{name}-edges.edl"


def _check_size(file: str, size: Size, budget: Size) -> None:
    if size.w > budget.w or size.h > budget.h:
        raise ValueError(
            f"{file} is {size.w} x {size.h} px, over the "
            f"{budget.w} x {budget.h} px budget (--max-size)"
        )


def _screen_size(text: str) -> Size:
    """The ``w`` and ``h`` in a screen's header (its first ``w`` and ``h``)."""
    found = re.search(r"^w (\d+)\nh (\d+)$", text, re.MULTILINE)
    if found is None:
        raise ValueError("no screen size in the .edl text")
    return Size(int(found.group(1)), int(found.group(2)))


def _controls(names: PvNames, base: str, x: int, y: int) -> list[str]:
    objects = []
    for field, label, width in _CONTROLS:
        objects.append(edl.button(x, y, width, _TEXT_H, names.pv(base, field), label))
        x += width + 5
    return objects


def _led_state(names: PvNames, base: str, x: int, y: int) -> list[str]:
    return [
        edl.led(x, y + 2, _LED, _LED, names.pv(base, "STATE"), edl.STATE_COLOURS),
        edl.update(x + _LED + 4, y, 90, _TEXT_H, names.pv(base, "STATE")),
    ]


def _element_row(names: PvNames, eid: str, y: int, indent: int) -> list[str]:
    base = names.elements[eid]
    return [
        edl.text(indent, y, 170 - indent + _MARGIN, _TEXT_H, eid),
        *_led_state(names, base, 185, y),
        edl.update(300, y, 260, _TEXT_H, names.pv(base, "DESC")),
        edl.update(565, y, WIDTH - 565 - _MARGIN, _TEXT_H, names.pv(base, "DETAIL")),
    ]


def _service_elements(service: ServiceSpec) -> tuple[str, ...]:
    return (*service.listeners, *service.files)


def _top(spec: StackSpec, names: PvNames, name: str, links: Sequence[Link]) -> str:
    objects: list[str] = []
    y = _MARGIN
    objects.append(edl.text(_MARGIN, y, 130, _TEXT_H, spec.prefix, bold=True))
    # The overall LED and STATE line up with the service rows' below. READY
    # and POST_READY are bi records: room for "NOT READY".
    objects.append(
        edl.led(145, y + 2, _LED, _LED, names.pv("", "STATE"), edl.OVERALL_COLOURS)
    )
    objects.append(edl.update(165, y, 90, _TEXT_H, names.pv("", "STATE")))
    objects.append(edl.text(265, y, 45, _TEXT_H, "Ready"))
    objects.append(edl.update(315, y, 90, _TEXT_H, names.pv("", "READY")))
    objects.append(edl.text(415, y, 80, _TEXT_H, "Post ready"))
    objects.append(edl.update(500, y, 90, _TEXT_H, names.pv("", "POST_READY")))
    if names.stack_controls:
        objects += _controls(names, "", 650, y)
    y += ROW + 4
    objects.append(edl.text(_MARGIN, y, 50, _TEXT_H, "Faults"))
    objects.append(edl.update(65, y, 40, _TEXT_H, names.pv("", "FAULTS")))
    objects.append(
        edl.multiline(
            115, y, WIDTH - 115 - _MARGIN, _FINDING_HEIGHT, names.pv("", "FAULT_MSG")
        )
    )
    y += _FINDING_HEIGHT + 8
    objects.append(edl.text(_MARGIN, y, 200, _TEXT_H, "Services", bold=True))
    y += ROW
    for sid, service in spec.services.items():
        base = names.services[sid]
        objects.append(edl.text(_MARGIN, y, 130, _TEXT_H, sid, bold=True))
        objects += _led_state(names, base, 145, y)
        objects.append(
            edl.update(_ACTIVE_X, y, _ACTIVE_W, _TEXT_H, names.pv(base, "ACTIVE"))
        )
        objects.append(
            edl.update(_NRESTARTS_X, y, 40, _TEXT_H, names.pv(base, "NRESTARTS"))
        )
        objects.append(edl.update(_CPU_X, y, 70, _TEXT_H, names.pv(base, "CPU")))
        objects += _controls(names, base, _ROW_CONTROLS_X, y)
        objects.append(
            edl.related(
                _controls_end(),
                y,
                70,
                _TEXT_H,
                "Details",
                detail_file(name, base),
            )
        )
        y += ROW
        for eid in _service_elements(service):
            objects += _element_row(names, eid, y, 30)
            y += ROW
    y += 8
    objects.append(edl.text(_MARGIN, y, 55, _TEXT_H, "Edges", bold=True))
    objects.append(edl.related(70, y, 100, _TEXT_H, "All edges", edges_file(name)))
    y += ROW
    for eid in spec.edges:
        base = names.elements[eid]
        objects += _led_state(names, base, _MARGIN, y)
        objects.append(edl.update(125, y, 300, _TEXT_H, names.pv(base, "DESC")))
        objects.append(edl.update(430, y, 330, _TEXT_H, names.pv(base, "DETAIL")))
        objects.append(
            edl.update(765, y, WIDTH - 765 - _MARGIN, _TEXT_H, names.pv(base, "SINCE"))
        )
        y += ROW
    if spec.groups:
        y += 8
        objects.append(edl.text(_MARGIN, y, 200, _TEXT_H, "Groups", bold=True))
        y += ROW
        for gid in spec.groups:
            base = names.groups[gid]
            objects.append(edl.text(_MARGIN, y, 130, _TEXT_H, gid, bold=True))
            objects += _led_state(names, base, 145, y)
            objects += _controls(names, base, 260, y)
            y += ROW
    objects.append(edl.text(WIDTH, _MARGIN, 200, _TEXT_H, "Events", bold=True))
    top = _MARGIN + ROW
    height = max(_PANE_HEIGHT, y - top)
    objects.append(edl.multiline(WIDTH, top, _EVENTS_W, height, names.pv("", "EVENTS")))
    y = top + height + 8
    objects.append(edl.exit_button(TOP_WIDTH - _MARGIN - 60, y))
    return edl.screen(TOP_WIDTH, y + ROW + _MARGIN, f"{spec.prefix} stack", objects)


def _controls_end() -> int:
    return _ROW_CONTROLS_X + sum(width + 5 for _, _, width in _CONTROLS)


def _detail(spec: StackSpec, names: PvNames, sid: str) -> str:
    service = spec.services[sid]
    base = names.services[sid]
    objects: list[str] = [edl.text(_MARGIN, _MARGIN, 400, _TEXT_H, sid, bold=True)]
    y = _MARGIN + ROW + 4
    wide = {"UNIT", "EXIT", "FINDING", "LASTJOB"}
    for field in (
        "UNIT",
        "STATE",
        "ACTIVE",
        "EXIT",
        "NRESTARTS",
        "CPU",
        "FINDING",
        "LASTJOB",
    ):
        width = WIDTH - 140 - _MARGIN if field in wide else 200
        objects.append(edl.text(_MARGIN, y, 120, _TEXT_H, field.capitalize()))
        pv = names.pv(base, field)
        if field == "FINDING":
            objects.append(edl.multiline(140, y, width, _FINDING_HEIGHT, pv))
            y += _FINDING_HEIGHT + 4
        else:
            objects.append(edl.update(140, y, width, _TEXT_H, pv))
            y += ROW
    y += 4
    objects += _controls(names, base, _MARGIN, y)
    y += ROW + 8
    for eid in _service_elements(service):
        objects += _element_row(names, eid, y, _MARGIN)
        y += ROW
    y += 8
    objects.append(
        edl.multiline(
            _MARGIN, y, WIDTH - 2 * _MARGIN, _PANE_HEIGHT, names.pv(base, "LOG")
        )
    )
    y += _PANE_HEIGHT + 8
    objects.append(edl.exit_button(WIDTH - _MARGIN - 60, y))
    return edl.screen(WIDTH, y + ROW + _MARGIN, f"{sid} - {spec.prefix}", objects)


def edges_screen(spec: StackSpec, names: PvNames, name: str, budget: Size) -> str:
    """Every edge: LED, description, detail and since, in columns of
    ``budget.h`` rows. Raises ``ValueError`` if the columns need more than
    ``budget.w``.
    """
    rows = max(1, (budget.h - _MARGIN - ROW - (ROW + 10) - _MARGIN) // ROW)
    columns = max(1, math.ceil(len(spec.edges) / rows))
    top = _MARGIN + ROW
    objects = [edl.text(_MARGIN, _MARGIN, 200, _TEXT_H, "Edges", bold=True)]
    for i, eid in enumerate(spec.edges):
        base = names.elements[eid]
        x = _MARGIN + (i // rows) * _EDGE_COLUMN
        y = top + (i % rows) * ROW
        objects.append(
            edl.led(x, y + 2, _LED, _LED, names.pv(base, "STATE"), edl.STATE_COLOURS)
        )
        for field, dx, w in (
            ("DESC", _EDGE_DESC_X, _EDGE_DESC_W),
            ("DETAIL", _EDGE_DETAIL_X, _EDGE_DETAIL_W),
            ("SINCE", _EDGE_SINCE_X, _EDGE_SINCE_W),
        ):
            objects.append(edl.update(x + dx, y, w, _TEXT_H, names.pv(base, field)))
    width = _MARGIN + (columns - 1) * _EDGE_COLUMN + _EDGE_SINCE_X + _EDGE_SINCE_W
    width += _MARGIN
    y = top + min(len(spec.edges), rows) * ROW + 10
    objects.append(edl.exit_button(width - _MARGIN - _EXIT_W, y))
    size = Size(width, y + ROW + _MARGIN)
    _check_size(edges_file(name), size, budget)
    return edl.screen(size.w, size.h, f"Edges - {spec.prefix}", objects)


def generate(
    spec: StackSpec,
    name: str,
    *,
    budget: Size = DEFAULT_BUDGET,
    links: Sequence[Link] = (),
) -> dict[str, str]:
    """File name -> ``.edl`` text: the top screen, the edges screen and a
    detail screen per service.

    Raises ``ValueError`` if two services, or a service and the edges screen,
    would write the same file (bases that differ only in case or in ``_``
    versus ``-``), or if a screen is bigger than ``budget``.
    """
    names = pv_names(spec)
    files: dict[str, str] = {}

    def add(file_name: str, text: str) -> None:
        _check_size(file_name, _screen_size(text), budget)
        files[file_name] = text

    add(f"{name}.edl", _top(spec, names, name, links))
    add(edges_file(name), edges_screen(spec, names, name, budget))
    owners: dict[str, str] = {}
    for sid, base in names.services.items():
        file_name = detail_file(name, base)
        if file_name == edges_file(name):
            raise ValueError(
                f"service {sid!r} (PV base {base!r}) maps to {file_name!r}, "
                "the same screen file as the edges screen; give it a PV base "
                "other than 'edges'"
            )
        if file_name in owners:
            raise ValueError(
                f"services {owners[file_name]!r} and {sid!r} (PV bases "
                f"{names.services[owners[file_name]]!r} and {base!r}) map to "
                f"the same screen file {file_name!r}; make their PV bases "
                "differ by more than case or '_' versus '-'"
            )
        owners[file_name] = sid
        add(file_name, _detail(spec, names, sid))
    return files
