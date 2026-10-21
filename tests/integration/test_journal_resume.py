"""The journal follower resumes after its journalctl child dies, with no
duplicate and no missing lines (``--after-cursor`` together with ``-n``).

Run with ``QDSC_TARGET=container|vm uv run pytest -m container
tests/integration/test_journal_resume.py -v``. Both targets keep a volatile
journal (``/run/log/journal``, no ``/var/log/journal``), so this is checked on
a volatile journal.
"""

import time

import pytest

pytestmark = pytest.mark.container

P = "SCDEMO:SC"
RELAY_UNIT = "scdemo-relay.service"
LOG_PV = f"{P}:RELAY:LOG"
STAMP = 9  # the tool prefixes each line with "HH:MM:SS "
# The brackets keep pgrep from matching the shell that runs this very command.
PGREP = "pgrep -u {user} -f '[j]ournalctl -f' || true"


def child_pids(container) -> set[int]:
    out = container.sh(PGREP.format(user=container.user))
    return {int(p) for p in out.split()}


def wait_new_child(container, old: set[int], timeout: float = 30) -> set[int]:
    deadline = time.monotonic() + timeout
    while True:
        now = child_pids(container)
        if now and not now & old:
            return now
        if time.monotonic() > deadline:
            pytest.fail(f"no new journalctl child within {timeout:g} s: {old} -> {now}")
        time.sleep(0.2)


def journal_messages(container) -> list[str]:
    out = container.sh(
        f"journalctl --no-pager -o cat -u {RELAY_UNIT}",
    )
    return out.splitlines()


def tool_ring(container) -> list[str]:
    """RELAY:LOG without the time prefix, one message per line."""
    text = container.ca("get", LOG_PV)[LOG_PV]
    return [line[STAMP:] for line in str(text).splitlines()]


def wait_ring_matches_journal(container, timeout: float = 30) -> list[str]:
    """Wait until the tool's ring is exactly the tail of the unit's journal."""
    deadline = time.monotonic() + timeout
    while True:
        truth = journal_messages(container)
        ring = tool_ring(container)
        if ring and truth[-len(ring) :] == ring:
            return ring
        if time.monotonic() > deadline:
            pytest.fail(
                f"RELAY:LOG is not the journal tail after {timeout:g} s:\n"
                f"ring: {ring}\njournal tail: {truth[-len(ring or [0]) :]}"
            )
        time.sleep(0.5)


def restart_relay(container, timeout: float = 30) -> None:
    """Restart the relay and wait until it has logged a new ``starting`` line.

    RELAY:LOG holds 20 lines and a relay start writes about 30, so the line is
    only in the ring while the relay is still starting, and the tool publishes
    too rarely to catch it; the journal is the witness for "in order".
    """
    before = count_starts(container)
    container.sh(f"systemctl restart {RELAY_UNIT}", timeout=120)
    deadline = time.monotonic() + timeout
    while count_starts(container) == before:
        if time.monotonic() > deadline:
            pytest.fail("the relay did not log 'starting' after its restart")
        time.sleep(0.2)


def count_starts(container) -> int:
    return sum("event=starting" in line for line in journal_messages(container))


def test_journal_follower_resumes_without_duplicates(container) -> None:
    container.gating(sinks="waitport", relay="waitport", controller="notify")
    container.start_tool()
    container.wait_for(
        lambda ev: any(
            e.get("kind") == "stack" and e.get("new") == "stopped" for e in ev
        ),
        30,
        "initial 'stack stopped' event",
    )
    container.start_stack()
    waited = container.ca("wait", f"{P}:READY", "READY", "--timeout", "90")
    assert waited["ok"], waited

    # Volatile journal on both targets (Task 0, spec 6.6).
    persistent = container.sh("test -d /var/log/journal && echo yes || echo no")
    assert persistent.strip() == "no"

    starts_before = count_starts(container)  # the journal holds earlier runs too
    first = child_pids(container)
    assert len(first) == 1, first

    restart_relay(container)
    container.ca("wait", f"{P}:READY", "READY", "--timeout", "90")
    wait_ring_matches_journal(container)

    container.sh(f"pkill -u {container.user} -f '[j]ournalctl -f'")
    second = wait_new_child(container, first)
    assert len(second) == 1, second

    restart_relay(container)
    container.ca("wait", f"{P}:READY", "READY", "--timeout", "90")
    wait_ring_matches_journal(container)

    assert count_starts(container) == starts_before + 2, "two restarts"
    # No duplicates and no gaps across the resume: the ring is exactly the
    # journal's tail after each restart.
