"""Frozen, fully expanded configuration values consumed by the rest of the app."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Proto = Literal["tcp", "udp"]
Phase = Literal["pre", "post"]
Sender = Literal["tcp", "connected", "unconnected", "remote"]


@dataclass(frozen=True)
class Defaults:
    poll_interval: float = 1.0
    fast_poll_interval: float = 0.1  # while units start (spec 6.3)
    fast_poll_window: float = 5.0  # s of fast polls after a unit change
    timeout: float = 30.0
    restart_limit: int = 3
    restart_window: float = 60.0
    cpu_threshold: float = 90.0
    cpu_window: float = 10.0
    journal_lines: int = 20
    journal_bytes: int = 5120
    job_timeout: float = 120.0


@dataclass(frozen=True)
class ListenerSpec:
    id: str
    owner: str
    name: str
    proto: Proto
    port: int
    address: str | None
    phase: Phase
    required: bool
    timeout: float | None
    on_host: bool


@dataclass(frozen=True)
class FileSpec:
    id: str
    owner: str
    path: str
    non_empty: bool
    phase: Phase
    required: bool
    timeout: float | None


@dataclass(frozen=True)
class ServiceSpec:
    id: str
    key: str
    instance: int | None
    unit: str
    pv: str
    listeners: tuple[str, ...]
    files: tuple[str, ...]
    required: bool
    timeout: float


@dataclass(frozen=True)
class HostSpec:
    id: str
    key: str
    instance: int | None
    address: str
    listeners: tuple[str, ...]


@dataclass(frozen=True)
class EdgeSpec:
    id: str
    client: str
    client_is_host: bool
    server: str
    proto: Proto
    sender: Sender
    phase: Phase
    required: bool
    timeout: float | None


@dataclass(frozen=True)
class GroupSpec:
    id: str
    unit: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class StackSpec:
    prefix: str
    top: str
    flags: Mapping[str, bool]
    defaults: Defaults
    services: Mapping[str, ServiceSpec]
    hosts: Mapping[str, HostSpec]
    listeners: Mapping[str, ListenerSpec]
    files: Mapping[str, FileSpec]
    edges: Mapping[str, EdgeSpec]
    groups: Mapping[str, GroupSpec]
    stack_group: str | None

    def service_by_unit(self, unit: str) -> ServiceSpec | None:
        """Return the service whose systemd unit name is ``unit``, if any."""
        for service in self.services.values():
            if service.unit == unit:
                return service
        return None
