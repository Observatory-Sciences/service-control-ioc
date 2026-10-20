import re
from dataclasses import replace
from pathlib import Path

import pytest

from qd_service_control import cli
from qd_service_control.config.load import load_config
from qd_service_control.config.spec import StackSpec
from qd_service_control.edm.screens import default_name, generate
from qd_service_control.pvnames import pv_names

REPO = Path(__file__).resolve().parents[2]
SCDEMO = REPO / "teststack/config/scdemo.yaml"
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
        assert sw == 900
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
    cli.main(["generate-edm", str(SCDEMO), str(out)])
    printed = capsys.readouterr().out.split()
    written = sorted(p for p in out.iterdir())
    assert len(written) == 5
    assert sorted(printed) == sorted(str(p) for p in written)
    assert (out / "scdemo-sc.edl").is_file()
