# Slurm Podman simulation cluster design

Status: implemented; build details superseded by the reusable three-phase
implementation documented in `docs/operations/build-and-runtime.md`.

## Goal

Build Slurm and MUNGE RPMs from explicitly pinned source releases on UBI 10.2,
retain the artifacts below `/var/packages`, install those artifacts into the
same image, and use that image for a local Slurm simulation consisting of one
controller, one MariaDB service, one `slurmdbd` service, and five compute
nodes. The protected REST and workflow services added by the production design
use the same image.

## Architecture

The multi-stage Dockerfile delegates to three independently callable commands.
`build-rpms.sh` builds upstream MUNGE first and installs its newly built
development RPMs in the disposable builder, then builds the upstream Slurm
RPMs with accounting, REST, JWT, TLS, YAML and cgroup-v2 support.
`install-runtime.sh` verifies the complete artifact manifest and installs only
the runtime RPM subset. `configure-runtime.sh` creates fixed service
identities and installs immutable profiles/helpers without generating a
secret. Binary and source RPMs remain under `/var/packages/RPMS` and
`/var/packages/SRPMS` in the final image.

Every cluster service uses the resulting image. A role-aware entrypoint starts
only the service selected for that container. MariaDB and `slurmdbd` are
separate roles; the latter renders its password-bearing configuration into
tmpfs at runtime, so credentials are not stored in source control or image
layers.

Compose declares five compute services explicitly. Each registers a stable
container hostname as a dynamic-normal Slurm node with a deterministic
simulation resource shape. A helper creates one shared MUNGE key, an HS256
development key, and independent database/broker credentials. Named volumes
preserve MariaDB, controller, workflow, shared-data and compute-spool state.

## Simulation constraints

The compute daemons use `proctrack/linuxproc` and `task/none`, avoiding
privileged containers and host cgroup delegation. Synthetic two-CPU, 1 GiB
node shapes are accepted through `SlurmdParameters=config_overrides`. This is
appropriate for scheduler and job-flow testing, not production isolation or
performance benchmarking.

## Verification

Shell syntax, Compose rendering, and the image build are checked locally.
The smoke test waits for five idle nodes, prints `sinfo`, and runs `hostname`
once on each node through `srun`.

The authoritative verification command is `scripts/acceptance-test.sh --start`.
It additionally checks RPM provenance, daemon identities, private listeners,
accounting, REST master/worker fan-out and elastic-node scale out/in. The
stable direct inspection command is `podman exec controller sinfo`.
