import pytest

from scdemo.env import ConfigError, Env, exit_on_config_error


def test_env_parses_and_defaults() -> None:
    env = Env({"SCDEMO_PORT_BASE": "30000", "SCDEMO_DELAY_BIND": "1.5"})
    assert env.int("SCDEMO_PORT_BASE", 20000) == 30000
    assert env.float("SCDEMO_DELAY_BIND", 0.0) == 1.5
    assert env.int("SCDEMO_SINK_COUNT", 2) == 2
    assert env.str("SCDEMO_HW_HOST", "127.0.0.2") == "127.0.0.2"


def test_env_bad_number_names_variable() -> None:
    with pytest.raises(ConfigError, match="SCDEMO_DELAY_BIND"):
        Env({"SCDEMO_DELAY_BIND": "abc"}).float("SCDEMO_DELAY_BIND", 0.0)
    with pytest.raises(ConfigError, match="SCDEMO_PORT_BASE"):
        Env({"SCDEMO_PORT_BASE": "1.5"}).int("SCDEMO_PORT_BASE", 20000)


def test_env_names_rejects_unknown() -> None:
    env = Env({"SCDEMO_FAULT_NO_BIND": "udp,stats"})
    allowed = frozenset({"udp", "stats"})
    assert env.names("SCDEMO_FAULT_NO_BIND", allowed) == {"udp", "stats"}
    with pytest.raises(ConfigError, match="bogus"):
        Env({"SCDEMO_FAULT_NO_BIND": "bogus"}).names(
            "SCDEMO_FAULT_NO_BIND", frozenset({"udp"})
        )


def test_env_names_unset_is_empty() -> None:
    assert Env({}).names("SCDEMO_FAULT_NO_BIND", frozenset({"udp"})) == frozenset()


def test_flag_values() -> None:
    assert Env({"F": "1"}).flag("F") is True
    assert Env({"F": "0"}).flag("F") is False
    assert Env({}).flag("F") is False


def test_flag_rejects_garbage() -> None:
    with pytest.raises(ConfigError, match="SCDEMO_FAULT_NO_READY"):
        Env({"SCDEMO_FAULT_NO_READY": "yes"}).flag("SCDEMO_FAULT_NO_READY")


def test_exit_on_config_error_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    @exit_on_config_error
    def main() -> None:
        raise ConfigError("SCDEMO_X: bad")

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert capsys.readouterr().err == "scdemo: SCDEMO_X: bad\n"


def test_exit_on_config_error_passes_through() -> None:
    calls: list[int] = []

    @exit_on_config_error
    def main() -> None:
        calls.append(1)

    main()
    assert calls == [1]


def test_float_or_none() -> None:
    assert Env({}).float_or_none("X") is None
    assert Env({"X": "2.5"}).float_or_none("X") == 2.5
    with pytest.raises(ConfigError, match="X"):
        Env({"X": "abc"}).float_or_none("X")
