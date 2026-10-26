# Slurm Podman simulation cluster design

## Goal

Build Slurm and MUNGE RPMs from their latest source releases on UBI 10.2,
retain the artifacts below `/var/packages`, install those artifacts into the
same image, and use that image for a local Slurm simulation consisting of one
controller, one MariaDB service, one `slurmdbd` service, and five compute
nodes. The protected REST and workflow services added by the production design
use the same image.

## Architecture

The multi-stage Dockerfile builds upstream MUNGE first and installs its newly
built development RPMs in the builder. It then builds the upstream Slurm RPMs
with MariaDB accounting support. Binary and source RPMs are copied into the
runtime image under `/var/packages/RPMS` and `/var/packages/SRPMS`; `dnf`
installs the binary artifacts and resolves their runtime dependencies.

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

Shell syntax, Compose rendering, and the image build should be checked locally.
The smoke test waits for five idle nodes, prints `sinfo`, and runs `hostname`
once on each node through `srun`.
