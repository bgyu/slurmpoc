from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid


EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "workflow"
sys.path.insert(0, str(EXAMPLES))
import master as demo_master  # noqa: E402


class FakeDispatchResponse:
    status = 200

    def __enter__(self) -> "FakeDispatchResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return b'{"status":"RUNNING"}'


class DemoJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.shared = self.root / "shared"
        self.shared.mkdir(mode=0o700)
        self.workflow_id = str(uuid.uuid4())
        self.workflow_root = self.shared / "workflows" / self.workflow_id
        self.input = self.shared / "input.json"
        self.input.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "chunks": [
                        {"chunk_id": "part-000", "value": 2},
                        {"chunk_id": "part-001", "value": -3},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.capability = self.root / "master.cap"
        self.capability.write_text("wfc_demo-capability\n", encoding="ascii")
        self.capability.chmod(0o400)
        self.dispatched_request = None
        self.base_environment = {
            **os.environ,
            "WORKFLOW_ID": self.workflow_id,
            "WORKFLOW_ALLOWED_FILE_ROOT": str(self.shared),
            "WORKFLOW_INPUT_SPEC_URI": self.input.as_uri(),
            "WORKFLOW_MANIFEST_URI": (self.workflow_root / "manifest.json").as_uri(),
            "WORKFLOW_OUTPUT_ROOT_URI": (self.workflow_root / "results").as_uri(),
            "WORKFLOW_SUMMARY_URI": (self.workflow_root / "summary.json").as_uri(),
            "WORKFLOW_CAPABILITY_FILE": str(self.capability),
            "WORKFLOW_BROKER_URL": "http://workflow-broker.invalid:8088",
            "WORKFLOW_CANCEL_LOCK": str(
                self.shared / ".workflow-control" / f"{self.workflow_id}.lock"
            ),
            "WORKFLOW_CANCEL_MARKER": str(
                self.shared / ".workflow-control" / f"{self.workflow_id}.cancelled"
            ),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_helper(
        self, name: str, *, extra_environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = {**self.base_environment, **(extra_environment or {})}
        return subprocess.run(
            [sys.executable, "-Es", str(EXAMPLES / name)],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

    def test_master_workers_and_afterany_finalizer_are_idempotent(self) -> None:
        def capture(request: object, timeout: int) -> FakeDispatchResponse:
            self.dispatched_request = request
            self.assertEqual(timeout, 15)
            return FakeDispatchResponse()

        with patch.dict(os.environ, self.base_environment, clear=True), patch.object(
            demo_master, "urlopen", side_effect=capture
        ):
            demo_master.main()
        manifest_path = self.workflow_root / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        assert self.dispatched_request is not None
        self.assertEqual(
            self.dispatched_request.full_url,
            "http://workflow-broker.invalid:8088"
            f"/v1/workflows/{self.workflow_id}/dispatch",
        )
        self.assertEqual(
            self.dispatched_request.get_header("X-workflow-capability"),
            "wfc_demo-capability",
        )
        dispatch_body = json.loads(self.dispatched_request.data)
        self.assertEqual(dispatch_body["manifest_sha256"], manifest_hash)
        self.assertFalse(self.capability.exists())

        worker_environment = {
            "WORKFLOW_MANIFEST_SHA256": manifest_hash,
        }
        # A durable cancellation marker prevents a new result even though the
        # task was already allocated.
        marker = Path(self.base_environment["WORKFLOW_CANCEL_MARKER"])
        marker.parent.mkdir(mode=0o750)
        marker.write_text(self.workflow_id + "\n", encoding="ascii")
        cancelled = self.run_helper(
            "worker.py",
            extra_environment={**worker_environment, "SLURM_ARRAY_TASK_ID": "0"},
        )
        self.assertEqual(cancelled.returncode, 75, cancelled.stderr)
        self.assertFalse((self.workflow_root / "results" / "part-000.json").exists())
        marker.unlink()

        for index in range(2):
            result = self.run_helper(
                "worker.py",
                extra_environment={
                    **worker_environment,
                    "SLURM_ARRAY_TASK_ID": str(index),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        # A requeued array element observes the identical durable result and
        # exits successfully without replacing it.
        replay = self.run_helper(
            "worker.py",
            extra_environment={**worker_environment, "SLURM_ARRAY_TASK_ID": "0"},
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertIn("already-committed", replay.stdout)

        finalizer = self.run_helper(
            "finalizer.py",
            extra_environment=worker_environment,
        )
        self.assertEqual(finalizer.returncode, 0, finalizer.stderr)
        summary = json.loads((self.workflow_root / "summary.json").read_text())
        self.assertEqual(summary["status"], "SUCCEEDED")
        self.assertEqual(summary["completed_chunks"], 2)
        results = [
            json.loads((self.workflow_root / "results" / name).read_text())
            for name in ("part-000.json", "part-001.json")
        ]
        self.assertEqual([item["result"] for item in results], [4, 9])


if __name__ == "__main__":
    unittest.main()
