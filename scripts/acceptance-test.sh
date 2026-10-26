#!/usr/bin/env bash
# End-to-end acceptance for the local integration profile.
#
# This test intentionally covers boundaries that unit tests cannot: installed
# RPM artifacts, daemon identities, non-root job launch, unpublished private
# listeners, REST workflow fan-out, and dynamic-node scale out/in. Production
# still requires the target-account acceptance gates documented in the runbook.
set -Eeuo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

start_cluster=0
case ${1:-} in
    '') ;;
    --start) start_cluster=1 ;;
    -h|--help)
        printf 'Usage: scripts/acceptance-test.sh [--start]\n'
        printf '  --start  Build/start the cluster before running acceptance.\n'
        exit 0
        ;;
    *) printf 'acceptance-test: unknown option: %s\n' "$1" >&2; exit 64 ;;
esac
(($# <= 1)) || { printf 'acceptance-test: too many arguments\n' >&2; exit 64; }

dynamic_key="accept-$$"
dynamic_added=0
temporary_compose=$(mktemp /tmp/slurmpoc-compose.XXXXXXXX.yaml)

cleanup() {
    local status=$?
    trap - EXIT
    if [[ $dynamic_added == 1 ]]; then
        # Preserve the original failure while making a best effort to leave no
        # lifecycle-managed test node behind. remove.sh retains state if it
        # cannot prove that draining is safe.
        scripts/nodes/remove.sh --timeout 120 \
            --reason 'acceptance cleanup' "$dynamic_key" >/dev/null 2>&1 || true
    fi
    rm -f -- "$temporary_compose"
    exit "$status"
}
trap cleanup EXIT

if [[ $start_cluster == 1 ]]; then
    scripts/cluster-up.sh
fi

printf '1/8 static validation\n'
python3 -m unittest discover -s workflow/tests -v
python3 -m unittest discover -s scripts/tests -v
bash -n scripts/*.sh scripts/lib/*.sh scripts/nodes/*.sh \
    deploy/lifecycle/lifecycle-common.sh \
    deploy/lifecycle/slurm-resume-program \
    deploy/lifecycle/slurm-suspend-program \
    deploy/aws/materialize-aws-secret deploy/quadlet/install.sh
git diff --check
podman compose --env-file .runtime/cluster.env -f compose.yaml \
    config >"$temporary_compose"

printf '2/8 image RPM and secret-boundary validation\n'
image=localhost/slurm-sim:${SLURM_IMAGE_TAG:-latest}
podman run --rm --entrypoint /bin/bash "$image" -c '
    set -Eeuo pipefail
    test "$(find /var/packages/RPMS -type f -name "*.rpm" | wc -l)" -ge 7
    test "$(find /var/packages/SRPMS -type f -name "*.rpm" | wc -l)" -ge 2
    cd /var/packages
    tail -n +5 BUILD-MANIFEST | sha256sum --check --strict
    test ! -e /run/site/munge.key
    test ! -e /run/site/database-password
    test ! -e /run/workflow/api.token
'
image_environment=$(podman image inspect --format \
    '{{range .Config.Env}}{{println .}}{{end}}' "$image")
if grep -Eiq '(PASSWORD|TOKEN|SECRET|PRIVATE_KEY|MUNGE_KEY)=' \
        <<<"$image_environment"; then
    printf 'acceptance-test: image configuration contains a secret-like environment name\n' >&2
    exit 1
fi

printf '3/8 daemon identity and private-listener validation\n'
declare -a daemon_checks=(
    'slurm-sim_database_1:mysql:mariadbd'
    'slurm-sim_slurmdbd_1:slurm:slurmdbd'
    'controller:slurm:slurmctld'
    'slurm-sim_restd_1:slurmrestd:slurmrestd'
    'slurm-sim_broker_1:workflow:python3'
    'slurm-sim_compute001_1:root:slurmd'
)
for check in "${daemon_checks[@]}"; do
    IFS=: read -r container user process <<<"$check"
    podman exec "$container" pgrep -u "$user" -x "$process" >/dev/null
done
[[ -z $(podman port slurm-sim_restd_1) ]] || {
    printf 'acceptance-test: raw slurmrestd unexpectedly has a host port\n' >&2
    exit 1
}
[[ -z $(podman port slurm-sim_database_1) ]] || {
    printf 'acceptance-test: MariaDB unexpectedly has a host port\n' >&2
    exit 1
}

# Prove slurmd actually switches away from root for the service job identity;
# checking only the daemon's UID would miss a dangerous job-launch regression.
job_uid=$(podman exec controller runuser --user workflow -- \
    srun --immediate=30 --nodes=1 --ntasks=1 /usr/bin/id -u)
[[ $job_uid == 64032 ]] || {
    printf 'acceptance-test: workflow job ran as uid %s, expected 64032\n' "$job_uid" >&2
    exit 1
}

printf '4/8 unauthenticated workflow rejection\n'
python3 - <<'PY'
import http.client
import json

connection = http.client.HTTPConnection("127.0.0.1", 8088, timeout=5)
connection.request(
    "POST",
    "/v1/workflows",
    body=json.dumps({}).encode("ascii"),
    headers={"Content-Type": "application/json"},
)
response = connection.getresponse()
response.read()
connection.close()
if response.status != 401:
    raise SystemExit(f"unauthenticated broker POST returned {response.status}, expected 401")
PY

printf '5/8 direct scheduler and accounting smoke test\n'
scripts/smoke-test.sh

printf '6/8 REST master/worker workflow\n'
scripts/submit-workflow.sh --chunks 20 --parallel 5 --wait --timeout 600

printf '7/8 dynamic node scale-out and placement\n'
scripts/nodes/add.sh "$dynamic_key"
dynamic_added=1
dynamic_node="dyn-$dynamic_key"
placed_host=$(podman exec controller \
    srun --immediate=30 --nodes=1 --nodelist="$dynamic_node" /usr/bin/hostname)
[[ $placed_host == "$dynamic_node" ]] || {
    printf 'acceptance-test: job placed on %s, expected %s\n' \
        "$placed_host" "$dynamic_node" >&2
    exit 1
}

printf '8/8 drain and scale-in\n'
scripts/nodes/drain.sh --timeout 120 --reason 'acceptance test' "$dynamic_key"
scripts/nodes/remove.sh --timeout 120 --reason 'acceptance test' "$dynamic_key"
dynamic_added=0
if podman exec controller \
        scontrol show node "$dynamic_node" >/dev/null 2>&1; then
    printf 'acceptance-test: removed node still exists in controller state\n' >&2
    exit 1
fi

printf '\nLocal acceptance passed. Production external controls remain separate gates.\n'
