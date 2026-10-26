"""Configuration parsing and fail-closed validation for the broker."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


SERVICE_IDENTITY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
HTTP_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _boolean(env: dict[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _integer(
    env: dict[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class BrokerConfig:
    database_path: Path
    listen_host: str
    listen_port: int
    public_base_url: str
    capability_dir: Path
    capability_job_dir: Path
    control_dir: Path
    allowed_file_root: Path
    master_script: Path
    worker_script: Path
    finalizer_script: Path
    helper_dir: Path
    profile: str = "local"
    auth_mode: str = "local-token"
    api_token_file: Path | None = None
    trusted_proxy_token_file: Path | None = None
    trusted_proxy_cidrs: tuple[str, ...] = ()
    callback_trusted_cidrs: tuple[str, ...] = ()
    database_url: str | None = None
    database_user: str | None = None
    database_password_file: Path | None = None
    database_ca_file: Path | None = None
    database_client_cert: Path | None = None
    database_client_key: Path | None = None
    database_connect_timeout: int = 10
    reconcile_interval_seconds: int = 15
    http_max_connections: int = 64
    capability_ttl_seconds: int = 900
    max_request_bytes: int = 64 * 1024
    max_chunks: int = 10000
    max_parallel: int = 100
    slurm_rest_url: str | None = None
    slurm_rest_socket: Path | None = None
    slurm_timeout_seconds: int = 15
    slurm_token_file: Path | None = None
    slurm_token_header: str = "X-SLURM-USER-TOKEN"
    slurm_token_prefix: str = ""
    slurm_user_header: str | None = "X-SLURM-USER-NAME"
    slurm_ca_file: Path | None = None
    slurm_client_cert: Path | None = None
    slurm_client_key: Path | None = None
    slurm_submit_user: str = "workflow"
    allow_insecure_slurm_http: bool = False

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "BrokerConfig":
        env = dict(os.environ if environ is None else environ)
        project_root = Path(__file__).resolve().parent.parent
        # Source checkouts keep the examples grouped below examples/workflow,
        # while the immutable image installs them directly below
        # /opt/slurm-workflow/examples.  Auto-detection keeps both layouts
        # usable without making production rely on the current directory.
        source_scripts = project_root / "examples" / "workflow"
        installed_scripts = project_root / "examples"
        scripts = source_scripts if source_scripts.is_dir() else installed_scripts
        socket_value = env.get("SLURM_REST_SOCKET")
        url_value = env.get("SLURM_REST_URL")
        if bool(socket_value) == bool(url_value):
            raise ValueError(
                "set exactly one of SLURM_REST_SOCKET or SLURM_REST_URL"
            )

        config = cls(
            database_path=Path(env.get("WORKFLOW_DB", "workflow.sqlite3")),
            listen_host=env.get("WORKFLOW_LISTEN_HOST", "127.0.0.1"),
            listen_port=_integer(env, "WORKFLOW_LISTEN_PORT", 8088, 1, 65535),
            public_base_url=env.get(
                "WORKFLOW_PUBLIC_BASE_URL", "http://workflow-broker:8088"
            ).rstrip("/"),
            capability_dir=Path(
                env.get("WORKFLOW_CAPABILITY_DIR", "/run/workflow/capabilities")
            ),
            capability_job_dir=Path(
                env.get(
                    "WORKFLOW_CAPABILITY_JOB_DIR",
                    env.get("WORKFLOW_CAPABILITY_DIR", "/run/workflow/capabilities"),
                )
            ),
            control_dir=Path(
                env.get("WORKFLOW_CONTROL_DIR", "/shared/.workflow-control")
            ),
            allowed_file_root=Path(
                env.get("WORKFLOW_ALLOWED_FILE_ROOT", "/shared")
            ),
            master_script=Path(
                env.get("WORKFLOW_MASTER_SCRIPT", str(scripts / "master.sbatch"))
            ),
            worker_script=Path(
                env.get("WORKFLOW_WORKER_SCRIPT", str(scripts / "worker.sbatch"))
            ),
            finalizer_script=Path(
                env.get(
                    "WORKFLOW_FINALIZER_SCRIPT", str(scripts / "finalizer.sbatch")
                )
            ),
            helper_dir=Path(env.get("WORKFLOW_HELPER_DIR", str(scripts))),
            profile=env.get("BROKER_PROFILE", "local").lower(),
            auth_mode=env.get(
                "BROKER_AUTH_MODE",
                "trusted-proxy"
                if env.get("BROKER_PROFILE", "local").lower() == "production"
                else "local-token",
            ).lower(),
            api_token_file=(
                Path(env["BROKER_API_TOKEN_FILE"])
                if env.get("BROKER_API_TOKEN_FILE")
                else None
            ),
            trusted_proxy_token_file=(
                Path(env["BROKER_TRUSTED_PROXY_TOKEN_FILE"])
                if env.get("BROKER_TRUSTED_PROXY_TOKEN_FILE")
                else None
            ),
            trusted_proxy_cidrs=tuple(
                item.strip()
                for item in env.get("BROKER_TRUSTED_PROXY_CIDRS", "").split(",")
                if item.strip()
            ),
            callback_trusted_cidrs=tuple(
                item.strip()
                for item in env.get("BROKER_CALLBACK_TRUSTED_CIDRS", "").split(",")
                if item.strip()
            ),
            database_url=env.get("BROKER_DATABASE_URL"),
            database_user=env.get("BROKER_DATABASE_USER"),
            database_password_file=(
                Path(env["BROKER_DATABASE_PASSWORD_FILE"])
                if env.get("BROKER_DATABASE_PASSWORD_FILE")
                else None
            ),
            database_ca_file=(
                Path(env["BROKER_DATABASE_CA_FILE"])
                if env.get("BROKER_DATABASE_CA_FILE")
                else None
            ),
            database_client_cert=(
                Path(env["BROKER_DATABASE_CLIENT_CERT"])
                if env.get("BROKER_DATABASE_CLIENT_CERT")
                else None
            ),
            database_client_key=(
                Path(env["BROKER_DATABASE_CLIENT_KEY"])
                if env.get("BROKER_DATABASE_CLIENT_KEY")
                else None
            ),
            database_connect_timeout=_integer(
                env, "BROKER_DATABASE_CONNECT_TIMEOUT", 10, 1, 120
            ),
            reconcile_interval_seconds=_integer(
                env, "BROKER_RECONCILE_INTERVAL_SECONDS", 15, 1, 3600
            ),
            http_max_connections=_integer(
                env, "BROKER_HTTP_MAX_CONNECTIONS", 64, 1, 1024
            ),
            capability_ttl_seconds=_integer(
                env, "WORKFLOW_CAPABILITY_TTL_SECONDS", 900, 30, 86400
            ),
            max_request_bytes=_integer(
                env, "WORKFLOW_MAX_REQUEST_BYTES", 64 * 1024, 1024, 1024 * 1024
            ),
            max_chunks=_integer(env, "WORKFLOW_MAX_CHUNKS", 10000, 1, 1000000),
            max_parallel=_integer(env, "WORKFLOW_MAX_PARALLEL", 100, 1, 10000),
            slurm_rest_url=url_value.rstrip("/") if url_value else None,
            slurm_rest_socket=Path(socket_value) if socket_value else None,
            slurm_timeout_seconds=_integer(
                env, "SLURM_REST_TIMEOUT_SECONDS", 15, 1, 300
            ),
            slurm_token_file=(
                Path(env["SLURM_REST_TOKEN_FILE"])
                if env.get("SLURM_REST_TOKEN_FILE")
                else None
            ),
            slurm_token_header=env.get(
                "SLURM_REST_TOKEN_HEADER", "X-SLURM-USER-TOKEN"
            ),
            slurm_token_prefix=env.get("SLURM_REST_TOKEN_PREFIX", ""),
            slurm_user_header=(
                env.get("SLURM_REST_USER_HEADER", "X-SLURM-USER-NAME") or None
            ),
            slurm_ca_file=(
                Path(env["SLURM_REST_CA_FILE"])
                if env.get("SLURM_REST_CA_FILE")
                else None
            ),
            slurm_client_cert=(
                Path(env["SLURM_REST_CLIENT_CERT"])
                if env.get("SLURM_REST_CLIENT_CERT")
                else None
            ),
            slurm_client_key=(
                Path(env["SLURM_REST_CLIENT_KEY"])
                if env.get("SLURM_REST_CLIENT_KEY")
                else None
            ),
            slurm_submit_user=env.get("SLURM_REST_SUBMIT_USER", "workflow"),
            allow_insecure_slurm_http=_boolean(
                env, "SLURM_REST_ALLOW_INSECURE_HTTP", False
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.profile not in {"local", "production"}:
            raise ValueError("BROKER_PROFILE must be local or production")
        if self.profile == "local":
            if self.auth_mode != "local-token" or not self.api_token_file:
                raise ValueError(
                    "local profile requires BROKER_AUTH_MODE=local-token and "
                    "BROKER_API_TOKEN_FILE"
                )
        elif (
            self.auth_mode != "trusted-proxy"
            or not self.trusted_proxy_token_file
            or not self.trusted_proxy_cidrs
            or not self.callback_trusted_cidrs
        ):
            raise ValueError(
                "production requires trusted-proxy auth, "
                "BROKER_TRUSTED_PROXY_TOKEN_FILE, BROKER_TRUSTED_PROXY_CIDRS, "
                "and BROKER_CALLBACK_TRUSTED_CIDRS"
            )
        for cidr in (*self.trusted_proxy_cidrs, *self.callback_trusted_cidrs):
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy CIDR: {cidr}") from exc
        if self.profile == "production" and not self.database_url:
            raise ValueError("production profile refuses SQLite; set BROKER_DATABASE_URL")
        if self.database_url:
            database = urlsplit(self.database_url)
            if database.scheme not in {"mysql", "mariadb"}:
                raise ValueError("BROKER_DATABASE_URL must use mysql:// or mariadb://")
            if (
                not database.hostname
                or not database.path.strip("/")
                or database.username
                or database.password
                or database.query
                or database.fragment
            ):
                raise ValueError(
                    "BROKER_DATABASE_URL needs host/database and no credentials/query"
                )
            if not self.database_user or not self.database_password_file:
                raise ValueError(
                    "MariaDB requires BROKER_DATABASE_USER and "
                    "BROKER_DATABASE_PASSWORD_FILE"
                )
            if self.profile == "production" and not self.database_ca_file:
                raise ValueError(
                    "production MariaDB requires BROKER_DATABASE_CA_FILE"
                )
        if self.database_client_key and not self.database_client_cert:
            raise ValueError("a database client key requires a client certificate")
        if bool(self.slurm_rest_socket) == bool(self.slurm_rest_url):
            raise ValueError("exactly one Slurm REST endpoint must be configured")
        if self.slurm_rest_url:
            parsed = urlsplit(self.slurm_rest_url)
            allowed = {"https"}
            if self.allow_insecure_slurm_http:
                allowed.add("http")
            if parsed.scheme not in allowed or not parsed.hostname:
                raise ValueError("SLURM_REST_URL must be an approved absolute URL")
            if parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("SLURM_REST_URL cannot contain credentials/query/fragment")
            if not self.slurm_token_file:
                raise ValueError(
                    "network Slurm REST requires SLURM_REST_TOKEN_FILE"
                )
        if self.slurm_client_key and not self.slurm_client_cert:
            raise ValueError("a Slurm client key requires a client certificate")
        if not HTTP_HEADER.fullmatch(self.slurm_token_header):
            raise ValueError("invalid Slurm token header name")
        if self.slurm_user_header and not HTTP_HEADER.fullmatch(
            self.slurm_user_header
        ):
            raise ValueError("invalid Slurm user header name")
        if len(self.slurm_token_prefix) > 64 or any(
            ch in self.slurm_token_prefix for ch in "\r\n"
        ):
            raise ValueError("invalid Slurm token prefix")
        if not SERVICE_IDENTITY.fullmatch(self.slurm_submit_user):
            raise ValueError("SLURM_REST_SUBMIT_USER is not a valid service identity")
        public_url = urlsplit(self.public_base_url)
        permitted_public_schemes = {"https"}
        if self.profile == "local":
            permitted_public_schemes.add("http")
        if (
            public_url.scheme not in permitted_public_schemes
            or not public_url.hostname
            or public_url.username
            or public_url.password
            or public_url.query
            or public_url.fragment
        ):
            raise ValueError("WORKFLOW_PUBLIC_BASE_URL is not an approved URL")
        for script in (
            self.master_script,
            self.worker_script,
            self.finalizer_script,
        ):
            if not script.is_file():
                raise ValueError(f"batch script does not exist: {script}")
        if not self.allowed_file_root.is_absolute():
            raise ValueError("WORKFLOW_ALLOWED_FILE_ROOT must be absolute")
        if not self.capability_job_dir.is_absolute():
            raise ValueError("WORKFLOW_CAPABILITY_JOB_DIR must be absolute")
        if not self.capability_dir.is_absolute():
            raise ValueError("WORKFLOW_CAPABILITY_DIR must be absolute")
        if not self.control_dir.is_absolute():
            raise ValueError("WORKFLOW_CONTROL_DIR must be absolute")
        try:
            self.control_dir.resolve(strict=False).relative_to(
                self.allowed_file_root.resolve(strict=False)
            )
        except ValueError as exc:
            raise ValueError(
                "WORKFLOW_CONTROL_DIR must be below WORKFLOW_ALLOWED_FILE_ROOT"
            ) from exc
        if not self.helper_dir.is_absolute():
            raise ValueError("WORKFLOW_HELPER_DIR must be absolute")
