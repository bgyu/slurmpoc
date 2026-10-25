#!/usr/bin/env bash
# One immutable image serves every cluster role, but this entrypoint gives each
# role a deliberately different runtime contract. PID 1 remains a very small
# root supervisor only long enough to validate read-only inputs, copy secrets
# into tmpfs with narrow ownership, and launch the daemon as its service user.
# slurmd is the sole exception: it must remain root to create cgroups and enter
# the submitting user's UID/GID before starting a job.
set -Eeuo pipefail

readonly RUNTIME_DIR=/run/slurm
readonly RUNTIME_CONFIG_DIR=$RUNTIME_DIR/config
readonly RUNTIME_MUNGE_KEY=/run/munge/munge.key
readonly RUNTIME_JWT_KEY=$RUNTIME_CONFIG_DIR/jwt_hs256.key
readonly RUNTIME_JWKS=$RUNTIME_CONFIG_DIR/slurm-jwks.json
readonly RUNTIME_STORAGE_CA=$RUNTIME_CONFIG_DIR/storage_ca.pem

role=${CONTAINER_ROLE:-}
profile=${SLURM_PROFILE:-production}
declare -a child_pids=()

log() {
    printf '[%s/%s] %s\n' "${role:-unset}" "$profile" "$*" >&2
}

die() {
    log "ERROR: $*"
    exit 78
}

usage_error() {
    log "ERROR: $*"
    exit 64
}

stop_children() {
    trap - TERM INT EXIT
    if ((${#child_pids[@]})); then
        kill -TERM "${child_pids[@]}" 2>/dev/null || true
        wait "${child_pids[@]}" 2>/dev/null || true
    fi
}

handle_signal() {
    stop_children
    exit 0
}

trap handle_signal TERM INT
trap stop_children EXIT

require_root_supervisor() {
    [[ $(id -u) -eq 0 ]] ||
        die 'entrypoint must start as container root to stage credentials and drop privileges'
}

validate_uint() {
    local value=$1 minimum=$2 maximum=$3 description=$4
    [[ $value =~ ^[0-9]+$ ]] || usage_error "$description must be an integer"
    ((value >= minimum && value <= maximum)) ||
        usage_error "$description must be between $minimum and $maximum"
}

validate_host() {
    local value=$1 description=$2
    [[ $value =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] ||
        usage_error "$description is not a supported DNS name or IPv4 address"
}

validate_name() {
    local value=$1 description=$2
    [[ $value =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,63}$ ]] ||
        usage_error "$description contains unsupported characters"
}

# Validate a bind-mounted input before copying it. Production insists that the
# source is root-owned; accepting a writable or symlinked source would let a
# local process swap policy/key material between validation and use.
validate_input_file() {
    local path=$1 description=$2 secrecy=${3:-public}
    local mode owner permissions

    [[ $path == /* ]] || die "$description must use an absolute path"
    [[ -f $path && ! -L $path && -r $path ]] ||
        die "$description must be a readable, non-symlink regular file: $path"
    mode=$(stat --format='%a' -- "$path") || die "cannot stat $description"
    owner=$(stat --format='%u' -- "$path") || die "cannot stat $description owner"
    [[ $mode =~ ^[0-7]{3,4}$ ]] || die "$description has an invalid mode"
    permissions=$((8#$mode))

    if [[ $secrecy == secret ]]; then
        (( (permissions & 0077) == 0 )) ||
            die "$description must not be accessible by group or other (use 0400 or 0600)"
    else
        (( (permissions & 0022) == 0 )) ||
            die "$description must not be group/world writable"
    fi
    if [[ $profile == production && $owner -ne 0 ]]; then
        die "$description must be root-owned in the production profile"
    fi
}

stage_file() {
    local source=$1 destination=$2 owner=$3 group=$4 mode=$5
    install -o "$owner" -g "$group" -m "$mode" -- "$source" "$destination"
}

config_values() {
    local key=$1 file=$2
    awk -v wanted="$key" '
        /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
        {
            line = $0
            sub(/[[:space:]]*#.*/, "", line)
            pos = index(line, "=")
            if (!pos) next
            name = substr(line, 1, pos - 1)
            value = substr(line, pos + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            if (tolower(name) == tolower(wanted)) print value
        }
    ' "$file"
}

config_value() {
    local key=$1 file=$2 values count
    values=$(config_values "$key" "$file")
    count=$(grep -c . <<<"$values" || true)
    [[ $count -eq 1 ]] ||
        die "$file must define $key exactly once (found $count)"
    printf '%s\n' "$values"
}

require_config_equals() {
    local file=$1 key=$2 expected=$3 actual
    actual=$(config_value "$key" "$file")
    [[ ${actual,,} == ${expected,,} ]] ||
        die "$file must set $key=$expected (found $actual)"
}

require_config_contains() {
    local file=$1 key=$2 required=$3 actual
    actual=$(config_value "$key" "$file")
    [[ ",${actual,,}," == *",${required,,},"* ]] ||
        die "$file parameter $key must contain $required"
}

validate_production_slurm_config() {
    local file=$1 auth_alt tls_parameters

    # Includes are useful on bare metal, but securely copying and validating a
    # graph of arbitrary paths inside a container is error-prone. Production
    # containers therefore receive one fully rendered slurm.conf.
    if grep -Eiq '^[[:space:]]*include([[:space:]]|=)' "$file"; then
        die 'production slurm.conf must be fully rendered and must not use Include directives'
    fi
    grep -q $'\r' "$file" && die 'production slurm.conf must use Unix line endings'

    require_config_equals "$file" AuthType auth/munge
    require_config_equals "$file" CredType cred/munge
    require_config_equals "$file" AuthAltTypes auth/jwt
    require_config_equals "$file" SlurmUser slurm
    require_config_equals "$file" SlurmdUser root
    require_config_equals "$file" DisableRootJobs YES
    require_config_equals "$file" TLSType tls/s2n
    require_config_contains "$file" CommunicationParameters disable_http
    require_config_equals "$file" SelectType select/cons_tres

    auth_alt=$(config_value AuthAltParameters "$file")
    [[ ",$auth_alt," == *",jwks=$RUNTIME_JWKS,"* ]] ||
        die "AuthAltParameters must use jwks=$RUNTIME_JWKS"
    [[ ",$auth_alt," == *',disable_token_creation,'* ]] ||
        die 'AuthAltParameters must disable local token creation'

    tls_parameters=$(config_value TLSParameters "$file")
    [[ ",$tls_parameters," == *",ca_cert_file=$RUNTIME_CONFIG_DIR/ca_cert.pem,"* ]] ||
        die 'TLSParameters must use the staged control-plane CA path'

    if [[ $role == compute ]]; then
        require_config_equals "$file" ProctrackType proctrack/cgroup
        require_config_contains "$file" TaskPlugin task/cgroup
        require_config_contains "$file" TaskPlugin task/affinity
        require_config_equals "$file" JobAcctGatherType jobacct_gather/cgroup
        require_config_equals "$file" NamespaceType namespace/tmpfs
        require_config_contains "$file" PrologFlags Contain
    fi
}

validate_cgroup_config() {
    local file=$1 plugin
    plugin=$(config_value CgroupPlugin "$file")
    if [[ $profile == production ]]; then
        [[ ${plugin,,} == autodetect || ${plugin,,} == cgroup/v2 ]] ||
            die 'production cgroup.conf must select autodetect or cgroup/v2'
        require_config_equals "$file" ConstrainCores yes
        require_config_equals "$file" ConstrainRAMSpace yes
        require_config_equals "$file" ConstrainSwapSpace yes
    elif [[ ${plugin,,} != disabled ]]; then
        die 'rootless simulation must explicitly use CgroupPlugin=disabled'
    fi
}

prepare_runtime_directories() {
    install -d -o root -g slurm -m 0755 "$RUNTIME_DIR" "$RUNTIME_CONFIG_DIR"
    # Daemons may create only PID files here.  Keeping the parent and config
    # directory root-owned prevents a compromised SlurmUser process from
    # replacing staged policy or trust material through a parent-dir rename.
    install -d -o slurm -g slurm -m 0755 "$RUNTIME_DIR/pids"
    install -d -o slurm -g slurm -m 0750 /var/log/slurm
}

prepare_slurm_config() {
    local source cgroup_source namespace_source

    if [[ $profile == simulation ]]; then
        source=${SLURM_CONFIG_FILE:-/etc/slurm/profiles/simulation/slurm.conf}
        cgroup_source=${SLURM_CGROUP_CONFIG_FILE:-/etc/slurm/profiles/simulation/cgroup.conf}
    else
        [[ -n ${SLURM_CONFIG_FILE:-} ]] ||
            die 'SLURM_CONFIG_FILE must be explicitly set for production'
        source=$SLURM_CONFIG_FILE
        cgroup_source=${SLURM_CGROUP_CONFIG_FILE:-/etc/slurm/profiles/production/cgroup.conf}
        namespace_source=${SLURM_JOB_CONTAINER_CONFIG_FILE:-/etc/slurm/profiles/production/job_container.conf}
    fi

    validate_input_file "$source" slurm.conf public
    validate_input_file "$cgroup_source" cgroup.conf public
    [[ $profile == simulation ]] ||
        validate_input_file "$namespace_source" job_container.conf public

    stage_file "$source" "$RUNTIME_CONFIG_DIR/slurm.conf" root slurm 0644
    stage_file "$cgroup_source" "$RUNTIME_CONFIG_DIR/cgroup.conf" root slurm 0644
    if [[ $profile == production ]]; then
        stage_file "$namespace_source" "$RUNTIME_CONFIG_DIR/job_container.conf" root slurm 0644
        validate_production_slurm_config "$RUNTIME_CONFIG_DIR/slurm.conf"
    fi
    validate_cgroup_config "$RUNTIME_CONFIG_DIR/cgroup.conf"
    export SLURM_CONF=$RUNTIME_CONFIG_DIR/slurm.conf
}

validate_jwks() {
    local path=$1
    python3 - "$path" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as stream:
    document = json.load(stream)
keys = document.get("keys") if isinstance(document, dict) else None
if not isinstance(keys, list) or not keys:
    raise SystemExit("JWKS must contain a non-empty keys array")
private_fields = {"d", "p", "q", "dp", "dq", "qi", "oth"}
usable = 0
for key in keys:
    if not isinstance(key, dict):
        raise SystemExit("each JWKS key must be an object")
    if private_fields.intersection(key):
        raise SystemExit("JWKS must not contain RSA private key material")
    if key.get("kty") == "RSA" and key.get("alg", "RS256") == "RS256":
        if isinstance(key.get("n"), str) and isinstance(key.get("e"), str):
            usable += 1
if not usable:
    raise SystemExit("JWKS contains no usable RS256 public key")
PY
}

prepare_jwt_trust() {
    local source size
    case "$profile:$role" in
        simulation:controller|simulation:slurmdbd)
            source=${SLURM_JWT_KEY_FILE:-}
            [[ -n $source ]] || die 'SLURM_JWT_KEY_FILE is required in simulation controller/slurmdbd'
            validate_input_file "$source" 'development HS256 JWT key' secret
            size=$(stat --format='%s' -- "$source")
            ((size >= 32)) || die 'HS256 JWT key must contain at least 32 random bytes'
            stage_file "$source" "$RUNTIME_JWT_KEY" slurm slurm 0400
            ;;
        production:controller|production:slurmdbd)
            source=${SLURM_JWKS_FILE:-}
            [[ -n $source ]] || die 'SLURM_JWKS_FILE is required in production'
            validate_input_file "$source" 'RS256 JWKS trust file' public
            validate_jwks "$source"
            stage_file "$source" "$RUNTIME_JWKS" slurm slurm 0400
            ;;
    esac
}

prepare_tls_identity() {
    [[ $profile == production ]] || return 0

    local ca=${SLURM_TLS_CA_FILE:-} cert=${SLURM_TLS_CERT_FILE:-}
    local key=${SLURM_TLS_KEY_FILE:-} owner cert_name key_name
    [[ -n $ca && -n $cert && -n $key ]] ||
        die 'production requires SLURM_TLS_CA_FILE, SLURM_TLS_CERT_FILE, and SLURM_TLS_KEY_FILE'
    validate_input_file "$ca" 'Slurm TLS CA certificate' public
    validate_input_file "$cert" 'Slurm TLS role certificate' public
    validate_input_file "$key" 'Slurm TLS private key' secret

    openssl x509 -in "$ca" -noout >/dev/null 2>&1 || die 'invalid Slurm TLS CA certificate'
    openssl x509 -in "$cert" -noout -checkend 86400 >/dev/null 2>&1 ||
        die 'Slurm TLS role certificate is invalid or expires within 24 hours'
    openssl pkey -in "$key" -noout >/dev/null 2>&1 ||
        die 'Slurm TLS private key is invalid or encrypted'
    [[ $(openssl x509 -in "$cert" -pubkey -noout | sha256sum) == \
       $(openssl pkey -in "$key" -pubout 2>/dev/null | sha256sum) ]] ||
        die 'Slurm TLS certificate does not match its private key'

    case "$role" in
        controller) owner=slurm; cert_name=ctld_cert.pem; key_name=ctld_cert_key.pem ;;
        slurmdbd) owner=slurm; cert_name=dbd_cert.pem; key_name=dbd_cert_key.pem ;;
        compute) owner=slurm; cert_name=slurmd_cert.pem; key_name=slurmd_cert_key.pem ;;
        restd) owner=slurmrestd; cert_name=restd_cert.pem; key_name=restd_cert_key.pem ;;
        *) die "TLS identity is not defined for role $role" ;;
    esac
    stage_file "$ca" "$RUNTIME_CONFIG_DIR/ca_cert.pem" root root 0644
    # tls/s2n documents mode 0600 for both role certificate and key.
    stage_file "$cert" "$RUNTIME_CONFIG_DIR/$cert_name" "$owner" "$owner" 0600
    stage_file "$key" "$RUNTIME_CONFIG_DIR/$key_name" "$owner" "$owner" 0600
}

prepare_munge() {
    local embedded=${START_EMBEDDED_MUNGE:-0} source attempt
    case "$embedded" in
        1)
            source=${MUNGE_KEY_FILE:-}
            [[ -n $source ]] || die 'MUNGE_KEY_FILE is required when START_EMBEDDED_MUNGE=1'
            validate_input_file "$source" 'MUNGE key' secret
            (($(stat --format='%s' -- "$source") >= 32)) ||
                die 'MUNGE key must contain at least 32 bytes'
            install -d -o munge -g munge -m 0755 /run/munge /var/log/munge
            install -d -o munge -g munge -m 0700 /var/lib/munge
            stage_file "$source" "$RUNTIME_MUNGE_KEY" munge munge 0400
            runuser --user munge -- munged --foreground \
                --key-file="$RUNTIME_MUNGE_KEY" &
            child_pids+=("$!")
            ;;
        0)
            # Production can bind a host-level munged Unix socket instead of
            # copying the cluster-wide key into each role container.
            ;;
        *) usage_error 'START_EMBEDDED_MUNGE must be 0 or 1' ;;
    esac

    for ((attempt = 1; attempt <= 30; attempt++)); do
        if munge -n >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    die 'MUNGE did not become ready; start embedded MUNGE or mount its socket'
}

wait_for_port() {
    local host=$1 port=$2 description=$3 attempts=${4:-90} attempt
    validate_host "$host" "$description host"
    validate_uint "$port" 1 65535 "$description port"
    validate_uint "$attempts" 1 600 "$description wait attempts"
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if timeout 1 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$host" "$port" \
                2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    die "timed out waiting for $description at $host:$port"
}

read_password_file() {
    local path=$1 description=$2 value
    validate_input_file "$path" "$description" secret
    value=$(<"$path")
    [[ $value =~ ^[[:alnum:]_@%+=.,:-]{16,256}$ ]] ||
        die "$description must be 16-256 characters from the safe alphabet"
    printf '%s' "$value"
}

run_database() {
    [[ $profile == simulation ]] ||
        die 'embedded MariaDB is integration-only; production must use an external HA database'
    local password_file=${MARIADB_SLURM_PASSWORD_FILE:-} password attempt
    [[ -n $password_file ]] || die 'MARIADB_SLURM_PASSWORD_FILE is required'
    password=$(read_password_file "$password_file" 'MariaDB Slurm password')

    install -d -o mysql -g mysql -m 0750 /var/lib/mysql /run/mariadb
    if [[ ! -d /var/lib/mysql/mysql ]]; then
        log 'initialising integration MariaDB data directory'
        mariadb-install-db --user=mysql --datadir=/var/lib/mysql \
            --auth-root-authentication-method=socket
    fi

    mariadbd --user=mysql --datadir=/var/lib/mysql \
        --socket=/run/mariadb/mariadb.sock --bind-address=0.0.0.0 \
        --skip-name-resolve &
    child_pids+=("$!")

    for ((attempt = 1; attempt <= 90; attempt++)); do
        if mariadb-admin --protocol=socket --socket=/run/mariadb/mariadb.sock \
                ping --silent >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    mariadb-admin --protocol=socket --socket=/run/mariadb/mariadb.sock \
        ping --silent >/dev/null || die 'MariaDB did not become ready'

    # The password alphabet excludes quote, backslash, whitespace and comment
    # characters. It crosses stdin only, never argv or the environment.
    mariadb --protocol=socket --socket=/run/mariadb/mariadb.sock -uroot <<SQL
CREATE DATABASE IF NOT EXISTS slurm_acct_db;
CREATE USER IF NOT EXISTS 'slurm'@'%' IDENTIFIED BY '${password}';
ALTER USER 'slurm'@'%' IDENTIFIED BY '${password}';
GRANT ALL PRIVILEGES ON slurm_acct_db.* TO 'slurm'@'%';
FLUSH PRIVILEGES;
SQL
    unset password
    log 'MariaDB is ready; slurmdbd runs in its own container'
}

render_slurmdbd_config() {
    local template output=$RUNTIME_CONFIG_DIR/slurmdbd.conf
    local host=${SLURMDBD_STORAGE_HOST:-database}
    local port=${SLURMDBD_STORAGE_PORT:-3306}
    local database=${SLURMDBD_STORAGE_DATABASE:-slurm_acct_db}
    local user=${SLURMDBD_STORAGE_USER:-slurm}
    local password_file=${SLURMDBD_STORAGE_PASSWORD_FILE:-}
    local pass_script=${SLURMDBD_STORAGE_PASS_SCRIPT:-}
    local credential_mode credential_path tls_line= storage_ca

    if [[ $profile == production ]]; then
        [[ -n ${SLURMDBD_CONFIG_TEMPLATE:-} ]] ||
            die 'SLURMDBD_CONFIG_TEMPLATE must be explicitly set for production'
        template=$SLURMDBD_CONFIG_TEMPLATE
    else
        template=${SLURMDBD_CONFIG_TEMPLATE:-/etc/slurm/profiles/simulation/slurmdbd.conf.template}
    fi
    validate_input_file "$template" 'slurmdbd configuration template' public
    validate_host "$host" 'database host'
    validate_uint "$port" 1 65535 'database port'
    validate_name "$database" 'database name'
    validate_name "$user" 'database user'

    if [[ -n $password_file && -n $pass_script ]] ||
       [[ -z $password_file && -z $pass_script ]]; then
        die 'set exactly one of SLURMDBD_STORAGE_PASSWORD_FILE or SLURMDBD_STORAGE_PASS_SCRIPT'
    fi
    if [[ -n $password_file ]]; then
        # The renderer reads the file directly, so its value is not copied into
        # a process argument or environment variable.
        read_password_file "$password_file" 'slurmdbd database password' >/dev/null
        credential_mode=password
        credential_path=$password_file
    else
        validate_input_file "$pass_script" 'database token generator' public
        [[ -x $pass_script ]] || die 'SLURMDBD_STORAGE_PASS_SCRIPT must be executable'
        credential_mode=script
        credential_path=$RUNTIME_CONFIG_DIR/storage-pass-script
        stage_file "$pass_script" "$credential_path" slurm slurm 0500
    fi

    if [[ $profile == production ]]; then
        storage_ca=${SLURMDBD_STORAGE_TLS_CA_FILE:-}
        [[ -n $storage_ca ]] ||
            die 'SLURMDBD_STORAGE_TLS_CA_FILE is required for the production database connection'
        validate_input_file "$storage_ca" 'database TLS CA certificate' public
        openssl x509 -in "$storage_ca" -noout >/dev/null 2>&1 ||
            die 'invalid database TLS CA certificate'
        stage_file "$storage_ca" "$RUNTIME_STORAGE_CA" root slurm 0440
        tls_line="StorageParameters=SSL_CA=$RUNTIME_STORAGE_CA"
    fi

    python3 - "$template" "$output" "$host" "$port" "$database" "$user" \
        "$credential_mode" "$credential_path" "$tls_line" <<'PY'
import os
import re
import sys
from pathlib import Path

template, output, host, port, database, user, mode, credential_path, tls = sys.argv[1:]
text = Path(template).read_text(encoding="utf-8")
if mode == "password":
    raw = Path(credential_path).read_bytes().rstrip(b"\n")
    if not re.fullmatch(rb"[A-Za-z0-9_@%+=.,:-]{16,256}", raw):
        raise SystemExit("database password changed after validation")
    credential = "StoragePass=" + raw.decode("ascii")
else:
    credential = "StoragePassScript=" + credential_path

replacements = {
    "@STORAGE_HOST@": host,
    "@STORAGE_PORT@": port,
    "@STORAGE_DATABASE@": database,
    "@STORAGE_USER@": user,
    "@STORAGE_CREDENTIAL@": credential,
    "@STORAGE_TLS_PARAMETERS@": tls,
}
for marker, value in replacements.items():
    if text.count(marker) != 1:
        raise SystemExit(f"template must contain {marker} exactly once")
    text = text.replace(marker, value)
if "@STORAGE_" in text:
    raise SystemExit("unresolved storage placeholder in rendered configuration")

temporary = output + ".new"
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
    chown slurm:slurm "$output"
    chmod 0600 "$output"
    printf '%s\n' "$host" >"$RUNTIME_DIR/storage-host"
    printf '%s\n' "$port" >"$RUNTIME_DIR/storage-port"
    chmod 0644 "$RUNTIME_DIR/storage-host" "$RUNTIME_DIR/storage-port"
}

run_slurmdbd() {
    local host port
    prepare_munge
    prepare_jwt_trust
    prepare_tls_identity
    render_slurmdbd_config
    host=$(<"$RUNTIME_DIR/storage-host")
    port=$(<"$RUNTIME_DIR/storage-port")
    wait_for_port "$host" "$port" 'accounting database'
    log 'starting slurmdbd as uid 64030 (slurm)'
    runuser --user slurm -- slurmdbd -D -v \
        -f "$RUNTIME_CONFIG_DIR/slurmdbd.conf" &
    child_pids+=("$!")
}

register_simulation_accounting() {
    local cluster_name
    cluster_name=$(config_value ClusterName "$SLURM_CONF")
    if ! sacctmgr --noheader --parsable2 show cluster "$cluster_name" \
            format=Cluster 2>/dev/null | cut -d'|' -f1 | grep -Fxq "$cluster_name"; then
        log "registering integration cluster $cluster_name"
        sacctmgr --immediate add cluster "$cluster_name"
    fi
    if ! sacctmgr --noheader --parsable2 show account demo \
            format=Account 2>/dev/null | cut -d'|' -f1 | grep -Fxq demo; then
        sacctmgr --immediate add account demo Description='Integration workflow account'
    fi
    if ! sacctmgr --noheader --parsable2 show association \
            where "Cluster=$cluster_name" User=workflow \
            format=Cluster,User 2>/dev/null | grep -Fq "$cluster_name|workflow|"; then
        sacctmgr --immediate add user workflow Account=demo "Cluster=$cluster_name"
    fi
}

run_controller() {
    local dbd_host dbd_port
    prepare_slurm_config
    prepare_munge
    prepare_jwt_trust
    prepare_tls_identity

    dbd_host=${SLURMDBD_HOST:-$(config_value AccountingStorageHost "$SLURM_CONF")}
    dbd_port=${SLURMDBD_PORT:-$(config_value AccountingStoragePort "$SLURM_CONF")}
    wait_for_port "$dbd_host" "$dbd_port" slurmdbd
    [[ $profile == production ]] || register_simulation_accounting

    install -d -o slurm -g slurm -m 0700 /var/spool/slurmctld
    install -d -o slurm -g slurm -m 0750 /var/log/slurm
    log 'starting slurmctld as uid 64030 (slurm)'
    runuser --user slurm -- slurmctld -D -v -f "$SLURM_CONF" &
    child_pids+=("$!")
}

require_production_compute_isolation() {
    local inside outside length fs_type
    read -r inside outside length < /proc/self/uid_map
    if [[ $inside != 0 || $outside != 0 || $length -lt 4294967294 ]]; then
        die 'production slurmd cannot run in a remapped/rootless user namespace'
    fi
    fs_type=$(stat --file-system --format='%T' /sys/fs/cgroup)
    [[ $fs_type == cgroup2fs && -r /sys/fs/cgroup/cgroup.controllers ]] ||
        die 'production compute requires a unified cgroup v2 hierarchy'
    [[ -w /sys/fs/cgroup/cgroup.subtree_control ]] ||
        die 'production compute requires a writable/delegated cgroup v2 subtree'
}

run_compute() {
    local controller feature=${SLURMD_FEATURE:-} conf_server=${SLURM_CONF_SERVER:-}
    prepare_slurm_config
    prepare_munge
    prepare_tls_identity
    [[ $profile == simulation ]] || require_production_compute_isolation

    controller=${SLURM_CONTROLLER_HOST:-$(config_values SlurmctldHost "$SLURM_CONF" | head -n1)}
    [[ $controller != *'('* ]] ||
        die 'set SLURM_CONTROLLER_HOST when SlurmctldHost uses address syntax'
    wait_for_port "$controller" 6817 slurmctld
    install -d -o root -g root -m 0755 /var/spool/slurmd
    if [[ $profile == production ]]; then
        install -d -o root -g root -m 0755 /var/lib/slurm/job_container
    fi

    if [[ -n $feature || -n $conf_server ]]; then
        [[ -n $feature && -n $conf_server ]] ||
            die 'SLURMD_FEATURE and SLURM_CONF_SERVER must be set together'
        [[ $feature =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] ||
            usage_error 'SLURMD_FEATURE must be one literal feature name'
        [[ $conf_server =~ ^[A-Za-z0-9_.:-]+(,[A-Za-z0-9_.:-]+)*$ ]] ||
            usage_error 'SLURM_CONF_SERVER must be a comma-separated controller list'
        log "starting dynamic FUTURE slurmd for $(hostname -s), profile $feature"
        slurmd -D -v --conf-server "$conf_server" "-F${feature}" &
    else
        log "starting statically named slurmd for $(hostname -s)"
        slurmd -D -v &
    fi
    child_pids+=("$!")
}

run_restd() {
    local listen=${SLURMRESTD_LISTEN:-} threads api_version auth_mode
    prepare_slurm_config
    # MUNGE is optional for JWT-only REST. The integration profile starts it
    # for CLI diagnostics; production can omit the cluster-wide MUNGE key.
    if [[ ${START_EMBEDDED_MUNGE:-0} == 1 ]]; then
        prepare_munge
    fi
    prepare_tls_identity

    auth_mode=${REST_AUTH_MODE:-jwt}
    [[ $auth_mode == jwt ]] || die 'only JWT REST authentication is supported'
    api_version=${SLURM_API_VERSION:-v0.0.45}
    [[ $api_version == v0.0.45 ]] ||
        die 'image intentionally exposes only pinned REST data parser v0.0.45'
    threads=${SLURMRESTD_THREADS:-${SLURMRESTD_MAX_CONNECTIONS:-32}}
    validate_uint "$threads" 2 64 'slurmrestd thread count'

    if [[ -z $listen ]]; then
        [[ $profile == production ]] && listen=https://0.0.0.0:6820 || listen=0.0.0.0:6820
    fi
    if [[ $profile == production ]]; then
        [[ $listen =~ ^https://[A-Za-z0-9.-]+:[0-9]{1,5}$ ]] ||
            die 'production SLURMRESTD_LISTEN must be an explicit https://host:port listener'
    else
        [[ $listen =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}$ ]] ||
            usage_error 'simulation SLURMRESTD_LISTEN must be host:port'
    fi

    wait_for_port "${SLURM_CONTROLLER_HOST:-controller}" 6817 slurmctld
    log "starting JWT-only slurmrestd as uid 64031 with $threads bounded threads"
    runuser --user slurmrestd -- env \
        SLURM_CONF="$SLURM_CONF" \
        SLURM_JWT=daemon \
        SLURMRESTD_DEBUG="${SLURMRESTD_DEBUG:-info}" \
        slurmrestd -a rest_auth/jwt -d "$api_version" -s slurmctld \
            -t "$threads" "$listen" &
    child_pids+=("$!")
}

run_token_issuer() {
    [[ $profile == simulation ]] ||
        die 'the built-in token issuer is forbidden outside the simulation profile'
    prepare_slurm_config
    prepare_munge
    wait_for_port "${SLURM_CONTROLLER_HOST:-controller}" 6817 slurmctld

    # This helper never receives the JWT signing key.  It authenticates to
    # slurmctld with MUNGE as SlurmUser and atomically publishes only a
    # short-lived token for the non-administrative workflow identity.
    log 'starting development-only short-lived token issuer'
    /usr/local/sbin/dev-token-renewer &
    child_pids+=("$!")
}

run_broker() {
    local source=${BROKER_API_TOKEN_FILE:-}
    local staged_token=/run/workflow/api.token

    # The production Quadlet starts `python3 -m workflow` directly as UID
    # 64032, with secret-manager files already owned for that identity.  The
    # root staging path below exists only because rootless Compose bind mounts
    # a host-owned development token as container root.
    [[ $profile == simulation ]] ||
        die 'production broker must start directly as uid 64032, not via the root role entrypoint'
    [[ -n $source ]] || die 'BROKER_API_TOKEN_FILE is required for the simulation broker'
    validate_input_file "$source" 'workflow broker API token' secret

    install -d -o workflow -g workflow -m 0700 /run/workflow
    stage_file "$source" "$staged_token" workflow workflow 0400
    install -d -o workflow -g workflow -m 0750 /var/lib/workflow

    # Only the trusted demo application identity can traverse workflow state.
    # User-supplied jobs are not permitted under this shared service UID.
    install -d -o workflow -g workflow -m 0750 /shared/jobs /shared/output \
        /shared/workflows
    install -d -o workflow -g workflow -m 0700 \
        /shared/.workflow-control /shared/.workflow-control/capabilities

    cd /opt/slurm-workflow
    log 'starting workflow broker as uid 64032 (workflow)'
    runuser --user workflow -- env \
        PYTHONDONTWRITEBYTECODE=1 \
        BROKER_API_TOKEN_FILE="$staged_token" \
        python3 -m workflow &
    child_pids+=("$!")
}

wait_for_essential_process() {
    local status
    if wait -n "${child_pids[@]}"; then
        status=0
    else
        status=$?
    fi
    log "an essential daemon exited with status $status"
    exit "$status"
}

require_root_supervisor
case "$profile" in
    simulation|production) ;;
    *) usage_error 'SLURM_PROFILE must be simulation or production' ;;
esac
case "$role" in
    database|slurmdbd|controller|compute|restd|token-issuer|broker) ;;
    *) usage_error 'unsupported CONTAINER_ROLE' ;;
esac

# Never let an inherited bearer token make administrative CLI calls silently
# switch from MUNGE to JWT. The REST child receives only the documented daemon
# sentinel in its own environment.
unset SLURM_JWT
umask 077
prepare_runtime_directories

case "$role" in
    database) run_database ;;
    slurmdbd) run_slurmdbd ;;
    controller) run_controller ;;
    compute) run_compute ;;
    restd) run_restd ;;
    token-issuer) run_token_issuer ;;
    broker) run_broker ;;
esac

wait_for_essential_process
