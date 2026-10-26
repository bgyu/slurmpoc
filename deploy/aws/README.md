# AWS deployment contract

This directory defines the cloud-specific boundary; it does not create an AWS
account or silently apply infrastructure. Convert these controls into the
organization's reviewed Terraform/CloudFormation modules. Every file here is
a deployment contract or example, not a directly deployable CloudFormation
stack. Required inputs
are account/region, two or more availability zones, approved CIDRs, immutable
AMI and container-image digests, cluster name, DNS zone, KMS key ARNs, secret
ARNs, launch-template ID and maximum node count.

The lifecycle service is a private, multi-AZ reconciler (for example ECS
Fargate plus DynamoDB) behind an mTLS-capable internal load balancer.  Its task
role is based on `lifecycle-iam-policy.json`. Replace every angle-bracket
token and validate the result with IAM Access Analyzer in the target account.
The example deliberately omits Spot, Auto Scaling, subnet/security-group
creation, arbitrary AMI selection and tag mutation after launch. The lifecycle
caller must always pass a reviewed numeric launch-template version; `$Latest`
and `$Default` are mutable aliases and are forbidden. Principals able to create
or modify that template are outside the lifecycle task role and require
separate change control.

The reconciler must enforce policy in addition to IAM:

1. Authenticate the client certificate and map its SAN to one cluster.
2. Expand the Slurm hostlist with a bounded parser and reject names outside
   the configured pool.
3. Store `(cluster, node, desired_generation, desired_state)` transactionally
   before returning 202.  Store the HTTP idempotency key and canonical request
   hash; an identical retry returns the same operation, while a mismatched
   body returns 409.
4. Allow at most one live EC2 instance tagged for a Slurm node.  Reconciliation,
   not repeated `RunInstances`, handles duplicate desired-state requests.
5. Launch only the configured, numeric-version template and add the six
   allow-listed tags to each instance, network interface and volume created by
   the request. The template fixes AMI, private subnets, security group,
   instance profile/type, encrypted volumes, IMDSv2 and hop limit one.
6. On suspend, terminate only an instance whose cluster, node and generation
   match durable state.  Never select an instance from a client-supplied ID.
7. Record CloudTrail-compatible request IDs and metrics without logging the
   resume JSON, client certificate, tokens or secret values.

`compute-node-iam-policy.json` is the maximum bootstrap role for a basic CPU
node.  Add the AWS-managed SSM core policy only when Session Manager is the
approved break-glass path.  Batch jobs do not inherit instance-role access:
block IMDS from workload cgroups/users with a host firewall or credential
proxy, require IMDSv2, and set the metadata hop limit to one.  Prefer task/job
specific credentials for S3 access rather than widening the node role.

`slurm-cloud.conf.example` is an opt-in render fragment, not a runtime Include.
The production entrypoint accepts one fully rendered `slurm.conf` so it can
validate the whole policy. Its 900 cloud names plus the base profile's 100
FUTURE names fit `MaxNodeCount=1000`. The base `standard` partition already
names only the FUTURE pool, so AWS nodes cannot silently join two partitions.
If capacity or shapes change, update node records, partition membership and
`MaxNodeCount` together.

The full topology, port matrix, secret ownership, storage choices and rollout
procedure are in `docs/operations/aws-deployment.md`.

`materialize-aws-secret` and `slurm-secret@.service` are an optional host-side
Secrets Manager bridge.  Install the script as
`/usr/local/libexec/slurm/materialize-aws-secret`, the unit as
`/etc/systemd/system/slurm-secret@.service`, and create only the role-specific
identifier files under `/etc/slurm-production/secrets`. Secret bytes are
written atomically to `/run/slurm-secrets`; they never enter systemd's
environment.  Fetch `munge` on a compute host with:

```bash
sudo systemctl start slurm-secret@munge.service
```

The unit is deliberately not globally enabled because controllers, accounting,
REST, broker and compute hosts must not fetch each other's credentials. A site
provisioning target should `Wants=` only its exact secret instances.

The runtime secret directory is root-owned mode 0711 (search but not list);
individual files remain owner-only. This lets a service reach its exact known
path while only root can add names. The broker mounts the directory read-only
so an atomic rename performed by its renewal agent is visible immediately; an
individual file bind mount would remain attached to the old inode.

The examples include MUNGE, accounting-database and workflow database/proxy
credentials. Do not store the broker's Slurm JWT as a long-lived Secrets
Manager value. A workload-authenticated renewal agent must obtain a
short-lived ordinary-user token and atomically replace
`/run/slurm-secrets/workflow-slurm-token` as uid/gid 64032, mode 0400. The REST
hosts do not need the MUNGE key in the JWT-only topology.

These files do not implement the lifecycle service, OIDC gateways, PKI agent,
broker JWT renewal agent or per-allocation workflow capability delivery. Those
are mandatory production components with the interfaces documented here and
in `docs/operations/aws-deployment.md`; deployment must fail closed while any
one is absent.
