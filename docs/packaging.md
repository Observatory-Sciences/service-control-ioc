# Packaging service-control-ioc in `qd-rpm-build`

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
  process (they all run as `xspress`) restart any unit in the list. The
  generated rule also returns NO for every other manage-units request from
  that user (other units, and other verbs such as reload), so reusing
  `xspress` would restrict its other systemd actions.

The unit gets the group through `SupplementaryGroups=systemd-journal`, so the
user's own group list need not change if only the tool needs it.

## 2. The tool venv, from `uv.lock`

Build a self-contained venv at build time from the lock, runtime dependencies
only and non-editable:

```bash
UV_PROJECT_ENVIRONMENT=/opt/service-control-ioc/venv \
    uv sync --frozen --no-dev --no-editable --package service-control-ioc
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
Description=service-control-ioc for the QD Xspress stack
After=dbus.service
[Service]
Type=simple
User=qdctl
Group=qdctl
SupplementaryGroups=systemd-journal
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/xspress.d/epics.env
ExecStart=/opt/service-control-ioc/venv/bin/service-control-ioc /etc/xspress.d/service-control-ioc.yaml
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
`/etc/polkit-1/rules.d/40-service-control-ioc.rules`, mode 0644:

```bash
service-control-generate-polkit service-control-ioc.yaml --user qdctl \
    > 40-service-control-ioc.rules
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
service-control-generate-edm service-control-ioc.yaml <edl-dir>
```

Install the generated `.edl` files with the other QD screens (where
`qd-epics-ioc.spec` installs `ODNProcServ.edl` today) and link the overview
from the main Xspress screen.

### Size budget

Every screen must fit 1900 x 1040 px, which the generator checks. Pass
`--max-size WxH` for a different display. A screen over the budget is an
error (exit code 2) naming the file and its size.

### Extra screens

A screen the generator can't draw, such as a wiring diagram, is a separate
`.edl` file that a script writes next to the generated ones. Add a button for
it to the top screen's header with `--link`, once for each screen:

```bash
service-control-generate-edm service-control-ioc.yaml <edl-dir> \
    --link 'Wiring=xspress-sc-wiring.edl'
```

`LABEL` is the button's text. `FILE.edl` is a file name in the same
directory as the top screen, not a path. Too many links for the header's
width is an error.

The script can use the generator's own building blocks, so it draws with the
same objects and PV names. The public API is:

- `service_control_ioc.pvnames.pv_names(spec)` returns a `PvNames`. Its
  `pv(base, field)` gives the full PV name, and `services`, `elements` and
  `groups` map a YAML id to its base. An empty `base` is the stack level.
- `service_control_ioc.edm.edl` has one function for each EDM object, each
  returning the object's `.edl` text: `screen`, `text`, `update`,
  `multiline`, `button`, `related`, `led`, `line` and `exit_button`. Also
  `STATE_COLOURS` and `OVERALL_COLOURS`, the colours for an element or
  service state and for the overall state.
- `line(points, pv, colours, *, width=2, dashed=False, arrow=False)` draws
  a polyline through `points` (a list of `(x, y)`) that takes the colour for
  the state in `pv`, for example an edge's `STATE` with `STATE_COLOURS`.
  Use it for the wires of a diagram. `arrow=True` puts an arrowhead on the
  last point.
- `service_control_ioc.edm.screens` has `default_name(prefix)`, the screen
  name the CLI uses when `--name` isn't given. It also has
  `top_file(name)`, `detail_file(name, base)` and `edges_file(name)`, the
  file names of the top screen, of a service's detail screen and of the
  "All edges" screen, to use as the target of `edl.related`. `Size`, `Link`, `DEFAULT_BUDGET`, `generate` and
  `edges_screen` are there too.

For example, a button for every service that opens its detail screen, and
a wire that follows an edge's state:

```python
from pathlib import Path

from service_control_ioc.config.load import load_config
from service_control_ioc.edm import edl
from service_control_ioc.edm.screens import default_name, detail_file
from service_control_ioc.pvnames import pv_names

spec = load_config(Path("service-control-ioc.yaml"))
names = pv_names(spec)
name = default_name(spec.prefix)
objects = [
    edl.related(10, 10 + 24 * i, 120, 20, sid, detail_file(name, base))
    for i, (sid, base) in enumerate(names.services.items())
]
edge = names.elements[next(iter(spec.edges))]  # the first edge
objects.append(
    edl.line(
        [(140, 20), (300, 20)],
        names.pv(edge, "STATE"),
        edl.STATE_COLOURS,
        arrow=True,
    )
)
Path("extra.edl").write_text(edl.screen(320, 300, "Extra", objects))
```

[qd-rpm-build#62](https://github.com/Observatory-Sciences/qd-rpm-build/issues/62)
is the first user: a hand-designed Xspress wiring diagram, linked with
`--link 'Wiring=xspress-sc-wiring.edl'`.

## 6. Retire procServControl

Once the tool is packaged and proven on the test VM:

- Remove `qd-xspress-procserv-control.service`, its `procserv-control.boot`
  and its template (`specs/epics-configs/templates/procserv-control.boot.template`)
  from `qd-epics-ioc.spec`.
- Remove `ODNProcServ.edl` and point its menu entries at the generated screen.
- `qd-check-connections.sh` and the docs (`deployment-engineer.md`,
  `fault-finding.md`) then name the new unit instead of the procServ one.
