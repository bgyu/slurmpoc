ARG UBI_IMAGE=registry.access.redhat.com/ubi10/ubi:10.2

# Versions and digests are global so both stages can record exactly what was
# built. A version override without its matching digest intentionally fails:
# silently following a changed release archive is not a reproducible build.
ARG SLURM_VERSION=26.05.5
ARG SLURM_SHA256=4680cafc82b0689865c885eb703441e7ebf764896c40fd03a50f2fcb60664f68
ARG MUNGE_VERSION=0.5.18
ARG MUNGE_SHA256=39c3ec6ef5604bfa206e8aa10fc05d5119040f6de4a554bc0fb98ca1aed838dc
ARG EPEL_RELEASE_SHA256=8ff46bb59651203a801759627c8c1829c6fe610e031f93c83747ba0832bb97ed
ARG CENTOS_GPG_KEY_SHA256=5af55449d6c9bc594e2e2fb7222374cb25a8ad2d8ea6ce3de894a3201944daa2


FROM ${UBI_IMAGE} AS rpm-builder

ARG SLURM_VERSION
ARG SLURM_SHA256
ARG MUNGE_VERSION
ARG MUNGE_SHA256
ARG EPEL_RELEASE_SHA256
ARG CENTOS_GPG_KEY_SHA256

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# EPEL supplies libjwt and s2n-tls. UBI intentionally publishes only a
# subset of RHEL packages, so the signed, ABI-compatible CentOS Stream repos
# are added to the individual commands which need the omitted development
# headers. Keeping these repos command-scoped prevents an accidental future
# install from changing provenance without being visible in this file.
RUN dnf -y install dnf-plugins-core curl ca-certificates rpm-build rpmdevtools \
        tar xz bzip2 gzip procps-ng && \
    curl --fail --location --retry 5 \
        --output /tmp/epel-release.noarch.rpm \
        https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm && \
    printf '%s  %s\n' "${EPEL_RELEASE_SHA256}" \
        /tmp/epel-release.noarch.rpm | sha256sum --check --strict - && \
    dnf -y install /tmp/epel-release.noarch.rpm && \
    curl --fail --location --retry 5 \
        --output /tmp/RPM-GPG-KEY-CentOS-Official \
        https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official-SHA256 && \
    printf '%s  %s\n' "${CENTOS_GPG_KEY_SHA256}" \
        /tmp/RPM-GPG-KEY-CentOS-Official | sha256sum --check --strict - && \
    rpm --import /tmp/RPM-GPG-KEY-CentOS-Official && \
    rm -f /tmp/RPM-GPG-KEY-CentOS-Official /tmp/epel-release.noarch.rpm && \
    rpmdev-setuptree

WORKDIR /tmp/sources

# MUNGE is built first because Slurm's auth/munge plugin needs its headers.
# The checksum is verified before either tar or rpmbuild interprets content
# from the archive; this is a deliberate boundary against poisoned spec files.
RUN test "${MUNGE_SHA256}" != "" && \
    curl --fail --location --retry 5 \
        --output "munge-${MUNGE_VERSION}.tar.xz" \
        "https://github.com/dun/munge/releases/download/munge-${MUNGE_VERSION}/munge-${MUNGE_VERSION}.tar.xz" && \
    printf '%s  %s\n' "${MUNGE_SHA256}" \
        "munge-${MUNGE_VERSION}.tar.xz" | sha256sum --check --strict - && \
    tar -xOf "munge-${MUNGE_VERSION}.tar.xz" \
        "munge-${MUNGE_VERSION}/munge.spec" > /tmp/munge.spec && \
    dnf builddep -y /tmp/munge.spec && \
    rpmbuild -ta --with check "munge-${MUNGE_VERSION}.tar.xz" && \
    dnf -y install /root/rpmbuild/RPMS/*/munge-*.rpm

# REST, JWT, YAML, TLS and native cgroup-v2 support are required build
# features, not best-effort autodetection. The explicit rpmbuild switches and
# post-build assertions turn a missing repository package into a build failure
# instead of producing an image that starts but lacks a security plugin.
RUN test "${SLURM_SHA256}" != "" && \
    curl --fail --location --retry 5 \
        --output "slurm-${SLURM_VERSION}.tar.bz2" \
        "https://download.schedmd.com/slurm/slurm-${SLURM_VERSION}.tar.bz2" && \
    printf '%s  %s\n' "${SLURM_SHA256}" \
        "slurm-${SLURM_VERSION}.tar.bz2" | sha256sum --check --strict - && \
    tar -xOf "slurm-${SLURM_VERSION}.tar.bz2" \
        "slurm-${SLURM_VERSION}/slurm.spec" > /tmp/slurm.spec && \
    stream_repos=( \
        --repofrompath=stream-baseos,https://mirror.stream.centos.org/10-stream/BaseOS/\$basearch/os/ \
        --repofrompath=stream-appstream,https://mirror.stream.centos.org/10-stream/AppStream/\$basearch/os/ \
        --repofrompath=stream-crb,https://mirror.stream.centos.org/10-stream/CRB/\$basearch/os/ \
        --setopt=stream-baseos.gpgcheck=1 \
        --setopt=stream-appstream.gpgcheck=1 \
        --setopt=stream-crb.gpgcheck=1 \
        --setopt=stream-baseos.priority=200 \
        --setopt=stream-appstream.priority=200 \
        --setopt=stream-crb.priority=200 \
    ) && \
    dnf -y "${stream_repos[@]}" install \
        dbus-devel json-c-devel kernel-headers libbpf-devel libjwt-devel \
        libyaml-devel llhttp-devel mariadb-connector-c-devel s2n-tls-devel && \
    dnf -y "${stream_repos[@]}" builddep /tmp/slurm.spec && \
    rpmbuild -ta --with cgroupv2 --with jwt --with slurmrestd \
        --with yaml "slurm-${SLURM_VERSION}.tar.bz2" && \
    find /root/rpmbuild/RPMS -type f \
        -name "slurm-slurmrestd-${SLURM_VERSION}-*.rpm" -print -quit | grep -q .

# Preserve both source and binary artifacts for audit/rebuild workflows. The
# manifest records upstream inputs and hashes every RPM copied to the runtime
# image; it contains no build-time secret or mutable timestamp.
RUN mkdir -p /var/packages/RPMS /var/packages/SRPMS && \
    find /root/rpmbuild/RPMS -type f -name '*.rpm' \
        -exec cp -v {} /var/packages/RPMS/ \; && \
    find /root/rpmbuild/SRPMS -type f -name '*.rpm' \
        -exec cp -v {} /var/packages/SRPMS/ \; && \
    { \
        printf 'MUNGE_VERSION=%s\nMUNGE_SHA256=%s\n' \
            "${MUNGE_VERSION}" "${MUNGE_SHA256}"; \
        printf 'SLURM_VERSION=%s\nSLURM_SHA256=%s\n' \
            "${SLURM_VERSION}" "${SLURM_SHA256}"; \
        find /var/packages/RPMS /var/packages/SRPMS -type f -name '*.rpm' \
            -print0 | sort -z | xargs -0 sha256sum; \
    } > /var/packages/BUILD-MANIFEST


FROM ${UBI_IMAGE} AS runtime

ARG SLURM_VERSION
ARG SLURM_SHA256
ARG MUNGE_VERSION
ARG MUNGE_SHA256
ARG EPEL_RELEASE_SHA256
ARG CENTOS_GPG_KEY_SHA256

LABEL org.opencontainers.image.title="Production-oriented Slurm runtime" \
      org.opencontainers.image.version="${SLURM_VERSION}" \
      org.opencontainers.image.source="https://download.schedmd.com/slurm/" \
      org.opencontainers.image.slurm.version="${SLURM_VERSION}" \
      org.opencontainers.image.slurm.sha256="${SLURM_SHA256}" \
      org.opencontainers.image.munge.version="${MUNGE_VERSION}" \
      org.opencontainers.image.munge.sha256="${MUNGE_SHA256}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=rpm-builder /var/packages /var/packages

# MariaDB remains in this image only for the self-contained integration role;
# production slurmdbd points at an externally operated database. Python and
# PyMySQL are intentionally installed from signed RPM repositories so the same
# immutable image can also host the separately unprivileged workflow broker.
RUN dnf -y install dnf-plugins-core ca-certificates curl && \
    curl --fail --location --retry 5 \
        --output /tmp/epel-release.noarch.rpm \
        https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm && \
    printf '%s  %s\n' "${EPEL_RELEASE_SHA256}" \
        /tmp/epel-release.noarch.rpm | sha256sum --check --strict - && \
    dnf -y install /tmp/epel-release.noarch.rpm && \
    curl --fail --location --retry 5 \
        --output /tmp/RPM-GPG-KEY-CentOS-Official \
        https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official-SHA256 && \
    printf '%s  %s\n' "${CENTOS_GPG_KEY_SHA256}" \
        /tmp/RPM-GPG-KEY-CentOS-Official | sha256sum --check --strict - && \
    rpm --import /tmp/RPM-GPG-KEY-CentOS-Official && \
    stream_repos=( \
        --repofrompath=stream-baseos,https://mirror.stream.centos.org/10-stream/BaseOS/\$basearch/os/ \
        --repofrompath=stream-appstream,https://mirror.stream.centos.org/10-stream/AppStream/\$basearch/os/ \
        --repofrompath=stream-crb,https://mirror.stream.centos.org/10-stream/CRB/\$basearch/os/ \
        --setopt=stream-baseos.gpgcheck=1 \
        --setopt=stream-appstream.gpgcheck=1 \
        --setopt=stream-crb.gpgcheck=1 \
        --setopt=stream-baseos.priority=200 \
        --setopt=stream-appstream.priority=200 \
        --setopt=stream-crb.priority=200 \
    ) && \
    wanted_rpms=(munge munge-libs slurm slurm-slurmctld slurm-slurmd \
        slurm-slurmdbd slurm-slurmrestd) && \
    runtime_rpms=() && \
    while IFS= read -r rpm_path; do \
        rpm_name=$(rpm -qp --queryformat '%{NAME}' "$rpm_path") || exit 1; \
        for wanted in "${wanted_rpms[@]}"; do \
            if [[ $rpm_name == "$wanted" ]]; then \
                runtime_rpms+=("$rpm_path"); \
            fi; \
        done; \
    done < <(find /var/packages/RPMS -type f -name '*.rpm' \
        ! -name '*-debuginfo-*' ! -name '*-debugsource-*' | sort) && \
    ((${#runtime_rpms[@]} == ${#wanted_rpms[@]})) && \
    dnf -y "${stream_repos[@]}" install "${runtime_rpms[@]}" mariadb-server \
        bash curl hostname iproute openssl procps-ng python3 python3-PyMySQL \
        shadow-utils util-linux && \
    dnf clean all && \
    rm -rf /var/cache/dnf /tmp/RPM-GPG-KEY-CentOS-Official \
        /tmp/epel-release.noarch.rpm

# Numeric identities are part of the multi-host contract: LDAP/SSSD and every
# image/host must resolve a submitting user to the same numbers.  MUNGE's RPM
# creates an implementation-selected system ID, so normalize it carefully and
# fail on a collision rather than silently taking over another account.
RUN munge_uid=$(id -u munge) && munge_gid=$(id -g munge) && \
    if [[ $munge_gid != 64029 ]]; then \
        ! getent group 64029 >/dev/null 2>&1 && groupmod --gid 64029 munge; \
    fi && \
    if [[ $munge_uid != 64029 ]]; then \
        ! getent passwd 64029 >/dev/null 2>&1 && \
            usermod --uid 64029 --gid 64029 --home-dir /var/lib/munge munge; \
    fi && \
    chown -R munge:munge /etc/munge /var/lib/munge /var/log/munge && \
    ! getent group 64030 >/dev/null 2>&1 && ! getent passwd 64030 >/dev/null 2>&1 && \
    groupadd --system --gid 64030 slurm && \
    useradd --system --uid 64030 --gid slurm --home-dir /var/lib/slurm \
        --shell /sbin/nologin --comment "Slurm workload manager" slurm && \
    ! getent group 64031 >/dev/null 2>&1 && ! getent passwd 64031 >/dev/null 2>&1 && \
    groupadd --system --gid 64031 slurmrestd && \
    useradd --system --uid 64031 --gid slurmrestd \
        --home-dir /var/lib/slurmrestd --shell /sbin/nologin \
        --comment "Slurm REST daemon" slurmrestd && \
    ! getent group 64032 >/dev/null 2>&1 && ! getent passwd 64032 >/dev/null 2>&1 && \
    groupadd --system --gid 64032 workflow && \
    useradd --system --uid 64032 --gid workflow --home-dir /var/lib/workflow \
        --shell /sbin/nologin --comment "Workflow demo identity" workflow && \
    test "$(id -u munge)" = 64029 && test "$(id -g munge)" = 64029 && \
    test "$(id -u slurm)" = 64030 && test "$(id -g slurm)" = 64030 && \
    test "$(id -u slurmrestd)" = 64031 && \
    test "$(id -u workflow)" = 64032 && \
    test "$(id -u mysql)" = 27 && test "$(id -g mysql)" = 27

# Runtime directories are pre-created with least privilege. Entrypoint startup
# repeats the relevant checks because named volumes can mask image ownership.
RUN install -d -o root -g slurm -m 0755 /etc/slurm/profiles \
        /etc/slurm/profiles/simulation /etc/slurm/profiles/production \
        /run/slurm /run/slurm/config && \
    install -d -o slurm -g slurm -m 0755 /run/slurm/pids && \
    install -d -o munge -g munge -m 0700 /var/lib/munge && \
    install -d -o munge -g munge -m 0755 /run/munge /var/log/munge && \
    install -d -o slurm -g slurm -m 0750 /var/log/slurm && \
    install -d -o slurm -g slurm -m 0700 /var/spool/slurmctld && \
    install -d -o root -g root -m 0755 /var/spool/slurmd \
        /var/lib/slurm/job_container && \
    install -d -o slurmrestd -g slurmrestd -m 0750 \
        /run/slurmrestd /var/lib/slurmrestd && \
    install -d -o workflow -g workflow -m 0750 /var/lib/workflow

COPY config/slurm.conf /etc/slurm/profiles/simulation/slurm.conf
COPY config/cgroup.conf /etc/slurm/profiles/simulation/cgroup.conf
COPY config/slurmdbd.conf.template /etc/slurm/profiles/simulation/slurmdbd.conf.template
COPY config/slurm.production.conf /etc/slurm/profiles/production/slurm.conf
COPY config/cgroup.production.conf /etc/slurm/profiles/production/cgroup.conf
COPY config/job_container.production.conf /etc/slurm/profiles/production/job_container.conf
COPY config/slurmdbd.production.conf.template /etc/slurm/profiles/production/slurmdbd.conf.template
COPY scripts/cluster-entrypoint.sh /usr/local/sbin/cluster-entrypoint
COPY scripts/cluster-healthcheck.sh /usr/local/sbin/cluster-healthcheck
COPY scripts/dev-token-renewer.sh /usr/local/sbin/dev-token-renewer
COPY deploy/lifecycle/lifecycle-common.sh /usr/local/libexec/slurm/lifecycle-common.sh
COPY deploy/lifecycle/slurm-resume-program /usr/local/libexec/slurm/slurm-resume-program
COPY deploy/lifecycle/slurm-suspend-program /usr/local/libexec/slurm/slurm-suspend-program
COPY workflow /opt/slurm-workflow/workflow
COPY examples/workflow /opt/slurm-workflow/examples

# The simulation symlinks keep ad-hoc CLI commands and `podman compose exec`
# compatible. Daemons always use the profile copied to /run via SLURM_CONF,
# which also works with a read-only image filesystem in production.
RUN ln -s profiles/simulation/slurm.conf /etc/slurm/slurm.conf && \
    ln -s profiles/simulation/cgroup.conf /etc/slurm/cgroup.conf && \
    chmod 0755 /usr/local/sbin/cluster-entrypoint \
        /usr/local/sbin/cluster-healthcheck \
        /usr/local/sbin/dev-token-renewer \
        /usr/local/libexec/slurm/lifecycle-common.sh \
        /usr/local/libexec/slurm/slurm-resume-program \
        /usr/local/libexec/slurm/slurm-suspend-program && \
    chown -R root:root /opt/slurm-workflow && \
    find /opt/slurm-workflow -type d -exec chmod 0755 {} + && \
    find /opt/slurm-workflow -type f -exec chmod 0644 {} + && \
    find /etc/slurm/profiles -type f -exec chown root:slurm {} + && \
    find /etc/slurm/profiles -type f -exec chmod 0644 {} + && \
    command -v slurmctld && command -v slurmd && command -v slurmdbd && \
    command -v slurmrestd && \
    test -f /usr/lib64/slurm/auth_jwt.so && \
    test -f /usr/lib64/slurm/rest_auth_jwt.so && \
    test -f /usr/lib64/slurm/rest_auth_local.so && \
    test -f /usr/lib64/slurm/data_parser_v0_0_45.so && \
    test -f /usr/lib64/slurm/cgroup_v2.so && \
    test -f /usr/lib64/slurm/task_cgroup.so && \
    test -f /usr/lib64/slurm/namespace_tmpfs.so && \
    test -f /usr/lib64/slurm/tls_s2n.so && \
    test -f /usr/lib64/slurm/http_parser_llhttp_parser.so && \
    test -f /usr/lib64/slurm/serializer_yaml.so && \
    test -f /usr/lib64/slurm/accounting_storage_mysql.so && \
    tail -n +5 /var/packages/BUILD-MANIFEST | sha256sum --check --strict && \
    for plugin in auth_jwt rest_auth_jwt data_parser_v0_0_45 cgroup_v2 \
            namespace_tmpfs tls_s2n accounting_storage_mysql; do \
        ! ldd "/usr/lib64/slurm/${plugin}.so" | grep -q 'not found'; \
    done

EXPOSE 3306 6817 6818 6819 6820

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 \
    CMD ["/usr/local/sbin/cluster-healthcheck"]

ENTRYPOINT ["/usr/local/sbin/cluster-entrypoint"]
