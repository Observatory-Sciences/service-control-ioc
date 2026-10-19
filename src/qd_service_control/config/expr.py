"""Safe integer expressions for config values.

Only integer literals, names, ``+ - * // %``, unary ``-`` and parentheses are
accepted. Expressions are parsed with ``ast`` and walked against a whitelist;
``eval`` is never used.
"""

import ast
import operator
from collections.abc import Callable, Mapping
from typing import Final


class ExprError(ValueError):
    """An expression or template is invalid or cannot be evaluated."""


_BINARY_OPS: Final[dict[type[ast.operator], Callable[[int, int], int]]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


def _eval_node(node: ast.expr, variables: Mapping[str, int]) -> int:
    match node:
        case ast.Constant(value=value) if type(value) is int:
            return value
        case ast.Name(id=name):
            try:
                value = variables[name]
            except KeyError:
                raise ExprError(f"unknown variable {name!r}") from None
            if type(value) is not int:
                raise ExprError(f"variable {name!r} is not an integer")
            return value
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return -_eval_node(operand, variables)
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _BINARY_OPS:
            lhs = _eval_node(left, variables)
            rhs = _eval_node(right, variables)
            try:
                return _BINARY_OPS[type(op)](lhs, rhs)
            except ZeroDivisionError:
                raise ExprError("division by zero") from None
        case _:
            raise ExprError(f"unsupported expression element: {ast.dump(node)}")


def eval_int(expr: int | str, variables: Mapping[str, int]) -> int:
    """Evaluate an integer expression with the given named integer variables."""
    if isinstance(expr, bool):
        raise ExprError("boolean is not an integer")
    if isinstance(expr, int):
        return expr
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except (SyntaxError, ValueError) as err:
        raise ExprError(f"invalid expression {expr!r}: {err}") from err
    return _eval_node(tree.body, variables)


def interpolate(template: str, variables: Mapping[str, int]) -> str:
    """Replace each ``{expr}`` with its integer value; ``{{``/``}}`` are literal."""
    out: list[str] = []
    i = 0
    length = len(template)
    while i < length:
        char = template[i]
        if char in "{}" and template[i + 1 : i + 2] == char:
            out.append(char)
            i += 2
        elif char == "{":
            end = template.find("}", i + 1)
            if end == -1:
                raise ExprError(f"unclosed '{{' in {template!r}")
            out.append(str(eval_int(template[i + 1 : end], variables)))
            i = end + 1
        elif char == "}":
            raise ExprError(f"unmatched '}}' in {template!r}")
        else:
            out.append(char)
            i += 1
    return "".join(out)
