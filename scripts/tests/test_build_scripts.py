"""Tests for reusable package build/install/configuration interfaces."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "scripts" / "lib" / "build-common.sh"


def run_bash(program: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", program],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class BuildScriptTests(unittest.TestCase):
    def test_pinned_version_file_is_parsed_as_data(self) -> None:
        result = run_bash(
            f"source {COMMON!s}; "
            f"build_load_versions {ROOT / 'build' / 'versions.conf'}; "
            "printf '%s|%s\n' \"$SLURM_VERSION\" \"$MUNGE_VERSION\""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "26.05.5|0.5.18")

    def test_version_parser_rejects_unknown_code_without_executing_it(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary)
            marker = root / "executed"
            version_file = root / "versions.conf"
            version_file.write_text(
                "FORMAT_VERSION=1\n"
                f"EVIL=$(touch${{IFS}}{marker})\n",
                encoding="ascii",
            )
            result = run_bash(
                f"source {COMMON!s}; build_load_versions {version_file!s}"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def test_manifest_requires_exactly_the_available_rpms(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            packages = Path(temporary) / "packages"
            (packages / "RPMS").mkdir(parents=True)
            (packages / "SRPMS").mkdir()
            rpm = packages / "RPMS" / "example-1.0-1.x86_64.rpm"
            srpm = packages / "SRPMS" / "example-1.0-1.src.rpm"
            rpm.write_bytes(b"binary-rpm")
            srpm.write_bytes(b"source-rpm")
            lines = [
                "MUNGE_VERSION=0.5.18",
                "MUNGE_SHA256=" + "a" * 64,
                "SLURM_VERSION=26.05.5",
                "SLURM_SHA256=" + "b" * 64,
            ]
            for path in (rpm, srpm):
                relative = path.relative_to(packages)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                lines.append(f"{digest}  {relative}")
            (packages / "BUILD-MANIFEST").write_text(
                "\n".join(lines) + "\n", encoding="ascii"
            )
            valid = run_bash(
                f"source {COMMON!s}; build_verify_manifest {packages!s}"
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)

            (packages / "RPMS" / "unrecorded-1.0.rpm").write_bytes(b"extra")
            invalid = run_bash(
                f"source {COMMON!s}; build_verify_manifest {packages!s}"
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("exactly", invalid.stderr)

    def test_manifest_sources_must_match_approved_pins(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            packages = Path(temporary) / "packages"
            (packages / "RPMS").mkdir(parents=True)
            (packages / "SRPMS").mkdir()
            (packages / "BUILD-MANIFEST").write_text(
                "MUNGE_VERSION=0.5.18\n"
                + "MUNGE_SHA256="
                + "a" * 64
                + "\nSLURM_VERSION=26.05.4\n"
                + "SLURM_SHA256="
                + "b" * 64
                + "\n",
                encoding="ascii",
            )
            result = run_bash(
                f"source {COMMON!s}; "
                "MUNGE_VERSION=0.5.18; MUNGE_SHA256="
                + "a" * 64
                + "; SLURM_VERSION=26.05.5; SLURM_SHA256="
                + "b" * 64
                + f"; build_verify_manifest_sources {packages!s}"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SLURM_VERSION", result.stderr)

    def test_source_override_requires_version_and_digest_pair(self) -> None:
        result = subprocess.run(
            [
                str(ROOT / "scripts" / "build-rpms.sh"),
                "--slurm-version",
                "99.1.0",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("together", result.stderr)

    def test_dockerfile_delegates_each_phase_to_public_script(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for command in (
            "/opt/slurm-source/scripts/build-rpms.sh",
            "/opt/slurm-source/scripts/install-runtime.sh",
            "/opt/slurm-source/scripts/configure-runtime.sh",
        ):
            self.assertIn(command, dockerfile)
        self.assertNotIn("RUN dnf ", dockerfile)
        self.assertNotIn("useradd --system", dockerfile)
        self.assertNotIn("rpmbuild -", dockerfile)

    def test_docker_defaults_match_versions_file(self) -> None:
        values: dict[str, str] = {}
        for line in (ROOT / "build" / "versions.conf").read_text().splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for key in ("SLURM_VERSION", "SLURM_SHA256", "MUNGE_VERSION", "MUNGE_SHA256"):
            self.assertIn(f"ARG {key}={values[key]}", dockerfile)

    def test_configuration_phase_never_generates_secret_material(self) -> None:
        script = (ROOT / "scripts" / "configure-runtime.sh").read_text(
            encoding="utf-8"
        )
        for forbidden in ("/dev/urandom", "openssl rand", "MUNGE_KEY_FILE="):
            self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
