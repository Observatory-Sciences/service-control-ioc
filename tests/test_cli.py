import subprocess
import sys
from pathlib import Path

import pytest

from qd_service_control.cli import check_config, ioc

REPO = Path(__file__).resolve().parents[1]
SCDEMO = REPO / "teststack/config/scdemo.yaml"


def test_check_config_cli(capsys):
    check_config.main([str(SCDEMO)])
    out = capsys.readouterr().out
    assert out.startswith(
        "services: 4, listeners: 10, files: 0, hosts: 1, edges: 12 (pre 6, post 6), groups: 1"
    )
    lines = out.splitlines()
    assert "service sink@1" in lines
    assert "listener relay.pull" in lines
    assert "host hw" in lines
    assert "edge hw -> relay.udp" in lines
    assert "group all" in lines


def test_check_config_cli_reports_issues_and_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "prefix: P\ntop: svc\nservices: {svc: {unit: s.service, bogus: 1}}\n"
    )
    with pytest.raises(SystemExit) as e:
        check_config.main([str(bad)])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert f"{bad}: services.svc.bogus: " in err


def test_run_cli_bad_config_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("prefix: P\ntop: svc\nservices: {svc: {unit: s.service, port: 1}}\n")
    with pytest.raises(SystemExit) as e:
        ioc.main([str(bad), "--log-format", "json"])
    assert e.value.code == 2
    assert f"{bad}: services.svc.port: " in capsys.readouterr().err


def test_check_config_pvs_lists_names(capsys):
    check_config.main([str(SCDEMO)])
    plain = capsys.readouterr().out.splitlines()
    check_config.main([str(SCDEMO), "--pvs"])
    with_pvs = capsys.readouterr().out.splitlines()
    assert with_pvs[0] == plain[0]
    assert with_pvs[: len(plain)] == plain
    pvs = with_pvs[len(plain) :]
    assert len(pvs) == 155
    assert "SCDEMO:SC:SINK1:STATE" in pvs


def test_check_config_does_not_import_softioc() -> None:
    code = (
        "import sys\n"
        "from qd_service_control.cli.check_config import main\n"
        "main([sys.argv[1], '--pvs'])\n"
        "assert 'softioc' not in sys.modules\n"
        "assert 'qd_service_control.ioc.app' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(SCDEMO)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("services: 4")


def test_generate_edm_does_not_import_softioc(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "from qd_service_control.cli.generate_edm import main\n"
        "main([sys.argv[1], sys.argv[2]])\n"
        "assert 'softioc' not in sys.modules\n"
        "assert 'aioca' not in sys.modules\n"
        "assert 'qd_service_control.ioc.app' not in sys.modules\n"
    )
    outdir = tmp_path / "edm"
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(SCDEMO), str(outdir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (outdir / "scdemo-sc.edl").is_file()
