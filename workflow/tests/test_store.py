from __future__ import annotations

import stat
import unittest

from workflow.errors import CapabilityExpired, Conflict, NotFound, Unauthorized
from workflow.security import capability_digest
from workflow.store import SQLiteStore, store_from_config
from workflow.tests.helpers import Workspace


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()
        self.now = 1_700_000_000
        self.store = SQLiteStore(
            self.workspace.root / "state" / "workflow.sqlite3",
            clock=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.workspace.close()

    def create(self, *, digest: str = "request-a") -> tuple[dict, bool]:
        return self.store.create_or_get_workflow(
            workflow_id="11111111-1111-4111-8111-111111111111",
            tenant="tenant-a",
            owner="alice",
            idempotency_key="client-request-1",
            request_digest=digest,
            request={"input_spec_uri": "file:///shared/input.json"},
            correlation_id="correlation-1",
            capability_hash=capability_digest("wfc_secret"),
            capability_path="/run/workflow/capability",
            capability_expires_at=self.now + 60,
        )

    def test_database_and_parent_are_private(self) -> None:
        mode = stat.S_IMODE((self.workspace.root / "state" / "workflow.sqlite3").stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse(stat.S_IMODE((self.workspace.root / "state").stat().st_mode) & 0o077)

    def test_idempotency_returns_winner_and_rejects_changed_request(self) -> None:
        first, created = self.create()
        second, created_again = self.create()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(
            first["workflow"]["workflow_id"], second["workflow"]["workflow_id"]
        )
        with self.assertRaises(Conflict):
            self.create(digest="request-b")

    def test_actor_lookup_hides_other_tenants(self) -> None:
        self.create()
        with self.assertRaises(NotFound):
            self.store.get_workflow_for_actor(
                "11111111-1111-4111-8111-111111111111", "tenant-b", "alice"
            )

    def test_capability_is_one_mutation_but_identical_response_replay_is_safe(self) -> None:
        self.create()
        arguments = {
            "workflow_id": "11111111-1111-4111-8111-111111111111",
            "token": "wfc_secret",
            "manifest_uri": "file:///shared/manifest.json",
            "manifest_sha256": "a" * 64,
            "chunk_count": 4,
            "max_parallel": 2,
            "summary_uri": "file:///shared/summary.json",
        }
        first = self.store.consume_capability(**arguments)
        replay = self.store.consume_capability(**arguments)
        self.assertIsNotNone(first["workflow"]["capability_used_at"])
        self.assertEqual(len(replay["generations"]), 1)
        with self.assertRaises(Conflict):
            self.store.consume_capability(**{**arguments, "chunk_count": 5})
        with self.assertRaises(Unauthorized):
            self.store.consume_capability(**{**arguments, "token": "wfc_wrong"})

    def test_expired_capability_never_mutates(self) -> None:
        self.create()
        self.now += 61
        with self.assertRaises(CapabilityExpired):
            self.store.consume_capability(
                workflow_id="11111111-1111-4111-8111-111111111111",
                token="wfc_secret",
                manifest_uri="file:///shared/manifest.json",
                manifest_sha256="a" * 64,
                chunk_count=1,
                max_parallel=1,
                summary_uri="file:///shared/summary.json",
            )
        self.assertEqual(self.store.get_workflow(
            "11111111-1111-4111-8111-111111111111"
        )["workflow"]["status"], "SUBMITTING_MASTER")

    def test_intent_claim_is_atomic(self) -> None:
        self.create()
        kwargs = {
            "intent_id": "22222222-2222-4222-8222-222222222222",
            "workflow_id": "11111111-1111-4111-8111-111111111111",
            "generation": 0,
            "kind": "master",
            "marker": "workflow=one;generation=0;role=master",
            "job_name": "wf-one-m-g0",
            "request": {"script": "#!/bin/bash\n", "job": {"name": "wf-one"}},
        }
        first = self.store.prepare_intent(**kwargs)
        second = self.store.prepare_intent(**kwargs)
        self.assertEqual(first["intent_id"], second["intent_id"])
        self.assertTrue(self.store.start_intent_attempt(first["intent_id"])["_claimed"])
        self.assertFalse(self.store.start_intent_attempt(first["intent_id"])["_claimed"])

    def test_production_profile_cannot_fall_back_to_sqlite(self) -> None:
        config = self.workspace.config(profile="production", database_url=None)
        with self.assertRaisesRegex(ValueError, "refuses the SQLite"):
            store_from_config(config)


if __name__ == "__main__":
    unittest.main()
