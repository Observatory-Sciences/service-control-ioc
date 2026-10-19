from qd_service_control.model.deps import DependencyGraph

AFTER = {
    "ctl": ["relay", "net.target"],
    "relay": ["sink@1", "sink@2"],
    "all": ["sink@1", "sink@2", "relay", "ctl"],
}
CONFIGURED = {"sink@1", "sink@2", "relay", "ctl", "all"}


def make() -> DependencyGraph:
    return DependencyGraph(AFTER, CONFIGURED, exclude={"all"})


def test_dependents_are_transitive_and_exclude() -> None:
    assert make().dependents_of("sink@1") == {"relay", "ctl"}


def test_dependencies_are_transitive_and_configured_only() -> None:
    assert make().dependencies_of("ctl") == {"relay", "sink@1", "sink@2"}


def test_cycles_terminate() -> None:
    graph = DependencyGraph({"a": ["b"], "b": ["a"]}, ["a", "b"])
    assert graph.dependents_of("a") == {"a", "b"}
    assert graph.dependencies_of("a") == {"a", "b"}


def test_unknown_unit_has_no_relations() -> None:
    assert make().dependents_of("nope") == frozenset()
