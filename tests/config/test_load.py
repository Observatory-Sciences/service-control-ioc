from pathlib import Path

import pytest

from qd_service_control.config.load import (
    ConfigFileError,
    ConfigIssue,
    load_config,
    load_config_text,
)

REPO = Path(__file__).resolve().parents[2]
SCDEMO = REPO / "teststack/config/scdemo.yaml"

PAIRED_YAML = """
prefix: "X:SC"
top: fp
vars: {base: 15000}
services:
  fp:
    unit: "fp@{n}.service"
    instances: {from: 1, to: 2}
  fr:
    unit: "fr@{n}.service"
    instances: [1, 2]
    listeners:
      ctrl: {proto: tcp, port: "base + n"}
  xc:
    unit: xc.service
    listeners:
      daq:
        proto: udp
        port: "base + 149 + n"
        instances: {from: 1, to: 2}
edges:
  - {client: fp, server: fr.ctrl}
  - {client: fr, server: xc.daq, sender: connected}
"""

BAD_YAML = """
prefix: X
top: relay
services:
  relay:
    unit: relay.service
    bogus: 1
    listeners:
      rep: {proto: tcp, port: "base + 1"}
      udp: {proto: udp, port: 5000}
edges:
  - {client: relay, server: nope.rep}
  - {client: relay, server: relay.udp}
"""

HEAD = "prefix: P\ntop: svc\n"
SVC = "services:\n  svc: {unit: svc.service}\n"


def issues_of(text: str) -> dict[str, list[str]]:
    with pytest.raises(ConfigFileError) as err:
        load_config_text(text)
    out: dict[str, list[str]] = {}
    for issue in err.value.issues:
        out.setdefault(issue.path, []).append(issue.message)
    return out


def test_scdemo_reference_config_expands():
    spec = load_config(SCDEMO)
    assert set(spec.services) == {"sink@1", "sink@2", "relay", "controller"}
    assert spec.services["sink@2"].unit == "scdemo-sink@2.service"
    assert spec.listeners["sink@2.stats"].port == 20022
    assert spec.listeners["relay.pull"].phase == "post"
    assert spec.listeners["hw.ctrl"].on_host is True
    assert set(spec.edges) >= {
        "relay -> sink@1.stats",
        "relay -> sink@2.stats",
        "sink@1 -> relay.pull",
        "sink@2 -> relay.pull",
        "controller -> relay.rep",
        "hw -> relay.udp",
    }
    assert spec.edges["relay -> sink@1.udp"].sender == "connected"
    assert spec.edges["relay -> sink@2.udp"].sender == "unconnected"
    assert len(spec.edges) == 12
    assert spec.groups["all"].members == ("sink@1", "sink@2", "relay", "controller")


def test_hardware_flag_off_drops_host_and_edges():
    text = SCDEMO.read_text().replace("hardware: true", "hardware: false")
    spec = load_config_text(text)
    assert "hw" not in spec.hosts
    assert "relay -> hw.ctrl" not in spec.edges
    assert len(spec.edges) == 10


def test_paired_instances():  # FP n -> FR n, and FR n -> xc.daq@n (listener-level instances)
    spec = load_config_text(PAIRED_YAML)
    assert set(spec.edges) == {
        "fp@1 -> fr@1.ctrl",
        "fp@2 -> fr@2.ctrl",
        "fr@1 -> xc.daq@1",
        "fr@2 -> xc.daq@2",
    }
    assert spec.listeners["xc.daq@2"].port == 15151
    assert spec.edges["fr@1 -> xc.daq@1"].proto == "udp"
    assert spec.edges["fp@1 -> fr@1.ctrl"].sender == "tcp"


def test_errors_are_collected_with_paths():
    with pytest.raises(ConfigFileError) as e:
        load_config_text(BAD_YAML)
    paths = {i.path for i in e.value.issues}
    assert {
        "services.relay.bogus",
        "services.relay.listeners.rep.port",
        "edges[0].server",
        "edges[1].sender",
    } <= paths


def test_issue_str_and_file_error_carries_file():
    assert str(ConfigIssue("a.b", "boom")) == "a.b: boom"
    with pytest.raises(ConfigFileError) as e:
        load_config_text("bogus: 1", Path("x.yaml"))
    assert e.value.file == Path("x.yaml")


def test_unknown_top_level_key_is_error():
    assert "bogus" in issues_of(HEAD + SVC + "bogus: 1\n")


def test_shape_error_missing_required_key():
    assert "prefix" in issues_of("top: svc\n" + SVC)


def test_vars_may_not_define_n():
    assert "vars.n" in issues_of(HEAD + "vars: {n: 1}\n" + SVC)


def test_unknown_flag_in_when_is_error():
    text = HEAD + "services:\n  svc: {unit: s.service, when: nope}\n"
    assert "services.svc.when" in issues_of(text)


def test_when_false_drops_elements_silently():
    text = (
        HEAD
        + """
flags: {disabled: false}
services:
  svc: {unit: s.service}
  opt:
    unit: o.service
    when: disabled
    files: [{path: /run/o.pid}]
    listeners:
      l: {proto: tcp, port: 5001}
  keep:
    unit: k.service
    files: [{path: /run/k.pid, when: disabled}]
    listeners:
      gone: {proto: tcp, port: 5002, when: disabled}
edges:
  - {client: svc, server: opt.l}
  - {client: opt, server: keep.gone}
  - {client: svc, server: keep.gone}
groups:
  g: {unit: g.service, members: [svc, opt]}
"""
    )
    spec = load_config_text(text)
    assert set(spec.services) == {"svc", "keep"}
    assert not spec.edges
    assert not spec.files
    assert not spec.listeners
    assert spec.groups["g"].members == ("svc",)


def test_instances_range_is_inclusive_and_list_form():
    text = (
        HEAD
        + """
services:
  svc: {unit: "s{n}.service", instances: [3, 5]}
  other: {unit: "o{n}.service", instances: {from: 2, to: 4}}
"""
    )
    assert set(load_config_text(text).services) == {
        "svc@3",
        "svc@5",
        "other@2",
        "other@3",
        "other@4",
    }
    assert load_config_text(text).services["svc@5"].instance == 5


def test_listener_instanced_by_owner_and_itself_is_error():
    text = (
        HEAD
        + """
services:
  svc:
    unit: "s{n}.service"
    instances: [1, 2]
    listeners:
      l: {proto: tcp, port: "5000 + n", instances: [1]}
"""
    )
    assert "services.svc.listeners.l.instances" in issues_of(text)


def test_defaults_and_timeout_resolution():
    text = (
        HEAD
        + """
defaults: {timeout: 12, poll_interval: 2.5}
services:
  svc:
    unit: s.service
    listeners:
      pre: {proto: tcp, port: 5001}
      post: {proto: tcp, port: 5002, phase: post}
      explicit: {proto: tcp, port: 5003, timeout: 4}
    files:
      - {path: /run/a.pid}
      - {path: /run/b.pid, phase: post}
"""
    )
    spec = load_config_text(text)
    assert spec.defaults.poll_interval == 2.5
    assert spec.defaults.fast_poll_interval == 0.1
    assert spec.defaults.fast_poll_window == 5.0
    assert spec.defaults.restart_limit == 3
    assert spec.services["svc"].timeout == 12
    assert spec.listeners["svc.pre"].timeout == 12
    assert spec.listeners["svc.post"].timeout is None
    assert spec.listeners["svc.explicit"].timeout == 4
    assert spec.files["svc:file0"].timeout == 12
    assert spec.files["svc:file1"].timeout is None


def test_unknown_defaults_field_is_error():
    assert "defaults.bogus" in issues_of(HEAD + "defaults: {bogus: 1}\n" + SVC)


def test_files_interpolate_and_default_non_empty():
    text = (
        HEAD
        + """
vars: {base: 7}
flags: {disabled: false}
services:
  svc:
    unit: "s{n}.service"
    instances: [1, 2]
    files:
      - {path: "/run/skipped{n}", when: disabled}
      - {path: "/run/s{n}-{base}.pid"}
      - {path: "/run/log{n}", non_empty: true}
"""
    )
    spec = load_config_text(text)
    assert spec.files["svc@2:file1"].path == "/run/s2-7.pid"
    assert spec.files["svc@2:file1"].non_empty is False
    assert spec.files["svc@2:file2"].path == "/run/log2"
    assert spec.files["svc@2:file2"].non_empty is True
    assert spec.services["svc@1"].files == ("svc@1:file1", "svc@1:file2")
    assert spec.files["svc@1:file1"].owner == "svc@1"


def test_service_required_defaults_true():
    text = (
        HEAD
        + "services:\n  svc: {unit: s.service}\n  opt: {unit: o.service, required: false}\n"
    )
    spec = load_config_text(text)
    assert spec.services["svc"].required is True
    assert spec.services["opt"].required is False


def test_unit_with_n_on_uninstanced_service_is_error():
    text = HEAD + 'services:\n  svc: {unit: "s{n}.service"}\n'
    assert "services.svc.unit" in issues_of(text)


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_port_out_of_range_is_error(port):
    text = (
        HEAD
        + f'services:\n  svc:\n    unit: s.service\n    listeners:\n      l: {{proto: tcp, port: "{port}"}}\n'
    )
    assert "services.svc.listeners.l.port" in issues_of(text)


def test_duplicate_instances_and_edges_are_errors():
    dup_inst = HEAD + 'services:\n  svc: {unit: "s{n}.service", instances: [1, 1]}\n'
    assert "services.svc.instances" in issues_of(dup_inst)
    dup_edge = (
        HEAD
        + """
services:
  svc:
    unit: s.service
    listeners:
      l: {proto: tcp, port: 5001}
edges:
  - {client: svc, server: svc.l}
  - {client: svc, server: svc.l}
"""
    )
    assert "edges[1]" in issues_of(dup_edge)


def test_service_and_host_keys_may_not_clash():
    text = HEAD + SVC + "hosts:\n  svc: {address: 127.0.0.2}\n"
    assert "hosts.svc" in issues_of(text)


def test_top_must_name_a_service():
    text = "prefix: P\ntop: nope\n" + SVC
    assert "top" in issues_of(text)


def test_group_members_must_name_services():
    text = HEAD + SVC + "groups:\n  g: {unit: g.service, members: [svc, nope]}\n"
    assert "groups.g.members[1]" in issues_of(text)


def test_edge_client_must_resolve():
    text = HEAD + SVC + "edges:\n  - {client: nope, server: svc.l}\n"
    assert "edges[0].client" in issues_of(text)


def test_edge_unknown_listener_and_malformed_server():
    text = (
        HEAD
        + SVC
        + "edges:\n  - {client: svc, server: svc.l}\n  - {client: svc, server: svc}\n"
    )
    found = issues_of(text)
    assert "edges[0].server" in found
    assert "edges[1].server" in found


def test_edge_unknown_flag_is_error():
    text = (
        HEAD
        + "services:\n  svc:\n    unit: s.service\n    listeners:\n      l: {proto: tcp, port: 5001}\n"
        + "edges:\n  - {client: svc, server: svc.l, when: nope}\n"
    )
    assert "edges[0].when" in issues_of(text)


TCP_SERVICE = """
services:
  svc:
    unit: s.service
    listeners:
      l: {proto: tcp, port: 5001}
hosts:
  h: {address: 127.0.0.2}
"""


def test_tcp_edge_sender_is_forbidden():
    text = (
        HEAD
        + TCP_SERVICE
        + "edges:\n  - {client: svc, server: svc.l, sender: connected}\n"
    )
    assert "edges[0].sender" in issues_of(text)


def test_udp_edge_requires_sender_and_host_client_must_be_remote():
    text = (
        HEAD
        + """
services:
  svc:
    unit: s.service
    listeners:
      u: {proto: udp, port: 5001}
hosts:
  h: {address: 127.0.0.2}
edges:
  - {client: svc, server: svc.u}
  - {client: h, server: svc.u, sender: connected}
  - {client: h, server: svc.u, sender: remote}
"""
    )
    found = issues_of(text)
    assert "edges[0].sender" in found
    assert "edges[1].sender" in found
    assert "edges[2].sender" not in found


def test_edge_sender_value_is_validated_by_schema():
    text = (
        HEAD + TCP_SERVICE + "edges:\n  - {client: svc, server: svc.l, sender: bogus}\n"
    )
    assert "edges[0].sender" in issues_of(text)


def test_edge_instances_filter_and_host_client():
    spec = load_config_text(SCDEMO.read_text())
    assert "relay -> sink@2.udp" in spec.edges
    assert spec.edges["hw -> relay.udp"].client_is_host is True
    assert spec.edges["relay -> hw.ctrl"].client_is_host is False
    assert spec.edges["relay -> hw.ctrl"].server == "hw.ctrl"


def test_edge_instances_without_instanced_end_is_error():
    text = (
        HEAD
        + TCP_SERVICE
        + "edges:\n  - {client: svc, server: svc.l, instances: [1]}\n"
    )
    assert "edges[0].instances" in issues_of(text)


def test_service_by_unit():
    spec = load_config_text(SCDEMO.read_text())
    found = spec.service_by_unit("scdemo-sink@2.service")
    assert found is not None
    assert found.id == "sink@2"
    assert spec.service_by_unit("nope.service") is None


def test_yaml_syntax_error_and_non_mapping():
    assert "<yaml>" in issues_of("a: [1, 2")
    assert "<root>" in issues_of("- 1\n- 2\n")


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigFileError) as e:
        load_config(tmp_path / "missing.yaml")
    assert e.value.file == tmp_path / "missing.yaml"
    assert e.value.issues[0].path == "<file>"


def test_tcp_edge_from_a_host_is_error():
    # A host can only be a remote UDP sender; a TCP edge from one used to pass
    # validation and crash the evaluator.
    text = HEAD + TCP_SERVICE + "edges:\n  - {client: h, server: svc.l}\n"
    found = issues_of(text)
    assert "edges[0].client" in found
    assert "remote" in found["edges[0].client"][0]


def test_duplicate_mapping_keys_are_errors():
    text = """
prefix: "X:SC"
top: a
services:
  a:
    unit: a.service
    listeners:
      p: {proto: tcp, port: 5000}
      p: {proto: tcp, port: 5001}
top: a
"""
    found = issues_of(text)
    assert list(found) == ["services.a.listeners.p", "top"]
    assert found["services.a.listeners.p"][0].startswith("duplicate key 'p' (line 9")
    assert "first at line 8" in found["services.a.listeners.p"][0]


def test_edge_expanding_to_nothing_is_error():
    text = """
prefix: "X:SC"
top: a
services:
  a:
    unit: a.service
    listeners:
      p: {proto: tcp, port: 5000}
  b:
    unit: "b@{n}.service"
    instances: {from: 1, to: 2}
edges:
  - {client: b, server: a.p, instances: [3]}
"""
    found = issues_of(text)
    assert list(found) == ["edges[0]"]
    assert "no edge" in found["edges[0]"][0]


def test_edge_dropped_by_when_stays_silent():
    text = """
prefix: "X:SC"
top: a
flags: {hw: false}
services:
  a:
    unit: a.service
    listeners:
      p: {proto: tcp, port: 5000}
  b:
    unit: b.service
    when: hw
edges:
  - {client: a, server: a.p, when: hw}
  - {client: b, server: a.p}
"""
    assert load_config_text(text).edges == {}


def test_group_id_equal_to_service_id_is_an_issue():
    text = HEAD + SVC + "groups:\n  svc: {unit: g.service, members: [svc]}\n"
    found = issues_of(text)
    assert "groups.svc" in found
    assert "also a service id" in found["groups.svc"][0]


def test_service_id_stack_is_an_issue():
    text = "prefix: P\ntop: stack\nservices:\n  stack: {unit: s.service}\n"
    found = issues_of(text)
    assert "services.stack" in found
    assert "reserved" in found["services.stack"][0]


def test_group_id_stack_is_an_issue():
    text = HEAD + SVC + "groups:\n  stack: {unit: g.service, members: [svc]}\n"
    found = issues_of(text)
    assert "groups.stack" in found
    assert "reserved" in found["groups.stack"][0]


@pytest.mark.parametrize(
    "key", ["poll_interval", "fast_poll_interval", "fast_poll_window"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_poll_settings_must_be_positive(key: str, value: float) -> None:
    issues = issues_of(HEAD + f"defaults: {{{key}: {value}}}\n" + SVC)
    assert f"defaults.{key}" in issues
