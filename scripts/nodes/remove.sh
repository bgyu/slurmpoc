#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=_lib.sh
source "$script_dir/_lib.sh"

usage() {
    cat <<'EOF'
Usage: scripts/nodes/remove.sh [--timeout SECONDS] [--reason TEXT] KEY_OR_NODE

Drain a lifecycle-managed node, refuse reservations or remaining jobs, stop its
container gracefully, delete its dynamic Slurm record, and delete only its
private spool/metadata. Repeating a completed removal is a no-op.
EOF
}

drain_timeout=${NODE_DRAIN_TIMEOUT:-300}
reason="local lifecycle removal by ${USER:-operator}"
target=
while (($#)); do
    case $1 in
        --timeout)
            (($# >= 2)) || die '--timeout requires a value'
            drain_timeout=$2
            shift 2
            ;;
        --reason)
            (($# >= 2)) || die '--reason requires a value'
            reason=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --*) die "unknown option: $1" ;;
        *)
            [[ -z $target ]] || die 'only one target may be supplied'
            target=$1
            shift
            ;;
    esac
done
[[ -n $target ]] || { usage >&2; exit 64; }

validate_settings
validate_uint_range "$drain_timeout" 1 "$NODE_MAX_DRAIN_TIMEOUT" drain-timeout
require_host_tools
prepare_state_root
take_lock exclusive
require_controller

if ! resolve_managed_target "$target"; then
    if [[ $target =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
        printf 'key=%s state=absent\n' "$target"
        exit 0
    fi
    die "no lifecycle-managed node matches $target"
fi

if [[ -n $RESOLVED_SLURM_NODE ]]; then
    drain_node_locked "$RESOLVED_SLURM_NODE" "$reason" "$drain_timeout"
else
    # Never discard a running container merely because controller registration
    # lookup failed.  That could kill work while the control plane is stale.
    if container_exists "$RESOLVED_CONTAINER" && container_running "$RESOLVED_CONTAINER"; then
        die "$RESOLVED_KEY has no controller mapping; refusing to stop its running container"
    fi
fi

if container_exists "$RESOLVED_CONTAINER"; then
    if container_running "$RESOLVED_CONTAINER"; then
        # A DRAIN state prevents new allocations while this graceful stop runs.
        podman_cmd stop --time "$NODE_STOP_TIMEOUT" "$RESOLVED_CONTAINER" >/dev/null
    fi
fi

if [[ -n $RESOLVED_SLURM_NODE ]]; then
    # Re-check both invariants after the daemon stopped.  The host flock cannot
    # prevent a Slurm administrator from creating a reservation concurrently.
    jobs=$(active_jobs_for_node "$RESOLVED_SLURM_NODE")
    [[ -z $jobs ]] || {
        log "refusing to release $RESOLVED_SLURM_NODE; jobs appeared after stop:"
        printf '%s\n' "$jobs" >&2
        exit 1
    }
    refuse_if_reserved "$RESOLVED_SLURM_NODE" || exit 1

    # Lifecycle-managed local nodes use dynamic-normal registration. Once the
    # daemon is stopped, retaining its record would consume MaxNodeCount
    # capacity; production FUTURE/CLOUD identities use separate lifecycle
    # automation and are never handled by this script.
    controller_exec scontrol delete "NodeName=$RESOLVED_SLURM_NODE"

    # Deletion is normally immediate. Poll so success means the controller no
    # longer advertises the node before its ownership metadata is erased.
    deleted=false
    for _ in {1..15}; do
        if ! node_record_by_name "$RESOLVED_SLURM_NODE" >/dev/null 2>&1; then
            deleted=true
            break
        fi
        sleep 2
    done
    [[ $deleted == true ]] ||
        die "$RESOLVED_SLURM_NODE was not deleted; retained container metadata"
fi

if container_exists "$RESOLVED_CONTAINER"; then
    podman_cmd rm "$RESOLVED_CONTAINER" >/dev/null
fi
safe_remove_node_dir "$RESOLVED_KEY"
printf 'key=%s node=%s state=absent\n' \
    "$RESOLVED_KEY" "${RESOLVED_SLURM_NODE:--}"
