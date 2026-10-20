"""Every PV name, derived from a ``StackSpec`` in one place.

The config loader checks names with ``pv_issues``. The IOC, the EDM generator and
the tests build them with ``pv_names``, so a name is never spelled twice.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from qd_service_control.config.spec import StackSpec

STACK_FIELDS: Final = ("READY", "STATE", "POST_READY", "FAULTS", "FAULT_MSG", "EVENTS")
CONTROL_FIELDS: Final = ("START", "STOP", "RESTART", "LASTJOB")
SERVICE_FIELDS: Final = (
    "STATE",
    "UNIT",
    "ACTIVE",
    "READY",
    "NRESTARTS",
    "EXIT",
    "CPU",
    "LOG",
    "FINDING",
    *CONTROL_FIELDS,
)
ELEMENT_FIELDS: Final = ("STATE", "DESC", "DETAIL", "SINCE")
GROUP_FIELDS: Final = ("STATE", *CONTROL_FIELDS)
MAX_PV: Final = 60

_TOKEN_UNSAFE: Final = re.compile(r"[^A-Z0-9_]")
_PV_SAFE: Final = re.compile(r"[A-Za-z0-9_:-]+")


def token(text: str) -> str:
    """Upper-case ``text``, drop ``@`` and turn any other odd character into ``_``."""
    return _TOKEN_UNSAFE.sub("_", text.upper().replace("@", ""))


@dataclass(frozen=True)
class PvNames:
    prefix: str
    stack_controls: bool
    services: Mapping[str, str]
    elements: Mapping[str, str]
    groups: Mapping[str, str]

    def pv(self, base: str, field: str) -> str:
        """Full PV name; an empty ``base`` is the stack level."""
        middle = f":{base}" if base else ""
        return f"{self.prefix}{middle}:{field}"

    def all(self) -> tuple[str, ...]:
        """Every served PV, sorted."""
        return tuple(sorted(name for name, _ in _entries(self)))


def _stack_fields(stack_controls: bool) -> tuple[str, ...]:
    return (*STACK_FIELDS, *CONTROL_FIELDS) if stack_controls else STACK_FIELDS


def _listener_token(listener_id: str, name: str) -> str:
    """The listener's token; an instanced listener gets its number appended."""
    _, at, number = listener_id.rpartition(".")[2].partition("@")
    return token(name + (number if at else ""))


def _owner_base(spec: StackSpec, owner: str) -> str:
    service = spec.services.get(owner)
    return service.pv if service is not None else token(owner)


def pv_names(spec: StackSpec) -> PvNames:
    """Derive the base of every service, listener, file, edge and group."""
    services = {sid: service.pv for sid, service in spec.services.items()}
    elements: dict[str, str] = {}
    for lid, listener in spec.listeners.items():
        if not listener.on_host:
            elements[lid] = (
                f"{_owner_base(spec, listener.owner)}:"
                f"{_listener_token(lid, listener.name)}"
            )
    for fid, file in spec.files.items():
        number = fid.rpartition(":file")[2]
        elements[fid] = f"{_owner_base(spec, file.owner)}:FILE{number}"
    for eid, edge in spec.edges.items():
        server = spec.listeners[edge.server]
        elements[eid] = (
            f"E:{_owner_base(spec, edge.client)}-{_owner_base(spec, server.owner)}"
            f"-{_listener_token(server.id, server.name)}"
        )
    groups = {gid: f"G:{token(gid)}" for gid in spec.groups}
    return PvNames(
        prefix=spec.prefix,
        stack_controls=spec.stack_group is not None,
        services=services,
        elements=elements,
        groups=groups,
    )


def _entries(names: PvNames, spec: StackSpec | None = None) -> list[tuple[str, str]]:
    """``(full name, yaml path)`` for every PV. Without ``spec`` the path is empty."""
    entries = [(names.pv("", f), "prefix") for f in _stack_fields(names.stack_controls)]
    for key, base in names.services.items():
        path = f"services.{spec.services[key].key}.pv" if spec else ""
        entries += [(names.pv(base, f), path) for f in SERVICE_FIELDS]
    for key, base in names.elements.items():
        path = _element_path(spec, key) if spec else ""
        entries += [(names.pv(base, f), path) for f in ELEMENT_FIELDS]
    for base in names.groups.values():
        entries += [(names.pv(base, f), "prefix") for f in GROUP_FIELDS]
    return entries


def _element_path(spec: StackSpec, element_id: str) -> str:
    """The YAML path of the service behind a listener or file; else ``prefix``."""
    listener = spec.listeners.get(element_id)
    file = spec.files.get(element_id)
    owner = listener.owner if listener else file.owner if file else None
    service = spec.services.get(owner) if owner else None
    return f"services.{service.key}.pv" if service else "prefix"


def pv_issues(spec: StackSpec) -> list[tuple[str, str]]:
    """``(yaml path, message)`` for unsafe, too long and duplicate PV names."""
    issues: list[tuple[str, str]] = []
    for service in spec.services.values():
        if not _PV_SAFE.fullmatch(service.pv):
            issues.append(
                (f"services.{service.key}.pv", f"pv '{service.pv}' is not PV-safe")
            )
    names = pv_names(spec)
    entries = _entries(names, spec)
    seen: set[str] = set()
    reported: set[str] = set()
    for name, path in entries:
        if name in seen and name not in reported:
            reported.add(name)
            issues.append((path, f"duplicate pv name: {name}"))
        seen.add(name)
    # One report per base: the longest name is the one that breaks the limit.
    longest: dict[str, tuple[str, str]] = {}
    for name, path in entries:
        size = len(name.encode())
        if size > MAX_PV:
            key = name.rpartition(":")[0]
            if key not in longest or size > len(longest[key][0].encode()):
                longest[key] = (name, path)
    issues += [
        (path, f"pv name too long: {name} ({len(name.encode())} > {MAX_PV} bytes)")
        for name, path in longest.values()
    ]
    return issues
