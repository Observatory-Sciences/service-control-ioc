"""The integration targets: environment selection and the VM's ssh/rsync argv.

These run on the Mac with ``subprocess.run`` faked, so they need no target.
"""

import subprocess
from pathlib import Path

import pytest

from integration.targets import (
    ContainerTarget,
    ShellError,
    VmTarget,
    target_from_env,
)

Call = list[str]


class FakeRun:
    """Stands in for ``subprocess.run``: records argv, returns canned results."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[Call] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(
        self, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, self.returncode, self.stdout, self.stderr
        )


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"QDSC_TARGET": "container"}, ContainerTarget),
        ({"QDSC_TARGET": "vm"}, VmTarget),
        ({"QDSC_CONTAINER": "1"}, ContainerTarget),
        ({}, None),
        ({"QDSC_CONTAINER": "0"}, None),
    ],
)
def test_target_from_env(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: type | None
) -> None:
    monkeypatch.delenv("QDSC_TARGET", raising=False)
    monkeypatch.delenv("QDSC_CONTAINER", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    target = target_from_env()
    if expected is None:
        assert target is None
    else:
        assert isinstance(target, expected)


def test_target_from_env_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDSC_TARGET", "mainframe")
    with pytest.raises(ValueError, match="mainframe"):
        target_from_env()


def test_target_attributes() -> None:
    assert (ContainerTarget.name, ContainerTarget.user, ContainerTarget.src) == (
        "container",
        "scdemo",
        "/src",
    )
    assert (VmTarget.name, VmTarget.user, VmTarget.src) == (
        "vm",
        "xspress",
        "/home/vmuser/qd-service-control",
    )


def test_vm_target_sh_uses_mux_and_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(stdout="ok\n")
    monkeypatch.setattr(subprocess, "run", fake)
    assert VmTarget().sh("echo 'hi there'") == "ok\n"
    assert fake.calls == [
        [
            "ssh",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPath=~/.ssh/cm-%C",
            "-o",
            "ControlPersist=15m",
            "-o",
            "ConnectTimeout=20",
            "test-vm",
            "sudo -n bash -c 'echo '\"'\"'hi there'\"'\"''",
        ]
    ]


def test_vm_target_ssh_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", FakeRun(255, stderr="timed out during banner")
    )
    with pytest.raises(
        ShellError, match="VM test-vm unreachable: timed out during banner"
    ):
        VmTarget().sh("true")


def test_vm_command_failure_is_not_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeRun(3, stdout="out", stderr="bad"))
    with pytest.raises(ShellError, match="exited 3") as info:
        VmTarget().sh("false")
    assert "unreachable" not in str(info.value)


def test_vm_prepare_rsync_excludes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    VmTarget().prepare()
    rsync = fake.calls[0]
    assert rsync[0] == "rsync"
    excludes = [rsync[i + 1] for i, arg in enumerate(rsync) if arg == "--exclude"]
    assert {".venv", ".git", "__pycache__", ".ruff_cache"} <= set(excludes)
    assert "--delete" in rsync
    assert rsync[-1] == "test-vm:qd-service-control/"
    assert Path(rsync[-2]).name == "qd-service-control"
    install = fake.calls[1]
    assert install[-1].endswith(
        "bash /home/vmuser/qd-service-control/teststack/install.sh --user xspress"
        " /home/vmuser/qd-service-control'"
    )
