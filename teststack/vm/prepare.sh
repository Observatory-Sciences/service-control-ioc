#!/usr/bin/env bash
# Prepare the QD test VM (test-vm, Rocky 9) for the integration
# suite. Run as root. Idempotent. Installs only what docs/acceptance/evidence/
# vm-probe.txt found missing: xwd (EPEL), netpbm-progs, xorg-x11-fonts-misc, rsync
# (found missing on first live run: VmTarget.prepare() syncs the repo with it) and
# uv (pinned, for root at /usr/local/bin/uv and for the login user). It does not
# touch epel-release, the qd-* units or any account.
# Usage: prepare.sh [login-user]   (default: vmuser, the account that owns the repo)
set -euo pipefail

UV_VERSION=0.13.1
LOGIN_USER=${1:-vmuser}

if [ "$(id -u)" -ne 0 ]; then
    echo "prepare.sh: must run as root" >&2
    exit 1
fi
rpm -q epel-release >/dev/null || { echo "prepare.sh: epel-release is missing (expected from the probe)" >&2; exit 1; }
getent passwd "$LOGIN_USER" >/dev/null || { echo "prepare.sh: user $LOGIN_USER does not exist" >&2; exit 1; }

missing=()
for pkg in rsync xwd netpbm-progs xorg-x11-fonts-misc; do
    rpm -q "$pkg" >/dev/null || missing+=("$pkg")
done
if [ "${#missing[@]}" -gt 0 ]; then
    dnf install -y "${missing[@]}" </dev/null
fi

# uv, pinned. The installer script and the release tarball are both from Astral.
uv_version() { /usr/local/bin/uv --version 2>/dev/null | awk '{print $2}' || true; }
if [ "$(uv_version)" != "$UV_VERSION" ]; then
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" \
        | env UV_UNMANAGED_INSTALL=/usr/local/bin sh
fi
[ "$(uv_version)" = "$UV_VERSION" ] || { echo "prepare.sh: uv $UV_VERSION not installed" >&2; exit 1; }

login_home=$(getent passwd "$LOGIN_USER" | cut -d: -f6)
login_group=$(id -gn "$LOGIN_USER")
install -d -m 0755 -o "$LOGIN_USER" -g "$login_group" "$login_home/.local/bin"
install -m 0755 -o "$LOGIN_USER" -g "$login_group" /usr/local/bin/uv "$login_home/.local/bin/uv"

echo "prepare.sh: xwd $(rpm -q xwd), netpbm-progs, fonts-misc, uv $(uv_version) for root and $LOGIN_USER"
