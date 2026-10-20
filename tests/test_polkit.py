import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from qd_service_control import main
from qd_service_control.config.load import load_config
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
    main(["generate-polkit", str(SCDEMO), "--user", "scdemo"])
    out = capsys.readouterr().out
    assert out == polkit_rule(load_config(SCDEMO), "scdemo")


def test_generate_polkit_cli_needs_user(capsys) -> None:
    with pytest.raises(SystemExit) as e:
        main(["generate-polkit", str(SCDEMO)])
    assert e.value.code == 2
    assert "--user" in capsys.readouterr().err


_HARNESS = """
const fs = require("fs");
const rule = fs.readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(process.argv[2]);
let fn = null;
const logs = [];
const polkit = {
  Result: {YES: "yes", NO: "no", NOT_HANDLED: "not_handled"},
  addRule: function (f) { fn = f; },
  log: function (m) { logs.push(m); },
};
new Function("polkit", rule)(polkit);
const out = cases.map(function (c) {
  const action = {id: c.id, lookup: function (k) { return c[k]; }};
  logs.length = 0;
  const result = fn(action, {user: c.user});
  return [result, logs.slice()];
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_polkit_rule_behaviour_under_node(tmp_path: Path) -> None:
    rule_file = tmp_path / "40-scdemo-sc.rules"
    rule_file.write_text(polkit_rule(load_config(SCDEMO), "scdemo"))
    manage = "org.freedesktop.systemd1.manage-units"
    cases = [
        {
            "id": manage,
            "user": "scdemo",
            "unit": "scdemo-sink@1.service",
            "verb": "restart",
        },
        {"id": manage, "user": "scdemo", "unit": "scdemo-all.service", "verb": "stop"},
        {
            "id": manage,
            "user": "scdemo",
            "unit": "scdemo-hwsim.service",
            "verb": "start",
        },
        {
            "id": manage,
            "user": "scdemo",
            "unit": "scdemo-relay.service",
            "verb": "kill",
        },
        {
            "id": manage,
            "user": "scdemo",
            "unit": "scdemo-sink@3.service",
            "verb": "start",
        },
        {
            "id": manage,
            "user": "other",
            "unit": "scdemo-relay.service",
            "verb": "start",
        },
        {"id": "org.freedesktop.login1.reboot", "user": "scdemo"},
    ]
    proc = subprocess.run(
        ["node", "-e", _HARNESS, str(rule_file), json.dumps(cases)],
        capture_output=True,
        text=True,
        check=True,
    )
    results = json.loads(proc.stdout)
    assert [r[0] for r in results] == [
        "yes",
        "yes",
        "no",
        "no",
        "no",
        "not_handled",
        "not_handled",
    ]
    assert results[0][1] == []
    assert results[2][1] == [
        "qd-service-control: denied scdemo unit=scdemo-hwsim.service verb=start"
    ]
    assert results[5][1] == []


def test_generate_polkit_does_not_import_softioc() -> None:
    code = (
        "import sys\n"
        "from qd_service_control import main\n"
        "main(['generate-polkit', sys.argv[1], '--user', 'scdemo'])\n"
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
