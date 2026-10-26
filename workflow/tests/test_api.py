from __future__ import annotations

from email.message import Message
from io import BytesIO
import ipaddress
import json
from types import SimpleNamespace
import unittest

from workflow.api import WorkflowRequestHandler
from workflow.errors import BadRequest, MethodNotAllowed, ServiceUnavailable, Unauthorized
from workflow.tests.helpers import Workspace


class StubStore:
    def __init__(self) -> None:
        self.available = True

    def ping(self) -> None:
        if not self.available:
            raise OSError("database unavailable")


class StubBroker:
    def __init__(self) -> None:
        self.store = StubStore()
        self.calls: list[tuple] = []

    def create_workflow(self, **kwargs: object) -> tuple[dict, bool]:
        self.calls.append(("create", kwargs))
        return {"workflow_id": "11111111-1111-4111-8111-111111111111"}, True

    def get_status(self, workflow_id: str, **kwargs: object) -> dict:
        self.calls.append(("get", workflow_id, kwargs))
        return {"workflow_id": workflow_id, "status": "RUNNING"}

    def dispatch(self, workflow_id: str, **kwargs: object) -> dict:
        self.calls.append(("dispatch", workflow_id, kwargs))
        return {"workflow_id": workflow_id, "status": "RUNNING"}

    def cancel(self, workflow_id: str, **kwargs: object) -> dict:
        self.calls.append(("cancel", workflow_id, kwargs))
        return {"workflow_id": workflow_id, "status": "CANCELLED"}


class APITests(unittest.TestCase):
    """Exercise the HTTP adapter without opening a sandbox-visible socket."""

    def setUp(self) -> None:
        self.workspace = Workspace()
        self.api_token = self.workspace.secret("api.token", "api-secret")
        self.broker = StubBroker()
        self.config = self.workspace.config(api_token_file=self.api_token)

    def tearDown(self) -> None:
        self.workspace.close()

    def route(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        peer: str = "127.0.0.1",
    ) -> tuple[dict, int]:
        encoded = b"" if body is None else json.dumps(body).encode("utf-8")
        message = Message()
        for key, value in (headers or {}).items():
            message.add_header(key, value)
        if body is not None:
            if message.get("Content-Type") is None:
                message.add_header("Content-Type", "application/json")
            message.add_header("Content-Length", str(len(encoded)))
        handler = object.__new__(WorkflowRequestHandler)
        handler.path = path
        handler.headers = message
        handler.rfile = BytesIO(encoded)
        handler.client_address = (peer, 12345)
        handler.correlation_id = "client-correlation-1"
        handler.server = SimpleNamespace(
            config=self.config,
            broker=self.broker,
            trusted_proxy_networks=tuple(
                ipaddress.ip_network(value, strict=False)
                for value in self.config.trusted_proxy_cidrs
            ),
            callback_trusted_networks=tuple(
                ipaddress.ip_network(value, strict=False)
                for value in self.config.callback_trusted_cidrs
            ),
        )
        return handler._route(method)

    @property
    def actor_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer api-secret",
            "X-Authenticated-Tenant": "tenant-a",
            "X-Authenticated-User": "alice",
            "X-Correlation-ID": "client-correlation-1",
        }

    def test_health_endpoints_are_uncredentialed_and_readiness_checks_database(self) -> None:
        payload, status = self.route("GET", "/livez")
        self.assertEqual((status, payload["status"]), (200, "live"))
        payload, status = self.route("GET", "/readyz")
        self.assertEqual((status, payload["status"]), (200, "ready"))
        self.broker.store.available = False
        with self.assertRaises(ServiceUnavailable):
            self.route("GET", "/readyz")

    def test_create_requires_file_backed_bearer_identity_and_idempotency(self) -> None:
        body = {
            "input_spec_uri": "file:///shared/input.json",
            "workspace_uri": "file:///shared/workflows",
        }
        payload, status = self.route(
            "POST",
            "/v1/workflows",
            body=body,
            headers={**self.actor_headers, "Idempotency-Key": "request-1"},
        )
        self.assertEqual(status, 201)
        self.assertIn("workflow_id", payload)
        call = self.broker.calls[-1][1]
        self.assertEqual(call["tenant"], "tenant-a")
        self.assertEqual(call["owner"], "alice")
        self.assertEqual(call["idempotency_key"], "request-1")
        self.assertEqual(call["correlation_id"], "client-correlation-1")
        with self.assertRaises(Unauthorized):
            self.route(
                "POST",
                "/v1/workflows",
                body=body,
                headers={"Idempotency-Key": "request-2"},
            )

    def test_capability_dispatch_does_not_require_slurm_jwt(self) -> None:
        workflow_id = "11111111-1111-4111-8111-111111111111"
        _payload, status = self.route(
            "POST",
            f"/v1/workflows/{workflow_id}/dispatch",
            body={
                "manifest_uri": "file:///shared/manifest.json",
                "manifest_sha256": "a" * 64,
                "chunk_count": 2,
            },
            headers={"X-Workflow-Capability": "wfc_callback"},
        )
        self.assertEqual(status, 200)
        call = self.broker.calls[-1]
        self.assertEqual(call[0], "dispatch")
        self.assertEqual(call[2]["capability"], "wfc_callback")

    def test_status_and_cancel_require_authenticated_actor(self) -> None:
        workflow_id = "11111111-1111-4111-8111-111111111111"
        _, status = self.route(
            "GET", f"/v1/workflows/{workflow_id}", headers=self.actor_headers
        )
        self.assertEqual(status, 200)
        payload, status = self.route(
            "POST",
            f"/v1/workflows/{workflow_id}/cancel",
            headers=self.actor_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "CANCELLED")

    def test_trusted_proxy_requires_peer_cidr_and_file_secret(self) -> None:
        proxy_secret = self.workspace.secret("proxy.token", "proxy-secret")
        self.config = self.workspace.config(
            profile="production",
            auth_mode="trusted-proxy",
            api_token_file=None,
            trusted_proxy_token_file=proxy_secret,
            trusted_proxy_cidrs=("127.0.0.0/8",),
            callback_trusted_cidrs=("127.0.0.0/8",),
        )
        headers = {
            "X-Workflow-Proxy-Token": "proxy-secret",
            "X-Authenticated-Tenant": "tenant-a",
            "X-Authenticated-User": "alice",
        }
        workflow_id = "11111111-1111-4111-8111-111111111111"
        _, status = self.route(
            "GET", f"/v1/workflows/{workflow_id}", headers=headers
        )
        self.assertEqual(status, 200)
        with self.assertRaises(Unauthorized):
            self.route(
                "GET",
                f"/v1/workflows/{workflow_id}",
                headers={**headers, "X-Workflow-Proxy-Token": "wrong"},
            )
        with self.assertRaises(Unauthorized):
            self.route(
                "GET",
                f"/v1/workflows/{workflow_id}",
                headers=headers,
                peer="192.0.2.10",
            )
        dispatch_body = {
            "manifest_uri": "file:///shared/manifest.json",
            "manifest_sha256": "a" * 64,
            "chunk_count": 1,
        }
        with self.assertRaises(Unauthorized):
            self.route(
                "POST",
                f"/v1/workflows/{workflow_id}/dispatch",
                body=dispatch_body,
                headers={"X-Workflow-Capability": "wfc_callback"},
                peer="192.0.2.10",
            )
        _, status = self.route(
            "POST",
            f"/v1/workflows/{workflow_id}/dispatch",
            body=dispatch_body,
            headers={"X-Workflow-Capability": "wfc_callback"},
        )
        self.assertEqual(status, 200)

    def test_unknown_query_and_wrong_method_fail_closed(self) -> None:
        with self.assertRaises(BadRequest):
            self.route("GET", "/livez?debug=true")
        with self.assertRaises(MethodNotAllowed):
            self.route("POST", "/livez")


if __name__ == "__main__":
    unittest.main()
