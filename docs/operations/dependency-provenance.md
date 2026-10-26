# Dependency sources and package provenance

Status: describes the implementation validated on 2026-10-26

Validated integration image:
`3473d6c90bdfc743d327a4d593ea915f4a3c3fc65a9b2dcf204597601cc7ace3`

## 1. Purpose and conclusion

This document explains every dependency source used to build and run MUNGE
and Slurm, the code that enables each source, and the evidence a production
release must retain.

The important conclusion is that UBI 10.2 alone does not contain the complete
build or runtime dependency set. The current local image is a deliberately
mixed EL10 image:

- UBI 10.2 supplies the base filesystem and public UBI BaseOS/AppStream RPMs;
- EPEL 10 supplies selected packages such as `libjwt` and `s2n-tls`;
- public CentOS Stream 10 BaseOS, AppStream and CRB fill other gaps, including
  `llhttp` and the integration-only MariaDB server; and
- this repository builds MUNGE and Slurm RPMs from checksum-pinned upstream
  source archives.

No Red Hat subscription is required for that public-repository profile. It is
suitable for this simulation and for controlled evaluation. It is not the
same supply-chain or support contract as a UBI image extended only from public
UBI repositories, or a UBI/RHEL image built from entitled RHEL repositories.

## 2. Dependency flow

```text
registry.access.redhat.com/ubi10/ubi:10.2
                |
                +---- public UBI BaseOS/AppStream (enabled in the base image)
                |
                +---- pinned EPEL release RPM ----> public EPEL 10
                |
                +---- pinned CentOS signing key --> command-scoped Stream 10
                |                                   BaseOS/AppStream/CRB
                |
                +---- pinned MUNGE source ----------> locally built RPM/SRPM
                |
                `---- pinned Slurm source ----------> locally built RPM/SRPM
                                                        |
                                                        v
                                         /var/packages/BUILD-MANIFEST
                                                        |
                                         install selected local RPMs
                                                        |
                           UBI + EPEL + Stream resolve runtime shared libraries
```

The builder and runtime stages resolve separate transactions. Development
packages remain in the disposable builder, but dynamically linked runtime
libraries required by the locally built Slurm plugins must be installed in
the final image.

## 3. Source classes and trust boundaries

| Source class | Location | Authentication/integrity | Subscription | Current purpose |
|---|---|---|---|---|
| UBI image | `registry.access.redhat.com/ubi10/ubi:10.2` | Registry transport and image metadata | No | Builder and runtime base |
| Public UBI RPM repositories | Defined by `/etc/yum.repos.d/ubi.repo` in UBI | Red Hat RPM signatures | No | Core build tools and most runtime packages |
| EPEL 10 | Installed by `epel-release-latest-10.noarch.rpm` | Bootstrap RPM SHA-256 pin, then repository RPM signatures | No | Packages absent from public UBI, notably JWT and s2n |
| CentOS Stream 10 | Direct BaseOS, AppStream and CRB URLs | Pinned CentOS key and RPM signature checking | No | Development packages and missing runtime packages |
| MUNGE upstream | GitHub release archive | SHA-256 pin | No | MUNGE RPM and SRPM construction |
| Slurm upstream | `download.schedmd.com` archive | SHA-256 pin | No | Slurm RPM and SRPM construction |
| Local package set | `/var/packages` | Exact path/digest manifest plus approved source-pin comparison | No | Packages installed into the runtime |

The UBI image reference is currently tag-pinned, not digest-pinned. The
MUNGE/Slurm archives and repository bootstrap artifacts are digest-pinned,
but repository metadata and dependency RPM NEVRAs are not snapshot-pinned.
Those distinctions are important when evaluating reproducibility.

Red Hat's RHEL 10 container documentation explains that public UBI
repositories contain only a subset of RHEL content, and that a subscribed
RHEL host can make wider entitled repositories available to UBI containers:

- <https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html-single/building_running_and_managing_containers/index>
- <https://access.redhat.com/articles/4238681>
- <https://access.redhat.com/support/policy/container-support-policy>

CentOS describes CRB as the repository intended for development content:

- <https://www.centos.org/centos10/>

## 4. Repository implementation

### 4.1 Pinned public inputs

[`build/versions.conf`](../../build/versions.conf) contains:

- the selected MUNGE and Slurm versions and source SHA-256 values;
- the expected SHA-256 of the EPEL release RPM; and
- the expected SHA-256 of the CentOS signing key.

[`build_load_versions`](../../scripts/lib/build-common.sh#L69) parses this file
as a restricted data format. It does not `source` it as shell code. Unknown
keys, duplicate keys, malformed versions, and malformed digests fail before
DNF or RPM runs.

### 4.2 UBI repositories

The standard UBI image contains public UBI repository definitions. Initial
calls such as the installation of `dnf-plugins-core`, `curl`, certificate
roots, RPM build tooling, and basic utilities use those enabled repositories.
DNF continues to prefer available UBI packages when the Stream repositories
are added with a lower repository priority.

The build does not mount `/usr/share/rhel/secrets`, register with Red Hat, or
copy an entitlement certificate. Therefore, entitled RHEL BaseOS,
AppStream, or CodeReady Builder content is not part of the current profile.

### 4.3 EPEL bootstrap

[`build_prepare_repositories`](../../scripts/lib/build-common.sh#L137):

1. downloads `epel-release-latest-10.noarch.rpm` into a private temporary
   directory;
2. verifies its configured SHA-256 before DNF sees the file;
3. installs the release RPM, which creates the EPEL repository definitions
   and signing-key configuration; and
4. deletes the temporary bootstrap directory.

Because the URL contains `latest` while the content digest is pinned, an EPEL
bootstrap update fails closed until `build/versions.conf` is reviewed and
updated. The dependency packages selected from EPEL after bootstrap are not
currently version-locked or obtained from a repository snapshot.

### 4.4 CentOS Stream repositories

[`build_set_stream_repository_args`](../../scripts/lib/build-common.sh#L156)
defines direct HTTPS URLs for Stream 10 BaseOS, AppStream and CRB. The helper
also enables package GPG checking and assigns priority `200` to all three
repositories.

These arguments are expanded only into the DNF commands that need Stream
content. They do not write a persistent Stream `.repo` file into the image.
The CentOS official signing key is downloaded and checksum-verified by
`build_prepare_repositories` before import.

Command scope prevents a later unrelated DNF invocation from silently using a
persisted Stream repository. It does not make the selected package versions
immutable: the public Stream URLs still point to moving repository content.

## 5. Build-stage dependencies

### 5.1 Bootstrap tools

[`build-rpms.sh`](../../scripts/build-rpms.sh#L125) installs `rpm-build`,
`rpmdevtools`, archive tools, and `procps-ng`, then builds MUNGE before Slurm.
The resulting MUNGE RPM set is installed only in the disposable builder so
that the subsequent Slurm build can find MUNGE headers and libraries.

### 5.2 MUNGE source RPM requirements

The validated `munge-0.5.18-1.el10.src.rpm` declares these direct build
requirements:

| Requirement | Purpose/source class |
|---|---|
| `bzip2-devel`, `openssl-devel`, `zlib-devel` | Compression and cryptography development headers; resolved from enabled EL10 repositories |
| `gcc`, `make` | Native compilation toolchain |
| `procps-ng`, `util-linux` | Build/test utilities |
| `systemd-rpm-macros` | EL system-service RPM macros |

`dnf builddep` resolves those names from the repositories enabled at that
point. The exact command is at
[`scripts/build-rpms.sh`](../../scripts/build-rpms.sh#L133).

### 5.3 Slurm source RPM requirements

The validated `slurm-26.05.5-1.el10.src.rpm` declares:

| Requirement group | Declared requirements |
|---|---|
| Toolchain | `autoconf`, `automake`, `gcc`, `make`, `pkgconf` |
| Authentication | `munge-devel`, `munge-libs`, `pam-devel` |
| REST and serialization | `libjwt-devel >= 1.10.0`, `libjwt-devel < 3`, `libyaml-devel >= 0.1.7`, `(llhttp-devel or http-parser-devel)`, `pkgconfig(json-c)` |
| Accounting | `mariadb-devel >= 5.0.0` |
| System integration | `kernel-headers`, `pkgconfig(dbus-1)`, `readline-devel`, `systemd` |
| Language tooling | `perl(ExtUtils::MakeMaker)`, `perl-devel`, `python3` |

This list comes from the generated SRPM rather than from assumptions about an
upstream README. It can be reproduced against any built artifact with:

```bash
rpm -qp --requires /var/packages/SRPMS/slurm-*.src.rpm
rpm -qp --requires /var/packages/SRPMS/munge-*.src.rpm
```

### 5.4 Explicit feature dependencies

[`build-rpms.sh`](../../scripts/build-rpms.sh#L148) installs the following
packages explicitly before running the Slurm spec's `dnf builddep`:

| Build package | Why it is explicit | Resulting capability/runtime library |
|---|---|---|
| `dbus-devel` | Satisfies `pkgconfig(dbus-1)` deterministically | `cgroup_v2` uses `libdbus-1.so.3` |
| `json-c-devel` | Satisfies `pkgconfig(json-c)` | Slurm JSON/data parsing support |
| `kernel-headers` | Required by the Slurm spec and `libbpf-devel` | Build-only headers |
| `libbpf-devel` | Enables current cgroup/eBPF-related build support | Build-only development package |
| `libjwt-devel` | Enables JWT authentication | `auth_jwt` uses `libjwt.so.2` |
| `libyaml-devel` | Enables YAML serialization | `serializer_yaml` uses `libyaml-0.so.2` |
| `llhttp-devel` | Selects the modern side of the spec's HTTP-parser alternative | `http_parser_llhttp_parser` uses `libllhttp.so.9.1` |
| `mariadb-connector-c-devel` | Provides the MariaDB/MySQL client development interface required by `mariadb-devel` | `accounting_storage_mysql` uses `libmariadb.so.3` |
| `s2n-tls-devel` | Enables Slurm internal TLS even though it is not a mandatory SRPM BuildRequires | `tls_s2n` uses `libs2n.so.1` |

The script upgrades `kernel-headers` from Stream AppStream with all other
repositories disabled before installing this feature set. The comment and
command at [`scripts/build-rpms.sh`](../../scripts/build-rpms.sh#L148) explain
the reason: current Stream `libbpf-devel` requires headers newer than the UBI
10.2 GA package. This replacement is confined to the builder but is direct
evidence that the builder is not a pure UBI 10.2 package environment.

### 5.5 Build outputs

The builder copies every generated binary RPM into `/var/packages/RPMS` and
both SRPMs into `/var/packages/SRPMS`. It does not copy downloaded third-party
dependency RPMs into `/var/packages`.

[`build_verify_manifest`](../../scripts/lib/build-common.sh#L170) requires the
manifest to describe exactly the RPM files in those two directories and
verifies every digest. [`build_verify_manifest_sources`](../../scripts/lib/build-common.sh#L207)
additionally matches the MUNGE and Slurm version/digest headers against the
approved version file.

Consequently, `BUILD-MANIFEST` proves the identity of the locally built
MUNGE/Slurm artifacts. It does not prove which NEVRAs from UBI, EPEL, or
Stream were consumed during `dnf builddep`. That evidence must come from the
CI dependency lock/report, DNF transaction output, and image SBOM.

## 6. Runtime dependencies

### 6.1 Locally built RPM selection

[`install-runtime.sh`](../../scripts/install-runtime.sh#L50) selects exactly
one RPM for each of these package names:

- `munge` and `munge-libs`;
- `slurm`;
- `slurm-slurmctld`;
- `slurm-slurmd`;
- `slurm-slurmdbd`; and
- `slurm-slurmrestd`.

Development, debug, compatibility, Perl, PMI, Slurm PAM, example and source
packages remain under `/var/packages` for evidence but are not installed.

### 6.2 Explicit operating-system packages

The runtime installer explicitly requests `bash`, `ca-certificates`, `curl`,
`hostname`, `iproute`, `openssl`, `procps-ng`, `python3`, `python3-PyMySQL`,
`shadow-utils`, and `util-linux`. DNF also installs the shared-library
providers required by the selected local RPMs.

With `--with-integration-database`, the script additionally requests
`mariadb-server`. This is the default used by the local all-in-one
[`Dockerfile`](../../Dockerfile#L60). `--without-integration-database` omits
the server for a production role image using an external database; it does
not remove the MariaDB connector needed by `slurmdbd`.

### 6.3 Observed resolution in the validated image

The following records came from the final runtime DNF transaction for the
validated image. They are evidence for that build, not permanent promises
about moving public repositories:

| Package | NEVRA/version observed | Repository selected |
|---|---|---|
| `jansson` | `2.14-3.el10.x86_64` | `ubi-10-for-x86_64-baseos-rpms` |
| `libjwt` | `2.1.2-1.el10_1.x86_64` | `epel` |
| `llhttp` | `9.1.3-10.el10.x86_64` | `stream-appstream` |
| `mariadb-connector-c` | `3.4.4-2.el10_2.x86_64` | `ubi-10-for-x86_64-baseos-rpms` |
| `mariadb-server` | `10.11.18-1.el10.x86_64` | `stream-appstream` |
| `python3-PyMySQL` | `1.1.1-3.el10.noarch` | `ubi-10-for-x86_64-appstream-rpms` |
| `s2n-tls` | `1.5.10-2.el10_1.x86_64` | `epel` |

The MariaDB server also pulled Stream MariaDB client/common/error-message
packages and optional server utilities. Those packages are needed only by the
simulation database role and should not be present in a minimal production
controller, compute, REST, broker, or `slurmdbd` image.

An RPM's `Vendor` header is not repository provenance. For example, packages
from a compatible repository may retain a vendor string from their source
build configuration. The repository identifier printed in the DNF
transaction is the relevant resolution evidence.

### 6.4 Plugin-to-library verification

The installer verifies plugin presence and runs `ldd` to reject unresolved
dependencies at [`scripts/install-runtime.sh`](../../scripts/install-runtime.sh#L101).
The configuration phase performs the broader plugin-presence assertion at
[`scripts/configure-runtime.sh`](../../scripts/configure-runtime.sh#L199).

The validated image resolves these important feature edges:

| Slurm plugin | Important shared library | Owning runtime RPM |
|---|---|---|
| `auth_jwt.so` | `libjwt.so.2` | `libjwt` |
| `auth_jwt.so` | `libjansson.so.4` | `jansson` |
| `http_parser_llhttp_parser.so` | `libllhttp.so.9.1` | `llhttp` |
| `serializer_yaml.so` | `libyaml-0.so.2` | `libyaml` |
| `tls_s2n.so` | `libs2n.so.1` | `s2n-tls` |
| `accounting_storage_mysql.so` | `libmariadb.so.3` | `mariadb-connector-c` |
| `cgroup_v2.so` | `libdbus-1.so.3`, `libsystemd.so.0` | `dbus-libs`, `systemd-libs` |

`rest_auth_jwt.so` relies on Slurm's common runtime linkage rather than
directly linking `libjwt` in the observed `ldd` output; the JWT library is
still required by the installed authentication feature set.

## 7. What is and is not reproducible today

The current implementation pins and verifies:

- MUNGE and Slurm source versions and archive digests;
- the EPEL release bootstrap RPM digest;
- the CentOS signing-key digest;
- every locally built RPM/SRPM path and digest; and
- the source pins named by the local package manifest.

It does not currently pin:

- the UBI base image by OCI digest;
- a UBI, EPEL, or CentOS Stream repository snapshot;
- repository metadata checksums in the Git checkout;
- the complete resolved build dependency NEVRA set;
- the complete resolved runtime dependency NEVRA set; or
- third-party dependency RPM payloads under `/var/packages`.

Therefore, the build is source-pinned and produces auditable artifacts, but a
future rebuild is not guaranteed to be bit-for-bit identical. Repository
updates can change dependency selection without changing
`build/versions.conf`.

## 8. Production repository profiles

### 8.1 Red Hat-supported profile

For an environment requiring Red Hat support, use an approved RHEL 10 builder
or UBI builder running on subscribed RHEL infrastructure. Resolve Red Hat
content through entitled BaseOS, AppStream and CodeReady Builder repositories,
preferably exposed through immutable Satellite content views or an equivalent
internal mirror.

Some required functionality may still depend on packages that are not in the
supported RHEL set. In the validated public build, `libjwt` and `s2n-tls` came
from EPEL. The production owner must explicitly choose one of these policies:

- accept those packages as third-party components with separate support and
  vulnerability ownership;
- rebuild and sign them into an internally governed RPM repository; or
- disable the dependent Slurm feature only if the approved architecture no
  longer requires it.

JWT and internal Slurm TLS are required by this repository's current
production design, so silently disabling them is not an acceptable fallback.

Subscription certificates, registry credentials and Satellite credentials
must be supplied as ephemeral CI/build secrets. They must not be copied into
the build context, image layers, `/var/packages`, an SBOM, build log, or OCI
label.

### 8.2 Subscription-free profile

For a subscription-free deployment, using CentOS Stream 10 consistently as
both builder and runtime is more coherent than mixing a UBI base with Stream
runtime packages. Another option is to retain UBI but build every missing
dependency into internally reviewed RPMs.

Either choice is a different support contract from RHEL. Mirror and snapshot
all external repositories, sign internal metadata and packages, and promote
the same image digest through environments.

### 8.3 Local simulation profile

The current UBI plus EPEL plus Stream profile prioritizes a self-contained,
subscription-free simulation that exercises all required Slurm plugins. Its
package signatures, source digests, manifest validation and runtime checks are
useful security controls, but they do not transform moving community
repositories into a supported or reproducible RHEL content set.

## 9. Required CI provenance evidence

Before promoting a production image, CI should retain:

1. the Git commit and reviewed `build/versions.conf`;
2. the resolved base-image OCI digest;
3. immutable repository snapshot/content-view identifiers;
4. the repository ID, epoch, name, version, release and architecture for every
   installed build and runtime RPM;
5. source-RPM name, RPM signature identity and payload checksum where
   available;
6. `/var/packages/BUILD-MANIFEST`;
7. the final image SBOM and vulnerability-policy result;
8. `ldd`/plugin validation output;
9. the final OCI digest and signing/provenance attestation; and
10. acceptance-test results for scheduler, accounting, REST workflow and node
    lifecycle behavior.

Useful inspection commands include:

```bash
# Direct BuildRequires from the artifacts, independent of source-tree prose.
rpm -qp --requires /var/packages/SRPMS/munge-*.src.rpm
rpm -qp --requires /var/packages/SRPMS/slurm-*.src.rpm

# Installed identity. Add the transaction repository ID from captured DNF
# output; Vendor alone does not identify the repository used.
rpm -qa --qf '%{EPOCHNUM}:%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\t%{VENDOR}\t%{SOURCERPM}\n' \
  | sort

# Runtime library owners and unresolved-link check.
rpm -qf /usr/lib64/libjwt.so.2 /usr/lib64/libllhttp.so.9.1 \
  /usr/lib64/libyaml-0.so.2 /usr/lib64/libs2n.so.1 \
  /usr/lib64/libmariadb.so.3
ldd /usr/lib64/slurm/auth_jwt.so
ldd /usr/lib64/slurm/http_parser_llhttp_parser.so
ldd /usr/lib64/slurm/tls_s2n.so
ldd /usr/lib64/slurm/accounting_storage_mysql.so
```

Generate these records during the build transaction. Do not rely on querying
the live public repositories later: their contents may have changed.

## 10. Change and incident rules

- Treat any repository addition, removal, priority change, key change, or
  package-origin change as a supply-chain change requiring review.
- Never edit `BUILD-MANIFEST` to make an artifact pass verification.
- If a pinned bootstrap digest changes unexpectedly, stop and investigate
  before updating the pin.
- If a dependency resolves from a different repository than the approved
  lock/report, fail the build.
- If `ldd` reports `not found`, fail image construction rather than disabling
  the affected plugin.
- If a CVE requires a dependency update, rebuild the complete image from the
  approved snapshot, produce a new SBOM and digest, and rerun acceptance.
- Never install or update packages interactively in a running Slurm role.

## 11. Implementation index

| Concern | Authoritative implementation |
|---|---|
| Public versions and bootstrap digests | [`build/versions.conf`](../../build/versions.conf) |
| Safe version-file parser | [`scripts/lib/build-common.sh`](../../scripts/lib/build-common.sh#L69) |
| EPEL bootstrap and CentOS key | [`scripts/lib/build-common.sh`](../../scripts/lib/build-common.sh#L137) |
| Stream URLs, GPG check and priority | [`scripts/lib/build-common.sh`](../../scripts/lib/build-common.sh#L156) |
| MUNGE/Slurm source URLs and build commands | [`scripts/build-rpms.sh`](../../scripts/build-rpms.sh#L125) |
| Explicit Slurm feature dependencies | [`scripts/build-rpms.sh`](../../scripts/build-rpms.sh#L148) |
| Package manifest generation | [`scripts/build-rpms.sh`](../../scripts/build-rpms.sh#L166) |
| Manifest and source-pin verification | [`scripts/lib/build-common.sh`](../../scripts/lib/build-common.sh#L170) |
| Selected runtime RPMs and OS packages | [`scripts/install-runtime.sh`](../../scripts/install-runtime.sh#L47) |
| Runtime executable/plugin/link verification | [`scripts/install-runtime.sh`](../../scripts/install-runtime.sh#L101) |
| Docker stage orchestration | [`Dockerfile`](../../Dockerfile) |

See also the broader
[build and runtime implementation](build-and-runtime.md) and
[production operations runbook](production-runbook.md).
