#!/usr/bin/env bash
# Install the verified runtime subset from a build-rpms.sh artifact directory.
set -Eeuo pipefail
umask 022

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd -- "$script_dir/.." && pwd)
# shellcheck source=lib/build-common.sh
source "$script_dir/lib/build-common.sh"

versions_file="$project_dir/build/versions.conf"
package_dir=/var/packages
integration_database=1

usage() {
    cat <<'EOF'
Usage: scripts/install-runtime.sh [OPTIONS]

Verify and install the required MUNGE and Slurm runtime RPMs.

Options:
  --versions-file FILE              Repository trust/digest defaults
  --packages-dir DIRECTORY          Artifact directory (default: /var/packages)
  --with-integration-database       Install MariaDB server (default)
  --without-integration-database    Omit the local-only database server
  -h, --help                        Show this help
EOF
}

while (($#)); do
    case $1 in
        --versions-file) (($# >= 2)) || build_die "$1 requires a value"; versions_file=$2; shift 2 ;;
        --packages-dir) (($# >= 2)) || build_die "$1 requires a value"; package_dir=$2; shift 2 ;;
        --with-integration-database) integration_database=1; shift ;;
        --without-integration-database) integration_database=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) build_die "unknown option: $1" ;;
    esac
done

versions_file=$(realpath -e -- "$versions_file") || build_die 'versions file does not resolve'
package_dir=$(realpath -e -- "$package_dir") || build_die 'package directory does not resolve'
build_load_versions "$versions_file"
build_require_root
build_require_el10
build_require_commands dnf rpm sha256sum find sort realpath ldd
build_verify_manifest "$package_dir"
build_verify_manifest_sources "$package_dir"

declare -a wanted_rpms=(
    munge
    munge-libs
    slurm
    slurm-slurmctld
    slurm-slurmd
    slurm-slurmdbd
    slurm-slurmrestd
)
declare -A selected_by_name=()

while IFS= read -r -d '' rpm_path; do
    rpm_name=$(rpm -qp --queryformat '%{NAME}' "$rpm_path") ||
        build_die "could not inspect RPM: $rpm_path"
    for wanted in "${wanted_rpms[@]}"; do
        [[ $rpm_name == "$wanted" ]] || continue
        [[ -z ${selected_by_name[$wanted]+present} ]] ||
            build_die "multiple RPMs provide required package $wanted"
        selected_by_name[$wanted]=$rpm_path
    done
done < <(find "$package_dir/RPMS" -type f -name '*.rpm' -print0)

declare -a runtime_rpms=()
for wanted in "${wanted_rpms[@]}"; do
    [[ -n ${selected_by_name[$wanted]:-} ]] ||
        build_die "required runtime RPM is missing: $wanted"
    runtime_rpms+=("${selected_by_name[$wanted]}")
done

build_prepare_repositories
build_set_stream_repository_args
declare -a runtime_dependencies=(
    bash
    ca-certificates
    curl
    hostname
    iproute
    openssl
    procps-ng
    python3
    python3-PyMySQL
    shadow-utils
    util-linux
)
((integration_database == 0)) || runtime_dependencies+=(mariadb-server)

dnf -y "${STREAM_REPOSITORY_ARGS[@]}" install \
    "${runtime_rpms[@]}" "${runtime_dependencies[@]}"
dnf clean all
rm -rf -- /var/cache/dnf

for executable in slurmctld slurmd slurmdbd slurmrestd munged; do
    command -v "$executable" >/dev/null 2>&1 ||
        build_die "installed runtime is missing executable: $executable"
done

for plugin in auth_jwt rest_auth_jwt data_parser_v0_0_45 cgroup_v2 \
        namespace_tmpfs tls_s2n accounting_storage_mysql; do
    plugin_path="/usr/lib64/slurm/$plugin.so"
    [[ -f $plugin_path ]] || build_die "installed runtime is missing plugin: $plugin"
    ! ldd "$plugin_path" | grep -q 'not found' ||
        build_die "installed plugin has an unresolved dependency: $plugin"
done

build_log "installed verified runtime packages from $package_dir"
