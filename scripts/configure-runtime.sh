#!/usr/bin/env bash
# Install immutable runtime configuration, identities, and application assets.
# Secret creation and daemon startup are intentionally outside this command.
set -Eeuo pipefail
umask 022

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd -- "$script_dir/.." && pwd)
# shellcheck source=lib/build-common.sh
source "$script_dir/lib/build-common.sh"

source_root=$project_dir
integration_database=1

usage() {
    cat <<'EOF'
Usage: scripts/configure-runtime.sh [OPTIONS]

Install users, directories, configurations, helpers, and workflow assets.

Options:
  --source-root DIRECTORY            Repository/runtime asset root
  --with-integration-database        Require the local mysql identity (default)
  --without-integration-database     Configure only external-database roles
  -h, --help                         Show this help
EOF
}

while (($#)); do
    case $1 in
        --source-root) (($# >= 2)) || build_die "$1 requires a value"; source_root=$2; shift 2 ;;
        --with-integration-database) integration_database=1; shift ;;
        --without-integration-database) integration_database=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) build_die "unknown option: $1" ;;
    esac
done

source_root=$(realpath -e -- "$source_root") || build_die 'source root does not resolve'
build_validate_input_directory "$source_root" 'source root'
build_require_root
build_require_el10
build_require_commands getent groupadd useradd groupmod usermod install find readlink realpath

asset() {
    local relative=$1
    build_validate_input_file "$source_root/$relative" "source asset $relative"
    printf '%s\n' "$source_root/$relative"
}

ensure_group() {
    local name=$1 gid=$2 current collision
    current=$(getent group "$name" || true)
    if [[ -n $current ]]; then
        [[ $(cut -d: -f3 <<<"$current") == "$gid" ]] ||
            build_die "group $name exists with an unexpected GID"
        return
    fi
    collision=$(getent group "$gid" || true)
    [[ -z $collision ]] || build_die "GID $gid is already assigned to another group"
    groupadd --system --gid "$gid" "$name"
}

ensure_user() {
    local name=$1 uid=$2 group=$3 home=$4 comment=$5 current collision shell
    current=$(getent passwd "$name" || true)
    if [[ -n $current ]]; then
        [[ $(cut -d: -f3 <<<"$current") == "$uid" ]] ||
            build_die "user $name exists with an unexpected UID"
        [[ $(cut -d: -f4 <<<"$current") == "$(getent group "$group" | cut -d: -f3)" ]] ||
            build_die "user $name has an unexpected primary group"
        [[ $(cut -d: -f6 <<<"$current") == "$home" ]] ||
            build_die "user $name has an unexpected home directory"
        shell=$(cut -d: -f7 <<<"$current")
        [[ $shell == /sbin/nologin || $shell == /usr/sbin/nologin ]] ||
            build_die "user $name has an unexpected login shell"
        return
    fi
    collision=$(getent passwd "$uid" || true)
    [[ -z $collision ]] || build_die "UID $uid is already assigned to another user"
    useradd --system --uid "$uid" --gid "$group" --home-dir "$home" \
        --shell /sbin/nologin --comment "$comment" "$name"
}

normalize_munge_identity() {
    local current_uid current_gid collision passwd_record shell
    getent passwd munge >/dev/null || build_die 'MUNGE RPM did not create user munge'
    getent group munge >/dev/null || build_die 'MUNGE RPM did not create group munge'
    current_gid=$(getent group munge | cut -d: -f3)
    if [[ $current_gid != 64029 ]]; then
        collision=$(getent group 64029 || true)
        [[ -z $collision ]] || build_die 'GID 64029 is already assigned'
        groupmod --gid 64029 munge
    fi
    current_uid=$(getent passwd munge | cut -d: -f3)
    if [[ $current_uid != 64029 ]]; then
        collision=$(getent passwd 64029 || true)
        [[ -z $collision ]] || build_die 'UID 64029 is already assigned'
        usermod --uid 64029 --gid 64029 --home /var/lib/munge munge
    fi
    passwd_record=$(getent passwd munge)
    [[ $(cut -d: -f4 <<<"$passwd_record") == 64029 ]] ||
        build_die 'user munge has an unexpected primary group'
    [[ $(cut -d: -f6 <<<"$passwd_record") == /var/lib/munge ]] ||
        build_die 'user munge has an unexpected home directory'
    # UBI's MUNGE package records /usr/sbin/nologin while locally created
    # identities use the equivalent /sbin/nologin compatibility path.
    shell=$(cut -d: -f7 <<<"$passwd_record")
    [[ $shell == /sbin/nologin || $shell == /usr/sbin/nologin ]] ||
        build_die 'user munge has an unexpected login shell'
}

install_asset() {
    local relative=$1 destination=$2 owner=$3 group=$4 mode=$5 source
    source=$(asset "$relative")
    install -o "$owner" -g "$group" -m "$mode" -- "$source" "$destination"
}

install_python_directory() {
    local relative=$1 destination=$2 source file
    source="$source_root/$relative"
    build_validate_input_directory "$source" "source asset directory $relative"
    [[ -z $(find "$source" -maxdepth 1 -type l -print -quit) ]] ||
        build_die "source asset directory contains a symbolic link: $relative"
    install -d -o root -g root -m 0755 "$destination"
    while IFS= read -r -d '' file; do
        install -o root -g root -m 0644 -- "$file" "$destination/${file##*/}"
    done < <(find "$source" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sbatch' \) -print0)
}

install_compatibility_symlink() {
    local target=$1 link=$2
    if [[ -L $link ]]; then
        [[ $(readlink -- "$link") == "$target" ]] ||
            build_die "existing symlink has an unexpected target: $link"
        return
    fi
    [[ ! -e $link ]] || build_die "refusing to replace existing file: $link"
    ln -s -- "$target" "$link"
}

normalize_munge_identity
ensure_group slurm 64030
ensure_user slurm 64030 slurm /var/lib/slurm 'Slurm workload manager'
ensure_group slurmrestd 64031
ensure_user slurmrestd 64031 slurmrestd /var/lib/slurmrestd 'Slurm REST daemon'
ensure_group workflow 64032
ensure_user workflow 64032 workflow /var/lib/workflow 'Workflow service identity'

chown -R munge:munge /etc/munge /var/lib/munge /var/log/munge
install -d -o root -g slurm -m 0755 /etc/slurm/profiles \
    /etc/slurm/profiles/simulation /etc/slurm/profiles/production \
    /run/slurm /run/slurm/config
install -d -o slurm -g slurm -m 0755 /run/slurm/pids
install -d -o munge -g munge -m 0700 /var/lib/munge
install -d -o munge -g munge -m 0755 /run/munge /var/log/munge
install -d -o slurm -g slurm -m 0750 /var/log/slurm
install -d -o slurm -g slurm -m 0700 /var/spool/slurmctld
install -d -o root -g root -m 0755 /var/spool/slurmd /var/lib/slurm/job_container
install -d -o slurmrestd -g slurmrestd -m 0750 \
    /run/slurmrestd /var/lib/slurmrestd
install -d -o workflow -g workflow -m 0750 /var/lib/workflow
install -d -o root -g root -m 0755 /usr/local/libexec/slurm /opt/slurm-workflow

install_asset config/slurm.conf /etc/slurm/profiles/simulation/slurm.conf root slurm 0644
install_asset config/cgroup.conf /etc/slurm/profiles/simulation/cgroup.conf root slurm 0644
install_asset config/slurmdbd.conf.template /etc/slurm/profiles/simulation/slurmdbd.conf.template root slurm 0644
install_asset config/slurm.production.conf /etc/slurm/profiles/production/slurm.conf root slurm 0644
install_asset config/cgroup.production.conf /etc/slurm/profiles/production/cgroup.conf root slurm 0644
install_asset config/job_container.production.conf /etc/slurm/profiles/production/job_container.conf root slurm 0644
install_asset config/slurmdbd.production.conf.template /etc/slurm/profiles/production/slurmdbd.conf.template root slurm 0644

install_asset scripts/cluster-entrypoint.sh /usr/local/sbin/cluster-entrypoint root root 0755
install_asset scripts/cluster-healthcheck.sh /usr/local/sbin/cluster-healthcheck root root 0755
install_asset scripts/dev-token-renewer.sh /usr/local/sbin/dev-token-renewer root root 0755
install_asset deploy/lifecycle/lifecycle-common.sh /usr/local/libexec/slurm/lifecycle-common.sh root root 0755
install_asset deploy/lifecycle/slurm-resume-program /usr/local/libexec/slurm/slurm-resume-program root root 0755
install_asset deploy/lifecycle/slurm-suspend-program /usr/local/libexec/slurm/slurm-suspend-program root root 0755

install_python_directory workflow /opt/slurm-workflow/workflow
install_python_directory examples/workflow /opt/slurm-workflow/examples
[[ -f /opt/slurm-workflow/workflow/__init__.py ]] ||
    build_die 'workflow package installation is incomplete'

install_compatibility_symlink profiles/simulation/slurm.conf /etc/slurm/slurm.conf
install_compatibility_symlink profiles/simulation/cgroup.conf /etc/slurm/cgroup.conf

for identity in munge:64029:64029 slurm:64030:64030 \
        slurmrestd:64031:64031 workflow:64032:64032; do
    IFS=: read -r name uid gid <<<"$identity"
    [[ $(id -u "$name") == "$uid" && $(id -g "$name") == "$gid" ]] ||
        build_die "runtime identity verification failed for $name"
done
if ((integration_database == 1)); then
    [[ $(id -u mysql) == 27 && $(id -g mysql) == 27 ]] ||
        build_die 'integration database identity must be mysql uid/gid 27'
fi

for plugin in auth_jwt rest_auth_jwt rest_auth_local data_parser_v0_0_45 \
        cgroup_v2 task_cgroup namespace_tmpfs tls_s2n \
        http_parser_llhttp_parser serializer_yaml accounting_storage_mysql; do
    [[ -f /usr/lib64/slurm/$plugin.so ]] ||
        build_die "configured runtime is missing plugin: $plugin"
done

build_log "configured runtime from $source_root"
