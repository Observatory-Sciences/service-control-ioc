#!/usr/bin/env bash
# Install the scdemo test stack on an EL9 host with systemd. Run as root.
# Idempotent: re-running rebuilds the venv contents and rewrites the same
# files. Touches only scdemo-* units (and their 10-user.conf drop-ins),
# /opt/scdemo, /etc/scdemo, the service user's systemd-journal membership, the
# /usr/local/bin/scdemo-dropin symlink, the service-control-ioc venv in
# /opt/service-control-ioc/venv and the polkit rule
# /etc/polkit-1/rules.d/40-scdemo-sc.rules. The tool's own unit,
# scdemo-sc.service, is installed but neither enabled nor started.
# Usage: install.sh [--user NAME] [source-dir]
#   --user NAME  run the stack and the tool as NAME (default scdemo). Only
#                scdemo is created here (in the throwaway container); any other
#                NAME, e.g. xspress on the test VM, must already exist.
#   source-dir   default: the repo root above this script
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SVC_USER=scdemo
SRC=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --user)
            [ "$#" -ge 2 ] || { echo "install.sh: --user needs a name" >&2; exit 2; }
            SVC_USER=$2
            shift 2
            ;;
        -*)
            echo "install.sh: unknown option $1" >&2
            exit 2
            ;;
        *)
            SRC=$1
            shift
            ;;
    esac
done
SRC=${SRC:-$(cd "$HERE/.." && pwd)}
PREFIX=/opt/scdemo
TOOL_VENV=/opt/service-control-ioc/venv
CA_VENV=$PREFIX/ca-venv
RULE=/etc/polkit-1/rules.d/40-scdemo-sc.rules
CONF=/etc/scdemo
UNITDIR=/etc/systemd/system
PYTHON=/usr/bin/python3.11
# sudo's secure_path (EL9) omits /usr/local/bin, where teststack/vm/prepare.sh
# puts uv, so look there when uv is not on PATH.
if [ -z "${UV:-}" ]; then
    if command -v uv >/dev/null 2>&1; then
        UV=uv
    else
        UV=/usr/local/bin/uv
    fi
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh: must run as root" >&2
    exit 1
fi
for f in "$SRC/pyproject.toml" "$SRC/uv.lock" "$SRC/teststack/pyproject.toml" "$SRC/src/service_control_ioc/__init__.py" "$SRC/teststack/config/scdemo.yaml"; do
    [ -f "$f" ] || { echo "install.sh: $f not found (is $SRC the repo root?)" >&2; exit 1; }
done

# The service user. scdemo is created here, without a home directory or login
# shell. Any other user (xspress on the test VM) must already exist: this
# script never creates accounts there.
if [ "$SVC_USER" = scdemo ]; then
    if ! getent group scdemo >/dev/null; then
        groupadd --system scdemo
    fi
    if ! getent passwd scdemo >/dev/null; then
        useradd --system --gid scdemo --no-create-home --home-dir / \
            --shell /sbin/nologin --comment "scdemo test stack" scdemo
    fi
elif ! getent passwd "$SVC_USER" >/dev/null; then
    echo "install.sh: user $SVC_USER does not exist (only scdemo is created)" >&2
    exit 1
fi
SVC_GROUP=$(id -gn "$SVC_USER")
# service-control-ioc runs as this user in the tests and follows the journal.
usermod -aG systemd-journal "$SVC_USER"

# The source may be a read-only mount, so build from a private copy.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp "$SRC/pyproject.toml" "$SRC/uv.lock" "$tmp/"
mkdir "$tmp/teststack"
cp -r "$SRC/teststack/pyproject.toml" "$SRC/teststack/src" "$tmp/teststack/"
cp -r "$SRC/src" "$tmp/"
find "$tmp" -name __pycache__ -type d -prune -exec rm -rf {} +

install -d -m 0755 "$PREFIX" "$PREFIX/bin" "$CONF"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
    "$UV" venv --python "$PYTHON" "$PREFIX/venv"
fi
"$UV" pip install --python "$PREFIX/venv" --reinstall-package scdemo "$tmp/teststack"
chmod -R a+rX "$PREFIX/venv"

# service-control-ioc itself, from uv.lock: runtime dependencies only (no dev
# group), non-editable so the venv does not point into $tmp. On aarch64 uv
# builds softioc from source with the lock's extra-build-dependencies.
install -d -m 0755 "$(dirname "$TOOL_VENV")"
(cd "$tmp" && UV_PROJECT_ENVIRONMENT=$TOOL_VENV "$UV" sync --frozen --no-dev \
    --no-editable --package service-control-ioc --python "$PYTHON")
chmod -R a+rX "$TOOL_VENV"

# The test Channel Access client (tests/integration/ca_client.py) needs aioca,
# a dev-only dependency, so it gets its own venv, pinned by uv.lock, and the
# tool venv stays exactly the lock's runtime set.
if [ ! -x "$CA_VENV/bin/python" ]; then
    "$UV" venv --python "$PYTHON" "$CA_VENV"
fi
(cd "$tmp" && "$UV" export --frozen --all-groups --no-hashes --no-emit-workspace \
    --quiet -o "$tmp/constraints.txt")
"$UV" pip install --python "$CA_VENV" -c "$tmp/constraints.txt" aioca
chmod -R a+rX "$CA_VENV"

install -m 0755 "$SRC/teststack/bin/scdemo-wait-for-port" "$PREFIX/bin/scdemo-wait-for-port"
ln -sfn "$PREFIX/venv/bin/scdemo-dropin" /usr/local/bin/scdemo-dropin

for unit in "$SRC"/teststack/systemd/scdemo-*.service; do
    install -m 0644 "$unit" "$UNITDIR/$(basename "$unit")"
    # The unit files say User=scdemo. For another user, a drop-in overrides it
    # (on the sink template too, so it covers every instance); for scdemo the
    # drop-in is removed again.
    dropin=$UNITDIR/$(basename "$unit").d/10-user.conf
    if [ "$SVC_USER" = scdemo ]; then
        rm -f "$dropin"
        rmdir --ignore-fail-on-non-empty "$(dirname "$dropin")" 2>/dev/null || true
    else
        install -d -m 0755 "$(dirname "$dropin")"
        printf '[Service]\nUser=%s\nGroup=%s\n' "$SVC_USER" "$SVC_GROUP" > "$tmp/10-user.conf"
        install -m 0644 -o root -g root "$tmp/10-user.conf" "$dropin"
    fi
done

# Env files: port base plus the gate the waitport style waits on.
write_env() {
    local name=$1 proto=${2:-} port=${3:-}
    {
        echo "# Installed by scdemo install.sh; overwritten on reinstall."
        echo "SCDEMO_PORT_BASE=20000"
        if [ -n "$proto" ]; then
            echo "SCDEMO_GATE_PROTO=$proto"
            echo "SCDEMO_GATE_PORT=$port"
        fi
    } > "$tmp/$name.env"
    install -m 0644 -o root -g root "$tmp/$name.env" "$CONF/$name.env"
}
write_env sink1 udp 20011
write_env sink2 udp 20012
write_env relay tcp 20100
write_env hwsim tcp 20200
write_env controller

# The tool's config and its Channel Access environment: loopback only, on the
# default port 5064.
install -m 0644 -o root -g root "$SRC/teststack/config/scdemo.yaml" "$CONF/scdemo-sc.yaml"
printf '%s\n' "# Installed by scdemo install.sh; overwritten on reinstall." \
    EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
    EPICS_CAS_INTF_ADDR_LIST=127.0.0.1 > "$tmp/ca.env"
install -m 0644 -o root -g root "$tmp/ca.env" "$CONF/ca.env"

# The EDM screens, generated from the same YAML.
"$TOOL_VENV/bin/service-control-generate-edm" "$CONF/scdemo-sc.yaml" "$PREFIX/edm"
chmod -R a+rX "$PREFIX/edm"

# The polkit rule, generated from the same YAML, lets the service user start, stop and
# restart exactly the stack's units. polkitd reloads it when it changes.
"$TOOL_VENV/bin/service-control-generate-polkit" "$CONF/scdemo-sc.yaml" \
    --user "$SVC_USER" > "$tmp/40-scdemo-sc.rules"
install -m 0644 -o root -g root "$tmp/40-scdemo-sc.rules" "$RULE"

systemctl daemon-reload
systemctl enable --now scdemo-hwsim.service
# A reinstall must run the new code, so restart hwsim if it was already up.
systemctl restart scdemo-hwsim.service
echo "install.sh: scdemo installed in $PREFIX (config in $CONF), service-control-ioc in $TOOL_VENV, polkit rule $RULE"
