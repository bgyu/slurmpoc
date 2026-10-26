"""Minimal client for the pinned Slurm REST API v0.0.45.

Only the calls needed by the workflow state machine are implemented.  Keeping
this boundary small is intentional: it is easier to audit retry behavior when
every mutating Slurm request passes through one place.
"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import stat
from typing import Any
from urllib.parse import quote, urlsplit


API_VERSION = "v0.0.45"
API_PREFIX = f"/slurm/{API_VERSION}"


class SlurmError(Exception):
    """Base class; messages never contain credentials or response bodies."""


class SlurmRejected(SlurmError):
    """A definitive 4xx response, safe to record as a rejected intent."""


class SlurmAmbiguous(SlurmError):
    """The server may have accepted a mutation before the response was lost."""


class SlurmUnavailable(SlurmError):
    """A read-only call could not reach Slurm."""


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over a private Unix socket for slurmrestd rest_auth/local."""

    def __init__(self, socket_path: Path, timeout: int) -> None:
        super().__init__(host="localhost", timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.socket_path)
        self.sock = connection


def _read_token(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SlurmUnavailable("Slurm token file is not a regular file")
    if info.st_mode & 0o077:
        raise SlurmUnavailable("Slurm token file permissions are too broad")
    if info.st_size > 16384:
        raise SlurmUnavailable("Slurm token file is unexpectedly large")
    token = path.read_text(encoding="ascii").strip()
    if not token or "\n" in token or "\r" in token:
        raise SlurmUnavailable("Slurm token file is invalid")
    return token


def _unwrap(value: Any) -> Any:
    """Handle both plain values and Slurm's optional-value wrapper objects."""

    if not isinstance(value, dict):
        return value
    for key in ("number", "string", "value"):
        if key in value and value[key] is not None:
            return value[key]
    return value


def _job_id_from_response(payload: dict[str, Any]) -> str:
    direct = payload.get("job_id")
    if direct is not None:
        return str(_unwrap(direct))
    jobs = payload.get("jobs")
    if isinstance(jobs, list) and jobs:
        job_id = jobs[0].get("job_id") if isinstance(jobs[0], dict) else None
        if job_id is not None:
            return str(_unwrap(job_id))
    response = payload.get("job_submit_response")
    if isinstance(response, dict) and response.get("job_id") is not None:
        return str(_unwrap(response["job_id"]))
    raise SlurmAmbiguous("Slurm accepted submission but returned no job identifier")


class SlurmClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        socket_path: Path | None = None,
        timeout: int = 15,
        token_file: Path | None = None,
        token_header: str = "X-SLURM-USER-TOKEN",
        token_prefix: str = "",
        user_header: str | None = "X-SLURM-USER-NAME",
        ca_file: Path | None = None,
        client_cert: Path | None = None,
        client_key: Path | None = None,
    ) -> None:
        if bool(base_url) == bool(socket_path):
            raise ValueError("configure exactly one Slurm REST transport")
        self.base_url = base_url
        self.socket_path = socket_path
        self.timeout = timeout
        self.token_file = token_file
        self.token_header = token_header
        self.token_prefix = token_prefix
        self.user_header = user_header
        self.base_path = ""
        self.parsed_url = None
        self.ssl_context = None
        if base_url:
            self.parsed_url = urlsplit(base_url)
            self.base_path = self.parsed_url.path.rstrip("/")
            if self.parsed_url.scheme == "https":
                context = ssl.create_default_context(
                    cafile=str(ca_file) if ca_file else None
                )
                if client_cert:
                    context.load_cert_chain(
                        str(client_cert), str(client_key) if client_key else None
                    )
                self.ssl_context = context

    @classmethod
    def from_config(cls, config: Any) -> "SlurmClient":
        return cls(
            base_url=config.slurm_rest_url,
            socket_path=config.slurm_rest_socket,
            timeout=config.slurm_timeout_seconds,
            token_file=config.slurm_token_file,
            token_header=config.slurm_token_header,
            token_prefix=config.slurm_token_prefix,
            user_header=config.slurm_user_header,
            ca_file=config.slurm_ca_file,
            client_cert=config.slurm_client_cert,
            client_key=config.slurm_client_key,
        )

    def submit_job(
        self, request: dict[str, Any], *, identity: str, correlation_id: str
    ) -> str:
        try:
            payload = self._request(
                "POST",
                f"{API_PREFIX}/job/submit",
                request,
                identity=identity,
                correlation_id=correlation_id,
                mutation=True,
            )
            return _job_id_from_response(payload)
        except SlurmRejected:
            raise
        except SlurmAmbiguous:
            raise
        except SlurmError as exc:
            raise SlurmAmbiguous(str(exc)) from exc

    def cancel_job(
        self, job_id: str, *, identity: str, correlation_id: str
    ) -> None:
        safe_job_id = quote(str(job_id), safe="_")
        try:
            self._request(
                "DELETE",
                f"{API_PREFIX}/job/{safe_job_id}",
                None,
                identity=identity,
                correlation_id=correlation_id,
                mutation=True,
                not_found_ok=True,
            )
        except SlurmRejected:
            raise
        except SlurmAmbiguous:
            raise
        except SlurmError as exc:
            raise SlurmAmbiguous(str(exc)) from exc

    def find_jobs_by_marker(
        self, marker: str, *, identity: str, correlation_id: str
    ) -> list[str]:
        payload = self._request(
            "GET",
            f"{API_PREFIX}/jobs?update_time=0",
            None,
            identity=identity,
            correlation_id=correlation_id,
            mutation=False,
        )
        matches: list[str] = []
        jobs = payload.get("jobs", [])
        if not isinstance(jobs, list):
            raise SlurmUnavailable("Slurm jobs response has an invalid shape")
        for job in jobs:
            if not isinstance(job, dict):
                continue
            comment = _unwrap(job.get("comment"))
            if comment != marker:
                continue
            job_id = _unwrap(job.get("job_id"))
            if job_id is not None:
                matches.append(str(job_id))
        return sorted(set(matches))

    def _connection(self) -> http.client.HTTPConnection:
        if self.socket_path:
            return UnixHTTPConnection(self.socket_path, self.timeout)
        assert self.parsed_url is not None
        host = self.parsed_url.hostname or ""
        if self.parsed_url.scheme == "https":
            return http.client.HTTPSConnection(
                host,
                self.parsed_url.port or 443,
                timeout=self.timeout,
                context=self.ssl_context,
            )
        return http.client.HTTPConnection(
            host, self.parsed_url.port or 80, timeout=self.timeout
        )

    def _headers(self, identity: str, correlation_id: str) -> dict[str, str]:
        if any(character in identity for character in "\r\n"):
            raise SlurmUnavailable("invalid Slurm identity")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Correlation-ID": correlation_id,
        }
        # rest_auth/local authenticates the Unix peer.  Sending an unrelated
        # bearer value on that path would only increase secret exposure.
        if not self.socket_path and self.token_file:
            token = _read_token(self.token_file)  # deliberately reload each call
            value = f"{self.token_prefix} {token}" if self.token_prefix else token
            headers[self.token_header] = value
        if not self.socket_path and self.user_header:
            headers[self.user_header] = identity
        return headers

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        identity: str,
        correlation_id: str,
        mutation: bool,
        not_found_ok: bool = False,
    ) -> dict[str, Any]:
        connection = self._connection()
        encoded = None
        if body is not None:
            encoded = json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        try:
            connection.request(
                method,
                self.base_path + path,
                body=encoded,
                headers=self._headers(identity, correlation_id),
            )
            response = connection.getresponse()
            response_body = response.read(8 * 1024 * 1024 + 1)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            error = SlurmAmbiguous if mutation else SlurmUnavailable
            raise error("Slurm REST transport failed") from exc
        finally:
            connection.close()

        if len(response_body) > 8 * 1024 * 1024:
            error = SlurmAmbiguous if mutation else SlurmUnavailable
            raise error("Slurm REST response exceeded the safety limit")
        if not_found_ok and response.status == 404:
            return {}
        if 400 <= response.status < 500:
            raise SlurmRejected(f"Slurm REST rejected request ({response.status})")
        if response.status >= 500:
            error = SlurmAmbiguous if mutation else SlurmUnavailable
            raise error(f"Slurm REST failed request ({response.status})")
        if not 200 <= response.status < 300:
            error = SlurmAmbiguous if mutation else SlurmUnavailable
            raise error(f"unexpected Slurm REST status ({response.status})")
        if not response_body:
            return {}
        try:
            parsed = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = SlurmAmbiguous if mutation else SlurmUnavailable
            raise error("Slurm REST returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            error = SlurmAmbiguous if mutation else SlurmUnavailable
            raise error("Slurm REST returned a non-object response")
        errors = parsed.get("errors")
        if isinstance(errors, list) and errors:
            # A REST 2xx carrying errors cannot prove a submit did not happen.
            error = SlurmAmbiguous if mutation else SlurmUnavailable
            raise error("Slurm REST response reported errors")
        return parsed
