"""The top, edges and per-service detail screens, from a ``StackSpec``."""

import math
import re
from collections.abc import Sequence
from typing import Final

from service_control_ioc.config.spec import ServiceSpec, StackSpec
from service_control_ioc.edm import edl
from service_control_ioc.edm.layout import (
    DEFAULT_BUDGET,
    EXIT_W,
    FINDING_HEIGHT,
    LED,
    MARGIN,
    PANE_HEIGHT,
    ROW,
    TEXT_H,
    Link,
    Size,
    controls,
    default_name,
    detail_file,
    edges_file,
    led_state,
)
from service_control_ioc.edm.top import top_screen
from service_control_ioc.pvnames import PvNames, pv_names

# The public API: callers import these from here.
__all__ = [
    "DEFAULT_BUDGET",
    "Link",
    "Size",
    "default_name",
    "detail_file",
    "edges_file",
    "edges_screen",
    "generate",
]

WIDTH: Final = 900  # a detail screen
# The edges screen: LED, DESC, DETAIL and SINCE, a column every _EDGE_COLUMN px.
_EDGE_COLUMN: Final = 700
_EDGE_DESC_X: Final = LED + 4
_EDGE_DESC_W: Final = 330
_EDGE_DETAIL_X: Final = _EDGE_DESC_X + _EDGE_DESC_W + 5
_EDGE_DETAIL_W: Final = 200
_EDGE_SINCE_X: Final = _EDGE_DETAIL_X + _EDGE_DETAIL_W + 5
_EDGE_SINCE_W: Final = 130


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


def _element_row(names: PvNames, eid: str, y: int, indent: int) -> list[str]:
    base = names.elements[eid]
    return [
        edl.text(indent, y, 170 - indent + MARGIN, TEXT_H, eid),
        *led_state(names, base, 185, y),
        edl.update(300, y, 260, TEXT_H, names.pv(base, "DESC")),
        edl.update(565, y, WIDTH - 565 - MARGIN, TEXT_H, names.pv(base, "DETAIL")),
    ]


def _service_elements(service: ServiceSpec) -> tuple[str, ...]:
    return (*service.listeners, *service.files)


def _detail(spec: StackSpec, names: PvNames, sid: str) -> str:
    service = spec.services[sid]
    base = names.services[sid]
    objects: list[str] = [edl.text(MARGIN, MARGIN, 400, TEXT_H, sid, bold=True)]
    y = MARGIN + ROW + 4
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
        width = WIDTH - 140 - MARGIN if field in wide else 200
        objects.append(edl.text(MARGIN, y, 120, TEXT_H, field.capitalize()))
        pv = names.pv(base, field)
        if field == "FINDING":
            objects.append(edl.multiline(140, y, width, FINDING_HEIGHT, pv))
            y += FINDING_HEIGHT + 4
        else:
            objects.append(edl.update(140, y, width, TEXT_H, pv))
            y += ROW
    y += 4
    objects += controls(names, base, MARGIN, y)
    y += ROW + 8
    for eid in _service_elements(service):
        objects += _element_row(names, eid, y, MARGIN)
        y += ROW
    y += 8
    objects.append(
        edl.multiline(MARGIN, y, WIDTH - 2 * MARGIN, PANE_HEIGHT, names.pv(base, "LOG"))
    )
    y += PANE_HEIGHT + 8
    objects.append(edl.exit_button(WIDTH - MARGIN - 60, y))
    return edl.screen(WIDTH, y + ROW + MARGIN, f"{sid} - {spec.prefix}", objects)


def edges_screen(spec: StackSpec, names: PvNames, name: str, budget: Size) -> str:
    """Every edge: LED, description, detail and since, in columns of
    ``budget.h`` rows. Raises ``ValueError`` if the columns need more than
    ``budget.w``.
    """
    rows = max(1, (budget.h - MARGIN - ROW - (ROW + 10) - MARGIN) // ROW)
    columns = max(1, math.ceil(len(spec.edges) / rows))
    top = MARGIN + ROW
    objects = [edl.text(MARGIN, MARGIN, 200, TEXT_H, "Edges", bold=True)]
    for i, eid in enumerate(spec.edges):
        base = names.elements[eid]
        x = MARGIN + (i // rows) * _EDGE_COLUMN
        y = top + (i % rows) * ROW
        objects.append(
            edl.led(x, y + 2, LED, LED, names.pv(base, "STATE"), edl.STATE_COLOURS)
        )
        for field, dx, w in (
            ("DESC", _EDGE_DESC_X, _EDGE_DESC_W),
            ("DETAIL", _EDGE_DETAIL_X, _EDGE_DETAIL_W),
            ("SINCE", _EDGE_SINCE_X, _EDGE_SINCE_W),
        ):
            objects.append(edl.update(x + dx, y, w, TEXT_H, names.pv(base, field)))
    width = MARGIN + (columns - 1) * _EDGE_COLUMN + _EDGE_SINCE_X + _EDGE_SINCE_W
    width += MARGIN
    y = top + min(len(spec.edges), rows) * ROW + 10
    objects.append(edl.exit_button(width - MARGIN - EXIT_W, y))
    size = Size(width, y + ROW + MARGIN)
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

    add(f"{name}.edl", top_screen(spec, names, name, budget, links))
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
