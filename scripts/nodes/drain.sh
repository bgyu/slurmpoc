#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=_lib.sh
source "$script_dir/_lib.sh"

usage() {
    cat <<'EOF'
Usage: scripts/nodes/drain.sh [--timeout SECONDS] [--reason TEXT] KEY_OR_NODE

Prevent new allocations and wait a bounded time for every queued job assigned
to this lifecycle-managed node to leave.  Reservations are never overridden.
EOF
}

drain_timeout=${NODE_DRAIN_TIMEOUT:-300}
reason="local lifecycle drain by ${USER:-operator}"
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

resolve_managed_target "$target" || die "no lifecycle-managed node matches $target"
[[ -n $RESOLVED_SLURM_NODE ]] ||
    die "$RESOLVED_KEY has not registered a Slurm node"
drain_node_locked "$RESOLVED_SLURM_NODE" "$reason" "$drain_timeout"
printf 'key=%s node=%s state=drained jobs=0\n' \
    "$RESOLVED_KEY" "$RESOLVED_SLURM_NODE"
