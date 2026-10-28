# service-control-ioc: v1 design

Date: 2026-10-19. Status: draft for review.

This design builds on [`docs/project-outline.md`](../../project-outline.md)
(the outline). The outline's problem statement, concepts (Section 4) and
architecture (Section 5) still hold, except where this document revises them
(Section 3). Section 12 lists the calls made here on the user's behalf.

## 1. Intent

On a QD Xspress system every service can show as running while one or more
of the connections between them is missing. The xspress IOC starts last and
sends a "configure" command down through the stack to the firmware. That
command then fails, and nothing the user can see says why. The service
status still shows green while the detector doesn't work. The user can't
resolve it, it can take weeks to get an engineer, and the engineer often
needs hours of debugging to find the missing link.

v1 fixes the false green. The user must see clearly, as it happens, that
the stack is **not ready** and which service or connection is missing. The
user isn't expected to fix it. The detail (which side is at fault, the exit
status, the unit's recent journal lines) is there for the engineer who
looks later, and should cut their hours to minutes.

## 2. Goals, non-goals, anti-goals

### Goals

1. A trustworthy **stack ready** verdict, meaning "safe for the top level to
   configure". It covers every configured unit, listener and required
   `pre` edge (Section 5.3). Edges that only appear after the top level has
   acted (`post`, for example the detector-card links) are shown, but they
   don't count toward it.
2. Per-element status (service, listener, edge), as it changes, with a
   state that separates "still starting" from "broken".
3. Diagnosis for the engineer: the faulty element, the likely side, the
   last journal lines, the exit status and restart count, and a
   `journalctl` reference.
4. Detecting gate violations: a dependent unit that started before its
   dependency was ready.
5. Start, stop and restart of units and group targets from the screen.
6. A generated EDM screen showing all of the above.
7. v1 is proven on a purpose-built test stack under real systemd, both
   locally and on the test VM (Section 10).

### Non-goals (v1)

- Gating or orchestrating startup. systemd and the units' own helpers gate
  it. The tool only reports (outline D1 stands). The tool is not a
  dependency of any stack unit.
- Application-level checks (ZMQ handshakes, configure success, traffic
  counts). Edges are transport level (outline D5, D6).
- Strict PV compatibility with `procServControl` or `odinprocservcontrol`
  (revises outline D12; see Section 3).
- The Xspress configuration and its validation on the real QD stack. That
  is the first follow-up after v1, and the design must make it
  straightforward (Section 11).
- Fan-in/fan-out test topologies. Multi-host stacks.
- RPM SPECs. Packaging stays in `qd-rpm-build`. This repo ships files
  ready for it to package (Section 9).
- Stack shapes that change at runtime. The shape is fixed per install, and
  a configuration change takes effect when the tool restarts.

### Anti-goals (a failure even if it technically works)

- **A false green.** The overall indicator shows ready while a required
  listener or edge is missing. Any doubt (unknown, stale or unreadable
  state) must show as not ready, never as ready.
- **Perturbing the stack.** The tool never connects to a service port
  to probe it, and never starts, stops or orders units on its own
  initiative.
- **Noise during a normal start.** Elements that are still coming up show
  as waiting, not as faults. Red has to mean something.
- **Another set of things to configure.** One YAML file drives the core,
  the PVs and the screen.

## 3. Changes to the outline's decisions

| Outline | Change |
| ------- | ------ |
| D8 (no subprocesses) | One exception: journal lines come from a long-running asyncio `journalctl` subprocess (Section 6.5). Everything else stays in-process. |
| D12 (strict PV compatibility) | Dropped. The old PVs are optional. The tool provides its own PVs and a new generated EDM screen, which replaces `ODNProcServ.edl`. Once deployed, the tool also replaces `qd-xspress-procserv-control.service`. procServ itself stays around the xspress IOC for its console. |
| Section 6 "Compatibility PVs" | As D12. |
| D7 (readiness checks) | Unchanged. Per-unit CPU is added as a health signal (Section 6.6). It can raise a fault but isn't a readiness condition. |
| Outline item 2 (journal) | Settled: the journal is tailed continuously and published per unit, like `procServControl`'s `IOCOUT`. |
| Outline item 3 (compatibility inventory) | Dropped with D12. |
| Outline item 6 (test stack gating) | Settled: gating is switchable per run (Section 10.3). The helpers are dev copies in this repo. |
| Outline Section 11 phase 5 | Moved after v1. |

## 4. Users and what they see

- **End user (beamline scientist or operator).** Looks at the EDM screen.
  Needs: one overall indicator (ready, starting or fault), and which row is
  red. Can press start, stop or restart on the stack or a unit. No other
  action is expected of them.
- **Engineer (often remote, later).** Reads the tool's log output in the
  journal (one line per event) and the screen's detail: per-unit journal
  tail, exit status, restart count, findings with `journalctl` references.

## 5. Model

The concepts are as in outline Section 4: service, dependency, listener,
edge, readiness, group, event. The details settled here:

### 5.1 Element states

Each service, listener and edge has one of these states:

| State     | Meaning                                                                                                      | Colour |
| --------- | ------------------------------------------------------------------------------------------------------------ | ------ |
| `unknown` | Not yet observed, or the observation failed or is stale.                                                     | grey   |
| `stopped` | A unit that is deliberately inactive (stopped by a job, not failed). A listener or edge is `stopped` only when the whole stack is stopped. | grey   |
| `waiting` | Expected but not yet there, and nothing depends on it yet.                                                    | amber  |
| `ready`   | Present (unit active and ready; listener bound/listening; edge established or, for UDP, best-effort present). | green  |
| `fault`   | Missing and it matters (Section 5.2), or the unit failed, or it is restart-looping.                           | red    |

**Stack ready** is true only when every configured unit and listener, and
every required `pre` edge, is `ready`. Any `unknown` element among them
makes it false (anti-goal 1). The overall state is one of these:

- `stopped`: every configured unit is inactive and none has failed.
- `starting`: not ready, nothing is `fault`, and at least one unit is active
  or activating.
- `stopping`: not ready, nothing is `fault`, and units are stopping (a stop
  or restart job is pending or a unit is deactivating) with none starting.
  The job's kind is its `JobType`, read from the job object (Section 6.1): a
  `Type=notify` unit is already active while its start job is still
  pending, and that is a start, never `stopping`.
- `ready`: stack ready is true.
- `fault`: anything else, meaning at least one element is `fault`.

**A deliberate stop of one unit while the rest is running is a fault**
(the user's decision). The stopped unit itself shows `stopped` (grey). The
listeners and edges it took down follow Section 5.2: they were ready and
dropped, so they are `fault`, and the overall state is `fault`. The stack
isn't ready to take data, whatever the reason. Only a stop of the whole
stack gives the grey overall `stopped`.

### 5.2 When missing becomes a fault

A missing unit, listener or `pre` edge is `waiting` until one of these
happens, then it is `fault`:

1. **A dependent is up.** A unit that depends on the element's owner (a
   systemd `After=`, `Requires=`, `Wants=` or `BindsTo=` relation, read
   over D-Bus) is `active`, or has started its main process, while the
   element is still missing. Example: the IOC is up, but FP2→FR2 isn't
   established.
2. **A timeout runs out.** The owning unit has been active for longer than
   the element's timeout (set in the YAML, with a global default).
3. **It was ready and then dropped.** Once an element has been `ready`,
   losing it is a fault immediately.

A unit whose `ActiveState` is `failed`, or whose `NRestarts` rises more than
the configured number of times within the configured window, is a fault.

A missing `post` element (Section 5.3) stays `waiting`. Rule 1 doesn't apply
to it. It becomes `fault` only by rule 3 (it was ready and dropped), or by
rule 2 if the YAML gives it a timeout. That timeout is counted from when
the top-level service became active.

### 5.3 Edges

An edge is `client -> server:port/proto`. The **server** is the unit that
owns the listening (TCP) or bound (UDP) socket. The **client** is the other
end. This is the transport view, whatever the ZMQ roles are: with a ZMQ
socket that binds on the "client" side, the YAML still names the TCP
listener's owner as the server. A server can be a remote IP (hardware).
Required edges count toward stack ready. Optional edges are shown but don't
count.

**Phase.** Each edge, and each listener, is `pre` (the default) or `post`:

- **`pre`**: must be up before the top level configures. It counts toward
  stack ready and is part of gate-violation checks.
- **`post`**: appears only after the top level has acted. It is shown with
  its own state, but it never counts toward stack ready and is never part of
  a gate-violation check. The YAML names the top-level service, which
  `post` timeouts are measured from.

Examples: on the QD stack, xspressControl connects to the detector cards
only on a `connect` command, and binds its DAQ ports (15150+n) only after
that. So the card edges, the DAQ listeners and the FR→xspress-control edges
are all `post`. In the test stack, relay→sink UDP starts only once the
controller has configured relay, so it is `post`.

- **TCP**: `ready` when at least one `ESTABLISHED` connection exists from
  a socket owned by the client unit to the server's port. For a remote
  server, the match is on the remote IP and port.
- **UDP, connected sender**: `ready` when the receiver is bound and the
  sender owns a UDP socket connected to the receiver's address and port.
- **UDP, unconnected sender**: `ready` (best effort, labelled "sender
  socket present") when the receiver is bound and the sender owns any UDP
  socket. The YAML says which kind an edge is.
- **UDP from a remote sender** (detector cards to an FR): only "receiver
  bound" can be seen, and the edge is shown that way.

### 5.4 Gate violations

A gate violation is when a dependent unit started its main process while a
dependency it is ordered after (`After=`) was not ready in the tool's
model, and that element *did* become ready afterwards: the dependent's
start preceded the element's first ready poll. Only the dependency's unit
state and its `pre` listeners, files and edges count. It is reported as a
finding naming both units and the late element.

- **Definite:** a socket poll at or after the dependent's start still saw
  the element missing, and it became ready later. For the unit itself the
  verdict is exact: it became active after the dependent started.
- **Possible:** the last poll before the start saw the element missing and
  the first poll after it saw it ready, so it appeared within one poll
  interval of the start. Sockets are polled fast while units start
  (Section 6.3), so this needs a start within one fast interval (100 ms by
  default) of the element appearing.
- **Vouched for by systemd:** no *possible* finding is raised when the
  dependency unit's exact `ActiveEnterTimestamp` precedes the dependent's
  start and the element is seen ready at the next poll. The dependency's
  gate (`READY=1`, an `ExecStartPost=` wait) already said it was ready; with
  `Type=notify` the dependent starts a few milliseconds after the bind, which
  no poll interval can resolve. A *definite* violation is still raised when
  a poll after the start sees the element missing, whatever the timestamp.
- **Never ready:** an element that never becomes ready (still missing, or
  `fault`) is not a gate violation. `listener_missing` and `edge_missing`
  report it (Section 5.2). A check therefore stays pending until the element
  becomes ready, the dependent stops, or the dependent's next invocation
  starts.

There is one finding per (dependent invocation, dependency); a definite one
replaces a possible one. Violations are events and findings. They don't by
themselves make an element `fault`, and they clear when the dependent's next
invocation starts.

### 5.5 Findings

Each finding is a reading of the model, following the rules in outline
Section 5 (unit not active, listener missing, edge missing with the
listener present, restart loop, gate violation, CPU spin). It names the
unit or units at fault and the likely side, and gives
`journalctl -u <unit> --since <time>`.

## 6. Observation

The builder picks the exact D-Bus calls and `/proc` parsing, within these
constraints. The `systemd-dbus` and `python-soft-ioc` project skills
describe the libraries.

### 6.1 Units (event-driven)

Use dbus-fast on the system bus: `Subscribe`, then `PropertiesChanged` on
each configured unit's object. Read `ActiveState`, `SubState`, `Result`,
`NRestarts`, `ExecMainStatus`/`ExecMainCode`, `MainPID`, `ControlGroup`,
the timestamps (`ActiveEnterTimestamp`, `ExecMainStartTimestamp` and so
on) and the dependency lists. systemd never signals `ControlGroup`, so a
unit's properties are read again whenever its `InvocationID` or `MainPID`
changes, to pick up the cgroup of a unit that started after the tool.
Neither `JobNew` nor a unit's `Job` property says what kind of job is
pending, so each new job's `JobType` is read from its job object
(`org.freedesktop.systemd1.Job`); a job already gone reads as unknown.
Reconnect to D-Bus and re-read all state after a D-Bus disconnect. Units in
`unknown` state meanwhile.

### 6.2 Dependencies

Read at startup from each unit's `After`, `Requires`, `Wants`, `BindsTo` and
`PartOf` (D2). Re-read on a systemd `Reloading` signal. Dependencies
between configured units are used as they are. Dependencies through
unconfigured units (targets and the like) are followed transitively to
reach configured units.

### 6.3 Sockets (polled every 1 s, every 100 ms while units start)

Read `/proc/net/{tcp,tcp6,udp,udp6}` and map socket inodes to PIDs through
`/proc/<pid>/fd`, and PIDs to units through the unit's `ControlGroup`
(`cgroup.procs`). This works because the tool runs as the stack's user
(D10). Sockets that can't be attributed are external. Use netlink
`sock_diag` only if `/proc` proves too slow or incomplete at QD scale.
That is the builder's decision, backed by measurement.

The poll runs every `poll_interval` (default 1 s). While any configured
unit is `activating` or has a job queued, and for `fast_poll_window`
seconds (default 5 s) after any unit changed state, it runs every
`fast_poll_interval` instead (default 100 ms; both set in the YAML
`defaults`). A unit change during a slow wait starts the fast polls at
once. The fast polls give each gate check (Section 5.4) a poll close before
every start, so a cleanly gated start raises no gate finding.

### 6.4 File checks

File exists, optionally non-empty (D7). Polled with the sockets.

### 6.5 Journal (continuous)

Run one long-lived asyncio subprocess,
`journalctl -f -o json -n <N> -u <unit> …`, for all configured units.
Template units are followed with globs (`-u 'scdemo-sink@*'`). Each output
line is one JSON entry, and it is assigned to a unit as follows:

- by `_SYSTEMD_UNIT`, which journald sets as a trusted field. It covers the
  service's stdout/stderr and its `ExecStartPre`/`ExecStartPost` helpers;
- for systemd's own messages about the unit (`_SYSTEMD_UNIT=init.scope`,
  for example "Main process exited"), by `UNIT` or `OBJECT_SYSTEMD_UNIT`.
  These are kept in the tail.

`MESSAGE` may be an array of bytes (when it isn't valid UTF-8) and must be
decoded. Raise the asyncio stream reader's line limit above the default 64
KiB. `-n` gives a total backlog, not one per unit, so each unit's ring is
filled at startup with a larger `-n` or a one-off read per unit (builder's
choice). Each unit's entries go into a ring of the last 20 lines, published like `procServControl`'s `IOCOUT` (a `UCHAR` waveform,
newest last), but capped at 999 bytes plus a NUL (1000 elements): the newest
whole lines that fit, a single longer line cut to its last 999 bytes on a
character boundary (plan 4 ruling 19). EDM's `multiLineTextUpdate` reads at
most 1000 bytes and prints junk after them unless a NUL comes first, so a
5120-byte buffer would hide the tail. Restart the subprocess if it exits, logging it
and backing off. Limit the rate of PV updates so a unit logging heavily
can't flood Channel Access. Reading the journal needs the tool's user to be
in `systemd-journal` (a packaging proposal, Section 9).

### 6.6 CPU

Each poll, read `cpu.stat` `usage_usec` for each unit's cgroup (its
`ControlGroup`, re-read when the unit starts, Section 6.1) and publish the per-unit CPU as a percentage of one core. A unit above the
threshold (default 90%) for longer than the window (default 10 s), both
set in the YAML, raises a "spinning" finding and makes the unit `fault`.
The fault clears when usage drops.

## 7. Control

Start, stop and restart of a configured unit, or of a configured group
(normally a target or a `PartOf=` umbrella unit such as
`qd-xspress-all.service`), each as a single systemd job (D3). The job's
progress and result are events. A failed job is a finding. A polkit rule,
scoped to the configured units and the tool's user, allows these jobs
(Section 9). The tool never starts or stops anything except on a control
PV write.

## 8. Front-end

### 8.1 IOC

One pythonSoftIOC process under systemd, non-interactive (no procServ
needed), asyncio throughout. The core has no EPICS dependency. The IOC
maps the model to PVs and control PVs to core calls (outline Section 5).
Every event is also logged through Python `logging` to stdout, one line per
event in the outline's Section 6 style.

### 8.2 PVs

The prefix comes from the YAML. Each element gets a short PV-safe name,
written in the YAML or derived from it, unique per kind. The layout is
indicative; the builder settles the names.

- Stack: `READY` (bi), `STATE` (mbbi: stopped/starting/stopping/ready/fault),
  `POST_READY` (bi: every required `post` element is ready, shown only and
  never part of `READY`),
  `FAULTS` (count), `FAULT_MSG` (latest finding, long string),
  `EVENTS` (recent events, char waveform, newest last, capped like `LOG`
  at 999 bytes plus a NUL, Section 6.5), `START`/`STOP`/`RESTART`
  (busy) for the stack's group.
- Per service `<svc>:`: `STATE` (mbbi, Section 5.1), `UNIT` (unit name),
  `ACTIVE` (systemd `ActiveState`), `READY`, `NRESTARTS`, `EXIT`
  (last exit status/signal), `CPU` (%), `LOG` (journal tail waveform, at most 999 bytes, Section 6.5),
  `FINDING`, `START`/`STOP`/`RESTART` (busy).
- Per listener and per edge: `STATE`, `DESC` (the human-readable
  `client -> server:port/proto`), `SINCE` (time of last change).
- Per group: `START`/`STOP`/`RESTART`, `STATE`.

Severity (`MAJOR` for fault, `MINOR` for waiting, `INVALID` for unknown) is
set on the state records, so EDM's alarm colours work without extra logic.

### 8.3 EDM screen

Generated from the same YAML by a subcommand of the tool, for example
`service-control-ioc generate-edm <config> <outdir>`. It isn't interactive,
so it keeps the spirit of D11. It generates:

- a top screen with the overall indicator, the stack controls, a fault
  message, and one row per service: state, active state, restarts, CPU,
  and controls. Each listener and edge row sits under its owning service,
  or in an edges section;
- a per-service detail screen (the journal tail, exit status, findings),
  opened from the row.

The journal tail and events panes (EDM `multiLineTextUpdate`) are tall
enough for a whole 20-line ring (plan 4, Task 5; the texts themselves are
capped at 999 bytes, Section 6.5). The scdemo test stack's screens fit a
1600x1200 display; the generator does not bound the size, so a stack with
more services or elements gets taller screens.

The builder chooses how the generator is driven (templates or code), and
the layout. It must run on macOS and on EL9, without EDM installed, and its
output is tested by checking that every PV it uses is served by the IOC
(as `qd-rpm-build`'s `test-rpms.sh` does for `ODNProcServ.edl`).

## 9. Configuration and deliverables

### 9.1 YAML

One file per stack. It contains:

- `prefix`, poll intervals (default 1 s, 100 ms while units start, 6.3),
  default timeouts, CPU threshold,
  restart-loop limits;
- `services`: unit name, PV name, listeners (`proto`, `port`, optional
  bind address, phase, timeout), file checks, optional/required;
- **instance groups**: a template unit (`scdemo-sink@.service`) with an
  instance range and listener/edge ports as expressions of the instance
  number. The tool expands them at startup;
- `top`: the top-level service (`post` timeouts count from when it becomes
  active);
- `edges`: client, server (a service or a remote `host`), port, proto,
  UDP sender kind, required/optional, phase (`pre`/`post`), timeout;
- **conditional sections**: a named flag (for example `hardware: false`)
  that switches a set of edges on or off. It is set in the YAML, fixed
  per install;
- `groups`: name, systemd unit (target or umbrella), member services.

Ports are **declared** in the YAML, as an independent statement of the
intended wiring. They aren't read from the services' own config files. A
service whose config has drifted then shows up as a missing edge or
listener. Dependencies are never declared, because they come from systemd
(D2).

Validation runs at startup, before the IOC starts. Errors give the
file, the YAML path and the reason, and the tool exits non-zero. It checks
for: unknown keys, duplicate names, edges referring to unknown services,
port expressions that don't evaluate, and configured units that systemd
doesn't know (an error) or that aren't loaded (a warning). The builder
picks the schema library and the expression syntax. Keep expressions to
integer arithmetic on the instance number, never `eval`.

### 9.2 Files this repo ships, ready to package

- The Python package, with `service-control-ioc` and its subcommands `run`
  (the IOC), `check-config`, `generate-edm` and `generate-polkit`.
- A systemd unit for the tool (runs as the stack user, `Type=notify` or
  `simple` at the builder's choice, restart on failure).
- The polkit rule, generated from the YAML by
  `generate-polkit CONFIG --user USER` (plan 3, Ruling 6), limited to the
  configured units and the tool's user.
- The test stack: services, unit files with the gating drop-ins, the dev
  wait helpers, its polkit rule and its YAML (the reference example).
- An example Xspress YAML is out of v1 (Section 11).

### 9.3 Proposals for `qd-rpm-build` (described, not made)

- Add the stack user (`xspress`) to `systemd-journal`.
- Package the tool, its unit, its polkit rule and the generated screen.
  Retire `qd-xspress-procserv-control.service` and `ODNProcServ.edl`.
- Gating changes learned from the test stack (Section 10.3).

## 10. Test stack and environments

### 10.1 Services

Python services we write, named `scdemo-*`, running as a dedicated user
(`scdemo`). The tool runs as that user too. They follow outline Section 7:
structured logging of every state transition, configurable delays, and
every fault injection listed there (bind fail or wrong port, crash after N
seconds, never connect or connect to the wrong port, stop sending, slow
ready, never `READY=1`, ignore the configuration push, spin at 100% CPU).
Behaviour, delays and faults are set through options or the environment,
so a drop-in can change them per scenario.

The base chain is sink ← relay ← controller (outline Section 7). It is
extended so that v1 covers these **connection types**:

- **ZMQ patterns**: REQ/REP, DEALER/ROUTER, PUSH/PULL and PUB/SUB,
  including one link where the ZMQ socket that binds is on the side that
  doesn't initiate the logical exchange, as Odin does.
- **UDP**: one connected sender and one unconnected (`sendto`) sender.
- **Plain TCP** client/server, and an **HTTP** server standing in for
  odin-control's API, with a client polling it.
- A **hardware simulator** (`scdemo-hwsim`) standing in for the detector
  cards. It is another process on the same host, with no extra host or
  container. It binds a second loopback address (for example `127.0.0.2`,
  since Linux routes all of `127.0.0.0/8` to `lo` without setup), and it
  listens and sends UDP on that address. It isn't a configured service. The
  YAML names it as a remote `host: 127.0.0.2`, so its edges are matched by
  IP and port exactly as real card IPs will be. Like the real cards, it is
  connected to only on the controller's command (a `post` edge).
- At least one `post` edge whose server-side listener is also bound only
  after configure, as xspressControl's DAQ ports are.
- **One template unit** with at least two instances and ports computed from
  the instance number, to exercise the YAML instance groups. There is no
  fan-in/fan-out topology.

The builder chooses how these map onto services: more services, or the
same service in different modes. The topology must stay small enough to
read on one screen.

### 10.2 Configuration

The test stack's YAML is the reference example of the format and covers
every feature in Section 9.1.

### 10.3 Gating

Each unit's gating style is **switchable per run** with drop-ins:
`Type=notify` with `READY=1`, `Type=simple` with an `ExecStartPost=` wait
helper, `Type=simple` with an `ExecStartPre=sleep`, and no gate at all (to
produce gate violations deliberately). The wait helpers are dev copies in
this repo, under `scdemo-*` names: a TCP-listening wait like
`qd-wait-for-port`, and a UDP-bound variant (the QD one is TCP only). The
results feed the `qd-rpm-build` gating proposals.

### 10.4 Scenarios

A scenario is a gating choice plus fault injections plus an expected
outcome: the sequence of events, the final element states, the findings,
and the stack-ready value. Scenarios are scripted, so they can be run
repeatedly in both environments. The builder picks the runner (pytest with
a systemd fixture, or a script). The set covers every fault injection,
every gating style, and a clean start, stop and restart through the control
PVs.

### 10.5 Environments

- **macOS unit tests**: config, expansion, model, state rules, findings,
  PV mapping, EDM generation, all against fake observers and a fake
  systemd (outline Section 8). `/proc` and cgroup parsing is tested against
  fixtures captured on EL9.
- **Local**: a Rocky 9 container with systemd as PID 1 under Rancher
  Desktop (aarch64), running the test stack, its gates, the tool and the
  scenarios.
- **Test VM** (x86_64): the same test stack and tool,
  installed with `scdemo-*` units and a dev PV prefix alongside the
  installed QD stack, which they must not disturb. There the units and the
  tool run as the existing `xspress` account. The `qd-test-vm` skill
  covers access.
- **Live EDM on the test VM**: EDM exists only as the x86_64 `qd-epics-edm`
  RPM, so the generated screen is viewed on the VM, against the running
  tool, during the scenarios. The builder chooses Xvfb with screenshots or X
  forwarding through the bastion. The VM is dedicated to this testing
  (user, 2026-10-20), so installing Xvfb and other tools there needs no
  separate approval.

### 10.6 v1 done

The test stack, with all the connection types above, runs under systemd
both locally and on the test VM, with this tool supervising it. Every
scenario gives its expected events, states, findings and stack-ready value,
checked automatically through the PVs. The generated EDM screen passes the
"every PV is served" check, and it has been **viewed live on the test VM**
for a clean start, a manual single-unit stop and at least one fault
scenario, with screenshots kept as evidence. Start, stop and restart work
through the PVs and from the screen.

## 11. After v1 (keep it easy)

- An Xspress YAML: FR/FP instance groups with the ports from
  `qd-check-connections` (`10000 + 10*n` and so on), odin-control,
  meta-writer, live-view-merge, xspress-control, and the IOC as top-level.
  Hardware edges sit behind the `hardware` flag. The card edges, the DAQ
  listeners (15150+n) and the FR→xspress-control edges are `post`
  (Section 5.3).
- Validation on the test VM, then the `qd-rpm-build` proposals.

## 12. Calls made in this design (please check)

These weren't discussed. They are my calls, and you can change any of them.

1. Five element states, with `unknown` and `stopped` grey, and overall
   states `stopped/starting/stopping/ready/fault` (5.1).
2. Any `unknown` element makes stack ready false (5.1).
3. Restart-loop and CPU-spin limits are configurable, with defaults
   (90%, 10 s) (5.2, 6.6).
4. Gate violations within one (fast, 100 ms) poll interval are reported
   as "possible", unless the dependency unit became active before the
   dependent started; elements that never become ready are not gate violations
   (5.4, rulings 13 and 14 of plan 4). Violations are findings, not faults.
5. One `journalctl -f` subprocess for all units, split by unit, 20 lines
   and 5120 bytes per unit like `IOCOUT`, published as the newest lines that
   fit in 999 bytes for EDM (plan 4 ruling 19), with rate-limited updates (6.5).
6. CPU from cgroup `cpu.stat`, not D-Bus `CPUUsageNSec` (6.6).
7. A `generate-edm` subcommand rather than generating the screen at IOC
   startup (8.3).
8. Indicative PV layout and naming (8.2).
9. Conditional edges as named flags in the YAML (9.1).
10. The tool's user for the test stack is `scdemo` (10.1).
11. Hardware is simulated by a local process on a second loopback address,
    named in the YAML as a remote host (10.1).
12. Scripted scenarios with expected outcomes as the acceptance test
    (10.4).
13. `post` elements: rule 1 doesn't apply, the optional timeout counts from
    when the top level becomes active, and a display-only `POST_READY` PV
    (5.2, 5.3, 8.2).
14. Listeners can be `post` as well as edges (5.3).
