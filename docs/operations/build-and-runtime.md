# Slurm package build and runtime implementation

Status: implemented and acceptance-tested on 2026-10-26

Implementation commit: `6b803ba`

Validated integration image: `3473d6c90bdfc743d327a4d593ea915f4a3c3fc65a9b2dcf204597601cc7ace3`

## 1. Scope and authoritative files

This document is the operating reference for producing the RPM artifact set,
installing it, and laying out the common Slurm runtime filesystem. The three
public commands are deliberately usable both from a UBI 10.2 multi-stage
container build and from an EL10 build or image-baking host:

| Phase | Public command | Input | Output |
|---|---|---|---|
| Build | `scripts/build-rpms.sh` | Pinned source metadata and signed package repositories | Binary/source RPMs plus manifest |
| Install | `scripts/install-runtime.sh` | Verified package directory | Installed runtime packages |
| Configure | `scripts/configure-runtime.sh` | Repository configuration/runtime assets | Users, directories, profiles and helpers |

Shared validation is implemented in `scripts/lib/build-common.sh`. Pinned
public inputs are data in `build/versions.conf`, not shell code. `Dockerfile`
is only an orchestrator: it copies these files into each stage and calls the
same commands an operator can call manually.

Daemon startup, secret creation, secret retrieval, image signing, SBOM
generation, and cloud infrastructure are intentionally outside these three
commands. Runtime startup belongs to `scripts/cluster-entrypoint.sh`; local
development secret creation belongs to `scripts/bootstrap-dev-secrets.sh`;
production secrets come from the site's workload-authenticated secret system.

## 2. Phase boundaries and data flow

```text
build/versions.conf + upstream archives + signed dependency repositories
                                 |
                                 v
                       scripts/build-rpms.sh
                                 |
              /var/packages/{RPMS,SRPMS,BUILD-MANIFEST}
                                 |
                                 v
                    scripts/install-runtime.sh
                                 |
                    verified installed RPM subset
                                 |
       repository assets ------>+------> scripts/configure-runtime.sh
                                               |
                          immutable configuration and runtime layout
                                               |
                          runtime-only secrets staged by entrypoint
```

No later phase silently rebuilds an earlier phase. The installer does not
accept an unmanifested RPM, and the configuration command does not invoke DNF
or create a key. This separation lets CI promote `/var/packages` as a reviewed
artifact and lets image baking fail before any daemon can start.

## 3. Pinned public inputs

`build/versions.conf` currently records:

| Input | Pinned value |
|---|---|
| MUNGE | `0.5.18` |
| Slurm | `26.05.5` |
| Format | `1` |
| EPEL release RPM | SHA-256 pinned |
| CentOS signing key | SHA-256 pinned |

The complete 64-character digests remain in the version file and Docker build
arguments so reviewers compare one authoritative value instead of copying it
from prose. The parser accepts only the seven known keys, rejects duplicates,
whitespace-bearing values, malformed versions, missing values, and unknown
keys, and never uses `source`. Consequently, modifying the version file cannot
execute shell code with the root privileges needed by RPM/DNF.

A source upgrade changes the version and digest together. Update these three
locations in one reviewed change:

1. `build/versions.conf`;
2. the four matching default `ARG` values in `Dockerfile`; and
3. the local default values in `scripts/cluster-up.sh`.

Contract tests compare both the Dockerfile and `cluster-up.sh` defaults with
the version file. A clean image build and the full acceptance suite are
required before promotion.

## 4. RPM construction: `build-rpms.sh`

### 4.1 Interface and prerequisites

```text
Usage: scripts/build-rpms.sh [OPTIONS]

  --versions-file FILE
  --output-dir DIRECTORY          default: /var/packages
  --work-dir DIRECTORY            default: /var/tmp/slurm-rpm-build
  --slurm-version VERSION         requires --slurm-sha256
  --slurm-sha256 DIGEST           requires --slurm-version
  --munge-version VERSION         requires --munge-sha256
  --munge-sha256 DIGEST           requires --munge-version
```

The command requires root inside a UBI/RHEL-compatible EL10 environment. It
rejects relative paths, `/` as a work/output target, symbolic-link output
directories, incomplete override pairs, and unsupported platform IDs. It
creates a private per-run directory under the selected work parent and a
staging directory beside the final output.

### 4.2 Repository trust

The builder first installs the minimum UBI repository tooling. It downloads
the EPEL release RPM and CentOS official key into a private temporary
directory, verifies each pinned SHA-256 before installation/import, and keeps
CentOS Stream repository definitions command-scoped. GPG verification remains
enabled for Stream BaseOS, AppStream and CRB.

The complete repository-to-package mapping, direct SRPM BuildRequires,
observed runtime transaction, plugin/library graph, subscription boundary and
production provenance requirements are maintained in
[dependency sources and package provenance](dependency-provenance.md).

The current Stream `libbpf-devel` dependency requires a newer
`kernel-headers` package than UBI 10.2 GA. That replacement occurs only in the
disposable builder. Builder packages are never copied into the runtime image;
only the generated RPM/SRPM files cross the stage boundary.

### 4.3 MUNGE and Slurm build sequence

MUNGE is built first because Slurm's MUNGE plugin needs its headers:

1. download the release archive into the private run directory;
2. verify its pinned digest before `tar` reads the embedded spec;
3. resolve spec build dependencies;
4. run `rpmbuild -ta --with check`; and
5. install the resulting MUNGE RPM set only into the disposable builder.

Slurm then follows the same verify-before-extract boundary. Its RPM build
explicitly enables `cgroupv2`, `jwt`, `slurmrestd`, and YAML. Development
dependencies provide MySQL accounting, `libjwt`, `llhttp`, `libyaml`,
`libbpf`, D-Bus and `tls/s2n`. The script fails if the expected
`slurm-slurmrestd` RPM was not produced.

### 4.4 Artifact publication and idempotency

All binary RPMs are copied into `RPMS/`, and both upstream source RPMs are
copied into `SRPMS/`. The manifest begins with exactly four source records:

```text
MUNGE_VERSION=<version>
MUNGE_SHA256=<source digest>
SLURM_VERSION=<version>
SLURM_SHA256=<source digest>
```

The remaining sorted records use relative paths and SHA-256:

```text
<rpm digest>  RPMS/<filename>.rpm
<rpm digest>  SRPMS/<filename>.src.rpm
```

Relative paths make the artifact directory relocatable while validation still
rejects `..`, absolute paths, unsafe characters and symlinks. The verifier
requires the recorded RPM path set to equal the actual RPM path set exactly;
an extra, missing, replaced or unrecorded package fails validation.

For a new output path, publication is one directory rename. For an existing
empty bind mount, the two package directories are moved first and the
manifest last. Therefore an interrupted publication has no trusted manifest
and fails closed. A non-empty directory without a valid matching manifest is
never merged or recursively deleted. Repeating a build against an intact,
matching package directory verifies it and exits without rebuilding.

## 5. Runtime installation: `install-runtime.sh`

### 5.1 Interface

```text
Usage: scripts/install-runtime.sh [OPTIONS]

  --versions-file FILE
  --packages-dir DIRECTORY         default: /var/packages
  --with-integration-database      default
  --without-integration-database
```

The command is root-only and EL10-only. Before invoking DNF it verifies:

- the manifest is a non-symlink regular file;
- every record has the safe relative-path grammar;
- the manifest records exactly every RPM present;
- every RPM digest matches;
- the MUNGE and Slurm source version/digest headers equal the approved values
  from `build/versions.conf`; and
- exactly one RPM exists for every required runtime package name.

This final source-pin comparison matters: an attacker cannot substitute a
self-consistent manifest and RPM directory built from an unapproved release.

### 5.2 Selected package set

Only these locally built binary RPM names are installed:

- `munge` and `munge-libs`;
- `slurm`;
- `slurm-slurmctld`;
- `slurm-slurmd`;
- `slurm-slurmdbd`; and
- `slurm-slurmrestd`.

Debug, debuginfo, development, Perl, compatibility and source RPMs remain in
`/var/packages` for audit/rebuild purposes but are not installed. Runtime
dependencies explicitly requested by the script are `bash`,
`ca-certificates`, `curl`, `hostname`, `iproute`, `openssl`, `procps-ng`,
`python3`, `python3-PyMySQL`, `shadow-utils`, and `util-linux`; DNF resolves
their signed transitive dependencies and the Slurm plugin libraries.
`--with-integration-database` additionally installs `mariadb-server` for the
self-contained Compose image. Production roles backed by an external HA
database can use `--without-integration-database` when constructing a
site-specific role image.

The local package manifest does not inventory third-party dependency RPMs.
Production CI must capture their exact repository IDs and NEVRAs plus the
final image SBOM as described in the dependency provenance document.

After DNF cleanup, the script requires all five daemon executables and checks
the following plugin libraries with `ldd` for unresolved dependencies:

- `auth_jwt` and `rest_auth_jwt`;
- `data_parser_v0_0_45`;
- `cgroup_v2`;
- `namespace_tmpfs`;
- `tls_s2n`; and
- `accounting_storage_mysql`.

## 6. Runtime filesystem configuration: `configure-runtime.sh`

### 6.1 Interface and identity contract

```text
Usage: scripts/configure-runtime.sh [OPTIONS]

  --source-root DIRECTORY
  --with-integration-database      default
  --without-integration-database
```

Every source asset must be a readable non-symlink regular file beneath the
explicit source root. Top-level workflow Python and batch files are copied;
tests, bytecode, caches and nested development files are not installed.

The fixed identities are:

| Identity | UID | GID | Home | Shell |
|---|---:|---:|---|---|
| `munge` | 64029 | 64029 | `/var/lib/munge` | nologin |
| `slurm` | 64030 | 64030 | `/var/lib/slurm` | nologin |
| `slurmrestd` | 64031 | 64031 | `/var/lib/slurmrestd` | nologin |
| `workflow` | 64032 | 64032 | `/var/lib/workflow` | nologin |

The MUNGE RPM initially allocates an implementation-selected system ID, so the
configuration phase normalizes it after checking both target IDs for a
collision. Other identities are created only when absent. Repeated execution
validates UID, GID, home and non-login shell rather than silently changing an
unexpected existing account. The integration-database option also requires
the package-defined `mysql` identity to be UID/GID 27.

These in-image IDs are part of the container contract, not a replacement for
production directory identity. Production hosts and centralized identity
must present matching job-user numbers across controller and compute nodes.
The complete daemon/job privilege model, file ownership rationale, official
source references, audit commands and current local submission gap are in
[service identities, job users, ownership, and permissions](identity-and-permissions.md).

### 6.2 Directory ownership and modes

| Path | Owner | Mode | Purpose |
|---|---|---:|---|
| `/etc/slurm/profiles/{simulation,production}` | `root:slurm` | 0755 | Immutable profile roots |
| `/run/slurm/pids` | `slurm:slurm` | 0755 | Daemon PID files |
| `/var/lib/munge` | `munge:munge` | 0700 | MUNGE state |
| `/run/munge`, `/var/log/munge` | `munge:munge` | 0755 | MUNGE runtime/log paths |
| `/var/log/slurm` | `slurm:slurm` | 0750 | Slurm logs |
| `/var/spool/slurmctld` | `slurm:slurm` | 0700 | Controller state mount point |
| `/var/spool/slurmd` | `root:root` | 0755 | Compute spool mount point |
| `/var/lib/slurm/job_container` | `root:root` | 0755 | Job namespace state |
| `/run/slurmrestd`, `/var/lib/slurmrestd` | `slurmrestd:slurmrestd` | 0750 | REST private state |
| `/var/lib/workflow` | `workflow:workflow` | 0750 | Broker state mount point |
| `/opt/slurm-workflow` | `root:root` | 0755 | Immutable application code |

Named volumes and host mounts can mask image ownership, so the runtime
entrypoint repeats role-specific checks and creates writable subdirectories
with the consuming identity at each start.

### 6.3 Installed assets

Simulation and production Slurm, cgroup, job-container and `slurmdbd`
templates are installed under `/etc/slurm/profiles`. Compatibility symlinks at
`/etc/slurm/slurm.conf` and `/etc/slurm/cgroup.conf` point only to the
simulation profile; production daemons use a validated profile staged below
`/run/slurm/config` through `SLURM_CONF`.

The command also installs:

- the role-aware entrypoint, health check and development token renewer under
  `/usr/local/sbin`;
- the production lifecycle client and shared helper under
  `/usr/local/libexec/slurm`;
- the broker Python package and demo master/worker/finalizer files under
  `/opt/slurm-workflow`; and
- executable and plugin assertions needed before the image layer commits.

The immutable source-to-image mapping is:

| Repository source | Installed destination |
|---|---|
| `config/slurm.conf` | `/etc/slurm/profiles/simulation/slurm.conf` |
| `config/cgroup.conf` | `/etc/slurm/profiles/simulation/cgroup.conf` |
| `config/slurmdbd.conf.template` | `/etc/slurm/profiles/simulation/slurmdbd.conf.template` |
| `config/slurm.production.conf` | `/etc/slurm/profiles/production/slurm.conf` |
| `config/cgroup.production.conf` | `/etc/slurm/profiles/production/cgroup.conf` |
| `config/job_container.production.conf` | `/etc/slurm/profiles/production/job_container.conf` |
| `config/slurmdbd.production.conf.template` | `/etc/slurm/profiles/production/slurmdbd.conf.template` |
| `scripts/cluster-entrypoint.sh` | `/usr/local/sbin/cluster-entrypoint` |
| `scripts/cluster-healthcheck.sh` | `/usr/local/sbin/cluster-healthcheck` |
| `scripts/dev-token-renewer.sh` | `/usr/local/sbin/dev-token-renewer` |
| `deploy/lifecycle/lifecycle-common.sh` | `/usr/local/libexec/slurm/lifecycle-common.sh` |
| `deploy/lifecycle/slurm-resume-program` | `/usr/local/libexec/slurm/slurm-resume-program` |
| `deploy/lifecycle/slurm-suspend-program` | `/usr/local/libexec/slurm/slurm-suspend-program` |
| top-level `workflow/*.py` | `/opt/slurm-workflow/workflow/` |
| top-level `examples/workflow/*.{py,sbatch}` | `/opt/slurm-workflow/examples/` |

It never generates or copies a MUNGE key, JWT key/token, database password,
TLS private key, certificate, runtime environment file or cloud credential.

## 7. Dockerfile orchestration

The builder stage copies only `versions.conf`, the common library and the RPM
builder, then calls `build-rpms.sh`. The runtime stage copies `/var/packages`,
calls `install-runtime.sh`, copies the required immutable assets, and calls
`configure-runtime.sh`. The temporary `/opt/slurm-source` tree is removed
before the final layer commits.

Build with:

```bash
podman build --format docker \
  --tag localhost/slurm-sim:latest \
  --file Dockerfile .
```

An upgrade must provide complete version/digest pairs:

```bash
podman build --format docker \
  --build-arg SLURM_VERSION=X.Y.Z \
  --build-arg SLURM_SHA256=64_LOWERCASE_HEX \
  --build-arg MUNGE_VERSION=X.Y.Z \
  --build-arg MUNGE_SHA256=64_LOWERCASE_HEX \
  --tag registry.example/slurm:X.Y.Z \
  --file Dockerfile .
```

Build arguments are public provenance, never a secret-delivery mechanism.

## 8. Manual execution

Do not run the scripts directly on Fedora 44; their RPM and dependency
contract is EL10. On an EL10 build host:

```bash
sudo scripts/build-rpms.sh \
  --output-dir /var/packages \
  --work-dir /var/tmp/slurm-rpm-build
sudo scripts/install-runtime.sh \
  --packages-dir /var/packages \
  --without-integration-database
sudo scripts/configure-runtime.sh \
  --source-root "$PWD" \
  --without-integration-database
```

From Fedora/WSL, use the checked-in Dockerfile. For an artifact-only build, a
disposable UBI container can mount an empty host artifact directory:

```bash
package_output=$(mktemp -d /tmp/slurm-packages.XXXXXXXX)
podman run --rm --user 0 \
  --volume "$PWD:/src:ro,Z" \
  --volume "$package_output:/var/packages:Z" \
  registry.access.redhat.com/ubi10/ubi:10.2 \
  /src/scripts/build-rpms.sh \
    --versions-file /src/build/versions.conf \
    --output-dir /var/packages
printf 'packages: %s\n' "$package_output"
```

The resulting directory is suitable for artifact storage or a later EL10
image-baking stage. Preserve `BUILD-MANIFEST`, `RPMS/`, and `SRPMS/` together.

## 9. CI promotion and production use

Production CI should perform these gates in order:

1. run unit, contract, shell-syntax and Compose-render tests;
2. build once from the pinned sources on an isolated builder;
3. verify the package manifest and required plugins in the final image;
4. generate an SBOM and vulnerability report for the final image and RPM set;
5. sign the image and provenance attestation in the approved CI identity;
6. run scheduler, accounting, REST workflow and node-lifecycle acceptance;
7. push to the private registry; and
8. promote the same immutable digest to staging and production.

Runtime hosts pull by digest. They do not run `build-rpms.sh`, reach public
source repositories, or decide which source version is current. The checked-in
Compose profile uses `localhost/slurm-sim:latest` only as a local development
tag; the Quadlet installer requires a digest reference.

## 10. Verification commands

```bash
python3 -m unittest discover -s workflow/tests -v
python3 -m unittest discover -s scripts/tests -v
bash -n scripts/*.sh scripts/lib/*.sh scripts/nodes/*.sh \
  deploy/lifecycle/lifecycle-common.sh \
  deploy/lifecycle/slurm-resume-program \
  deploy/lifecycle/slurm-suspend-program \
  deploy/aws/materialize-aws-secret deploy/quadlet/install.sh
git diff --check
scripts/acceptance-test.sh --start
```

The acceptance suite verifies manifest integrity, absence of embedded secrets,
daemon identities, non-root job execution, private listeners, conventional
job submission and accounting, a REST-submitted master with a throttled
20-element worker array, and dynamic-node add/place/drain/remove behavior.

The directly addressable controller name is intentionally stable:

```bash
podman exec controller sinfo
```

## 11. Failure modes and recovery

- **Existing package directory differs:** preserve it for investigation or
  choose a new empty output. The builder will not merge or delete it.
- **Manifest source pin mismatch:** the artifact set came from different
  approved inputs. Do not bypass the check; rebuild or supply the correct
  reviewed version file.
- **Manifest hash/path mismatch:** quarantine the entire artifact set. Do not
  install individual RPMs from it.
- **UID/GID collision:** correct the base image or directory identity source.
  Never reassign an unrelated account automatically.
- **Missing or unresolved plugin:** treat the image build as failed. Do not
  disable the corresponding production configuration feature.
- **Configuration source is a symlink:** supply a real, reviewed source tree.
  The command deliberately does not follow a mutable indirection as root.
- **Interrupted bind-mount publication:** use a new empty output directory;
  the missing final manifest ensures the partial set is not trusted.
- **Runtime secret missing:** fix secret materialization and restart the role.
  Never add secret generation to these build/configuration phases.

## 12. Local versus production contract

The common image proves software compatibility and keeps every daemon in a
separate container. It does not by itself provide production HA, centralized
POSIX identity, managed secrets, TLS/OIDC gateways, cgroup delegation,
SELinux policy, durable storage, backup/restore, or cloud lifecycle controls.
Those requirements are defined in the
[production runbook](production-runbook.md),
[AWS deployment contract](aws-deployment.md), and
[Quadlet deployment contract](../../deploy/quadlet/README.md).
