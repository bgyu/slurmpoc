from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from workflow.slurm import (
    API_PREFIX,
    SlurmAmbiguous,
    SlurmClient,
    SlurmRejected,
    SlurmUnavailable,
    _read_token,
)


class FakeResponse:
    def __init__(self, status: int, payload: object | None = None) -> None:
        self.status = status
        self.body = (
            b""
            if payload is None
            else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )

    def read(self, _limit: int) -> bytes:
        return self.body


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> None:
        self.calls.append((method, path, body, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class SlurmClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token = self.root / "slurm.jwt"
        self.token.write_text("signed-jwt\n", encoding="ascii")
        self.token.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def client_with(self, response: FakeResponse) -> tuple[SlurmClient, FakeConnection]:
        client = SlurmClient(
            base_url="https://slurm.internal/gateway",
            timeout=3,
            token_file=self.token,
        )
        connection = FakeConnection(response)
        client._connection = lambda: connection  # type: ignore[method-assign]
        return client, connection

    def test_submit_uses_pinned_api_and_required_slurm_jwt_headers(self) -> None:
        client, connection = self.client_with(FakeResponse(200, {"job_id": 42}))
        job_id = client.submit_job(
            {"script": "#!/bin/bash\n", "job": {"name": "demo"}},
            identity="workflow",
            correlation_id="correlation-1",
        )
        self.assertEqual(job_id, "42")
        method, path, body, headers = connection.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, f"/gateway{API_PREFIX}/job/submit")
        self.assertEqual(headers["X-SLURM-USER-NAME"], "workflow")
        self.assertEqual(headers["X-SLURM-USER-TOKEN"], "signed-jwt")
        self.assertEqual(headers["X-Correlation-ID"], "correlation-1")
        self.assertEqual(json.loads(body or b"{}") ["job"]["name"], "demo")
        self.assertTrue(connection.closed)

    def test_token_is_reloaded_to_allow_atomic_rotation(self) -> None:
        client, first = self.client_with(FakeResponse(200, {"jobs": []}))
        client.find_jobs_by_marker(
            "marker", identity="workflow", correlation_id="correlation-1"
        )
        self.token.write_text("rotated-jwt\n", encoding="ascii")
        self.token.chmod(0o600)
        second = FakeConnection(FakeResponse(200, {"jobs": []}))
        client._connection = lambda: second  # type: ignore[method-assign]
        client.find_jobs_by_marker(
            "marker", identity="workflow", correlation_id="correlation-2"
        )
        self.assertEqual(
            first.calls[0][3]["X-SLURM-USER-TOKEN"], "signed-jwt"
        )
        self.assertEqual(
            second.calls[0][3]["X-SLURM-USER-TOKEN"], "rotated-jwt"
        )

    def test_find_by_exact_comment_unwraps_openapi_values(self) -> None:
        client, _connection = self.client_with(
            FakeResponse(
                200,
                {
                    "jobs": [
                        {
                            "job_id": {"number": 7},
                            "comment": {"string": "wanted"},
                        },
                        {"job_id": 8, "comment": "other"},
                    ]
                },
            )
        )
        self.assertEqual(
            client.find_jobs_by_marker(
                "wanted", identity="workflow", correlation_id="c-1"
            ),
            ["7"],
        )

    def test_mutation_failures_are_conservatively_ambiguous(self) -> None:
        client, _ = self.client_with(FakeResponse(503, {"errors": []}))
        with self.assertRaises(SlurmAmbiguous):
            client.submit_job(
                {"script": "#!/bin/bash\n", "job": {}},
                identity="workflow",
                correlation_id="c-1",
            )
        client, _ = self.client_with(FakeResponse(400, {"errors": []}))
        with self.assertRaises(SlurmRejected):
            client.submit_job(
                {"script": "#!/bin/bash\n", "job": {}},
                identity="workflow",
                correlation_id="c-1",
            )
        client, _ = self.client_with(
            FakeResponse(200, {"errors": [{"description": "unknown"}]})
        )
        with self.assertRaises(SlurmAmbiguous):
            client.submit_job(
                {"script": "#!/bin/bash\n", "job": {}},
                identity="workflow",
                correlation_id="c-1",
            )

    def test_read_only_transport_failure_is_unavailable(self) -> None:
        client, connection = self.client_with(FakeResponse(200, {"jobs": []}))

        def fail(*_args: object, **_kwargs: object) -> None:
            raise OSError("connection lost")

        connection.request = fail  # type: ignore[method-assign]
        with self.assertRaises(SlurmUnavailable):
            client.find_jobs_by_marker(
                "marker", identity="workflow", correlation_id="c-1"
            )

    def test_token_file_rejects_group_access_and_symlinks(self) -> None:
        self.token.chmod(0o640)
        with self.assertRaises(SlurmUnavailable):
            _read_token(self.token)
        self.token.chmod(0o600)
        link = self.root / "jwt.link"
        link.symlink_to(self.token)
        with self.assertRaises(SlurmUnavailable):
            _read_token(link)


if __name__ == "__main__":
    unittest.main()
