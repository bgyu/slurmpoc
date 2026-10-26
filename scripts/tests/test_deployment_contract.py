"""Static regression tests for production and local deployment boundaries.

These tests do not pretend to replace target-account failover, cgroup, PKI, or
IAM tests. They protect the high-value fail-closed settings from being removed
by an otherwise innocuous configuration edit.
"""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class DeploymentContractTests(unittest.TestCase):
    def test_production_slurm_isolation_and_authentication_are_mandatory(self) -> None:
        config = read("config/slurm.production.conf")
        required = {
            "AuthType=auth/munge",
            "CredType=cred/munge",
            "AuthAltTypes=auth/jwt",
            "TLSType=tls/s2n",
            "CommunicationParameters=disable_http",
            "DisableRootJobs=YES",
            "ProctrackType=proctrack/cgroup",
            "TaskPlugin=task/cgroup,task/affinity",
            "JobAcctGatherType=jobacct_gather/cgroup",
            "NamespaceType=namespace/tmpfs",
            "PrologFlags=Contain",
            "AccountingStorageEnforce=associations,limits,qos,safe",
        }
        settings = {
            line.strip()
            for line in config.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(required.issubset(settings), required - settings)
        self.assertIn("disable_token_creation", config)
        self.assertIn("jwks=/run/slurm/config/slurm-jwks.json", config)

    def test_production_partitions_have_explicit_bounded_membership(self) -> None:
        config = read("config/slurm.production.conf")
        self.assertIn("MaxNodeCount=1000", config)
        self.assertIn("NodeName=compute[0001-0100]", config)
        self.assertIn(
            "PartitionName=standard Nodes=compute[0001-0100]", config
        )
        active_lines = [
            line.strip()
            for line in config.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(
            any(line.startswith("PartitionName=") and "Nodes=ALL" in line for line in active_lines)
        )

    def test_production_compute_requires_rootful_cgroup_v2_contract(self) -> None:
        cgroup = read("config/cgroup.production.conf")
        unit = read("deploy/quadlet/slurmd@.container")
        for setting in (
            "ConstrainCores=yes",
            "ConstrainRAMSpace=yes",
            "ConstrainSwapSpace=yes",
            "ConstrainDevices=yes",
        ):
            self.assertIn(setting, cgroup)
        self.assertIn("Delegate=yes", unit)
        self.assertIn("CgroupsMode=enabled", unit)
        self.assertIn("/sys/fs/cgroup:/sys/fs/cgroup:rw", unit)
        self.assertNotIn("Privileged=true", unit)

    def test_restd_stages_public_jwks_and_binds_only_loopback(self) -> None:
        unit = read("deploy/quadlet/slurmrestd@.container")
        self.assertIn("Environment=SLURM_JWKS_FILE=/run/site/slurm-jwks.json", unit)
        self.assertIn(
            "slurm-jwks.json:/run/site/slurm-jwks.json:ro", unit
        )
        self.assertNotIn(
            "slurm-jwks.json:/run/slurm/config/slurm-jwks.json", unit
        )
        self.assertIn(
            "Environment=SLURMRESTD_LISTEN=https://127.0.0.1:6820", unit
        )

    def test_production_broker_is_nonroot_and_capability_paths_are_separate(self) -> None:
        unit = read("deploy/quadlet/workflow-broker.container")
        self.assertIn("User=64032", unit)
        self.assertIn("Group=64032", unit)
        self.assertIn("DropCapability=all", unit)
        self.assertIn(
            "Environment=WORKFLOW_CAPABILITY_DIR=/run/workflow/capabilities", unit
        )
        self.assertIn(
            "Environment=WORKFLOW_CAPABILITY_JOB_DIR=/run/slurm-job-capabilities",
            unit,
        )
        self.assertNotIn("SLURM_REST_TOKEN=", unit)

    def test_local_controller_has_stable_direct_podman_name(self) -> None:
        compose = read("compose.yaml")
        self.assertIn("container_name: controller", compose)
        self.assertIn('"127.0.0.1:${WORKFLOW_PORT:-8088}:8088"', compose)


if __name__ == "__main__":
    unittest.main()
