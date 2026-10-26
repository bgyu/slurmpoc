# Service identities, job users, ownership, and permissions

Status: production security reference for the implementation validated on
2026-10-27

## 1. Scope and security objective

This document defines the service-user, job-user, configuration ownership,
secret ownership, mutable-state, and container privilege contract for this
repository. It also explains why a `podman exec` shell can report `root` even
when the Slurm daemon is running as an unprivileged account.

The production objectives are:

1. immutable code and policy are owned by root and are not writable by a
   daemon or job;
2. each network-facing or stateful service has a distinct non-login identity;
3. mutable state is writable only by the service that owns it;
4. secrets are readable only by their intended service;
5. `slurmd` alone retains root because it must establish cgroups, namespaces,
   supplementary groups, and the submitting job's UID/GID;
6. job applications execute as an ordinary POSIX user, never as `root`; and
7. Podman control and `podman exec` remain administrator operations, not a job
   submission interface for cluster users.

The current implementation mostly satisfies the daemon model. One local-only
submission path still needs correction: `scripts/submit-job.sh` invokes
`sbatch` through a default-root `podman compose exec`, so a conventional demo
job can be submitted as root. Section 8 documents the required correction and
the regression tests that should accompany it.

## 2. Four identities that must not be confused

A containerized Slurm role can expose four different identities:

| Identity | Meaning | Example |
|---|---|---|
| Image/container default user | User Podman assigns when starting a process without an override | Root, because the shared image has no Dockerfile `USER` |
| Container PID 1 | Entrypoint or supervisor process | Root shell that validates and stages files |
| Daemon identity | User that owns the actual service process | `slurm`, `slurmrestd`, `munge`, or `mysql` |
| Job identity | UID/GID assigned to `slurmstepd` and application processes | `workflow` in the demo, or an authenticated human user in production |

Seeing one of these as root does not prove that the others are root. Security
review must inspect the actual daemon process and a launched job.

### 2.1 Why `podman exec ... whoami` reports root

`podman exec` creates a new process. Without `--user`, it uses the image or
container default user; if none is configured, that normally defaults to
root. In rootless Podman, container UID 0 maps through a user namespace and
normally corresponds to the unprivileged host user that invoked Podman.

This behavior is documented by Podman:

- [Podman `exec` manual](https://docs.podman.io/en/latest/markdown/podman-exec.1.html)

These commands inspect different things:

```bash
# Creates a new root diagnostic process; not a daemon identity test.
podman exec controller whoami

# Creates a diagnostic process as the named service account.
podman exec --user slurm controller id

# Inspects the users of processes that are already running.
podman exec controller ps -eo user,uid,gid,pid,ppid,comm,args
podman exec slurm-sim_compute001_1 \
  ps -eo user,uid,gid,pid,ppid,comm,args
```

When troubleshooting a rootful production host, `podman top` can additionally
show both container and host identities. Do not grant ordinary HPC users
permission to run these commands. A principal that controls a rootful Podman
container or socket can create privileged processes and mounts and must be
treated as a host administrator.

## 3. Service account design

Service accounts must be system accounts with no password, no interactive
login shell, no SSH authorization, and a narrowly scoped state directory.
Do not reuse a service account as a human account, and do not run two exposed
services under one identity merely because they are packaged together.

The image creates or validates these fixed identities in
[`configure-runtime.sh`](../../scripts/configure-runtime.sh#L142):

| Service identity | UID/GID | Intended process | Login shell |
|---|---:|---|---|
| `munge` | 64029 | `munged` | `nologin` |
| `slurm` | 64030 | `slurmctld`, `slurmdbd` | `nologin` |
| `slurmrestd` | 64031 | `slurmrestd` | `nologin` |
| `workflow` | 64032 | Workflow broker and trusted demo workflow jobs | `nologin` |
| `mysql` | 27 | Integration-only MariaDB | `nologin` as supplied by the RPM |

Fixed IDs are useful for image-owned files, bind mounts, EFS access points,
tmpfiles rules, and host-side secret delivery. Provisioning must reserve them
and fail on a collision; it must never silently renumber an unrelated account.

Human/job identities are a separate namespace. They should come from the
site's authoritative POSIX identity service and must not overlap these service
IDs.

### 3.1 Recommended process identities

| Process | Production identity | Reason and authoritative source |
|---|---|---|
| `munged` | Dedicated `munge` user | MUNGE recommends a unique non-privileged system account with a non-login shell. [MUNGE QUICKSTART](https://github.com/dun/munge/blob/master/QUICKSTART) |
| `slurmctld` | Dedicated `slurm` user | SchedMD recommends a non-root `SlurmUser`. [SchedMD `slurm.conf`](https://slurm.schedmd.com/slurm.conf.html#OPT_SlurmUser) |
| `slurmdbd` | Dedicated `slurm` user | It reads protected accounting configuration and writes Slurm accounting state/logs as `SlurmUser`. [SchedMD `slurmdbd.conf`](https://slurm.schedmd.com/slurmdbd.conf.html) |
| `slurmrestd` | Unique `slurmrestd` user, not root or `SlurmUser` | SchedMD explicitly requires a separate unprivileged REST identity. [REST quick start](https://slurm.schedmd.com/rest_quickstart.html), [REST details](https://slurm.schedmd.com/rest.html) |
| Workflow broker | Unique `workflow` user | Limits a broker compromise to its database, capability, log, and workflow paths |
| MariaDB | Package `mysql` user | Database files must not be writable by Slurm or jobs |
| `slurmd` | Root | Required to enter the submitting user's identity and manage node resources. [SchedMD administrator guide](https://slurm.schedmd.com/quickstart_admin.html) |
| User application | Submitting user's UID/GID | The signed job credential binds the allocation and steps to the submitted UID/GID. [SchedMD job-launch design](https://slurm.schedmd.com/job_launch.html) |

### 3.2 Current startup implementation

The common entrypoint starts as root so it can validate bind-mounted inputs,
copy credentials into tmpfs with narrow ownership, and then start child
processes using the intended account. The relevant implementations are:

- MUNGE: `runuser --user munge` at
  [`cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L323);
- MariaDB: `mariadbd --user=mysql` at
  [`cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L473);
- `slurmdbd`: `runuser --user slurm` at
  [`cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L620);
- `slurmctld`: `runuser --user slurm` at
  [`cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L659);
- `slurmd`: intentionally remains root at
  [`cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L692);
- `slurmrestd`: `runuser --user slurmrestd` at
  [`cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L738); and
- local broker: `runuser --user workflow` at
  [`cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L793).

The simulation-only token renewer retains a root helper so it can atomically
publish an owner-correct token, but it invokes token creation as `slurm`, holds
no signing key, and is forbidden in the production profile. See
[`dev-token-renewer.sh`](../../scripts/dev-token-renewer.sh#L1). Production
uses a workload-authenticated renewal component running with only the access
needed to replace the broker's UID-64032 token file.

The acceptance suite verifies those real child-process users with `pgrep -u`
and separately launches a job that must report UID 64032. See
[`scripts/acceptance-test.sh`](../../scripts/acceptance-test.sh#L81).

The root PID 1 supervisor remains alive to forward signals and monitor its
children. That is materially different from running `slurmctld`, `slurmdbd`,
or `slurmrestd` as root, but it is still a root process and should be removed
from production roles when host-side credential staging makes it unnecessary.
Section 11 describes that hardening path.

## 4. Why `slurmd` runs as root but jobs do not

SchedMD states that `SlurmdUser` should remain root in almost all cases so the
node daemon can launch jobs as the submitting user. The Slurm administrator
guide is stronger: because `slurmd` initiates and manages user jobs, it must
execute as root.

Authoritative sources:

- [SchedMD `SlurmdUser`](https://slurm.schedmd.com/slurm.conf.html#OPT_SlurmdUser)
- [SchedMD Quick Start Administrator Guide](https://slurm.schedmd.com/quickstart_admin.html)
- [SchedMD job-launch design](https://slurm.schedmd.com/job_launch.html)

The expected launch flow is:

```text
authenticated submitter UID/GID
            |
            v
slurmctld creates signed job/step credential
            |
            v
root slurmd validates credential on allocated node
            |
            v
slurmstepd creates cgroup and namespaces
            |
            v
setgroups + setgid + setuid to submitted POSIX identity
            |
            v
application runs as ordinary user
```

Production additionally uses `task/cgroup`, `proctrack/cgroup`, constrained
RAM/CPU/swap settings, and `job_container/tmpfs`. SchedMD documents that the
job-container plugin gives each job a private `/tmp` and `/dev/shm`, while its
base directory remains root-managed:

- [job_container/tmpfs guide](https://slurm.schedmd.com/job_container_tmpfs.html)
- [`job_container.conf` reference](https://slurm.schedmd.com/job_container.conf.html)

Making `slurmd` unprivileged would not improve security if the workaround is
to run every job as root or disable cgroup, group, and namespace enforcement.
The correct boundary is a root node daemon on a dedicated compute host that
drops every job to a validated ordinary UID/GID.

## 5. Rootless simulation versus rootful production

The local WSL stack uses rootless Podman. Container root is mapped into the
unprivileged Podman user's user namespace, so it is not host UID 0. This is a
useful protection for a simulation, but it is not proof of a secure multi-user
HPC compute boundary.

The production compute contract requires:

- rootful Podman on a dedicated compute host;
- a non-remapped root identity for `slurmd`;
- a writable/delegated unified cgroup v2 hierarchy;
- SELinux enforcing with reviewed container/cgroup policy;
- a read-only container root filesystem;
- no-new-privileges and a capability allowlist rather than `--privileged`;
- job-private namespaces and temporary directories; and
- matching numeric job identities on every node and filesystem.

The runtime refuses a production compute container whose user namespace or
cgroup hierarchy does not satisfy this contract in
[`require_production_compute_isolation`](../../scripts/cluster-entrypoint.sh#L678).
The production Quadlet grants only its reviewed capability set in
[`slurmd@.container`](../../deploy/quadlet/slurmd@.container#L13).

Because the compute role has `CAP_SYS_ADMIN`, host cgroup access, and job
launch powers, a container escape remains a host-security event. Compute hosts
must be dedicated to this Slurm trust domain; do not colocate unrelated
services or give job users access to Podman.

## 6. Configuration ownership principles

Apply these rules consistently:

1. **Root owns policy.** Configuration, executable hooks, installed code, unit
   files, and trust anchors are owned by root and are not group/world writable.
2. **The daemon reads but cannot rewrite policy.** Where a daemon needs read
   access, use public readability for non-secret cluster configuration or a
   dedicated read group for sensitive configuration.
3. **The service owns mutable state.** Controller state, database state,
   broker state, PID locations, and logs are owned only by the consuming
   service.
4. **Root-owned parent directories prevent replacement attacks.** A mode-0400
   file is insufficient if its daemon-writable parent permits rename or
   replacement.
5. **Secrets use owner-only modes.** Private keys, passwords, bearer tokens,
   MUNGE keys, and JWT signing material use 0400 or 0600.
6. **Public does not mean writable.** CA certificates, public JWKS, and
   `slurm.conf` can be 0644 when required, but remain root-owned.
7. **Mounted configuration is read-only.** Host inputs use `:ro`, and the
   container root filesystem is read-only in production.
8. **No application writes under `/etc` or `/opt`.** Writable data belongs in
   `/run`, `/var/lib`, `/var/spool`, `/var/log`, or a purpose-specific mounted
   data root.

The entrypoint validates that production inputs are real non-symlink files,
not group/world writable, and root-owned before staging them. See
[`validate_input_file`](../../scripts/cluster-entrypoint.sh#L78).

## 7. Recommended file and directory matrix

### 7.1 Configuration, trust, and secrets

| Path class | Owner | Mode | Rationale/source |
|---|---|---:|---|
| `slurm.conf` | `root:root` or `root:slurm` | 0644 | SchedMD requires it to be readable to users/nodes and not writable by regular users. [`slurm.conf` file permissions](https://slurm.schedmd.com/slurm.conf.html#SECTION_FILE-AND-DIRECTORY-PERMISSIONS) |
| `cgroup.conf` | `root:root` | 0644 | Non-secret root-owned scheduling/isolation policy |
| `job_container.conf` | `root:root` | 0644 | Non-secret root-owned namespace policy |
| `slurmdbd.conf` containing credential | `slurm:slurm` 0600, or `root:slurm` 0640 | 0600/0640 | Explicit SchedMD requirement. [`slurmdbd.conf`](https://slurm.schedmd.com/slurmdbd.conf.html) |
| MUNGE key | `munge:munge` | 0400 or upstream-compatible 0600 | MUNGE requires ownership by the daemon identity; this project uses stricter 0400. [MUNGE QUICKSTART](https://github.com/dun/munge/blob/master/QUICKSTART) |
| HS256 JWT key | `slurm:slurm` or `root:slurm` | 0400 | SchedMD recommends SlurmUser/root ownership and 0400. [JWT guide](https://slurm.schedmd.com/jwt.html) |
| Public JWKS | `root:root` input; staged owner is consuming daemon | 0644 input, 0400 staged | Public key material is not secret, but changing it changes who can mint cluster identities. [JWT guide](https://slurm.schedmd.com/jwt.html) |
| TLS CA certificate | `root:root` | 0644 | Must be readable by Slurm components. [Slurm TLS](https://slurm.schedmd.com/tls.html) |
| Daemon TLS certificate/key | Individual daemon user | 0600 | SchedMD specifies role-only access and unique role pairs. [`TLSParameters`](https://slurm.schedmd.com/slurm.conf.html#OPT_TLSParameters) |
| Database password or token helper | `root:root` source; staged for `slurm` only | 0400 or executable 0500 | Only `slurmdbd` may read/execute it |
| Broker database password | `workflow:workflow` | 0400/0600 | Broker-only credential |
| Broker Slurm JWT | `workflow:workflow` | 0400 | Short-lived ordinary-user credential, atomically replaced |
| Gateway-to-broker token | `workflow:workflow` for broker copy | 0400 | Distinct per host; never an environment value |
| Lifecycle client key | `slurm:slurm` | 0400 | Resume/suspend hooks execute as `SlurmUser` |
| Installed scripts/hooks | `root:root` | 0755, parent root-owned | A daemon/job must not rewrite code that a privileged process executes |

Current staging behavior for config, JWT/JWKS, TLS and MUNGE is implemented
at [`cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L203). The
runtime configuration phase installs immutable assets at
[`configure-runtime.sh`](../../scripts/configure-runtime.sh#L150).

### 7.2 Mutable directories

| Directory | Owner | Mode | Notes |
|---|---|---:|---|
| `/var/spool/slurmctld` | `slurm:slurm` | 0700 | `StateSaveLocation`; shared by HA controllers where required |
| `/var/spool/slurmd` | `root:root` | 0755 | SchedMD requires root write and mode 0755 so job scripts can execute |
| `/var/lib/slurm/job_container` | `root:root` | 0755 | Root-managed per-node namespace base |
| `/run/slurm/config` | `root:slurm` | 0755; files narrower | Root-owned parent prevents daemon replacement of staged policy |
| `/run/slurm/pids` | Service-appropriate | 0755 | PID files only; production role images may use separate role-specific paths |
| `/var/log/slurm` | `slurm:slurm` for controller/DBD | 0750, files 0640 | Root-owned or role-specific location for compute logs is also acceptable |
| `/var/lib/munge` | `munge:munge` | 0700 | MUNGE private state |
| `/run/munge` | `munge:munge` | 0755 | Allows clients to traverse to the MUNGE socket |
| `/var/lib/slurmrestd` | `slurmrestd:slurmrestd` | 0750 | REST-private mutable state |
| `/var/lib/workflow` | `workflow:workflow` | 0750 | Broker state; database files are owner-only |
| Workflow capability directory | `workflow:workflow` | 0700 | Never shared between mutually untrusted jobs |
| User home/job directory | Actual user UID/GID | 0700 or site project ACL/setgid policy | Never owned by `slurm`, root, or the workflow broker merely for convenience |

SchedMD's authoritative directory requirements, including
`StateSaveLocation` and `SlurmdSpoolDir`, are in the
[`slurm.conf` file and directory permissions section](https://slurm.schedmd.com/slurm.conf.html#SECTION_FILE-AND-DIRECTORY-PERMISSIONS).

The repository's image-time matrix is also recorded in
[build-and-runtime.md](build-and-runtime.md#62-directory-ownership-and-modes).

## 8. Preventing root jobs

There are four independent controls. Production should use all of them:

1. authenticate the caller as a non-root POSIX identity;
2. never issue a Slurm JWT or trusted proxy identity for root;
3. set `DisableRootJobs=YES`; and
4. require an approved Slurm accounting association/QOS.

The production configuration already sets:

```text
DisableRootJobs=YES
AccountingStorageEnforce=associations,limits,qos,safe
```

See [`config/slurm.production.conf`](../../config/slurm.production.conf#L24).
SchedMD documents `DisableRootJobs` in
[`slurm.conf`](https://slurm.schedmd.com/slurm.conf.html#OPT_DisableRootJobs)
and association enforcement in the
[Resource Limits guide](https://slurm.schedmd.com/resource_limits.html#OPT_AccountingStorageEnforce).

Do not create a root association, do not allow the identity provider to emit a
root Slurm identity, and do not use a root/`SlurmUser` proxy token without a
gateway that performs explicit user authorization. SchedMD describes JWT
creation as equivalent to full cluster root trust:

- [SchedMD JWT authentication: full root trust](https://slurm.schedmd.com/jwt.html#FULL-ROOT-TRUST-IN-JWT-CREATION)

### 8.1 Known local `submit-job.sh` gap

The local script currently executes:

```bash
podman compose -f compose.yaml exec -T controller \
  sbatch --chdir=/shared/jobs ...
```

Because the image has no default `USER`, this `sbatch` process runs as root.
The simulation `slurm.conf` does not currently set `DisableRootJobs=YES`.
This means the conventional local demo path can submit a root job even though
the REST workflow path correctly submits as UID 64032.

The correction should be implemented as one reviewed change:

1. run the local `sbatch` command as `workflow` or a dedicated demo job user;
2. set `DisableRootJobs=YES` in the simulation configuration;
3. add an acceptance test that invokes `submit-job.sh` and asserts the job's
   UID is the expected non-root value; and
4. add a negative test proving root job submission is rejected.

A safe direct diagnostic form is:

```bash
podman exec --user workflow -i controller \
  sbatch --chdir=/shared/jobs < examples/hostname.sbatch
```

This is for the local simulation only. Production users must submit through a
login/submission node or the authenticated REST gateway, not through Podman.

## 9. Human users and the workflow service account

### 9.1 Single application service model

The current demo broker submits its master, worker array and finalizer using a
short-lived token for the ordinary `workflow` account. Jobs therefore run as
UID/GID 64032, not root. This model is appropriate when one trusted application
owns all work and application-level tenant controls are sufficient.

The acceptance test proves `slurmd` changes the job to UID 64032 at
[`scripts/acceptance-test.sh`](../../scripts/acceptance-test.sh#L103).

### 9.2 Multi-user HPC model

If application processes and outputs must belong to each authenticated human,
the workflow service account is not enough. The production design must submit
each job under that human's authorized POSIX identity and preserve that
identity when the master asks the broker to dispatch workers.

Required controls include:

- authoritative mapping from OIDC identity to one approved POSIX username;
- stable numeric UID, primary GID, and supplementary groups;
- the same mapping on controllers, submission services, compute nodes and
  shared filesystems;
- a Slurm accounting association and QOS for the user/account;
- per-user or per-allocation callback capability delivery;
- no caller-controlled `X-SLURM-USER-*` headers; and
- no long-lived administrator or `SlurmUser` token delivered to a job.

SchedMD's troubleshooting guide requires users to be configured on the
controller and compute nodes, with consistent identities:

- [SchedMD Troubleshooting Guide](https://slurm.schedmd.com/troubleshoot.html)

MUNGE also requires consistent UID/GID mappings for authenticating users
within its security realm:

- [MUNGE QUICKSTART](https://github.com/dun/munge/blob/master/QUICKSTART)

Use a centralized POSIX identity service such as LDAP/SSSD, or use Slurm's
complete JWT client-identity claims only after a separate security review.
SchedMD documents `use_jwt_client_ids` and `use_jwt_client_ids_only` in the
[JWT guide](https://slurm.schedmd.com/jwt.html#LOCAL-USER-LOOKUP-CONTROL).
Never create or renumber local human accounts opportunistically from an
untrusted token.

## 10. Prolog, epilog, and hook permissions

Privileged hooks are code execution boundaries:

- compute `Prolog` and `Epilog` run as `SlurmdUser`, normally root;
- `PrologSlurmctld` and `EpilogSlurmctld` run as `SlurmUser`;
- task prolog/epilog hooks run as the user task identity; and
- resume/suspend programs run as `SlurmUser` when executed by `slurmctld`.

The authoritative execution matrix is the
[SchedMD Prolog and Epilog Guide](https://slurm.schedmd.com/prolog_epilog.html).

Production requirements:

- hook files and every parent directory are root-owned and not group/world
  writable;
- use absolute paths and a fixed minimal `PATH`;
- never evaluate job-controlled text as shell code;
- validate all job/node identifiers against narrow grammars;
- do not put secrets in arguments, environment, stdout or logs;
- cap input size and execution time;
- avoid calling Slurm commands recursively from prolog/epilog scripts; and
- make operations idempotent because hooks may be retried around failures.

The lifecycle client follows these rules in
[`deploy/lifecycle/lifecycle-common.sh`](../../deploy/lifecycle/lifecycle-common.sh).

## 11. Container hardening and future privilege reduction

Production Quadlets already apply:

- `ReadOnly=true`;
- `NoNewPrivileges=true`;
- `DropCapability=all` followed by a role-specific allowlist;
- read-only configuration and secret mounts;
- tmpfs for `/run`, `/tmp`, and logs where appropriate;
- PID and memory limits; and
- health checks and explicit startup prerequisites.

The broker is the strongest example: it starts directly with `User=64032`,
`Group=64032`, and no capabilities in
[`workflow-broker.container`](../../deploy/quadlet/workflow-broker.container#L7).

The next hardening step is to eliminate the root supervisor from every role
except compute:

1. stage/validate role files with a host systemd unit, systemd credentials, or
   a narrowly scoped root helper;
2. run MUNGE as a host or separate unit and mount only its socket where
   practical;
3. mount already owner-correct daemon certificates, JWKS and configuration;
4. set Quadlet `User=`/`Group=` directly for controller, `slurmdbd`, and
   `slurmrestd`; and
5. remove `CAP_CHOWN`, `CAP_SETUID`, `CAP_SETGID`, `CAP_DAC_OVERRIDE`, and the
   shell supervisor from those role containers.

`slurmd` remains a deliberate exception. It retains a reviewed capability
allowlist and root identity, never `--privileged`. Remove a capability only
after exercising cgroups, namespaces, MPI, GRES, prolog/epilog and job cleanup
on an SELinux-enforcing staging host.

## 12. Verification and acceptance

### 12.1 Inspect daemon processes

With the local stack running:

```bash
for container in \
  slurm-sim_database_1 slurm-sim_slurmdbd_1 controller \
  slurm-sim_restd_1 slurm-sim_broker_1 slurm-sim_compute001_1; do
  printf '\n[%s]\n' "$container"
  podman exec "$container" ps -eo user,uid,gid,pid,ppid,comm,args
done
```

Expected daemon users:

```text
mariadbd    mysql
munged      munge
slurmdbd    slurm
slurmctld   slurm
slurmrestd  slurmrestd
python3     workflow
slurmd      root
```

A root `cluster-entrypoint` process can coexist with these children until the
future hardening in Section 11 is completed.

### 12.2 Prove job identity

For the trusted local application account:

```bash
podman exec controller runuser --user workflow -- \
  srun --immediate=30 --nodes=1 --ntasks=1 \
  /bin/sh -c 'id -a; umask; stat -c "%u:%g %a %n" /tmp'
```

Production acceptance must submit through the same login/REST boundary users
will use and record:

```bash
id -a
getent passwd "$USER"
getent group "$(id -g)"
stat -c '%u:%g %a %n' "$HOME" "$SLURM_SUBMIT_DIR"
```

Reject the deployment if the job is UID 0, the UID/GID differs across nodes,
supplementary groups are incomplete, the job owns Slurm state/configuration,
or another user's private output is readable.

### 12.3 Audit ownership and writable policy

```bash
# No policy/code file should be writable by group or other.
find /etc/slurm /opt/slurm-workflow /usr/local/libexec/slurm \
  -xdev -perm /022 -print

# Review identities and key state paths without printing secret contents.
getent passwd munge slurm slurmrestd workflow
stat -c '%U:%G %a %n' \
  /run/slurm/config /var/spool/slurmctld /var/spool/slurmd \
  /var/lib/slurm/job_container /var/lib/workflow
```

Run secret-mode checks by pathname and metadata only. Never include a key,
password, bearer token, database URL, or callback capability in CI logs.

### 12.4 Mandatory regression gates

Automated acceptance should prove:

- the expected UID owns each daemon;
- no daemon other than `slurmd` runs as root;
- local conventional and REST workflow jobs both run non-root;
- root job submission is rejected;
- job UID/GID and supplementary groups match centralized identity;
- jobs cannot write configuration, service code, keys, controller state, or
  another workflow's capability/output;
- cgroup CPU/RAM limits and private `/tmp` are effective;
- configuration and secret bind mounts are read-only;
- production `slurmrestd` is neither root nor `SlurmUser`; and
- Podman/socket access is unavailable to job identities.

## 13. Authoritative external references

The recommendations in this document are derived from these primary sources:

- [Podman `exec` manual](https://docs.podman.io/en/latest/markdown/podman-exec.1.html)
- [SchedMD `slurm.conf`](https://slurm.schedmd.com/slurm.conf.html)
- [SchedMD Quick Start Administrator Guide](https://slurm.schedmd.com/quickstart_admin.html)
- [SchedMD Job Launch Design Guide](https://slurm.schedmd.com/job_launch.html)
- [SchedMD REST API Quick Start](https://slurm.schedmd.com/rest_quickstart.html)
- [SchedMD REST API Details](https://slurm.schedmd.com/rest.html)
- [SchedMD `slurmdbd.conf`](https://slurm.schedmd.com/slurmdbd.conf.html)
- [SchedMD JWT Authentication](https://slurm.schedmd.com/jwt.html)
- [SchedMD TLS](https://slurm.schedmd.com/tls.html)
- [SchedMD Authentication Plugins](https://slurm.schedmd.com/authentication.html)
- [SchedMD Resource Limits](https://slurm.schedmd.com/resource_limits.html)
- [SchedMD job_container/tmpfs](https://slurm.schedmd.com/job_container_tmpfs.html)
- [SchedMD `job_container.conf`](https://slurm.schedmd.com/job_container.conf.html)
- [SchedMD Prolog and Epilog Guide](https://slurm.schedmd.com/prolog_epilog.html)
- [SchedMD Troubleshooting Guide](https://slurm.schedmd.com/troubleshoot.html)
- [MUNGE QUICKSTART](https://github.com/dun/munge/blob/master/QUICKSTART)

## 14. Repository implementation index

| Concern | Implementation |
|---|---|
| Fixed system users and directory modes | [`scripts/configure-runtime.sh`](../../scripts/configure-runtime.sh#L142) |
| Input ownership/mode validation | [`scripts/cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L78) |
| Runtime configuration staging | [`scripts/cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L203) |
| JWT/JWKS staging | [`scripts/cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L267) |
| TLS identity staging | [`scripts/cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L290) |
| MUNGE identity/key staging | [`scripts/cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L323) |
| Daemon privilege drop | [`scripts/cluster-entrypoint.sh`](../../scripts/cluster-entrypoint.sh#L620) |
| Production root-job prevention | [`config/slurm.production.conf`](../../config/slurm.production.conf#L24) |
| Production cgroup/namespace policy | [`config/slurm.production.conf`](../../config/slurm.production.conf#L26) |
| Compute privilege allowlist | [`deploy/quadlet/slurmd@.container`](../../deploy/quadlet/slurmd@.container#L13) |
| Broker direct non-root startup | [`deploy/quadlet/workflow-broker.container`](../../deploy/quadlet/workflow-broker.container#L7) |
| Daemon/job identity acceptance | [`scripts/acceptance-test.sh`](../../scripts/acceptance-test.sh#L81) |
| Known local conventional submission path | [`scripts/submit-job.sh`](../../scripts/submit-job.sh#L20) |

Related documents:

- [build and runtime implementation](build-and-runtime.md)
- [dependency sources and package provenance](dependency-provenance.md)
- [production operations runbook](production-runbook.md)
- [AWS deployment and security contract](aws-deployment.md)
