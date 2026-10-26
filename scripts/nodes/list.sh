#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=_lib.sh
source "$script_dir/_lib.sh"

if (($#)); then
    if [[ $1 == -h || $1 == --help ]]; then
        printf 'Usage: scripts/nodes/list.sh\n'
        exit 0
    fi
    die 'list.sh does not accept arguments'
fi

validate_settings
require_host_tools
prepare_state_root
take_lock shared

controller_ok=false
if controller_exec scontrol ping >/dev/null 2>&1; then
    controller_ok=true
fi

printf '%-32s %-48s %-9s %-20s %-24s %s\n' \
    KEY CONTAINER RUNNING SLURM_NODE STATE PROFILE

while IFS= read -r container; do
    [[ -n $container ]] || continue
    key=$(container_label "$container" "$KEY_LABEL")
    profile=$(container_label "$container" "$PROFILE_LABEL")
    hostname=$(podman_cmd inspect --format '{{.Config.Hostname}}' "$container")
    running=$(podman_cmd inspect --format '{{.State.Running}}' "$container")
    node=$(read_state_value "$key" node-name || true)
    state=UNAVAILABLE
    if [[ $controller_ok == true ]]; then
        record=
        if [[ -n $node ]]; then
            record=$(node_record_by_name "$node" 2>/dev/null || true)
        fi
        if [[ -z $record ]]; then
            record=$(node_record_by_hostname "$hostname" 2>/dev/null || true)
        fi
        if [[ -n $record ]]; then
            IFS='|' read -r node state _ <<< "$record"
        else
            node=-
            state=UNREGISTERED
        fi
    else
        [[ -n $node ]] || node=-
    fi
    printf '%-32s %-48s %-9s %-20s %-24s %s\n' \
        "$key" "$container" "$running" "$node" "$state" "$profile"
done < <(podman_cmd ps --all --filter "label=$MANAGED_LABEL=true" \
    --filter "label=$CLUSTER_LABEL=$CLUSTER_LABEL_VALUE" \
    --format '{{.Names}}' | sort)
