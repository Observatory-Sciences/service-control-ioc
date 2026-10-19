"""The full local stack: sinks, hwsim, relay and the controller together."""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator

import pytest
import zmq
import zmq.asyncio

from harness import ServiceProc, stack


async def _assert_clean_exit(proc: ServiceProc) -> None:
    """SIGTERM: exit 0 within 2 s, ``shutdown`` last, no stray output."""
    async with asyncio.timeout(2):
        assert await proc.terminate() == 0
    assert proc.events[-1]["event"] == "shutdown"
    assert proc.other_stdout == []
    assert proc.stderr_lines == []


def _count(proc: ServiceProc, event: str, **match: str) -> int:
    return sum(
        1
        for e in proc.events
        if e["event"] == event and all(e.get(k) == v for k, v in match.items())
    )


@pytest.mark.asyncio
async def test_full_stack_comes_up(port_base):
    async with stack(
        port_base
    ) as s:  # conftest helper: sink×2, hwsim, relay, controller in order
        await s.controller.wait_event("configured", timeout=15)
        await s.controller.wait_event("ready")
        for name in ("req", "dealer", "sub", "http"):
            await s.controller.wait_event("connected", name=name)
        await s.controller.wait_event("status", timeout=10)
        await s.sink1.wait_event("first_datagram")
        await s.sink2.wait_event("first_datagram")
        await _assert_clean_exit(s.controller)


@pytest.mark.asyncio
async def test_ignored_config_times_out_and_retries(port_base):
    async with stack(port_base, relay={"SCDEMO_FAULT_IGNORE_CONFIG": "1"}) as s:
        await s.controller.wait_event("config_timeout", timeout=10)
        await s.controller.wait_event("configuring", timeout=10)
        assert not any(e["event"] == "ready" for e in s.controller.events)
        timeout = await s.controller.wait_event("config_timeout", timeout=10)
        await s.controller.wait_event("configuring", timeout=10, after=timeout)
        assert not any(e["event"] == "ready" for e in s.controller.events)
        await _assert_clean_exit(s.controller)


@pytest.mark.asyncio
async def test_controller_reconfigures_after_relay_restart(port_base):
    async with stack(port_base) as s:
        first = await s.controller.wait_event("configured", timeout=15)
        http_up = await s.controller.wait_event("connected", name="http")
        await s.restart("relay")
        second = await s.controller.wait_event(
            "configured", timeout=20, after=first
        )  # match only events after `first`
        assert second["boot_id"] != first["boot_id"]
        down = await s.controller.wait_event("disconnected", name="http", after=http_up)
        await s.controller.wait_event("connected", name="http", after=down, timeout=10)
        await s.relay.wait_event("configured")
        assert _count(s.controller, "ready") == 1
        await _assert_clean_exit(s.controller)


@pytest.mark.asyncio
async def test_controller_starts_before_relay_and_configures_when_it_appears(
    port_base,
):
    async with ServiceProc("controller", port_base) as ctrl:
        await ctrl.wait_event("configuring")
        await ctrl.wait_event("config_timeout", timeout=10)
        async with (
            ServiceProc("sink", port_base, SCDEMO_INSTANCE="1"),
            ServiceProc("sink", port_base, SCDEMO_INSTANCE="2"),
            ServiceProc("relay", port_base) as relay,
        ):
            await relay.wait_event("ready")
            configured = await ctrl.wait_event("configured", timeout=15)
            await ctrl.wait_event("ready")
            await relay.wait_event("configured")
            status = await ctrl.wait_event("status", timeout=10)
            assert status["boot_id"] == configured["boot_id"]
            await _assert_clean_exit(ctrl)


@pytest.mark.asyncio
async def test_controller_sigterm_with_relay_present(port_base):
    async with stack(port_base) as s:
        await s.controller.wait_event("ready", timeout=15)
        await s.controller.wait_event("status", timeout=10)
        await _assert_clean_exit(s.controller)


@pytest.mark.asyncio
async def test_controller_sigterm_with_relay_absent(port_base):
    async with ServiceProc("controller", port_base) as ctrl:
        await ctrl.wait_event("configuring")
        for name in ("req", "dealer", "sub", "http"):
            await ctrl.wait_event("connecting", name=name)
        await _assert_clean_exit(ctrl)


@pytest.mark.asyncio
async def test_controller_sigterm_while_configure_pending(port_base):
    async with stack(port_base, relay={"SCDEMO_FAULT_IGNORE_CONFIG": "1"}) as s:
        await s.relay.wait_event("config_ignored", timeout=10)
        await _assert_clean_exit(s.controller)


@pytest.mark.asyncio
async def test_http_holds_one_connection_and_status_is_rate_limited(port_base):
    async with stack(port_base) as s:
        await s.controller.wait_event("ready", timeout=15)
        loop = asyncio.get_running_loop()
        first = await s.controller.wait_event("status", timeout=10)
        t_first = loop.time()
        second = await s.controller.wait_event("status", timeout=10, after=first)
        t_second = loop.time()
        await s.controller.wait_event("status", timeout=10, after=second)
        t_third = loop.time()
        # At most one status line every 5 s (pipe jitter allowed for).
        assert t_second - t_first >= 4.5
        assert t_third - t_second >= 4.5
        # ~10 s of 1 s polls went over one kept-alive HTTP connection.
        assert _count(s.relay, "connected", name="http-client") == 1
        assert _count(s.controller, "connected", name="http") == 1
        assert _count(s.controller, "disconnected", name="http") == 0
        await _assert_clean_exit(s.controller)


@contextlib.asynccontextmanager
async def _fake_relay(
    port_base: int,
) -> AsyncGenerator[tuple[zmq.asyncio.Socket, zmq.asyncio.Socket]]:
    """A REP and a ROUTER bound where the relay's would be."""
    ctx = zmq.asyncio.Context()
    ctx.linger = 0
    rep = ctx.socket(zmq.REP)
    router = ctx.socket(zmq.ROUTER)
    try:
        rep.bind(f"tcp://127.0.0.1:{port_base + 100}")
        router.bind(f"tcp://127.0.0.1:{port_base + 101}")
        yield rep, router
    finally:
        rep.close(linger=0)
        router.close(linger=0)
        ctx.term()


async def _answer_configures(rep: zmq.asyncio.Socket, seen: list[object]) -> None:
    while True:
        request = json.loads(await rep.recv())
        seen.append(request)
        reply = {"ok": True, "rate_hz": request["rate_hz"], "boot_id": "f" * 32}
        await rep.send(json.dumps(reply).encode())


@pytest.mark.asyncio
async def test_three_status_timeouts_trigger_reconfigure(port_base):
    """A ROUTER that never answers ``status``: after 3 timeouts, configure again."""
    seen: list[object] = []
    async with (
        _fake_relay(port_base) as (rep, _router),
        ServiceProc("controller", port_base, SCDEMO_RATE_HZ="7") as ctrl,
        asyncio.TaskGroup() as tg,
    ):
        answer = tg.create_task(_answer_configures(rep, seen))
        first = await ctrl.wait_event("configured", timeout=10)
        assert first["boot_id"] == "f" * 32
        again = await ctrl.wait_event("configuring", timeout=15, after=first)
        assert again["reason"] == "status_timeout"
        await ctrl.wait_event("configured", timeout=10, after=again)
        assert seen == [{"cmd": "configure", "rate_hz": 7}] * 2
        assert _count(ctrl, "ready") == 1
        await _assert_clean_exit(ctrl)
        answer.cancel()


@pytest.mark.asyncio
async def test_no_connect_req_never_configures(port_base):
    async with stack(port_base, controller={"SCDEMO_FAULT_NO_CONNECT": "req"}) as s:
        await s.controller.wait_event("connect_skipped", name="req")
        await s.controller.wait_event("connected", name="http", timeout=10)
        await s.controller.wait_event("status", timeout=10)
        assert _count(s.controller, "configuring") == 0
        assert _count(s.controller, "ready") == 0
        await _assert_clean_exit(s.controller)


@pytest.mark.asyncio
async def test_wrong_connect_dealer_uses_port_plus_1000(port_base):
    async with ServiceProc(
        "controller", port_base, SCDEMO_FAULT_WRONG_CONNECT="dealer"
    ) as ctrl:
        event = await ctrl.wait_event("connecting", name="dealer")
        assert event["port"] == str(port_base + 1101)
        await _assert_clean_exit(ctrl)


@pytest.mark.asyncio
async def test_controller_bad_env_exits_2(port_base):
    async with ServiceProc(
        "controller", port_base, SCDEMO_FAULT_NO_CONNECT="nope"
    ) as ctrl:
        assert await ctrl.wait_exit() == 2
        assert len(ctrl.stderr_lines) == 1
        assert "SCDEMO_FAULT_NO_CONNECT" in ctrl.stderr_lines[0]
