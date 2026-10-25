#!/usr/bin/env bash
# Development-only short-lived Slurm JWT delivery.
#
# This helper deliberately does not possess the HS256 signing key.  It asks the
# controller to mint a token through the authenticated Slurm RPC channel, then
# publishes only a short-lived ordinary-user token through a protected runtime
# volume.  Production disables `scontrol token` and uses OIDC/JWKS token
# exchange instead; starting this helper requires an explicit opt-in.
set -Eeuo pipefail

[[ ${ALLOW_DEVELOPMENT_TOKEN_ISSUER:-0} == 1 ]] || {
    printf 'dev-token-renewer: development issuer was not explicitly enabled\n' >&2
    exit 78
}
[[ ${SLURM_PROFILE:-simulation} == simulation ]] || {
    printf 'dev-token-renewer: refusing to run outside the simulation profile\n' >&2
    exit 78
}

token_file=${SLURM_TOKEN_FILE:-/run/slurm-token/workflow.jwt}
token_user=${SLURM_TOKEN_USER:-workflow}
lifespan=${SLURM_TOKEN_LIFESPAN:-600}
refresh=${SLURM_TOKEN_REFRESH_SECONDS:-300}

[[ $token_user =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || {
    printf 'dev-token-renewer: invalid token user\n' >&2
    exit 64
}
[[ $lifespan =~ ^[0-9]+$ && $lifespan -ge 60 && $lifespan -le 900 ]] || {
    printf 'dev-token-renewer: lifespan must be between 60 and 900 seconds\n' >&2
    exit 64
}
[[ $refresh =~ ^[0-9]+$ && $refresh -ge 30 && $refresh -lt $lifespan ]] || {
    printf 'dev-token-renewer: refresh must be at least 30 and less than lifespan\n' >&2
    exit 64
}

token_dir=${token_file%/*}
install -d -o workflow -g workflow -m 0750 "$token_dir"

for _ in {1..90}; do
    if scontrol ping 2>/dev/null | grep -q 'UP'; then
        break
    fi
    sleep 2
done
scontrol ping 2>/dev/null | grep -q 'UP' || {
    printf 'dev-token-renewer: controller did not become ready\n' >&2
    exit 1
}

while true; do
    # Run token creation as SlurmUser rather than container root.  SlurmUser is
    # allowed to request another user's token, while the resulting file grants
    # only the ordinary workflow user's scheduler permissions.
    token_assignment=$(runuser --user slurm -- \
        scontrol token "username=$token_user" "lifespan=$lifespan")
    [[ $token_assignment == SLURM_JWT=* ]] || {
        printf 'dev-token-renewer: controller returned an unexpected response\n' >&2
        exit 1
    }
    token=${token_assignment#SLURM_JWT=}
    [[ $token =~ ^[A-Za-z0-9_.~-]+$ ]] || {
        printf 'dev-token-renewer: controller returned a malformed token\n' >&2
        exit 1
    }

    temporary=$(mktemp --tmpdir="$token_dir" '.jwt.XXXXXXXX')
    printf '%s\n' "$token" >"$temporary"
    unset token token_assignment
    chown workflow:workflow "$temporary"
    chmod 0400 "$temporary"
    mv -f -- "$temporary" "$token_file"
    sleep "$refresh"
done
