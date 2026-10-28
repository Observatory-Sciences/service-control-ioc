import re
from pathlib import Path

import pytest

from service_control_ioc.config.load import load_config, load_config_text
from service_control_ioc.config.spec import StackSpec
from service_control_ioc.edm.screens import Link, Size, generate
from service_control_ioc.pvnames import pv_names

REPO = Path(__file__).resolve().parents[2]
SCDEMO = REPO / "teststack/config/scdemo.yaml"
PV_LINE = re.compile(r'^(?:controlPv|visPv|alarmPv) "([^"]*)"$', re.MULTILINE)

_PLAIN = """
prefix: P
top: b
services:
  a:
    unit: a.service
    listeners: {http: {proto: tcp, port: 8000}}
  b: {unit: b.service}
edges:
  - {client: b, server: a.http}
"""

_GROUPS = """
prefix: P
top: b
stack_group: all
services:
  a: {unit: a.service}
  b: {unit: b.service}
groups:
  all: {unit: all.service, members: [a, b]}
  pair: {unit: pair.service, members: [a]}
"""


def _instanced(numbers: list[int]) -> str:
    return f"""
prefix: P
top: b
services:
  a:
    unit: "a@{{n}}.service"
    pv: "A{{n}}"
    instances: {numbers}
    listeners: {{http: {{proto: tcp, port: "8000 + n"}}}}
  b: {{unit: b.service}}
edges:
  - {{client: b, server: a.http}}
"""


@pytest.fixture(scope="module")
def scdemo() -> StackSpec:
    return load_config(SCDEMO)


def pvs(text: str) -> list[str]:
    return PV_LINE.findall(text)


def _size(text: str) -> tuple[int, int]:
    found = re.search(r"^w (\d+)\nh (\d+)$", text, re.MULTILINE)
    assert found is not None
    return int(found.group(1)), int(found.group(2))


def _texts(text: str) -> list[str]:
    return re.findall(r'^value \{\n  "(.*)"\n\}$', text, re.MULTILINE)


def test_xspress_fits_with_full_cells(xspress: StackSpec) -> None:
    top = generate(xspress, "x")["x.edl"]
    w, h = _size(top)
    assert w == 1900
    assert h <= 1040
    names = pv_names(xspress)
    assert names.pv(names.services["frame_receiver@3"], "CPU") in pvs(top)


def test_every_state_is_on_top_once(xspress: StackSpec) -> None:
    top = pvs(generate(xspress, "x")["x.edl"])
    names = pv_names(xspress)
    for base in names.services.values():  # one LED (5 rectangles) + one STATE update
        assert top.count(names.pv(base, "STATE")) == 6, base
    for base in names.elements.values():  # one LED
        assert top.count(names.pv(base, "STATE")) == 5, base


def test_eight_instances_use_compact_cells() -> None:
    spec = load_config_text(_instanced(list(range(1, 9))))
    top = generate(spec, "x")["x.edl"]
    names = pv_names(spec)
    assert _size(top)[0] == 160 + 8 * 120 + 540
    assert not any(
        names.pv(b, "CPU") in pvs(top) for s, b in names.services.items() if "@" in s
    )


def test_too_many_instances_is_an_error() -> None:
    with pytest.raises(ValueError, match="11 instance columns do not fit 1900 px"):
        generate(load_config_text(_instanced(list(range(1, 12)))), "x")


def test_more_columns_than_edge_led_slots_is_an_error() -> None:
    # Wide enough for 11 compact cells, but an edge row has 10 LED slots.
    spec = load_config_text(_instanced(list(range(1, 12))))
    with pytest.raises(ValueError, match="11 instance columns .* 10 edge LED"):
        generate(spec, "x", budget=Size(2100, 1040))


def test_sparse_instances_head_their_columns() -> None:
    texts = _texts(generate(load_config_text(_instanced([3, 5])), "x")["x.edl"])
    assert "3" in texts
    assert "5" in texts
    assert "1" not in texts


def test_no_instances_is_full_width_without_headings() -> None:
    # two services, one edge, no instances
    top = generate(load_config_text(_PLAIN), "x")["x.edl"]
    assert _size(top)[0] == 160 + 610 + 540
    assert not any(re.fullmatch(r"\d{1,3}", t) for t in _texts(top))


def test_links_add_header_buttons(xspress: StackSpec) -> None:
    top = generate(xspress, "x", links=[Link("Wiring", "xspress-sc-wiring.edl")])[
        "x.edl"
    ]
    assert '0 "xspress-sc-wiring.edl"' in top
    assert 'buttonLabel "Wiring"' in top


def test_header_links_must_fit_the_top_screen(scdemo: StackSpec) -> None:
    def links(n: int) -> list[Link]:
        return [Link(f"L{i}", f"l{i}.edl") for i in range(n)]

    top = generate(scdemo, "scdemo", links=links(4))["scdemo.edl"]
    assert 'buttonLabel "L3"' in top
    with pytest.raises(ValueError, match=r"5 header links.*1310 px.*4 fit"):
        generate(scdemo, "scdemo", links=links(5))


def test_duplicate_edge_labels_get_their_own_rows(scdemo: StackSpec) -> None:
    top = generate(scdemo, "scdemo")["scdemo.edl"]
    assert top.count('"relay -> sink.udp  (post)"') == 2


def test_generated_files_are_ascii(xspress: StackSpec) -> None:
    assert all(t.isascii() for t in generate(xspress, "x").values())


def test_every_group_gets_a_row() -> None:
    """The stack group too: its PVs are on no other screen."""
    spec = load_config_text(_GROUPS)
    top = pvs(generate(spec, "x")["x.edl"])
    names = pv_names(spec)
    for gid in ("all", "pair"):
        base = names.groups[gid]
        assert top.count(names.pv(base, "STATE")) == 6, gid  # LED + STATE update
        for field in ("START", "STOP", "RESTART"):
            assert names.pv(base, field) in top, (gid, field)
