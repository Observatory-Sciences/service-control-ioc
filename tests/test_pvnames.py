from pathlib import Path

import pytest
import yaml

from qd_service_control.config.load import (
    ConfigFileError,
    load_config,
    load_config_text,
)
from qd_service_control.pvnames import (
    MAX_PV,
    SERVICE_FIELDS,
    pv_issues,
    pv_names,
    token,
)

REPO = Path(__file__).resolve().parents[1]
SCDEMO = REPO / "teststack/config/scdemo.yaml"


def issues_of(text: str) -> dict[str, list[str]]:
    with pytest.raises(ConfigFileError) as err:
        load_config_text(text)
    out: dict[str, list[str]] = {}
    for issue in err.value.issues:
        out.setdefault(issue.path, []).append(issue.message)
    return out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sink@1", "SINK1"),
        ("a-b", "A_B"),
        ("a.b c", "A_B_C"),
        ("Relay_2", "RELAY_2"),
    ],
)
def test_token(text: str, expected: str) -> None:
    assert token(text) == expected


def test_scdemo_pv_names() -> None:
    names = pv_names(load_config(SCDEMO))
    everything = names.all()
    assert len(everything) == 155
    assert len(set(everything)) == 155
    assert list(everything) == sorted(everything)
    for expected in (
        "SCDEMO:SC:STATE",
        "SCDEMO:SC:START",
        "SCDEMO:SC:SINK1:STATE",
        "SCDEMO:SC:SINK1:UDP:STATE",
        "SCDEMO:SC:RELAY:PULL:DESC",
        "SCDEMO:SC:E:RELAY-SINK1-STATS:STATE",
        "SCDEMO:SC:E:HW-RELAY-UDP:STATE",
        "SCDEMO:SC:E:CONTROLLER-RELAY-ROUTER:SINCE",
        "SCDEMO:SC:G:ALL:RESTART",
    ):
        assert expected in everything
    assert all(len(n.encode()) <= MAX_PV for n in everything)
    assert names.services["sink@1"] == "SINK1"
    assert names.pv(names.services["relay"], "LOG") == "SCDEMO:SC:RELAY:LOG"
    assert names.pv("", "READY") == "SCDEMO:SC:READY"


def test_scdemo_breakdown() -> None:
    names = pv_names(load_config(SCDEMO))
    everything = names.all()
    assert sum(1 for n in everything if ":E:" in n) == 48
    assert sum(1 for n in everything if ":G:" in n) == 5
    assert len(names.services) * len(SERVICE_FIELDS) == 52


def test_host_listeners_have_no_pvs() -> None:
    everything = pv_names(load_config(SCDEMO)).all()
    assert not any(n.startswith("SCDEMO:SC:HW") for n in everything)


def test_no_stack_group_no_stack_controls() -> None:
    text = SCDEMO.read_text().replace("stack_group: all\n", "")
    assert "stack_group" not in text
    names = pv_names(load_config_text(text))
    assert names.stack_controls is False
    everything = names.all()
    assert len(everything) == 151  # 155 minus START, STOP, RESTART, LASTJOB
    assert "SCDEMO:SC:START" not in everything
    assert "SCDEMO:SC:LASTJOB" not in everything
    assert "SCDEMO:SC:STATE" in everything


def test_service_pv_override_with_instance() -> None:
    text = """
prefix: "SCDEMO:SC"
top: s
services:
  s:
    unit: "s@{n}.service"
    pv: "S{n}"
    instances: [1, 2]
"""
    spec = load_config_text(text)
    assert spec.services["s@1"].pv == "S1"
    assert "SCDEMO:SC:S1:STATE" in pv_names(spec).all()
    assert "SCDEMO:SC:S2:STATE" in pv_names(spec).all()


def test_service_pv_defaults_to_token() -> None:
    spec = load_config(SCDEMO)
    assert spec.services["sink@2"].pv == "SINK2"
    assert spec.services["relay"].pv == "RELAY"


def test_pv_name_too_long_is_config_issue() -> None:
    text = SCDEMO.read_text().replace("SCDEMO:SC", "P" * 40)
    issues = issues_of(text)
    messages = [m for ms in issues.values() for m in ms]
    assert any(m.startswith("pv name too long") for m in messages)
    assert all("> 60 bytes" in m for m in messages if "too long" in m)
    # Service-derived names point at the service, the rest at the prefix.
    assert "services.controller.pv" in issues
    assert "prefix" in issues


def test_duplicate_pv_name_is_config_issue() -> None:
    text = """
prefix: P
top: a_b
services:
  a_b: {unit: x.service}
  a-b: {unit: y.service}
"""
    messages = [m for ms in issues_of(text).values() for m in ms]
    assert "duplicate pv name: P:A_B:STATE" in messages


def test_unsafe_pv_override_is_config_issue() -> None:
    text = 'prefix: P\ntop: s\nservices:\n  s: {unit: x.service, pv: "a b"}\n'
    assert issues_of(text)["services.s.pv"] == ["pv 'a b' is not PV-safe"]


def load_yaml_scalar(yaml_text: str) -> str:
    return str(yaml.safe_load(f'"{yaml_text}"'))


def issues_prefix(yaml_text: str) -> str:
    """The prefix value YAML gives for ``yaml_text`` in double quotes."""
    return load_yaml_scalar(yaml_text)


@pytest.mark.parametrize("prefix", ["P X", "P\\nX=1;//", "P\\u2028", 'P\\"Q', "P.Q"])
def test_unsafe_prefix_is_config_issue(prefix: str) -> None:
    text = f'prefix: "{prefix}"\ntop: s\nservices:\n  s: {{unit: x.service}}\n'
    issues = issues_of(text)
    assert list(issues) == ["prefix"]
    (message,) = issues["prefix"]
    assert message == f"prefix {issues_prefix(prefix)!r} is not PV-safe"


def test_safe_prefixes_load() -> None:
    assert load_config(SCDEMO).prefix == "SCDEMO:SC"
    text = 'prefix: "A-b_9:X"\ntop: s\nservices:\n  s: {unit: x.service}\n'
    assert load_config_text(text).prefix == "A-b_9:X"


def test_unknown_stack_group_is_config_issue() -> None:
    text = SCDEMO.read_text().replace("stack_group: all", "stack_group: nope")
    assert issues_of(text)["stack_group"] == ["unknown group 'nope'"]


def test_job_timeout_default_and_override() -> None:
    assert load_config(SCDEMO).defaults.job_timeout == 120.0
    text = SCDEMO.read_text().replace(
        "vars: {base: 20000}", "vars: {base: 20000}\ndefaults: {job_timeout: 5}"
    )
    assert load_config_text(text).defaults.job_timeout == 5.0


def test_pv_issues_clean_for_scdemo() -> None:
    assert pv_issues(load_config(SCDEMO)) == []


def test_instanced_listener_gets_its_number() -> None:
    text = """
prefix: P
top: xc
services:
  xc:
    unit: xc.service
    listeners:
      daq: {proto: udp, port: 5000, instances: [1, 2]}
"""
    everything = pv_names(load_config_text(text)).all()
    assert "P:XC:DAQ1:STATE" in everything
    assert "P:XC:DAQ2:STATE" in everything
