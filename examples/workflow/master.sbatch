#!/usr/bin/env bash
# The REST request contains this file's text.  Runtime logic lives in the
# immutable, root-owned helper so operators can audit one implementation and
# Slurm's spool does not need to carry a large generated script.
set -Eeuo pipefail
umask 077

helper_dir=${WORKFLOW_HELPER_DIR:-/opt/slurm-workflow/examples}
[[ $helper_dir == /* && -f $helper_dir/master.py ]] || {
    echo "workflow master helper is unavailable" >&2
    exit 70
}
exec python3 -Es "$helper_dir/master.py"
