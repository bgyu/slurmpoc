# Production Slurm operations runbook

Use this runbook with the approved production design and the AWS contract.  A
change is not complete merely because a container is running: verify scheduler
state, accounting, authentication, isolation and rollback at every stage.

## Release artifact prerequisite

Build and promote the image through the complete procedure in
`docs/operations/build-and-runtime.md`. In summary, CI must start from a clean,
reviewed Git commit; build the RPMs from the pinned version/digest file; verify
the exact manifest and installed runtime subset; configure the immutable
filesystem without secrets; run local acceptance; generate SBOM and
vulnerability evidence; sign the image/provenance; and publish one immutable
digest. Staging and production consume that digest. Runtime hosts never build
Slurm or retrieve upstream source archives.

The release record must retain the Git commit, source pins, complete
`BUILD-MANIFEST`, OCI digest, SBOM digest, scan-policy result, signature
identity, acceptance result and any database/state compatibility decision.
It must also retain the repository ID and NEVRA for every resolved build and
runtime dependency according to
`docs/operations/dependency-provenance.md`; RPM `Vendor` metadata alone is not
proof of the repository used by DNF.

## 1. Initial deployment

1. Record the change ticket, exact Git commit, OCI image digest, SBOM digest,
   signature identity, Slurm/MUNGE versions and database migration impact.
2. Provision network, DNS, shared storage, RDS, KMS, secret ARNs, private PKI,
   OIDC gateways, lifecycle service, broker JWT renewal and allocation-private
   capability delivery through reviewed IaC.
3. Create centralized POSIX users/groups and confirm numeric identity parity on
   every role.  Never create production users opportunistically in a container.
4. Render one complete, root-owned `slurm.conf` under
   `/etc/slurm-production/config`; runtime `Include` directives are rejected.
   Preserve the base standard partition's explicit FUTURE-node membership
   when adding the AWS cloud pool. Validate the rendered controller and
   separate `slurmdbd` template
   against the exact image in staging, and verify no service/job identity can
   write them.
5. Materialize runtime secrets and certificates in host tmpfs.  Check owner,
   mode, expiry, SAN/EKU and issuer without printing their contents.
6. Mount controller state and shared data before starting containers.  An empty
   state directory during recovery is a stop condition, not permission to form
   a new cluster.
7. Install the digest-pinned Quadlets.  Inspect generated units before enabling
   them.
8. Start RDS, primary `slurmdbd`, backup `slurmdbd`, primary controller and
   backup controller, in that order.  Confirm only one controller is primary.
9. Run the one-time audited `sacctmgr` bootstrap for cluster, accounts, users,
   associations and QOS.  The normal container entrypoint must not mutate this
   policy on every restart.
10. Start private REST daemons and same-host authenticating gateways. Prove the
    REST listener rejects clear text and the gateway verifies its loopback TLS
    certificate. Start workflow gateways/brokers only if post-allocation,
    per-master capability delivery is implemented; the repository template
    otherwise fails closed. Run negative JWT/header tests before a load
    balancer health check can pass.
11. Start one canary compute node; prove non-root UID, cgroup CPU/RAM limits,
    private `/tmp`, accounting and output ownership.  Expand the pool only after
    the canary is clean.

## 2. Routine health

Alert on, at minimum:

- controller/database failover, restart loops and state filesystem latency;
- pending jobs by reason/age, scheduler cycle duration and RPC saturation;
- REST active connections, request queue, 4xx/5xx/rate-limit counts and latency;
- lifecycle desired/actual divergence, launch/registration p50/p95/p99,
  `POWERING_UP` past budget and orphan-tagged instances;
- MUNGE failures, TLS/JWKS/certificate expiry and clock skew;
- cgroup OOM, node `DRAIN`, `DOWN`, `INVALID_REG`, `NOT_RESPONDING` states;
- RDS connections, storage, replica/failover status and backup age; and
- workflow intents stuck between master, dispatch, worker array and finalizer.

Also alert before a broker JWT, callback capability or daemon certificate has
less remaining lifetime than the measured queue/renewal budget. A capability
created before a long queue wait is a fault, not a reason to extend it to days.

Dashboards and alerts must use cluster/workflow/job/request IDs.  Redact
Authorization, `X-SLURM-USER-TOKEN`, callback capability, database URL and
resume context fields at ingestion.

## 3. Manual node operations

### Local integration environment

The local commands are lock-protected and manage only containers carrying this
repository's ownership labels:

```bash
scripts/nodes/add.sh elastic-1
scripts/nodes/list.sh
scripts/nodes/drain.sh --timeout 300 --reason 'change CHG-1234' elastic-1
scripts/nodes/remove.sh --timeout 300 --reason 'change CHG-1234' elastic-1
```

`add.sh` reuses the controller's exact `/shared` named volume (or a verified
bind inside this repository), so jobs do not get a split view of manifests.
It uses the immutable image's validated compute entrypoint and drops every
container capability except the explicit simulation job-launch set.  `slurmd`
still runs as container root so it can enter the workflow UID; rootless Podman
maps that root to the unprivileged WSL user and is not production isolation.
`remove.sh` refuses a reservation or active job, stops `slurmd`, deletes its
dynamic-normal controller record, then deletes only that node's private
metadata/spool. Production FUTURE/CLOUD identities use the separately
authorized lifecycle path and are never managed by these local scripts.

### AWS production environment

Normal demand is automatic.  For controlled scale-in:

```bash
scontrol power down asap aws-cpu0042 Reason='change CHG-1234'
scontrol show node aws-cpu0042
squeue --nodes=aws-cpu0042 --states=all
```

Wait for jobs to leave and for lifecycle reconciliation to report terminated.
Do not call `TerminateInstances` manually.  If emergency termination is
unavoidable, capture affected jobs/state, cancel or requeue explicitly, use the
break-glass role, and reconcile Slurm before releasing the incident.

For a stuck resume: inspect lifecycle operation/request ID, launch-template
failure, secret/PKI retrieval, mounts, image verification and `slurmd`
registration. Verify that host name, Quadlet instance and static Slurm identity
are the same `aws-cpuNNNN` and that no FUTURE registration variables were set.
Do not repeatedly issue `RESUME`; Slurm will mark the node down
after `ResumeTimeout`.  Fix the cause, terminate any orphan, then run:

```bash
scontrol update NodeName=aws-cpu0042 State=RESUME
```

only after desired and actual infrastructure state agree.

## 4. Configuration change

1. Render and validate the complete configuration offline against the exact
   image digest; never deploy a fragment or runtime Include.
2. Back up controller state/config and record `scontrol show config`.
3. Apply non-disruptive config to backup first where supported, then primary;
   use `scontrol reconfigure` only for parameters documented as reloadable.
4. For listener, TLS plugin, cgroup or identity changes, use a rolling restart
   and canary compute.  Drain compute nodes before restarting `slurmd`.
5. Run REST submission, accounting, array, cancellation and lifecycle tests.
6. Roll back the config commit/digest, not an ad-hoc file edit.

Never change a node's CPUs, memory or GRES while it is registered/running.
Create a new approved profile and drain/replace nodes.

## 5. Image and Slurm upgrade

Read release notes for protocol, state-file, REST schema, accounting schema and
plugin compatibility.  Back up and restore-test before the first upgraded
daemon.  The conservative order is database backup, accounting daemons,
controllers, REST/broker, then drained compute batches, subject to the selected
Slurm version's documented compatibility window.

Change each source version only with its matching SHA-256. Keep
`build/versions.conf`, Dockerfile defaults and `cluster-up.sh` local defaults
in sync, build into a new empty artifact directory, and require the installer
to match manifest source pins to the approved version file. Never edit a
manifest to make a check pass. Re-run the full build/runtime and target-account
acceptance gates before signing the replacement digest.

Keep the previous signed digest and AMI available through the rollback window.
Do not downgrade after an irreversible state/database migration without the
tested restore procedure.  Never use `latest` or auto-update production
Quadlets.

## 6. Secret and certificate rotation

- OIDC signing keys: publish old+new JWKS, then rolling-restart REST,
  controller and accounting peers so each role stages the new bundle (their
  individual file binds otherwise retain the prior inode). Start signing with
  new, wait longer than maximum token TTL/cache, remove old, repeat the rolling
  restart and test wrong-key rejection.
- TLS CA: distribute dual trust, rotate one HA peer/canary node at a time,
  verify, then remove old trust.
- Database password: create/enable a second credential if supported, rotate
  consumers one at a time, revoke old, and audit failed authentication.
- Broker ordinary-user JWT: renewal agent atomically replaces the private file;
  keep lifetime minutes, not days.
- Gateway-to-broker proxy token: use a distinct value per host, generate at
  least 256 random bits, stage the replacement to that host's gateway and
  broker, switch the gateway and revoke the old value without ever logging
  either token.
- MUNGE: schedule a full trust-domain maintenance as described in the AWS
  deployment document.  Never attempt an uncoordinated rolling key change.

On suspected JWT signing-key compromise, remove the key from JWKS, stop API
admission until caches expire/reload, revoke issuer credentials, review all API
and job activity, and rotate related broker credentials.  On MUNGE compromise,
isolate the cluster network and rotate the entire cluster trust domain.

## 7. Backup and restore

Back up site configuration and IaC in Git, controller state with filesystem
snapshots, Slurm/workflow databases with RDS PITR, and manifests/results using
the storage service's versioning/backup policy.  Secrets are backed up by their
managed secret/KMS lifecycle, not copied into the backup archive.

Quarterly, restore into an isolated VPC with different DNS and disabled
lifecycle hooks. Confirm controller state, `sacct` and associations/QOS. Run a
synthetic workflow only after the isolated environment has the same
allocation-private capability delivery control; never weaken that boundary to
make a restore test pass. A snapshot that has not passed restore testing is not
a recovery plan.

## 8. Safe shutdown and disaster recovery

For planned shutdown, stop API admission, stop new submissions, drain/cancel by
policy, power down compute, verify accounting flush, stop the backup then
primary controller, then accounting.  Preserve controller state and databases.

During controller failure, first determine whether the old primary is truly
stopped or fenced.  Starting backup against shared state while the old primary
still writes is a split-brain risk.  During regional recovery, restore state
and databases consistently, issue new PKI identities/DNS, leave cloud resume
disabled, validate scheduler/accounting, then enable a small compute canary.

## 9. Production stop conditions

Do not open the cluster to users if any of these is true:

- secrets are in Git, image layers, environment variables, command lines or
  user data;
- raw `slurmrestd` or database ports are client reachable;
- raw broker HTTP is VPC-reachable or a gateway can forward caller-supplied
  identity headers;
- compute jobs can reach IMDS or read another job's files/callback capability;
- callback capabilities are created while a master is still queued, stored on
  shared storage, or no node-attested per-allocation delivery exists;
- cgroup v2 enforcement, UID/GID parity or clock synchronization is absent;
- controller peers do not share one tested state store;
- lifecycle IAM can terminate untagged/other-cluster instances, or the
  reconciler accepts `$Latest`/`$Default` launch-template versions;
- node removal can bypass drain/reservation checks; or
- backup restore, controller failover and ambiguous REST retry tests have not
  passed in the target environment.
