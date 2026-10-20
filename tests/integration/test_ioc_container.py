"""The soft IOC against the real scdemo stack in the Rocky 9 systemd container.

Run with ``QDSC_TARGET=container uv run pytest -m container -v``. The tool runs as
``scdemo`` (the transient ``qdsc-dev`` unit, or ``scdemo-sc.service``), so its
unit jobs go through the polkit rule that ``install.sh`` generates.
"""

import time
from collections.abc import Sequence

import pytest

pytestmark = pytest.mark.container

Event = dict[str, object]
P = "SCDEMO:SC"
JOB_TIMEOUT = "150"


def texts(events: Sequence[Event]) -> list[str]:
    return [str(e.get("text")) for e in events]


def stack_news(events: Sequence[Event]) -> list[str]:
    return [str(e.get("new")) for e in events if e.get("kind") == "stack"]


def faults(events: Sequence[Event]) -> list[str]:
    return [t for t in texts(events) if t.startswith("FAULT ")]


def start_tool_and_wait_stopped(container) -> None:
    container.start_tool()
    container.wait_for(
        lambda ev: "stopped" in stack_news(ev), 30, "initial 'stack stopped' event"
    )


def test_every_pv_served_in_container(container) -> None:
    start_tool_and_wait_stopped(container)
    names = (
        f"/opt/qd-service-control/venv/bin/qd-service-control check-config --pvs"
        f" {container.target.src}/teststack/config/scdemo.yaml | grep '^{P}:'"
    )
    count = int(container.sh(f"{names} | wc -l").strip())
    assert count == 155
    assert container.ca("connect", "-", stdin_cmd=names) == []


def test_start_stop_restart_through_pvs(container) -> None:
    container.gating(sinks="waitport", relay="waitport", controller="notify")
    start_tool_and_wait_stopped(container)

    # Start the stack from stopped.
    put = container.ca("put", f"{P}:START", "1", "--wait", "--timeout", JOB_TIMEOUT)
    assert put["ok"], put
    assert container.get(f"{P}:LASTJOB") == "start: done"
    waited = container.ca("wait", f"{P}:STATE", "ready", "--timeout", "60")
    assert waited["ok"], waited
    assert container.get_int(f"{P}:READY") == 1

    # Restart one unit: the job is done and nothing is reported as a fault.
    before = len(container.tool_events())
    put = container.ca(
        "put", f"{P}:RELAY:RESTART", "1", "--wait", "--timeout", JOB_TIMEOUT
    )
    assert put["ok"], put
    assert container.get(f"{P}:RELAY:LASTJOB") == "restart: done"
    waited = container.ca("wait", f"{P}:STATE", "ready", "--timeout", "60")
    assert waited["ok"], waited
    restart_events = container.tool_events()[before:]
    assert not faults(restart_events), "\n".join(texts(restart_events))

    # Stop the stack: stopping, then stopped, with no starting between.
    before = len(container.tool_events())
    put = container.ca("put", f"{P}:STOP", "1", "--wait", "--timeout", JOB_TIMEOUT)
    assert put["ok"], put
    assert container.get(f"{P}:LASTJOB") == "stop: done"
    events = container.wait_for(
        lambda ev: "stopped" in stack_news(ev[before:]), 60, "'stack stopped'"
    )[before:]
    news = stack_news(events)
    assert news[: news.index("stopped") + 1] == ["stopping", "stopped"], news
    assert not faults(events), "\n".join(texts(events))
    assert container.get(f"{P}:STATE") == "stopped"


def test_polkit_denial_is_job_failed_finding(container) -> None:
    start_tool_and_wait_stopped(container)
    try:
        container.remove_rule()
        put = container.ca(
            "put", f"{P}:SINK1:RESTART", "1", "--wait", "--timeout", JOB_TIMEOUT
        )
        assert put["ok"], put  # the write completes; the job did not
        assert container.get(f"{P}:SINK1:LASTJOB").startswith("restart: error")
        assert container.get_int(f"{P}:FAULTS") >= 1
        container.wait_for(
            lambda ev: any(t.startswith("FAULT job_failed sink@1") for t in texts(ev)),
            10,
            "'FAULT job_failed sink@1'",
        )

        container.restore_rule()
        put = container.ca(
            "put", f"{P}:SINK1:RESTART", "1", "--wait", "--timeout", JOB_TIMEOUT
        )
        assert put["ok"], put
        assert container.get(f"{P}:SINK1:LASTJOB") == "restart: done"
        container.wait_for(
            lambda ev: any(
                t.startswith("CLEARED job_failed sink@1") for t in texts(ev)
            ),
            10,
            "'CLEARED job_failed sink@1'",
        )
    finally:
        container.restore_rule()


def test_tool_unit_restarts_on_failure(container) -> None:
    show = container.sh("systemctl show -p Restart,User scdemo-sc.service")
    assert "Restart=on-failure" in show.splitlines()
    assert f"User={container.user}" in show.splitlines()
    try:
        container.sh("systemctl start scdemo-sc.service")
        waited = container.ca("wait", f"{P}:STATE", "stopped", "--timeout", "30")
        assert waited["ok"], waited
        pid = container.sh(
            "systemctl show -p MainPID --value scdemo-sc.service"
        ).strip()
        assert pid not in {"", "0"}
        container.sh(f"kill -9 {pid}")

        deadline = time.monotonic() + 30
        while True:
            restarts = container.sh(
                "systemctl show -p NRestarts --value scdemo-sc.service"
            ).strip()
            if restarts == "1":
                break
            if time.monotonic() > deadline:
                pytest.fail(f"scdemo-sc.service NRestarts={restarts} after 30 s")
            time.sleep(0.5)
        assert container.ca("connect", f"{P}:STATE", "--timeout", "30") == []
        new_pid = container.sh(
            "systemctl show -p MainPID --value scdemo-sc.service"
        ).strip()
        assert new_pid not in {"", "0", pid}
    finally:
        container.sh(
            "systemctl stop scdemo-sc.service\n"
            "systemctl reset-failed scdemo-sc.service 2>/dev/null || true"
        )
