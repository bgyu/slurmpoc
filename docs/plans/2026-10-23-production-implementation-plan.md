# Production Slurm implementation plan

Status: implemented as the repository production contract. Account-specific
IaC, OIDC gateways, PKI/renewal agents, the lifecycle reconciler and
allocation-private capability delivery remain external production
dependencies rather than silently simulated components.

This plan implements the approved design in independently testable layers.
Each completed layer is recorded as a focused Git commit so the build,
security decisions, workflow service, and deployment examples can be reviewed
or reverted independently.

## Layer 1: build and runtime contract

- Harden the Dockerfile with version/digest inputs, required plugin checks,
  fixed service users, and a build manifest in `/var/packages`. The completed
  implementation delegates those responsibilities to `build-rpms.sh`,
  `install-runtime.sh`, and `configure-runtime.sh`; see
  `docs/operations/build-and-runtime.md`.
- Replace the multi-daemon database role with separate MariaDB and `slurmdbd`
  roles in the production contract.  The local profile may still use one
  image, but every daemon remains a distinct process/container.
- Add startup permission validation and runtime-secret staging.

Verification: build image; inspect RPM manifest; run daemon/plugin checks;
scan image configuration and history for generated secrets.

## Layer 2: Slurm configuration and elastic nodes

- Add production and simulation configuration templates.
- Enable configless operation, bounded dynamic-normal nodes in the rootless
  simulation, and bounded `FUTURE`/`CLOUD` identities in production.
- Add cgroup v2 production settings and an explicit WSL simulation override.
- Implement lock-protected, idempotent add/drain/remove node commands.

Verification: start five nodes; add a node; schedule work on it; drain/remove
it; reject unsafe active removal.

## Layer 3: protected REST plane

- Add a dedicated `slurmrestd` role and identity.
- Pin REST API `v0.0.45` and limit loaded plugins/connections.
- Add a TLS/authentication gateway contract and a self-contained development
  issuer.  Production requires external OIDC/JWKS.
- Add REST readiness and negative security tests.

Verification: submit a simple job through the gateway; prove direct client
access and forged/expired tokens fail.

## Layer 4: durable workflow service

- Implement workflow records and idempotency constraints.
- Submit a master through Slurm REST.
- Issue and atomically consume a short-lived callback capability.
- Submit a throttled worker array and an `afterany` finalizer.
- Implement reconciliation and explicit cancellation.

Verification: run the demo workflow; retry all API operations; restart the
broker around an ambiguous submission; verify one master and one array.

## Layer 5: deployment and operations

- Harden Compose as an integration profile.
- Add Quadlet/systemd examples and an AWS deployment contract.
- Document secret-manager, PKI, IAM, networking, storage, backup, restore,
  upgrade, node lifecycle, and incident procedures.
- Add a complete local acceptance script and operator commands.

Verification: lint configuration, render Compose/Quadlet definitions, run the
acceptance suite, and document limitations not testable inside WSL.

## Implementation history

The focused Git history is:

- `6f44fc8`: approved production architecture and threat model;
- `2e2e6ca`: hardened source build and runtime image;
- `e86af0c`: secure REST-driven workflow implementation;
- `c47894f`: AWS/Quadlet deployment contract; and
- `eab278f`, `f043de5`, `6b803ba`: reusable build-phase design, plan and
  implementation.

The history is evidence of review boundaries, not a substitute for image
signing, target-account acceptance or an auditable deployment change record.
