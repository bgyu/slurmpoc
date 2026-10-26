from __future__ import annotations

import os
from pathlib import Path
import unittest

from workflow.config import BrokerConfig
from workflow.errors import ServiceUnavailable
from workflow.security import CapabilityFiles, read_secret_file, secret_matches
from workflow.tests.helpers import Workspace


class ConfigSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()

    def tearDown(self) -> None:
        self.workspace.close()

    def test_local_config_accepts_file_backed_auth_and_insecure_dev_rest(self) -> None:
        api = self.workspace.secret("api.token", "local-secret")
        jwt = self.workspace.secret("slurm.jwt", "jwt-secret")
        env = {
            "BROKER_PROFILE": "local",
            "BROKER_AUTH_MODE": "local-token",
            "BROKER_API_TOKEN_FILE": str(api),
            "WORKFLOW_DB": str(self.workspace.root / "workflow.sqlite3"),
            "WORKFLOW_CAPABILITY_DIR": str(self.workspace.capabilities),
            "WORKFLOW_CAPABILITY_JOB_DIR": str(self.workspace.capabilities),
            "WORKFLOW_CONTROL_DIR": str(self.workspace.shared / ".control"),
            "WORKFLOW_ALLOWED_FILE_ROOT": str(self.workspace.shared),
            "WORKFLOW_MASTER_SCRIPT": str(self.workspace.scripts / "master.sbatch"),
            "WORKFLOW_WORKER_SCRIPT": str(self.workspace.scripts / "worker.sbatch"),
            "WORKFLOW_FINALIZER_SCRIPT": str(
                self.workspace.scripts / "finalizer.sbatch"
            ),
            "WORKFLOW_HELPER_DIR": str(self.workspace.scripts),
            "WORKFLOW_PUBLIC_BASE_URL": "http://workflow:8088",
            "SLURM_REST_URL": "http://slurmrestd:6820",
            "SLURM_REST_ALLOW_INSECURE_HTTP": "true",
            "SLURM_REST_TOKEN_FILE": str(jwt),
        }
        config = BrokerConfig.from_env(env)
        self.assertEqual(config.profile, "local")
        self.assertEqual(config.slurm_submit_user, "workflow")

    def test_production_requires_proxy_database_tls_and_https(self) -> None:
        config = self.workspace.config(
            profile="production",
            auth_mode="trusted-proxy",
            trusted_proxy_token_file=self.workspace.root / "proxy.token",
            trusted_proxy_cidrs=("10.0.0.0/8",),
            callback_trusted_cidrs=("10.0.0.0/8",),
            database_url=None,
            public_base_url="https://workflow.internal",
            slurm_rest_url="https://slurm.internal",
            allow_insecure_slurm_http=False,
        )
        with self.assertRaisesRegex(ValueError, "refuses SQLite"):
            config.validate()

        config = self.workspace.config(
            profile="production",
            auth_mode="trusted-proxy",
            trusted_proxy_token_file=self.workspace.root / "proxy.token",
            trusted_proxy_cidrs=("not-a-cidr",),
            callback_trusted_cidrs=("10.0.0.0/8",),
            database_url="mariadb://db.internal/workflows",
            database_user="workflow",
            database_password_file=self.workspace.root / "db.password",
            database_ca_file=self.workspace.root / "ca.pem",
            public_base_url="https://workflow.internal",
            slurm_rest_url="https://slurm.internal",
            allow_insecure_slurm_http=False,
        )
        with self.assertRaisesRegex(ValueError, "invalid trusted proxy CIDR"):
            config.validate()

    def test_network_rest_requires_jwt_file_and_rejects_url_credentials(self) -> None:
        without_token = self.workspace.config(slurm_token_file=None)
        with self.assertRaisesRegex(ValueError, "SLURM_REST_TOKEN_FILE"):
            without_token.validate()
        credentials = self.workspace.config(
            slurm_rest_url="https://user:password@slurm.internal"
        )
        with self.assertRaisesRegex(ValueError, "credentials"):
            credentials.validate()

    def test_secret_reader_rejects_symlink_and_broad_mode_and_supports_rotation(self) -> None:
        secret = self.workspace.secret("auth.secret", "first")
        self.assertEqual(read_secret_file(secret, purpose="test"), "first")
        self.assertTrue(secret_matches("first", secret, purpose="test"))
        secret.write_text("second\n", encoding="ascii")
        os.chmod(secret, 0o600)
        self.assertTrue(secret_matches("second", secret, purpose="test"))
        os.chmod(secret, 0o644)
        with self.assertRaises(ServiceUnavailable):
            read_secret_file(secret, purpose="test")
        link = self.workspace.root / "secret.link"
        link.symlink_to(secret)
        with self.assertRaises(ServiceUnavailable):
            read_secret_file(link, purpose="test")

    def test_capability_file_is_exclusive_private_and_non_overwriting(self) -> None:
        files = CapabilityFiles(
            self.workspace.root / "private-capabilities",
            Path("/job/capabilities"),
        )
        job_path = files.stage("workflow-one", "wfc_secret")
        private = files.private_path("workflow-one")
        self.assertEqual(job_path, Path("/job/capabilities/workflow-one.cap"))
        self.assertEqual(private.stat().st_mode & 0o777, 0o400)
        with self.assertRaises(FileExistsError):
            files.stage("workflow-one", "wfc_other")


if __name__ == "__main__":
    unittest.main()
