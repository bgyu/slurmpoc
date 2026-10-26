#!/usr/bin/env bash
# Build reproducible MUNGE and Slurm RPM/SRPM artifacts on EL10.
set -Eeuo pipefail
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd -- "$script_dir/.." && pwd)
# shellcheck source=lib/build-common.sh
source "$script_dir/lib/build-common.sh"

versions_file="$project_dir/build/versions.conf"
output_dir=/var/packages
work_dir=/var/tmp/slurm-rpm-build
slurm_version_override=
slurm_sha_override=
munge_version_override=
munge_sha_override=

usage() {
    cat <<'EOF'
Usage: scripts/build-rpms.sh [OPTIONS]

Build MUNGE and Slurm RPMs from verified upstream sources.

Options:
  --versions-file FILE       Pinned defaults file
  --output-dir DIRECTORY     Artifact directory (default: /var/packages)
  --work-dir DIRECTORY       Temporary build parent
  --slurm-version VERSION    Override together with --slurm-sha256
  --slurm-sha256 DIGEST      Override together with --slurm-version
  --munge-version VERSION    Override together with --munge-sha256
  --munge-sha256 DIGEST      Override together with --munge-version
  -h, --help                 Show this help
EOF
}

while (($#)); do
    case $1 in
        --versions-file) (($# >= 2)) || build_die "$1 requires a value"; versions_file=$2; shift 2 ;;
        --output-dir) (($# >= 2)) || build_die "$1 requires a value"; output_dir=$2; shift 2 ;;
        --work-dir) (($# >= 2)) || build_die "$1 requires a value"; work_dir=$2; shift 2 ;;
        --slurm-version) (($# >= 2)) || build_die "$1 requires a value"; slurm_version_override=$2; shift 2 ;;
        --slurm-sha256) (($# >= 2)) || build_die "$1 requires a value"; slurm_sha_override=$2; shift 2 ;;
        --munge-version) (($# >= 2)) || build_die "$1 requires a value"; munge_version_override=$2; shift 2 ;;
        --munge-sha256) (($# >= 2)) || build_die "$1 requires a value"; munge_sha_override=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) build_die "unknown option: $1" ;;
    esac
done

versions_file=$(realpath -e -- "$versions_file") || build_die 'versions file does not resolve'
build_load_versions "$versions_file"
build_validate_absolute_path "$output_dir" 'output directory'
build_validate_absolute_path "$work_dir" 'work directory'

if [[ -n $slurm_version_override || -n $slurm_sha_override ]]; then
    [[ -n $slurm_version_override && -n $slurm_sha_override ]] ||
        build_die 'override Slurm version and SHA-256 together'
    build_validate_version "$slurm_version_override" SLURM_VERSION
    build_validate_sha256 "$slurm_sha_override" SLURM_SHA256
    SLURM_VERSION=$slurm_version_override
    SLURM_SHA256=$slurm_sha_override
fi
if [[ -n $munge_version_override || -n $munge_sha_override ]]; then
    [[ -n $munge_version_override && -n $munge_sha_override ]] ||
        build_die 'override MUNGE version and SHA-256 together'
    build_validate_version "$munge_version_override" MUNGE_VERSION
    build_validate_sha256 "$munge_sha_override" MUNGE_SHA256
    MUNGE_VERSION=$munge_version_override
    MUNGE_SHA256=$munge_sha_override
fi

build_require_root
build_require_el10
build_require_commands dnf rpm sha256sum find sort xargs mktemp realpath

# A verified matching artifact set makes a repeated invocation a no-op. Never
# merge a new build into an unknown directory: stale RPMs would make provenance
# and later package selection ambiguous.
output_preexisting=0
if [[ -e $output_dir ]]; then
    output_preexisting=1
    [[ -d $output_dir && ! -L $output_dir ]] ||
        build_die "output path exists but is not a real directory: $output_dir"
    if [[ -f $output_dir/BUILD-MANIFEST && ! -L $output_dir/BUILD-MANIFEST ]]; then
        grep -Fxq "MUNGE_VERSION=$MUNGE_VERSION" "$output_dir/BUILD-MANIFEST" &&
            grep -Fxq "MUNGE_SHA256=$MUNGE_SHA256" "$output_dir/BUILD-MANIFEST" &&
            grep -Fxq "SLURM_VERSION=$SLURM_VERSION" "$output_dir/BUILD-MANIFEST" &&
            grep -Fxq "SLURM_SHA256=$SLURM_SHA256" "$output_dir/BUILD-MANIFEST" ||
            build_die 'existing package directory was built from different sources'
        build_verify_manifest "$output_dir"
        build_verify_manifest_sources "$output_dir"
        build_log "verified existing package set at $output_dir"
        exit 0
    fi
    [[ -z $(find "$output_dir" -mindepth 1 -maxdepth 1 -print -quit) ]] ||
        build_die 'output directory is non-empty and has no reusable manifest'
fi

install -d -m 0700 "$work_dir"
run_dir=$(mktemp -d "$work_dir/run.XXXXXXXX")
output_parent=${output_dir%/*}
[[ -n $output_parent ]] || output_parent=/
install -d -m 0755 "$output_parent"
stage_dir=$(mktemp -d "$output_parent/.slurm-packages.XXXXXXXX")

cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n $run_dir ]]; then
        [[ ! -e $run_dir || $run_dir == "$work_dir"/run.* ]] && rm -rf -- "$run_dir"
    fi
    if [[ -n $stage_dir ]]; then
        [[ ! -e $stage_dir || $stage_dir == "$output_parent"/.slurm-packages.* ]] &&
            rm -rf -- "$stage_dir"
    fi
    exit "$status"
}
trap cleanup EXIT

source_dir="$run_dir/sources"
top_dir="$run_dir/rpmbuild"
install -d -m 0700 "$source_dir" "$top_dir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}

build_prepare_repositories
build_set_stream_repository_args
dnf -y install rpm-build rpmdevtools tar xz bzip2 gzip procps-ng

munge_archive="$source_dir/munge-$MUNGE_VERSION.tar.xz"
build_download_verified \
    "https://github.com/dun/munge/releases/download/munge-$MUNGE_VERSION/munge-$MUNGE_VERSION.tar.xz" \
    "$munge_archive" "$MUNGE_SHA256" 'MUNGE source archive'
tar -xOf "$munge_archive" "munge-$MUNGE_VERSION/munge.spec" > "$run_dir/munge.spec"
dnf builddep -y "$run_dir/munge.spec"
rpmbuild --define "_topdir $top_dir" -ta --with check "$munge_archive"
mapfile -d '' -t munge_build_rpms < <(
    find "$top_dir/RPMS" -type f -name 'munge-*.rpm' -print0
)
((${#munge_build_rpms[@]})) || build_die 'MUNGE build produced no RPMs'
dnf -y install "${munge_build_rpms[@]}"

slurm_archive="$source_dir/slurm-$SLURM_VERSION.tar.bz2"
build_download_verified \
    "https://download.schedmd.com/slurm/slurm-$SLURM_VERSION.tar.bz2" \
    "$slurm_archive" "$SLURM_SHA256" 'Slurm source archive'
tar -xOf "$slurm_archive" "slurm-$SLURM_VERSION/slurm.spec" > "$run_dir/slurm.spec"

# Current Stream libbpf-devel requires a newer header-only package than the
# UBI GA build. Restrict this replacement to the disposable builder.
dnf -y "${STREAM_REPOSITORY_ARGS[@]}" --disablerepo='*' \
    --enablerepo=stream-appstream upgrade kernel-headers
dnf -y "${STREAM_REPOSITORY_ARGS[@]}" --allowerasing install \
    dbus-devel json-c-devel kernel-headers libbpf-devel libjwt-devel \
    libyaml-devel llhttp-devel mariadb-connector-c-devel s2n-tls-devel
dnf -y "${STREAM_REPOSITORY_ARGS[@]}" builddep "$run_dir/slurm.spec"
rpmbuild --define "_topdir $top_dir" -ta --with cgroupv2 --with jwt \
    --with slurmrestd --with yaml "$slurm_archive"
find "$top_dir/RPMS" -type f \
    -name "slurm-slurmrestd-$SLURM_VERSION-*.rpm" -print -quit | grep -q . ||
    build_die 'Slurm REST RPM was not produced'

install -d -m 0755 "$stage_dir/RPMS" "$stage_dir/SRPMS"
find "$top_dir/RPMS" -type f -name '*.rpm' -exec cp -p {} "$stage_dir/RPMS/" \;
find "$top_dir/SRPMS" -type f -name '*.rpm' -exec cp -p {} "$stage_dir/SRPMS/" \;

manifest_temporary="$stage_dir/.BUILD-MANIFEST.new"
{
    printf 'MUNGE_VERSION=%s\nMUNGE_SHA256=%s\n' "$MUNGE_VERSION" "$MUNGE_SHA256"
    printf 'SLURM_VERSION=%s\nSLURM_SHA256=%s\n' "$SLURM_VERSION" "$SLURM_SHA256"
    (
        cd -- "$stage_dir"
        find RPMS SRPMS -type f -name '*.rpm' -print0 |
            sort -z | xargs -0 sha256sum
    )
} > "$manifest_temporary"
chmod 0644 "$manifest_temporary"
mv -- "$manifest_temporary" "$stage_dir/BUILD-MANIFEST"
build_verify_manifest "$stage_dir"
build_verify_manifest_sources "$stage_dir"

if ((output_preexisting == 1)); then
    [[ -z $(find "$output_dir" -mindepth 1 -maxdepth 1 -print -quit) ]] ||
        build_die 'output directory became non-empty during build'
    # A bind mount cannot be replaced with rename(2). Publish its two artifact
    # directories first and the manifest last; absence of the manifest makes a
    # host interruption fail closed on the next invocation.
    mv -- "$stage_dir/RPMS" "$output_dir/RPMS"
    mv -- "$stage_dir/SRPMS" "$output_dir/SRPMS"
    mv -- "$stage_dir/BUILD-MANIFEST" "$output_dir/BUILD-MANIFEST"
    rmdir -- "$stage_dir"
else
    mv -- "$stage_dir" "$output_dir"
fi
stage_dir=
chmod 0755 "$output_dir" "$output_dir/RPMS" "$output_dir/SRPMS"
build_log "built verified packages in $output_dir"
