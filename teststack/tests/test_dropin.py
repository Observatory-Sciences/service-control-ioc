import subprocess
from pathlib import Path

import pytest

from scdemo import dropin
from scdemo.dropin import render_faults, render_gating
from scdemo.env import ConfigError, exit_on_config_error

SINK = "scdemo-sink@.service"
CONTROLLER = "scdemo-controller.service"


def test_render_gating_none() -> None:
    assert render_gating("none") == "[Service]\nType=simple\n"


def test_render_gating_sleep() -> None:
    assert (
        render_gating("sleep")
        == "[Service]\nType=simple\nExecStartPre=/bin/sleep 5.0\n"
    )
    assert (
        render_gating("sleep", 2.5)
        == "[Service]\nType=simple\nExecStartPre=/bin/sleep 2.5\n"
    )


def test_render_gating_waitport() -> None:
    assert render_gating("waitport") == (
        "[Service]\nType=simple\n"
        "ExecStartPost=/opt/scdemo/bin/scdemo-wait-for-port ${SCDEMO_GATE_PROTO} ${SCDEMO_GATE_PORT}\n"
    )


def test_render_gating_notify() -> None:
    assert render_gating("notify") == (
        "[Service]\nType=notify\nNotifyAccess=main\nTimeoutStartSec=30\n"
    )


def test_render_gating_unknown_style() -> None:
    with pytest.raises(ValueError, match="bogus"):
        render_gating("bogus")


def test_render_faults() -> None:
    assert (
        render_faults({"SCDEMO_FAULT_CRASH_AFTER": "5"})
        == '[Service]\nEnvironment="SCDEMO_FAULT_CRASH_AFTER=5"\n'
    )
    assert render_faults({"SCDEMO_DELAY_BIND": "1", "SCDEMO_FAULT_NO_READY": "1"}) == (
        '[Service]\nEnvironment="SCDEMO_DELAY_BIND=1"\nEnvironment="SCDEMO_FAULT_NO_READY=1"\n'
    )


def test_render_faults_rejects_other_keys() -> None:
    with pytest.raises(ValueError, match="PATH"):
        render_faults({"PATH": "x"})


def test_render_faults_escapes_systemd_quoting() -> None:
    assert render_faults({"SCDEMO_FAULT_NO_BIND": 'a"b\\c%d'}) == (
        '[Service]\nEnvironment="SCDEMO_FAULT_NO_BIND=a\\"b\\\\c%%d"\n'
    )


def run(tmp_path: Path, *args: str) -> None:
    dropin.main(["--root", str(tmp_path), "--no-reload", *args])


def test_gating_writes_file(tmp_path: Path) -> None:
    run(tmp_path, "gating", SINK, "notify")
    path = tmp_path / f"{SINK}.d" / "50-gating.conf"
    assert path.read_text() == render_gating("notify")
    assert [p.name for p in path.parent.iterdir()] == ["50-gating.conf"]


def test_gating_sleep_option(tmp_path: Path) -> None:
    run(tmp_path, "gating", SINK, "sleep", "--sleep", "1.5")
    text = (tmp_path / f"{SINK}.d" / "50-gating.conf").read_text()
    assert "ExecStartPre=/bin/sleep 1.5\n" in text


def test_gating_overwrites(tmp_path: Path) -> None:
    run(tmp_path, "gating", SINK, "notify")
    run(tmp_path, "gating", SINK, "none")
    assert (tmp_path / f"{SINK}.d" / "50-gating.conf").read_text() == render_gating(
        "none"
    )


def test_fault_writes_file(tmp_path: Path) -> None:
    run(
        tmp_path,
        "fault",
        "scdemo-relay.service",
        "SCDEMO_FAULT_NO_BIND=rep",
        "SCDEMO_DELAY_BIND=2",
    )
    text = (tmp_path / "scdemo-relay.service.d" / "60-faults.conf").read_text()
    assert text == render_faults(
        {"SCDEMO_FAULT_NO_BIND": "rep", "SCDEMO_DELAY_BIND": "2"}
    )


def test_fault_value_may_contain_equals(tmp_path: Path) -> None:
    run(tmp_path, "fault", SINK, "SCDEMO_FAULT_NO_BIND=a=b")
    text = (tmp_path / f"{SINK}.d" / "60-faults.conf").read_text()
    assert "SCDEMO_FAULT_NO_BIND=a=b" in text


def test_clear_removes_both_and_only_those(tmp_path: Path) -> None:
    run(tmp_path, "gating", SINK, "notify")
    run(tmp_path, "fault", SINK, "SCDEMO_FAULT_NO_READY=1")
    other = tmp_path / f"{SINK}.d" / "10-other.conf"
    other.write_text("keep")
    run(tmp_path, "clear", SINK)
    assert [p.name for p in other.parent.iterdir()] == ["10-other.conf"]
    assert other.read_text() == "keep"


def test_clear_removes_empty_dir(tmp_path: Path) -> None:
    run(tmp_path, "gating", SINK, "notify")
    run(tmp_path, "clear", SINK)
    assert not (tmp_path / f"{SINK}.d").exists()


def test_clear_when_absent_is_ok(tmp_path: Path) -> None:
    run(tmp_path, "clear", SINK)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("unit", [CONTROLLER, "scdemo-controller"])
def test_waitport_on_controller_exits_2(
    tmp_path: Path, unit: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run(tmp_path, "gating", unit, "waitport")
    assert exc_info.value.code == 2
    assert "no listener" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_unknown_style_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run(tmp_path, "gating", SINK, "bogus")
    assert exc_info.value.code == 2
    assert "bogus" in capsys.readouterr().err


def test_fault_malformed_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run(tmp_path, "fault", SINK, "SCDEMO_FAULT_NO_READY")
    assert exc_info.value.code == 2
    assert capsys.readouterr().err.startswith("scdemo: ")


def test_fault_bad_key_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run(tmp_path, "fault", SINK, "PATH=x")
    assert exc_info.value.code == 2
    assert "PATH" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_fault_without_settings_exits_2(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run(tmp_path, "fault", SINK)
    assert exc_info.value.code == 2


def test_reload_runs_systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        calls.append((cmd, kwargs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    dropin.main(["--root", str(tmp_path), "gating", SINK, "none"])
    assert calls == [(["systemctl", "daemon-reload"], {"check": True})]


def test_no_reload_skips_systemctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("systemctl must not run")

    monkeypatch.setattr(subprocess, "run", boom)
    run(tmp_path, "gating", SINK, "none")


def test_missing_systemctl_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(SystemExit) as exc_info:
        dropin.main(["--root", str(tmp_path), "gating", SINK, "none"])
    assert exc_info.value.code == 2
    assert "systemctl" in capsys.readouterr().err


def test_failed_systemctl_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["systemctl", "daemon-reload"])

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(SystemExit) as exc_info:
        dropin.main(["--root", str(tmp_path), "clear", SINK])
    assert exc_info.value.code == 2
    assert "daemon-reload" in capsys.readouterr().err


def test_exit_on_config_error_keeps_arguments() -> None:
    seen: list[list[str]] = []

    @exit_on_config_error
    def main(argv: list[str]) -> None:
        seen.append(argv)
        raise ConfigError("x")

    with pytest.raises(SystemExit):
        main(["a"])
    assert seen == [["a"]]
