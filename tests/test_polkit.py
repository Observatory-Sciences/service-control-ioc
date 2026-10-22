import json
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from qd_service_control.cli.generate_polkit import main
from qd_service_control.config.load import load_config
from qd_service_control.config.spec import StackSpec
from qd_service_control.polkit import polkit_rule

REPO = Path(__file__).resolve().parents[1]
SCDEMO = REPO / "teststack/config/scdemo.yaml"
UNITS = [
    "scdemo-all.service",
    "scdemo-controller.service",
    "scdemo-relay.service",
    "scdemo-sink@1.service",
    "scdemo-sink@2.service",
]


def array_after(rule: str, name: str) -> list[str]:
    """The JSON array literal assigned to ``var <name>`` in the rule."""
    match = re.search(rf"var {name} = (\[[^\]]*\]);", rule)
    assert match, f"no 'var {name} = [...]' in rule:\n{rule}"
    return json.loads(match.group(1))


def test_polkit_rule_lists_units_exactly() -> None:
    rule = polkit_rule(load_config(SCDEMO), "scdemo")
    assert array_after(rule, "units") == UNITS
    assert array_after(rule, "verbs") == ["start", "stop", "restart"]
    assert 'action.id !== "org.freedesktop.systemd1.manage-units"' in rule
    assert 'subject.user !== "scdemo"' in rule
    assert "polkit.Result.NOT_HANDLED" in rule
    assert "polkit.log(" in rule
    # An exact list: no regular expression literal or constructor anywhere.
    assert "RegExp" not in rule
    assert ".test(" not in rule
    assert not re.search(r"=\s*/", rule)
    # ES5: no let/const/arrow functions/template literals.
    assert not re.search(r"\b(let|const)\b|=>|`", rule)


def test_polkit_rule_escapes_user() -> None:
    rule = polkit_rule(load_config(SCDEMO), 'a"b\\c')
    assert 'subject.user !== "a\\"b\\\\c"' in rule


def test_generate_polkit_cli(capsys) -> None:
    main([str(SCDEMO), "--user", "scdemo"])
    out = capsys.readouterr().out
    assert out == polkit_rule(load_config(SCDEMO), "scdemo")


def test_generate_polkit_cli_needs_user(capsys) -> None:
    with pytest.raises(SystemExit) as e:
        main([str(SCDEMO)])
    assert e.value.code == 2
    assert "--user" in capsys.readouterr().err


_HARNESS = """
const fs = require("fs");
const rule = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(process.argv[2]);
const fns = [];
const logs = [];
const polkit = {
  Result: {YES: "yes", NO: "no", NOT_HANDLED: "not_handled"},
  addRule: function (f) { fns.push(f); },
  log: function (m) { logs.push(m); },
};
new Function("polkit", rule)(polkit);
const out = cases.map(function (c) {
  const action = {id: c.id, lookup: function (k) { return c[k]; }};
  logs.length = 0;
  const results = fns.map(function (fn) { return fn(action, {user: c.user}); });
  return [results, logs.slice()];
});
process.stdout.write(JSON.stringify(out));
"""
MANAGE = "org.freedesktop.systemd1.manage-units"
EVIL = 'x\n"polkit.addRule(function(){return "yes"});\u2028y=1;\u2029//'
# Valid JS once it leaves the comment: adds a second rule that allows anything.
EVIL_RULE = '\npolkit.addRule(function(){return "yes"})//'


def manage(user: str, unit: str, verb: str) -> dict[str, str]:
    return {"id": MANAGE, "user": user, "unit": unit, "verb": verb}


def run_rule(tmp_path: Path, rule: str, cases: list[dict[str, str]]) -> list:
    """Evaluate ``rule`` under node with a fake ``polkit``: per case, the
    results of every rule added and the log lines."""
    rule_file = tmp_path / "40-scdemo-sc.rules"
    rule_file.write_text(rule, encoding="utf-8")
    proc = subprocess.run(
        ["node", "-e", _HARNESS, str(rule_file), json.dumps(cases)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
def test_polkit_rule_behaviour_under_node(tmp_path: Path) -> None:
    cases = [
        manage("scdemo", "scdemo-sink@1.service", "restart"),
        manage("scdemo", "scdemo-all.service", "stop"),
        manage("scdemo", "scdemo-hwsim.service", "start"),
        manage("scdemo", "scdemo-relay.service", "kill"),
        manage("scdemo", "scdemo-sink@3.service", "start"),
        manage("other", "scdemo-relay.service", "start"),
        {"id": "org.freedesktop.login1.reboot", "user": "scdemo"},
    ]
    results = run_rule(tmp_path, polkit_rule(load_config(SCDEMO), "scdemo"), cases)
    assert [r[0] for r in results] == [
        ["yes"],
        ["yes"],
        ["no"],
        ["no"],
        ["no"],
        ["not_handled"],
        ["not_handled"],
    ]
    assert results[0][1] == []
    assert results[2][1] == [
        "qd-service-control: denied scdemo unit=scdemo-hwsim.service verb=start"
    ]
    assert results[5][1] == []


def evil_spec(evil: str = EVIL) -> StackSpec:
    """The scdemo spec with JS-hostile text in the prefix and one unit name
    (not loadable from YAML; this tests the generator on its own)."""
    spec = load_config(SCDEMO)
    services = dict(spec.services)
    services["relay"] = replace(services["relay"], unit=f"r{evil}.service")
    return replace(spec, prefix=f"P{evil}", services=services)


_JS_STRING = re.compile(r'"(?:[^"\\\n\u2028\u2029]|\\.)*"')


def skeleton(rule: str) -> list[str]:
    """The rule's lines with every string literal replaced by ``S``."""
    return [_JS_STRING.sub("S", line) for line in rule.split("\n")]


def test_polkit_rule_keeps_yaml_text_inside_literals() -> None:
    clean = polkit_rule(load_config(SCDEMO), "scdemo")
    rule = polkit_rule(evil_spec(), f"u{EVIL}")
    # No JS line terminator, and outside string literals the rule is the
    # clean rule: nothing from the YAML or the user name leaves a literal.
    assert "\u2028" not in rule
    assert "\u2029" not in rule
    assert "\r" not in rule
    assert skeleton(rule) == skeleton(clean)


@needs_node
@pytest.mark.parametrize("evil", [EVIL, EVIL_RULE])
def test_polkit_rule_injection_under_node(tmp_path: Path, evil: str) -> None:
    user = f"u{evil}"
    unit = f"r{evil}.service"
    cases = [
        manage(user, unit, "restart"),
        manage(user, "scdemo-sink@1.service", "start"),
        manage(user, "scdemo-hwsim.service", "start"),
        manage("scdemo", "scdemo-relay.service", "start"),
        {"id": "org.freedesktop.login1.reboot", "user": user},
    ]
    results = run_rule(tmp_path, polkit_rule(evil_spec(evil), user), cases)
    # Exactly one rule was added, and it decides as the clean rule would.
    assert [r[0] for r in results] == [
        ["yes"],
        ["yes"],
        ["no"],
        ["not_handled"],
        ["not_handled"],
    ]


def test_generate_polkit_does_not_import_softioc() -> None:
    code = (
        "import sys\n"
        "from qd_service_control.cli.generate_polkit import main\n"
        "main([sys.argv[1], '--user', 'scdemo'])\n"
        "assert 'softioc' not in sys.modules\n"
        "assert 'qd_service_control.ioc.app' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(SCDEMO)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "polkit.addRule(" in result.stdout
