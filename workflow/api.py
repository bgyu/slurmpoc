"""Small, fail-closed HTTP boundary for the workflow state machine.

TLS and OIDC validation belong at the production gateway.  The broker still
authenticates that gateway by both direct peer address and a rotating
file-backed bearer secret; it never trusts caller-supplied forwarding headers.
The standard-library server is intentionally narrow and exposes no generic
proxy, file, shell, or Slurm endpoint.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import re
import signal
import threading
from typing import Any
from urllib.parse import urlsplit
import uuid

from .config import BrokerConfig
from .errors import (
    BadRequest,
    BrokerError,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    ServiceUnavailable,
    Unauthorized,
    UnsupportedMediaType,
)
from .security import secret_matches
from .service import IDENTIFIER, WorkflowBroker
from .slurm import SlurmClient
from .store import store_from_config


LOG = logging.getLogger("workflow.api")
WORKFLOW_PATH = re.compile(
    r"^/v1/workflows/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})(?:/(dispatch|cancel))?$"
)


class WorkflowHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server with a hard in-process concurrency ceiling.

    A gateway must also enforce limits, but retaining a local ceiling prevents
    an accidentally permissive load balancer from exhausting the broker.  The
    semaphore covers the complete request lifetime, not just handler creation.
    """

    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        broker: WorkflowBroker,
        config: BrokerConfig,
    ) -> None:
        self.broker = broker
        self.config = config
        self.trusted_proxy_networks = tuple(
            ipaddress.ip_network(value, strict=False)
            for value in config.trusted_proxy_cidrs
        )
        self.callback_trusted_networks = tuple(
            ipaddress.ip_network(value, strict=False)
            for value in config.callback_trusted_cidrs
        )
        self._slots = threading.BoundedSemaphore(config.http_max_connections)
        super().__init__(address, WorkflowRequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class WorkflowRequestHandler(BaseHTTPRequestHandler):
    """HTTP adapter; all durable and scheduling behavior stays in the broker."""

    protocol_version = "HTTP/1.1"
    server_version = "workflow-broker"
    sys_version = ""

    @property
    def app(self) -> WorkflowHTTPServer:
        return self.server  # type: ignore[return-value]

    def version_string(self) -> str:
        # Do not disclose the Python patch level to untrusted callers.
        return self.server_version

    def log_message(self, message: str, *args: Any) -> None:
        # BaseHTTPRequestHandler logs only method/path/status, but this explicit
        # override guarantees request headers (which include bearer material)
        # never enter application logs.
        LOG.info("peer=%s %s", self.client_address[0], message % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._run("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._run("POST")

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        self._run("PUT")

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self._run("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        self._run("PATCH")

    def _run(self, method: str) -> None:
        self.correlation_id = str(uuid.uuid4())
        try:
            supplied_correlation = self._one_header("X-Correlation-ID")
            if supplied_correlation is not None:
                if not IDENTIFIER.fullmatch(supplied_correlation):
                    raise BadRequest("X-Correlation-ID has an invalid format")
                self.correlation_id = supplied_correlation
            payload, status = self._route(method)
            self._send_json(status, payload)
        except BrokerError as error:
            self._send_json(
                error.status_code,
                {
                    "error": {
                        "code": error.code,
                        "message": error.public_message,
                    },
                    "correlation_id": self.correlation_id,
                },
            )
        except (BrokenPipeError, ConnectionResetError):
            return
        except BaseException:
            LOG.exception("unhandled request failure correlation_id=%s", self.correlation_id)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "internal broker error",
                    },
                    "correlation_id": self.correlation_id,
                },
            )

    def _route(self, method: str) -> tuple[dict[str, Any], int]:
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.query or target.fragment:
            raise BadRequest("request target must be a plain path without a query")
        path = target.path

        if path == "/livez":
            if method != "GET":
                raise MethodNotAllowed("health endpoint requires GET")
            return {"status": "live"}, HTTPStatus.OK
        if path in {"/readyz", "/healthz"}:
            if method != "GET":
                raise MethodNotAllowed("health endpoint requires GET")
            try:
                self.app.broker.store.ping()
            except BaseException as exc:
                raise ServiceUnavailable("workflow database is unavailable") from exc
            return {"status": "ready"}, HTTPStatus.OK

        if path == "/v1/workflows":
            if method != "POST":
                raise MethodNotAllowed("workflow collection requires POST")
            tenant, owner = self._authenticated_actor()
            idempotency_key = self._required_header("Idempotency-Key")
            body = self._read_json_body()
            result, created = self.app.broker.create_workflow(
                tenant=tenant,
                owner=owner,
                idempotency_key=idempotency_key,
                request=body,
                correlation_id=self.correlation_id,
            )
            return result, HTTPStatus.CREATED if created else HTTPStatus.OK

        match = WORKFLOW_PATH.fullmatch(path)
        if not match:
            raise NotFound("endpoint not found")
        workflow_id, action = match.groups()
        if action == "dispatch":
            if method != "POST":
                raise MethodNotAllowed("workflow dispatch requires POST")
            if self.app.config.profile == "production" and not self._peer_in(
                self.app.callback_trusted_networks
            ):
                raise Unauthorized("workflow callback peer is not trusted")
            capability = self._required_header("X-Workflow-Capability")
            return (
                self.app.broker.dispatch(
                    workflow_id,
                    capability=capability,
                    request=self._read_json_body(),
                ),
                HTTPStatus.OK,
            )

        tenant, owner = self._authenticated_actor()
        if action == "cancel":
            if method != "POST":
                raise MethodNotAllowed("workflow cancellation requires POST")
            self._require_empty_body()
            return (
                self.app.broker.cancel(workflow_id, tenant=tenant, owner=owner),
                HTTPStatus.OK,
            )
        if method != "GET":
            raise MethodNotAllowed("workflow status requires GET")
        return (
            self.app.broker.get_status(workflow_id, tenant=tenant, owner=owner),
            HTTPStatus.OK,
        )

    def _authenticated_actor(self) -> tuple[str, str]:
        config = self.app.config
        if config.auth_mode == "local-token":
            authorization = self._one_header("Authorization")
            if authorization is None:
                raise Unauthorized("bearer authentication is required")
            scheme, separator, token = authorization.partition(" ")
            if separator != " " or scheme.lower() != "bearer" or not token:
                raise Unauthorized("invalid bearer authentication")
            if not secret_matches(token, config.api_token_file, purpose="local API"):
                raise Unauthorized("invalid bearer authentication")
        elif config.auth_mode == "trusted-proxy":
            if not self._peer_in(self.app.trusted_proxy_networks):
                raise Unauthorized("request did not arrive from a trusted proxy")
            proxy_token = self._one_header("X-Workflow-Proxy-Token")
            if proxy_token is None:
                raise Unauthorized("trusted proxy authentication is required")
            if not secret_matches(
                proxy_token,
                config.trusted_proxy_token_file,
                purpose="trusted proxy",
            ):
                raise Unauthorized("trusted proxy authentication failed")
        else:  # Config validation should make this unreachable.
            raise ServiceUnavailable("broker authentication mode is invalid")

        # These values are trusted only after authenticating the direct peer.
        # The production gateway must remove incoming copies before injecting
        # values derived from verified OIDC claims.
        tenant = self._required_header("X-Authenticated-Tenant")
        owner = self._required_header("X-Authenticated-User")
        return tenant, owner

    def _peer_in(self, networks: tuple[Any, ...]) -> bool:
        try:
            peer = ipaddress.ip_address(self.client_address[0].split("%", 1)[0])
        except ValueError as exc:
            raise Unauthorized("peer address is invalid") from exc
        mapped = getattr(peer, "ipv4_mapped", None)
        if mapped is not None:
            peer = mapped
        return any(peer in network for network in networks)

    def _read_json_body(self) -> dict[str, Any]:
        if self._one_header("Transfer-Encoding") is not None:
            raise BadRequest("chunked request bodies are not accepted")
        content_type = self._required_header("Content-Type")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise UnsupportedMediaType("Content-Type must be application/json")
        length_text = self._required_header("Content-Length")
        try:
            length = int(length_text, 10)
        except ValueError as exc:
            raise BadRequest("Content-Length is invalid") from exc
        if length <= 0:
            raise BadRequest("JSON request body is required")
        if length > self.app.config.max_request_bytes:
            raise PayloadTooLarge("request body exceeds the configured limit")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise BadRequest("request body ended early")
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest("request body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise BadRequest("request body must be a JSON object")
        return body

    def _require_empty_body(self) -> None:
        if self._one_header("Transfer-Encoding") is not None:
            raise BadRequest("request body is not accepted")
        value = self._one_header("Content-Length")
        if value is None:
            return
        try:
            length = int(value, 10)
        except ValueError as exc:
            raise BadRequest("Content-Length is invalid") from exc
        if length != 0:
            raise BadRequest("request body is not accepted")

    def _one_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        if len(values) > 1:
            raise BadRequest(f"duplicate {name} header")
        if not values:
            return None
        value = values[0].strip()
        if not value or "\r" in value or "\n" in value or "\x00" in value:
            raise BadRequest(f"{name} header is invalid")
        return value

    def _required_header(self, name: str) -> str:
        value = self._one_header(name)
        if value is None:
            raise BadRequest(f"{name} header is required")
        return value

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Correlation-ID", self.correlation_id)
        # One request per connection eliminates ambiguity from an unread body
        # on a bodyless endpoint and keeps the private service easy to proxy.
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True


class Reconciler(threading.Thread):
    """Continuously resolves intents left uncertain by process/network loss."""

    def __init__(self, broker: WorkflowBroker, interval: int) -> None:
        super().__init__(name="workflow-reconciler", daemon=True)
        self.broker = broker
        self.interval = interval
        self.stopping = threading.Event()

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.broker.reconcile_all()
            except BaseException:
                # A reconciliation defect must be visible to operations, while
                # preserving API availability for status and cancellation.
                LOG.exception("workflow reconciliation pass failed")
            self.stopping.wait(self.interval)

    def stop(self) -> None:
        self.stopping.set()


def run() -> None:
    """Build dependencies from validated environment and serve until signalled."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = BrokerConfig.from_env()
    store = store_from_config(config)
    slurm = SlurmClient.from_config(config)
    broker = WorkflowBroker(config, store, slurm)
    server = WorkflowHTTPServer((config.listen_host, config.listen_port), broker, config)
    server.timeout = 1.0
    reconciler = Reconciler(broker, config.reconcile_interval_seconds)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    reconciler.start()
    LOG.info("workflow broker listening on %s:%d", config.listen_host, config.listen_port)
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        reconciler.stop()
        reconciler.join(timeout=min(5, config.reconcile_interval_seconds + 1))
        server.server_close()
