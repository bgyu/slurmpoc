#!/usr/bin/env bash
# Shared implementation for the local dynamic-node commands.
#
# This file is intentionally not an executable command. Public commands take
# a bounded flock before calling these helpers, so two terminals cannot race a
# dynamic registration against a stop or metadata update.

readonly NODES_SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly PROJECT_DIR=$(cd -- "$NODES_SCRIPT_DIR/../.." && pwd)
readonly RUNTIME_DIR=${SLURM_RUNTIME_DIR:-"$PROJECT_DIR/.runtime"}
readonly NODES_STATE_DIR=${SLURM_NODES_STATE_DIR:-"$RUNTIME_DIR/nodes"}
readonly NODE_LOCK_FILE="$NODES_STATE_DIR/lifecycle.lock"
readonly COMPOSE_FILE="$PROJECT_DIR/compose.yaml"
readonly COMPOSE_ENV_FILE="$RUNTIME_DIR/cluster.env"
readonly MANAGED_LABEL=io.slurmpoc.dynamic-node
readonly KEY_LABEL=io.slurmpoc.node-key
readonly PROFILE_LABEL=io.slurmpoc.node-profile
readonly CLUSTER_LABEL=io.slurmpoc.cluster
readonly CLUSTER_LABEL_VALUE=slurm-sim

NODE_COMMAND_TIMEOUT=${NODE_COMMAND_TIMEOUT:-20}
NODE_LOCK_TIMEOUT=${NODE_LOCK_TIMEOUT:-20}
NODE_MAX_DRAIN_TIMEOUT=${NODE_MAX_DRAIN_TIMEOUT:-3600}
NODE_STOP_TIMEOUT=${NODE_STOP_TIMEOUT:-30}

log() {
    printf '[nodes] %s\n' "$*" >&2
}

die() {
    log "ERROR: $*"
    exit 1
}

validate_uint_range() {
    local value=$1 minimum=$2 maximum=$3 description=$4
    [[ $value =~ ^[0-9]+$ ]] || die "$description must be an integer"
    ((value >= minimum && value <= maximum)) ||
        die "$description must be between $minimum and $maximum"
}

validate_settings() {
    validate_uint_range "$NODE_COMMAND_TIMEOUT" 1 300 NODE_COMMAND_TIMEOUT
    validate_uint_range "$NODE_LOCK_TIMEOUT" 0 300 NODE_LOCK_TIMEOUT
    validate_uint_range "$NODE_MAX_DRAIN_TIMEOUT" 1 86400 NODE_MAX_DRAIN_TIMEOUT
    validate_uint_range "$NODE_STOP_TIMEOUT" 1 120 NODE_STOP_TIMEOUT
}

validate_key() {
    local key=$1
    # The key becomes a directory, a container name, a label, and a hostname.
    # Keeping one deliberately small alphabet avoids traversal, option
    # injection, and hostname ambiguity at every one of those boundaries.
    [[ $key =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] ||
        die 'node key must match [a-z0-9][a-z0-9-]{0,31}'
}

validate_profile() {
    local profile=$1
    # -F accepts a single Slurm feature.  Do not accept a feature expression or
    # whitespace because it would make pool selection broader than requested.
    [[ $profile =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] ||
        die 'profile must be one Slurm feature (letters, digits, dot, dash, underscore)'
}

validate_slurm_node_name() {
    [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]
}

require_host_tools() {
    local tool
    for tool in podman flock timeout awk sed realpath grep find stat; do
        command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
    done
    [[ -f $COMPOSE_FILE ]] || die "missing $COMPOSE_FILE"
    [[ -r $COMPOSE_ENV_FILE ]] ||
        die "missing $COMPOSE_ENV_FILE; start the base cluster first"
    [[ -s $RUNTIME_DIR/secrets/munge.key && ! -L $RUNTIME_DIR/secrets/munge.key ]] ||
        die "missing $RUNTIME_DIR/secrets/munge.key; start the base cluster first"
    local secret_mode
    secret_mode=$(stat --format='%a' "$RUNTIME_DIR/secrets/munge.key") ||
        die 'cannot stat the local MUNGE key'
    (((8#$secret_mode & 077) == 0)) ||
        die 'the local MUNGE key must not be accessible by group or other'
}

prepare_state_root() {
    umask 077
    if [[ -L $NODES_STATE_DIR ]]; then
        die "$NODES_STATE_DIR must not be a symbolic link"
    fi
    install -d -m 0700 "$NODES_STATE_DIR"
}

take_lock() {
    local mode=$1
    exec 9>"$NODE_LOCK_FILE"
    if [[ $mode == shared ]]; then
        flock --shared --wait "$NODE_LOCK_TIMEOUT" 9 ||
            die 'timed out waiting for the node lifecycle read lock'
    else
        flock --exclusive --wait "$NODE_LOCK_TIMEOUT" 9 ||
            die 'timed out waiting for the node lifecycle write lock'
    fi
}

podman_cmd() {
    (
        # Bash descriptors are inherited across exec by default.  Never pass
        # the lifecycle lock to Podman: `podman run --detach` hands inherited
        # descriptors to conmon, which otherwise keeps the flock forever after
        # this script exits and blocks every later scale operation.
        exec 9>&-
        timeout --foreground "${NODE_COMMAND_TIMEOUT}s" podman "$@"
    )
}

compose_cmd() {
    (
        # The Compose provider ultimately launches Podman/conmon too, so apply
        # the same close-on-exec boundary as podman_cmd.
        exec 9>&-
        cd -- "$PROJECT_DIR"
        timeout --foreground "${NODE_COMMAND_TIMEOUT}s" \
            podman compose --env-file "$COMPOSE_ENV_FILE" \
                --file "$COMPOSE_FILE" "$@"
    )
}

controller_exec() {
    compose_cmd exec -T controller "$@"
}

compose_service_id() {
    local wanted=$1 id service matched=

    # Docker Compose accepts `ps --quiet SERVICE`, but the podman-compose
    # version shipped by Fedora 44 does not.  Query all project containers and
    # select by the interoperable Compose service label instead.  Requiring a
    # single match prevents an ambiguous or stale container ID from becoming
    # the source of the image, network, or shared-volume configuration used by
    # a dynamic node.
    while IFS= read -r id; do
        [[ -n $id ]] || continue
        service=$(podman_cmd inspect --format \
            '{{ index .Config.Labels "com.docker.compose.service" }}' "$id")
        [[ $service == "$wanted" ]] || continue
        [[ -z $matched ]] || die "multiple Compose containers match service $wanted"
        matched=$id
    done < <(compose_cmd ps --quiet)
    [[ -n $matched ]] || return 1
    printf '%s\n' "$matched"
}

require_controller() {
    local controller_id
    controller_id=$(compose_service_id controller) || true
    [[ -n $controller_id ]] || die 'the Compose controller is not running'
    controller_exec scontrol ping >/dev/null ||
        die 'slurmctld is not responding in the controller container'
}

container_name_for_key() {
    printf 'slurm-sim-dyn-%s\n' "$1"
}

hostname_for_key() {
    # Prefixing distinguishes lifecycle-managed hosts from the five Compose
    # simulation nodes and from production FUTURE/CLOUD identities.
    printf 'dyn-%s\n' "$1"
}

node_dir_for_key() {
    printf '%s/%s\n' "$NODES_STATE_DIR" "$1"
}

read_state_value() {
    local key=$1 name=$2 path value
    path="$(node_dir_for_key "$key")/$name"
    [[ -f $path && ! -L $path ]] || return 1
    IFS= read -r value < "$path" || true
    printf '%s\n' "$value"
}

write_state_value() {
    local key=$1 name=$2 value=$3 directory temporary
    directory=$(node_dir_for_key "$key")
    [[ ! -L $directory ]] || die "$directory must not be a symbolic link"
    install -d -m 0700 "$directory"
    temporary="$directory/.${name}.$$"
    printf '%s\n' "$value" > "$temporary"
    chmod 0600 "$temporary"
    mv -f -- "$temporary" "$directory/$name"
}

container_exists() {
    podman_cmd container exists "$1"
}

container_running() {
    [[ $(podman_cmd inspect --format '{{.State.Running}}' "$1") == true ]]
}

container_label() {
    local container=$1 label=$2
    podman_cmd inspect --format "{{ index .Config.Labels \"$label\" }}" "$container"
}

assert_managed_container() {
    local container=$1 expected_key=$2
    [[ $(container_label "$container" "$MANAGED_LABEL") == true ]] ||
        die "container $container exists but is not lifecycle-managed"
    [[ $(container_label "$container" "$CLUSTER_LABEL") == "$CLUSTER_LABEL_VALUE" ]] ||
        die "container $container belongs to a different cluster"
    [[ $(container_label "$container" "$KEY_LABEL") == "$expected_key" ]] ||
        die "container $container has an unexpected node key label"
}

all_node_records() {
    # FUTURE nodes are hidden from an ordinary scontrol listing.  --future
    # includes those spare identities as well as active nodes.
    controller_exec scontrol --future show nodes --oneliner
}

record_field() {
    local record=$1 wanted=$2 token
    for token in $record; do
        if [[ $token == "$wanted="* ]]; then
            printf '%s\n' "${token#*=}"
            return 0
        fi
    done
    return 1
}

node_record_by_hostname() {
    local hostname=$1 records
    records=$(all_node_records)
    awk -v wanted="$hostname" '
        {
            name = ""; host = ""; state = ""
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^NodeName=/) { name = substr($i, 10) }
                if ($i ~ /^NodeHostName=/) { host = substr($i, 14) }
                if ($i ~ /^State=/) { state = substr($i, 7) }
            }
            if (host == wanted) {
                matches++
                result = name "|" state
            }
        }
        END {
            if (matches == 1) { print result; exit 0 }
            if (matches > 1) { exit 2 }
            exit 1
        }
    ' <<< "$records"
}

node_record_by_name() {
    local node=$1 records
    records=$(all_node_records)
    awk -v wanted="$node" '
        {
            name = ""; state = ""; host = ""
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^NodeName=/) { name = substr($i, 10) }
                if ($i ~ /^NodeHostName=/) { host = substr($i, 14) }
                if ($i ~ /^State=/) { state = substr($i, 7) }
            }
            if (name == wanted) {
                print name "|" state "|" host
                exit 0
            }
        }
        END { if (name != wanted) exit 1 }
    ' <<< "$records"
}

wait_for_registration() {
    local key=$1 hostname=$2 wait_seconds=$3 deadline record node state delay=1
    deadline=$(( $(date +%s) + wait_seconds ))
    while (( $(date +%s) <= deadline )); do
        if record=$(node_record_by_hostname "$hostname" 2>/dev/null); then
            IFS='|' read -r node state <<< "$record"
            if [[ $state == *INVALID_REG* ]]; then
                die "node $node registered with INVALID_REG; verify its declared dynamic resource profile"
            fi
            if [[ $state != FUTURE* && $state != UNKNOWN* ]]; then
                validate_slurm_node_name "$node" ||
                    die "controller returned unsafe node name: $node"
                write_state_value "$key" node-name "$node"
                printf '%s|%s\n' "$node" "$state"
                return 0
            fi
        fi
        sleep "$delay"
        ((delay < 8)) && delay=$((delay * 2))
    done
    return 1
}

active_jobs_for_node() {
    local node=$1
    # Querying all queued states for this node is intentionally conservative.
    # It catches RUNNING, SUSPENDED, CONFIGURING and COMPLETING jobs without a
    # version-specific state allow-list that could omit a newly added state.
    controller_exec squeue --noheader --nodes="$node" \
        --format='%A|%T|%N'
}

reservations_for_node() {
    local node=$1 reservations line reservation_name node_list expanded
    reservations=$(controller_exec scontrol show reservation --oneliner)
    while IFS= read -r line; do
        [[ -n $line ]] || continue
        reservation_name=$(record_field "$line" ReservationName || true)
        node_list=$(record_field "$line" Nodes || true)
        [[ -n $reservation_name && -n $node_list && $node_list != '(null)' ]] ||
            continue
        # An ALL reservation is conservatively treated as referring to every
        # node; guessing otherwise could invalidate a future reservation.
        if [[ $node_list == ALL ]]; then
            printf '%s|%s\n' "$reservation_name" "$node_list"
            continue
        fi
        expanded=$(controller_exec scontrol show hostnames "$node_list") ||
            die "could not expand reservation hostlist $node_list"
        if grep -Fxq -- "$node" <<< "$expanded"; then
            printf '%s|%s\n' "$reservation_name" "$node_list"
        fi
    done <<< "$reservations"
}

refuse_if_reserved() {
    local node=$1 matches
    matches=$(reservations_for_node "$node")
    if [[ -n $matches ]]; then
        log "refusing lifecycle change: $node is referenced by reservation(s):"
        printf '%s\n' "$matches" >&2
        return 1
    fi
}

wait_for_no_active_jobs() {
    local node=$1 wait_seconds=$2 deadline jobs delay=1 now remaining
    deadline=$(( $(date +%s) + wait_seconds ))
    while true; do
        jobs=$(active_jobs_for_node "$node")
        if [[ -z $jobs ]]; then
            return 0
        fi
        now=$(date +%s)
        if ((now >= deadline)); then
            log "refusing to continue: active jobs remain on $node after ${wait_seconds}s:"
            printf '%s\n' "$jobs" >&2
            return 1
        fi
        remaining=$((deadline - now))
        ((delay > remaining)) && delay=$remaining
        log "waiting ${delay}s for jobs on $node to leave"
        sleep "$delay"
        ((delay < 10)) && delay=$((delay * 2))
        ((delay > 10)) && delay=10
    done
}

drain_node_locked() {
    local node=$1 reason=$2 wait_seconds=$3 record state
    validate_uint_range "$wait_seconds" 1 "$NODE_MAX_DRAIN_TIMEOUT" drain-timeout
    [[ $reason != *$'\n'* && $reason != *$'\r'* && ${#reason} -le 128 ]] ||
        die 'drain reason must be one line of at most 128 characters'

    record=$(node_record_by_name "$node") || die "Slurm node $node does not exist"
    IFS='|' read -r _ state _ <<< "$record"

    # A reservation is checked before changing state so a mistaken request has
    # no scheduler side effect, then checked again after the wait to close the
    # race with an administrator creating a reservation while jobs drain.
    refuse_if_reserved "$node" || return 1
    if [[ $state != FUTURE* && $state != *DRAIN* ]]; then
        controller_exec scontrol update "NodeName=$node" State=DRAIN \
            "Reason=$reason"
        log "marked $node DRAIN"
    fi
    if [[ $state != FUTURE* ]]; then
        wait_for_no_active_jobs "$node" "$wait_seconds" || return 1
    fi
    refuse_if_reserved "$node" || return 1
}

resolve_managed_target() {
    local target=$1 candidate key directory stored_node hostname record
    RESOLVED_KEY=
    RESOLVED_CONTAINER=
    RESOLVED_HOSTNAME=
    RESOLVED_SLURM_NODE=

    # First treat the argument as the stable logical key.  This path also
    # supports idempotent cleanup after the container has already disappeared.
    if [[ $target =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
        candidate=$(container_name_for_key "$target")
        directory=$(node_dir_for_key "$target")
        if container_exists "$candidate" || [[ -d $directory && ! -L $directory ]]; then
            RESOLVED_KEY=$target
            RESOLVED_CONTAINER=$candidate
        fi
    fi

    # Operators commonly know the assigned Slurm NodeName, so resolve that
    # through trusted metadata or live managed-container labels as a fallback.
    if [[ -z $RESOLVED_KEY ]]; then
        shopt -s nullglob
        for directory in "$NODES_STATE_DIR"/*; do
            [[ -d $directory && ! -L $directory ]] || continue
            key=${directory##*/}
            [[ $key =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || continue
            stored_node=$(read_state_value "$key" node-name || true)
            if [[ $stored_node == "$target" ]]; then
                RESOLVED_KEY=$key
                RESOLVED_CONTAINER=$(container_name_for_key "$key")
                break
            fi
        done
        shopt -u nullglob
    fi

    if [[ -z $RESOLVED_KEY ]]; then
        while IFS= read -r candidate; do
            [[ -n $candidate ]] || continue
            key=$(container_label "$candidate" "$KEY_LABEL")
            [[ $key =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || continue
            hostname=$(podman_cmd inspect --format '{{.Config.Hostname}}' "$candidate")
            if record=$(node_record_by_hostname "$hostname" 2>/dev/null); then
                stored_node=${record%%|*}
                if [[ $stored_node == "$target" ]]; then
                    RESOLVED_KEY=$key
                    RESOLVED_CONTAINER=$candidate
                    break
                fi
            fi
        done < <(podman_cmd ps --all --filter "label=$MANAGED_LABEL=true" \
            --filter "label=$CLUSTER_LABEL=$CLUSTER_LABEL_VALUE" \
            --format '{{.Names}}')
    fi

    [[ -n $RESOLVED_KEY ]] || return 1
    validate_key "$RESOLVED_KEY"
    if container_exists "$RESOLVED_CONTAINER"; then
        assert_managed_container "$RESOLVED_CONTAINER" "$RESOLVED_KEY"
        RESOLVED_HOSTNAME=$(podman_cmd inspect --format '{{.Config.Hostname}}' \
            "$RESOLVED_CONTAINER")
    else
        RESOLVED_HOSTNAME=$(read_state_value "$RESOLVED_KEY" hostname || true)
    fi
    RESOLVED_SLURM_NODE=$(read_state_value "$RESOLVED_KEY" node-name || true)
    if [[ -z $RESOLVED_SLURM_NODE && -n $RESOLVED_HOSTNAME ]]; then
        if record=$(node_record_by_hostname "$RESOLVED_HOSTNAME" 2>/dev/null); then
            RESOLVED_SLURM_NODE=${record%%|*}
            write_state_value "$RESOLVED_KEY" node-name "$RESOLVED_SLURM_NODE"
        fi
    fi
    [[ -z $RESOLVED_SLURM_NODE ]] ||
        validate_slurm_node_name "$RESOLVED_SLURM_NODE" ||
        die 'stored Slurm node name contains unsafe characters'
}

safe_remove_node_dir() {
    local key=$1 directory
    validate_key "$key"
    directory=$(node_dir_for_key "$key")
    [[ -e $directory ]] || return 0
    [[ -d $directory && ! -L $directory ]] ||
        die "refusing to remove unexpected path $directory"
    case $directory in
        "$NODES_STATE_DIR"/*) ;;
        *) die "refusing to remove path outside $NODES_STATE_DIR" ;;
    esac
    # -xdev prevents a compromised or accidentally mounted spool subtree from
    # making cleanup cross a filesystem boundary.  The only expected contents
    # are this node's registration metadata and private slurmd spool.
    find -P "$directory" -xdev -depth -delete
}
