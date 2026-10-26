# Reusable Slurm build, install, and configuration scripts

Status: approved on 2026-10-26

## Purpose

Move package construction, RPM installation, and runtime filesystem setup out
of Dockerfile `RUN` blocks into three independently callable scripts. The same
implementation must work when invoked manually on a compatible UBI/RHEL 10
host or during a multi-stage container build.

The refactor must preserve the existing supply-chain, privilege, runtime-user,
plugin, and `/var/packages` contracts. It must not turn configuration scripts
into secret-generation or daemon-startup tools.

## Public interfaces

### `scripts/build-rpms.sh`

This root-only build-stage command:

1. validates versions, SHA-256 values, output/work paths, and architecture;
2. configures only the signed dependency repositories required for the build;
3. downloads and verifies MUNGE and Slurm archives before extracting specs;
4. installs build dependencies;
5. builds and checks MUNGE, then installs its new build RPMs;
6. builds Slurm with cgroup v2, JWT, REST, YAML, MySQL accounting, namespace,
   HTTP parser, and `tls/s2n` support;
7. copies binary and source RPMs into the requested package directory; and
8. atomically creates a deterministic `BUILD-MANIFEST` containing source
   versions/digests and hashes for every RPM.

Defaults are read from a repository-owned, non-secret version file. Command
line options override complete version/digest pairs; a version cannot be
changed without explicitly supplying its matching digest. The default output
is `/var/packages`. A caller-supplied output directory must be absolute, may
not be `/`, and is never recursively erased by the script.

### `scripts/install-runtime.sh`

This root-only runtime-stage command:

1. validates the package directory and `BUILD-MANIFEST` without following
   symbolic links;
2. verifies every recorded RPM hash before invoking the package manager;
3. selects exactly the required MUNGE and Slurm runtime RPM names, excluding
   debug, source, and development packages;
4. configures signed runtime dependency repositories;
5. installs runtime tools and the selected locally built RPMs;
6. optionally installs MariaDB server for the local integration image; and
7. cleans package-manager metadata and verifies daemon executables and runtime
   shared-library resolution.

The default includes the integration database to preserve the existing single
image. `--without-integration-database` supports a smaller production role
image later without changing package validation.

### `scripts/configure-runtime.sh`

This root-only filesystem/configuration command accepts an explicit source
root and:

1. normalizes or creates the fixed `munge`, `slurm`, `slurmrestd`, and
   `workflow` numeric identities, failing on UID/GID collisions;
2. creates runtime directories with the existing least-privilege modes;
3. installs simulation and production configuration profiles;
4. installs the entrypoint, health check, development token helper, lifecycle
   hooks, workflow package, and workflow examples;
5. creates simulation compatibility symlinks;
6. normalizes file ownership and executable modes; and
7. verifies identities, required plugins, configuration assets, and installed
   helpers.

It never generates MUNGE keys, JWTs, passwords, certificates, or environment
files. Runtime secret staging remains the entrypoint's responsibility.

## Shared implementation

`scripts/lib/build-common.sh` contains only reusable validation and repository
helpers. It is not a public command. It provides:

- strict absolute-path and SHA-256 validation;
- root and supported-platform checks;
- pinned EPEL package and CentOS signing-key installation;
- command-scoped CentOS Stream repository arguments; and
- consistent diagnostics and temporary-file cleanup.

The version file is parsed as data rather than sourced as shell code. It holds
only pinned versions, hashes, and public dependency artifact hashes. Scripts
reject unknown, duplicate, malformed, or missing keys.

## Dockerfile integration

The Dockerfile becomes orchestration rather than implementation:

1. copy the version file, common library, and `build-rpms.sh` into the builder;
2. invoke `build-rpms.sh` with Docker build arguments as explicit overrides;
3. copy `/var/packages` into the runtime stage;
4. copy the repository runtime assets plus `install-runtime.sh` and
   `configure-runtime.sh` into a temporary image path;
5. invoke the install script, then the configuration script; and
6. remove the temporary source payload.

Build arguments and OCI labels remain available. The script defaults and
Dockerfile defaults must be kept identical and are checked by tests.

## Error handling and idempotency

All scripts use strict shell mode, a restrictive umask where files are
created, bounded downloads with retries, and descriptive nonzero exits.
Downloads go to private temporary files and are verified before use. Package
manifests are written to a temporary file and renamed only after every RPM is
present and hashed.

The install and configuration scripts are safe to repeat with the same input.
Identity collisions, source-digest mismatches, missing package names, manifest
tampering, unsafe paths, unresolved libraries, missing plugins, or unsupported
platform versions fail closed.

## Verification

Automated tests cover argument parsing and validation, version-file parsing,
unsafe paths, manifest corruption, Dockerfile use of all three scripts, and
the separation between configuration and secrets. Acceptance requires:

- a full UBI 10.2 image build;
- verified RPM/SRPM artifacts under `/var/packages`;
- installed Slurm/MUNGE versions and plugins;
- the existing 46 unit/contract tests;
- scheduler and accounting smoke tests;
- the REST master/worker workflow; and
- dynamic node add, placement, drain, and removal.
