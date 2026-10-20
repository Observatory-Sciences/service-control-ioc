"""``Ioc.run`` in-process, without records or ``iocInit``: subscription cleanup."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from qd_service_control.config.load import load_config
from qd_service_control.config.spec import StackSpec
from qd_service_control.ioc.app import Ioc
from qd_service_control.model.types import Event, JobOutcome, StackView, Verb
from qd_service_control.observe.journal import JournalLine

SCDEMO = Path(__file__).resolve().parents[2] / "teststack" / "config" / "scdemo.yaml"


class JournalRefused(Exception):
    pass


class RefusingApi:
    """A ``CoreApi`` whose journal subscription raises."""

    def __init__(self) -> None:
        self.subscribers: list[Callable[[Event], None]] = []

    @property
    def spec(self) -> StackSpec:
        return replace(load_config(SCDEMO), prefix="QDSCRUN:SC")

    def view(self) -> StackView:
        raise AssertionError("run() must not publish after a failed subscribe")

    def subscribe(self, cb: Callable[[Event], None]) -> Callable[[], None]:
        self.subscribers.append(cb)
        return lambda: self.subscribers.remove(cb)

    def subscribe_journal(
        self, cb: Callable[[JournalLine], None]
    ) -> Callable[[], None]:
        raise JournalRefused

    def journal(self, service_id: str) -> tuple[str, ...]:
        return ()

    async def control(self, target: str, verb: Verb) -> JobOutcome:
        raise AssertionError("no control in this test")


@pytest.mark.asyncio
async def test_run_unsubscribes_events_if_journal_subscribe_fails() -> None:
    api = RefusingApi()
    with pytest.raises(JournalRefused):
        await Ioc(api).run()
    assert api.subscribers == []
