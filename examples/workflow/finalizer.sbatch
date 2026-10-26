#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

helper_dir=${WORKFLOW_HELPER_DIR:-/opt/slurm-workflow/examples}
[[ $helper_dir == /* && -f $helper_dir/finalizer.py ]] || {
    echo "workflow finalizer helper is unavailable" >&2
    exit 70
}
exec python3 -Es "$helper_dir/finalizer.py"
