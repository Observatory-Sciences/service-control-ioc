import socket
import tempfile
from pathlib import Path

from scdemo.notify import sd_notify


def test_sd_notify_sends_to_socket() -> None:
    # pytest's tmp_path exceeds the AF_UNIX path limit on macOS, so use a short dir.
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        path = Path(tmp) / "n.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as rx:
            rx.bind(str(path))
            assert sd_notify("READY=1", {"NOTIFY_SOCKET": str(path)}) is True
            assert rx.recv(64) == b"READY=1"


def test_sd_notify_without_socket_is_noop() -> None:
    assert sd_notify("READY=1", {}) is False
