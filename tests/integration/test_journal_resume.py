"""The journal follower resumes after its journalctl child dies, with no
duplicate and no missing lines (``--after-cursor`` together with ``-n``).

``RELAY:LOG`` (the newest whole lines of a 20-line ring that fit in 999 bytes,
ruling 19) heals a re-read, so it cannot show a resume bug alone. The test therefore also checks the mechanism: the new
child's argv carries ``-n`` and ``--after-cursor=C``, and running that argv
(without ``-f``) returns exactly the entries after ``C``, no backlog.

Run with ``QDSC_TARGET=container|vm uv run pytest -m container
tests/integration/test_journal_resume.py -v``. Both targets keep a volatile
journal (``/run/log/journal``, no ``/var/log/journal``); the test records that
as a property but does not require it.
"""

import json
import shlex
import time

import pytest

pytestmark = pytest.mark.container

P = "SCDEMO:SC"
RELAY_UNIT = "scdemo-relay.service"
LOG_PV = f"{P}:RELAY:LOG"
STAMP = 9  # the tool prefixes each line with "HH:MM:SS "
# The brackets keep pgrep from matching the shell that runs this very command.
PGREP = "pgrep -u {user} -f '[j]ournalctl -f' || true"
CURSOR = "--after-cursor="
LATE_LINES = 5  # lines that may land between the two journal reads
LOG_BYTES = 999  # ruling 19: what EDM's multiLineTextUpdate can show
MIN_RING = 5  # a relay start logs ~20 lines of < 100 bytes: >= 5 must be shown


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
    text = str(container.ca("get", LOG_PV)[LOG_PV])
    assert len(text.encode()) <= LOG_BYTES, f"RELAY:LOG has {len(text.encode())} bytes"
    return [line[STAMP:] for line in text.splitlines()]


def wait_ring_matches_journal(container, timeout: float = 30) -> list[str]:
    """Wait until the tool's ring is exactly the tail of the unit's journal."""
    deadline = time.monotonic() + timeout
    while True:
        truth = journal_messages(container)
        ring = tool_ring(container)
        # A short ring would make the tail comparison weak (ruling 19 cut it
        # from 20 lines to what fits in 999 bytes).
        if len(ring) >= MIN_RING and truth[-len(ring) :] == ring:
            return ring
        if time.monotonic() > deadline:
            pytest.fail(
                f"RELAY:LOG is not the journal tail after {timeout:g} s:\n"
                f"ring: {ring}\njournal tail: {truth[-len(ring or [0]) :]}"
            )
        time.sleep(0.5)


def wait_ready(container) -> None:
    waited = container.ca("wait", f"{P}:READY", "READY", "--timeout", "90")
    assert waited["ok"], waited


def count_starts(container) -> int:
    return sum("event=starting" in line for line in journal_messages(container))


def wait_starts(container, before: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while count_starts(container) == before:
        if time.monotonic() > deadline:
            pytest.fail("the relay did not log 'starting' after its restart")
        time.sleep(0.2)


def restart_relay(container) -> None:
    """Restart the relay and wait until it has logged a new ``starting`` line."""
    before = count_starts(container)
    container.sh(f"systemctl restart {RELAY_UNIT}", timeout=120)
    wait_starts(container, before)


def child_argv(container) -> list[str]:
    """The argv of the tool's one journalctl child."""
    out = container.sh(
        f"pgrep -a -u {container.user} -f '[j]ournalctl -f' || true"
    ).splitlines()
    assert len(out) == 1, out
    return shlex.split(out[0].split(maxsplit=1)[1])


def cursors(container, argv: list[str]) -> list[str]:
    """Run ``argv`` once on the target, as the tool's user; the entries' cursors."""
    out = container.sh(f"runuser -u {container.user} -- {shlex.join(argv)}")
    return [json.loads(line)["__CURSOR"] for line in out.splitlines() if line]


def check_resume_argv(container, argv: list[str]) -> None:
    """The resumed follower asks for ``-n N --after-cursor=C`` and gets just
    the entries after ``C``."""
    assert "-n" in argv, argv
    given = [a for a in argv if a.startswith(CURSOR)]
    assert len(given) == 1, f"the resumed journalctl has no {CURSOR}C: {argv}"
    (given,) = given
    cursor = given.removeprefix(CURSOR)
    once = [a for a in argv if a != "-f"]
    resumed = cursors(container, once)
    # Everything the journal holds for the same units, in order.
    base = [a for a in once if not a.startswith(CURSOR)]
    base[base.index("-n") + 1] = "all"
    everything = cursors(container, base)
    assert cursor in everything, "the cursor the follower resumed from is unknown"
    after = everything[everything.index(cursor) + 1 :]
    assert after, "nothing was logged after the resume cursor (no gap to cover)"
    # Lines can still arrive between the two reads, so `after` may be longer;
    # a re-read backlog or a lost line would break the prefix instead.
    late = len(after) - len(resumed)
    detail = (
        f"{len(resumed)} entries after --after-cursor, journal has {len(after)}"
        f" after it ({len(everything)} in all); the backlog was re-read or lines"
        " were lost"
    )
    assert 0 <= late <= LATE_LINES, detail
    assert resumed == after[: len(resumed)], detail


def test_journal_follower_resumes_without_duplicates(
    container, record_property
) -> None:
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
    wait_ready(container)

    # Recorded, not required (Task 0 found both targets volatile).
    persistent = container.sh("test -d /var/log/journal && echo yes || echo no")
    record_property("journal_persistent", persistent.strip())

    starts_before = count_starts(container)  # the journal holds earlier runs too
    first = child_pids(container)
    assert len(first) == 1, first

    restart_relay(container)
    wait_ready(container)
    wait_ring_matches_journal(container)

    # Kill the child and restart the relay inside the follower's backoff
    # window, so the lines logged while no journalctl runs are a real gap.
    before = count_starts(container)
    container.sh(
        f"pkill -u {container.user} -f '[j]ournalctl -f'\n"
        f"systemctl restart --no-block {RELAY_UNIT}"
    )
    second = wait_new_child(container, first)
    assert len(second) == 1, second
    wait_starts(container, before)
    wait_ready(container)
    wait_ring_matches_journal(container)
    check_resume_argv(container, child_argv(container))

    restart_relay(container)
    wait_ready(container)
    wait_ring_matches_journal(container)

    assert count_starts(container) == starts_before + 3, "three restarts"
