ARG UBI_IMAGE=registry.access.redhat.com/ubi10/ubi:10.2

# Keep OCI metadata and upgrade overrides visible at the build interface. The
# scripts reject incomplete version/digest pairs and otherwise use the same
# pinned defaults recorded in build/versions.conf.
ARG SLURM_VERSION=26.05.5
ARG SLURM_SHA256=4680cafc82b0689865c885eb703441e7ebf764896c40fd03a50f2fcb60664f68
ARG MUNGE_VERSION=0.5.18
ARG MUNGE_SHA256=39c3ec6ef5604bfa206e8aa10fc05d5119040f6de4a554bc0fb98ca1aed838dc


FROM ${UBI_IMAGE} AS rpm-builder

ARG SLURM_VERSION
ARG SLURM_SHA256
ARG MUNGE_VERSION
ARG MUNGE_SHA256

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The builder uses the same command an operator can run manually on an EL10
# build host. Package-manager, source-verification and rpmbuild logic lives in
# the script rather than a Docker-only RUN block.
COPY build/versions.conf /opt/slurm-source/build/versions.conf
COPY scripts/lib/build-common.sh /opt/slurm-source/scripts/lib/build-common.sh
COPY scripts/build-rpms.sh /opt/slurm-source/scripts/build-rpms.sh
RUN chmod 0755 /opt/slurm-source/scripts/build-rpms.sh && \
    /opt/slurm-source/scripts/build-rpms.sh \
        --versions-file /opt/slurm-source/build/versions.conf \
        --output-dir /var/packages \
        --work-dir /var/tmp/slurm-rpm-build \
        --slurm-version "$SLURM_VERSION" \
        --slurm-sha256 "$SLURM_SHA256" \
        --munge-version "$MUNGE_VERSION" \
        --munge-sha256 "$MUNGE_SHA256"


FROM ${UBI_IMAGE} AS runtime

ARG SLURM_VERSION
ARG SLURM_SHA256
ARG MUNGE_VERSION
ARG MUNGE_SHA256

LABEL org.opencontainers.image.title="Production-oriented Slurm runtime" \
      org.opencontainers.image.version="${SLURM_VERSION}" \
      org.opencontainers.image.source="https://download.schedmd.com/slurm/" \
      org.opencontainers.image.slurm.version="${SLURM_VERSION}" \
      org.opencontainers.image.slurm.sha256="${SLURM_SHA256}" \
      org.opencontainers.image.munge.version="${MUNGE_VERSION}" \
      org.opencontainers.image.munge.sha256="${MUNGE_SHA256}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=rpm-builder /var/packages /var/packages
COPY build/versions.conf /opt/slurm-source/build/versions.conf
COPY scripts/lib/build-common.sh /opt/slurm-source/scripts/lib/build-common.sh
COPY scripts/install-runtime.sh /opt/slurm-source/scripts/install-runtime.sh

# Verification occurs before DNF sees a local RPM. MariaDB remains an explicit
# integration-image option; production slurmdbd uses an external HA database.
RUN chmod 0755 /opt/slurm-source/scripts/install-runtime.sh && \
    /opt/slurm-source/scripts/install-runtime.sh \
        --versions-file /opt/slurm-source/build/versions.conf \
        --packages-dir /var/packages \
        --with-integration-database

COPY scripts/configure-runtime.sh /opt/slurm-source/scripts/configure-runtime.sh
COPY config /opt/slurm-source/config
COPY scripts/cluster-entrypoint.sh /opt/slurm-source/scripts/cluster-entrypoint.sh
COPY scripts/cluster-healthcheck.sh /opt/slurm-source/scripts/cluster-healthcheck.sh
COPY scripts/dev-token-renewer.sh /opt/slurm-source/scripts/dev-token-renewer.sh
COPY deploy/lifecycle /opt/slurm-source/deploy/lifecycle
COPY workflow /opt/slurm-source/workflow
COPY examples/workflow /opt/slurm-source/examples/workflow

# Service identities, permissions and immutable assets are installed through
# the same idempotent configuration command available to a manual host setup.
RUN chmod 0755 /opt/slurm-source/scripts/configure-runtime.sh && \
    /opt/slurm-source/scripts/configure-runtime.sh \
        --source-root /opt/slurm-source \
        --with-integration-database && \
    rm -rf -- /opt/slurm-source

EXPOSE 3306 6817 6818 6819 6820

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 \
    CMD ["/usr/local/sbin/cluster-healthcheck"]

ENTRYPOINT ["/usr/local/sbin/cluster-entrypoint"]
