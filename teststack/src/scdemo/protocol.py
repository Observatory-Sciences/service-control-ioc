"""Datagram wire format, sequence tracking and the port table."""

import struct
from dataclasses import dataclass, field
from typing import Final

DATAGRAM_SIZE: Final = 32
_HEADER: Final = struct.Struct("!HQ")


def pack_datagram(stream: int, seq: int) -> bytes:
    """Pack ``stream`` and ``seq`` into a fixed-size datagram, zero padded."""
    return _HEADER.pack(stream, seq).ljust(DATAGRAM_SIZE, b"\0")


def unpack_datagram(data: bytes) -> tuple[int, int]:
    """Return ``(stream, seq)``. Raise ValueError if ``data`` is too short."""
    if len(data) < _HEADER.size:
        raise ValueError(f"short datagram: {len(data)} bytes, need {_HEADER.size}")
    stream, seq = _HEADER.unpack_from(data)
    return stream, seq


@dataclass(slots=True)
class SeqTracker:
    """Track datagram sequence numbers and report gaps.

    ``count`` is every datagram fed, ``gaps`` the number of gap events,
    ``missing`` the datagrams skipped over by gaps, and ``late`` the datagrams
    that arrived below the next expected number (late or duplicated). A late
    datagram does not reduce ``missing``. ``restarts`` counts the sender
    restarting its stream: a ``seq`` of 0 after traffic beyond seq 0 resyncs
    the tracker (no gap, not late).
    """

    count: int = 0
    gaps: int = 0
    missing: int = 0
    late: int = 0
    restarts: int = 0
    _expected: int | None = field(default=None, repr=False)

    def feed(self, seq: int) -> int | None:
        """Record ``seq``. Return the number missing before it, else None."""
        self.count += 1
        if seq == 0 and self._expected is not None and self._expected > 1:
            self.restarts += 1
            self._expected = 1
            return None
        if self._expected is None or seq == self._expected:
            self._expected = seq + 1
            return None
        if seq < self._expected:
            self.late += 1
            return None
        missing = seq - self._expected
        self.gaps += 1
        self.missing += missing
        self._expected = seq + 1
        return missing


@dataclass(frozen=True, slots=True)
class Ports:
    """Every port in the stack, as offsets from one base."""

    base: int
    rep: int
    router: int
    pub: int
    pull: int
    http: int
    relay_udp: int
    hw_ctrl: int

    def sink_udp(self, n: int) -> int:
        return self.base + 10 + n

    def sink_stats(self, n: int) -> int:
        return self.base + 20 + n


def ports(base: int) -> Ports:
    return Ports(
        base=base,
        rep=base + 100,
        router=base + 101,
        pub=base + 102,
        pull=base + 103,
        http=base + 104,
        relay_udp=base + 105,
        hw_ctrl=base + 200,
    )
