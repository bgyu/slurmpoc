#!/usr/bin/env bash
set -Eeuo pipefail

# Render and install the reference rootful Quadlet units.  This script does not
# enable or start anything: production rollout order, quorum and controller
# failover are operator decisions and must remain visible in change control.

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
destination=/etc/containers/systemd
image_reference=

usage() {
    cat <<'EOF'
Usage: deploy/quadlet/install.sh --image REGISTRY/REPOSITORY@sha256:DIGEST [--destination DIR]

Install digest-pinned rootful Quadlet definitions, without enabling services.
EOF
}

die() {
    printf '[quadlet-install] ERROR: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case $1 in
        --image)
            (($# >= 2)) || die '--image requires a value'
            image_reference=$2
            shift 2
            ;;
        --destination)
            (($# >= 2)) || die '--destination requires a value'
            destination=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ $image_reference =~ ^[A-Za-z0-9][A-Za-z0-9./:_@-]*@sha256:[a-f0-9]{64}$ ]] ||
    die 'image must be a registry/repository reference pinned by a full sha256 digest'
[[ $destination == /* && ! -L $destination ]] ||
    die 'destination must be an absolute, non-symlink directory path'

umask 022
install -d -o root -g root -m 0755 "$destination"
for unit in "$script_dir"/*.container; do
    install -o root -g root -m 0644 "$unit" "$destination/${unit##*/}"
done

temporary=$(mktemp "$destination/.slurm-runtime.image.XXXXXXXX")
trap 'rm -f -- "$temporary"' EXIT
# The reference validation above excludes '&', the only replacement-sensitive
# character for this sed expression.  No shell evaluation is involved.
sed "s&@@IMAGE_REFERENCE@@&$image_reference&" \
    "$script_dir/slurm-runtime.image.in" > "$temporary"
chown root:root "$temporary"
chmod 0644 "$temporary"
mv -f -- "$temporary" "$destination/slurm-runtime.image"
trap - EXIT

printf 'Installed Quadlet files in %s. Validate, then run systemctl daemon-reload.\n' \
    "$destination"
