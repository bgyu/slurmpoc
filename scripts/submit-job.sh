#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

if (($# < 1)) || [[ $1 == -h || $1 == --help ]]; then
    printf 'Usage: %s JOB_SCRIPT [SBATCH_OPTIONS...]\n' "${0##*/}"
    printf 'Example: %s examples/hostname.sbatch --partition=debug\n' "${0##*/}"
    exit $(( $# < 1 ? 64 : 0 ))
fi

job_script=$1
shift

if [[ ! -f $job_script || ! -r $job_script ]]; then
    printf 'Job script is not a readable file: %s\n' "$job_script" >&2
    exit 66
fi

cd "$project_dir"
podman compose -f compose.yaml exec -T controller \
    sbatch --chdir=/shared/jobs "$@" < "$job_script"

printf 'Output is in the named shared-data volume at /shared/output.\n'
printf 'Inspect it with: podman exec controller ls -l /shared/output\n'
