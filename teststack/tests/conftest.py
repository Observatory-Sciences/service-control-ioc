"""Shared fixtures. Harness classes live in ``harness.py`` (importlib mode cannot
``import conftest``), so put this directory on sys.path for ``from harness import``."""

import random
import socket
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

PORT_SPAN = 211  # offsets +0 .. +210


def _range_is_free(base: int) -> bool:
    with ExitStack() as stack:
        try:
            for port in range(base, base + PORT_SPAN):
                for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                    s = stack.enter_context(socket.socket(socket.AF_INET, kind))
                    s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@pytest.fixture
def port_base() -> int:
    """A random base in 30000-59000 whose whole +0..+210 range is free."""
    while True:
        base = random.randint(30000, 59000)
        if _range_is_free(base):
            return base
