"""Focused unit tests for the host-side workflow integration client."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "submit-workflow.py"
SPEC = importlib.util.spec_from_file_location("submit_workflow_client", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


class WorkflowClientTests(unittest.TestCase):
    def test_controller_programs_are_valid_python(self) -> None:
        compile(CLIENT.STAGE_PROGRAM, "<stage-program>", "exec")
        compile(CLIENT.VALIDATE_PROGRAM, "<validate-program>", "exec")

    def test_demo_input_is_deterministic_and_complete(self) -> None:
        document = CLIENT.json.loads(CLIENT.make_input(3))
        self.assertEqual(
            document,
            {
                "schema_version": 1,
                "chunks": [
                    {"chunk_id": "chunk-000001", "value": 1},
                    {"chunk_id": "chunk-000002", "value": 2},
                    {"chunk_id": "chunk-000003", "value": 3},
                ],
            },
        )

    def test_cli_rejects_parallelism_greater_than_chunk_count(self) -> None:
        with self.assertRaises(SystemExit):
            CLIENT.parse_args(["--chunks", "2", "--parallel", "3"])

    def test_secret_reader_requires_private_real_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            secrets_dir = project / ".runtime" / "secrets"
            secrets_dir.mkdir(parents=True, mode=0o700)
            (project / ".runtime").chmod(0o700)
            secrets_dir.chmod(0o700)
            token = secrets_dir / "broker-api.token"
            token.write_text("a" * 64 + "\n", encoding="ascii")
            token.chmod(0o600)
            self.assertEqual(CLIENT.read_local_bearer(project), "a" * 64)
            token.chmod(0o644)
            with self.assertRaises(CLIENT.ClientError):
                CLIENT.read_local_bearer(project)

    def test_secret_reader_rejects_symlink_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            secrets_dir = project / ".runtime" / "secrets"
            secrets_dir.mkdir(parents=True, mode=0o700)
            (project / ".runtime").chmod(0o700)
            secrets_dir.chmod(0o700)
            target = project / "target"
            target.write_text("b" * 64 + "\n", encoding="ascii")
            target.chmod(0o600)
            (secrets_dir / "broker-api.token").symlink_to(target)
            with self.assertRaises(CLIENT.ClientError):
                CLIENT.read_local_bearer(project)


if __name__ == "__main__":
    unittest.main()
