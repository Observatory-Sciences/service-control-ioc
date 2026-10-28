from pathlib import Path

import pytest

from service_control_ioc.config.load import load_config
from service_control_ioc.config.spec import StackSpec

REPO = Path(__file__).resolve().parents[2]
XSPRESS = REPO / "tests/fixtures/xspress-4card.yaml"


@pytest.fixture(scope="module")
def xspress() -> StackSpec:
    return load_config(XSPRESS)


@pytest.fixture(scope="module")
def xspress_text() -> str:
    return XSPRESS.read_text(encoding="utf-8")
