#!/usr/bin/env bash
# Keep the documented command stable while the substantive client remains
# testable, shell-free Python.  `exec` preserves signals and the Python exit
# status, which matters to CI callers waiting for workflow completion.
set -Eeuo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$project_dir/scripts/submit-workflow.py" "$@"
