# Slurm on UBI 10.2 with Podman

This repository builds MUNGE and Slurm from pinned upstream source archives,
produces source and binary RPMs, installs the required RPMs into one UBI 10.2
runtime image, and uses that image for every cluster role.

Two deployment profiles are intentionally separate:

- `compose.yaml` is a self-contained rootless-Podman integration cluster for
  Fedora 44 under WSL: one MariaDB, one `slurmdbd`, one controller, one private
  `slurmrestd`, one workflow broker, and five compute nodes.
- `deploy/quadlet` and `deploy/aws` define the hardened rootful production
  contract: HA controllers/accounting, managed databases and secrets, TLS,
  OIDC/JWKS, cgroup v2, dedicated compute hosts, and a narrow cloud lifecycle
  API. They are reference deployment inputs, not an account-specific IaC stack.

The pinned defaults are MUNGE 0.5.18 and Slurm 26.05.5. An upgrade requires
both a new version and its verified SHA-256 digest; the build never follows an
unbounded source `latest` reference.

## Local quick start

Requirements are rootless Podman, a Compose provider (`podman-compose` works),
and enough WSL resources for the build plus eleven running containers.

```bash
scripts/cluster-up.sh
scripts/smoke-test.sh
scripts/submit-workflow.sh --chunks 20 --parallel 5 --wait
```

The first command creates private development credentials under the ignored
`.runtime/` directory, builds `localhost/slurm-sim:latest`, starts the cluster,
and waits for five idle nodes and the protected workflow API. It is safe to
repeat: state is held in named volumes and accounting bootstrap is idempotent.

Useful inspection commands:

```bash
podman exec controller sinfo --Node --long
podman exec controller squeue
podman exec controller sacct -X --starttime today \
  --format=JobID,JobName,State,Elapsed,NodeList
podman compose logs --tail=100 controller slurmdbd broker
```

Stop the containers while preserving named-volume state, or explicitly remove
that state:

```bash
scripts/cluster-down.sh
scripts/cluster-down.sh --volumes
```

`--volumes` destroys local controller, accounting, workflow, compute-spool,
and shared-demo data. Development keys remain in `.runtime/secrets`; remove
that directory only when deliberately rotating the local cluster identity.

## RPM build and image contents

Package construction, runtime installation, and filesystem configuration are
separate commands. On a disposable UBI/RHEL-compatible EL10 build host:

```bash
sudo scripts/build-rpms.sh \
  --output-dir /var/packages \
  --work-dir /var/tmp/slurm-rpm-build
```

On the target EL10 runtime host or image filesystem:

```bash
sudo scripts/install-runtime.sh \
  --packages-dir /var/packages \
  --with-integration-database
sudo scripts/configure-runtime.sh \
  --source-root "$PWD" \
  --with-integration-database
```

Production roles using an external database may pass
`--without-integration-database` to the last two commands. The configuration
command installs identities and immutable assets but never creates secrets.
Pinned defaults live in `build/versions.conf`; use `--help` for explicit
version/digest overrides and alternate paths. These commands intentionally
refuse Fedora or another non-EL10 host, so from Fedora WSL run them inside UBI
10.2 or use the multi-stage build below.

The Dockerfile is a thin caller of those same three scripts:

```bash
podman build --format docker \
  --tag localhost/slurm-sim:latest \
  --file Dockerfile .
```

The complete phase contract—including every input, selected RPM, directory
mode, identity, manifest rule, manual-container invocation, failure mode and
CI promotion gate—is in the
[package build and runtime implementation](docs/operations/build-and-runtime.md).
The exact UBI, EPEL, CentOS Stream, upstream-source and local-RPM dependency
flow—including observed package resolution, support boundaries and required
CI evidence—is in
[dependency sources and package provenance](docs/operations/dependency-provenance.md).

`build-rpms.sh` verifies source archives before extraction, builds MUNGE first,
then builds Slurm with REST/JWT, MySQL accounting, YAML, `tls/s2n`, cgroup v2,
and namespace plugins. The final image contains:

- binary RPMs in `/var/packages/RPMS`;
- source RPMs in `/var/packages/SRPMS`;
- source versions plus RPM hashes in `/var/packages/BUILD-MANIFEST`; and
- only the selected runtime RPMs installed from that package set.

The signed EPEL and CentOS Stream repositories provide dependencies absent
from the UBI subset. Stream kernel headers are a builder-only input needed by
current `libbpf-devel`; the runtime remains based on UBI 10.2.

For a source upgrade, provide version and matching digest together:

```bash
podman build --format docker \
  --build-arg SLURM_VERSION=X.Y.Z \
  --build-arg SLURM_SHA256=64_LOWERCASE_HEX \
  --build-arg MUNGE_VERSION=X.Y.Z \
  --build-arg MUNGE_SHA256=64_LOWERCASE_HEX \
  --tag localhost/slurm-sim:X.Y.Z \
  --file Dockerfile .
```

## Job submission

A conventional batch script can be streamed into the controller without
mounting the source checkout into a container. Run the submission process as
the ordinary local workflow account so the resulting application is not UID
0:

```bash
podman exec --user workflow -i controller \
  sbatch --chdir=/shared/jobs --wait < examples/hostname.sbatch
podman exec controller ls -l /shared/output
```

`scripts/submit-job.sh` currently uses the container's default exec user and
is retained only as a documented local gap until its root-submission behavior
and negative regression test are corrected together. Production users never
submit through Podman. See
[service identities, job users, ownership, and permissions](docs/operations/identity-and-permissions.md#81-known-local-submit-jobsh-gap).

The local cluster uses the `shared-data` named volume at `/shared`. It is not a
host bind mount; inspect files through a cluster container. Fixed and
lifecycle-managed compute nodes attach the same volume.

The REST workflow demo follows this path:

```text
local client -> loopback broker -> private slurmrestd -> master batch job
                                                      -> throttled worker array
                                                      -> afterany finalizer
```

`submit-workflow.sh` stages a bounded JSON input, authenticates to the broker
with a mode-0600 local token, submits the master through Slurm REST API
`v0.0.45`, waits, and validates every chunk result. The master creates one
immutable manifest and makes one authenticated callback. The broker submits a
single bounded array such as `0-19%5`, not one REST request per chunk.

The development JWT signing key is mounted only into `slurmctld` and
`slurmdbd`. A separate issuer that does not possess the key asks the controller
for a short-lived token limited to the `workflow` identity. The broker reloads
the token file for each Slurm request, so atomic rotation does not require a
restart. Raw `slurmrestd` is not published; only the broker is bound to
`127.0.0.1:8088`.

## Add and remove compute nodes at runtime

The initial five nodes use dynamic-normal registration because a rootless WSL
container sees the VM's physical topology rather than its Podman CPU quota.
The entrypoint declares a deterministic two-CPU simulation shape, while
`MaxNodeCount=100` bounds growth.

```bash
scripts/nodes/add.sh extra-01
scripts/nodes/list.sh
podman exec controller \
  srun --nodes=1 --nodelist=dyn-extra-01 /usr/bin/hostname
scripts/nodes/drain.sh --timeout 300 --reason 'local maintenance' extra-01
scripts/nodes/remove.sh --timeout 300 --reason 'local maintenance' extra-01
```

Lifecycle operations are serialized by a host lock and manage only containers
with this project's ownership labels. Removal refuses active jobs and Slurm
reservations, drains first, stops `slurmd`, deletes the dynamic controller
record, and removes only that node's private metadata/spool. Repeating a
completed removal is a no-op.

Production elasticity is different: the supplied Slurm power-save hooks send
mTLS-authenticated desired-state requests to a separately authorized lifecycle
service. Controllers receive no Podman socket and no EC2 mutation credential.
The AWS contract uses static `State=CLOUD` identities, generation-aware
reconciliation, tagged resources, and narrowly scoped IAM.

## Process identities and privilege boundary

Not every process can or should run as an unprivileged user:

| Process | Runtime identity | Reason |
|---|---:|---|
| `munged` | `munge` (64029) | Dedicated authentication identity. |
| `slurmctld`, `slurmdbd` | `slurm` (64030) | No root privileges required. |
| `slurmrestd` | `slurmrestd` (64031) | Separate REST compromise boundary. |
| workflow broker/jobs | `workflow` (64032) | Deliberately narrow demo application identity. |
| MariaDB | `mysql` (27) | Database package identity. |
| `slurmd` | root | Must create cgroups/namespaces and enter the submitted job UID/GID. |
| container PID 1 | root supervisor | Validates/stages read-only inputs, then drops daemon privilege. |

The local cluster is rootless: container root maps to the unprivileged WSL
user. That is useful containment but is not a production multi-user boundary.
Production compute must use dedicated rootful hosts with cgroup v2, SELinux,
matching centralized numeric identities, and the reviewed capability set. Do
not make `slurmd` non-root by running user jobs as root or by disabling cgroup
and identity enforcement.

## Secrets, keys, and permissions

No secret is accepted through a Compose environment value, build argument, or
Git-tracked file. `scripts/bootstrap-dev-secrets.sh` uses the kernel CSPRNG,
creates `.runtime` directories mode 0700, and writes secret files mode 0600.
The root supervisor validates non-symlink regular files and permissions before
copying role-specific material to tmpfs with the consuming UID and a narrower
mode.

Production replaces local files with Secrets Manager/Vault and workload PKI:

- MUNGE uses one high-entropy cluster key, distributed only to cluster members;
- REST trusts public RS256 JWKS and never receives an identity-provider private
  signing key;
- service-unique TLS keys and certificates protect internal Slurm RPC;
- database and broker credentials are distinct and atomically rotated; and
- bearer tokens, passwords, and private keys never enter image layers, argv,
  environment variables, user data, logs, or `/var/packages`.

## Production deployment path

Read these before adapting the examples:

- [architecture and threat model](docs/plans/2026-10-23-production-slurm-design.md)
- [package build and runtime implementation](docs/operations/build-and-runtime.md)
- [dependency sources and package provenance](docs/operations/dependency-provenance.md)
- [service identities, job users, ownership, and permissions](docs/operations/identity-and-permissions.md)
- [implementation rationale](docs/plans/2026-10-23-production-implementation-plan.md)
- [AWS deployment contract](docs/operations/aws-deployment.md)
- [production operations runbook](docs/operations/production-runbook.md)
- [rootful Quadlet contract](deploy/quadlet/README.md)

The production templates fail closed on missing root-owned configuration,
public JWKS, MUNGE material, TLS identity, database CA, cgroup delegation, or
durable mounts. They require an image promoted by digest and deliberately do
not start or enable services during installation.

One production integration remains site-specific by design: a node-attested
component must deliver a one-use callback capability into only the allocated
master job after it starts. The Quadlet broker keeps the source capability in
private tmpfs and advertises a different job-visible path, so workflows fail
closed until that component exists. Do not replace it with a shared capability
directory; all demo jobs use UID 64032 and could otherwise read each other's
credentials.

## Validation

Run unit and integration checks after changes:

```bash
python3 -m unittest discover -s workflow/tests -v
python3 -m unittest discover -s scripts/tests -v
bash -n scripts/*.sh scripts/lib/*.sh scripts/nodes/*.sh \
  deploy/lifecycle/lifecycle-common.sh \
  deploy/lifecycle/slurm-resume-program \
  deploy/lifecycle/slurm-suspend-program \
  deploy/aws/materialize-aws-secret deploy/quadlet/install.sh
scripts/smoke-test.sh
scripts/submit-workflow.sh --chunks 20 --parallel 5 --wait
```

Production acceptance additionally requires target-account tests for HA
failover, restore, TLS/JWT rejection, cgroup isolation, identity parity,
node launch/drain/termination, lifecycle idempotency, IMDS isolation, and
cross-job callback-capability disclosure. A healthy local Compose cluster
does not prove those external controls.
