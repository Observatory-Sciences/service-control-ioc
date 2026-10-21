"""``teststack/screen/edmshot.py``'s pure helpers (the X parts run on the VM,
in ``tests/integration/test_screens.py``)."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "screen" / "edmshot.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("edmshot", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


edmshot = _load()

EDL = """4 0 1
beginScreenProperties
title "relay - P"
endScreenProperties

# (Message Button)
object activeMessageButtonClass
beginObjectProperties
x 465
y 10
w 60
h 20
controlPv "P:START"
pressValue "1"
endObjectProperties

# (Message Button)
object activeMessageButtonClass
beginObjectProperties
x 530
y 10
w 61
h 21
controlPv "P:STOP"
endObjectProperties
"""


def test_pixel_reads_p6_with_comment(tmp_path: Path) -> None:
    ppm = tmp_path / "a.ppm"
    pixels = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    ppm.write_bytes(b"P6\n# xwdtopnm\n2 2\n255\n" + pixels)
    assert edmshot.pixel(ppm, 0, 0) == [1, 2, 3]
    assert edmshot.pixel(ppm, 1, 1) == [10, 11, 12]
    with pytest.raises(edmshot.EdmShotError, match="outside"):
        edmshot.pixel(ppm, 2, 0)


def test_truncated_ppm_is_an_error(tmp_path: Path) -> None:
    ppm = tmp_path / "a.ppm"
    ppm.write_bytes(b"P6 2 2 255\n" + bytes(5))
    with pytest.raises(edmshot.EdmShotError, match="truncated"):
        edmshot.pixel(ppm, 0, 0)


def test_button_centre_by_control_pv(tmp_path: Path) -> None:
    edl = tmp_path / "s.edl"
    edl.write_text(EDL)
    assert edmshot.button_centre(edl, "P:START") == (495, 20)
    assert edmshot.button_centre(edl, "P:STOP") == (560, 20)
    with pytest.raises(edmshot.EdmShotError, match="0 message buttons"):
        edmshot.button_centre(edl, "P:RESTART")


def test_screen_title(tmp_path: Path) -> None:
    edl = tmp_path / "s.edl"
    edl.write_text(EDL)
    assert edmshot._screen_title(str(edl)) == "relay - P"


def test_edm_environment_overrides_qd_ca_port() -> None:
    cmd = edmshot._edm_command(["scdemo-sc.edl"])
    lines = cmd.splitlines()
    # The QD env first, then scdemo's loopback CA and port 5064 on top of it.
    assert lines[0] == ". /opt/QD/env/qd-epics-edm.sh"
    assert lines[1] == "set -a; . /etc/scdemo/ca.env; set +a"
    assert "EPICS_CA_SERVER_PORT=5064" in lines[2]
    assert "EDMDATAFILES=/opt/scdemo/edm" in lines[2]
    assert lines[-1] == "exec edm -x -noautomsg scdemo-sc.edl"
