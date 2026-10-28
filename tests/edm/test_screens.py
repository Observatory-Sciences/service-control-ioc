import re
from dataclasses import replace
from pathlib import Path

import pytest

from service_control_ioc.cli import generate_edm
from service_control_ioc.config.load import load_config, load_config_text
from service_control_ioc.config.spec import StackSpec
from service_control_ioc.edm.screens import (
    DEFAULT_BUDGET,
    Size,
    default_name,
    edges_file,
    edges_screen,
    generate,
)
from service_control_ioc.pvnames import pv_names

REPO = Path(__file__).resolve().parents[2]
SCDEMO = REPO / "teststack/config/scdemo.yaml"
# Every screen fits a 1600x1200 display (the VM's Xvfb screen), so nothing
# needs scrolling and screenshots show the whole screen.
SCREEN_W, SCREEN_H = 1600, 1200
# courier-medium-r-12.0 lines are 19 px apart in EDM's multiLineTextUpdate; a
# pane shows a full 20-line ring (journal and events) with its margins.
PANE_LINES, LINE_H, PANE_MARGINS = 20, 19, 20
PV_LINE = re.compile(r'^(?:controlPv|visPv|alarmPv) "([^"]*)"$', re.MULTILINE)


@pytest.fixture(scope="module")
def spec() -> StackSpec:
    return load_config(SCDEMO)


@pytest.fixture(scope="module")
def files(spec: StackSpec) -> dict[str, str]:
    return generate(spec, "scdemo")


def test_default_name() -> None:
    assert default_name("SCDEMO:SC") == "scdemo-sc"


def test_file_set(files: dict[str, str]) -> None:
    assert sorted(files) == [
        "scdemo-controller.edl",
        "scdemo-edges.edl",
        "scdemo-relay.edl",
        "scdemo-sink1.edl",
        "scdemo-sink2.edl",
        "scdemo.edl",
    ]


def test_every_pv_is_served(spec: StackSpec, files: dict[str, str]) -> None:
    served = set(pv_names(spec).all())
    for name, text in files.items():
        used = PV_LINE.findall(text)
        assert used, name
        assert set(used) <= served, (name, set(used) - served)


def test_screen_shows_every_element(spec: StackSpec, files: dict[str, str]) -> None:
    names = pv_names(spec)
    top = set(PV_LINE.findall(files["scdemo.edl"]))
    assert names.pv("", "STATE") in top
    assert names.pv("", "READY") in top
    for base in (*names.services.values(), *names.elements.values()):
        assert names.pv(base, "STATE") in top, base
    for sid, base in names.services.items():
        detail = set(PV_LINE.findall(files[f"scdemo-{base.lower()}.edl"]))
        assert names.pv(base, "LOG") in detail, sid


def test_stack_buttons_follow_stack_group(spec: StackSpec) -> None:
    names = pv_names(spec)
    top = PV_LINE.findall(generate(spec, "x")["x.edl"])
    assert names.pv("", "START") in top
    plain = replace(spec, stack_group=None)
    assert f"{spec.prefix}:START" not in PV_LINE.findall(generate(plain, "x")["x.edl"])


def test_related_displays_point_to_generated_files(files: dict[str, str]) -> None:
    targets = {
        m
        for text in files.values()
        for m in re.findall(r'^  0 "(.+\.edl)"$', text, re.MULTILINE)
    }
    assert targets
    assert targets <= set(files)


def test_objects_balanced_and_in_bounds(files: dict[str, str]) -> None:
    for name, text in files.items():
        assert text.count("beginObjectProperties") == text.count("endObjectProperties")
        sw = int(re.search(r"^w (\d+)$", text, re.MULTILINE).group(1))  # ty: ignore[unresolved-attribute] - screen header always present
        sh = int(re.search(r"^h (\d+)$", text, re.MULTILINE).group(1))  # ty: ignore[unresolved-attribute] - screen header always present
        assert sw <= SCREEN_W, name
        assert sh <= SCREEN_H, name
        for obj in text.split("beginObjectProperties")[1:]:
            vals = {
                k: int(re.search(rf"^{k} (\d+)$", obj, re.MULTILINE).group(1))  # ty: ignore[unresolved-attribute] - every object has geometry
                for k in "xywh"
            }
            assert vals["x"] + vals["w"] <= sw, name
            assert vals["y"] + vals["h"] <= sh, name


def test_generate_is_deterministic(spec: StackSpec) -> None:
    assert generate(spec, "scdemo") == generate(spec, "scdemo")


def test_generate_edm_cli_writes_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "new" / "dir"
    generate_edm.main([str(SCDEMO), str(out)])
    printed = capsys.readouterr().out.split()
    written = sorted(p for p in out.iterdir())
    assert len(written) == 6
    assert sorted(printed) == sorted(str(p) for p in written)
    assert (out / "scdemo-sc.edl").is_file()


def _geometry(text: str, pv: str) -> dict[str, int]:
    """x, y, w and h of the object whose ``controlPv`` is ``pv``."""
    obj = next(o for o in text.split("object ") if f'controlPv "{pv}"' in o)
    return {
        k: int(re.search(rf"^{k} (\d+)$", obj, re.MULTILINE).group(1))  # ty: ignore[unresolved-attribute] - every object has geometry
        for k in "xywh"
    }


def test_detail_log_shows_the_whole_ring(
    spec: StackSpec, files: dict[str, str]
) -> None:
    """Regression (Task 5, seen live): the 320 px LOG pane showed 16 lines, so
    the newest lines of a 20-line journal ring were hidden."""
    names = pv_names(spec)
    for base in names.services.values():
        log = _geometry(files[f"scdemo-{base.lower()}.edl"], names.pv(base, "LOG"))
        assert log["w"] == 880, base
        assert log["h"] >= PANE_LINES * LINE_H + PANE_MARGINS, base


def test_top_events_show_the_whole_ring(spec: StackSpec, files: dict[str, str]) -> None:
    """Regression (Task 5, seen live): the 300 px EVENTS pane showed 15 of the
    20 ring lines, hiding the newest events."""
    events = _geometry(files["scdemo.edl"], pv_names(spec).pv("", "EVENTS"))
    assert events["h"] >= PANE_LINES * LINE_H + PANE_MARGINS


@pytest.mark.parametrize("field", ["READY", "POST_READY"])
def test_top_ready_fields_fit_not_ready(
    spec: StackSpec, files: dict[str, str], field: str
) -> None:
    """Regression (Task 5, seen live): 40 px showed "READY" as "READ". The bi
    states are "READY" and "NOT READY": 9 characters of arial 12 (< 9 px each)."""
    update = _geometry(files["scdemo.edl"], pv_names(spec).pv("", field))
    assert update["w"] >= 9 * 9


_COLLIDING = """
prefix: P
top: a
services:
  a: {unit: a.service, pv: A_B}
  b: {unit: b.service, pv: a_b}
"""


def test_generate_rejects_detail_screens_with_the_same_file_name() -> None:
    other = load_config_text(_COLLIDING)
    with pytest.raises(ValueError, match="A_B.*a_b|a_b.*A_B"):
        generate(other, "x")


def test_generate_edm_reports_collision_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(_COLLIDING)
    with pytest.raises(SystemExit) as e:
        generate_edm.main([str(config), str(tmp_path / "out")])
    assert e.value.code == 2
    assert "same screen file" in capsys.readouterr().err


def test_top_active_fits_activating(spec: StackSpec, files: dict[str, str]) -> None:
    """Regression (Task 5, seen live): 80 px showed "activating/start-post"
    as "activating/sta". 21 characters of arial 12 need about 150 px."""
    names = pv_names(spec)
    for base in names.services.values():
        active = _geometry(files["scdemo.edl"], names.pv(base, "ACTIVE"))
        assert active["w"] >= 150, base


def _multiline_lines(text: str, pv: str) -> int:
    obj = next(o for o in text.split("object ") if f'controlPv "{pv}"' in o)
    assert obj.startswith("multiLineTextUpdateClass"), pv
    return (_geometry(text, pv)["h"] - PANE_MARGINS) // LINE_H


def test_findings_wrap_over_three_lines(spec: StackSpec, files: dict[str, str]) -> None:
    """Regression (Task 5, seen live): one-line FINDING and FAULT_MSG updates
    cut off the journalctl reference at the end of the text."""
    names = pv_names(spec)
    assert _multiline_lines(files["scdemo.edl"], names.pv("", "FAULT_MSG")) >= 3
    for base in names.services.values():
        detail = files[f"scdemo-{base.lower()}.edl"]
        assert _multiline_lines(detail, names.pv(base, "FINDING")) >= 3, base


def _screen_width(text: str) -> int:
    return int(re.search(r"^w (\d+)$", text, re.MULTILINE).group(1))  # ty: ignore[unresolved-attribute] - screen header always present


def test_edges_screen_lists_every_edge(spec: StackSpec, files: dict[str, str]) -> None:
    names = pv_names(spec)
    used = set(PV_LINE.findall(files[edges_file("scdemo")]))
    assert spec.edges
    for eid in spec.edges:
        base = names.elements[eid]
        for field in ("DESC", "DETAIL", "SINCE"):
            assert names.pv(base, field) in used, (eid, field)


def test_edges_screen_splits_into_columns(xspress: StackSpec) -> None:
    names = pv_names(xspress)
    text = edges_screen(xspress, names, "x", DEFAULT_BUDGET)
    desc = [names.pv(names.elements[eid], "DESC") for eid in xspress.edges]
    assert len(desc) == 44
    xs = {_geometry(text, pv)["x"] for pv in desc}
    assert len(xs) == 2
    assert max(xs) - min(xs) == 700
    assert sum(_geometry(text, pv)["x"] == min(xs) for pv in desc) == 40
    assert _screen_width(text) <= DEFAULT_BUDGET.w


def test_budget_is_enforced(spec: StackSpec) -> None:
    with pytest.raises(
        ValueError, match=r"scdemo\.edl is \d+ x \d+ px, over the 600 x 400 px budget"
    ):
        generate(spec, "scdemo", budget=Size(600, 400))


def test_edges_file_collides_with_detail_screen() -> None:
    other = load_config_text(
        """
prefix: P
top: a
services:
  a: {unit: a.service, pv: EDGES}
"""
    )
    with pytest.raises(ValueError, match="same screen file"):
        generate(other, "x")
