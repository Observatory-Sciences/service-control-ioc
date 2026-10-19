import asyncio
import contextlib
import json
import socket
import struct
from collections.abc import AsyncGenerator

import pytest
import zmq
import zmq.asyncio

from harness import ServiceProc, zmq_req
from scdemo.http import HttpClient
from scdemo.protocol import pack_datagram


async def _assert_clean_exit(proc: ServiceProc) -> None:
    """SIGTERM: exit 0 within 2 s, ``shutdown`` last, no stray output."""
    async with asyncio.timeout(2):
        assert await proc.terminate() == 0
    assert proc.events[-1]["event"] == "shutdown"
    assert proc.other_stdout == []
    assert proc.stderr_lines == []


def _configure(rate_hz: int = 20) -> dict[str, object]:
    return {"cmd": "configure", "rate_hz": rate_hz}


@contextlib.asynccontextmanager
async def _zmq(kind: int, endpoint: str) -> AsyncGenerator[zmq.asyncio.Socket]:
    ctx = zmq.asyncio.Context()
    ctx.linger = 0
    sock = ctx.socket(kind)
    try:
        sock.connect(endpoint)
        yield sock
    finally:
        sock.close(linger=0)
        ctx.term()


@contextlib.asynccontextmanager
async def _sinks(port_base: int) -> AsyncGenerator[tuple[ServiceProc, ServiceProc]]:
    async with (
        ServiceProc("sink", port_base, SCDEMO_INSTANCE="1") as s1,
        ServiceProc("sink", port_base, SCDEMO_INSTANCE="2") as s2,
    ):
        yield s1, s2


@pytest.mark.asyncio
async def test_relay_configure_starts_post_links(port_base):
    async with (
        ServiceProc("sink", port_base, SCDEMO_INSTANCE="1") as s1,
        ServiceProc("sink", port_base, SCDEMO_INSTANCE="2") as s2,
        ServiceProc("hwsim", port_base) as hw,
        ServiceProc("relay", port_base) as relay,
    ):
        await relay.wait_event("ready")
        assert not any(
            e["event"] == "bound" and e.get("name") == "pull" for e in relay.events
        )
        reply = await zmq_req(
            f"tcp://127.0.0.1:{port_base + 100}", {"cmd": "configure", "rate_hz": 20}
        )
        assert reply["ok"] is True
        await relay.wait_event("bound", name="pull")
        await relay.wait_event("connected", name="hw")
        await s1.wait_event("first_datagram")
        await s2.wait_event("first_datagram")
        await s1.wait_event("connected", name="push")
        await relay.wait_event("first_datagram", name="udp")
        await hw.wait_event("sending", name="udp")
        await relay.wait_event("configured")
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
async def test_relay_configures_without_hwsim_and_retries(port_base):
    async with (
        ServiceProc("sink", port_base, SCDEMO_INSTANCE="1") as s1,
        ServiceProc("sink", port_base, SCDEMO_INSTANCE="2") as s2,
        ServiceProc("relay", port_base) as relay,
    ):
        await relay.wait_event("ready")
        assert (
            await zmq_req(
                f"tcp://127.0.0.1:{port_base + 100}",
                {"cmd": "configure", "rate_hz": 20},
            )
        )["ok"]
        await s1.wait_event("first_datagram")
        async with ServiceProc("hwsim", port_base):
            await relay.wait_event("connected", name="hw", timeout=5)
        assert s2.returncode is None
        await relay.wait_event("disconnected", name="hw")
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
async def test_relay_binds_in_order_then_connects_stats_then_ready(port_base):
    async with _sinks(port_base) as (s1, s2), ServiceProc("relay", port_base) as relay:
        await relay.wait_event("ready")
        bound = [e["name"] for e in relay.events if e["event"] == "bound"]
        assert bound == ["rep", "router", "pub", "http", "udp"]
        names = [
            (e["event"], e.get("name"))
            for e in relay.events
            if e["event"] in {"bound", "connected", "ready"}
        ]
        assert names.index(("bound", "udp")) < names.index(("connected", "stats@1"))
        assert names.index(("connected", "stats@1")) < names.index(("ready", None))
        assert names.index(("connected", "stats@2")) < names.index(("ready", None))
        await s1.wait_event("connected", name="stats-client")
        await s2.wait_event("connected", name="stats-client")


@pytest.mark.asyncio
async def test_relay_waits_for_stats_before_ready(port_base):
    async with ServiceProc("relay", port_base) as relay:
        await relay.wait_event("connecting", name="stats@1")
        async with ServiceProc("sink", port_base, SCDEMO_INSTANCE="1"):
            await relay.wait_event("connected", name="stats@1", timeout=5)
            assert not any(e["event"] == "ready" for e in relay.events)
            async with ServiceProc("sink", port_base, SCDEMO_INSTANCE="2"):
                await relay.wait_event("ready", timeout=5)
            await relay.wait_event("disconnected", name="stats@2", timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("configure", [False, True])
async def test_relay_sigterm_clean_exit(port_base, configure):
    async with (
        _sinks(port_base),
        ServiceProc("hwsim", port_base) as hw,
        ServiceProc("relay", port_base) as relay,
    ):
        await relay.wait_event("ready")
        # An open HTTP keep-alive client and a SUB socket during shutdown.
        client = HttpClient("127.0.0.1", port_base + 104)
        await client.get_json("/status")
        async with _zmq(zmq.SUB, f"tcp://127.0.0.1:{port_base + 102}"):
            if configure:
                await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure())
                await relay.wait_event("connected", name="hw")
                await relay.wait_event("first_datagram", name="udp")
                await hw.wait_event("sending", name="udp")
            await _assert_clean_exit(relay)
        await client.close()


@pytest.mark.asyncio
async def test_relay_sigterm_while_retrying_stats(port_base):
    async with ServiceProc("relay", port_base) as relay:
        await relay.wait_event("connecting", name="stats@1")
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
async def test_relay_no_bind_keeps_running(port_base):
    async with (
        _sinks(port_base),
        ServiceProc("relay", port_base, SCDEMO_FAULT_NO_BIND="http") as relay,
    ):
        await relay.wait_event("bind_skipped", name="http")
        await relay.wait_event("ready")
        reply = await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure())
        assert reply["ok"] is True
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
async def test_relay_no_bind_pull_still_configures(port_base):
    async with (
        _sinks(port_base),
        ServiceProc("relay", port_base, SCDEMO_FAULT_NO_BIND="pull") as relay,
    ):
        await relay.wait_event("ready")
        reply = await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure())
        assert reply["ok"] is True
        await relay.wait_event("bind_skipped", name="pull")
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
async def test_relay_wrong_port_binds_plus_1000(port_base):
    async with (
        _sinks(port_base),
        ServiceProc("relay", port_base, SCDEMO_FAULT_WRONG_PORT="router") as relay,
    ):
        await relay.wait_event("bound", name="router", port=str(port_base + 1101))
        await relay.wait_event("ready")


@pytest.mark.asyncio
async def test_relay_ignore_config_never_replies(port_base):
    async with (
        _sinks(port_base),
        ServiceProc("relay", port_base, SCDEMO_FAULT_IGNORE_CONFIG="1") as relay,
    ):
        await relay.wait_event("ready")
        with pytest.raises(TimeoutError):
            await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure(), timeout=1)
        await relay.wait_event("config_ignored")
        assert not any(e["event"] == "configured" for e in relay.events)
        assert not any(e.get("name") == "pull" for e in relay.events)
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
async def test_relay_every_reply_carries_boot_id(port_base):
    async with _sinks(port_base), ServiceProc("relay", port_base) as relay:
        await relay.wait_event("ready")
        reply = await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure())
        boot_id = reply["boot_id"]
        assert isinstance(boot_id, str)
        assert len(boot_id) == 32
        int(boot_id, 16)

        async with _zmq(zmq.DEALER, f"tcp://127.0.0.1:{port_base + 101}") as dealer:
            async with asyncio.timeout(5):
                await dealer.send(b"status")
                status = json.loads(await dealer.recv())
            assert status["boot_id"] == boot_id
            assert status["configured"] is True
            async with asyncio.timeout(5):  # REQ-style envelope works too
                await dealer.send_multipart([b"", b'{"cmd": "status"}'])
                empty, body = await dealer.recv_multipart()
            assert empty == b""
            assert json.loads(body)["boot_id"] == boot_id

        async with _zmq(zmq.SUB, f"tcp://127.0.0.1:{port_base + 102}") as sub:
            sub.setsockopt_string(zmq.SUBSCRIBE, "tick")
            async with asyncio.timeout(5):
                tick = (await sub.recv()).decode().split()
            assert tick[0] == "tick"
            int(tick[1])
            assert tick[2] == boot_id

        client = HttpClient("127.0.0.1", port_base + 104)
        try:
            http_status = await client.get_json("/status")
        finally:
            await client.close()
        assert http_status["boot_id"] == boot_id
        assert set(http_status) >= {"udp", "sent", "pull", "configured", "hw"}
        await relay.wait_event("connected", name="http-client")


@pytest.mark.asyncio
async def test_relay_second_configure_is_idempotent(port_base):
    async with _sinks(port_base) as (s1, _s2), ServiceProc("relay", port_base) as relay:
        await relay.wait_event("ready")
        endpoint = f"tcp://127.0.0.1:{port_base + 100}"
        first = await zmq_req(endpoint, _configure())
        await s1.wait_event("first_datagram")
        second = await zmq_req(endpoint, _configure())
        assert first == second
        assert first["ok"] is True
        configured = [e for e in relay.events if e["event"] == "configured"]
        assert len(configured) == 2

        def count(event: str, name: str) -> int:
            return sum(
                1 for e in relay.events if e["event"] == event and e.get("name") == name
            )

        assert count("bound", "pull") == 1
        assert count("sending", "udp@1") == 1
        assert count("sending", "udp@2") == 1
        assert count("connecting", "hw") == 1
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_body",
    [b"not json", b"[1, 2]", b'{"cmd": "nope"}', b'{"cmd": "configure"}'],
)
async def test_relay_bad_rep_request_gets_error_reply(port_base, request_body):
    async with _sinks(port_base), ServiceProc("relay", port_base) as relay:
        await relay.wait_event("ready")
        async with _zmq(zmq.REQ, f"tcp://127.0.0.1:{port_base + 100}") as req:
            async with asyncio.timeout(5):
                await req.send(request_body)
                reply = json.loads(await req.recv())
        assert reply["ok"] is False
        assert "boot_id" in reply
        # The REP socket is still usable.
        ok = await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure())
        assert ok["ok"] is True
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
async def test_relay_udp_to_absent_connected_sink_is_quiet(port_base):
    # Only sink 2 runs, so udp@1 (connected) hits a closed port: ICMP errors
    # must not kill the relay or print anything.
    async with (
        ServiceProc("sink", port_base, SCDEMO_INSTANCE="2") as s2,
        ServiceProc("relay", port_base, SCDEMO_FAULT_NO_CONNECT="stats@1") as relay,
    ):
        await relay.wait_event("connect_skipped", name="stats@1")
        await relay.wait_event("ready")
        await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure(rate_hz=200))
        await relay.wait_event("sending", name="udp@1")
        await s2.wait_event("first_datagram")
        client = HttpClient("127.0.0.1", port_base + 104)
        try:
            async with asyncio.timeout(5):
                while True:  # wait until udp@1 has sent well past the first error
                    sent = (await client.get_json("/status"))["sent"]
                    assert isinstance(sent, dict)
                    if sent["udp@1"] > 100:
                        break
                    await asyncio.sleep(0.1)
        finally:
            await client.close()
        assert relay.returncode is None
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
async def test_relay_wrong_connect_stats_still_ready(port_base):
    async with (
        _sinks(port_base),
        ServiceProc("relay", port_base, SCDEMO_FAULT_WRONG_CONNECT="stats@2") as relay,
    ):
        await relay.wait_event("connecting", name="stats@2", port=str(port_base + 1022))
        await relay.wait_event("ready")
        assert not any(
            e["event"] == "connected" and e.get("name") == "stats@2"
            for e in relay.events
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("SCDEMO_FAULT_NO_CONNECT", "stats@3"),
        ("SCDEMO_FAULT_WRONG_CONNECT", "udp@9"),
        ("SCDEMO_FAULT_NO_BIND", "ctrl"),
        ("SCDEMO_SINK_COUNT", "zero"),
        ("SCDEMO_SINK_COUNT", "0"),
    ],
)
async def test_relay_bad_env_exits_2(port_base, var, value):
    async with ServiceProc("relay", port_base, **{var: value}) as relay:
        assert await relay.wait_exit() == 2
        assert len(relay.stderr_lines) == 1
        assert var in relay.stderr_lines[0]


@pytest.mark.asyncio
async def test_relay_sink_count_sets_edges(port_base):
    async with (
        ServiceProc("sink", port_base, SCDEMO_INSTANCE="1"),
        ServiceProc(
            "relay",
            port_base,
            SCDEMO_SINK_COUNT="1",
            SCDEMO_FAULT_NO_CONNECT="hw",
        ) as relay,
    ):
        await relay.wait_event("ready")
        assert not any(e.get("name") == "stats@2" for e in relay.events)
        await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure())
        await relay.wait_event("connect_skipped", name="hw")
        assert not any(e.get("name") == "udp@2" for e in relay.events)


async def _http_raw(port: int, data: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        w.write(data)
        async with asyncio.timeout(5):
            return await r.read()
    finally:
        w.close()
        with contextlib.suppress(ConnectionError):
            await w.wait_closed()


@pytest.mark.asyncio
async def test_relay_http_bad_clients_are_quiet(port_base):
    async with _sinks(port_base), ServiceProc("relay", port_base) as relay:
        await relay.wait_event("ready")
        port = port_base + 104
        too_long = await _http_raw(port, b"GET /" + b"x" * (70 * 1024) + b"\r\n")
        assert too_long.startswith(b"HTTP/1.1 400 ")
        garbage = await _http_raw(port, b"\x00\x01garbage\r\n\r\n")
        assert garbage.startswith(b"HTTP/1.1 400 ")
        with socket.create_connection(("127.0.0.1", port)) as c:
            c.sendall(b"GET /status HTTP/1.1\r\n")
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client = HttpClient("127.0.0.1", port)
        try:
            for _ in range(3):
                assert "boot_id" in await client.get_json("/status")
            assert client.connections_opened == 1
        finally:
            await client.close()
        await _assert_clean_exit(relay)


@pytest.mark.asyncio
async def test_relay_udp_logs_restart_when_hwsim_starts_again(port_base):
    async with _sinks(port_base), ServiceProc("relay", port_base) as relay:
        await relay.wait_event("ready")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            for seq in (0, 1, 2):
                tx.sendto(pack_datagram(0, seq), ("127.0.0.1", port_base + 105))
            first = await relay.wait_event("first_datagram", name="udp", seq="0")
            assert "restart" not in first
            for seq in (0, 1):
                tx.sendto(pack_datagram(0, seq), ("127.0.0.1", port_base + 105))
            await relay.wait_event(
                "first_datagram", name="udp", seq="0", restart="1", after=first
            )
        assert not any(e["event"] == "gap_detected" for e in relay.events)


@pytest.mark.asyncio
async def test_relay_udp1_connected_udp2_unconnected(port_base):
    async with _sinks(port_base), ServiceProc("relay", port_base) as relay:
        await relay.wait_event("ready")
        await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure())
        udp1 = await relay.wait_event("sending", name="udp@1")
        udp2 = await relay.wait_event("sending", name="udp@2")
        assert (udp1["connected"], udp2["connected"]) == ("1", "0")


@pytest.mark.asyncio
async def test_relay_sending_stopped_on_shutdown_has_reason(port_base):
    async with _sinks(port_base), ServiceProc("relay", port_base) as relay:
        await relay.wait_event("ready")
        await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure())
        await relay.wait_event("sending", name="udp@2")
        await _assert_clean_exit(relay)
        stopped = [e for e in relay.events if e["event"] == "sending_stopped"]
        assert {(e["name"], e["reason"]) for e in stopped} == {
            ("udp@1", "shutdown"),
            ("udp@2", "shutdown"),
        }


@pytest.mark.asyncio
async def test_relay_stop_sending_after_fault(port_base):
    async with (
        _sinks(port_base) as (s1, _s2),
        ServiceProc("relay", port_base, SCDEMO_FAULT_STOP_SENDING_AFTER="0.5") as relay,
    ):
        await relay.wait_event("ready")
        await zmq_req(f"tcp://127.0.0.1:{port_base + 100}", _configure(rate_hz=100))
        await s1.wait_event("first_datagram")
        stopped = await relay.wait_event(
            "sending_stopped", name="udp@1", reason="fault", timeout=3
        )
        await relay.wait_event("sending_stopped", name="udp@2", reason="fault")
        assert stopped["reason"] == "fault"
        client = HttpClient("127.0.0.1", port_base + 104)
        try:
            before = (await client.get_json("/status"))["sent"]
            await asyncio.sleep(0.3)
            after = (await client.get_json("/status"))["sent"]
        finally:
            await client.close()
        assert isinstance(before, dict)
        assert before == after
        assert before["udp@1"] > 0
        # The sink sees the datagrams stop too: its PONG count stays put.
        r, w = await asyncio.open_connection("127.0.0.1", port_base + 21)
        try:
            counts = []
            for _ in range(2):
                w.write(b"PING\n")
                async with asyncio.timeout(2):
                    counts.append(int((await r.readline()).split()[1]))
                await asyncio.sleep(0.3)
        finally:
            w.close()
            await w.wait_closed()
        assert counts[0] == counts[1] > 0
        assert relay.returncode is None
        await _assert_clean_exit(relay)
