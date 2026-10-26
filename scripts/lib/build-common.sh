#!/usr/bin/env bash
# Shared validation and signed-repository helpers for package build/install.
# This file is sourced by public commands and intentionally performs no action
# at load time.

build_log() {
    printf '[slurm-build] %s\n' "$*" >&2
}

build_die() {
    build_log "ERROR: $*"
    exit 1
}

build_require_root() {
    ((EUID == 0)) || build_die 'this command must run as root'
}

build_require_el10() {
    local platform=
    [[ -r /etc/os-release ]] || build_die '/etc/os-release is unavailable'
    while IFS='=' read -r key value; do
        [[ $key == PLATFORM_ID ]] || continue
        value=${value%\"}
        value=${value#\"}
        platform=$value
    done < /etc/os-release
    [[ $platform == platform:el10 ]] ||
        build_die "this command requires a UBI/RHEL-compatible EL10 environment (found ${platform:-unknown})"
}

build_require_commands() {
    local command_name
    for command_name in "$@"; do
        command -v "$command_name" >/dev/null 2>&1 ||
            build_die "$command_name is required"
    done
}

build_validate_sha256() {
    [[ $1 =~ ^[0-9a-f]{64}$ ]] || build_die "$2 must be 64 lowercase hex characters"
}

build_validate_version() {
    [[ $1 =~ ^[0-9]+([.][0-9]+){1,3}([_-][A-Za-z0-9.]+)?$ ]] ||
        build_die "$2 contains unsupported characters"
}

build_validate_absolute_path() {
    local path=$1 description=$2
    [[ $path == /* && $path != / && $path != *$'\n'* && $path != *$'\r'* ]] ||
        build_die "$description must be an absolute path other than /"
}

build_validate_input_directory() {
    local path=$1 description=$2
    build_validate_absolute_path "$path" "$description"
    [[ -d $path && ! -L $path ]] ||
        build_die "$description must be a real directory: $path"
}

build_validate_input_file() {
    local path=$1 description=$2
    build_validate_absolute_path "$path" "$description"
    [[ -f $path && ! -L $path && -r $path ]] ||
        build_die "$description must be a readable, non-symlink regular file: $path"
}

# Parse the repository version file as a deliberately tiny data grammar. Using
# `source` here would allow a compromised configuration file to execute with
# the root privileges required by the package manager.
build_load_versions() {
    local file=$1 line key value required
    local -A seen=()
    build_validate_input_file "$file" 'versions file'

    unset FORMAT_VERSION SLURM_VERSION SLURM_SHA256 MUNGE_VERSION MUNGE_SHA256 \
        EPEL_RELEASE_SHA256 CENTOS_GPG_KEY_SHA256
    while IFS= read -r line || [[ -n $line ]]; do
        [[ $line =~ ^[[:space:]]*$ || $line =~ ^[[:space:]]*# ]] && continue
        [[ $line =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]+)$ ]] ||
            build_die "malformed line in versions file: $line"
        key=${BASH_REMATCH[1]}
        value=${BASH_REMATCH[2]}
        case $key in
            FORMAT_VERSION|SLURM_VERSION|SLURM_SHA256|MUNGE_VERSION|MUNGE_SHA256|\
            EPEL_RELEASE_SHA256|CENTOS_GPG_KEY_SHA256) ;;
            *) build_die "unknown versions-file key: $key" ;;
        esac
        [[ -z ${seen[$key]+present} ]] ||
            build_die "duplicate versions-file key: $key"
        seen[$key]=1
        printf -v "$key" '%s' "$value"
        declare -g "$key"
    done < "$file"

    for required in FORMAT_VERSION SLURM_VERSION SLURM_SHA256 MUNGE_VERSION \
        MUNGE_SHA256 EPEL_RELEASE_SHA256 CENTOS_GPG_KEY_SHA256; do
        [[ -n ${!required:-} ]] || build_die "missing versions-file key: $required"
    done
    [[ $FORMAT_VERSION == 1 ]] || build_die 'unsupported versions-file format'
    build_validate_version "$SLURM_VERSION" SLURM_VERSION
    build_validate_sha256 "$SLURM_SHA256" SLURM_SHA256
    build_validate_version "$MUNGE_VERSION" MUNGE_VERSION
    build_validate_sha256 "$MUNGE_SHA256" MUNGE_SHA256
    build_validate_sha256 "$EPEL_RELEASE_SHA256" EPEL_RELEASE_SHA256
    build_validate_sha256 "$CENTOS_GPG_KEY_SHA256" CENTOS_GPG_KEY_SHA256
}

build_download_verified() {
    local url=$1 destination=$2 digest=$3 description=$4 temporary
    build_validate_absolute_path "$destination" "$description destination"
    build_validate_sha256 "$digest" "$description digest"
    [[ ! -L $destination ]] || build_die "$description destination is a symbolic link"
    if [[ -f $destination ]]; then
        printf '%s  %s\n' "$digest" "$destination" |
            sha256sum --check --strict - >/dev/null ||
            build_die "existing $description failed checksum validation: $destination"
        return 0
    fi
    temporary=$(mktemp "${destination}.download.XXXXXXXX")
    if ! curl --fail --location --retry 5 --output "$temporary" "$url"; then
        rm -f -- "$temporary"
        build_die "failed to download $description"
    fi
    if ! printf '%s  %s\n' "$digest" "$temporary" |
            sha256sum --check --strict - >/dev/null; then
        rm -f -- "$temporary"
        build_die "$description checksum validation failed"
    fi
    chmod 0600 "$temporary"
    mv -- "$temporary" "$destination"
}

# Install only public repository metadata and signing trust. Source archives,
# credentials, and application configuration never pass through this helper.
build_prepare_repositories() {
    local temporary epel key
    dnf -y install dnf-plugins-core curl ca-certificates
    temporary=$(mktemp -d /tmp/slurm-repositories.XXXXXXXX)
    epel="$temporary/epel-release.noarch.rpm"
    key="$temporary/RPM-GPG-KEY-CentOS-Official"
    build_download_verified \
        'https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm' \
        "$epel" "$EPEL_RELEASE_SHA256" 'EPEL release package'
    dnf -y install "$epel"
    build_download_verified \
        'https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official-SHA256' \
        "$key" "$CENTOS_GPG_KEY_SHA256" 'CentOS signing key'
    rpm --import "$key"
    rm -rf -- "$temporary"
}

# Callers expand this array into DNF. Repositories remain command-scoped, so a
# future unrelated install cannot silently change package provenance.
build_set_stream_repository_args() {
    STREAM_REPOSITORY_ARGS=(
        '--repofrompath=stream-baseos,https://mirror.stream.centos.org/10-stream/BaseOS/$basearch/os/'
        '--repofrompath=stream-appstream,https://mirror.stream.centos.org/10-stream/AppStream/$basearch/os/'
        '--repofrompath=stream-crb,https://mirror.stream.centos.org/10-stream/CRB/$basearch/os/'
        '--setopt=stream-baseos.gpgcheck=1'
        '--setopt=stream-appstream.gpgcheck=1'
        '--setopt=stream-crb.gpgcheck=1'
        '--setopt=stream-baseos.priority=200'
        '--setopt=stream-appstream.priority=200'
        '--setopt=stream-crb.priority=200'
    )
}

build_verify_manifest() {
    local package_dir=$1 manifest line_count recorded actual
    build_validate_input_directory "$package_dir" 'package directory'
    manifest="$package_dir/BUILD-MANIFEST"
    build_validate_input_file "$manifest" 'package manifest'
    line_count=$(wc -l < "$manifest")
    ((line_count >= 6)) || build_die 'package manifest is incomplete'
    sed -n '1p' "$manifest" | grep -Eq '^MUNGE_VERSION=[0-9A-Za-z._-]+$' ||
        build_die 'package manifest has an invalid MUNGE version header'
    sed -n '2p' "$manifest" | grep -Eq '^MUNGE_SHA256=[0-9a-f]{64}$' ||
        build_die 'package manifest has an invalid MUNGE digest header'
    sed -n '3p' "$manifest" | grep -Eq '^SLURM_VERSION=[0-9A-Za-z._-]+$' ||
        build_die 'package manifest has an invalid Slurm version header'
    sed -n '4p' "$manifest" | grep -Eq '^SLURM_SHA256=[0-9a-f]{64}$' ||
        build_die 'package manifest has an invalid Slurm digest header'
    tail -n +5 "$manifest" |
        grep -Eqv '^[0-9a-f]{64}  (RPMS|SRPMS)/[A-Za-z0-9+_.~-]+[.]rpm$' &&
        build_die 'package manifest contains an unsafe artifact record'
    [[ -z $(find "$package_dir/RPMS" "$package_dir/SRPMS" -type l -print -quit) ]] ||
        build_die 'package directory contains a symbolic link'
    recorded=$(tail -n +5 "$manifest" | cut -d' ' -f3- | sort)
    actual=$(
        cd -- "$package_dir"
        find RPMS SRPMS -type f -name '*.rpm' -print | sort
    )
    [[ $recorded == "$actual" ]] ||
        build_die 'package manifest does not describe exactly the available RPMs'
    (
        cd -- "$package_dir"
        tail -n +5 BUILD-MANIFEST | sha256sum --check --strict
    ) || build_die 'package manifest verification failed'
}

# Hash verification proves that artifacts have not changed since the manifest
# was written. This separate pin check proves that the manifest itself names
# the source releases approved by this checkout. Keeping both checks avoids a
# valid-but-unapproved package set being installed after a directory swap.
build_verify_manifest_sources() {
    local package_dir=$1 manifest key
    build_validate_input_directory "$package_dir" 'package directory'
    manifest="$package_dir/BUILD-MANIFEST"
    build_validate_input_file "$manifest" 'package manifest'

    for key in MUNGE_VERSION MUNGE_SHA256 SLURM_VERSION SLURM_SHA256; do
        [[ -n ${!key:-} ]] ||
            build_die "expected source pin is unset: $key"
        grep -Fxq "$key=${!key}" "$manifest" ||
            build_die "package manifest does not match approved source pin: $key"
    done
}
