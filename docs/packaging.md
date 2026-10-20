# Packaging qd-service-control in `qd-rpm-build`

These are proposals for `qd-rpm-build` (spec Section 9.3). They are described
here and not made: `qd-rpm-build` changes need the maintainers' approval. The
scdemo test stack (`teststack/`) already installs the tool this way, so each
item below has a working model in this repo.

## 1. A user for the tool, in `systemd-journal`

The tool follows its units' journals with `journalctl`, which needs
`systemd-journal` (or `adm`/`wheel`) membership. Without it `journalctl -f`
exits at once with "insufficient permissions".

Two options:

- **A dedicated `qdctl` system user (recommended).** Create it with
  `useradd -r -M -s /sbin/nologin` in `%pre`, as `qd-epics-ioc.spec` does for
  `xspress`, and add it to `systemd-journal`. The polkit rule then names only
  `qdctl`, so no stack process can start or stop its siblings.
- **Reuse `xspress`.** This is simpler, but the rule would then let every QD
  process (they all run as `xspress`) restart any unit in the list.

The unit gets the group through `SupplementaryGroups=systemd-journal`, so the
user's own group list need not change if only the tool needs it.

## 2. The tool venv, from `uv.lock`

Build a self-contained venv at build time from the lock, runtime dependencies
only and non-editable:

```bash
UV_PROJECT_ENVIRONMENT=/opt/qd-service-control/venv \
    uv sync --frozen --no-dev --no-editable --package qd-service-control
```

`teststack/install.sh` does exactly this. Notes for the SPEC:

- **x86_64 needs no toolchain.** PyPI has manylinux x86_64 cp311 wheels for
  softioc and its EPICS libraries (plan 3, Ruling 10), so the build only
  downloads. No `gcc`, `make` or `python3.11-devel` in `BuildRequires`. Plan 4
  confirms this on the x86_64 test VM.
- **aarch64 builds softioc from source.** The lock's
  `[tool.uv.extra-build-dependencies]` supplies `epicscorelibs` to the build;
  `uv sync` honours it (checked in the aarch64 container, uv 0.13.1). It needs
  `gcc gcc-c++ make perl python3.11-devel`. This matters only if an aarch64
  package is ever wanted.
- The venv uses the `epicscorelibs` wheel's libca, not `/opt/QD/epics/base`.
- The venv's interpreter is `/usr/bin/python3.11`, so the RPM `Requires:
  python3.11`.
- `aioca` is a dev-only dependency and is not in the venv.

## 3. The unit

Model it on `teststack/systemd/scdemo-sc.service`:

```ini
[Unit]
Description=qd-service-control for the QD Xspress stack
After=dbus.service
[Service]
Type=simple
User=qdctl
Group=qdctl
SupplementaryGroups=systemd-journal
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/xspress.d/epics.env
ExecStart=/opt/qd-service-control/venv/bin/qd-service-control run /etc/xspress.d/qd-service-control.yaml
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
```

- It is not `PartOf=qd-xspress-all.service`, and no stack unit depends on it
  (spec Section 2): the tool must keep running while the stack stops, starts
  or fails.
- `EnvironmentFile=/etc/xspress.d/epics.env` puts its PVs on the stack's CA
  port (6064) next to the other QD IOCs.
- Never run it with `python -O` (softioc's `longStringIn` bound is only an
  `assert`).

## 4. The polkit rule

Generate it at build time from the same YAML, and install it as
`/etc/polkit-1/rules.d/40-qd-service-control.rules`, mode 0644:

```bash
qd-service-control generate-polkit qd-service-control.yaml --user qdctl \
    > 40-qd-service-control.rules
```

The rule handles only `org.freedesktop.systemd1.manage-units` for that user.
It allows `start`, `stop` and `restart` on an exact list of units (every
service unit, template instances in full, plus every group unit such as
`qd-xspress-all.service`). It logs and denies anything else from that user,
and leaves every other user and action to later rules. The `40-` prefix makes
it run before EL9's `49-polkit-pkla-compat` and `50-default`. polkitd reloads
rules when the file changes. Mark it `%config(noreplace)` only if sites are
expected to edit it; regenerating it from the YAML is the intended way to
change it.

## 5. The EDM screen, generated at build time

```bash
qd-service-control generate-edm qd-service-control.yaml <edl-dir>
```

Install the generated `.edl` files with the other QD screens (where
`qd-epics-ioc.spec` installs `ODNProcServ.edl` today) and link the overview
from the main Xspress screen.

## 6. Retire procServControl

Once the tool is packaged and proven on the test VM:

- Remove `qd-xspress-procserv-control.service`, its `procserv-control.boot`
  and its template (`specs/epics-configs/templates/procserv-control.boot.template`)
  from `qd-epics-ioc.spec`.
- Remove `ODNProcServ.edl` and point its menu entries at the generated screen.
- `qd-check-connections.sh` and the docs (`deployment-engineer.md`,
  `fault-finding.md`) then name the new unit instead of the procServ one.
