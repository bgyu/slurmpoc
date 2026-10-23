# Slurm Podman simulation cluster design

## Goal

Build Slurm and MUNGE RPMs from their latest source releases on UBI 10.2,
retain the artifacts below `/var/packages`, install those artifacts into the
same image, and use that image for a local Slurm simulation consisting of one
controller, one database service, and five compute nodes.

## Architecture

The multi-stage Dockerfile builds upstream MUNGE first and installs its newly
built development RPMs in the builder. It then builds the upstream Slurm RPMs
with MariaDB accounting support. Binary and source RPMs are copied into the
runtime image under `/var/packages/RPMS` and `/var/packages/SRPMS`; `dnf`
installs the binary artifacts and resolves their runtime dependencies.

Every cluster service uses the resulting image. A role-aware entrypoint starts
MUNGE followed by the appropriate daemon. The database role also initializes
and runs MariaDB. It renders the password-bearing `slurmdbd.conf` at runtime,
so credentials are not stored in source control or image layers.

Compose declares five compute services explicitly to give Slurm stable node
names. A helper creates one shared MUNGE key and a random database password.
Named volumes preserve MariaDB and controller state.

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
