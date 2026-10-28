"""One function per EDM object, in the EDM 4-0-1 file format.

Property order and fonts follow ``procServControl.edl``. The LED colours are
``rgb`` values rather than ``colors.list`` indices, so they do not depend on
the order of the host's colour file. EDM still draws an ``rgb`` colour as the
nearest entry of its palette, so each value is an entry of the standard EDM
``colors.list`` (the index and name are given with it).
"""

from collections.abc import Mapping, Sequence
from typing import Final

Rgb = tuple[int, int, int]  # 16-bit EDM rgb, written "rgb r g b"

_GREY: Final[Rgb] = (40606, 40606, 40606)  # static 6 "Title"
_AMBER: Final[Rgb] = (65535, 52428, 0)  # static 38 "amber-38"
_GREEN: Final[Rgb] = (0, 65535, 0)  # static 15 "Green LED: On"
_RED: Final[Rgb] = (65535, 0, 0)  # static 20 "Red LED: On"

# ELEMENT_STATES index: unknown, stopped, waiting, ready, fault
STATE_COLOURS: Final[Mapping[int, Rgb]] = {
    0: _GREY,
    1: _GREY,
    2: _AMBER,
    3: _GREEN,
    4: _RED,
}
# OVERALL_STATES index: stopped, starting, stopping, ready, fault
OVERALL_COLOURS: Final[Mapping[int, Rgb]] = {
    0: _GREY,
    1: _AMBER,
    2: _AMBER,
    3: _GREEN,
    4: _RED,
}

_FONT: Final = "arial-medium-r-12.0"
_BOLD: Final = "arial-bold-r-12.0"
_MONO: Final = "courier-medium-r-12.0"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rgb(colour: Rgb) -> str:
    return "rgb {} {} {}".format(*colour)


def _object(
    label: str,
    cls: str,
    version: tuple[int, int, int],
    x: int,
    y: int,
    w: int,
    h: int,
    body: str,
) -> str:
    major, minor, release = version
    return (
        f"# ({label})\nobject {cls}\nbeginObjectProperties\n"
        f"major {major}\nminor {minor}\nrelease {release}\n"
        f"x {x}\ny {y}\nw {w}\nh {h}\n{body}endObjectProperties\n"
    )


def screen(w: int, h: int, title: str, objects: Sequence[str]) -> str:
    header = (
        "4 0 1\nbeginScreenProperties\nmajor 4\nminor 0\nrelease 1\n"
        f"x 100\ny 100\nw {w}\nh {h}\n"
        f'font "{_FONT}"\nctlFont "{_FONT}"\nbtnFont "{_FONT}"\n'
        "fgColor index 14\nbgColor index 3\ntextColor index 14\n"
        "ctlFgColor1 index 14\nctlFgColor2 index 0\n"
        "ctlBgColor1 index 0\nctlBgColor2 index 14\n"
        "topShadowColor index 0\nbotShadowColor index 14\n"
        f"title {_quote(title)}\n"
        "showGrid\nsnapToGrid\ngridSize 4\ndisableScroll\nendScreenProperties\n"
    )
    return header + "".join("\n" + obj for obj in objects)


def text(x: int, y: int, w: int, h: int, value: str, *, bold: bool = False) -> str:
    body = (
        f'font "{_BOLD if bold else _FONT}"\nfontAlign "left"\n'
        "fgColor index 14\nbgColor index 3\nuseDisplayBg\n"
        f"value {{\n  {_quote(value)}\n}}\n"
    )
    return _object("Static Text", "activeXTextClass", (4, 1, 1), x, y, w, h, body)


def update(x: int, y: int, w: int, h: int, pv: str) -> str:
    body = (
        f"controlPv {_quote(pv)}\nfgColor index 16\nfgAlarm\n"
        f'bgColor index 10\nfill\nfont "{_FONT}"\n'
    )
    return _object("Textupdate", "TextupdateClass", (10, 0, 0), x, y, w, h, body)


def multiline(x: int, y: int, w: int, h: int, pv: str) -> str:
    body = (
        f"controlPv {_quote(pv)}\nfgColour index 16\nfgAlarm\n"
        f'bgColour index 10\nfill\nfont "{_MONO}"\n'
    )
    return _object(
        "multiLineTextUpdate", "multiLineTextUpdateClass", (10, 0, 0), x, y, w, h, body
    )


def button(x: int, y: int, w: int, h: int, pv: str, label: str) -> str:
    body = (
        "fgColor index 14\nonColor index 4\noffColor index 4\n"
        "inconsistentColor index 0\ntopShadowColor index 1\nbotShadowColor index 11\n"
        f'controlPv {_quote(pv)}\npressValue "1"\n'
        f"onLabel {_quote(label)}\noffLabel {_quote(label)}\n"
        f'3d\nfont "{_BOLD}"\n'
    )
    return _object(
        "Message Button", "activeMessageButtonClass", (4, 1, 0), x, y, w, h, body
    )


def related(x: int, y: int, w: int, h: int, label: str, file: str) -> str:
    body = (
        "fgColor index 14\nbgColor index 4\ntopShadowColor index 1\n"
        f'botShadowColor index 11\nfont "{_BOLD}"\n'
        f"buttonLabel {_quote(label)}\nnumDsps 1\n"
        f"displayFileName {{\n  0 {_quote(file)}\n}}\n"
        f"menuLabel {{\n  0 {_quote(label)}\n}}\n"
    )
    return _object(
        "Related Display", "relatedDisplayClass", (4, 4, 0), x, y, w, h, body
    )


def led(x: int, y: int, w: int, h: int, pv: str, colours: Mapping[int, Rgb]) -> str:
    """One rectangle per state index, each visible only while ``pv`` is in it."""
    rectangles = []
    for index, colour in sorted(colours.items()):
        body = (
            f"lineColor {_rgb(colour)}\nfill\nfillColor {_rgb(colour)}\n"
            f'visPv {_quote(pv)}\nvisMin "{index}"\nvisMax "{index + 1}"\n'
        )
        rectangles.append(
            _object("Rectangle", "activeRectangleClass", (4, 0, 0), x, y, w, h, body)
        )
    return "\n".join(rectangles)


def line(
    points: Sequence[tuple[int, int]],
    pv: str,
    colours: Mapping[int, Rgb],
    *,
    width: int = 2,
    dashed: bool = False,
    arrow: bool = False,
) -> str:
    """One polyline per state index, each visible only while ``pv`` is in it.

    ``x``, ``y``, ``w`` and ``h`` are the bounding box of ``points``, at
    least 1 wide and high so that a straight horizontal or vertical line
    keeps a non-empty box. ``arrow`` puts an arrowhead on the last point.
    """
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    left, top = min(xs), min(ys)
    w, h = max(max(xs) - left, 1), max(max(ys) - top, 1)
    x_points = "".join(f"  {i} {x}\n" for i, x in enumerate(xs))
    y_points = "".join(f"  {i} {y}\n" for i, y in enumerate(ys))
    polylines = []
    for index, colour in sorted(colours.items()):
        body = f"lineColor {_rgb(colour)}\nfillColor {_rgb(colour)}\n"
        if width != 1:
            body += f"lineWidth {width}\n"
        if dashed:
            body += 'lineStyle "dash"\n'
        body += f'visPv {_quote(pv)}\nvisMin "{index}"\nvisMax "{index + 1}"\n'
        if arrow:
            body += 'arrows "to"\n'
        body += (
            f"numPoints {len(points)}\nxPoints {{\n{x_points}}}\n"
            f"yPoints {{\n{y_points}}}\n"
        )
        polylines.append(
            _object("Lines", "activeLineClass", (4, 0, 1), left, top, w, h, body)
        )
    return "\n".join(polylines)


def exit_button(x: int, y: int) -> str:
    body = (
        "fgColor index 46\nbgColor index 3\ntopShadowColor index 1\n"
        f'botShadowColor index 11\nlabel "EXIT"\nfont "{_BOLD}"\n3d\n'
    )
    return _object(
        "Exit Button", "activeExitButtonClass", (4, 1, 0), x, y, 60, 24, body
    )
