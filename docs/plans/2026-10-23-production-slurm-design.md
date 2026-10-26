# Production-oriented Slurm on Podman: design and threat model

Status: approved for implementation on 2026-10-23

## 1. Purpose and production boundary

This repository provides two related deployment profiles built from the same
source and exercised by the same tests:

1. `compose.yaml` is a self-contained integration environment for Fedora WSL
   and rootless Podman.  It proves RPM construction, Slurm REST submission,
   accounting, workflow fan-out, and safe node lifecycle behavior.  A single
   host cannot prove high availability, host isolation, or a production PKI.
2. The production profile is intended for dedicated RHEL-compatible hosts in
   AWS or another cloud, using rootful Podman managed by systemd/Quadlet.  It
   uses external identity, secrets, TLS certificates, durable state, database
   high availability, and a separately authorized infrastructure lifecycle
   service.

The same immutable Slurm image is used for each Slurm role, as required.  Each
container starts exactly one role.  Role-specific Unix users, filesystem
permissions, Linux capabilities, and network policy remain different even
though the image digest is the same.

The local profile is deliberately called an integration environment, not a
production cluster.  Moving its Compose file to an EC2 instance does not make
it production-ready.

## 2. Security and reliability goals

The implementation is designed around these invariants:

- No private key, password, MUNGE key, or bearer token is stored in Git, an
  image layer, an RPM, `/var/packages`, or a Compose environment variable.
- Internet clients cannot connect directly to `slurmrestd`, `slurmctld`,
  `slurmdbd`, MariaDB, or a compute daemon.
- `slurmctld`, `slurmdbd`, `slurmrestd`, MUNGE, the gateway, and the workflow
  service run with distinct, non-login identities.  `slurmd` remains root on
  a production compute host because it must establish job identities and
  cgroups.  Jobs run as their submitting POSIX identity, never as root.
- A batch job never receives a JWT signing key, Slurm administrator token,
  database password, Podman socket, or cloud credential.
- A lost HTTP response, process restart, or retry cannot silently create a
  second workflow or a second worker array.
- Routine scale-in never kills a running job.  A node is drained, checked, and
  only then stopped and returned to the available identity pool.
- Source versions and hashes are explicit.  A rebuild never follows an
  unbounded `latest` reference.
- The controller has bounded REST and scheduler load.  Chunk fan-out uses one
  Slurm job array instead of one REST request per chunk.

## 3. Architecture

```text
                                      private control-plane network

  User/client -- HTTPS + OIDC --> gateway -------> slurmrestd ----> slurmctld
                                    |                   |                |
                                    |                   |                +--> state
                                    v                   +--------------> slurmdbd
                              workflow broker                                |
                                    ^                                        v
                                    |                                  MariaDB HA
                                    |
                       one-use callback capability
                                    |
                              master batch job
                                    |
                                    +-- immutable manifest
                                    |
                         worker job array + finalizer
                                    |
                          elastic compute node pool
```

Production instances are placed in private subnets.  Only the gateway load
balancer has an ingress path from approved clients.  Security groups and host
firewalls express the same trust boundaries as the application configuration.

### 3.1 Production service placement

- Two controller hosts provide primary/backup `slurmctld`.  Controller state
  is kept on durable, protected storage suitable for Slurm failover.
- Two accounting hosts provide primary/backup `slurmdbd` where the selected
  Slurm version and operating model support it.
- MariaDB is an HA managed service (for AWS, normally RDS/Aurora-compatible
  MariaDB/MySQL only after compatibility validation) or a separately operated
  database cluster.  Slurm containers do not administer the database server.
- Two or more gateways and workflow brokers are stateless at the HTTP tier;
  workflow state and idempotency records live in a durable application
  database separate from Slurm accounting credentials.
- Compute containers run on dedicated compute hosts.  Production compute is
  rootful because rootless Podman cannot give `slurmd` the cgroup and identity
  controls needed for multi-user isolation.

The local profile runs MariaDB and `slurmdbd` in separate containers built
from the same image. It still uses a single non-HA host and development
database, which is never the documented production topology.

## 4. Build and supply-chain design

The multi-stage Containerfile performs the following operations:

1. Download explicit MUNGE and Slurm release archives over TLS.
2. Verify configured SHA-256 digests before extracting or executing RPM spec
   content.  Production CI must fail if a digest is omitted.
3. Build MUNGE first and install only its build artifacts into the Slurm build
   stage.
4. Build Slurm with MySQL accounting, JWT, REST, HTTP parser, JSON, YAML, and
   cgroup support.  The build asserts that required RPMs were produced.
5. Copy source and binary RPMs to `/var/packages/SRPMS` and
   `/var/packages/RPMS` in the runtime image.
6. Install the verified local RPMs and fail the build if required plugins or
   daemons are missing.
7. Record source versions and digests as OCI labels and emit a package
   manifest under `/var/packages/BUILD-MANIFEST`.

Production CI additionally signs the image, creates an SBOM and vulnerability
report, and promotes the image by digest between environments.  Runtime hosts
must not rebuild source independently.

## 5. Authentication, JWT, and TLS

### 5.1 Cluster authentication

MUNGE remains the cluster authentication mechanism.  A CSPRNG-generated key is
created by the production secret manager, distributed only to Slurm cluster
members, mounted at runtime, and staged into tmpfs as `munge:munge` mode
`0400`.  Rotation is a coordinated cluster operation.  Possession of this key
places a system inside the Slurm trust domain, so it is never exposed to
clients or workflow code.

For Slurm versions supporting `tls/s2n`, production enables internal RPC
encryption with certificates issued by the site PKI.  The sample development
CA is never reused in production.

### 5.2 REST authentication

The preferred production mode is an external OIDC issuer using short-lived
RS256 tokens and a protected JWKS trust file:

```ini
AuthAltTypes=auth/jwt
AuthAltParameters=jwks=/run/secrets/slurm-jwks.json,userclaimfield=preferred_username
```

The private signing key remains in the identity provider.  Slurm receives
only integrity-protected public JWKS material.  Local `scontrol token`
creation is disabled.  POSIX user names and numeric identities must be stable
on controllers and compute nodes, normally through SSSD/LDAP.

The gateway terminates TLS 1.3, validates issuer/audience/time claims, strips
all caller-supplied `X-SLURM-USER-*` headers, and injects trusted values.  It
also enforces request-size, rate, connection, and timeout limits.  Raw
`slurmrestd` binds only to a private listener and loads only the pinned REST
API plugin and required Slurm data source.

Slurm JWTs are identity tokens, not fine-grained OAuth scopes.  The ability to
mint an arbitrary trusted token is effectively cluster-root authority.  A
signing key therefore never appears in a compute job.

### 5.3 Development JWT fallback

The self-contained profile may use a random 256-bit HS256 key.  It is mounted
only into the controller and `slurmdbd`, mode `0400`, and never becomes part of
the image.  The development issuer does **not** receive the signing key: it
authenticates over the MUNGE-protected Slurm RPC channel and asks the controller
for an ordinary `workflow` user token that expires within fifteen minutes.
The production entrypoint rejects this mode unless an explicit development
profile is selected.

## 6. File, directory, and process ownership

| Object | Owner and mode | Rationale |
|---|---|---|
| `slurm.conf`, `cgroup.conf` | `root:slurm`, `0644`, read-only | Clients need to read cluster configuration; no workload may alter it. |
| `slurmdbd.conf` | `slurm:slurm`, `0600` | It may reference database credentials. |
| MUNGE key | `munge:munge`, `0400`, tmpfs | Shared cluster root of trust. |
| JWKS or HS256 trust file | consuming daemon identity, `0400` | Controllers/accounting use `slurm`; REST uses `slurmrestd`. Replacement changes REST trust. |
| Slurm TLS certificate and private key | service identity, `0600` | The s2n plugin enforces private, service-owned TLS files; the private key is never image-baked. |
| Controller state | `slurm:slurm`, `0700` | Contains authoritative scheduling state. |
| Compute spool | `root:root`, `0755` with protected children | `slurmd` owns job launch state. |
| Job callback capability | job UID, `0400`, private tmpfs | Not inherited in a job environment or image. |

All service UIDs/GIDs are fixed.  Containers use read-only root filesystems,
dedicated writable volumes, `no-new-privileges`, dropped capabilities, and
explicit tmpfs paths wherever the daemon permits it.  A startup validator
fails closed on a missing secret, group/world-writable config, wrong owner, or
unsupported role.

## 7. cgroup and job isolation

Production uses cgroup v2 with:

- `ProctrackType=proctrack/cgroup`
- `TaskPlugin=task/cgroup,task/affinity`
- `JobAcctGatherType=jobacct_gather/cgroup`
- CPU, RAM, swap, and device constraints
- finite job time and memory defaults
- a per-job private temporary directory

`slurmd` runs as root on a dedicated compute host/container so it can change
to the submitting UID and manage the delegated cgroup subtree.  The rootless
WSL integration profile cannot claim equivalent isolation; where the WSL
Podman machine cannot delegate writable cgroups it uses an explicit simulation
override, printed at startup and rejected by the production profile.

## 8. Elastic compute-node lifecycle

Both profiles use configless Slurm and bound controller cardinality, but their
node identity mechanisms deliberately differ:

- rootless WSL uses dynamic-normal (`slurmd -Z --conf`) registration because
  the container sees the VM topology rather than its Podman CPU quota;
- a non-cloud production pool may use predeclared dynamic `FUTURE` nodes; and
- the AWS power-save pool uses statically named `State=CLOUD` identities so a
  resume request for `aws-cpuNNNN` can only register that exact node.

Production `FUTURE` registration follows these controls:

- `SelectType=select/cons_tres`
- `MaxNodeCount` bounds the controller allocation.
- Approved resource profiles are predefined instead of accepting arbitrary
  resource claims from joining daemons.
- `SlurmctldParameters=enable_configless` keeps authoritative configuration on
  the controller.
- Compute nodes bootstrap from explicit primary/backup controller addresses or
  `_slurmctld._tcp` DNS SRV records.
- Starting `slurmd -F<profile>` claims an unused `FUTURE` identity.

Scale-up is idempotent: allocate an identity, create its private spool,
provision runtime trust, start the container, and wait for valid registration.

Local scale-down is deliberately conservative:

1. Mark the selected node `DRAIN` with an audit reason.
2. Wait with bounded exponential backoff until no job is running or completing.
3. Refuse removal if a reservation still refers to the node.
4. Stop `slurmd` gracefully.
5. Delete the dynamic-normal controller record and remove only that node's
   private metadata/spool. A production FUTURE implementation instead returns
   its predeclared identity to `FUTURE`; a CLOUD node follows Slurm power-save
   state and the external reconciler.

Automatic AWS elasticity uses `State=CLOUD`, `ResumeProgram`, and
`SuspendProgram`, but those hooks call a narrow, authenticated lifecycle API.
The controller is not given an EC2 administrator credential or a Podman
socket.  IAM permits only tagged instances, subnets, security groups, launch
templates, and instance profiles belonging to this cluster.  Scale requests
are idempotent and rate-limited.  Failed and partially created instances are
reconciled by the lifecycle service.

The workflow identity cannot create, delete, drain, or power nodes.  Pending
array tasks naturally consume capacity when new nodes become available.

## 9. REST-driven master/worker workflow

### 9.1 Submission sequence

1. A client sends `POST /v1/workflows` with an OIDC credential and an
   `Idempotency-Key`.
2. The broker validates the request against policy, records a submission
   intent, and submits the master using Slurm REST API `v0.0.45`.
3. The master writes an immutable JSON manifest to durable shared/object
   storage.  Each entry has a stable chunk ID, input URI, expected output URI,
   and optional digest.
4. After the allocation begins, a trusted node agent or prolog obtains a
   random, short-lived, one-use callback capability.  It is delivered as a
   `0400` file in job-private tmpfs, never as an environment variable.
5. The master calls `POST /v1/workflows/{id}/dispatch` with the manifest URI
   and hash.
6. The broker atomically consumes the capability and submits one throttled
   array, for example `0-999%20`, plus an `afterany` finalizer.
7. Every worker selects exactly one manifest entry with
   `SLURM_ARRAY_TASK_ID` and commits output idempotently.

The broker runs outside Slurm allocations and persists workflow state.  It is
not a Slurm administrator.  In production it uses identity-provider token
exchange to obtain a short-lived token representing the original ordinary
user.  Account, QOS, partition, time, memory, submit-count, and array-size
limits form the scheduler-side policy boundary.

### 9.2 Idempotency and reconciliation

Slurm REST job submission does not provide an application idempotency key.
The broker therefore owns this invariant:

- `(tenant, idempotency_key)` is unique.
- `(workflow_id, generation, manifest_hash)` is unique.
- A submission intent is committed before the Slurm call.
- Workflow and generation identifiers are stored in the Slurm job name or
  comment so an ambiguous timeout can be reconciled.
- Retrying a completed operation returns its saved result.
- More chunks create a new deterministic generation; an existing array is not
  expanded in place.

Workers provide at-least-once safety.  They write a temporary result and then
use atomic rename or conditional object-store creation for a key containing
workflow ID, chunk ID, and algorithm version.  Requeue can recompute, but it
cannot duplicate a committed result.

### 9.3 Cancellation and finalization

Cancellation first records `CANCEL_REQUESTED`, then explicitly cancels the
master, worker array, and dependent jobs.  A master job is not assumed to own
the lifecycle of separately submitted children.  Workers trap termination and
must not commit new results after cancellation.

An `afterany` finalizer always runs and evaluates the durable result ledger.
Using only `afterok` would strand finalization when one array element fails.

## 10. Observability and operations

- Every request has a correlation ID propagated into workflow and Slurm job
  metadata.
- Audit logs record authenticated identity, workflow, action, policy decision,
  manifest hash, and Slurm job IDs.  Tokens and passwords are redacted.
- `/livez`, `/readyz`, and `/healthz` protect REST routing.
- Alerts cover controller failover, database replication/backups, node
  registration failures, credential expiry, REST error rate, queue latency,
  pending reasons, and failed workflow reconciliation.
- Controller state and databases are backed up and restoration is exercised.
- NTP/chrony is mandatory because MUNGE and JWT both depend on correct time.
- Upgrade runbooks use a tested version matrix and immutable image digest;
  they do not rebuild on a live controller.

## 11. AWS deployment mapping

The repository will include parameterized examples rather than embedding AWS
account identifiers:

- private subnets across availability zones;
- an internal load balancer for gateway/broker traffic;
- Secrets Manager or Vault CSI/agent delivery for MUNGE, database, and TLS
  material;
- Route 53 private DNS for controller discovery;
- EFS or another filesystem validated for Slurm controller state and shared
  demo data, with S3 normally preferred for chunk input/output;
- RDS or a separately managed MariaDB service after Slurm compatibility and
  failure-mode testing;
- an EC2 launch template using a pinned image digest and least-privilege
  instance profile;
- CloudWatch/OpenTelemetry log and metric export;
- a lifecycle role restricted by mandatory cluster tags.

The production deployment layer may be Terraform, CloudFormation, or an
existing organization platform.  The container contract does not require
Terraform specifically: configuration, secrets, identity, networking, state,
and lifecycle APIs are explicit inputs.

## 12. Alternatives and rationale

### Master directly holds a Slurm JWT

Rejected as the production default.  It satisfies a literal nested REST call,
but the bearer token carries all authority of that POSIX user and has no
per-workflow endpoint scope.  A one-use broker capability has a much smaller
blast radius.

### One allocation with `srun` job steps

Useful for tightly coupled work, but rejected for this workflow.  The master
would reserve peak resources in advance, could not naturally grow onto newly
added nodes, and would hold idle capacity.

### One REST submission per chunk

Rejected.  It creates avoidable controller load and makes cancellation,
throttling, and reconciliation harder.  A Slurm array is the scheduler-native
representation of homogeneous chunk work.

### Give `slurmctld` the Podman socket or AWS administrator credentials

Rejected.  A compromise of the controller would become an infrastructure
compromise.  A narrow lifecycle API and tagged-resource IAM policy keep these
trust domains separate.

## 13. Acceptance criteria

The local automated acceptance path must demonstrate:

- verified RPMs exist under `/var/packages` and required Slurm REST/JWT/cgroup
  plugins are installed;
- secrets are absent from image history, process arguments, Compose
  environment, and source-controlled files;
- service daemons have the documented UIDs and jobs are non-root;
- direct access to `slurmrestd` is unavailable from the client network;
- expired, forged, and replayed credentials fail;
- a master is submitted through REST, dispatches a manifest through the
  broker, and produces a worker array and finalizer;
- retrying a workflow request does not create duplicate jobs;
- five workers can start initially, more workers can join while work is
  pending, and an idle worker can be safely drained and removed;
- active-node removal is refused or waits for drain completion;
- controller and broker restarts do not lose workflow mappings;
- cgroup limits work on a production-capable Linux host; the WSL simulation
  limitation is detected and explicitly reported.

## 14. Upstream references

- [Slurm REST security and deployment](https://slurm.schedmd.com/rest.html)
- [Slurm JWT authentication](https://slurm.schedmd.com/jwt.html)
- [Slurm REST API](https://slurm.schedmd.com/rest_api.html)
- [REST client and version lifecycle](https://slurm.schedmd.com/rest_clients.html)
- [Dynamic nodes](https://slurm.schedmd.com/dynamic_nodes.html)
- [Configless Slurm](https://slurm.schedmd.com/configless_slurm.html)
- [Power-saving and cloud nodes](https://slurm.schedmd.com/power_save.html)
- [Slurm job arrays](https://slurm.schedmd.com/job_array.html)
- [Slurm cgroup v2](https://slurm.schedmd.com/cgroup_v2.html)
- [MUNGE and Slurm authentication](https://slurm.schedmd.com/authentication.html)
- [Slurm TLS](https://slurm.schedmd.com/tls.html)
