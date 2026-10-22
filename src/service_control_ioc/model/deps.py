"""Unit ordering graph. Only ``After=`` orders units."""

from collections.abc import Iterable, Mapping


class DependencyGraph:
    """Transitive ordering queries over the configured units."""

    def __init__(
        self,
        after: Mapping[str, Iterable[str]],
        configured: Iterable[str],
        exclude: Iterable[str] = (),
    ) -> None:
        self._configured = frozenset(configured)
        self._exclude = frozenset(exclude)
        self._after: dict[str, frozenset[str]] = {
            unit: frozenset(deps) & self._configured
            for unit, deps in after.items()
            if unit in self._configured
        }
        before: dict[str, set[str]] = {}
        for unit, deps in self._after.items():
            for dep in deps:
                before.setdefault(dep, set()).add(unit)
        self._before = {unit: frozenset(users) for unit, users in before.items()}

    def dependents_of(self, unit: str) -> frozenset[str]:
        """Configured units ordered after ``unit``, transitively, minus exclude."""
        return _reach(self._before, unit) - self._exclude

    def dependencies_of(self, unit: str) -> frozenset[str]:
        """Configured units that ``unit`` is ordered after, transitively."""
        return _reach(self._after, unit)


def _reach(edges: Mapping[str, frozenset[str]], start: str) -> frozenset[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        for nxt in edges.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return frozenset(seen)
