#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

if [[ ! -f .runtime/cluster.env ]]; then
    printf 'cluster-down: .runtime/cluster.env is missing; nothing was initialized\n' >&2
    exit 0
fi

# Removing orphans is scoped by the Compose project label. It is required when
# upgrading from the old compute1..compute5 service names, and does not touch
# unrelated Podman containers.
podman compose --env-file .runtime/cluster.env -f compose.yaml \
    down --remove-orphans "$@"
