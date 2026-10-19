import pytest

from scdemo.protocol import (
    DATAGRAM_SIZE,
    SeqTracker,
    pack_datagram,
    ports,
    unpack_datagram,
)


def test_datagram_round_trip() -> None:
    assert unpack_datagram(pack_datagram(2, 123456789)) == (2, 123456789)
    assert len(pack_datagram(1, 0)) == DATAGRAM_SIZE


def test_short_datagram_rejected() -> None:
    with pytest.raises(ValueError, match="short"):
        unpack_datagram(b"\x00" * 9)


def test_seq_tracker_gap_and_late() -> None:
    t = SeqTracker()
    assert [t.feed(s) for s in (0, 1, 2)] == [None, None, None]
    assert t.feed(5) == 2
    assert t.feed(3) is None
    assert t.late == 1
    assert (t.count, t.gaps, t.missing) == (5, 1, 2)


def test_seq_tracker_first_datagram_is_not_a_gap() -> None:
    t = SeqTracker()
    assert t.feed(1000) is None
    assert (t.count, t.gaps, t.missing) == (1, 0, 0)


def test_seq_tracker_duplicate_is_not_a_gap() -> None:
    t = SeqTracker()
    assert [t.feed(s) for s in (0, 1, 1)] == [None, None, None]
    assert (t.gaps, t.late) == (0, 1)


def test_ports_follow_table() -> None:
    p = ports(20000)
    assert (
        p.sink_udp(1),
        p.sink_stats(2),
        p.rep,
        p.pull,
        p.http,
        p.relay_udp,
        p.hw_ctrl,
    ) == (20011, 20022, 20100, 20103, 20104, 20105, 20200)
    assert (p.router, p.pub) == (20101, 20102)
