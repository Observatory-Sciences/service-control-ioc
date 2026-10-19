import logging

import pytest

from scdemo.logs import format_event, log_event, parse_logfmt, setup_logging


def test_logfmt_round_trip_with_spaces() -> None:
    line = format_event(
        "bound", "sink", inst=1, name="udp", proto="udp", port=20011, note="a b"
    )
    assert line.startswith("event=bound svc=sink inst=1 ")
    assert parse_logfmt(line) == {
        "event": "bound",
        "svc": "sink",
        "inst": "1",
        "name": "udp",
        "proto": "udp",
        "port": "20011",
        "note": "a b",
    }


def test_logfmt_without_inst() -> None:
    assert format_event("ready", "relay") == "event=ready svc=relay"


def test_logfmt_quotes_and_escapes() -> None:
    line = format_event("status", "relay", note='say "hi" a=b \\ end')
    assert parse_logfmt(line)["note"] == 'say "hi" a=b \\ end'
    assert 'note="say \\"hi\\" a=b \\\\ end"' in line


def test_logfmt_empty_value_round_trips() -> None:
    assert parse_logfmt(format_event("status", "relay", note=""))["note"] == ""


def test_log_event_writes_one_line_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        setup_logging("sink", 2)
        log_event("bound", name="udp", port=20012)
        out = capsys.readouterr().out
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
    assert out == "event=bound svc=sink inst=2 name=udp port=20012\n"
