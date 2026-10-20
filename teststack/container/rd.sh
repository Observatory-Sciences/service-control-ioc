#!/usr/bin/env bash
# Drive the scdemo Rocky 9 systemd test container (Rancher Desktop docker).
# Usage: rd.sh build|up|install|smoke|shell [args...]|down
set -euo pipefail

DOCKER=${DOCKER:-$HOME/.rd/bin/docker}
IMAGE=scdemo-el9
NAME=scdemo-el9
READY_TIMEOUT=${READY_TIMEOUT:-120}

# Repo root from this script's own location (teststack/container/rd.sh).
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

is_running() {
    [ "$("$DOCKER" inspect --type container -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" = true ]
}

exists() {
    "$DOCKER" inspect --type container "$NAME" >/dev/null 2>&1
}

cmd_build() {
    "$DOCKER" build -t "$IMAGE" -f "$HERE/Dockerfile" "$HERE"
}

# True when the container was created from an older image than the current
# $IMAGE (the image was rebuilt since), so it must be recreated.
is_stale() {
    local have want
    have=$("$DOCKER" inspect --type container -f '{{.Image}}' "$NAME" 2>/dev/null || true)
    want=$("$DOCKER" image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)
    [ -n "$want" ] && [ "$have" != "$want" ]
}

# Idempotent: running on the current image -> nothing to start; running on an
# older image, or exists but stopped -> remove and recreate (a stopped systemd
# container has stale state and cannot be trusted); absent -> create. Always
# ends by waiting for systemd to finish booting.
cmd_up() {
    if is_running && is_stale; then
        echo "$NAME runs an older $IMAGE image; recreating"
        "$DOCKER" rm -f "$NAME" >/dev/null
    fi
    if is_running; then
        echo "$NAME already running"
    else
        if exists; then
            echo "removing stopped $NAME"
            "$DOCKER" rm -f "$NAME" >/dev/null
        fi
        "$DOCKER" run -d --name "$NAME" --privileged --cgroupns=private \
            --tmpfs /run --tmpfs /tmp -v "$REPO:/src:ro" "$IMAGE" >/dev/null
    fi
    wait_ready
}

wait_ready() {
    local state=""
    local start=$SECONDS
    echo "waiting for systemd (timeout ${READY_TIMEOUT}s)..."
    while :; do
        if (( SECONDS - start >= READY_TIMEOUT )); then
            echo "ERROR: $NAME not ready after ${READY_TIMEOUT}s (last state: ${state:-none})" >&2
            "$DOCKER" logs --tail 50 "$NAME" >&2 || true
            return 1
        fi
        if ! is_running; then
            echo "ERROR: $NAME exited during boot" >&2
            "$DOCKER" logs --tail 50 "$NAME" >&2 || true
            return 1
        fi
        # --wait blocks until boot finishes; each attempt is capped at 10 s by timeout(1),
        # and the loop re-checks the overall deadline between attempts.
        state=$("$DOCKER" exec "$NAME" timeout 10 systemctl is-system-running --wait 2>/dev/null || true)
        case "$state" in
            running)
                echo "systemd: running"
                return 0
                ;;
            degraded)
                echo "systemd: degraded; failed units:"
                "$DOCKER" exec "$NAME" systemctl --failed --no-pager || true
                return 0
                ;;
        esac
        sleep 1
    done
}

# Exec a script from the read-only repo mount inside the container.
exec_script() {
    local script=$1
    shift
    is_running || { echo "ERROR: $NAME is not running; run '$0 up' first" >&2; exit 1; }
    if ! "$DOCKER" exec "$NAME" test -f "$script"; then
        echo "ERROR: $script not found in $NAME (not created yet?)" >&2
        exit 1
    fi
    "$DOCKER" exec "$NAME" bash "$script" "$@"
}

cmd_shell() {
    is_running || { echo "ERROR: $NAME is not running; run '$0 up' first" >&2; exit 1; }
    # Plain string flag, not an array: empty arrays break under set -u on bash 3.2.
    # -i keeps piped stdin flowing; -t only when both ends are a terminal.
    local ti=-i
    if [ -t 0 ] && [ -t 1 ]; then ti=-it; fi
    if [ "$#" -eq 0 ]; then
        "$DOCKER" exec $ti "$NAME" bash -l
    else
        "$DOCKER" exec $ti "$NAME" bash "$@"
    fi
}

cmd_down() {
    "$DOCKER" rm -f "$NAME" >/dev/null 2>&1 || true
}

sub=${1:-}
[ "$#" -gt 0 ] && shift
case "$sub" in
    build) cmd_build ;;
    up) cmd_up ;;
    install) exec_script /src/teststack/install.sh "$@" ;;
    smoke) exec_script /src/teststack/container/smoke.sh "$@" ;;
    shell) cmd_shell "$@" ;;
    down) cmd_down ;;
    *)
        echo "usage: $0 build|up|install|smoke|shell [args...]|down" >&2
        exit 2
        ;;
esac
