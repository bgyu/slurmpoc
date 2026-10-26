#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=_lib.sh
source "$script_dir/_lib.sh"

usage() {
    cat <<'EOF'
Usage: scripts/nodes/add.sh [--profile FEATURE] [--timeout SECONDS] KEY

Start one local Podman slurmd and register a bounded dynamic simulation node.
KEY is a stable local lifecycle name; Slurm uses the managed host name.
EOF
}

profile=${NODE_PROFILE:-local-podman}
registration_timeout=${NODE_REGISTRATION_TIMEOUT:-120}
key=
while (($#)); do
    case $1 in
        --profile)
            (($# >= 2)) || die '--profile requires a value'
            profile=$2
            shift 2
            ;;
        --timeout)
            (($# >= 2)) || die '--timeout requires a value'
            registration_timeout=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --*) die "unknown option: $1" ;;
        *)
            [[ -z $key ]] || die 'only one KEY may be supplied'
            key=$1
            shift
            ;;
    esac
done
[[ -n $key ]] || { usage >&2; exit 64; }

validate_settings
validate_key "$key"
validate_profile "$profile"
validate_uint_range "$registration_timeout" 1 600 registration-timeout
require_host_tools
prepare_state_root
take_lock exclusive
require_controller

container=$(container_name_for_key "$key")
hostname=$(hostname_for_key "$key")
node_directory=$(node_dir_for_key "$key")

if container_exists "$container"; then
    assert_managed_container "$container" "$key"
    existing_profile=$(container_label "$container" "$PROFILE_LABEL")
    [[ $existing_profile == "$profile" ]] ||
        die "existing container uses profile $existing_profile, not $profile"
    if ! container_running "$container"; then
        stored_node=$(read_state_value "$key" node-name || true)
        [[ -z $stored_node ]] ||
            die "managed container is stopped while it owns $stored_node; run remove.sh $key to reconcile safely"
        log "restarting stopped pre-registration container $container"
        podman_cmd start "$container" >/dev/null
    else
        log "container $container already exists; checking registration"
    fi
    if registration=$(wait_for_registration "$key" "$hostname" "$registration_timeout"); then
        IFS='|' read -r node state <<< "$registration"
        printf 'key=%s container=%s node=%s state=%s\n' \
            "$key" "$container" "$node" "$state"
        exit 0
    fi
    podman_cmd logs --tail 100 "$container" >&2 || true
    die "timed out waiting for $container to register"
fi

if [[ -d $node_directory && ! -L $node_directory ]]; then
    stored_node=$(read_state_value "$key" node-name || true)
    [[ -z $stored_node ]] ||
        die "metadata for $key still owns Slurm node $stored_node; run remove.sh $key first"
fi

controller_id=$(compose_service_id controller) ||
    die 'the Compose controller disappeared during node creation'
image=${NODE_IMAGE:-$(podman_cmd inspect --format '{{.ImageName}}' "$controller_id")}
[[ -n $image && $image != *$'\n'* ]] || die 'could not determine a single node image'
podman_cmd image exists "$image" || die "node image is not present locally: $image"

if [[ -n ${NODE_NETWORK:-} ]]; then
    network=$NODE_NETWORK
else
    mapfile -t inspected_networks < <(podman_cmd inspect \
        --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
        "$controller_id")
    controller_networks=()
    for inspected_network in "${inspected_networks[@]}"; do
        # Podman appends a newline to the rendered Go template even though
        # println already emitted one.  Ignore that provider-specific empty
        # record so one real network is still counted as one network.
        [[ -n $inspected_network ]] && controller_networks+=("$inspected_network")
    done
    ((${#controller_networks[@]} == 1)) ||
        die 'controller must have exactly one network, or set NODE_NETWORK explicitly'
    network=${controller_networks[0]}
fi

node_cpus=${NODE_CPUS:-2}
node_memory=${NODE_MEMORY:-1536m}
[[ $node_cpus =~ ^[1-9][0-9]*([.][0-9]+)?$ ]] || die 'NODE_CPUS is invalid'
[[ $node_memory =~ ^[1-9][0-9]*([kKmMgG][bB]?)?$ ]] || die 'NODE_MEMORY is invalid'

[[ ! -L $node_directory ]] || die "$node_directory must not be a symbolic link"
install -d -m 0700 "$node_directory"
install -d -m 0700 "$node_directory/spool"
write_state_value "$key" hostname "$hostname"
write_state_value "$key" profile "$profile"

# Reuse the controller's exact /shared backing store.  A separate directory or
# volume would give dynamic jobs a split-brain view of workflow manifests and
# results.  Named volumes are safe to reattach by name.  Bind sources are
# accepted only after canonicalising them beneath this repository, so an
# unexpected host path exposed to the controller is not propagated further.
shared_mount=$(podman_cmd inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/shared"}}{{.Type}}|{{.Name}}|{{.Source}}{{println}}{{end}}{{end}}' \
    "$controller_id")
[[ -n $shared_mount && $shared_mount != *$'\n'* ]] ||
    die 'controller must have exactly one /shared mount'
IFS='|' read -r shared_type shared_name shared_source <<< "$shared_mount"
case $shared_type in
    volume)
        [[ -n $shared_name && $shared_name =~ ^[A-Za-z0-9_.-]+$ ]] ||
            die 'controller /shared named volume has an unsafe name'
        shared_volume_spec="$shared_name:/shared:rw"
        ;;
    bind)
        shared_source=$(realpath -e -- "$shared_source") ||
            die 'controller /shared bind source does not resolve'
        case $shared_source in
            "$PROJECT_DIR"/*) shared_volume_spec="$shared_source:/shared:rw,z" ;;
            *) die 'refusing to propagate a /shared bind outside the project directory' ;;
        esac
        ;;
    *) die "unsupported controller /shared mount type: $shared_type" ;;
esac

log "starting $container from $image on $network"
# Dynamic nodes deliberately use the image's normal compute entrypoint.  That
# keeps input validation, secret staging, service UID handling and health
# behavior identical to the fixed Compose nodes.  slurmd itself remains root
# inside the rootless user namespace so it can enter the workflow UID; every
# ambient capability outside the tested simulation launch set is removed.
podman_cmd run --detach \
    --name "$container" \
    --hostname "$hostname" \
    --network "$network" \
    --pull never \
    --read-only \
    --pids-limit 512 \
    --cpus "$node_cpus" \
    --memory "$node_memory" \
    --stop-timeout "$NODE_STOP_TIMEOUT" \
    --security-opt no-new-privileges \
    --cap-drop all \
    --cap-add CHOWN \
    --cap-add DAC_OVERRIDE \
    --cap-add FOWNER \
    --cap-add KILL \
    --cap-add SETGID \
    --cap-add SETUID \
    --cap-add SYS_NICE \
    --label "$MANAGED_LABEL=true" \
    --label "$KEY_LABEL=$key" \
    --label "$PROFILE_LABEL=$profile" \
    --label "$CLUSTER_LABEL=$CLUSTER_LABEL_VALUE" \
    --env 'CONTAINER_ROLE=compute' \
    --env 'CLUSTER_DEPLOYMENT_PROFILE=simulation' \
    --env 'START_EMBEDDED_MUNGE=1' \
    --env 'MUNGE_KEY_FILE=/run/secrets/munge.key' \
    --env "SLURMD_FEATURE=$profile" \
    --env 'SLURM_CONF_SERVER=controller:6817' \
    --tmpfs /run:rw,nodev,nosuid,noexec,size=64m,mode=0755 \
    --tmpfs /tmp:rw,nodev,nosuid,noexec,size=128m,mode=1777 \
    --tmpfs /var/log:rw,nodev,nosuid,noexec,size=64m,mode=0755 \
    --tmpfs /var/lib/munge:rw,nodev,nosuid,noexec,size=16m,mode=0700 \
    --volume "$RUNTIME_DIR/secrets/munge.key:/run/secrets/munge.key:ro,z" \
    --volume "$shared_volume_spec" \
    --volume "$node_directory/spool:/var/spool/slurmd:rw,Z" \
    "$image" >/dev/null

if registration=$(wait_for_registration "$key" "$hostname" "$registration_timeout"); then
    IFS='|' read -r node state <<< "$registration"
    printf 'key=%s container=%s node=%s state=%s\n' \
        "$key" "$container" "$node" "$state"
    exit 0
fi

# Retain the failed container and metadata: deleting them would erase logs and
# could lose the only mapping to a late controller registration.  A retry is
# idempotent; remove.sh performs the guarded reconciliation if it must be torn
# down.
podman_cmd logs --tail 100 "$container" >&2 || true
die "timed out after ${registration_timeout}s waiting for $container to register; the managed container was retained"
