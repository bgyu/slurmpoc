#!/usr/bin/env bash
# Keep health checks read-only and role-local.  They prove both that the daemon
# process exists and, where possible, that it participates in the cluster.
# Readiness dependencies belong in the entrypoint; health checks must return
# promptly so Podman can distinguish failure from a slow monitoring command.
set -euo pipefail

role=${CONTAINER_ROLE:-}
profile=${SLURM_PROFILE:-production}
runtime_conf=/run/slurm/config/slurm.conf

if [[ -r $runtime_conf ]]; then
    export SLURM_CONF=$runtime_conf
fi

case "$role" in
    database)
        pgrep -u mysql -x mariadbd >/dev/null
        timeout 5 mariadb-admin --protocol=socket \
            --socket=/run/mariadb/mariadb.sock ping --silent >/dev/null
        ;;
    slurmdbd)
        pgrep -u slurm -x slurmdbd >/dev/null
        # A TCP connect is deliberately used instead of a fake Slurm request;
        # production slurmdbd requires tls/s2n and rejects cleartext payloads.
        timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/6819'
        ;;
    controller)
        pgrep -u slurm -x slurmctld >/dev/null
        timeout 5 scontrol ping 2>/dev/null | grep -q 'UP'
        ;;
    compute)
        pgrep -u root -x slurmd >/dev/null
        host=$(hostname -s)
        # Dynamic FUTURE allocation deliberately gives NodeName (computeNNN)
        # and NodeHostName (compute-host-NNN) different values. Match the
        # latter instead of assuming that the container hostname is NodeName.
        timeout 5 scontrol show nodes --oneliner 2>/dev/null | \
            awk -v wanted="$host" '
                {
                    host = ""; state = ""
                    for (i = 1; i <= NF; i++) {
                        if ($i ~ /^NodeHostName=/) host = substr($i, 14)
                        if ($i ~ /^State=/) state = substr($i, 7)
                    }
                    if (host == wanted && state !~ /^(FUTURE|UNKNOWN)/) found = 1
                }
                END { exit(found ? 0 : 1) }
            '
        ;;
    restd)
        pgrep -u slurmrestd -x slurmrestd >/dev/null
        listen=${SLURMRESTD_LISTEN:-}
        if [[ -z $listen ]]; then
            [[ $profile == production ]] && listen=https://0.0.0.0:6820 || listen=0.0.0.0:6820
        fi
        address=${listen#https://}
        port=${address##*:}
        # slurmrestd does not promise a version-independent /readyz route.
        # The daemon has already completed all plugin/listener setup before
        # accepting TCP, so pgrep plus a bounded local connect is the stable
        # role-local liveness check.  Authenticated API readiness is checked by
        # the broker and external monitoring.
        timeout 5 bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1"' _ "$port"
        ;;
    token-issuer)
        token_file=${SLURM_TOKEN_FILE:-/run/slurm-token/workflow.jwt}
        pgrep -f '^/usr/bin/bash /usr/local/sbin/dev-token-renewer$' >/dev/null ||
            pgrep -f '^bash /usr/local/sbin/dev-token-renewer$' >/dev/null
        test -s "$token_file"
        # Rotation occurs every five minutes by default; a ten-minute bound
        # catches a wedged issuer without parsing or printing the bearer token.
        find "$token_file" -maxdepth 0 -type f -mmin -10 | grep -q .
        ;;
    broker)
        pgrep -u workflow -f '^python3 -m workflow$' >/dev/null
        timeout 5 python3 -c \
            'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8088/readyz", timeout=3).read()'
        ;;
    *)
        exit 1
        ;;
esac
