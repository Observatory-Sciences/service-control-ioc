from dataclasses import replace
from pathlib import Path

import pytest

from service_control_ioc.config.load import load_config
from service_control_ioc.config.spec import StackSpec
from service_control_ioc.observe.match import (
    EdgeFact,
    ListenerFact,
    match_edge,
    match_listener,
)
from service_control_ioc.observe.procfs import (
    TCP_ESTABLISHED,
    TCP_LISTEN,
    UDP_ESTABLISHED,
    UDP_UNCONN,
    OwnedSock,
    Sock,
    SocketTable,
)

SPEC = load_config(Path(__file__).parents[2] / "teststack" / "config" / "scdemo.yaml")
RELAY = "scdemo-relay.service"
CONTROLLER = "scdemo-controller.service"
SINK1 = "scdemo-sink@1.service"

CLOSE_WAIT = 0x08
FIN_WAIT1 = 0x04
FIN_WAIT2 = 0x05
TIME_WAIT = 0x06


def owned(
    unit: str | None,
    proto: str,
    local: tuple[str, int],
    remote: tuple[str, int],
    state: int,
) -> OwnedSock:
    assert proto in ("tcp", "udp")
    sock = Sock(proto, local[0], local[1], remote[0], remote[1], state, 1)
    return OwnedSock(sock, 1, unit)


def listen(unit: str | None, ip: str, port: int) -> OwnedSock:
    return owned(unit, "tcp", (ip, port), ("0.0.0.0", 0), TCP_LISTEN)


def conn(unit: str | None, remote: tuple[str, int], state: int) -> OwnedSock:
    return owned(unit, "tcp", ("127.0.0.1", 40000), remote, state)


def table(*entries: OwnedSock) -> SocketTable:
    return SocketTable(tuple(entries))


def edge_fact(spec: StackSpec, edge_id: str, tbl: SocketTable) -> EdgeFact:
    listeners = {
        lid: match_listener(
            ls,
            spec.services[ls.owner].unit if ls.owner in spec.services else "",
            tbl,
        )
        for lid, ls in spec.listeners.items()
    }
    return match_edge(spec.edges[edge_id], spec, tbl, listeners)


def test_listener_present() -> None:
    fact = match_listener(
        SPEC.listeners["relay.rep"], RELAY, table(listen(RELAY, "127.0.0.1", 20100))
    )
    assert fact == ListenerFact(True, "127.0.0.1:20100/tcp listening", None)


def test_listener_missing() -> None:
    fact = match_listener(SPEC.listeners["relay.rep"], RELAY, table())
    assert not fact.present
    assert fact.taken_by is None


def test_listener_taken_by_external() -> None:
    fact = match_listener(
        SPEC.listeners["relay.rep"], RELAY, table(listen(None, "127.0.0.1", 20100))
    )
    assert not fact.present
    assert fact.taken_by == "external"


def test_listener_taken_by_other_unit() -> None:
    fact = match_listener(
        SPEC.listeners["relay.rep"],
        RELAY,
        table(listen(CONTROLLER, "127.0.0.1", 20100)),
    )
    assert not fact.present
    assert fact.taken_by == CONTROLLER


def test_listener_wildcard_bind_is_present() -> None:
    fact = match_listener(
        SPEC.listeners["relay.rep"], RELAY, table(listen(RELAY, "0.0.0.0", 20100))
    )
    assert fact.present


def test_listener_wrong_address_is_not_present() -> None:
    fact = match_listener(
        SPEC.listeners["relay.rep"], RELAY, table(listen(RELAY, "127.0.0.9", 20100))
    )
    assert not fact.present


def test_tcp_listener_ignores_non_listen_state() -> None:
    fact = match_listener(
        SPEC.listeners["relay.rep"],
        RELAY,
        table(conn(RELAY, ("127.0.0.1", 20100), TCP_ESTABLISHED)),
    )
    assert not fact.present


def test_udp_listener_present() -> None:
    entry = owned(SINK1, "udp", ("127.0.0.1", 20011), ("0.0.0.0", 0), UDP_UNCONN)
    fact = match_listener(SPEC.listeners["sink@1.udp"], SINK1, table(entry))
    assert fact == ListenerFact(True, "127.0.0.1:20011/udp bound", None)


def test_udp_listener_ignores_tcp_entry_on_same_port() -> None:
    fact = match_listener(
        SPEC.listeners["sink@1.udp"], SINK1, table(listen(SINK1, "127.0.0.1", 20011))
    )
    assert not fact.present


def test_any_address_listener_matches_any_ip() -> None:
    spec = SPEC.listeners["relay.rep"]
    anywhere = replace(spec, address=None)
    fact = match_listener(anywhere, RELAY, table(listen(RELAY, "10.1.2.3", 20100)))
    assert fact.present


def test_edge_present_for_established_to_listener() -> None:
    tbl = table(
        listen(RELAY, "127.0.0.1", 20100),
        conn(CONTROLLER, ("127.0.0.1", 20100), TCP_ESTABLISHED),
    )
    fact = edge_fact(SPEC, "controller -> relay.rep", tbl)
    assert fact == EdgeFact(True, "established", True)


def test_edge_wrong_remote_ip() -> None:
    tbl = table(
        listen(RELAY, "127.0.0.1", 20100),
        conn(CONTROLLER, ("127.0.0.3", 20100), TCP_ESTABLISHED),
    )
    assert not edge_fact(SPEC, "controller -> relay.rep", tbl).present


def test_edge_owned_by_other_unit() -> None:
    tbl = table(
        listen(RELAY, "127.0.0.1", 20100),
        conn(SINK1, ("127.0.0.1", 20100), TCP_ESTABLISHED),
    )
    assert not edge_fact(SPEC, "controller -> relay.rep", tbl).present


def test_server_missing_is_reported() -> None:
    tbl = table(conn(CONTROLLER, ("127.0.0.1", 20100), TCP_ESTABLISHED))
    fact = edge_fact(SPEC, "controller -> relay.rep", tbl)
    assert fact.present
    assert not fact.server_present


@pytest.mark.parametrize("state", [CLOSE_WAIT, FIN_WAIT1, FIN_WAIT2, TIME_WAIT])
def test_close_wait_is_not_established(state: int) -> None:
    tbl = table(
        listen(RELAY, "127.0.0.1", 20100),
        conn(CONTROLLER, ("127.0.0.1", 20100), state),
    )
    assert not edge_fact(SPEC, "controller -> relay.rep", tbl).present


def test_edge_to_host_needs_host_address() -> None:
    good = table(conn(RELAY, ("127.0.0.2", 20200), TCP_ESTABLISHED))
    bad = table(conn(RELAY, ("127.0.0.1", 20200), TCP_ESTABLISHED))
    fact = edge_fact(SPEC, "relay -> hw.ctrl", good)
    assert fact == EdgeFact(True, "established", True)
    assert not edge_fact(SPEC, "relay -> hw.ctrl", bad).present


def test_edge_to_wildcard_listener_accepts_loopback_or_own_ip() -> None:
    spec = SPEC.listeners["relay.rep"]
    wild = replace(spec, address=None)
    stack = replace(SPEC, listeners={**SPEC.listeners, wild.id: wild})
    edge = stack.edges["controller -> relay.rep"]
    tbl = table(
        listen(RELAY, "0.0.0.0", 20100),
        owned(
            CONTROLLER, "tcp", ("10.1.2.3", 41000), ("10.1.2.3", 20100), TCP_ESTABLISHED
        ),
    )
    listeners = {"relay.rep": match_listener(wild, RELAY, tbl)}
    assert match_edge(edge, stack, tbl, listeners).present
    other = table(
        listen(RELAY, "0.0.0.0", 20100),
        owned(
            CONTROLLER, "tcp", ("10.1.2.3", 41000), ("10.9.9.9", 20100), TCP_ESTABLISHED
        ),
    )
    assert not match_edge(edge, stack, other, listeners).present


def test_udp_connected_needs_udp_established() -> None:
    server = owned(SINK1, "udp", ("127.0.0.1", 20011), ("0.0.0.0", 0), UDP_UNCONN)
    unconnected = owned(RELAY, "udp", ("127.0.0.1", 41000), ("0.0.0.0", 0), UDP_UNCONN)
    connected = owned(
        RELAY, "udp", ("127.0.0.1", 41000), ("127.0.0.1", 20011), UDP_ESTABLISHED
    )
    assert not edge_fact(
        SPEC, "relay -> sink@1.udp", table(server, unconnected)
    ).present
    fact = edge_fact(SPEC, "relay -> sink@1.udp", table(server, connected))
    assert fact == EdgeFact(True, "connected", True)


def test_udp_connected_needs_receiver_bound() -> None:
    """Ruling 20 (spec 5.3): the OS keeps a connected UDP socket after the
    receiver stops, so the edge is ready only while the receiver is bound too,
    as for an unconnected sender."""
    connected = owned(
        RELAY, "udp", ("127.0.0.1", 41000), ("127.0.0.1", 20011), UDP_ESTABLISHED
    )
    fact = edge_fact(SPEC, "relay -> sink@1.udp", table(connected))
    assert fact == EdgeFact(False, "connected", False)


def test_udp_unconnected_needs_sender_socket() -> None:
    sink2 = "scdemo-sink@2.service"
    server = owned(sink2, "udp", ("127.0.0.1", 20012), ("0.0.0.0", 0), UDP_UNCONN)
    own = owned(RELAY, "udp", ("127.0.0.1", 20105), ("0.0.0.0", 0), UDP_UNCONN)
    sender = owned(RELAY, "udp", ("0.0.0.0", 45000), ("0.0.0.0", 0), UDP_UNCONN)
    fact = edge_fact(SPEC, "relay -> sink@2.udp", table(server, own))
    assert not fact.present
    assert fact.server_present
    fact = edge_fact(SPEC, "relay -> sink@2.udp", table(server, own, sender))
    assert fact == EdgeFact(True, "sender socket present", True)


def test_udp_remote_follows_receiver() -> None:
    own = owned(RELAY, "udp", ("127.0.0.1", 20105), ("0.0.0.0", 0), UDP_UNCONN)
    fact = edge_fact(SPEC, "hw -> relay.udp", table(own))
    assert fact == EdgeFact(True, "receiver bound (remote sender not observable)", True)
    assert not edge_fact(SPEC, "hw -> relay.udp", table()).present
