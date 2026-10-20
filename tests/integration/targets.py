"""Where the integration tests run: the Rocky 9 container or the test VM.

``target_from_env()`` picks one from ``QDSC_TARGET`` (``container`` or ``vm``;
the legacy ``QDSC_CONTAINER=1`` means container). A target runs bash as root,
prepares itself (sync and install the test stack) and copies files back.
"""

import os
import shlex
import subprocess
from pathlib import Path
from typing import Final, Protocol

REPO: Final = Path(__file__).resolve().parents[2]
RD: Final = REPO / "teststack" / "container" / "rd.sh"
DOCKER: Final = os.environ.get("DOCKER", str(Path.home() / ".rd" / "bin" / "docker"))
CONTAINER_NAME: Final = "scdemo-el9"
RSYNC_EXCLUDES: Final = (".venv", ".git", "__pycache__", ".ruff_cache")
SSH_UNREACHABLE: Final = 255
ROOT_PATH: Final = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LOGIN_USER: Final = "vmuser"  # owns the repo copy on the VM


class ShellError(AssertionError):
    """A command on the target exited non-zero, or the target is unreachable."""


class Target(Protocol):
    name: str  # "container" | "vm"
    user: str  # the account the tool and the stack run as
    src: str  # the repo path on the target

    def sh(self, cmd: str, timeout: float = 120) -> str:
        """Run ``cmd`` with bash as root; raise ``ShellError`` on non-zero."""
        ...

    def prepare(self) -> None:
        """Bring the target up and install the test stack."""
        ...

    def fetch(self, remote: str, local: Path) -> None:
        """Copy a file from the target to the Mac."""
        ...


def _run(
    argv: list[str], timeout: float, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv``; stdin is empty unless ``stdin_text`` is given."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=stdin_text,
        stdin=None if stdin_text is not None else subprocess.DEVNULL,
    )


def _failure(argv: list[str], proc: subprocess.CompletedProcess[str]) -> ShellError:
    return ShellError(
        f"{shlex.join(argv)} exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


class ContainerTarget:
    """The plan 1 container, driven through ``teststack/container/rd.sh``."""

    name: str = "container"
    user: str = "scdemo"
    src: str = "/src"

    def _rd(self, *args: str, timeout: float = 600) -> str:
        argv = [str(RD), *args]
        proc = _run(argv, timeout)
        if proc.returncode != 0:
            raise _failure(argv, proc)
        return proc.stdout

    def sh(self, cmd: str, timeout: float = 120) -> str:
        return self._rd("shell", "-c", cmd, timeout=timeout)

    def prepare(self) -> None:
        self._rd("up")
        self._rd("install")

    def fetch(self, remote: str, local: Path) -> None:
        argv = [DOCKER, "cp", f"{CONTAINER_NAME}:{remote}", str(local)]
        proc = _run(argv, 120)
        if proc.returncode != 0:
            raise _failure(argv, proc)


class VmTarget:
    """The dedicated test VM, over a multiplexed ssh connection with sudo."""

    HOST: Final = "test-vm"
    SSH_OPTS: Final = (
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPath=~/.ssh/cm-%C",
        "-o",
        "ControlPersist=15m",
        "-o",
        "ConnectTimeout=20",
    )
    name: str = "vm"
    user: str = "xspress"
    src: str = f"/home/{LOGIN_USER}/qd-service-control"

    def _ssh(
        self, remote_cmd: str, timeout: float, stdin_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        argv = ["ssh", *self.SSH_OPTS, self.HOST, remote_cmd]
        try:
            proc = _run(argv, timeout, stdin_text)
        except subprocess.TimeoutExpired as err:
            raise ShellError(f"VM {self.HOST} unreachable: {err}") from err
        if proc.returncode == SSH_UNREACHABLE:
            raise ShellError(f"VM {self.HOST} unreachable: {proc.stderr.strip()}")
        return proc

    def sh(self, cmd: str, timeout: float = 120) -> str:
        # sudo's secure_path (EL9) omits /usr/local/bin, where scdemo-dropin and
        # uv live, so put it back as root's login PATH has it.
        remote = f"sudo -n env PATH={ROOT_PATH} bash -c {shlex.quote(cmd)}"
        proc = self._ssh(remote, timeout)
        if proc.returncode != 0:
            raise _failure(["ssh", self.HOST, remote], proc)
        return proc.stdout

    def prepare(self) -> None:
        # prepare.sh first: it installs rsync, which the sync below needs.
        script = (REPO / "teststack" / "vm" / "prepare.sh").read_text()
        proc = self._ssh(f"sudo -n bash -s -- {shlex.quote(LOGIN_USER)}", 900, script)
        if proc.returncode != 0:
            raise _failure(["ssh", self.HOST, "prepare.sh"], proc)
        excludes = [arg for ex in RSYNC_EXCLUDES for arg in ("--exclude", ex)]
        argv = [
            "rsync",
            "-az",
            "--delete",
            *excludes,
            "-e",
            shlex.join(["ssh", *self.SSH_OPTS]),
            f"{REPO}/",
            # Absolute, so it does not depend on the ssh login user's home.
            f"{self.HOST}:{self.src}/",
        ]
        try:
            proc = _run(argv, 600)
        except subprocess.TimeoutExpired as err:
            raise ShellError(f"VM {self.HOST} unreachable: {err}") from err
        if proc.returncode != 0:
            if proc.returncode == SSH_UNREACHABLE:
                raise ShellError(f"VM {self.HOST} unreachable: {proc.stderr.strip()}")
            raise _failure(argv, proc)
        self.sh(
            f"bash {self.src}/teststack/install.sh --user {self.user} {self.src}",
            timeout=1800,
        )

    def fetch(self, remote: str, local: Path) -> None:
        # Binary-safe, and root-readable: stream the bytes of `sudo cat`.
        argv = [
            "ssh",
            *self.SSH_OPTS,
            self.HOST,
            f"sudo -n cat {shlex.quote(remote)}",
        ]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=120,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as err:
            raise ShellError(f"VM {self.HOST} unreachable: {err}") from err
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            if proc.returncode == SSH_UNREACHABLE:
                raise ShellError(f"VM {self.HOST} unreachable: {stderr}")
            raise ShellError(f"{shlex.join(argv)} exited {proc.returncode}: {stderr}")
        local.write_bytes(proc.stdout)


def target_from_env() -> Target | None:
    """The target ``QDSC_TARGET`` names, or None (tests then skip)."""
    choice = os.environ.get("QDSC_TARGET")
    if choice is None and os.environ.get("QDSC_CONTAINER") == "1":
        choice = "container"
    if choice is None or choice == "":
        return None
    if choice == "container":
        return ContainerTarget()
    if choice == "vm":
        return VmTarget()
    raise ValueError(f"QDSC_TARGET={choice!r}: expected 'container' or 'vm'")
