import pytest

from scdemo import notify
from scdemo.env import ConfigError, Env
from scdemo.faults import Faults, announce_ready
from scdemo.logs import parse_logfmt, setup_logging


def make(environ: dict[str, str]) -> Faults:
    return Faults.from_env(
        Env(environ), frozenset({"udp", "stats"}), frozenset({"push"})
    )


def test_bind_and_connect_ports() -> None:
    env = Env(
        {
            "SCDEMO_FAULT_NO_BIND": "udp",
            "SCDEMO_FAULT_WRONG_PORT": "stats",
            "SCDEMO_FAULT_WRONG_CONNECT": "push",
        }
    )
    f = Faults.from_env(env, frozenset({"udp", "stats"}), frozenset({"push"}))
    assert f.bind_port("udp", 20011) is None
    assert f.bind_port("stats", 20021) == 21021
    assert f.bind_port("other", 20031) == 20031
    assert f.connect_port("push", 20103) == 21103


def test_no_connect_skips() -> None:
    f = make({"SCDEMO_FAULT_NO_CONNECT": "push"})
    assert f.connect_port("push", 20103) is None


def test_faults_reject_unknown_edge() -> None:
    with pytest.raises(ConfigError, match="SCDEMO_FAULT_NO_CONNECT"):
        Faults.from_env(
            Env({"SCDEMO_FAULT_NO_CONNECT": "nope"}), frozenset(), frozenset({"push"})
        )


def test_defaults() -> None:
    f = make({})
    assert (f.delay_bind, f.delay_connect, f.delay_ready) == (0.0, 0.0, 0.0)
    assert f.crash_after is None
    assert f.stop_sending_after is None
    assert f.spin_after is None
    assert f.no_ready is False
    assert f.ignore_config is False


def test_parses_timers_and_flags() -> None:
    f = make(
        {
            "SCDEMO_DELAY_BIND": "1.5",
            "SCDEMO_DELAY_CONNECT": "2",
            "SCDEMO_DELAY_READY": "3",
            "SCDEMO_FAULT_CRASH_AFTER": "4",
            "SCDEMO_FAULT_STOP_SENDING_AFTER": "5",
            "SCDEMO_FAULT_SPIN_AFTER": "6",
            "SCDEMO_FAULT_NO_READY": "1",
            "SCDEMO_FAULT_IGNORE_CONFIG": "1",
        }
    )
    assert (f.delay_bind, f.delay_connect, f.delay_ready) == (1.5, 2.0, 3.0)
    assert (f.crash_after, f.stop_sending_after, f.spin_after) == (4.0, 5.0, 6.0)
    assert f.no_ready is True
    assert f.ignore_config is True


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-1"])
@pytest.mark.parametrize(
    "name",
    [
        "SCDEMO_DELAY_BIND",
        "SCDEMO_DELAY_CONNECT",
        "SCDEMO_DELAY_READY",
        "SCDEMO_FAULT_CRASH_AFTER",
        "SCDEMO_FAULT_STOP_SENDING_AFTER",
        "SCDEMO_FAULT_SPIN_AFTER",
    ],
)
def test_rejects_non_finite_or_negative(name: str, bad: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make({name: bad})


@pytest.mark.asyncio
async def test_announce_ready_sends_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(notify, "sd_notify", lambda state: sent.append(state) or True)
    setup_logging("relay")
    await announce_ready(make({}))
    fields = parse_logfmt(capsys.readouterr().out.strip())
    assert fields["event"] == "ready"
    assert "skipped" not in fields
    assert sent == ["READY=1"]


@pytest.mark.asyncio
async def test_announce_ready_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(notify, "sd_notify", lambda state: sent.append(state) or True)
    setup_logging("relay")
    await announce_ready(make({"SCDEMO_FAULT_NO_READY": "1"}))
    fields = parse_logfmt(capsys.readouterr().out.strip())
    assert fields["event"] == "ready"
    assert fields["skipped"] == "1"
    assert sent == []
