"""The top screen: the stack header, an instance grid and the edges.

The grid has a row per service family (one YAML service key) and per
listener or file, with a column per instance number. A singleton is drawn
full width at the grid's left. The edges and the events are a column right
of the grid.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple

from service_control_ioc.config.spec import StackSpec
from service_control_ioc.edm import edl
from service_control_ioc.edm.families import (
    EdgeRow,
    Family,
    Row,
    edge_rows,
    instance_columns,
    service_families,
)
from service_control_ioc.edm.layout import (
    EXIT_W,
    FINDING_HEIGHT,
    GAP,
    LED,
    MARGIN,
    PANE_HEIGHT,
    ROW,
    STATE_W,
    TEXT_H,
    Link,
    Size,
    controls,
    detail_file,
    edges_file,
    led_state,
)
from service_control_ioc.pvnames import PvNames

# Header: the stack's state and controls, then a button per link.
_PREFIX_W: Final = 130
_OVERALL_X: Final = 145
_READY_LABEL_X: Final = 265
_READY_LABEL_W: Final = 45
_READY_X: Final = 315
_POST_READY_LABEL_X: Final = 415
_POST_READY_LABEL_W: Final = 80
_POST_READY_X: Final = 500
_STACK_CONTROLS_X: Final = 650
_LINKS_X: Final = 870
_LINK_W: Final = 90
_LINK_STEP: Final = 95
_FAULTS_Y: Final = MARGIN + ROW + 4
_FAULTS_W: Final = 50
_FAULTS_X: Final = 65
_FAULT_COUNT_W: Final = 40
_FAULT_MSG_X: Final = 115
# The grid: a name column, then a cell per instance column.
_NAME_W: Final = 145
_INDENT: Final = 30  # listener and file rows
_GRID_X: Final = 160
_FULL_CELL: Final = 300
_COMPACT_CELL: Final = 120
# The full-width form of a service without an instance needs this much.
_MIN_AREA: Final = 610
_HEADING_W: Final = 50
_SECTION_W: Final = 200
_GROUP_STATE_X: Final = 145
_GROUP_CONTROLS_X: Final = 260
# Element cells: LED, port (or "file"), and DETAIL in a full cell.
_ELEMENT_TEXT_X: Final = LED + 4
_ELEMENT_TEXT_W: Final = 50
_ELEMENT_DETAIL_X: Final = 74
_ELEMENT_DETAIL_W: Final = 222
# The right-hand column: edges then events.
_RIGHT_W: Final = 520
_EDGES_W: Final = 100
_ALL_EDGES_X: Final = 420
_ALL_EDGES_W: Final = 100
_EDGE_LABEL_W: Final = 290
_EDGE_LEDS_X: Final = 300
_EDGE_LED_STEP: Final = 22
# An edge row has an LED slot per instance column, within the right column.
_EDGE_SLOTS: Final = (_RIGHT_W - _EDGE_LEDS_X - LED) // _EDGE_LED_STEP + 1


class _ServiceCell(NamedTuple):
    """Where a service's LED, texts and buttons go, as x offsets."""

    updates: tuple[tuple[str, int, int], ...]  # field, x, width
    buttons: tuple[tuple[str, str, int, int], ...]  # field, label, x, width
    details: tuple[str, int, int]  # label, x, width


_STATE_X: Final = LED + 4
# A service without an instance: one full-width form at the grid's left.
_FORM: Final = _ServiceCell(
    updates=(
        ("STATE", _STATE_X, 76),
        ("ACTIVE", 100, 150),
        ("NRESTARTS", 254, 30),
        ("CPU", 288, 40),
    ),
    buttons=(
        ("START", "Start", 332, 60),
        ("STOP", "Stop", 397, 60),
        ("RESTART", "Restart", 462, 70),
    ),
    details=("Details", 537, 70),
)
_FULL: Final = _ServiceCell(
    updates=(("STATE", _STATE_X, 76), ("CPU", 100, 40)),
    buttons=(
        ("START", "Start", 144, 36),
        ("STOP", "Stop", 182, 34),
        ("RESTART", "Restart", 218, 50),
    ),
    details=("...", 272, 26),
)
# Too narrow for the buttons: they are one click away, on the detail screen.
_COMPACT: Final = _ServiceCell(
    updates=(("STATE", _STATE_X, 66),),
    buttons=(),
    details=("...", 90, 26),
)


@dataclass(frozen=True)
class _Grid:
    columns: tuple[int, ...]
    cell: int

    @property
    def area(self) -> int:
        return max(len(self.columns) * self.cell, _MIN_AREA)

    @property
    def full(self) -> bool:
        return self.cell == _FULL_CELL

    def x(self, instance: int | None) -> int:
        """The left of the cell for ``instance`` (the grid's left for none)."""
        if instance is None:
            return _GRID_X
        return _GRID_X + self.columns.index(instance) * self.cell


def _width(area: int) -> int:
    """The screen width for a grid ``area`` wide, with the right column."""
    return _GRID_X + area + MARGIN + _RIGHT_W + MARGIN


def _grid(spec: StackSpec, budget: Size) -> _Grid:
    """Full cells if they fit ``budget.w``, else compact ones.

    Raises ``ValueError`` if neither fits, or if there are more columns than
    an edge row has LED slots.
    """
    columns = instance_columns(spec)
    for cell in (_FULL_CELL, _COMPACT_CELL):
        grid = _Grid(columns, cell)
        if _width(grid.area) <= budget.w:
            break
    else:
        raise ValueError(f"{len(columns)} instance columns do not fit {budget.w} px")
    if len(columns) > _EDGE_SLOTS:
        raise ValueError(
            f"{len(columns)} instance columns do not fit the {_EDGE_SLOTS} edge "
            "LED slots of an edge row"
        )
    return grid


def top_screen(
    spec: StackSpec, names: PvNames, name: str, budget: Size, links: Sequence[Link]
) -> str:
    """The stack header, the instance grid, and the edges and events.

    Raises ``ValueError`` if even compact cells do not fit ``budget.w``, or
    if the header links run past the right edge of the screen.
    """
    grid = _grid(spec, budget)
    width = _width(grid.area)
    _check_links(len(links), width)
    objects = _header(spec, names, links, width)
    heading_y = _FAULTS_Y + FINDING_HEIGHT + GAP
    left, left_bottom = _left(spec, names, name, grid, heading_y)
    right_x = _GRID_X + grid.area + MARGIN
    right, right_bottom = _right(spec, names, name, grid, right_x, heading_y)
    events_y = right_bottom + GAP
    objects += left + right
    objects.append(edl.text(right_x, events_y, _SECTION_W, TEXT_H, "Events", bold=True))
    top = events_y + ROW
    height = max(PANE_HEIGHT, left_bottom - top)
    objects.append(
        edl.multiline(right_x, top, _RIGHT_W, height, names.pv("", "EVENTS"))
    )
    y = max(left_bottom, top + height) + GAP
    objects.append(edl.exit_button(width - MARGIN - EXIT_W, y))
    return edl.screen(width, y + ROW + MARGIN, f"{spec.prefix} stack", objects)


def _check_links(count: int, width: int) -> None:
    """Raise ``ValueError`` if ``count`` header links overrun a ``width`` screen."""
    room = width - MARGIN - _LINKS_X - _LINK_W
    fit = room // _LINK_STEP + 1 if room >= 0 else 0
    if count > fit:
        raise ValueError(
            f"{count} header links do not fit the {width} px top screen ({fit} fit)"
        )


def _header(
    spec: StackSpec, names: PvNames, links: Sequence[Link], width: int
) -> list[str]:
    """The stack's state, readiness, controls, links and faults."""
    y = MARGIN
    stack = names.pv("", "STATE")
    # READY and POST_READY are bi records: room for "NOT READY".
    objects = [
        edl.text(MARGIN, y, _PREFIX_W, TEXT_H, spec.prefix, bold=True),
        edl.led(_OVERALL_X, y + 2, LED, LED, stack, edl.OVERALL_COLOURS),
        edl.update(_OVERALL_X + LED + 4, y, STATE_W, TEXT_H, stack),
        edl.text(_READY_LABEL_X, y, _READY_LABEL_W, TEXT_H, "Ready"),
        edl.update(_READY_X, y, STATE_W, TEXT_H, names.pv("", "READY")),
        edl.text(_POST_READY_LABEL_X, y, _POST_READY_LABEL_W, TEXT_H, "Post ready"),
        edl.update(_POST_READY_X, y, STATE_W, TEXT_H, names.pv("", "POST_READY")),
    ]
    if names.stack_controls:
        objects += controls(names, "", _STACK_CONTROLS_X, y)
    for i, link in enumerate(links):
        x = _LINKS_X + i * _LINK_STEP
        objects.append(edl.related(x, y, _LINK_W, TEXT_H, link.label, link.file))
    y = _FAULTS_Y
    objects += [
        edl.text(MARGIN, y, _FAULTS_W, TEXT_H, "Faults"),
        edl.update(_FAULTS_X, y, _FAULT_COUNT_W, TEXT_H, names.pv("", "FAULTS")),
        edl.multiline(
            _FAULT_MSG_X,
            y,
            width - _FAULT_MSG_X - MARGIN,
            FINDING_HEIGHT,
            names.pv("", "FAULT_MSG"),
        ),
    ]
    return objects


def _left(
    spec: StackSpec, names: PvNames, name: str, grid: _Grid, y: int
) -> tuple[list[str], int]:
    """The services grid and the groups, from ``y``; and the y below them."""
    objects = [edl.text(MARGIN, y, _NAME_W, TEXT_H, "Services", bold=True)]
    for instance in grid.columns:
        objects.append(
            edl.text(grid.x(instance), y, _HEADING_W, TEXT_H, str(instance), bold=True)
        )
    y += ROW
    for family in service_families(spec):
        objects += _family(names, name, grid, family, y)
        y += ROW
        for row in family.rows:
            objects += _element_row(spec, names, grid, row, y)
            y += ROW
    # Every group, the stack group too: no other screen shows its PVs.
    if spec.groups:
        y += GAP
        objects.append(edl.text(MARGIN, y, _SECTION_W, TEXT_H, "Groups", bold=True))
        y += ROW
        for gid in spec.groups:
            base = names.groups[gid]
            objects.append(edl.text(MARGIN, y, _PREFIX_W, TEXT_H, gid, bold=True))
            objects += led_state(names, base, _GROUP_STATE_X, y)
            objects += controls(names, base, _GROUP_CONTROLS_X, y)
            y += ROW
    return objects, y


def _family(
    names: PvNames, name: str, grid: _Grid, family: Family, y: int
) -> list[str]:
    """The family's key, then a cell per member (or the full-width form)."""
    objects = [edl.text(MARGIN, y, _NAME_W, TEXT_H, family.key, bold=True)]
    for instance, sid in family.members.items():
        if instance is None:
            layout = _FORM
        else:
            layout = _FULL if grid.full else _COMPACT
        objects += _service_cell(
            names, name, names.services[sid], layout, grid.x(instance), y
        )
    return objects


def _service_cell(
    names: PvNames, name: str, base: str, layout: _ServiceCell, x: int, y: int
) -> list[str]:
    state = names.pv(base, "STATE")
    objects = [edl.led(x, y + 2, LED, LED, state, edl.STATE_COLOURS)]
    for field, dx, w in layout.updates:
        objects.append(edl.update(x + dx, y, w, TEXT_H, names.pv(base, field)))
    for field, label, dx, w in layout.buttons:
        objects.append(edl.button(x + dx, y, w, TEXT_H, names.pv(base, field), label))
    label, dx, w = layout.details
    objects.append(edl.related(x + dx, y, w, TEXT_H, label, detail_file(name, base)))
    return objects


def _element_row(
    spec: StackSpec, names: PvNames, grid: _Grid, row: Row, y: int
) -> list[str]:
    """A listener or file row: a cell per instance, full for a singleton."""
    objects = [edl.text(_INDENT, y, MARGIN + _NAME_W - _INDENT, TEXT_H, row.label)]
    for instance, eid in row.cells.items():
        base = names.elements[eid]
        x = grid.x(instance)
        listener = spec.listeners.get(eid)
        port = str(listener.port) if listener is not None else "file"
        objects += [
            edl.led(x, y + 2, LED, LED, names.pv(base, "STATE"), edl.STATE_COLOURS),
            edl.text(x + _ELEMENT_TEXT_X, y, _ELEMENT_TEXT_W, TEXT_H, port),
        ]
        if instance is None or grid.full:
            objects.append(
                edl.update(
                    x + _ELEMENT_DETAIL_X,
                    y,
                    _ELEMENT_DETAIL_W,
                    TEXT_H,
                    names.pv(base, "DETAIL"),
                )
            )
    return objects


def _right(
    spec: StackSpec, names: PvNames, name: str, grid: _Grid, x: int, y: int
) -> tuple[list[str], int]:
    """The edge rows from ``y``, an LED per instance; and the y below them."""
    objects = [
        edl.text(x, y, _EDGES_W, TEXT_H, "Edges", bold=True),
        edl.related(
            x + _ALL_EDGES_X, y, _ALL_EDGES_W, TEXT_H, "All edges", edges_file(name)
        ),
    ]
    y += ROW
    for row in edge_rows(spec):
        objects += _edge_row(names, grid, row, x, y)
        y += ROW
    return objects, y


def _edge_row(names: PvNames, grid: _Grid, row: EdgeRow, x: int, y: int) -> list[str]:
    label = row.label + ("  (post)" if row.post else "")
    objects = [edl.text(x, y, _EDGE_LABEL_W, TEXT_H, label)]
    for instance, eid in row.cells.items():
        slot = 0 if instance is None else grid.columns.index(instance)
        state = names.pv(names.elements[eid], "STATE")
        objects.append(
            edl.led(
                x + _EDGE_LEDS_X + slot * _EDGE_LED_STEP,
                y + 2,
                LED,
                LED,
                state,
                edl.STATE_COLOURS,
            )
        )
    return objects
