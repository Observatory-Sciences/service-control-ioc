"""Fixtures for the integration tests (``-m container``).

They run against a real systemd target chosen by ``SCIOC_TARGET`` (``container``,
the plan 1 Rocky 9 container, or ``vm``, the test VM at ``SCIOC_VM_HOST`` logged
into as ``SCIOC_VM_USER``; see ``targets.py``) and
run ``service-control-ioc`` there as the transient unit ``scioc-dev``, against
real systemd, D-Bus, /proc and journald. The tool serves its IOC there too, on
the loopback CA of ``/etc/scdemo/ca.env``; ``Host.ca`` reads and writes its PVs
with ``ca_client.py`` on the target. Skipped unless a target is selected.
"""

import json
import shlex
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

import pytest

from integration.targets import ShellError, Target, target_from_env

__all__ = ["ShellError"]

TOOL_UNIT: Final = "scioc-dev"
SC_UNIT: Final = "scdemo-sc.service"
CA_ENV: Final = "/etc/scdemo/ca.env"
CA_PYTHON: Final = "/opt/scdemo/ca-venv/bin/python"
TOOL_BIN_DIR: Final = "/opt/service-control-ioc/venv/bin"
TOOL_BIN: Final = f"{TOOL_BIN_DIR}/service-control-ioc"
CONFIG: Final = "/etc/scdemo/scdemo-sc.yaml"
RULE: Final = "/etc/polkit-1/rules.d/40-scdemo-sc.rules"
TOOL_CMD: Final = f"{TOOL_BIN} {CONFIG} --log-format json"
STACK_UNITS: Final = (
    "scdemo-all.service",
    "scdemo-controller.service",
    "scdemo-relay.service",
    "scdemo-sink@1.service",
    "scdemo-sink@2.service",
)
DROPIN_UNITS: Final = (
    "scdemo-sink@.service",
    "scdemo-sink@1.service",
    "scdemo-sink@2.service",
    "scdemo-relay.service",
    "scdemo-controller.service",
)

Event = dict[str, object]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if target_from_env() is not None:
        return
    skip = pytest.mark.skip(reason="integration tests need SCIOC_TARGET=container|vm")
    for item in items:
        if "container" in item.keywords:
            item.add_marker(skip)


@dataclass
class Host:
    """The prepared target, with helpers to drive the stack and the tool."""

    target: Target
    invocation: str | None = None

    @property
    def user(self) -> str:
        return self.target.user

    def sh(self, cmd: str, timeout: float = 120) -> str:
        """Run ``cmd`` with bash on the target as root; raise on non-zero."""
        return self.target.sh(cmd, timeout=timeout)

    # -- the stack ------------------------------------------------------------

    def gating(self, sinks: str, relay: str, controller: str) -> None:
        self.sh(
            "set -e\n"
            f"scdemo-dropin --no-reload gating scdemo-sink@.service {sinks}\n"
            f"scdemo-dropin --no-reload gating scdemo-relay.service {relay}\n"
            f"scdemo-dropin --no-reload gating scdemo-controller.service {controller}\n"
            "systemctl daemon-reload"
        )

    def fault(self, unit: str, *settings: str) -> None:
        self.sh(f"scdemo-dropin fault {unit} {shlex.join(settings)}")

    def start_stack(self, *, block: bool = False) -> None:
        flag = "" if block else "--no-block "
        self.sh(f"systemctl start {flag}scdemo-all.service", timeout=180)

    def stop_stack(self) -> None:
        self.sh(f"systemctl stop {' '.join(STACK_UNITS)}", timeout=180)
        self.sh("systemctl reset-failed 'scdemo-*' || true")

    def clear(self) -> None:
        """Stop the tool (both units) and the stack, and remove every drop-in."""
        self.stop_tool()
        self.sh(
            f"systemctl stop {SC_UNIT} 2>/dev/null || true\n"
            f"systemctl reset-failed {SC_UNIT} 2>/dev/null || true"
        )
        clears = "\n".join(
            f"scdemo-dropin --no-reload clear {u} || rc=1" for u in DROPIN_UNITS
        )
        self.sh(f"rc=0\n{clears}\nsystemctl daemon-reload || rc=1\nexit $rc")
        self.stop_stack()

    # -- the tool -------------------------------------------------------------

    def start_tool(self) -> None:
        """Run the tool as the transient unit ``scioc-dev`` (the target's user)."""
        self.stop_tool()
        self.sh(
            f"systemd-run --quiet --unit={TOOL_UNIT}"
            f" --uid={self.user}"
            " -p SupplementaryGroups=systemd-journal"
            f" -p EnvironmentFile={CA_ENV}"
            f" -p Environment=PYTHONUNBUFFERED=1 {TOOL_CMD}"
        )
        self.invocation = self.sh(
            f"systemctl show -p InvocationID --value {TOOL_UNIT}"
        ).strip()
        assert self.invocation, "scioc-dev has no invocation ID"

    def stop_tool(self) -> None:
        self.sh(
            f"systemctl stop {TOOL_UNIT} 2>/dev/null || true\n"
            f"systemctl reset-failed {TOOL_UNIT} 2>/dev/null || true"
        )

    def tool_lines(self, invocation: str | None = None) -> list[str]:
        """Every stdout line of the current ``scioc-dev`` run (or ``invocation``)."""
        invocation = invocation or self.invocation
        assert invocation, "start_tool() first"
        out = self.sh(
            f"journalctl --no-pager -o cat _SYSTEMD_INVOCATION_ID={invocation}"
        )
        return out.splitlines()

    def tool_events(self, invocation: str | None = None) -> list[Event]:
        """The current run's (or ``invocation``'s) JSON log lines that parse as
        objects."""
        events: list[Event] = []
        for line in self.tool_lines(invocation):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def wait_for(
        self,
        pred: Callable[[list[Event]], bool],
        timeout: float,
        what: str,
    ) -> list[Event]:
        """Poll the tool's events until ``pred`` holds; fail after ``timeout`` s."""
        deadline = time.monotonic() + timeout
        while True:
            events = self.tool_events()
            if pred(events):
                return events
            if time.monotonic() > deadline:
                text = "\n".join(str(e.get("text")) for e in events)
                pytest.fail(f"no {what} within {timeout:g} s; events:\n{text}")
            time.sleep(0.5)

    # -- facts about the target -------------------------------------------------

    def unit_states(self, units: Sequence[str]) -> dict[str, str]:
        """``ActiveState`` of each unit (``systemctl is-active``)."""
        out = self.sh(f"systemctl is-active {shlex.join(units)} || true")
        states = out.split()
        assert len(states) == len(units), f"systemctl is-active gave {out!r}"
        return dict(zip(units, states, strict=True))

    def arch(self) -> str:
        return self.sh("uname -m").strip()

    def now(self) -> float:
        """The target's ``time.time()``, the clock of the tool's event ``ts``."""
        return float(self.sh("date +%s.%N").strip())

    # -- Channel Access (ca_client.py) ----------------------------------------

    def ca(self, *args: str, stdin_cmd: str | None = None, check: bool = True) -> Any:
        """Run ``ca_client.py *args`` in the container and parse its JSON.

        ``stdin_cmd`` is a shell command whose output is piped to the client
        (give ``-`` as the PV). ``check=False`` returns the JSON of a failed
        ``put`` or ``wait`` instead of raising.
        """
        pipe = f"{stdin_cmd} | " if stdin_cmd else ""
        tail = "" if check else " || true"
        client = f"{CA_PYTHON} {self.target.src}/tests/integration/ca_client.py"
        out = self.sh(
            f"set -o pipefail; set -a; . {CA_ENV}; set +a\n{pipe}{client} {shlex.join(args)}{tail}",
            timeout=300,
        )
        return json.loads(out.strip().splitlines()[-1])

    def get(self, pv: str) -> str:
        """The value of ``pv`` as text (``ca_client.py get``)."""
        values = self.ca("get", pv)
        return str(values[pv])

    def get_int(self, pv: str) -> int:
        """The value of ``pv`` as a number (``ca_client.py get --int``)."""
        values = self.ca("get", "--int", pv)
        return int(values[pv])

    # -- the polkit rule --------------------------------------------------------

    def _polkit_loads(self) -> list[str]:
        out = self.sh(
            "journalctl --no-pager -o cat -u polkit.service"
            " | grep 'Finished loading, compiling and executing' || true"
        )
        return out.splitlines()

    def _wait_polkit_reload(self, before: int, rules: int, timeout: float = 30) -> None:
        """Wait until polkitd has reloaded (a load line after the first
        ``before``) and now runs ``rules`` rules."""
        deadline = time.monotonic() + timeout
        while True:
            loads = self._polkit_loads()
            if len(loads) > before and loads[-1].endswith(f"executing {rules} rules"):
                return
            if time.monotonic() > deadline:
                pytest.fail(f"polkitd did not reload to {rules} rules: {loads[-3:]}")
            time.sleep(0.5)

    def _rule_count(self) -> int:
        out = self.sh(
            "ls /etc/polkit-1/rules.d/*.rules /usr/share/polkit-1/rules.d/*.rules"
            " 2>/dev/null | wc -l"
        )
        return int(out.strip())

    def remove_rule(self) -> None:
        """Remove the tool's polkit rule and wait for polkitd to drop it."""
        before = len(self._polkit_loads())
        self.sh(f"rm -f {RULE}")
        self._wait_polkit_reload(before, self._rule_count())

    def restore_rule(self) -> None:
        """Regenerate the rule as install.sh does, and wait for polkitd to load
        it. Does nothing if the rule is already there."""
        if self.sh(f"test -f {RULE} && echo yes || true").strip() == "yes":
            return
        before = len(self._polkit_loads())
        self.sh(
            "set -e\n"
            f"{TOOL_BIN_DIR}/service-control-generate-polkit {CONFIG} --user {self.user}"
            " > /root/40-scdemo-sc.rules\n"
            f"install -m 0644 /root/40-scdemo-sc.rules {RULE}"
        )
        self._wait_polkit_reload(before, self._rule_count())


@pytest.fixture(scope="session")
def container_session() -> Host:
    target = target_from_env()
    if target is None:
        pytest.skip("integration tests need SCIOC_TARGET=container|vm")
    target.prepare()
    return Host(target)


@pytest.fixture
def container(container_session: Host) -> Iterator[Host]:
    """The target with the stack and tool stopped and no drop-ins, before and
    after each test."""
    container_session.clear()
    yield container_session
    container_session.clear()
    container_session.invocation = None
