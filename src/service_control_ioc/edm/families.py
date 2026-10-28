"""Fold an expanded stack into per-family instance rows (no EDM output).

A family is every instance of one YAML service key. A row is one listener,
file or edge, with a cell per instance number (``None`` for a singleton).
Hosts are not a family: their listeners have no PVs. They still appear as
edge servers.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from service_control_ioc.config.spec import EdgeSpec, StackSpec


@dataclass(frozen=True)
class Row:
    label: str  # listener name, "file<i>", or edge label
    cells: Mapping[int | None, str]  # instance -> element or edge id


@dataclass(frozen=True)
class Family:
    key: str  # YAML service key
    members: Mapping[int | None, str]  # instance -> service id
    rows: tuple[Row, ...]  # listeners then files, YAML order


@dataclass(frozen=True)
class EdgeRow(Row):
    post: bool  # any edge in the row has phase "post"


def instance_columns(spec: StackSpec) -> tuple[int, ...]:
    """Return the sorted instance numbers used by services, listeners and edges."""
    numbers = {s.instance for s in spec.services.values()}
    numbers |= {listener.instance for listener in spec.listeners.values()}
    numbers |= {e.instance for e in spec.edges.values()}
    return tuple(sorted(n for n in numbers if n is not None))


def service_families(spec: StackSpec) -> tuple[Family, ...]:
    """Group services by YAML key, in YAML order, with their listener and file rows."""
    keys: dict[str, list[str]] = {}
    for service in spec.services.values():
        keys.setdefault(service.key, []).append(service.id)
    return tuple(_family(spec, key, ids) for key, ids in keys.items())


def _family(spec: StackSpec, key: str, ids: list[str]) -> Family:
    members = {spec.services[sid].instance: sid for sid in ids}
    listener_rows: dict[str, dict[int | None, str]] = {}
    file_rows: dict[str, dict[int | None, str]] = {}
    for sid in ids:
        service = spec.services[sid]
        for lid in service.listeners:
            listener = spec.listeners[lid]
            listener_rows.setdefault(listener.name, {})[listener.instance] = lid
        for fid in service.files:
            label = fid.partition(":")[2]
            file_rows.setdefault(label, {})[service.instance] = fid
    rows = tuple(Row(label, cells) for label, cells in listener_rows.items())
    rows += tuple(Row(label, cells) for label, cells in file_rows.items())
    return Family(key, members, rows)


def edge_rows(spec: StackSpec) -> tuple[EdgeRow, ...]:
    """Group edges by YAML ``edges:`` entry, in YAML order, one row per entry."""
    templates: dict[int, list[EdgeSpec]] = {}
    for edge in spec.edges.values():
        templates.setdefault(edge.template, []).append(edge)
    return tuple(
        EdgeRow(
            _edge_label(spec, edges[0]),
            {e.instance: e.id for e in edges},
            any(e.phase == "post" for e in edges),
        )
        for _, edges in sorted(templates.items())
    )


def _owner_key(spec: StackSpec, owner: str) -> str:
    if owner in spec.hosts:
        return spec.hosts[owner].key
    return spec.services[owner].key


def _edge_label(spec: StackSpec, edge: EdgeSpec) -> str:
    server = spec.listeners[edge.server]
    return (
        f"{_owner_key(spec, edge.client)} -> "
        f"{_owner_key(spec, server.owner)}.{server.name}"
    )
