"""Load a YAML config: validate its shape, expand instances, check references."""

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import ValidationError

from qd_service_control.config.expr import ExprError, eval_int, interpolate
from qd_service_control.config.schema import (
    ConfigRaw,
    EdgeRaw,
    FileRaw,
    HostRaw,
    ListenerRaw,
    ServiceRaw,
)
from qd_service_control.config.spec import (
    Defaults,
    EdgeSpec,
    FileSpec,
    GroupSpec,
    HostSpec,
    ListenerSpec,
    Phase,
    Proto,
    Sender,
    ServiceSpec,
    StackSpec,
)

_MIN_PORT = 1
_MAX_PORT = 65535

_T = TypeVar("_T")


@dataclass(frozen=True)
class ConfigIssue:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class ConfigFileError(Exception):
    """The config file is unreadable or invalid; ``issues`` lists every problem."""

    def __init__(self, file: Path, issues: list[ConfigIssue]) -> None:
        super().__init__(f"{file}: {len(issues)} issue(s)")
        self.file = file
        self.issues = issues


def load_config(path: Path) -> StackSpec:
    """Read and expand the config at ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ConfigFileError(path, [ConfigIssue("<file>", str(err))]) from err
    return load_config_text(text, path)


def load_config_text(text: str, file: Path = Path("<string>")) -> StackSpec:
    """Parse, validate and expand config ``text``; ``file`` is only for messages."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        duplicates = _duplicate_keys(root, ()) if root is not None else []
        if duplicates:
            raise ConfigFileError(file, duplicates)
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigFileError(
            file, [ConfigIssue("<yaml>", _yaml_message(err))]
        ) from err
    if not isinstance(data, dict):
        raise ConfigFileError(
            file, [ConfigIssue("<root>", "expected a mapping at the top level")]
        )
    raw, issues = _validate_shape(data, file)
    expander = _Expander(raw, issues)
    spec = expander.expand()
    if expander.issues:
        raise ConfigFileError(file, expander.issues)
    return spec


def _duplicate_keys(node: yaml.Node, path: tuple[int | str, ...]) -> list[ConfigIssue]:
    """Repeated mapping keys, which YAML loaders silently resolve as last-wins."""
    issues: list[ConfigIssue] = []
    if isinstance(node, yaml.MappingNode):
        first: dict[str, yaml.Node] = {}
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, yaml.ScalarNode) else None
            if not isinstance(key, str) or key == "<<":
                continue
            if key in first:
                line = key_node.start_mark.line + 1
                column = key_node.start_mark.column + 1
                was = first[key].start_mark.line + 1
                issues.append(
                    ConfigIssue(
                        _format_loc((*path, key)),
                        f"duplicate key {key!r} (line {line}, column {column});"
                        f" first at line {was}",
                    )
                )
            else:
                first[key] = key_node
            issues.extend(_duplicate_keys(value_node, (*path, key)))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            issues.extend(_duplicate_keys(item, (*path, index)))
    return issues


def _yaml_message(err: yaml.YAMLError) -> str:
    if isinstance(err, yaml.MarkedYAMLError) and err.problem_mark is not None:
        mark = err.problem_mark
        return f"{err.problem} (line {mark.line + 1}, column {mark.column + 1})"
    return " ".join(str(err).split())


def _format_loc(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "<root>"


def _validate_shape(
    data: dict[Any, Any], file: Path
) -> tuple[ConfigRaw, list[ConfigIssue]]:
    """Validate with pydantic. Unknown keys are reported but, so that later
    problems are still found in the same run, removed and validation retried.
    Any other shape error stops here."""
    try:
        return ConfigRaw.model_validate(data), []
    except ValidationError as err:
        errors = err.errors()
    issues = [ConfigIssue(_format_loc(e["loc"]), e["msg"]) for e in errors]
    if any(e["type"] != "extra_forbidden" for e in errors):
        raise ConfigFileError(file, issues)
    cleaned = copy.deepcopy(data)
    for e in errors:
        node: Any = cleaned  # walking the YAML-shaped tree named by the error loc
        for key in e["loc"][:-1]:
            node = node[key]
        del node[e["loc"][-1]]
    return ConfigRaw.model_validate(cleaned), issues


@dataclass
class _ListenerInfo:
    proto: Proto
    instances: list[int] | None
    dropped: bool


@dataclass
class _OwnerInfo:
    kind: Literal["services", "hosts"]
    instances: list[int] | None
    dropped: bool
    ids: list[str] = field(default_factory=list)
    listeners: dict[str, _ListenerInfo] = field(default_factory=dict)


class _Expander:
    def __init__(self, raw: ConfigRaw, issues: list[ConfigIssue]) -> None:
        self.raw = raw
        self.issues = issues
        self._seen = {(i.path, i.message) for i in issues}
        self.defaults = Defaults(**raw.defaults.model_dump())
        self.vars: dict[str, int] = {k: v for k, v in raw.vars.items() if k != "n"}
        self.owners: dict[str, _OwnerInfo] = {}
        self.services: dict[str, ServiceSpec] = {}
        self.hosts: dict[str, HostSpec] = {}
        self.listeners: dict[str, ListenerSpec] = {}
        self.files: dict[str, FileSpec] = {}
        self.edges: dict[str, EdgeSpec] = {}
        self.groups: dict[str, GroupSpec] = {}

    def issue(self, path: str, message: str) -> None:
        if (path, message) not in self._seen:
            self._seen.add((path, message))
            self.issues.append(ConfigIssue(path, message))

    def expand(self) -> StackSpec:
        if "n" in self.raw.vars:
            self.issue("vars.n", "'n' is reserved for the instance number")
        for key in self.raw.hosts:
            if key in self.raw.services:
                self.issue(f"hosts.{key}", f"key {key!r} is already a service")
        for key, service in self.raw.services.items():
            self._service(key, service)
        for key, host in self.raw.hosts.items():
            if key not in self.raw.services:
                self._host(key, host)
        for index, edge in enumerate(self.raw.edges):
            self._edge(index, edge)
        self._top_and_groups()
        return StackSpec(
            prefix=self.raw.prefix,
            top=self.raw.top,
            flags=dict(self.raw.flags),
            defaults=self.defaults,
            services=self.services,
            hosts=self.hosts,
            listeners=self.listeners,
            files=self.files,
            edges=self.edges,
            groups=self.groups,
        )

    # -- helpers ---------------------------------------------------------

    def _keep(self, when: str | None, path: str) -> bool:
        """False when ``when`` names a false flag. An unknown flag is an error."""
        if when is None:
            return True
        if when not in self.raw.flags:
            self.issue(path, f"unknown flag {when!r}")
            return True
        return self.raw.flags[when]

    def _variables(self, n: int | None) -> dict[str, int]:
        return self.vars if n is None else {**self.vars, "n": n}

    def _timeout(self, phase: Phase, timeout: float | None) -> float | None:
        if timeout is not None:
            return timeout
        return self.defaults.timeout if phase == "pre" else None

    def _text(self, path: str, template: str, n: int | None) -> str | None:
        return self._try(path, interpolate, template, self._variables(n))

    def _try(self, path: str, func: Callable[..., _T], *args: object) -> _T | None:
        try:
            return func(*args)
        except ExprError as err:
            self.issue(path, str(err))
            return None

    def _put(self, table: dict[str, _T], key: str, value: _T, path: str) -> bool:
        if key in table:
            self.issue(path, f"duplicate id {key!r}")
            return False
        table[key] = value
        return True

    def _instance_numbers(
        self, instances: list[int] | None, path: str
    ) -> list[int | None]:
        if instances is None:
            return [None]
        unique = list(dict.fromkeys(instances))
        if len(unique) != len(instances):
            self.issue(path, "duplicate instance numbers")
        return list(unique)

    # -- services and hosts ---------------------------------------------

    def _register(
        self,
        kind: Literal["services", "hosts"],
        key: str,
        raw: ServiceRaw | HostRaw,
    ) -> tuple[_OwnerInfo, str]:
        base = f"{kind}.{key}"
        dropped = not self._keep(raw.when, f"{base}.when")
        info = _OwnerInfo(kind, raw.instances, dropped)
        for name, lraw in raw.listeners.items():
            lpath = f"{base}.listeners.{name}"
            keep = self._keep(lraw.when, f"{lpath}.when")
            if raw.instances is not None and lraw.instances is not None:
                self.issue(
                    f"{lpath}.instances",
                    "listener is instanced by its owner and by itself",
                )
            info.listeners[name] = _ListenerInfo(
                lraw.proto, lraw.instances, dropped or not keep
            )
        self.owners[key] = info
        return info, base

    def _service(self, key: str, raw: ServiceRaw) -> None:
        info, base = self._register("services", key, raw)
        if info.dropped:
            return
        for n in self._instance_numbers(raw.instances, f"{base}.instances"):
            sid = key if n is None else f"{key}@{n}"
            info.ids.append(sid)
            listener_ids = self._listeners(sid, n, raw.listeners, info, base, None)
            file_ids = [
                fid
                for index, fraw in enumerate(raw.files)
                if (fid := self._file(sid, n, index, fraw, f"{base}.files[{index}]"))
            ]
            unit = self._text(f"{base}.unit", raw.unit, n)
            if unit is None:
                continue
            self._put(
                self.services,
                sid,
                ServiceSpec(
                    id=sid,
                    key=key,
                    instance=n,
                    unit=unit,
                    listeners=tuple(listener_ids),
                    files=tuple(file_ids),
                    required=raw.required,
                    timeout=(
                        raw.timeout
                        if raw.timeout is not None
                        else self.defaults.timeout
                    ),
                ),
                base,
            )

    def _host(self, key: str, raw: HostRaw) -> None:
        info, base = self._register("hosts", key, raw)
        if info.dropped:
            return
        for n in self._instance_numbers(raw.instances, f"{base}.instances"):
            hid = key if n is None else f"{key}@{n}"
            info.ids.append(hid)
            address = self._text(f"{base}.address", raw.address, n)
            if address is None:
                continue
            listener_ids = self._listeners(hid, n, raw.listeners, info, base, address)
            self._put(
                self.hosts,
                hid,
                HostSpec(
                    id=hid,
                    key=key,
                    instance=n,
                    address=address,
                    listeners=tuple(listener_ids),
                ),
                base,
            )

    def _file(
        self, owner: str, n: int | None, index: int, raw: FileRaw, path: str
    ) -> str | None:
        if not self._keep(raw.when, f"{path}.when"):
            return None
        text = self._text(f"{path}.path", raw.path, n)
        if text is None:
            return None
        fid = f"{owner}:file{index}"
        spec = FileSpec(
            id=fid,
            owner=owner,
            path=text,
            non_empty=raw.non_empty,
            phase=raw.phase,
            required=raw.required,
            timeout=self._timeout(raw.phase, raw.timeout),
        )
        return fid if self._put(self.files, fid, spec, path) else None

    def _listeners(
        self,
        owner: str,
        owner_n: int | None,
        raws: Mapping[str, ListenerRaw],
        info: _OwnerInfo,
        base: str,
        host_address: str | None,
    ) -> list[str]:
        ids: list[str] = []
        for name, raw in raws.items():
            if info.listeners[name].dropped:
                continue
            path = f"{base}.listeners.{name}"
            if owner_n is not None:
                numbers: list[int | None] = [None]
            elif raw.instances is not None:
                numbers = self._instance_numbers(raw.instances, f"{path}.instances")
            else:
                numbers = [None]
            for ln in numbers:
                n = owner_n if owner_n is not None else ln
                lid = f"{owner}.{name}" + ("" if ln is None else f"@{ln}")
                port = self._try(f"{path}.port", eval_int, raw.port, self._variables(n))
                if port is None:
                    continue
                if not _MIN_PORT <= port <= _MAX_PORT:
                    self.issue(f"{path}.port", f"port {port} is outside 1..65535")
                    continue
                spec = ListenerSpec(
                    id=lid,
                    owner=owner,
                    name=name,
                    proto=raw.proto,
                    port=port,
                    address=raw.address if raw.address is not None else host_address,
                    phase=raw.phase,
                    required=raw.required,
                    timeout=self._timeout(raw.phase, raw.timeout),
                    on_host=host_address is not None,
                )
                if self._put(self.listeners, lid, spec, path):
                    ids.append(lid)
        return ids

    # -- edges -----------------------------------------------------------

    def _edge(self, index: int, raw: EdgeRaw) -> None:
        path = f"edges[{index}]"
        keep = self._keep(raw.when, f"{path}.when")
        client = self.owners.get(raw.client)
        if client is None:
            self.issue(f"{path}.client", f"unknown service or host {raw.client!r}")
        owner_key, dot, name = raw.server.partition(".")
        server = linfo = None
        if not dot or not name:
            self.issue(f"{path}.server", "expected '<owner>.<listener>'")
        elif (server := self.owners.get(owner_key)) is None:
            self.issue(f"{path}.server", f"unknown service or host {owner_key!r}")
        elif (linfo := server.listeners.get(name)) is None:
            self.issue(f"{path}.server", f"{owner_key!r} has no listener {name!r}")
        if client is None or server is None or linfo is None:
            return
        if not keep or client.dropped or server.dropped or linfo.dropped:
            return
        sender = self._sender(path, raw, linfo.proto, client)
        if sender is None:
            return

        client_n, server_n = client.instances, server.instances or linfo.instances
        if client_n and server_n:
            numbers: list[int | None] = [n for n in client_n if n in server_n]
        elif client_n or server_n:
            numbers = list(client_n or server_n or [])
        else:
            numbers = [None]
        if raw.instances is not None:
            if numbers == [None]:
                self.issue(f"{path}.instances", "neither end of the edge is instanced")
                return
            numbers = [n for n in numbers if n in raw.instances]
        if not numbers:
            self.issue(
                path,
                "the instance numbers of its ends (and its 'instances') leave no edge",
            )
            return

        for n in dict.fromkeys(numbers):
            cid = raw.client if not client_n else f"{raw.client}@{n}"
            if server.instances:
                lid = f"{owner_key}@{n}.{name}"
            elif linfo.instances:
                lid = f"{owner_key}.{name}@{n}"
            else:
                lid = f"{owner_key}.{name}"
            edge = EdgeSpec(
                id=f"{cid} -> {lid}",
                client=cid,
                client_is_host=client.kind == "hosts",
                server=lid,
                proto=linfo.proto,
                sender=sender,
                phase=raw.phase,
                required=raw.required,
                timeout=self._timeout(raw.phase, raw.timeout),
            )
            self._put(self.edges, edge.id, edge, path)

    def _sender(
        self, path: str, raw: EdgeRaw, proto: Proto, client: _OwnerInfo
    ) -> Sender | None:
        if proto == "tcp":
            if client.kind == "hosts":
                self.issue(
                    f"{path}.client",
                    "an edge from a host must be a UDP edge with sender 'remote'"
                    " (a host's TCP connections cannot be observed)",
                )
                return None
            if raw.sender is not None:
                self.issue(f"{path}.sender", "sender is not allowed on a TCP edge")
                return None
            return "tcp"
        if raw.sender is None:
            self.issue(
                f"{path}.sender",
                "sender is required on a UDP edge (connected, unconnected or remote)",
            )
            return None
        if client.kind == "hosts" and raw.sender != "remote":
            self.issue(
                f"{path}.sender", "an edge from a host must have sender 'remote'"
            )
            return None
        return raw.sender

    # -- top and groups ---------------------------------------------------

    def _top_and_groups(self) -> None:
        top = self.owners.get(self.raw.top)
        if top is None or top.kind != "services" or top.dropped:
            self.issue("top", f"{self.raw.top!r} is not a service")
        for gkey, group in self.raw.groups.items():
            members: list[str] = []
            for index, member in enumerate(group.members):
                info = self.owners.get(member)
                if info is None or info.kind != "services":
                    self.issue(
                        f"groups.{gkey}.members[{index}]",
                        f"{member!r} is not a service",
                    )
                else:
                    members.extend(info.ids)
            self.groups[gkey] = GroupSpec(
                id=gkey, unit=group.unit, members=tuple(members)
            )
