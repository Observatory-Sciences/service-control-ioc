"""The acceptance scenarios (``scenarios.SCENARIOS``) on a real systemd target.

Run with ``SCIOC_TARGET=container uv run pytest -m container
tests/integration/test_scenarios.py -v`` (or ``SCIOC_TARGET=vm``). Each run
writes ``$SCIOC_EVIDENCE/<target>/<id>.json`` (default
``docs/acceptance/evidence``); ``summarise.py`` turns those into tables.
"""

import pytest

from integration.evidence import evidence_dir
from integration.scenarios import SCENARIOS, Scenario, run_scenario

pytestmark = pytest.mark.container


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario(container_session, sc: Scenario) -> None:
    # The runner clears the target itself and asserts it is clean first.
    result = run_scenario(container_session, sc, evidence_dir())
    assert result.passed, (
        f"{sc.id} ({sc.title}) failed, evidence {result.evidence}:\n"
        + "\n".join(result.failures)
    )
