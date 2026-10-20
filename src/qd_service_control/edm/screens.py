"""The top screen and one detail screen per service, from a ``StackSpec``."""

import re
from typing import Final

from qd_service_control.config.spec import ServiceSpec, StackSpec
from qd_service_control.edm import edl
from qd_service_control.pvnames import PvNames, pv_names

WIDTH: Final = 900
ROW: Final = 24
_MARGIN: Final = 10
_LED: Final = 16
_LOG_HEIGHT: Final = 320
_EVENT_HEIGHT: Final = 300
_TEXT_H: Final = 20
_CONTROLS: Final = (
    ("START", "Start", 60),
    ("STOP", "Stop", 60),
    ("RESTART", "Restart", 70),
)
_UNSAFE: Final = re.compile(r"[^a-z0-9_-]+")


def default_name(prefix: str) -> str:
    """``SCDEMO:SC`` -> ``scdemo-sc``."""
    return _UNSAFE.sub("-", prefix.lower()).strip("-")


def _file_stem(name: str, base: str) -> str:
    return f"{name}-{_UNSAFE.sub('-', base.lower())}"


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


def _top(spec: StackSpec, names: PvNames, name: str) -> str:
    objects: list[str] = []
    y = _MARGIN
    objects.append(edl.text(_MARGIN, y, 200, _TEXT_H, spec.prefix, bold=True))
    objects.append(
        edl.led(230, y + 2, _LED, _LED, names.pv("", "STATE"), edl.OVERALL_COLOURS)
    )
    objects.append(edl.update(250, y, 100, _TEXT_H, names.pv("", "STATE")))
    objects.append(edl.text(360, y, 50, _TEXT_H, "Ready"))
    objects.append(edl.update(415, y, 40, _TEXT_H, names.pv("", "READY")))
    objects.append(edl.text(465, y, 90, _TEXT_H, "Post ready"))
    objects.append(edl.update(560, y, 40, _TEXT_H, names.pv("", "POST_READY")))
    if names.stack_controls:
        objects += _controls(names, "", 650, y)
    y += ROW + 4
    objects.append(edl.text(_MARGIN, y, 50, _TEXT_H, "Faults"))
    objects.append(edl.update(65, y, 40, _TEXT_H, names.pv("", "FAULTS")))
    objects.append(
        edl.update(115, y, WIDTH - 115 - _MARGIN, _TEXT_H, names.pv("", "FAULT_MSG"))
    )
    y += ROW + 8
    objects.append(edl.text(_MARGIN, y, 200, _TEXT_H, "Services", bold=True))
    y += ROW
    for sid, service in spec.services.items():
        base = names.services[sid]
        objects.append(edl.text(_MARGIN, y, 130, _TEXT_H, sid, bold=True))
        objects += _led_state(names, base, 145, y)
        objects.append(edl.update(260, y, 80, _TEXT_H, names.pv(base, "ACTIVE")))
        objects.append(edl.update(345, y, 40, _TEXT_H, names.pv(base, "NRESTARTS")))
        objects.append(edl.update(390, y, 70, _TEXT_H, names.pv(base, "CPU")))
        objects += _controls(names, base, 465, y)
        objects.append(
            edl.related(
                _controls_end(),
                y,
                70,
                _TEXT_H,
                "Details",
                _file_stem(name, base) + ".edl",
            )
        )
        y += ROW
        for eid in _service_elements(service):
            objects += _element_row(names, eid, y, 30)
            y += ROW
    y += 8
    objects.append(edl.text(_MARGIN, y, 200, _TEXT_H, "Edges", bold=True))
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
    y += 8
    objects.append(edl.text(_MARGIN, y, 200, _TEXT_H, "Events", bold=True))
    y += ROW
    objects.append(
        edl.multiline(
            _MARGIN, y, WIDTH - 2 * _MARGIN, _EVENT_HEIGHT, names.pv("", "EVENTS")
        )
    )
    y += _EVENT_HEIGHT + 8
    objects.append(edl.exit_button(WIDTH - _MARGIN - 60, y))
    return edl.screen(WIDTH, y + ROW + _MARGIN, f"{spec.prefix} stack", objects)


def _controls_end() -> int:
    return 465 + sum(width + 5 for _, _, width in _CONTROLS)


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
        objects.append(edl.update(140, y, width, _TEXT_H, names.pv(base, field)))
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
            _MARGIN, y, WIDTH - 2 * _MARGIN, _LOG_HEIGHT, names.pv(base, "LOG")
        )
    )
    y += _LOG_HEIGHT + 8
    objects.append(edl.exit_button(WIDTH - _MARGIN - 60, y))
    return edl.screen(WIDTH, y + ROW + _MARGIN, f"{sid} - {spec.prefix}", objects)


def generate(spec: StackSpec, name: str) -> dict[str, str]:
    """File name -> ``.edl`` text: the top screen and a detail screen per service."""
    names = pv_names(spec)
    files = {f"{name}.edl": _top(spec, names, name)}
    for sid, base in names.services.items():
        files[f"{_file_stem(name, base)}.edl"] = _detail(spec, names, sid)
    return files
