"""Shared fixtures. Harness classes live in ``harness.py`` (importlib mode cannot
``import conftest``), so put this directory on sys.path for ``from harness import``."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from harness import find_port_base


@pytest.fixture
def port_base() -> int:
    """A random base in 30000-59000 whose whole +0..+210 range is free."""
    return find_port_base()
