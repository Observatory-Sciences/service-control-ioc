"""Match configured listeners and edges against an observed socket table."""

from collections.abc import Mapping
from dataclasses import dataclass

from qd_service_control.config.spec import EdgeSpec, ListenerSpec, StackSpec
from qd_service_control.observe.procfs import (
    TCP_ESTABLISHED,
    TCP_LISTEN,
    UDP_ESTABLISHED,
    UDP_UNCONN,
    OwnedSock,
    SocketTable,
)

_WILDCARD = "0.0.0.0"


@dataclass(frozen=True, slots=True)
class ListenerFact:
    present: bool
    detail: str
    taken_by: str | None


@dataclass(frozen=True, slots=True)
class EdgeFact:
    present: bool
    detail: str
    server_present: bool


def _bound_state(spec: ListenerSpec, entry: OwnedSock) -> bool:
    sock = entry.sock
    if sock.proto != spec.proto or sock.local_port != spec.port:
        return False
    if spec.proto == "tcp":
        return sock.state == TCP_LISTEN
    return sock.state in (UDP_UNCONN, UDP_ESTABLISHED)


def _address_ok(spec: ListenerSpec, ip: str) -> bool:
    return spec.address is None or ip in (spec.address, _WILDCARD)


def match_listener(spec: ListenerSpec, unit: str, table: SocketTable) -> ListenerFact:
    """Find ``spec`` bound by ``unit``, or report who else holds its port."""
    taken_by: str | None = None
    for entry in table.entries:
        if not _bound_state(spec, entry):
            continue
        if entry.unit == unit and _address_ok(spec, entry.sock.local_ip):
            noun = "listening" if spec.proto == "tcp" else "bound"
            detail = f"{entry.sock.local_ip}:{spec.port}/{spec.proto} {noun}"
            return ListenerFact(True, detail, None)
        if entry.unit != unit and taken_by is None:
            taken_by = entry.unit or "external"
    detail = f"{spec.port}/{spec.proto} not bound"
    if taken_by is not None:
        detail = f"{spec.port}/{spec.proto} taken by {taken_by}"
    return ListenerFact(False, detail, taken_by)


def _tcp_remote_ok(entry: OwnedSock, server: ListenerSpec, stack: StackSpec) -> bool:
    remote = entry.sock.remote_ip
    if server.on_host:
        return remote == stack.hosts[server.owner].address
    if server.address is not None and server.address != _WILDCARD:
        return remote == server.address
    return remote.startswith("127.") or remote == entry.sock.local_ip


def match_edge(
    spec: EdgeSpec,
    stack: StackSpec,
    table: SocketTable,
    listeners: Mapping[str, ListenerFact],
) -> EdgeFact:
    """Decide whether the client side of ``spec`` is observably connected."""
    server = stack.listeners[spec.server]
    server_present = server.on_host or listeners[spec.server].present
    if spec.proto == "udp" and spec.sender == "remote":
        return EdgeFact(
            server_present,
            "receiver bound (remote sender not observable)",
            server_present,
        )
    client_unit = stack.services[spec.client].unit
    mine = table.owned_by(client_unit)
    if spec.proto == "tcp":
        found = any(
            e.sock.proto == "tcp"
            and e.sock.state == TCP_ESTABLISHED  # half-closed states do not count
            and e.sock.remote_port == server.port
            and _tcp_remote_ok(e, server, stack)
            for e in mine
        )
        return EdgeFact(found, "established", server_present)
    if spec.sender == "connected":
        # The OS keeps a connected UDP socket after the receiver stops, so the
        # receiver must be bound too (spec 5.3, plan 4 ruling 20).
        ip = server.address
        found = server_present and any(
            e.sock.proto == "udp"
            and e.sock.state == UDP_ESTABLISHED
            and e.sock.remote_port == server.port
            and (ip is None or e.sock.remote_ip == ip)
            for e in mine
        )
        return EdgeFact(found, "connected", server_present)
    own_ports = {
        stack.listeners[lid].port
        for lid in stack.services[spec.client].listeners
        if stack.listeners[lid].proto == "udp"
    }
    found = server_present and any(
        e.sock.proto == "udp" and e.sock.local_port not in own_ports for e in mine
    )
    return EdgeFact(found, "sender socket present", server_present)
