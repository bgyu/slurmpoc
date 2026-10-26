from __future__ import annotations

import json
from pathlib import Path
import stat
import unittest
import uuid

from workflow.errors import BadRequest, Conflict
from workflow.security import capability_digest
from workflow.service import WorkflowBroker, _canonical_bytes
from workflow.slurm import SlurmAmbiguous
from workflow.store import SQLiteStore
from workflow.tests.helpers import FakeSlurm, Workspace


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()
        self.config = self.workspace.config(slurm_submit_user="workflow")
        self.store = SQLiteStore(self.config.database_path, clock=lambda: 1_700_000_000)
        self.slurm = FakeSlurm()
        self.broker = WorkflowBroker(
            self.config,
            self.store,
            self.slurm,  # type: ignore[arg-type]
            clock=lambda: 1_700_000_000,
            token_factory=lambda: "wfc_test-capability",
        )
        self.request = {
            "input_spec_uri": (self.workspace.shared / "input.json").as_uri(),
            "workspace_uri": (self.workspace.shared / "workflows").as_uri(),
            "max_parallel": 3,
            "slurm": {
                "partition": "debug",
                "time_limit": 5,
                "memory_per_node": 128,
                "cpus_per_task": 1,
            },
        }

    def tearDown(self) -> None:
        self.workspace.close()

    def create(self, key: str = "request-1") -> tuple[dict, bool]:
        return self.broker.create_workflow(
            tenant="tenant-a",
            owner="alice",
            idempotency_key=key,
            request=self.request,
            correlation_id="correlation-1",
        )

    @staticmethod
    def environment(request: dict) -> dict[str, str]:
        return dict(item.split("=", 1) for item in request["job"]["environment"])

    def test_create_submits_master_without_putting_capability_in_environment(self) -> None:
        result, created = self.create()
        self.assertTrue(created)
        self.assertEqual(result["status"], "MASTER_SUBMITTED")
        self.assertEqual(result["jobs"]["master"]["job_id"], "100")
        request, identity, correlation = self.slurm.requests[0]
        self.assertEqual(identity, "workflow")
        self.assertEqual(correlation, "correlation-1")
        environment = self.environment(request)
        self.assertNotIn("wfc_test-capability", json.dumps(request))
        self.assertEqual(environment["WORKFLOW_ID"], result["workflow_id"])
        capability_path = Path(environment["WORKFLOW_CAPABILITY_FILE"])
        self.assertEqual(capability_path.read_text().strip(), "wfc_test-capability")
        self.assertEqual(capability_path.stat().st_mode & 0o777, 0o400)

    def test_all_jobs_use_private_per_workflow_shared_log_paths(self) -> None:
        created, _ = self.create()
        workflow_id = created["workflow_id"]
        log_directory = self.config.control_dir / "job-logs" / workflow_id

        self.assertTrue(log_directory.is_dir())
        self.assertEqual(stat.S_IMODE(log_directory.stat().st_mode), 0o700)
        master = self.slurm.requests[0][0]["job"]
        self.assertEqual(
            master["standard_output"],
            str(log_directory / "master-g0-%j.out"),
        )
        self.assertEqual(
            master["standard_error"],
            str(log_directory / "master-g0-%j.err"),
        )

        manifest = (
            self.workspace.shared / "workflows" / workflow_id / "manifest.json"
        ).as_uri()
        self.broker.dispatch(
            workflow_id,
            capability="wfc_test-capability",
            request={
                "manifest_uri": manifest,
                "manifest_sha256": "a" * 64,
                "chunk_count": 2,
            },
        )
        array = self.slurm.requests[1][0]["job"]
        finalizer = self.slurm.requests[2][0]["job"]
        self.assertEqual(
            array["standard_output"],
            str(log_directory / "worker-g0-%A_%a.out"),
        )
        self.assertEqual(
            array["standard_error"],
            str(log_directory / "worker-g0-%A_%a.err"),
        )
        self.assertEqual(
            finalizer["standard_output"],
            str(log_directory / "finalizer-g0-%j.out"),
        )
        self.assertEqual(
            finalizer["standard_error"],
            str(log_directory / "finalizer-g0-%j.err"),
        )

    def test_create_refuses_permissive_shared_log_root(self) -> None:
        log_root = self.config.control_dir / "job-logs"
        log_root.mkdir(mode=0o700)
        log_root.chmod(0o750)

        with self.assertRaisesRegex(ValueError, "mode 0700"):
            self.create()
        self.assertEqual(self.slurm.requests, [])
        self.assertEqual(list(self.config.capability_dir.glob("*.cap")), [])

    def test_create_idempotency_submits_only_one_master(self) -> None:
        first, _ = self.create()
        second, created = self.create()
        self.assertFalse(created)
        self.assertEqual(first["workflow_id"], second["workflow_id"])
        self.assertEqual(len(self.slurm.requests), 1)
        with self.assertRaises(Conflict):
            changed = {**self.request, "max_parallel": 2}
            self.broker.create_workflow(
                tenant="tenant-a",
                owner="alice",
                idempotency_key="request-1",
                request=changed,
                correlation_id="correlation-2",
            )

    def test_dispatch_submits_throttled_array_and_afterany_finalizer_once(self) -> None:
        created, _ = self.create()
        workflow_id = created["workflow_id"]
        manifest = (
            self.workspace.shared / "workflows" / workflow_id / "manifest.json"
        ).as_uri()
        dispatch = {
            "manifest_uri": manifest,
            "manifest_sha256": "a" * 64,
            "chunk_count": 7,
        }
        result = self.broker.dispatch(
            workflow_id, capability="wfc_test-capability", request=dispatch
        )
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(len(self.slurm.requests), 3)
        array_request = self.slurm.requests[1][0]
        finalizer_request = self.slurm.requests[2][0]
        self.assertEqual(array_request["job"]["array"], "0-6%3")
        self.assertEqual(finalizer_request["job"]["dependency"], "afterany:101")
        self.assertNotIn("WORKFLOW_CAPABILITY_FILE", self.environment(array_request))
        self.assertFalse((self.config.capability_dir / f"{workflow_id}.cap").exists())

        replay = self.broker.dispatch(
            workflow_id, capability="wfc_test-capability", request=dispatch
        )
        self.assertEqual(replay["jobs"]["array"]["job_id"], "101")
        self.assertEqual(len(self.slurm.requests), 3)
        with self.assertRaises(Conflict):
            self.broker.dispatch(
                workflow_id,
                capability="wfc_test-capability",
                request={**dispatch, "chunk_count": 8},
            )

    def test_ambiguous_submit_is_reconciled_by_unique_marker(self) -> None:
        self.slurm.submit_error = SlurmAmbiguous("response lost")
        result, _ = self.create()
        self.assertEqual(result["status"], "MASTER_SUBMISSION_UNKNOWN")
        request = self.slurm.requests[0][0]
        marker = request["job"]["comment"]
        self.slurm.markers[marker] = ["987"]
        self.broker.reconcile(result["workflow_id"])
        reconciled = self.store.get_workflow(result["workflow_id"])
        self.assertEqual(reconciled["workflow"]["status"], "MASTER_SUBMITTED")
        self.assertEqual(reconciled["intents"][0]["slurm_job_id"], "987")

    def test_prepared_intent_is_resumed_because_no_rest_call_was_made(self) -> None:
        workflow_id = str(uuid.uuid4())
        capability = "wfc_prepared"
        specification = self.broker._validate_create_request(self.request)
        self.broker.capabilities.stage(workflow_id, capability)
        snapshot, _ = self.store.create_or_get_workflow(
            workflow_id=workflow_id,
            tenant="tenant-a",
            owner="alice",
            idempotency_key="prepared-1",
            request_digest="a" * 64,
            request=specification,
            correlation_id="correlation-prepared",
            capability_hash=capability_digest(capability),
            capability_path=str(self.broker.capabilities.job_path(workflow_id)),
            capability_expires_at=1_700_000_100,
        )
        request = self.broker._job_request(
            snapshot["workflow"],
            generation=0,
            kind="master",
            script="#!/bin/bash\n",
            environment={"PATH": "/usr/bin"},
            slurm_options={},
        )
        marker = request["job"]["comment"]
        intent_id = str(uuid.uuid5(uuid.UUID(workflow_id), f"0:master:{marker}"))
        self.store.prepare_intent(
            intent_id=intent_id,
            workflow_id=workflow_id,
            generation=0,
            kind="master",
            marker=marker,
            job_name=request["job"]["name"],
            request=request,
        )
        self.broker.reconcile(workflow_id)
        self.assertEqual(len(self.slurm.requests), 1)
        self.assertEqual(
            self.store.get_workflow(workflow_id)["workflow"]["status"],
            "MASTER_SUBMITTED",
        )

    def test_cancel_marks_before_scancel_and_cancels_every_known_job(self) -> None:
        created, _ = self.create()
        workflow_id = created["workflow_id"]
        result = self.broker.cancel(
            workflow_id, tenant="tenant-a", owner="alice"
        )
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(self.slurm.cancelled, ["100"])
        marker = self.config.control_dir / f"{workflow_id}.cancelled"
        self.assertEqual(marker.read_text().strip(), workflow_id)

    def test_summary_drives_terminal_status(self) -> None:
        created, _ = self.create()
        workflow_id = created["workflow_id"]
        manifest = (
            self.workspace.shared / "workflows" / workflow_id / "manifest.json"
        ).as_uri()
        result = self.broker.dispatch(
            workflow_id,
            capability="wfc_test-capability",
            request={
                "manifest_uri": manifest,
                "manifest_sha256": "a" * 64,
                "chunk_count": 1,
            },
        )
        summary_path = Path(result["generations"][0]["summary_uri"].removeprefix("file://"))
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps({"workflow_id": workflow_id, "status": "SUCCEEDED"}),
            encoding="utf-8",
        )
        status = self.broker.get_status(
            workflow_id, tenant="tenant-a", owner="alice"
        )
        self.assertEqual(status["status"], "SUCCEEDED")

    def test_uri_and_policy_validation_rejects_escape_and_unknown_fields(self) -> None:
        with self.assertRaises(BadRequest):
            self.broker.create_workflow(
                tenant="tenant-a",
                owner="alice",
                idempotency_key="escape-1",
                request={
                    **self.request,
                    "input_spec_uri": Path("/etc/passwd").as_uri(),
                },
                correlation_id="correlation-escape",
            )
        with self.assertRaises(BadRequest):
            self.broker.create_workflow(
                tenant="tenant-a",
                owner="alice",
                idempotency_key="unknown-1",
                request={**self.request, "shell": "rm -rf /"},
                correlation_id="correlation-unknown",
            )


if __name__ == "__main__":
    unittest.main()
