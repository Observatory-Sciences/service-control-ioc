"""Raw YAML shape, validated by pydantic. Expansion happens in ``load``."""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from qd_service_control.config.spec import Phase, Proto

_RESERVED_RANGE_KEYS = {"from", "to"}


def _expand_instances(value: object) -> object:
    """Turn ``{from: a, to: b}`` into the inclusive list; pass lists through."""
    if not isinstance(value, dict):
        return value
    if set(value) != _RESERVED_RANGE_KEYS:
        raise ValueError("expected {from: a, to: b} or a list of integers")
    low, high = value["from"], value["to"]
    if not all(type(x) is int for x in (low, high)):
        raise ValueError("'from' and 'to' must be integers")
    return list(range(low, high + 1))


def _expression_text(value: object) -> object:
    """Allow a bare integer where an expression string is expected."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


Instances = Annotated[
    list[int], BeforeValidator(_expand_instances), Field(min_length=1)
]
Expression = Annotated[str, BeforeValidator(_expression_text)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DefaultsRaw(_Strict):
    poll_interval: float = 1.0
    timeout: float = 30.0
    restart_limit: int = 3
    restart_window: float = 60.0
    cpu_threshold: float = 90.0
    cpu_window: float = 10.0
    journal_lines: int = 20
    journal_bytes: int = 5120


class ListenerRaw(_Strict):
    proto: Proto
    port: Expression
    address: str | None = None
    phase: Phase = "pre"
    required: bool = True
    timeout: float | None = None
    when: str | None = None
    instances: Instances | None = None


class FileRaw(_Strict):
    path: str
    non_empty: bool = False
    phase: Phase = "pre"
    required: bool = True
    timeout: float | None = None
    when: str | None = None


class ServiceRaw(_Strict):
    unit: str
    instances: Instances | None = None
    required: bool = True
    timeout: float | None = None
    when: str | None = None
    listeners: dict[str, ListenerRaw] = Field(default_factory=dict)
    files: list[FileRaw] = Field(default_factory=list)


class HostRaw(_Strict):
    address: str
    instances: Instances | None = None
    when: str | None = None
    listeners: dict[str, ListenerRaw] = Field(default_factory=dict)


class EdgeRaw(_Strict):
    client: str
    server: str
    sender: Literal["connected", "unconnected", "remote"] | None = None
    phase: Phase = "pre"
    required: bool = True
    timeout: float | None = None
    when: str | None = None
    instances: Instances | None = None


class GroupRaw(_Strict):
    unit: str
    members: list[str]


class ConfigRaw(_Strict):
    prefix: str
    top: str
    vars: dict[str, int] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    defaults: DefaultsRaw = Field(default_factory=DefaultsRaw)
    services: dict[str, ServiceRaw] = Field(default_factory=dict)
    hosts: dict[str, HostRaw] = Field(default_factory=dict)
    edges: list[EdgeRaw] = Field(default_factory=list)
    groups: dict[str, GroupRaw] = Field(default_factory=dict)
