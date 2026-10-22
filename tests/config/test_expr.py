import pytest

from service_control_ioc.config.expr import ExprError, eval_int, interpolate


def test_eval_int_arithmetic():
    assert eval_int("base + 10 * (n - 1)", {"base": 10000, "n": 3}) == 10020
    assert eval_int(15150, {}) == 15150
    # (7 // 2) % 3 == 0, so the whole expression is -1.
    assert eval_int("-1 + 7 // 2 % 3", {}) == -1


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os')",
        "n.real",
        "1 / 2",
        "x",
        "1 +",
        "2 ** 3",
        "'a'",
        "True",
        "1 // 0",
        "5 % 0",
        "+1",
        "n[0]",
        "",
    ],
)
def test_eval_int_rejects(bad):
    with pytest.raises(ExprError):
        eval_int(bad, {"n": 1})


def test_eval_int_rejects_bool_values():
    with pytest.raises(ExprError):
        eval_int(True, {})
    with pytest.raises(ExprError):
        eval_int("b", {"b": True})


def test_interpolate():
    assert interpolate("scdemo-sink@{n}.service", {"n": 2}) == "scdemo-sink@2.service"
    assert interpolate("192.168.0.{1 + n}", {"n": 1}) == "192.168.0.2"
    assert interpolate("{{literal}}", {}) == "{literal}"


@pytest.mark.parametrize("bad", ["a{n", "a}b", "{x}", "{}"])
def test_interpolate_rejects(bad):
    with pytest.raises(ExprError):
        interpolate(bad, {"n": 1})
