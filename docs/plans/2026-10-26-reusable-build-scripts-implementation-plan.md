# Reusable build scripts implementation plan

Status: completed on 2026-10-26 in commit `6b803ba`. This file preserves the
implementation sequence; current operating details are maintained in
`docs/operations/build-and-runtime.md`.

## 1. Shared configuration and validation

- Add `build/versions.conf` containing pinned public version and digest data.
- Add `scripts/lib/build-common.sh` for strict parsing, root/platform/path
  checks, signed repository bootstrapping, and command-scoped Stream options.
- Add unit tests that execute validation functions without changing the host.

Gate: shell syntax passes; malformed, duplicate, unknown, and incomplete
version files fail; unsafe output/source paths fail.

## 2. RPM construction command

- Move builder dependency setup, source download/checking, `rpmbuild`, package
  collection, and manifest generation into `scripts/build-rpms.sh`.
- Keep work and output paths parameterized and non-destructive.
- Preserve all current Slurm build feature switches and RPM assertions.

Gate: invoke the script in the UBI builder stage and verify RPM/SRPM hashes.

## 3. Runtime installation command

- Move runtime repository setup, exact RPM selection, dependency installation,
  cleanup, executable checks, and library checks into
  `scripts/install-runtime.sh`.
- Provide an explicit integration-database toggle.

Gate: manifest tampering and missing/duplicate required RPMs fail before DNF;
the normal image contains exactly the required installed daemon packages.

## 4. Runtime configuration command

- Move service identity normalization, directory creation, configuration and
  helper installation, permissions, symlinks, and plugin assertions into
  `scripts/configure-runtime.sh`.
- Accept `--source-root`; reject missing or symlinked assets.
- Keep runtime secret creation outside this command.

Gate: repeated invocation succeeds; numeric IDs, modes, plugins, and installed
assets match the runtime contract.

## 5. Thin Dockerfile and documentation

- Replace large `RUN` implementations with copies and invocations of the three
  scripts.
- Keep version build arguments, OCI labels, `/var/packages`, health check, and
  entrypoint unchanged.
- Document manual UBI/RHEL usage and the database toggle in `README.md`.
- Extend deployment-contract tests to ensure Dockerfile/script separation and
  default-version consistency.

Gate: Dockerfile contains no embedded `rpmbuild`, user creation, or package
selection implementation.

## 6. End-to-end verification and history

- Run unit, contract, syntax, and diff checks.
- Build the image from the refactored Dockerfile.
- Run package/identity/plugin assertions.
- Restart the local cluster on preserved volumes.
- Run the full local acceptance suite.
- Commit implementation and documentation with a clean worktree.

Completion evidence: two clean UBI 10.2 builds succeeded; the final validated
image was `3473d6c90bdfc743d327a4d593ea915f4a3c3fc65a9b2dcf204597601cc7ace3`.
The full acceptance suite passed manifest/source-pin validation, daemon and job
identity checks, five-node scheduling/accounting, a REST-submitted workflow
with 20 validated chunks, and dynamic-node add/place/drain/remove. The live
controller remained directly inspectable with `podman exec controller sinfo`.
