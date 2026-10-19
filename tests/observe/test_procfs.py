from pathlib import Path

import pytest

from qd_service_control.observe.procfs import (
    TCP_ESTABLISHED,
    TCP_LISTEN,
    UDP_UNCONN,
    parse_proc_net,
    read_cpu_usec,
    scan_sockets,
    unit_from_cgroup,
)

FIX = Path(__file__).parent.parent / "fixtures" / "el9"
HDR = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)
EL9_TCP = (FIX / "tcp").read_text()
RELAY_REP_INODE = next(
    s.inode
    for s in parse_proc_net(EL9_TCP, "tcp")
    if s.state == TCP_LISTEN and s.local_port == 20100
)


def fake_proc(
    root: Path,
    net: dict[str, str],
    pids: dict[int, tuple[str, list[int]]],
) -> Path:
    proc = root / "proc"
    (proc / "net").mkdir(parents=True)
    for name, text in net.items():
        (proc / "net" / name).write_text(text)
    for pid, (cgroup, inodes) in pids.items():
        d = proc / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "cgroup").write_text(cgroup)
        for n, inode in enumerate(inodes, start=3):
            (d / "fd" / str(n)).symlink_to(f"socket:[{inode}]")
    return proc


def test_parse_el9_tcp_has_relay_listeners() -> None:
    socks = parse_proc_net(EL9_TCP, "tcp")
    listening = {(s.local_ip, s.local_port) for s in socks if s.state == TCP_LISTEN}
    assert {
        ("127.0.0.1", 20100),
        ("127.0.0.1", 20104),
        ("127.0.0.2", 20200),
    } <= listening
    assert any(s.state == TCP_ESTABLISHED and s.remote_port == 20100 for s in socks)


def test_parse_el9_udp() -> None:
    socks = parse_proc_net((FIX / "udp").read_text(), "udp")
    assert socks
    assert all(s.proto == "udp" for s in socks)
    assert any(s.state == UDP_UNCONN for s in socks)


def test_parse_wildcard_v4() -> None:
    text = HDR + (
        "   0: 00000000:4E85 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000"
        "        0 998 1 0 100 0 0 10 0\n"
    )
    (a,) = parse_proc_net(text, "tcp")
    assert (a.local_ip, a.local_port, a.inode) == ("0.0.0.0", 20101, 998)


def test_parse_skips_blank_and_malformed_lines() -> None:
    assert parse_proc_net(HDR + "\n   garbage\n", "tcp") == []


def test_unit_from_cgroup_el9() -> None:
    assert (
        unit_from_cgroup((FIX / "cgroup-relay").read_text()) == "scdemo-relay.service"
    )
    assert (
        unit_from_cgroup((FIX / "cgroup-sink1").read_text()) == "scdemo-sink@1.service"
    )
    assert unit_from_cgroup("0::/user.slice/user-1000.slice/session-3.scope\n") is None
    assert unit_from_cgroup("") is None


def test_scan_sockets_fake_tree(tmp_path: Path) -> None:
    proc = fake_proc(
        tmp_path,
        net={"tcp": EL9_TCP},
        pids={
            101: ("0::/system.slice/scdemo-relay.service\n", [RELAY_REP_INODE]),
            202: ("0::/system.slice/other.service\n", []),
        },
    )
    table = scan_sockets(proc)
    (rep,) = [e for e in table.entries if e.sock.inode == RELAY_REP_INODE]
    assert (rep.pid, rep.unit) == (101, "scdemo-relay.service")
    assert table.owned_by("scdemo-relay.service") == (rep,)
    unowned = [e for e in table.entries if e.sock.inode != RELAY_REP_INODE]
    assert unowned
    assert all(e.pid is None and e.unit is None for e in unowned)


def test_scan_sockets_tolerates_vanishing_pid(tmp_path: Path) -> None:
    proc = fake_proc(
        tmp_path,
        net={"tcp": EL9_TCP},
        pids={101: ("0::/system.slice/scdemo-relay.service\n", [RELAY_REP_INODE])},
    )
    (proc / "303").mkdir()  # pid dir with no cgroup and no fd (like an exited process)
    (proc / "404").write_text("not a directory")
    table = scan_sockets(proc)
    assert any(e.unit == "scdemo-relay.service" for e in table.entries)


def test_scan_sockets_skips_unreadable_fd_dir(tmp_path: Path) -> None:
    proc = fake_proc(
        tmp_path,
        net={"tcp": EL9_TCP},
        pids={101: ("0::/system.slice/scdemo-relay.service\n", [RELAY_REP_INODE])},
    )
    (proc / "505" / "fd").mkdir(parents=True)
    (proc / "505" / "cgroup").write_text("0::/system.slice/x.service\n")
    (proc / "505" / "fd").chmod(0)
    try:
        table = scan_sockets(proc)
    finally:
        (proc / "505" / "fd").chmod(0o755)
    assert any(e.pid == 101 for e in table.entries)


def test_scan_sockets_ignores_non_socket_fds(tmp_path: Path) -> None:
    proc = fake_proc(
        tmp_path,
        net={"tcp": EL9_TCP},
        pids={101: ("0::/system.slice/scdemo-relay.service\n", [])},
    )
    (proc / "101" / "fd" / "9").symlink_to("/dev/null")
    (proc / "101" / "fd" / "10").symlink_to("socket:[notanumber]")
    table = scan_sockets(proc)
    assert all(e.pid is None for e in table.entries)


def test_scan_sockets_missing_net_files(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    assert scan_sockets(proc).entries == ()


def test_read_cpu_usec(tmp_path: Path) -> None:
    d = tmp_path / "system.slice/scdemo-relay.service"
    d.mkdir(parents=True)
    (d / "cpu.stat").write_text((FIX / "cpu.stat").read_text())
    usec = read_cpu_usec(tmp_path, "/system.slice/scdemo-relay.service")
    assert usec is not None
    assert usec > 0
    assert read_cpu_usec(tmp_path, "/system.slice/missing.service") is None


def test_read_cpu_usec_without_usage_line(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "cpu.stat").write_text("user_usec 5\n")
    assert read_cpu_usec(tmp_path, "/x") is None


@pytest.mark.parametrize("pid", ["abc", "self"])
def test_scan_sockets_ignores_non_numeric_entries(tmp_path: Path, pid: str) -> None:
    proc = fake_proc(tmp_path, net={"tcp": EL9_TCP}, pids={})
    (proc / pid).mkdir()
    assert scan_sockets(proc).entries
