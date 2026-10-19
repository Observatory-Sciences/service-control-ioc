#!/usr/bin/env bash
# Smoke-test the installed scdemo stack under every gating style.
# Runs inside the EL9 container (or any host where install.sh ran), as root.
# For each style: set gating drop-ins, restart scdemo-all.service, and wait up
# to DEADLINE seconds for all units active, all listeners/binds present and
# all edges established. Prints PASS <style> or FAIL <style>: <missing>.
# Exits 1 if any style failed. All gating drop-ins are cleared at the end.
# Overrides (debugging and self-tests): SMOKE_STYLES (subset of the four
# styles), SMOKE_DEADLINE (seconds, default 30, counted from before the
# restart), SMOKE_DROPIN (path to scdemo-dropin).
set -euo pipefail

DROPIN=${SMOKE_DROPIN:-/opt/scdemo/venv/bin/scdemo-dropin}
DEADLINE=${SMOKE_DEADLINE:-30}
STYLES=${SMOKE_STYLES:-"none sleep waitport notify"}

UNITS="scdemo-sink@1.service scdemo-sink@2.service scdemo-relay.service scdemo-controller.service scdemo-hwsim.service"
GATED="scdemo-sink@.service scdemo-relay.service scdemo-controller.service"
TCP_LISTEN="20021 20022 20100 20101 20102 20103 20104"
UDP_BIND="20011 20012 20105"
TCP_ESTAB="20021 20022 20100 20101 20102 20103 20104"
HW=127.0.0.2:20200

# EXIT trap: clear every unit even if one fails, always reload, and exit
# non-zero if cleanup failed (keeping the script's own failure status).
# shellcheck disable=SC2329  # invoked by the EXIT trap
clear_all() {
    local status=$? rc=0 unit
    for unit in $GATED; do
        "$DROPIN" --no-reload clear "$unit" || rc=1
    done
    systemctl daemon-reload || rc=1
    if [ "$rc" -ne 0 ]; then
        echo "smoke.sh: could not clear all gating drop-ins" >&2
        [ "$status" -ne 0 ] || status=1
    fi
    exit "$status"
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
    local style=$1 ctl_style=$1 start elapsed rc=0 miss
    [ "$style" = waitport ] && ctl_style=notify
    # errexit does not apply here (run_style is called in an || list), so
    # every setup step is checked explicitly.
    if ! { "$DROPIN" --no-reload gating scdemo-sink@.service "$style" &&
        "$DROPIN" --no-reload gating scdemo-relay.service "$style" &&
        "$DROPIN" --no-reload gating scdemo-controller.service "$ctl_style" &&
        systemctl daemon-reload; }; then
        echo "FAIL $style: could not set drop-ins"
        return 1
    fi
    systemctl reset-failed 'scdemo-*' 2>/dev/null || true

    # The restart blocks while gating runs, so the deadline is checked on
    # every pass, including the first one after the restart returns.
    start=$SECONDS
    systemctl restart scdemo-all.service || rc=$?
    while :; do
        elapsed=$((SECONDS - start))
        if (( elapsed > DEADLINE )); then
            miss=$(missing)
            [ "$rc" -eq 0 ] || miss="restart exit $rc; $miss"
            echo "FAIL $style: stack took ${elapsed}s, over the ${DEADLINE}s deadline${miss:+; missing: $miss}"
            return 1
        fi
        miss=$(missing)
        if [ -z "$miss" ] && [ "$rc" -eq 0 ]; then
            echo "# $style: all up after ${elapsed}s"
            echo "PASS $style"
            return 0
        fi
        sleep 0.5
    done
}

for style in $STYLES; do
    case "$style" in
        none | sleep | waitport | notify) ;;
        *) echo "smoke.sh: unknown style '$style' (allowed: none sleep waitport notify)" >&2; exit 2 ;;
    esac
done
[[ "$DEADLINE" =~ ^[0-9]+$ ]] || { echo "smoke.sh: SMOKE_DEADLINE must be whole seconds, got '$DEADLINE'" >&2; exit 2; }
DEADLINE=$((10#$DEADLINE))  # base 10: a leading zero (08, 09) is not octal
[ -x "$DROPIN" ] || { echo "smoke.sh: $DROPIN missing; run install.sh first" >&2; exit 1; }
trap clear_all EXIT

failed=0
for style in $STYLES; do
    run_style "$style" || failed=1
done
exit "$failed"
