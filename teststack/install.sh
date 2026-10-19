#!/usr/bin/env bash
# Install the scdemo test stack on an EL9 host with systemd. Run as root.
# Idempotent: re-running rebuilds the venv contents and rewrites the same
# files. Touches only scdemo-* units, /opt/scdemo, /etc/scdemo, the scdemo
# user (and its systemd-journal membership), the /usr/local/bin/scdemo-dropin
# symlink and the qd-service-control venv in /opt/qd-service-control/venv.
# Usage: install.sh [source-dir]   (default: the repo root above this script)
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=${1:-$(cd "$HERE/.." && pwd)}
PREFIX=/opt/scdemo
TOOL_VENV=/opt/qd-service-control/venv
CONF=/etc/scdemo
UNITDIR=/etc/systemd/system
PYTHON=/usr/bin/python3.11
UV=${UV:-uv}

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh: must run as root" >&2
    exit 1
fi
for f in "$SRC/pyproject.toml" "$SRC/uv.lock" "$SRC/teststack/pyproject.toml" "$SRC/src/qd_service_control/__init__.py"; do
    [ -f "$f" ] || { echo "install.sh: $f not found (is $SRC the repo root?)" >&2; exit 1; }
done

# System user and group, no home directory and no login shell.
if ! getent group scdemo >/dev/null; then
    groupadd --system scdemo
fi
if ! getent passwd scdemo >/dev/null; then
    useradd --system --gid scdemo --no-create-home --home-dir / \
        --shell /sbin/nologin --comment "scdemo test stack" scdemo
fi
# qd-service-control runs as scdemo in the tests and follows the journal.
usermod -aG systemd-journal scdemo

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

# qd-service-control itself (runtime dependencies only, no dev group).
install -d -m 0755 "$(dirname "$TOOL_VENV")"
if [ ! -x "$TOOL_VENV/bin/python" ]; then
    "$UV" venv --python "$PYTHON" "$TOOL_VENV"
fi
"$UV" pip install --python "$TOOL_VENV" --reinstall-package qd-service-control "$tmp"
chmod -R a+rX "$TOOL_VENV"

install -m 0755 "$SRC/teststack/bin/scdemo-wait-for-port" "$PREFIX/bin/scdemo-wait-for-port"
ln -sfn "$PREFIX/venv/bin/scdemo-dropin" /usr/local/bin/scdemo-dropin

for unit in "$SRC"/teststack/systemd/scdemo-*.service; do
    install -m 0644 "$unit" "$UNITDIR/$(basename "$unit")"
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

systemctl daemon-reload
systemctl enable --now scdemo-hwsim.service
# A reinstall must run the new code, so restart hwsim if it was already up.
systemctl restart scdemo-hwsim.service
echo "install.sh: scdemo installed in $PREFIX (config in $CONF), qd-service-control in $TOOL_VENV"
