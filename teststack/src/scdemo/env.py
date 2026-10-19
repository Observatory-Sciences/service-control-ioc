"""Typed reading of ``SCDEMO_*`` environment variables."""

import functools
import sys
from collections.abc import Callable, Mapping
from typing import Final

# Env defines methods named int/float/str, which shadow the builtins inside the
# class body, so annotations there use these aliases.
_Int: Final = int
_Float: Final = float
_Str: Final = str


class ConfigError(Exception):
    """An environment variable has an invalid value. The message names it."""


class Env:
    """A view of an environment mapping with validated, typed accessors."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def int(self, name: _Str, default: _Int) -> _Int:
        raw = self._environ.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as err:
            raise ConfigError(f"{name}: not an integer: {raw!r}") from err

    def float(self, name: _Str, default: _Float) -> _Float:
        raw = self._environ.get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError as err:
            raise ConfigError(f"{name}: not a number: {raw!r}") from err

    def str(self, name: _Str, default: _Str) -> _Str:
        return self._environ.get(name, default)

    def flag(self, name: _Str) -> bool:
        """``"1"`` is true, unset or ``"0"`` is false, anything else is an error."""
        raw = self._environ.get(name)
        if raw is None or raw == "0":
            return False
        if raw == "1":
            return True
        raise ConfigError(f"{name}: must be 0 or 1: {raw!r}")

    def names(self, name: _Str, allowed: frozenset[_Str]) -> frozenset[_Str]:
        """A comma-separated set of names, each of which must be in ``allowed``."""
        raw = self._environ.get(name, "")
        found = frozenset(part.strip() for part in raw.split(",") if part.strip())
        unknown = sorted(found - allowed)
        if unknown:
            raise ConfigError(
                f"{name}: unknown name {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})"
            )
        return found


def exit_on_config_error(fn: Callable[[], None]) -> Callable[[], None]:
    """Decorate a console-script ``main``: ``ConfigError`` becomes one stderr line and exit 2."""

    @functools.wraps(fn)
    def wrapper() -> None:
        try:
            fn()
        except ConfigError as err:
            print(f"scdemo: {err}", file=sys.stderr)
            raise SystemExit(
                2
            ) from None  # the message is the whole story, no traceback

    return wrapper
