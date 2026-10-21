"""The soft IOC over Channel Access: records, publishing and control PVs.

Each test runs ``ioc_child.py`` (one IOC per process) and reads it with aioca
over loopback; see ``conftest.py``.
"""

import asyncio

import numpy
import pytest
from aioca import DBR_CHAR_STR, FORMAT_TIME, caget, camonitor, caput

NO_ALARM, MINOR, MAJOR, INVALID = 0, 1, 2, 3
SINK1_UNIT = "scdemo-sink@1.service"


def _svc(ioc, sid: str, field: str) -> str:
    return ioc.pv(ioc.names.services[sid], field)


def _stack(ioc, field: str) -> str:
    return ioc.pv("", field)


@pytest.mark.asyncio
async def test_every_pv_name_is_served(ioc) -> None:
    names = list(ioc.names.all())
    values = await caget(names, timeout=10, throw=False)
    missing = [v.name for v in values if not v.ok]
    assert missing == []


@pytest.mark.asyncio
async def test_initial_values_are_invalid_not_ready(ioc) -> None:
    state = await caget(_stack(ioc, "STATE"), format=FORMAT_TIME)
    assert state.severity == INVALID
    assert await caget(_stack(ioc, "READY")) == 0
    assert await caget(_stack(ioc, "POST_READY")) == 0
    sink = await caget(_svc(ioc, "sink@1", "STATE"), format=FORMAT_TIME, datatype=str)
    assert (str(sink), sink.severity) == ("unknown", INVALID)
    cpu = await caget(_svc(ioc, "sink@1", "CPU"), format=FORMAT_TIME)
    assert cpu.severity == INVALID
    assert await caget(_svc(ioc, "relay", "RESTART")) == 0


@pytest.mark.asyncio
async def test_ready_view_publishes(ioc) -> None:
    await ioc.send({"view": "ready"})
    await ioc.wait_for(_stack(ioc, "STATE"), lambda v: v == "ready", datatype=str)
    assert await caget(_stack(ioc, "READY")) == 1
    state = await caget(_stack(ioc, "STATE"), format=FORMAT_TIME)
    assert state.severity == NO_ALARM
    sink = await caget(_svc(ioc, "sink@1", "STATE"), datatype=str)
    assert sink == "ready"
    cpu = await caget(_svc(ioc, "sink@1", "CPU"), format=FORMAT_TIME)
    assert (round(float(cpu), 1), cpu.severity) == (1.5, NO_ALARM)
    assert await caget(_svc(ioc, "sink@1", "ACTIVE")) == "active/running"
    unit = await caget(_svc(ioc, "sink@1", "UNIT"), datatype=DBR_CHAR_STR)
    assert unit == SINK1_UNIT


@pytest.mark.asyncio
async def test_fault_view_severity_major(ioc) -> None:
    await ioc.send({"view": "fault"})
    await ioc.wait_for(_stack(ioc, "STATE"), lambda v: v == "fault", datatype=str)
    state = await caget(_stack(ioc, "STATE"), format=FORMAT_TIME)
    assert state.severity == MAJOR
    msg = await caget(_stack(ioc, "FAULT_MSG"), datatype=DBR_CHAR_STR)
    assert "look at" in msg
    assert await caget(_stack(ioc, "FAULTS")) > 0
    relay = await caget(_svc(ioc, "relay", "STATE"), format=FORMAT_TIME, datatype=str)
    assert (str(relay), relay.severity) == ("fault", MAJOR)
    finding = await caget(_svc(ioc, "relay", "FINDING"), datatype=DBR_CHAR_STR)
    assert "scdemo-relay.service" in finding


@pytest.mark.asyncio
async def test_restart_pv_calls_control_and_resets(ioc) -> None:
    await caput(_svc(ioc, "relay", "RESTART"), 1, wait=True, timeout=10)
    assert await ioc.next_control() == ["relay", "restart"]
    assert await caget(_svc(ioc, "relay", "RESTART")) == 0
    assert await caget(_svc(ioc, "relay", "LASTJOB")) == "restart: done"


@pytest.mark.asyncio
async def test_stack_start_targets_stack(ioc) -> None:
    await caput(_stack(ioc, "START"), 1, wait=True, timeout=10)
    assert await ioc.next_control() == ["stack", "start"]
    assert await caget(_stack(ioc, "START")) == 0
    assert await caget(_stack(ioc, "LASTJOB")) == "start: done"


@pytest.mark.asyncio
async def test_group_stop_targets_group(ioc) -> None:
    group = ioc.names.groups["all"]
    await caput(ioc.pv(group, "STOP"), 1, wait=True, timeout=10)
    assert await ioc.next_control() == ["all", "stop"]
    assert await caget(ioc.pv(group, "LASTJOB")) == "stop: done"


@pytest.mark.asyncio
async def test_write_of_zero_does_nothing(ioc) -> None:
    await caput(_svc(ioc, "relay", "RESTART"), 0, wait=True, timeout=10)
    await caput(_svc(ioc, "relay", "STOP"), 1, wait=True, timeout=10)
    # The first control call is the STOP: the zero write made none.
    assert await ioc.next_control() == ["relay", "stop"]
    assert await caget(_svc(ioc, "relay", "LASTJOB")) == "stop: done"


@pytest.mark.asyncio
async def test_control_that_raises_still_completes(ioc) -> None:
    await ioc.send({"control_raises": True})
    await caput(_svc(ioc, "sink@1", "RESTART"), 1, wait=True, timeout=10)
    assert await ioc.next_control() == ["sink@1", "restart"]
    assert await caget(_svc(ioc, "sink@1", "RESTART")) == 0
    assert await caget(_svc(ioc, "sink@1", "LASTJOB")) == "restart: error"
    # The record is usable again afterwards.
    await ioc.send({"control_raises": False})
    await caput(_svc(ioc, "sink@1", "RESTART"), 1, wait=True, timeout=10)
    assert await ioc.next_control() == ["sink@1", "restart"]
    assert await caget(_svc(ioc, "sink@1", "LASTJOB")) == "restart: done"


@pytest.mark.asyncio
async def test_log_is_rate_limited(ioc) -> None:
    """200 lines in 0.1 s: LOG updates at most 3 times in 1 s (1 or 2 expected)."""
    pv = _svc(ioc, "sink@1", "LOG")
    loop = asyncio.get_running_loop()
    values: list[str] = []
    first = asyncio.Event()

    def update(value: object) -> None:
        values.append(str(value))
        first.set()

    sub = camonitor(pv, update, datatype=DBR_CHAR_STR)
    try:
        async with asyncio.timeout(5):
            await first.wait()  # the monitor's initial value
        values.clear()
        start = loop.time()
        for batch in range(10):
            await ioc.send(
                *(
                    {"journal": [SINK1_UNIT, f"line {batch * 20 + i}"]}
                    for i in range(20)
                )
            )
            await asyncio.sleep(0.01)
        last = "line 199\n"
        await ioc.wait_for(pv, lambda v: str(v).endswith(last), datatype=DBR_CHAR_STR)
        await asyncio.sleep(max(0.0, start + 1.0 - loop.time()))
    finally:
        sub.close()
    assert 1 <= len(values) <= 3
    assert values[-1].endswith(last)
    assert values[-1].splitlines()[0] == "line 180"  # the newest 20 lines


EDM_TEXT = 1000  # EDM multiLineTextUpdate's MAX_TEXT_LENGTH (ruling 19)


def _edm_safe(raw: object) -> list[str]:
    """Check what EDM's multiLineTextUpdate would read from a char record (it
    copies at most 1000 bytes and prints junk unless a NUL comes within them),
    and return the text's lines."""
    data = numpy.asarray(raw, dtype=numpy.uint8).tobytes()
    assert len(data) <= EDM_TEXT, f"{len(data)} elements, EDM reads {EDM_TEXT}"
    assert 0 in data[:EDM_TEXT], "no NUL within the first 1000 bytes"
    text = data.split(b"\0", 1)[0]
    assert len(text) <= EDM_TEXT - 1
    return text.decode().splitlines()


@pytest.mark.asyncio
async def test_log_fits_edm_with_newest_whole_lines(ioc) -> None:
    """Ruling 19: LOG holds the newest whole lines that fit in 999 bytes."""
    lines = [
        f"line {i:02d} " + "x" * 91 for i in range(20)
    ]  # 100 bytes with the newline
    await ioc.send(*({"journal": [SINK1_UNIT, line]} for line in lines))
    pv = _svc(ioc, "sink@1", "LOG")
    await ioc.wait_for(
        pv, lambda v: str(v).endswith(lines[-1] + "\n"), datatype=DBR_CHAR_STR
    )
    shown = _edm_safe(await caget(pv, count=0))
    assert shown == lines[-9:]  # 9 x 100 bytes fit in 999, 10 do not


@pytest.mark.asyncio
async def test_log_single_long_line_is_cut_to_fit(ioc) -> None:
    line = "head " + "y" * 1500 + " TAIL"
    await ioc.send({"journal": [SINK1_UNIT, line]})
    pv = _svc(ioc, "sink@1", "LOG")
    await ioc.wait_for(pv, lambda v: str(v).endswith(" TAIL\n"), datatype=DBR_CHAR_STR)
    (shown,) = _edm_safe(await caget(pv, count=0))
    assert shown.endswith(" TAIL")
    assert len(shown) == EDM_TEXT - 2  # 999 bytes with the newline


@pytest.mark.asyncio
async def test_events_fit_edm_with_newest_whole_lines(ioc) -> None:
    events = [
        f"event {i:02d} " + "z" * 70 for i in range(20)
    ]  # 80 bytes with the newline
    await ioc.send(*({"event": e} for e in events))
    pv = _stack(ioc, "EVENTS")
    await ioc.wait_for(pv, lambda v: str(v).endswith(events[-1]), datatype=DBR_CHAR_STR)
    shown = _edm_safe(await caget(pv, count=0))
    assert shown == events[-12:]  # 12 x 80 bytes fit in 999, 13 do not


@pytest.mark.asyncio
async def test_events_ring_last_20(ioc) -> None:
    await ioc.send(*({"event": f"event {i}"} for i in range(25)))
    pv = _stack(ioc, "EVENTS")
    value = await ioc.wait_for(
        pv, lambda v: str(v).endswith("event 24"), datatype=DBR_CHAR_STR
    )
    assert str(value).splitlines() == [f"event {i}" for i in range(5, 25)]
