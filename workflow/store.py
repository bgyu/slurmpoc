"""SQLite-backed workflow and submission intent ledger."""

from __future__ import annotations

from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any, Iterator, Protocol, runtime_checkable
from urllib.parse import unquote, urlsplit

from .errors import CapabilityExpired, Conflict, NotFound, Unauthorized
from .security import capability_digest


SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
);
INSERT OR IGNORE INTO broker_schema (singleton, schema_version) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    owner TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    capability_hash TEXT NOT NULL,
    capability_path TEXT NOT NULL,
    capability_expires_at INTEGER NOT NULL,
    capability_used_at INTEGER,
    cancel_requested_at INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (tenant, idempotency_key)
);

CREATE TABLE IF NOT EXISTS generations (
    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    manifest_uri TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    max_parallel INTEGER NOT NULL,
    summary_uri TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, generation),
    UNIQUE (workflow_id, generation, manifest_sha256)
);

CREATE TABLE IF NOT EXISTS submission_intents (
    intent_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('master', 'array', 'finalizer')),
    marker TEXT NOT NULL UNIQUE,
    job_name TEXT NOT NULL,
    state TEXT NOT NULL,
    request_json TEXT NOT NULL,
    slurm_job_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (workflow_id, generation, kind)
);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    occurred_at INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS submission_intents_pending
    ON submission_intents(state, updated_at);
CREATE INDEX IF NOT EXISTS audit_events_workflow
    ON audit_events(workflow_id, sequence);
"""

MARIADB_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS broker_schema (
        singleton TINYINT PRIMARY KEY,
        schema_version INT NOT NULL,
        CONSTRAINT broker_schema_singleton CHECK (singleton = 1)
    ) ENGINE=InnoDB
    """,
    """
    INSERT IGNORE INTO broker_schema (singleton, schema_version) VALUES (1, 1)
    """,
    """
    CREATE TABLE IF NOT EXISTS workflows (
        workflow_id VARCHAR(64) PRIMARY KEY,
        tenant VARCHAR(128) NOT NULL,
        owner VARCHAR(128) NOT NULL,
        idempotency_key VARCHAR(200) NOT NULL,
        request_digest CHAR(64) NOT NULL,
        request_json LONGTEXT NOT NULL,
        correlation_id VARCHAR(128) NOT NULL,
        status VARCHAR(64) NOT NULL,
        capability_hash CHAR(64) NOT NULL,
        capability_path VARCHAR(1024) NOT NULL,
        capability_expires_at BIGINT NOT NULL,
        capability_used_at BIGINT NULL,
        cancel_requested_at BIGINT NULL,
        last_error TEXT NULL,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        UNIQUE KEY workflows_tenant_idempotency (tenant, idempotency_key)
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS generations (
        workflow_id VARCHAR(64) NOT NULL,
        generation INT NOT NULL,
        manifest_uri VARCHAR(2048) NOT NULL,
        manifest_sha256 CHAR(64) NOT NULL,
        chunk_count INT NOT NULL,
        max_parallel INT NOT NULL,
        summary_uri VARCHAR(2048) NOT NULL,
        status VARCHAR(64) NOT NULL,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY (workflow_id, generation),
        UNIQUE KEY generations_manifest
            (workflow_id, generation, manifest_sha256),
        CONSTRAINT generations_workflow_fk FOREIGN KEY (workflow_id)
            REFERENCES workflows(workflow_id) ON DELETE CASCADE
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS submission_intents (
        intent_id VARCHAR(64) PRIMARY KEY,
        workflow_id VARCHAR(64) NOT NULL,
        generation INT NOT NULL,
        kind VARCHAR(16) NOT NULL,
        marker VARCHAR(255) NOT NULL,
        job_name VARCHAR(128) NOT NULL,
        state VARCHAR(32) NOT NULL,
        request_json LONGTEXT NOT NULL,
        slurm_job_id VARCHAR(64) NULL,
        attempt_count INT NOT NULL DEFAULT 0,
        last_error TEXT NULL,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        UNIQUE KEY submission_marker (marker),
        UNIQUE KEY submission_workflow_kind (workflow_id, generation, kind),
        KEY submission_pending (state, updated_at),
        CONSTRAINT submission_workflow_fk FOREIGN KEY (workflow_id)
            REFERENCES workflows(workflow_id) ON DELETE CASCADE
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        sequence BIGINT AUTO_INCREMENT PRIMARY KEY,
        workflow_id VARCHAR(64) NOT NULL,
        occurred_at BIGINT NOT NULL,
        actor VARCHAR(128) NOT NULL,
        action VARCHAR(128) NOT NULL,
        detail_json LONGTEXT NOT NULL,
        KEY audit_workflow (workflow_id, sequence),
        CONSTRAINT audit_workflow_fk FOREIGN KEY (workflow_id)
            REFERENCES workflows(workflow_id) ON DELETE CASCADE
    ) ENGINE=InnoDB
    """,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@runtime_checkable
class Store(Protocol):
    """Persistence boundary used by the broker service.

    Implementations must provide serializable create/consume/cancel operations
    and a unique constraint for both idempotency keys and submission intents.
    """

    def ping(self) -> None: ...

    def create_or_get_workflow(self, **kwargs: Any) -> tuple[dict[str, Any], bool]: ...

    def get_workflow(self, workflow_id: str) -> dict[str, Any]: ...

    def get_workflow_for_actor(
        self, workflow_id: str, tenant: str, owner: str
    ) -> dict[str, Any]: ...

    def consume_capability(self, **kwargs: Any) -> dict[str, Any]: ...

    def prepare_intent(self, **kwargs: Any) -> dict[str, Any]: ...

    def start_intent_attempt(self, intent_id: str) -> dict[str, Any]: ...

    def mark_intent(self, intent_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def request_cancellation(
        self, workflow_id: str, tenant: str, owner: str
    ) -> dict[str, Any]: ...

    def set_workflow_status(
        self, workflow_id: str, status: str, error: str | None = None
    ) -> dict[str, Any]: ...

    def pending_intents(
        self, workflow_id: str | None = None
    ) -> list[dict[str, Any]]: ...


class SQLiteStore:
    """Small transactional store with a fresh connection per operation.

    WAL permits concurrent status reads while an HTTP worker records an intent.
    FULL synchronous mode matters here: a committed intent must survive a host
    crash before the corresponding non-transactional Slurm call is made.
    """

    def __init__(self, path: Path, clock: callable = time.time) -> None:
        self.path = path
        self.clock = clock
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(SCHEMA)
            version = connection.execute(
                "SELECT schema_version FROM broker_schema WHERE singleton=1"
            ).fetchone()
            if not version or version["schema_version"] != 1:
                raise RuntimeError("unsupported workflow database schema version")
        os.chmod(path, 0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path, timeout=10.0, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def ping(self) -> None:
        with self.connection() as connection:
            connection.execute("SELECT 1").fetchone()

    def create_or_get_workflow(
        self,
        *,
        workflow_id: str,
        tenant: str,
        owner: str,
        idempotency_key: str,
        request_digest: str,
        request: dict[str, Any],
        correlation_id: str,
        capability_hash: str,
        capability_path: str,
        capability_expires_at: int,
    ) -> tuple[dict[str, Any], bool]:
        now = int(self.clock())
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM workflows WHERE tenant=? AND idempotency_key=?",
                (tenant, idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_digest"] != request_digest:
                    raise Conflict(
                        "Idempotency-Key was already used with a different request"
                    )
                return self._snapshot(connection, existing["workflow_id"]), False

            try:
                connection.execute(
                    """
                    INSERT INTO workflows (
                        workflow_id, tenant, owner, idempotency_key, request_digest,
                        request_json, correlation_id, status, capability_hash,
                        capability_path, capability_expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'SUBMITTING_MASTER', ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        tenant,
                        owner,
                        idempotency_key,
                        request_digest,
                        _canonical(request),
                        correlation_id,
                        capability_hash,
                        capability_path,
                        capability_expires_at,
                        now,
                        now,
                    ),
                )
            except self.integrity_errors:
                # A second broker replica can pass the initial read.  The
                # database uniqueness constraint is the arbiter; return its
                # winner rather than creating a second master.
                existing = connection.execute(
                    "SELECT * FROM workflows WHERE tenant=? AND idempotency_key=?",
                    (tenant, idempotency_key),
                ).fetchone()
                if not existing:
                    raise
                if existing["request_digest"] != request_digest:
                    raise Conflict(
                        "Idempotency-Key was already used with a different request"
                    )
                return self._snapshot(connection, existing["workflow_id"]), False
            self._audit(
                connection,
                workflow_id,
                owner,
                "workflow.created",
                {"correlation_id": correlation_id},
                now,
            )
            return self._snapshot(connection, workflow_id), True

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            return self._snapshot(connection, workflow_id)

    def get_workflow_for_actor(
        self, workflow_id: str, tenant: str, owner: str
    ) -> dict[str, Any]:
        snapshot = self.get_workflow(workflow_id)
        workflow = snapshot["workflow"]
        if workflow["tenant"] != tenant or workflow["owner"] != owner:
            # Deliberately hide whether another tenant owns the identifier.
            raise NotFound("workflow not found")
        return snapshot

    def consume_capability(
        self,
        *,
        workflow_id: str,
        token: str,
        manifest_uri: str,
        manifest_sha256: str,
        chunk_count: int,
        max_parallel: int,
        summary_uri: str,
        actor: str = "workflow-master",
    ) -> dict[str, Any]:
        now = int(self.clock())
        supplied_hash = capability_digest(token)
        with self.transaction() as connection:
            workflow = self._locked_workflow(connection, workflow_id)
            if not workflow:
                raise NotFound("workflow not found")
            if not hmac.compare_digest(workflow["capability_hash"], supplied_hash):
                raise Unauthorized("invalid workflow capability")
            if workflow["capability_expires_at"] < now:
                raise CapabilityExpired("workflow capability has expired")
            if workflow["capability_used_at"] is not None:
                # The capability authorizes one mutation, not one HTTP
                # response.  If the master loses the response after commit it
                # may repeat the *identical* dispatch safely.  A different
                # manifest remains a conflict and can never create generation
                # zero twice.
                existing = connection.execute(
                    """
                    SELECT * FROM generations
                     WHERE workflow_id=? AND generation=0
                    """,
                    (workflow_id,),
                ).fetchone()
                if (
                    existing
                    and existing["manifest_uri"] == manifest_uri
                    and existing["manifest_sha256"] == manifest_sha256
                    and existing["chunk_count"] == chunk_count
                    and existing["max_parallel"] == max_parallel
                    and existing["summary_uri"] == summary_uri
                ):
                    return self._snapshot(connection, workflow_id)
                raise Conflict(
                    "workflow capability was consumed by a different dispatch"
                )
            if workflow["cancel_requested_at"] is not None:
                raise Conflict("workflow cancellation has been requested")
            if workflow["status"] in {"FAILED", "CANCELLED", "SUCCEEDED"}:
                raise Conflict("workflow is already terminal")

            # Generation zero is deterministic for the first immutable
            # manifest.  The normalized table intentionally leaves room for a
            # later API to create generation N+1 rather than mutating arrays.
            generation = 0
            connection.execute(
                """
                INSERT INTO generations (
                    workflow_id, generation, manifest_uri, manifest_sha256,
                    chunk_count, max_parallel, summary_uri, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'DISPATCHING', ?, ?)
                """,
                (
                    workflow_id,
                    generation,
                    manifest_uri,
                    manifest_sha256,
                    chunk_count,
                    max_parallel,
                    summary_uri,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE workflows
                   SET capability_used_at=?, status='DISPATCHING', updated_at=?
                 WHERE workflow_id=?
                """,
                (now, now, workflow_id),
            )
            self._audit(
                connection,
                workflow_id,
                actor,
                "capability.consumed",
                {
                    "generation": generation,
                    "manifest_sha256": manifest_sha256,
                    "chunk_count": chunk_count,
                },
                now,
            )
            return self._snapshot(connection, workflow_id)

    def prepare_intent(
        self,
        *,
        intent_id: str,
        workflow_id: str,
        generation: int,
        kind: str,
        marker: str,
        job_name: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        request_json = _canonical(request)
        now = int(self.clock())
        with self.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM submission_intents
                 WHERE workflow_id=? AND generation=? AND kind=?
                """,
                (workflow_id, generation, kind),
            ).fetchone()
            if existing:
                if existing["request_json"] != request_json:
                    raise Conflict("submission intent payload changed")
                return dict(existing)
            connection.execute(
                """
                INSERT INTO submission_intents (
                    intent_id, workflow_id, generation, kind, marker, job_name,
                    state, request_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?, ?)
                """,
                (
                    intent_id,
                    workflow_id,
                    generation,
                    kind,
                    marker,
                    job_name,
                    request_json,
                    now,
                    now,
                ),
            )
            self._audit(
                connection,
                workflow_id,
                "workflow-broker",
                f"submission.{kind}.prepared",
                {"generation": generation, "marker": marker},
                now,
            )
            return dict(
                connection.execute(
                    "SELECT * FROM submission_intents WHERE intent_id=?",
                    (intent_id,),
                ).fetchone()
            )

    def start_intent_attempt(self, intent_id: str) -> dict[str, Any]:
        now = int(self.clock())
        with self.transaction() as connection:
            result = connection.execute(
                """
                UPDATE submission_intents
                   SET state='SUBMITTING', attempt_count=attempt_count+1,
                       updated_at=?
                 WHERE intent_id=? AND state='PREPARED'
                """,
                (now, intent_id),
            )
            row = connection.execute(
                "SELECT * FROM submission_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if not row:
                raise NotFound("submission intent not found")
            value = dict(row)
            value["_claimed"] = bool(result.rowcount)
            return value

    def mark_intent(
        self,
        intent_id: str,
        *,
        state: str,
        slurm_job_id: str | None = None,
        error: str | None = None,
        workflow_status: str | None = None,
        generation_status: str | None = None,
    ) -> dict[str, Any]:
        now = int(self.clock())
        with self.transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM submission_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if not intent:
                raise NotFound("submission intent not found")
            connection.execute(
                """
                UPDATE submission_intents
                   SET state=?, slurm_job_id=COALESCE(?, slurm_job_id),
                       last_error=?, updated_at=?
                 WHERE intent_id=?
                """,
                (state, slurm_job_id, error, now, intent_id),
            )
            if workflow_status:
                connection.execute(
                    """
                    UPDATE workflows SET status=?, last_error=?, updated_at=?
                     WHERE workflow_id=?
                    """,
                    (workflow_status, error, now, intent["workflow_id"]),
                )
            if generation_status:
                connection.execute(
                    """
                    UPDATE generations SET status=?, updated_at=?
                     WHERE workflow_id=? AND generation=?
                    """,
                    (
                        generation_status,
                        now,
                        intent["workflow_id"],
                        intent["generation"],
                    ),
                )
            self._audit(
                connection,
                intent["workflow_id"],
                "workflow-broker",
                f"submission.{intent['kind']}.{state.lower()}",
                {
                    "generation": intent["generation"],
                    "slurm_job_id": slurm_job_id,
                    "error": error,
                },
                now,
            )
            return self._snapshot(connection, intent["workflow_id"])

    def request_cancellation(
        self, workflow_id: str, tenant: str, owner: str
    ) -> dict[str, Any]:
        now = int(self.clock())
        with self.transaction() as connection:
            workflow = self._locked_workflow(connection, workflow_id)
            if not workflow or workflow["tenant"] != tenant or workflow["owner"] != owner:
                raise NotFound("workflow not found")
            if workflow["status"] in {"CANCELLED", "SUCCEEDED", "FAILED"}:
                return self._snapshot(connection, workflow_id)
            connection.execute(
                """
                UPDATE workflows
                   SET status='CANCEL_REQUESTED',
                       cancel_requested_at=COALESCE(cancel_requested_at, ?),
                       updated_at=?
                 WHERE workflow_id=?
                """,
                (now, now, workflow_id),
            )
            self._audit(
                connection,
                workflow_id,
                owner,
                "workflow.cancel_requested",
                {},
                now,
            )
            return self._snapshot(connection, workflow_id)

    def set_workflow_status(
        self, workflow_id: str, status: str, error: str | None = None
    ) -> dict[str, Any]:
        now = int(self.clock())
        with self.transaction() as connection:
            result = connection.execute(
                """
                UPDATE workflows SET status=?, last_error=?, updated_at=?
                 WHERE workflow_id=?
                """,
                (status, error, now, workflow_id),
            )
            if not result.rowcount:
                raise NotFound("workflow not found")
            return self._snapshot(connection, workflow_id)

    def pending_intents(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        states = ("PREPARED", "SUBMITTING", "AMBIGUOUS")
        with self.connection() as connection:
            if workflow_id:
                rows = connection.execute(
                    """
                    SELECT * FROM submission_intents
                     WHERE workflow_id=? AND state IN (?, ?, ?)
                     ORDER BY created_at, kind
                    """,
                    (workflow_id, *states),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM submission_intents
                     WHERE state IN (?, ?, ?)
                     ORDER BY created_at, kind
                    """,
                    states,
                ).fetchall()
            return [dict(row) for row in rows]

    def _snapshot(
        self, connection: sqlite3.Connection, workflow_id: str
    ) -> dict[str, Any]:
        workflow = connection.execute(
            "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if not workflow:
            raise NotFound("workflow not found")
        generations = connection.execute(
            "SELECT * FROM generations WHERE workflow_id=? ORDER BY generation",
            (workflow_id,),
        ).fetchall()
        intents = connection.execute(
            """
            SELECT * FROM submission_intents
             WHERE workflow_id=? ORDER BY generation, created_at, kind
            """,
            (workflow_id,),
        ).fetchall()
        result = {
            "workflow": dict(workflow),
            "generations": [dict(row) for row in generations],
            "intents": [dict(row) for row in intents],
        }
        result["workflow"]["request"] = json.loads(
            result["workflow"].pop("request_json")
        )
        for intent in result["intents"]:
            intent["request"] = json.loads(intent.pop("request_json"))
        return result

    @staticmethod
    def _locked_workflow(
        connection: sqlite3.Connection, workflow_id: str
    ) -> sqlite3.Row | None:
        # BEGIN IMMEDIATE already serializes SQLite writers.
        return connection.execute(
            "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        workflow_id: str,
        actor: str,
        action: str,
        detail: dict[str, Any],
        occurred_at: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                workflow_id, occurred_at, actor, action, detail_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (workflow_id, occurred_at, actor, action, _canonical(detail)),
        )

    integrity_errors: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,)


class _MariaConnection:
    """Tiny adapter that keeps the store's SQL independent of DB-API quirks."""

    def __init__(self, connection: Any) -> None:
        self.raw = connection

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        # All broker statements use positional placeholders and contain no
        # literal question marks.  Keeping that small SQL subset makes the
        # durability logic identical in local and production profiles.
        cursor = self.raw.cursor()
        cursor.execute(statement.replace("?", "%s"), parameters)
        return cursor

    def begin(self) -> None:
        self.raw.begin()

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def _read_password_file(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("database password file must be a regular non-symlink")
    if info.st_mode & 0o077:
        raise ValueError("database password file must have mode 0400 or 0600")
    if info.st_size > 4096:
        raise ValueError("database password file is unexpectedly large")
    password = path.read_text(encoding="utf-8").strip()
    if not password or "\n" in password or "\r" in password:
        raise ValueError("database password file must contain one non-empty line")
    return password


class MariaDBStore(SQLiteStore):
    """HA-capable production store using PyMySQL and transactional InnoDB.

    It deliberately reuses the state-transition methods above.  This avoids a
    dangerous class of profile-specific semantic drift: idempotency and
    capability consumption have exactly the same transaction boundaries in
    tests (SQLite) and production (MariaDB).
    """

    def __init__(
        self,
        database_url: str,
        user: str,
        password_file: Path,
        *,
        ca_file: Path | None = None,
        client_cert: Path | None = None,
        client_key: Path | None = None,
        connect_timeout: int = 10,
        clock: callable = time.time,
        initialize_schema: bool = False,
    ) -> None:
        try:
            import pymysql  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "MariaDB backend selected but PyMySQL is not installed"
            ) from exc
        parsed = urlsplit(database_url)
        database = unquote(parsed.path.lstrip("/"))
        if "/" in database or not database:
            raise ValueError("BROKER_DATABASE_URL must name exactly one database")
        self.driver = pymysql
        self.integrity_errors = (pymysql.err.IntegrityError,)
        self.host = parsed.hostname or ""
        self.port = parsed.port or 3306
        self.database = database
        self.user = user
        self.password_file = password_file
        self.ca_file = ca_file
        self.client_cert = client_cert
        self.client_key = client_key
        self.connect_timeout = connect_timeout
        self.clock = clock
        # Validate the secret before attempting a network connection.  Its
        # contents are never retained as an attribute or placed in an error.
        _read_password_file(password_file)
        with self.connection() as connection:
            if initialize_schema:
                # This path is used only by ``python -m workflow.migrate`` with
                # temporary DDL credentials.  The normal broker intentionally
                # never attempts CREATE/ALTER at startup.
                for statement in MARIADB_SCHEMA:
                    connection.execute(statement)
            try:
                version = connection.execute(
                    "SELECT schema_version FROM broker_schema WHERE singleton=1"
                ).fetchone()
            except pymysql.err.ProgrammingError as exc:
                raise RuntimeError(
                    "workflow schema is missing; run python3 -m workflow.migrate"
                ) from exc
            if not version or version["schema_version"] != 1:
                raise RuntimeError("unsupported workflow database schema version")

    @contextmanager
    def connection(self) -> Iterator[_MariaConnection]:
        ssl_options: dict[str, Any] | None = None
        if self.ca_file:
            ssl_options = {
                "ca": str(self.ca_file),
                "check_hostname": True,
            }
            if self.client_cert:
                ssl_options["cert"] = str(self.client_cert)
            if self.client_key:
                ssl_options["key"] = str(self.client_key)
        raw = self.driver.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=_read_password_file(self.password_file),
            database=self.database,
            charset="utf8mb4",
            cursorclass=self.driver.cursors.DictCursor,
            autocommit=True,
            connect_timeout=self.connect_timeout,
            read_timeout=self.connect_timeout,
            write_timeout=self.connect_timeout,
            ssl=ssl_options,
            init_command="SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
        )
        connection = _MariaConnection(raw)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[_MariaConnection]:
        with self.connection() as connection:
            connection.begin()
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _locked_workflow(
        connection: _MariaConnection, workflow_id: str
    ) -> dict[str, Any] | None:
        return connection.execute(
            "SELECT * FROM workflows WHERE workflow_id=? FOR UPDATE", (workflow_id,)
        ).fetchone()


def store_from_config(config: Any, clock: callable = time.time) -> Store:
    """Select the local or production backend without importing PyMySQL locally."""

    if config.database_url:
        return MariaDBStore(
            config.database_url,
            config.database_user,
            config.database_password_file,
            ca_file=config.database_ca_file,
            client_cert=config.database_client_cert,
            client_key=config.database_client_key,
            connect_timeout=config.database_connect_timeout,
            clock=clock,
        )
    if config.profile == "production":
        # Config validation normally catches this; retain the guard for callers
        # constructing BrokerConfig directly in tests or embedded deployments.
        raise ValueError("production profile refuses the SQLite backend")
    return SQLiteStore(config.database_path, clock=clock)
