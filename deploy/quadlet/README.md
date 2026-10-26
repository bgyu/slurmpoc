# Rootful Podman/Quadlet production deployment contract

These files are reviewable host-unit templates. They are not a complete
production deployment and `install.sh` intentionally does not enable or start
anything. Terraform/CloudFormation, image baking, PKI, identity, secret
renewal, storage mounts, gateway configuration and monitoring remain explicit
site responsibilities. Treat every `.container` and `.env.example` file as a
contract to be rendered and acceptance-tested, not as a turnkey default.

The units use host networking because a Podman bridge on one EC2 instance is
not a multi-host cluster network. Security groups and host firewalls must
enforce the port matrix in `docs/operations/aws-deployment.md`.

## 1. Install an immutable image reference

Build, manifest validation, runtime package selection and fixed identity
creation are defined in `docs/operations/build-and-runtime.md`. Production CI
must complete that contract, SBOM/vulnerability gates and acceptance before
publishing the digest used here. Quadlet hosts never run the RPM builder.

Install a promoted image by digest only:

```bash
sudo deploy/quadlet/install.sh \
  --image 123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/slurm@sha256:64_HEX_DIGEST
sudo install -o root -g root -m 0644 \
  deploy/quadlet/slurm-production.tmpfiles.conf \
  /etc/tmpfiles.d/slurm-production.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/slurm-production.conf
```

`install.sh` installs only the Quadlet/image definitions. IaC must separately
render the environment files and create every host path below before a unit is
started. Do not copy development secrets or blindly promote the example DNS,
CIDR, resource and cluster values.

## 2. Required host inputs

All configuration and trust files are root-owned and not group/world writable.
Secret and private-key files use mode `0400` unless the consuming renewal agent
requires `0600` for atomic replacement.

| Role | Required inputs before start |
|---|---|
| controller | Fully rendered `config/slurm.conf`; public JWKS and Slurm CA; role TLS cert/key; MUNGE key; lifecycle config, CA, cert/key; mounted shared controller state; `env/controller-INSTANCE.env`. |
| accounting | Reviewed `config/slurmdbd.conf.template`; public JWKS, Slurm CA and database CA; role TLS cert/key; MUNGE key and database credential; `env/slurmdbd-INSTANCE.env`. |
| REST | Fully rendered `config/slurm.conf`; public JWKS and Slurm CA; role TLS cert/key; `env/slurmrestd-INSTANCE.env`; a same-host authenticating gateway. |
| compute | The same fully rendered `config/slurm.conf`; Slurm CA and host-unique TLS cert/key; MUNGE key; shared-data and node-private spool mounts; an empty or approved `env/slurmd-NODE.env`. |
| broker | Workflow database/API CA, gateway-to-broker token, renewable ordinary-user Slurm JWT, shared workflow storage, production environment file, and the external capability-delivery control described below. |

The environment examples contain no secrets and are normally installed as
`root:root 0644`. Environment files for an instance must exist even when they
are intentionally empty. The fixed in-image identities are `slurm` 64030,
`slurmrestd` 64031 and `workflow` 64032. Shared paths and renewal agents must
use those numeric identities; name-only ownership is not a cross-host contract.
The shared controller-state root is uid/gid 64030, mode 0700 on both peers.
The broker's EFS access point (or equivalent) must present its workflow root as
uid/gid 64032, mode 0750, with `.workflow-control` mode 0700. Do not make the
mount root world-writable to work around an ownership error.

The controller, accounting and compute units explicitly start one private
`munged` process because they receive the MUNGE key. The JWT-only REST unit
does not receive the cluster-wide MUNGE key. Its public JWKS is mounted as a
read-only input, validated for usable public RS256 material, and staged into
private runtime ownership at the path used by `AuthAltParameters`.

`/run/slurm-secrets` is root-owned mode 0711: service UIDs may traverse a known
path but cannot list or create entries. Every file remains owner-only. The
broker mounts that directory read-only because a bind mount of one file pins
its old inode and would not observe an atomic token rotation. Dedicated broker
hosts must contain only broker secrets in this directory.

## 3. Instance identities

The intended template instances are:

- controller: `slurm-controller@primary` and `@backup`;
- accounting: `slurmdbd@primary` and `@backup`;
- REST: `slurmrestd@a` (and another reviewed instance/host), each with a
  same-host OIDC-aware gateway;
- compute: `slurmd@aws-cpu0001`, where `%i`, the host name and the configured
  `State=CLOUD` Slurm NodeName are identical; and
- broker: one `workflow-broker` per host behind a same-host authenticating
  gateway and an internal load balancer.

AWS power-save nodes are statically named `CLOUD` identities. They must not
set `SLURMD_FEATURE` or `SLURM_CONF_SERVER`; the mounted, fully rendered
configuration lets the host register as exactly `%i`. The separate
`compute[0001-0100] State=FUTURE` pool may use dynamic registration, but only
when its environment file sets both singular entrypoint variables shown in
`env/slurmd-compute0001.env.example`. Never use dynamic FUTURE allocation to
satisfy a resume request for a particular `aws-cpuNNNN` identity.

## 4. TLS and gateway boundary

`slurmrestd` listens on `https://127.0.0.1:6820`. The explicit `https://`
prefix makes clear-text connections fail rather than relying on opportunistic
TLS detection. Its unique certificate therefore needs an IP SAN for
`127.0.0.1`, and the same-host gateway must verify that certificate against the
Slurm CA. No security-group rule may expose port 6820.

The workflow broker also listens only on loopback. Its gateway authenticates
OIDC, deletes caller-supplied identity/proxy headers, injects the verified
identity, and presents the private proxy token. The example trusts only
`127.0.0.1/32` for both authenticated-user requests and forwarded master
callbacks. The gateway permits the callback route only from the compute tier;
the broker then requires its one-use capability. The broker's outbound Slurm
API URL is HTTPS and verifies
`/etc/slurm-production/trust/slurm-api-ca.pem`.

## 5. Mandatory workflow capability delivery

The broker unit deliberately keeps callback capabilities in private tmpfs and
advertises a different job-visible path. This is fail-closed: the demo master
cannot dispatch until a site-supplied, node-attested component delivers that
one capability into only the master allocation as uid 64032, mode 0400, after
the job starts. This repository does not yet implement that production
delivery component.

Do not point `WORKFLOW_CAPABILITY_DIR` and
`WORKFLOW_CAPABILITY_JOB_DIR` at shared storage. All broker-submitted demo
jobs currently use uid 64032, so a shared directory would let one job read
another workflow's capability. Do not enable the production broker workflow
until the delivery component has passed cross-job disclosure, queued-job TTL,
replay, cancellation and cleanup tests. The local Compose profile remains the
only supported shared-file demo.

## 6. Validate before enabling

After all inputs are rendered, inspect generated commands and dependencies:

```bash
sudo systemctl daemon-reload
sudo /usr/lib/systemd/system-generators/podman-system-generator --dryrun
sudo systemd-analyze verify /run/systemd/generator/slurm-controller@.service
sudo systemd-analyze verify /run/systemd/generator/slurmd@.service
```

Start roles only in the rollout order in the production runbook. The
`ExecStartPre` assertions intentionally turn missing runtime material into a
hard failure. A successful process start is not acceptance; verify TLS,
MUNGE, controller/accounting membership, identity, cgroups, job launch and
negative authentication tests.

The compute unit deliberately does not use `--privileged`. It still grants
`CAP_SYS_ADMIN`, exposes a delegated host cgroup-v2 hierarchy and grants job
launch capabilities. Compute hosts must therefore be dedicated to this Slurm
trust domain. Enable the RHEL `container_manage_cgroup` SELinux policy and a
site-specific policy; do not disable SELinux. Test the exact MPI, GRES,
prolog/epilog and namespace set in staging before removing a capability.
