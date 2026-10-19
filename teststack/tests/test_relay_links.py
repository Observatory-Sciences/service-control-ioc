"""Relay link coroutines survive any OSError on an established link."""

import asyncio
import errno

import pytest
import zmq.asyncio

from scdemo import relay


class _BrokenReader:
    """A StreamReader whose reads fail as a lost route would."""

    async def readline(self) -> bytes:
        raise OSError(errno.EHOSTUNREACH, "No route to host")

    async def read(self, n: int = -1) -> bytes:
        raise OSError(errno.ETIMEDOUT, "Connection timed out")


class _Writer:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Recorder:
    """Collects relay events; ``twice`` is set at the second disconnect."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.disconnects = 0
        self.twice = asyncio.Event()

    def log(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))
        if event == "disconnected":
            self.disconnects += 1
            if self.disconnects >= 2:
                self.twice.set()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()

    async def fake_open(host: str, port: int) -> tuple[_BrokenReader, _Writer]:
        return _BrokenReader(), _Writer()

    monkeypatch.setattr(relay, "log_event", rec.log)
    monkeypatch.setattr(relay, "RETRY_PERIOD", 0.01)
    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    return rec


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["stats@1", "hw"])
async def test_link_survives_oserror(recorder: _Recorder, link: str) -> None:
    cfg = relay.load_config({"SCDEMO_PORT_BASE": "20000"})
    ctx = zmq.asyncio.Context()
    try:
        r = relay.Relay(cfg, ctx)
        if link == "hw":
            task = asyncio.ensure_future(r.run_hw(10))
        else:
            task = asyncio.ensure_future(r.run_stats(1, asyncio.Event()))
        try:
            async with asyncio.timeout(2):
                await recorder.twice.wait()
            assert {f["name"] for e, f in recorder.events if e == "disconnected"} == {
                link
            }
            assert not task.done()  # the error did not end the link task
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        ctx.term()
