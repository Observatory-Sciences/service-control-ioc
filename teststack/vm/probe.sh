#!/usr/bin/env bash
# Read-only probe of the QD test VM. Prints "key: value" lines.
# Run as an ordinary user; uses `sudo -n` where root is needed.
set -u

kv() { printf '%s: %s\n' "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1 && command -v "$1" || echo missing; }
rpmq() { rpm -q "$1" 2>/dev/null | head -1 || true; }
oneline() { tr '\n' ' ' | sed 's/ *$//'; }

kv os "$(. /etc/os-release && echo "$PRETTY_NAME")"
kv arch "$(uname -m)"
kv kernel "$(uname -r)"
kv who "$(id -un) ($(id -Gn))"
kv python3.11 "$(have python3.11)"
kv uv "$(have uv) $(uv --version 2>/dev/null) | root: $(sudo -n sh -c 'command -v uv || ls /usr/local/bin/uv 2>/dev/null || echo missing' 2>&1)"
kv gcc "$(have gcc)"
kv epel "$(rpmq epel-release)"
if sudo -n true 2>/dev/null; then kv sudo yes; else kv sudo no; fi

kv xspress-id "$(id xspress 2>&1)"
kv xspress-passwd "$(getent passwd xspress)"
if sudo -n -u xspress true 2>/dev/null; then kv xspress-sudo-u yes; else kv xspress-sudo-u no; fi
if sudo -n su -s /bin/bash xspress -c true 2>/dev/null; then kv xspress-su yes; else kv xspress-su no; fi
kv xspress-groups "$(id -Gn xspress 2>&1) | journal-member: $(id -Gn xspress 2>/dev/null | grep -qw systemd-journal && echo yes || echo no)"

for t in Xvfb xdotool xwd xwininfo xwdtopnm pnmtopng convert; do kv "$t" "$(have $t)"; done
kv xvfb "$(rpmq xorg-x11-server-Xvfb)"
kv netpbm "$(rpmq netpbm-progs)"
kv imagemagick "$(rpmq ImageMagick)"
kv x-fonts "$(rpm -qa 'xorg-x11-fonts*' | sort | oneline)"

kv edm-env "$(ls /opt/QD/env/qd-epics-edm.sh 2>&1)"
kv edm "$(bash -c '. /opt/QD/env/qd-epics-edm.sh >/dev/null 2>&1; command -v edm || echo missing')"
kv edm-rpm "$(rpm -qa 'qd-epics-edm*' | oneline)"
kv edm-help "$(ls -d /opt/QD/epics/edm/helpFiles 2>&1)"
kv edm-colors "$(find /opt/QD -name colors.list 2>/dev/null | oneline)"

kv polkit "$(pkaction --version 2>&1)"
kv polkit-rules "$(ls -1 /etc/polkit-1/rules.d /usr/share/polkit-1/rules.d 2>&1 | oneline)"
kv journal-persistent "$(ls -d /var/log/journal 2>&1) | storage: $(grep -hE '^Storage' /etc/systemd/journald.conf /etc/systemd/journald.conf.d/* 2>/dev/null | oneline)"

kv qd-units "$(systemctl list-units 'qd-*' --all --no-legend --no-pager | awk '{print $1"="$3"/"$4}' | oneline)"
kv qd-unit-files "$(systemctl list-unit-files 'qd-*' --no-legend --no-pager | awk '{print $1"="$2}' | oneline)"
# Listeners owned by scdemo-* units (hwsim on 127.0.0.2:20200 after install.sh) are
# ours, not a clash: list them separately and exclude them from port-clash.
scdemo_pids=$(systemctl list-units 'scdemo-*' --all --no-legend --no-pager 2>/dev/null | awk '{print $1}' \
    | xargs -r -n1 systemctl show -p MainPID --value 2>/dev/null | grep -vx 0 | oneline || true)
ports_re=':(20[0-9]{3}|2010|2020|5064|5065)$'
listeners_all=$(sudo -n ss -ltnupH 2>/dev/null || ss -ltnuH 2>/dev/null || true)
owned() { # stdin: ss lines; keep (or with -v drop) those owned by an scdemo MainPID
    local pat=; for p in $scdemo_pids; do pat="${pat:+$pat|}pid=$p,"; done
    if [ -z "$pat" ]; then [ "${1:-}" = -v ] && cat || true; else grep ${1:-} -E "$pat" || true; fi
}
clash=$(printf '%s\n' "$listeners_all" | owned -v | awk '{print $5}' | grep -E "$ports_re" | sort -u | oneline || true)
kv port-clash "${clash:-none}"
kv scdemo-listeners "$(printf '%s\n' "$listeners_all" | owned | awk '{print $1"/"$5}' | grep -E "$ports_re" | sort -u | oneline || true)"
kv listeners-ca "$(ss -lntuH 2>/dev/null | awk '{print $1"/"$5}' | grep -E ':(5064|5065|6064|6065)$' | sort -u | oneline)"
kv existing-scdemo "units: $(systemctl list-units 'scdemo-*' 'qdsc-*' --all --no-legend --no-pager | awk '{print $1}' | oneline) | files: $(systemctl list-unit-files 'scdemo-*' --no-legend --no-pager | awk '{print $1"="$2}' | oneline) | dirs: $(ls -d /opt/scdemo /opt/qd-service-control /etc/scdemo 2>&1 | oneline)"
kv pypi "$(curl -sI -m 10 https://pypi.org 2>&1 | head -1 | tr -d '\r')"
kv pypi-files "$(curl -sI -m 10 https://files.pythonhosted.org 2>&1 | head -1 | tr -d '\r')"
kv repo "$(ls -d ~/qd-service-control 2>&1)"
kv disk "$(df -h / | tail -1 | awk '{print $4" free"}')"
