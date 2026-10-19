from pathlib import Path

import pytest

from qd_service_control import main

REPO = Path(__file__).resolve().parents[1]
SCDEMO = REPO / "teststack/config/scdemo.yaml"


def test_check_config_cli(capsys):
    main(["check-config", str(SCDEMO)])
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
        main(["check-config", str(bad)])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert f"{bad}: services.svc.bogus: " in err
