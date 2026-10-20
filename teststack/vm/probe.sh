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
kv uv "$(have uv) $(uv --version 2>/dev/null) | root: $(sudo -n sh -c 'command -v uv || echo missing' 2>&1)"
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
kv port-clash "$( (ss -ltnuH 2>/dev/null | awk '{print $5}' | grep -E ':(20[0-9]{3}|2010|2020|5064|5065)$' | sort -u | oneline) || true)"
kv listeners-ca "$(ss -lntuH 2>/dev/null | awk '{print $1"/"$5}' | grep -E ':(5064|5065|6064|6065)$' | sort -u | oneline)"
kv existing-scdemo "units: $(systemctl list-units 'scdemo-*' 'qdsc-*' --all --no-legend --no-pager | awk '{print $1}' | oneline) | files: $(systemctl list-unit-files 'scdemo-*' --no-legend --no-pager | awk '{print $1"="$2}' | oneline) | dirs: $(ls -d /opt/scdemo /opt/qd-service-control /etc/scdemo 2>&1 | oneline)"
kv pypi "$(curl -sI -m 10 https://pypi.org 2>&1 | head -1 | tr -d '\r')"
kv pypi-files "$(curl -sI -m 10 https://files.pythonhosted.org 2>&1 | head -1 | tr -d '\r')"
kv repo "$(ls -d ~/qd-service-control 2>&1)"
kv disk "$(df -h / | tail -1 | awk '{print $4" free"}')"
