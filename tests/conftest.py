"""Shared test setup. Test doubles live in ``fakes.py`` (importlib mode cannot
``import conftest``), so put this directory on sys.path for ``from fakes import``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
