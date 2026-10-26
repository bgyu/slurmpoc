#!/usr/bin/env bash
# Build and start the complete local integration topology.
#
# This command intentionally owns development-secret initialization and image
# construction, but never prints or exports a secret. Production hosts consume
# an already promoted image digest and secret-manager mounts instead.
set -Eeuo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runtime_dir="$project_dir/.runtime"
compose_file="$project_dir/compose.yaml"

command -v podman >/dev/null 2>&1 || {
    printf 'cluster-up: podman is required but was not found in PATH\n' >&2
    exit 127
}

"$project_dir/scripts/bootstrap-dev-secrets.sh"

slurm_version=${SLURM_VERSION:-26.05.5}
slurm_sha256=${SLURM_SHA256:-4680cafc82b0689865c885eb703441e7ebf764896c40fd03a50f2fcb60664f68}
munge_version=${MUNGE_VERSION:-0.5.18}
munge_sha256=${MUNGE_SHA256:-39c3ec6ef5604bfa206e8aa10fc05d5119040f6de4a554bc0fb98ca1aed838dc}
image_tag=${SLURM_IMAGE_TAG:-latest}

cd "$project_dir"
podman build --format docker \
    --build-arg "SLURM_VERSION=$slurm_version" \
    --build-arg "SLURM_SHA256=$slurm_sha256" \
    --build-arg "MUNGE_VERSION=$munge_version" \
    --build-arg "MUNGE_SHA256=$munge_sha256" \
    --tag "localhost/slurm-sim:$image_tag" \
    --file Dockerfile .

# --remove-orphans is important when upgrading the older static topology: it
# removes only containers carrying this Compose project's labels, not unrelated
# Podman containers belonging to the user.
podman compose --env-file "$runtime_dir/cluster.env" -f "$compose_file" \
    up --no-build --detach --remove-orphans

printf '\nWaiting for five dynamic compute nodes to become idle...\n'
idle_nodes=0
for _ in {1..120}; do
    node_list=$(podman compose --env-file "$runtime_dir/cluster.env" \
        -f "$compose_file" exec -T controller \
        sinfo --noheader --states=idle --Node --format='%N' 2>/dev/null || true)
    idle_nodes=$(printf '%s\n' "$node_list" | sort -u | \
        awk 'NF { count++ } END { print count + 0 }')
    if [[ $idle_nodes -ge 5 ]]; then
        break
    fi
    sleep 2
done

if [[ $idle_nodes -lt 5 ]]; then
    printf 'Cluster startup timed out: only %s of 5 nodes are idle.\n' \
        "$idle_nodes" >&2
    podman compose --env-file "$runtime_dir/cluster.env" -f "$compose_file" \
        logs --tail=150 database slurmdbd controller compute001 >&2 || true
    exit 1
fi

printf 'Waiting for the protected workflow API...\n'
broker_ready=0
for _ in {1..60}; do
    if podman compose --env-file "$runtime_dir/cluster.env" -f "$compose_file" \
        exec -T broker python3 -c \
        'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8088/readyz", timeout=2).read()' \
        >/dev/null 2>&1; then
        broker_ready=1
        break
    fi
    sleep 2
done
if [[ $broker_ready != 1 ]]; then
    printf 'Workflow broker did not become ready.\n' >&2
    podman compose --env-file "$runtime_dir/cluster.env" -f "$compose_file" \
        logs --tail=150 token-issuer restd broker >&2 || true
    exit 1
fi

printf '\nCluster and workflow API are ready.\n'
podman compose --env-file "$runtime_dir/cluster.env" -f "$compose_file" \
    exec -T controller sinfo --Node --long
printf 'Smoke test:       scripts/smoke-test.sh\n'
printf 'Workflow demo:    scripts/submit-workflow.sh --chunks 20 --parallel 5 --wait\n'
printf 'Add compute node: scripts/nodes/add.sh extra-01\n'
printf 'Controller shell: podman exec -it controller bash\n'
