#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

printf 'Waiting for all five compute nodes to become idle...\n'
idle_nodes=0
for _ in {1..90}; do
    node_list=$(podman compose exec -T controller \
        sinfo --noheader --states=idle --Node --format='%N' \
        2>/dev/null || true)
    idle_nodes=$(printf '%s\n' "$node_list" | sort -u | \
        awk 'NF { count++ } END { print count + 0 }')
    if [[ $idle_nodes -ge 5 ]]; then
        break
    fi
    sleep 2
done

if [[ $idle_nodes -lt 5 ]]; then
    printf 'Timed out: only %s of 5 compute nodes are idle.\n' "$idle_nodes" >&2
    podman compose exec -T controller sinfo --Node --long >&2 || true
    exit 1
fi

podman compose exec -T controller sinfo --Node --long
printf '\nRunning hostname once on every simulated compute node:\n'
hostnames=$(timeout 90 podman compose exec -T controller \
    srun --immediate=30 --nodes=5 --ntasks=5 --ntasks-per-node=1 \
    /usr/bin/hostname | sort)
printf '%s\n' "$hostnames"

expected=$(printf 'compute-host-%03d\n' {1..5})
if [[ $hostnames != "$expected" ]]; then
    printf 'Unexpected task placement; expected five initial compute hosts.\n' >&2
    exit 1
fi

printf '\nSubmitting and waiting for a five-node batch job...\n'
job_result=$(timeout 90 podman compose exec -T controller \
    sbatch --parsable --wait --chdir=/shared/jobs \
    < "$project_dir/examples/hostname.sbatch")
job_id=${job_result%%;*}
job_id=${job_id//$'\r'/}

if [[ ! $job_id =~ ^[0-9]+$ ]]; then
    printf 'Could not parse the batch job ID from: %s\n' "$job_result" >&2
    exit 1
fi

state=
for _ in {1..30}; do
    accounting_rows=$(podman compose exec -T controller \
        sacct -X --jobs="$job_id" --noheader --parsable2 --format=State \
        2>/dev/null || true)
    state=$(printf '%s\n' "$accounting_rows" | \
        awk -F'|' 'NF { print $1; exit }')
    [[ $state == COMPLETED ]] && break
    sleep 1
done

if [[ $state != COMPLETED ]]; then
    printf 'Batch job %s accounting state is %s, expected COMPLETED.\n' \
        "$job_id" "${state:-missing}" >&2
    exit 1
fi

batch_output="/shared/output/hostnames-${job_id}.out"
if ! podman compose exec -T controller test -s "$batch_output"; then
    printf 'Batch output was not created in shared storage: %s\n' \
        "$batch_output" >&2
    exit 1
fi

printf 'Batch job %s completed; output:\n' "$job_id"
batch_hostnames=$(podman compose exec -T controller sort "$batch_output")
printf '%s\n' "$batch_hostnames"
if [[ $batch_hostnames != "$expected" ]]; then
    printf 'Unexpected batch output; expected compute1 through compute5.\n' >&2
    exit 1
fi
printf '\nSmoke test passed.\n'
