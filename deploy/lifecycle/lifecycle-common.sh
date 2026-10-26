#!/usr/bin/env bash
# Common client for Slurm ResumeProgram and SuspendProgram.
#
# This code deliberately contains no Podman, EC2, IAM, or cloud-SDK calls.  The
# controller is allowed to request a lifecycle transition, not to administer
# infrastructure.  The mTLS lifecycle service owns cloud authorization and
# reconciliation behind a much narrower API.

lifecycle_die() {
    printf '[slurm-lifecycle] ERROR: %s\n' "$*" >&2
    exit 1
}

lifecycle_log() {
    printf '[slurm-lifecycle] %s\n' "$*" >&2
}

# Do not source this file.  It is an administrator-controlled data file, not a
# shell program, and treating it as shell would turn a configuration mistake
# into code execution in the slurmctld security context.
lifecycle_load_config() {
    local line key value required
    declare -gA LIFECYCLE_SETTINGS=()

    while IFS= read -r line || [[ -n $line ]]; do
        [[ $line =~ ^[[:space:]]*$ || $line =~ ^[[:space:]]*# ]] && continue
        [[ $line =~ ^[A-Z][A-Z0-9_]*=[^[:space:]]+$ ]] ||
            lifecycle_die "malformed line in $LIFECYCLE_CONFIG"
        key=${line%%=*}
        value=${line#*=}
        case $key in
            API_URL|CLUSTER_ID|TLS_CA_FILE|TLS_CERT_FILE|TLS_KEY_FILE|\
            CONNECT_TIMEOUT_SECONDS|TOTAL_TIMEOUT_SECONDS|MAX_RESUME_CONTEXT_BYTES) ;;
            *) lifecycle_die "unknown setting $key in $LIFECYCLE_CONFIG" ;;
        esac
        [[ -z ${LIFECYCLE_SETTINGS[$key]+present} ]] ||
            lifecycle_die "duplicate $key in $LIFECYCLE_CONFIG"
        LIFECYCLE_SETTINGS[$key]=$value
    done < "$LIFECYCLE_CONFIG"

    for required in API_URL CLUSTER_ID TLS_CA_FILE TLS_CERT_FILE TLS_KEY_FILE \
        CONNECT_TIMEOUT_SECONDS TOTAL_TIMEOUT_SECONDS MAX_RESUME_CONTEXT_BYTES; do
        [[ -n ${LIFECYCLE_SETTINGS[$required]:-} ]] ||
            lifecycle_die "missing $required in $LIFECYCLE_CONFIG"
    done
}

lifecycle_validate_uint() {
    local value=$1 minimum=$2 maximum=$3 name=$4
    [[ $value =~ ^[0-9]+$ ]] || lifecycle_die "$name must be an integer"
    ((value >= minimum && value <= maximum)) ||
        lifecycle_die "$name must be between $minimum and $maximum"
}

lifecycle_validate_private_key() {
    local path=$1 mode owner
    [[ $path == /* && -f $path && ! -L $path && -r $path ]] ||
        lifecycle_die "TLS_KEY_FILE must be a readable, non-symlink regular file"
    mode=$(stat --format='%a' "$path") || lifecycle_die "cannot stat TLS key $path"
    owner=$(stat --format='%u' "$path") || lifecycle_die "cannot stat TLS key $path"
    # Interpret the mode as octal.  Any group/other permission exposes the
    # client identity to another local principal and is therefore rejected.
    # The hook runs as SlurmUser, so accepting a differently owned key would
    # make rotation and revocation depend on ambient root/container behavior.
    (((8#$mode & 077) == 0)) ||
        lifecycle_die "TLS key $path must not have group/other permissions"
    ((owner == EUID)) ||
        lifecycle_die "TLS key $path must be owned by uid $EUID"
}

lifecycle_validate_public_file() {
    local path=$1 description=$2 mode
    [[ $path == /* && -f $path && ! -L $path && -r $path ]] ||
        lifecycle_die "$description must be an absolute readable, non-symlink regular file"
    mode=$(stat --format='%a' "$path") || lifecycle_die "cannot stat $path"
    (((8#$mode & 022) == 0)) ||
        lifecycle_die "$description $path must not be group/world writable"
}

lifecycle_init() {
    local config_mode config_owner
    for tool in curl sha256sum base64 date head mktemp stat; do
        command -v "$tool" >/dev/null 2>&1 || lifecycle_die "$tool is required"
    done

    LIFECYCLE_CONFIG=${SLURM_LIFECYCLE_CONFIG:-/etc/slurm/lifecycle-client.conf}
    [[ $LIFECYCLE_CONFIG == /* && -f $LIFECYCLE_CONFIG && ! -L $LIFECYCLE_CONFIG ]] ||
        lifecycle_die 'SLURM_LIFECYCLE_CONFIG must name a non-symlink regular file'
    config_mode=$(stat --format='%a' "$LIFECYCLE_CONFIG") ||
        lifecycle_die "cannot stat $LIFECYCLE_CONFIG"
    config_owner=$(stat --format='%u' "$LIFECYCLE_CONFIG") ||
        lifecycle_die "cannot stat owner of $LIFECYCLE_CONFIG"
    ((config_owner == 0)) ||
        lifecycle_die "$LIFECYCLE_CONFIG must be owned by root"
    (((8#$config_mode & 022) == 0)) ||
        lifecycle_die "$LIFECYCLE_CONFIG must not be group/world writable"

    lifecycle_load_config
    LIFECYCLE_API_URL=${LIFECYCLE_SETTINGS[API_URL]}
    LIFECYCLE_CLUSTER_ID=${LIFECYCLE_SETTINGS[CLUSTER_ID]}
    LIFECYCLE_CA_FILE=${LIFECYCLE_SETTINGS[TLS_CA_FILE]}
    LIFECYCLE_CERT_FILE=${LIFECYCLE_SETTINGS[TLS_CERT_FILE]}
    LIFECYCLE_KEY_FILE=${LIFECYCLE_SETTINGS[TLS_KEY_FILE]}
    LIFECYCLE_CONNECT_TIMEOUT=${LIFECYCLE_SETTINGS[CONNECT_TIMEOUT_SECONDS]}
    LIFECYCLE_TOTAL_TIMEOUT=${LIFECYCLE_SETTINGS[TOTAL_TIMEOUT_SECONDS]}
    LIFECYCLE_MAX_RESUME_BYTES=${LIFECYCLE_SETTINGS[MAX_RESUME_CONTEXT_BYTES]}

    [[ $LIFECYCLE_API_URL =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[-A-Za-z0-9._~]+)*$ ]] ||
        lifecycle_die 'API_URL must be a fixed HTTPS origin/path without query or credentials'
    LIFECYCLE_API_URL=${LIFECYCLE_API_URL%/}
    [[ $LIFECYCLE_CLUSTER_ID =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] ||
        lifecycle_die 'CLUSTER_ID contains unsupported characters'
    lifecycle_validate_public_file "$LIFECYCLE_CA_FILE" TLS_CA_FILE
    lifecycle_validate_public_file "$LIFECYCLE_CERT_FILE" TLS_CERT_FILE
    lifecycle_validate_private_key "$LIFECYCLE_KEY_FILE"
    lifecycle_validate_uint "$LIFECYCLE_CONNECT_TIMEOUT" 1 10 CONNECT_TIMEOUT_SECONDS
    lifecycle_validate_uint "$LIFECYCLE_TOTAL_TIMEOUT" 2 30 TOTAL_TIMEOUT_SECONDS
    ((LIFECYCLE_TOTAL_TIMEOUT >= LIFECYCLE_CONNECT_TIMEOUT)) ||
        lifecycle_die 'TOTAL_TIMEOUT_SECONDS must be at least CONNECT_TIMEOUT_SECONDS'
    lifecycle_validate_uint "$LIFECYCLE_MAX_RESUME_BYTES" 1024 10485760 \
        MAX_RESUME_CONTEXT_BYTES
}

# A subshell gives every request its own EXIT trap.  This matters because
# slurmctld can terminate or replace a hook during controller failover; private
# copies of the resume context must not accumulate in /tmp.
lifecycle_request() (
    local action=$1 hostlist=$2 resume_context=${3:-} work_dir payload response
    local context_b64= context_size request_material idempotency_key request_id endpoint
    local http_status

    [[ $action == resume || $action == suspend ]] ||
        lifecycle_die "unsupported lifecycle action: $action"
    # This is Slurm hostlist syntax, not an arbitrary user string.  Restricting
    # it also makes direct JSON construction below unambiguous.
    ((${#hostlist} <= 4096)) || lifecycle_die 'node hostlist is too long'
    [[ $hostlist =~ ^[A-Za-z0-9_.-]+([,\[][A-Za-z0-9_,.\]-]*)?$ ]] ||
        lifecycle_die 'node hostlist contains unsupported characters'

    work_dir=$(mktemp -d "${TMPDIR:-/tmp}/slurm-lifecycle.XXXXXXXX") ||
        lifecycle_die 'could not create private request directory'
    chmod 0700 "$work_dir"
    payload="$work_dir/request.json"
    response="$work_dir/response.json"
    trap 'rm -rf -- "$work_dir"' EXIT

    if [[ -n $resume_context ]]; then
        [[ -f $resume_context && ! -L $resume_context && -r $resume_context ]] ||
            lifecycle_die 'the copied resume context is not a readable regular file'
        context_size=$(stat --format='%s' "$resume_context") ||
            lifecycle_die 'could not stat the copied resume context'
        ((context_size <= LIFECYCLE_MAX_RESUME_BYTES)) ||
            lifecycle_die "resume context exceeds ${LIFECYCLE_MAX_RESUME_BYTES} bytes"
        context_b64=$(base64 --wrap=0 "$resume_context") ||
            lifecycle_die 'could not encode resume context'
    fi

    # The key is stable for curl retries in *this* invocation but deliberately
    # differs for a later suspend/resume cycle of the same node.  Reusing a
    # content-only key forever would cause a later legitimate suspend to replay
    # the response from an older instance generation.  The server must also
    # reconcile desired state by node name, so two separate hook invocations
    # remain semantically idempotent.
    if [[ -r /proc/sys/kernel/random/uuid ]]; then
        IFS= read -r request_id < /proc/sys/kernel/random/uuid
    else
        request_id=$(printf '%s|%s|%s|%s' "$LIFECYCLE_CLUSTER_ID" "$action" \
            "$$" "$(date +%s%N)" |
            sha256sum)
        request_id=${request_id%% *}
    fi
    request_material="$LIFECYCLE_CLUSTER_ID|$action|$hostlist|$context_b64|$request_id"
    idempotency_key=$(printf '%s' "$request_material" | sha256sum)
    idempotency_key="slurm-${idempotency_key%% *}"

    printf '{"cluster_id":"%s","nodes":"%s","resume_context_b64":"%s"}\n' \
        "$LIFECYCLE_CLUSTER_ID" "$hostlist" "$context_b64" > "$payload"
    chmod 0600 "$payload"
    endpoint="$LIFECYCLE_API_URL/v1/clusters/$LIFECYCLE_CLUSTER_ID/nodes:$action"

    lifecycle_log "requesting $action for $hostlist request_id=$request_id"
    # --fail-with-body produces a failure exit status for every non-2xx HTTP
    # response while retaining the bounded error body for diagnostics.  Curl's
    # retry reuses the same idempotency key, so a lost 2xx response cannot
    # create a second infrastructure operation.
    if ! http_status=$(curl --silent --show-error --fail-with-body \
        --proto '=https' --tlsv1.3 \
        --connect-timeout "$LIFECYCLE_CONNECT_TIMEOUT" \
        --max-time "$LIFECYCLE_TOTAL_TIMEOUT" \
        --retry 2 --retry-delay 1 --retry-all-errors \
        --retry-max-time "$LIFECYCLE_TOTAL_TIMEOUT" \
        --max-filesize 65536 \
        --cacert "$LIFECYCLE_CA_FILE" \
        --cert "$LIFECYCLE_CERT_FILE" \
        --key "$LIFECYCLE_KEY_FILE" \
        --header 'Content-Type: application/json' \
        --header "Idempotency-Key: $idempotency_key" \
        --header "X-Request-ID: $request_id" \
        --data-binary "@$payload" \
        --output "$response" \
        --write-out '%{http_code}' \
        --request POST "$endpoint"); then
        lifecycle_log "lifecycle API rejected or timed out request_id=$request_id"
        if [[ -s $response ]]; then
            # Cap diagnostics so an unhealthy peer cannot flood slurmctld logs.
            head --bytes=2048 "$response" >&2
            printf '\n' >&2
        fi
        return 1
    fi
    case $http_status in
        200|202) ;;
        *)
            lifecycle_log "unexpected lifecycle API status $http_status request_id=$request_id"
            return 1
            ;;
    esac
    lifecycle_log "$action accepted for $hostlist request_id=$request_id"
)
