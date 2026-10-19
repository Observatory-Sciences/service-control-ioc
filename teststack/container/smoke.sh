#!/usr/bin/env bash
# Smoke-test the installed scdemo stack under every gating style.
# Runs inside the EL9 container (or any host where install.sh ran), as root.
# For each style: set gating drop-ins, restart scdemo-all.service, and wait up
# to DEADLINE seconds for all units active, all listeners/binds present and
# all edges established. Prints PASS <style> or FAIL <style>: <missing>.
# Exits 1 if any style failed. All gating drop-ins are cleared at the end.
set -euo pipefail

DROPIN=/opt/scdemo/venv/bin/scdemo-dropin
DEADLINE=${SMOKE_DEADLINE:-30}
STYLES=${SMOKE_STYLES:-"none sleep waitport notify"}

UNITS="scdemo-sink@1.service scdemo-sink@2.service scdemo-relay.service scdemo-controller.service scdemo-hwsim.service"
GATED="scdemo-sink@.service scdemo-relay.service scdemo-controller.service"
TCP_LISTEN="20021 20022 20100 20101 20102 20103 20104"
UDP_BIND="20011 20012 20105"
TCP_ESTAB="20021 20022 20100 20101 20102 20103 20104"
HW=127.0.0.2:20200

# shellcheck disable=SC2329  # invoked by the EXIT trap
clear_all() {
    local unit
    for unit in $GATED; do
        "$DROPIN" --no-reload clear "$unit"
    done
    systemctl daemon-reload
}

# Print what is missing (space-separated), or nothing when everything is up.
missing() {
    local miss="" unit port state
    for unit in $UNITS; do
        state=$(systemctl is-active "$unit" || true)
        [ "$state" = active ] || miss+=" $unit=$state"
    done
    for port in $TCP_LISTEN; do
        [ -n "$(ss -Hltn "sport = :$port")" ] || miss+=" listen:tcp/$port"
    done
    [ -n "$(ss -Hltn "src $HW")" ] || miss+=" listen:tcp/$HW"
    for port in $UDP_BIND; do
        [ -n "$(ss -Hlun "sport = :$port")" ] || miss+=" bind:udp/$port"
    done
    for port in $TCP_ESTAB; do
        [ -n "$(ss -Htn state established "dport = :$port")" ] || miss+=" estab:tcp/$port"
    done
    [ -n "$(ss -Htn state established "dst $HW")" ] || miss+=" estab:tcp/$HW"
    echo "${miss# }"
}

run_style() {
    local style=$1 ctl_style=$1 start rc=0 miss
    [ "$style" = waitport ] && ctl_style=notify
    "$DROPIN" --no-reload gating scdemo-sink@.service "$style"
    "$DROPIN" --no-reload gating scdemo-relay.service "$style"
    "$DROPIN" --no-reload gating scdemo-controller.service "$ctl_style"
    systemctl daemon-reload
    systemctl reset-failed 'scdemo-*' 2>/dev/null || true

    start=$SECONDS
    systemctl restart scdemo-all.service || rc=$?
    while :; do
        miss=$(missing)
        if [ -z "$miss" ] && [ "$rc" -eq 0 ]; then
            echo "# $style: all up after $((SECONDS - start))s"
            echo "PASS $style"
            return 0
        fi
        if (( SECONDS - start >= DEADLINE )); then
            [ "$rc" -eq 0 ] || miss="restart exit $rc; $miss"
            echo "FAIL $style: $miss (after ${DEADLINE}s)"
            return 1
        fi
        sleep 0.5
    done
}

[ -x "$DROPIN" ] || { echo "smoke.sh: $DROPIN missing; run install.sh first" >&2; exit 1; }
trap clear_all EXIT

failed=0
for style in $STYLES; do
    run_style "$style" || failed=1
done
exit "$failed"
