#!/usr/bin/env bash
# Generate secrets only for the self-contained integration profile.
#
# Production deployments MUST obtain these values from a managed secret
# service and mount them as files.  Keeping this helper separate from
# cluster-up.sh makes the development-only trust bootstrap visible and keeps
# secret values out of Compose interpolation and process environments.
set -Eeuo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runtime_dir="$project_dir/.runtime"
secret_dir="$runtime_dir/secrets"

umask 077
install -d -m 0700 "$runtime_dir" "$secret_dir"

die() {
    printf 'bootstrap-dev-secrets: %s\n' "$*" >&2
    exit 1
}

create_random_file() {
    local destination=$1 byte_count=$2 encoding=${3:-raw}
    local temporary

    # Refuse symlinks even in a development directory.  Otherwise an attacker
    # with write access to the workspace could redirect secret creation into a
    # different file owned by the invoking user.
    [[ ! -L $destination ]] || die "refusing symbolic-link target: $destination"
    [[ ! -e $destination ]] || return 0

    temporary=$(mktemp --tmpdir="$secret_dir" '.secret.XXXXXXXX')
    if [[ $encoding == hex ]]; then
        od -An -N"$byte_count" -tx1 /dev/urandom | tr -d ' \n' >"$temporary"
        printf '\n' >>"$temporary"
    else
        dd if=/dev/urandom of="$temporary" bs="$byte_count" count=1 status=none
    fi
    chmod 0600 "$temporary"
    mv -n -- "$temporary" "$destination"
    if [[ -e $temporary ]]; then
        rm -f -- "$temporary"
    fi
}

# Migrate the original proof-of-concept layout before creating new values.
# `mv` keeps the existing cluster identity while removing the duplicate key
# from the old, less structured location.  Nothing outside .runtime is ever a
# migration target.
if [[ -s $runtime_dir/munge.key && ! -e $secret_dir/munge.key ]]; then
    [[ ! -L $runtime_dir/munge.key ]] || die 'legacy MUNGE key is a symlink'
    mv -- "$runtime_dir/munge.key" "$secret_dir/munge.key"
    chmod 0600 "$secret_dir/munge.key"
fi

# An earlier version placed the MariaDB password in cluster.env.  Read it
# without evaluating the file, migrate it to a secret file, then atomically
# rewrite the env file with only non-secret Compose settings.
if [[ -f $runtime_dir/cluster.env ]] && \
        grep -q '^MARIADB_SLURM_PASSWORD=' "$runtime_dir/cluster.env"; then
    legacy_password=$(sed -n 's/^MARIADB_SLURM_PASSWORD=//p' \
        "$runtime_dir/cluster.env" | head -n1)
    [[ $legacy_password =~ ^[[:alnum:]_@%+=.,:-]{16,128}$ ]] || \
        die 'legacy MariaDB password has an unsafe format'
    if [[ ! -e $secret_dir/mariadb-slurm.password ]]; then
        printf '%s\n' "$legacy_password" >"$secret_dir/mariadb-slurm.password"
        chmod 0600 "$secret_dir/mariadb-slurm.password"
    fi
    unset legacy_password

    filtered_env=$(mktemp --tmpdir="$runtime_dir" '.cluster-env.XXXXXXXX')
    grep -v '^MARIADB_SLURM_PASSWORD=' "$runtime_dir/cluster.env" \
        >"$filtered_env" || true
    chmod 0600 "$filtered_env"
    mv -- "$filtered_env" "$runtime_dir/cluster.env"
fi

# Earlier revisions used SLURM_PROFILE for the deployment selector. Slurm's
# own CLI reserves that variable for job profiling, so migrate it out of the
# SLURM_* namespace without changing any user-selected non-secret settings.
if [[ -f $runtime_dir/cluster.env ]] && \
        grep -Eq '^(SLURM_PROFILE|SLURM_DEPLOYMENT_PROFILE)=' \
            "$runtime_dir/cluster.env"; then
    migrated_env=$(mktemp --tmpdir="$runtime_dir" '.cluster-env.XXXXXXXX')
    grep -Ev '^(SLURM_PROFILE|SLURM_DEPLOYMENT_PROFILE|CLUSTER_DEPLOYMENT_PROFILE)=' \
        "$runtime_dir/cluster.env" >"$migrated_env" || true
    printf 'CLUSTER_DEPLOYMENT_PROFILE=simulation\n' >>"$migrated_env"
    chmod 0600 "$migrated_env"
    mv -- "$migrated_env" "$runtime_dir/cluster.env"
fi

# MUNGE recommends a high-entropy key.  It is shared by every Slurm daemon but
# never by MariaDB-only, gateway-only, or client processes.
create_random_file "$secret_dir/munge.key" 1024 raw

# Slurm's HS256 development mode requires at least 256 bits.  The raw key is
# mounted only on slurmctld and slurmdbd.  Production uses external RS256/JWKS
# and does not create this file on cluster hosts.
create_random_file "$secret_dir/slurm-jwt-hs256.key" 32 raw

# Hex avoids quoting ambiguity when the local MariaDB bootstrap client reads
# the password.  The value is still passed by protected file, never by argv or
# environment.
create_random_file "$secret_dir/mariadb-slurm.password" 32 hex

# This authenticates the host-side demo client to the workflow broker.  It is
# not a Slurm JWT and grants no direct Slurm API access.
create_random_file "$secret_dir/broker-api.token" 32 hex

find "$secret_dir" -maxdepth 1 -type f -exec chmod 0600 {} +

# Non-secret settings can safely use an env file for Compose interpolation.
# Keep the file deterministic and free of passwords/tokens by construction.
if [[ ! -e $runtime_dir/cluster.env ]]; then
    temporary=$(mktemp --tmpdir="$runtime_dir" '.cluster-env.XXXXXXXX')
    {
        printf 'SLURM_IMAGE_TAG=latest\n'
        printf 'CLUSTER_DEPLOYMENT_PROFILE=simulation\n'
        printf 'SLURM_API_VERSION=v0.0.45\n'
    } >"$temporary"
    chmod 0600 "$temporary"
    mv -n -- "$temporary" "$runtime_dir/cluster.env"
    if [[ -e $temporary ]]; then
        rm -f -- "$temporary"
    fi
fi

printf 'Development secrets are ready in %s (values were not printed).\n' \
    "$secret_dir"
