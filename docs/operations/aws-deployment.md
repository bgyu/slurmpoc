# AWS production deployment and security contract

This document turns the general production design into an AWS deployment
contract. It is intentionally explicit about trust boundaries and failure
modes. The repository supplies containers, host units, Slurm configuration
examples and the narrow lifecycle client; the organization's IaC remains the
authoritative implementation of AWS resources. The files under `deploy/` are
deployment contracts and fail-closed examples, not a turnkey AWS stack.

## 1. Supported production shape

Use a dedicated AWS account or tightly controlled workload account and at
least two Availability Zones.  No cluster instance has a public IPv4 address.
The reference topology is:

- two controller EC2 instances on separate AZs, with primary/backup
  `slurmctld` and shared, durable `StateSaveLocation`;
- two accounting EC2 instances with primary/backup `slurmdbd`;
- RDS Multi-AZ for Slurm accounting and a separate schema/credential (or
  separate cluster) for the workflow broker;
- two REST/gateway instances or tasks behind an approved TLS load balancer;
  raw `slurmrestd` listens with mandatory TLS only on loopback beside its
  gateway;
- two workflow broker/gateway hosts behind an internal load balancer; raw
  broker HTTP also listens only on loopback;
- an internal mTLS lifecycle service across two AZs, with durable idempotency
  and node-mapping records;
- private, replaceable compute instances created from one approved launch
  template; and
- Route 53 private DNS, AWS KMS, Secrets Manager, CloudWatch and interface VPC
  endpoints.

The integration Compose stack is not an AWS production topology. In
particular, one EC2 instance running that Compose file still has a single
failure domain and rootless cgroup limitations.

This repository does not instantiate the OIDC gateways, lifecycle service,
workload-attested PKI agent, broker JWT renewal agent, centralized identity or
per-allocation callback-capability delivery agent. They are mandatory
production dependencies. Do not replace a missing dependency with a public
port, static long-lived token, shared capability directory or controller cloud
credentials.

## 2. Availability Zones and failure semantics

Pin controller and accounting peers to different AZs.  Stable private DNS
names (`controller-primary`, `controller-backup`, `slurmdbd-primary`, and
`slurmdbd-backup`) resolve only inside the VPC.  Slurm decides controller
failover; an external load balancer must not independently send the same
controller identity to two active processes.

Controller state must be common to both peers.  A local EBS volume on each
controller is not shared state and can split the scheduler after failover.
Use a tested, low-latency, POSIX shared filesystem such as an appropriately
configured EFS or FSx for OpenZFS deployment, and benchmark metadata latency
with the expected job rate.  Protect it with backups and a mount dependency so
the controller fails closed instead of starting against an empty directory.

RDS must have automated backups, point-in-time recovery, encryption, deletion
protection and a tested restore procedure.  Database reachability loss stops
new accounting-dependent work but must not cause an operator to bypass
`AccountingStorageEnforce` or start an untracked replacement database.

## 3. Network design

Use separate private subnet tiers for control, data services and compute.
Security groups reference other security groups, not broad VPC CIDRs.  Network
ACLs are a coarse second layer; host firewalls and application authentication
remain enabled.

| Source | Destination | Port/protocol | Purpose |
|---|---|---:|---|
| Approved corporate/VPN clients | public or internal API gateway | TCP 443 | TLS 1.3 and OIDC workflow/Slurm API |
| Gateway on the same REST host | `slurmrestd` loopback | TCP 6820 | Verified TLS to the role-unique REST certificate; never an SG rule |
| Gateway on the same broker host | workflow broker loopback | TCP 8088 | Private authenticated-proxy hop; never an SG rule |
| Controllers, REST daemons | controllers | TCP 6817 | Slurm controller RPC |
| Controllers and approved launch clients | compute SG | TCP 6818 | `slurmd` RPC |
| Controllers | accounting SG | TCP 6819 | Slurm accounting RPC |
| Accounting SG | RDS SG | TCP 3306 | TLS database connection |
| Controllers | lifecycle service SG | TCP 443 | Mutual-TLS desired-state request |
| Cluster instances | Route 53 resolver/NTP | UDP/TCP 53, UDP 123 | DNS and time synchronization |
| Cluster instances | VPC endpoints | TCP 443 | ECR, S3, KMS, Secrets Manager, logs, SSM |

MPI, PMIx, `srun` and application traffic can require compute-to-compute port
ranges.  Determine and configure those ranges for the chosen MPI stack.  If a
broad east-west rule is unavoidable, scope it to the compute SG and cluster
subnets; never extend it to the controller, database or client networks.

Do not permit VPC ingress to 6820, 3306, or the Podman API/socket.  Do not
install an SSH bastion by default.  Use audited SSM Session Manager as a
time-bound break-glass path if organizational policy permits it.

## 4. Identity and token boundary

The external gateway authenticates users with the organization's OIDC issuer.
It validates signature, issuer, audience, expiry/not-before and the approved
POSIX-name claim. It removes every inbound `X-SLURM-USER-*` header before
injecting trusted headers. For the workflow API it also removes
`X-Authenticated-*` and `X-Workflow-Proxy-Token`, injects identity derived
from verified claims, and authenticates to the loopback broker with the
private proxy token. The proxy sets body, request-rate, connection and
deadline limits before forwarding to the pinned Slurm REST API `v0.0.45`.
Master callbacks also traverse this gateway: its callback route accepts only
the compute security tier, strips identity/proxy headers, applies strict body
and rate limits, and forwards from loopback. The broker then validates the
one-use workflow capability; it never trusts a VPC-wide CIDR to assert users.

Use RS256/ES256 and publish public keys as JWKS.  Slurm gets only the public
JWKS file.  The identity-provider signing key never enters this image, a
controller, a compute node or Secrets Manager owned by this stack.  Disable
local `scontrol token` creation.  Tokens should normally expire within five to
fifteen minutes, and token audience must be unique to this Slurm API.

The broker token represents an ordinary workflow POSIX account, not root or
`SlurmUser`. A workload-authenticated renewal agent obtains a token with a
five-to-fifteen-minute lifetime and atomically replaces the uid-64032,
mode-0400 file. It is neither an environment value nor a static Secrets
Manager value. The gateway must have an explicit service-token path that
validates this audience/identity and does not honor a caller-selected user
header.

The master job receives only a single-use broker callback capability
constrained to one workflow and one dispatch. It never receives that Slurm
bearer token. In production, create the capability after the allocation starts
and deliver it into allocation-private storage as the job uid, mode 0400. The
current demo broker creates the capability at API-submission time and this
repository has no node delivery agent. Consequently, the production broker
Quadlet is intentionally fail-closed and must not be enabled for workflows
until the site supplies that component and queued-job TTL tests pass. Never
put the capability directory on shared storage: every demo job currently uses
uid 64032, which is not a per-workflow read boundary.

`WORKFLOW_PUBLIC_BASE_URL` must use HTTPS with a chain trusted by compute-node
system trust because the demo master does not accept a custom CA option.

Central identity (normally SSSD/LDAP) must return identical numeric UID/GID
values on controller and compute hosts and inside the compute container.
Deployment acceptance must prove `getent passwd <test-user>` and a submitted
job's real/effective IDs before admitting users.
The complete service/job identity model, ownership matrix, verification
commands and upstream references are in
[service identities, job users, ownership, and permissions](identity-and-permissions.md).

## 5. PKI separation and rotation

Use separate issuing policy, and preferably separate intermediate CAs, for:

1. user-facing gateway server certificates;
2. Slurm internal `tls/s2n` daemon certificates; and
3. lifecycle service mTLS client/server certificates.

This separation prevents a compromised compute-node Slurm certificate from
authorizing EC2 termination.  Lifecycle client certificates contain
`clientAuth` EKU and a SAN bound to exactly one cluster, for example
`spiffe://hpc.example/slurm/production-a/controller`.  The lifecycle service
requires `serverAuth`.  Certificates are short lived and renewed before one
third of their lifetime remains.

Generate private keys on the consuming host or in a workload-attested agent.
Do not distribute one private TLS key to a group of nodes.  Write a new key and
certificate to a private temporary path, validate chain/SAN/EKU, `fsync`, then
atomically rename.  Restart or reload one HA peer at a time.  During CA
rotation, distribute a bundle containing old and new issuers, rotate leaf
certificates, then remove the old issuer.

Host-side expected ownership is:

| Path class | Owner/mode | Consumers |
|---|---|---|
| `/run/slurm-secrets` directory | `root:root 0711` | search-only; only root can list/create entries |
| `/run/slurm-secrets/munge.key` | `root:root 0400` | entrypoint copies to MUNGE tmpfs |
| controller lifecycle CA/certificate | `root:root 0644` | resume/suspend hook only |
| controller lifecycle client key | numeric Slurm UID `64030`, `0400` | resume/suspend hook only |
| daemon TLS certificate/key source | `root:root 0400` | entrypoint stages both mode 0600 for one daemon role |
| OIDC JWKS/CA bundle | `root:root 0444` or `0644` | non-secret trust material |
| workflow database/proxy/JWT files | workflow UID `64032`, `0400` | broker only |
| `slurmdbd` database password | `root:root 0400` | entrypoint stages for `slurm` |

The MUNGE key is a cluster root of trust.  Create it as 1024 random bytes with
a CSPRNG, store it as a binary Secrets Manager value encrypted under a
cluster-specific KMS key, and grant each role only that secret ARN.  MUNGE has
no graceful multi-key overlap, so rotation is a coordinated maintenance event:
drain jobs, stop daemons, replace the key everywhere, start accounting then
controllers then computes, and run authentication tests before reopening.

The broker mounts its host secret directory read-only rather than binding each
file. Atomic rename then changes the directory entry observed in the container,
so request-time reload sees a renewed JWT/proxy token. A single-file bind mount
would pin the previous inode indefinitely. Broker hosts contain only their
three broker credentials beneath this directory; owner-only file modes remain
the confidentiality boundary.

## 6. AWS credentials and IMDS

Only the lifecycle service can call `RunInstances` and `TerminateInstances`.
Controllers have no EC2 mutation policy. The lifecycle policy in
`deploy/aws/lifecycle-iam-policy.json` requires a fixed launch template and
region, prevents resource overrides, constrains tag-on-create, and permits
termination only for properly tagged cluster instances. The reconciler must
pass a reviewed numeric template version and the template itself must fix AMI,
private subnets, security group, instance profile/type, encrypted volumes,
IMDSv2 and hop limit one. Never use `$Latest` or `$Default`; IAM's launch
template condition binds the template ID, not an immutable version.

The lifecycle task role cannot create or modify launch templates. Keep those
permissions in a separate change-control role, and require every launched
instance, interface and volume to carry `Name`, `SlurmCluster`,
`SlurmNodeName`, `SlurmGeneration`, `ManagedBy` and `LifecycleRequestId`.
Run IAM Access Analyzer and a real
staging launch test after substituting ARNs; EC2 authorization evaluates every
resource referenced by `RunInstances`, so an untested hand edit can either
break launch or widen it.

Compute bootstrap can read only its cluster MUNGE secret and promoted ECR
repository.  Restrict the EC2 metadata hop limit to one and block job UIDs and
job cgroups from `169.254.169.254` at the host firewall.  IMDSv2 alone is not a
job credential boundary when untrusted code runs directly on the host network.

## 7. Storage classes

Use storage by purpose rather than mounting one writable filesystem everywhere:

- controller state: durable shared POSIX filesystem, accessible only to both
  controllers, presented as uid/gid 64030 mode 0700, snapshotted and
  restore-tested;
- Slurm accounting and workflow records: encrypted Multi-AZ RDS, separate
  database users, TLS verification and point-in-time recovery;
- manifests and small demo results: EFS mounted `/srv/slurm/shared` with access
  points and least-privilege POSIX ownership; never store callback
  capabilities there;
- high-throughput datasets: FSx for Lustre linked to versioned S3 objects, or a
  site-approved equivalent;
- compute spool/job-container directories: encrypted local EBS/NVMe, never
  shared, deleted only after the Slurm node is drained; and
- logs: journald forwarded to a protected CloudWatch log group with retention
  and subscription filters; the container filesystem is not a log archive.

Mount EFS/FSx on the host before starting Quadlet.  On enforcing SELinux hosts,
use the supported `container_use_nfs`/site policy rather than recursively
relabeling a shared filesystem.  The Quadlet templates intentionally omit `:Z`
from shared mounts.

The workflow EFS access point must present its root to the broker and service
jobs as numeric uid/gid 64032, mode 0750; pre-create `.workflow-control` as
uid/gid 64032, mode 0700. Do not grant world write or rely on a local name
mapping. Separate untrusted-user storage from this service path, and remember
that POSIX mode alone is not a per-workflow boundary for jobs sharing uid
64032.

## 8. Compute lifecycle contract

Use `deploy/aws/slurm-cloud.conf.example` only after the private lifecycle API
is live. Render it into the one authoritative `slurm.conf`; the production
entrypoint rejects runtime Include directives so it can validate all security
settings. The base `standard` partition explicitly names only its FUTURE pool;
preserve that separation when adding the cloud partition.

Slurm invokes its Resume/Suspend programs as `SlurmUser`; those
programs authenticate by mTLS and submit desired state.  They do not contain
AWS SDK calls.

Resume reconciliation must:

1. expand and validate node names against an allow-listed profile;
2. allocate a monotonically increasing node generation;
3. persist intent before returning HTTP 202;
4. launch at most one tagged instance for `(cluster,node,generation)`;
5. bootstrap the digest-pinned image, MUNGE, PKI, shared mounts and Quadlet;
6. name the EC2 host and Quadlet instance exactly `aws-cpuNNNN`, leave dynamic
   FUTURE-registration variables unset, and start `slurmd` so the static
   `State=CLOUD` identity requested by Slurm is the identity that registers;
   and
7. report failure before `ResumeTimeout`; otherwise Slurm marks it down and
   requeues the requesting job.

Suspend requests are issued only after Slurm sees the node idle.  The service
still verifies ownership/generation, stops `slurmd`, waits for instance
termination and records completion.  Manual emergency scale-in uses
`scontrol power down asap <nodes>`; never terminate an EC2 instance first.

The scripts intentionally copy `SLURM_RESUME_FILE` immediately because Slurm
removes it during controller shutdown.  They cap its size, strictly parse
configuration, require TLS 1.3/mTLS, reuse one idempotency key across HTTP
retries, and cap response/time.  Distinct lifecycle invocations use distinct
keys so a future suspend of a newly created node cannot replay an old result.
The service must additionally reconcile desired state, making duplicate hook
invocations safe.

## 9. Image/host bootstrap sequence

1. Build once in CI through `scripts/build-rpms.sh`,
   `scripts/install-runtime.sh`, and `scripts/configure-runtime.sh` as defined
   in `docs/operations/build-and-runtime.md`. Verify source pins and every RPM
   against `/var/packages/BUILD-MANIFEST`, generate SBOM and vulnerability
   evidence, sign the image/provenance, and promote the same digest into
   production ECR.
2. Bake a RHEL-compatible AMI with Podman, Quadlet, AWS CLI/credential helper,
   SSSD client, time sync, log forwarding and the organization PKI agent.
3. Verify the image signature/digest before allowing the Quadlet image unit to
   pull.  Never put registry passwords in user data.
4. Mount durable/shared filesystems through native systemd mount units.
5. Retrieve the role's minimum Secrets Manager values using its instance role,
   write them beneath tmpfs `/run/slurm-secrets`, then discard response buffers.
   The optional `deploy/aws/materialize-aws-secret` helper and
   `slurm-secret@.service` implement an atomic file-only fetch; wire only the
   role-specific secret instances into the site's provisioning target.
   REST hosts do not fetch MUNGE in the JWT-only topology. Broker Slurm JWTs
   come from the short-lived renewal agent, not Secrets Manager.
6. Obtain a workload-attested, role-unique TLS certificate and create
   `/run/slurm-pki` with
   the ownership above.
7. Render one complete Slurm configuration plus the role environment files,
   then install Quadlet using the immutable image digest. The installer does
   not enable units.
8. Start accounting, then primary/backup controllers, REST gateways/brokers,
   and finally one canary compute node.

User data is observable to principals with instance permissions and often
persists in launch templates.  It may contain non-secret identifiers and
paths, never a key, password, bearer token, certificate private key, or secret
value.

## 10. Production acceptance gates

Before DNS cutover or admitting data, prove all of the following in the target
account:

- no security group permits `0.0.0.0/0` or `::/0` except approved gateway 443;
- raw `slurmrestd`, controller, database and lifecycle ports are unreachable
  from client and compute subnets except where the matrix permits;
- forged, wrong-audience, expired and caller-supplied Slurm identity headers
  are rejected;
- `slurmrestd` rejects clear text on loopback, its gateway verifies the REST
  certificate/IP SAN, and port 6820 is unreachable from the VPC;
- image history, environment, logs, `/var/packages` and instance user data
  contain no secrets;
- controller failover preserves queued/running job state;
- RDS and controller-state restores succeed in an isolated cluster;
- a node launches under load, registers before the measured timeout, runs a
  non-root test job with cgroup limits, drains and terminates safely;
- duplicate lifecycle requests create one instance, and a later generation of
  the same node can launch/suspend normally;
- the resumed `aws-cpuNNNN` registers as that exact requested CLOUD identity,
  never as an unrelated FUTURE node;
- capability delivery occurs after allocation start, a queued master cannot
  expire before delivery, and concurrent uid-64032 jobs cannot read or replay
  each other's callback capability;
- IMDS and unrelated secret ARNs are inaccessible from a batch job; and
- CloudTrail, scheduler, gateway, broker and lifecycle request IDs support one
  end-to-end incident timeline without logging credentials.

## 11. Authoritative references

- [Slurm REST daemon and mandatory `https://` listeners](https://slurm.schedmd.com/slurmrestd.html)
- [Slurm internal TLS configuration](https://slurm.schedmd.com/tls.html)
- [Slurm power-saving lifecycle](https://slurm.schedmd.com/power_save.html)
- [AWS control of EC2 launch-template usage](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/use-launch-templates-to-control-launching-instances.html)
- [AWS `RunInstances` and launch-template IAM examples](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ExamplePolicies_EC2.html)
