"""Read-only views of /proc sockets and cgroups (IPv4 only, Linux)."""

import re
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

TCP_LISTEN: Final = 0x0A
TCP_ESTABLISHED: Final = 0x01
UDP_UNCONN: Final = 0x07
UDP_ESTABLISHED: Final = 0x01

_SOCKET_LINK: Final = re.compile(r"socket:\[(\d+)\]")


@dataclass(frozen=True, slots=True)
class Sock:
    proto: Literal["tcp", "udp"]
    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int
    state: int
    inode: int


@dataclass(frozen=True, slots=True)
class OwnedSock:
    sock: Sock
    pid: int | None
    unit: str | None  # None = external / unattributed


@dataclass(frozen=True, slots=True)
class SocketTable:
    entries: tuple[OwnedSock, ...]

    def owned_by(self, unit: str) -> tuple[OwnedSock, ...]:
        return tuple(e for e in self.entries if e.unit == unit)


def _parse_addr(text: str) -> tuple[str, int]:
    ip_hex, port_hex = text.split(":")
    # The address is one 32-bit word in host byte order (little-endian on
    # x86_64 and aarch64); the port is plain big-endian hex.
    ip = socket.inet_ntoa(struct.pack("<I", int(ip_hex, 16)))
    return ip, int(port_hex, 16)


def parse_proc_net(text: str, proto: Literal["tcp", "udp"]) -> list[Sock]:
    """Parse /proc/net/tcp or /proc/net/udp. Malformed lines are skipped."""
    socks: list[Sock] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        try:
            local_ip, local_port = _parse_addr(fields[1])
            remote_ip, remote_port = _parse_addr(fields[2])
            socks.append(
                Sock(
                    proto,
                    local_ip,
                    local_port,
                    remote_ip,
                    remote_port,
                    int(fields[3], 16),
                    int(fields[9]),
                )
            )
        except ValueError:
            continue
    return socks


def unit_from_cgroup(text: str) -> str | None:
    """Return the service unit from a cgroup v2 ``0::/...`` line, if any.

    The innermost ``.service`` component wins, so a process in a sub-cgroup
    of the unit (``x.service/control``, a delegated subtree) belongs to it.
    """
    for line in text.splitlines():
        if not line.startswith("0::"):
            continue
        for part in reversed(line[3:].strip("/").split("/")):
            if part.endswith(".service"):
                return part
        return None
    return None


def _read_net(proc_root: Path, proto: Literal["tcp", "udp"]) -> list[Sock]:
    """Read one /proc/net table. Errors propagate: an unreadable table is not
    an empty one, and the caller makes the elements unknown rather than missing.
    """
    return parse_proc_net((proc_root / "net" / proto).read_text(), proto)


def _pid_sockets(pid_dir: Path) -> tuple[str | None, list[int]] | None:
    """Return (unit, socket inodes) of one pid, or None if it vanished/is unreadable."""
    try:
        unit = unit_from_cgroup((pid_dir / "cgroup").read_text())
        inodes: list[int] = []
        for fd in (pid_dir / "fd").iterdir():
            try:
                target = str(fd.readlink())
            except OSError:
                continue  # this fd closed while walking
            m = _SOCKET_LINK.fullmatch(target)
            if m:
                inodes.append(int(m.group(1)))
    except OSError:
        # FileNotFoundError, PermissionError, ProcessLookupError, ...: skip this pid.
        return None
    return unit, inodes


def scan_sockets(proc_root: Path = Path("/proc")) -> SocketTable:
    """Map IPv4 TCP/UDP sockets to the owning pid and systemd service unit.

    Raises ``OSError`` if ``/proc`` or a ``/proc/net`` table cannot be read; a
    pid that vanishes or cannot be read is skipped.
    """
    owners: dict[int, tuple[int, str | None]] = {}
    children = sorted(proc_root.iterdir())
    for pid_dir in children:
        if not pid_dir.name.isdecimal():
            continue
        found = _pid_sockets(pid_dir)
        if found is None:
            continue
        unit, inodes = found
        for inode in inodes:
            owners.setdefault(inode, (int(pid_dir.name), unit))
    entries = tuple(
        OwnedSock(sock, *owners.get(sock.inode, (None, None)))
        for proto in ("tcp", "udp")
        for sock in _read_net(proc_root, proto)
    )
    return SocketTable(entries)


def read_cpu_usec(cgroup_root: Path, control_group: str) -> int | None:
    """Return ``usage_usec`` from the cgroup's cpu.stat, or None if unavailable."""
    path = cgroup_root / control_group.lstrip("/") / "cpu.stat"
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec" and value.strip().isdecimal():
            return int(value)
    return None
