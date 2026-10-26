"""Workflow state machine and Slurm orchestration."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import uuid

from .config import BrokerConfig
from .errors import BadRequest, Conflict, ServiceUnavailable
from .security import CapabilityFiles, capability_digest, new_capability
from .slurm import SlurmAmbiguous, SlurmClient, SlurmRejected, SlurmUnavailable
from .store import Store


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:@+-]{0,199}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SLURM_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,127}$")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _safe_error(error: BaseException) -> str:
    # Slurm exception messages are intentionally body-free.  Still cap them so
    # a pathological local exception cannot inflate the durable audit record.
    return str(error).replace("\r", " ").replace("\n", " ")[:300]


class WorkflowBroker:
    def __init__(
        self,
        config: BrokerConfig,
        store: Store,
        slurm: SlurmClient,
        *,
        clock: callable = time.time,
        token_factory: callable = new_capability,
    ) -> None:
        self.config = config
        self.store = store
        self.slurm = slurm
        self.clock = clock
        self.token_factory = token_factory
        self.capabilities = CapabilityFiles(
            config.capability_dir, config.capability_job_dir
        )
        config.control_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        if config.control_dir.is_symlink() or not config.control_dir.is_dir():
            raise ValueError("workflow control directory must be a real directory")
        if config.control_dir.stat().st_mode & 0o002:
            raise ValueError("workflow control directory must not be world writable")
        self.scripts = {
            "master": self._read_script(config.master_script),
            "array": self._read_script(config.worker_script),
            "finalizer": self._read_script(config.finalizer_script),
        }

    @staticmethod
    def _read_script(path: Path) -> str:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"batch script must be a regular non-symlink: {path}")
        if info.st_mode & 0o022:
            raise ValueError(f"batch script must not be group/world writable: {path}")
        if info.st_size > 1024 * 1024:
            raise ValueError(f"batch script is unexpectedly large: {path}")
        value = path.read_text(encoding="utf-8")
        if not value.startswith("#!"):
            raise ValueError(f"batch script lacks a shebang: {path}")
        return value

    def create_workflow(
        self,
        *,
        tenant: str,
        owner: str,
        idempotency_key: str,
        request: dict[str, Any],
        correlation_id: str,
    ) -> tuple[dict[str, Any], bool]:
        self._validate_actor(tenant, owner)
        if not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise BadRequest("Idempotency-Key has an invalid format")
        if not IDENTIFIER.fullmatch(correlation_id):
            raise BadRequest("correlation ID has an invalid format")
        specification = self._validate_create_request(request)
        request_digest = hashlib.sha256(_canonical_bytes(specification)).hexdigest()
        workflow_id = str(uuid.uuid4())
        token = self.token_factory()

        # Create the job's log directory before committing the workflow.  A
        # workflow whose master cannot open stdout/stderr is not runnable, and
        # recording it first would leave an idempotency key permanently bound
        # to a job that can never be submitted.  Both the log directory and
        # capability are cleaned up when the durable create loses a race or
        # fails before Slurm receives an intent.
        self._ensure_job_log_directory(workflow_id)
        try:
            job_capability_path = self.capabilities.stage(workflow_id, token)
            snapshot, created = self.store.create_or_get_workflow(
                workflow_id=workflow_id,
                tenant=tenant,
                owner=owner,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                request=specification,
                correlation_id=correlation_id,
                capability_hash=capability_digest(token),
                capability_path=str(job_capability_path),
                capability_expires_at=int(self.clock())
                + self.config.capability_ttl_seconds,
            )
        except BaseException:
            self.capabilities.remove(workflow_id)
            self._remove_empty_job_log_directory(workflow_id)
            raise
        if not created:
            # The winning request already owns a different capability file.
            self.capabilities.remove(workflow_id)
            self._remove_empty_job_log_directory(workflow_id)
            self.reconcile(snapshot["workflow"]["workflow_id"])
            return self.public_snapshot(
                self.store.get_workflow(snapshot["workflow"]["workflow_id"])
            ), False

        self._submit_master(snapshot)
        result = self.store.get_workflow(workflow_id)
        return self.public_snapshot(result), True

    def get_status(
        self, workflow_id: str, *, tenant: str, owner: str
    ) -> dict[str, Any]:
        self._validate_workflow_id(workflow_id)
        self.store.get_workflow_for_actor(workflow_id, tenant, owner)
        self.reconcile(workflow_id)
        self._refresh_from_summary(workflow_id)
        return self.public_snapshot(
            self.store.get_workflow_for_actor(workflow_id, tenant, owner)
        )

    def dispatch(
        self, workflow_id: str, *, capability: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_workflow_id(workflow_id)
        if not capability or len(capability) > 256:
            raise BadRequest("workflow capability is missing or invalid")
        if not isinstance(request, dict) or set(request) != {
            "manifest_uri",
            "manifest_sha256",
            "chunk_count",
        }:
            raise BadRequest(
                "dispatch requires manifest_uri, manifest_sha256, and chunk_count"
            )
        snapshot = self.store.get_workflow(workflow_id)
        specification = snapshot["workflow"]["request"]
        expected = self._derived_uris(specification["workspace_uri"], workflow_id)
        manifest_uri = self._validate_uri(request["manifest_uri"], "manifest_uri")
        if manifest_uri != expected["manifest_uri"]:
            raise BadRequest("manifest_uri does not match this workflow")
        manifest_hash = request["manifest_sha256"]
        if not isinstance(manifest_hash, str) or not SHA256.fullmatch(manifest_hash):
            raise BadRequest("manifest_sha256 must be lowercase hexadecimal")
        chunk_count = request["chunk_count"]
        if (
            isinstance(chunk_count, bool)
            or not isinstance(chunk_count, int)
            or not 1 <= chunk_count <= self.config.max_chunks
        ):
            raise BadRequest("chunk_count is outside broker policy")

        snapshot = self.store.consume_capability(
            workflow_id=workflow_id,
            token=capability,
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_hash,
            chunk_count=chunk_count,
            max_parallel=specification["max_parallel"],
            summary_uri=expected["summary_uri"],
        )
        # The database rejects every subsequent use even if unlinking is
        # interrupted.  Removing the file additionally minimizes exposure.
        self.capabilities.remove(workflow_id)
        self._submit_array(snapshot)
        return self.public_snapshot(self.store.get_workflow(workflow_id))

    def cancel(
        self, workflow_id: str, *, tenant: str, owner: str
    ) -> dict[str, Any]:
        self._validate_workflow_id(workflow_id)
        self._validate_actor(tenant, owner)
        lock_path, cancel_path = self._control_paths(workflow_id)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o640)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("workflow cancellation lock is not a regular file")
            # Workers take a shared lock for their final check-and-commit.  The
            # marker is made durable *before* CANCEL_REQUESTED.  This order is
            # fail-closed: even if the database write then fails, no later
            # worker can commit output while operations repair the request.
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = self.store.get_workflow_for_actor(workflow_id, tenant, owner)
            if current["workflow"]["status"] not in {
                "CANCELLED",
                "SUCCEEDED",
                "FAILED",
            }:
                self._create_cancel_marker(cancel_path, workflow_id)
            snapshot = self.store.request_cancellation(workflow_id, tenant, owner)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self.capabilities.remove(workflow_id)

        if snapshot["workflow"]["status"] != "CANCEL_REQUESTED":
            return self.public_snapshot(snapshot)
        self.reconcile(workflow_id, submit_dependents=False)
        snapshot = self.store.get_workflow(workflow_id)
        failures: list[str] = []
        for intent in snapshot["intents"]:
            job_id = intent["slurm_job_id"]
            if not job_id:
                continue
            try:
                self.slurm.cancel_job(
                    job_id,
                    # The token file belongs to this deliberately narrow POSIX
                    # service identity.  Never combine one static token with a
                    # caller-controlled impersonation header.
                    identity=self.config.slurm_submit_user,
                    correlation_id=snapshot["workflow"]["correlation_id"],
                )
            except (SlurmAmbiguous, SlurmRejected) as error:
                failures.append(_safe_error(error))
        pending = self.store.pending_intents(workflow_id)
        if failures or pending:
            detail = "; ".join(failures) if failures else "submission mapping unresolved"
            self.store.set_workflow_status(
                workflow_id, "CANCEL_REQUESTED", detail
            )
        else:
            self.store.set_workflow_status(workflow_id, "CANCELLED")
        return self.public_snapshot(self.store.get_workflow(workflow_id))

    def reconcile(
        self, workflow_id: str | None = None, *, submit_dependents: bool = True
    ) -> None:
        pending = self.store.pending_intents(workflow_id)
        for intent in pending:
            snapshot = self.store.get_workflow(intent["workflow_id"])
            workflow = snapshot["workflow"]
            if intent["state"] == "PREPARED":
                if workflow["cancel_requested_at"] is not None:
                    # PREPARED proves the REST mutation was never attempted:
                    # start_intent_attempt changes the state before I/O.  It is
                    # therefore safe to abandon during cancellation.
                    self.store.mark_intent(
                        intent["intent_id"],
                        state="ABANDONED",
                        error="cancelled before submission",
                    )
                    continue
                try:
                    request = json.loads(intent["request_json"])
                except (KeyError, TypeError, json.JSONDecodeError):
                    self.store.mark_intent(
                        intent["intent_id"],
                        state="REJECTED",
                        error="durable submission intent is invalid",
                        workflow_status="NEEDS_ATTENTION",
                    )
                    continue
                job_id = self._submit_intent(
                    snapshot,
                    intent["generation"],
                    intent["kind"],
                    request,
                )
                if (
                    job_id
                    and intent["kind"] == "array"
                    and submit_dependents
                    and workflow["cancel_requested_at"] is None
                ):
                    self._ensure_finalizer(
                        self.store.get_workflow(intent["workflow_id"]), job_id
                    )
                continue
            try:
                matches = self.slurm.find_jobs_by_marker(
                    intent["marker"],
                    identity=self.config.slurm_submit_user,
                    correlation_id=workflow["correlation_id"],
                )
            except SlurmUnavailable:
                continue
            if not matches:
                self.store.mark_intent(
                    intent["intent_id"],
                    state="AMBIGUOUS",
                    error="no marked Slurm job is currently visible",
                    workflow_status=self._unknown_status(intent["kind"]),
                    generation_status=(
                        self._unknown_status(intent["kind"])
                        if intent["kind"] != "master"
                        else None
                    ),
                )
                continue
            if len(matches) > 1:
                self.store.mark_intent(
                    intent["intent_id"],
                    state="DUPLICATE",
                    error="multiple Slurm jobs have the same workflow marker",
                    workflow_status="NEEDS_ATTENTION",
                    generation_status=(
                        "NEEDS_ATTENTION" if intent["kind"] != "master" else None
                    ),
                )
                continue
            self._record_submission(intent, matches[0])
            if intent["kind"] == "array" and submit_dependents:
                updated = self.store.get_workflow(intent["workflow_id"])
                if updated["workflow"]["cancel_requested_at"] is None:
                    self._ensure_finalizer(updated, matches[0])

    def reconcile_all(self) -> None:
        self.reconcile(None)

    def public_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        workflow = snapshot["workflow"]
        generations = [
            {
                "generation": row["generation"],
                "manifest_uri": row["manifest_uri"],
                "manifest_sha256": row["manifest_sha256"],
                "chunk_count": row["chunk_count"],
                "max_parallel": row["max_parallel"],
                "summary_uri": row["summary_uri"],
                "status": row["status"],
            }
            for row in snapshot["generations"]
        ]
        jobs = {
            intent["kind"]: {
                "generation": intent["generation"],
                "state": intent["state"],
                "job_id": intent["slurm_job_id"],
                "marker": intent["marker"],
            }
            for intent in snapshot["intents"]
        }
        return {
            "workflow_id": workflow["workflow_id"],
            "status": workflow["status"],
            "owner": workflow["owner"],
            "tenant": workflow["tenant"],
            "correlation_id": workflow["correlation_id"],
            "capability_expires_at": workflow["capability_expires_at"],
            "capability_consumed": workflow["capability_used_at"] is not None,
            "cancel_requested_at": workflow["cancel_requested_at"],
            "last_error": workflow["last_error"],
            "created_at": workflow["created_at"],
            "updated_at": workflow["updated_at"],
            "generations": generations,
            "jobs": jobs,
            "links": {
                "self": f"/v1/workflows/{workflow['workflow_id']}",
                "dispatch": f"/v1/workflows/{workflow['workflow_id']}/dispatch",
                "cancel": f"/v1/workflows/{workflow['workflow_id']}/cancel",
            },
        }

    def _submit_master(self, snapshot: dict[str, Any]) -> None:
        workflow = snapshot["workflow"]
        specification = workflow["request"]
        uris = self._derived_uris(
            specification["workspace_uri"], workflow["workflow_id"]
        )
        lock_path, cancel_path = self._control_paths(workflow["workflow_id"])
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "WORKFLOW_ID": workflow["workflow_id"],
            "WORKFLOW_BROKER_URL": self.config.public_base_url,
            "WORKFLOW_CAPABILITY_FILE": workflow["capability_path"],
            "WORKFLOW_INPUT_SPEC_URI": specification["input_spec_uri"],
            "WORKFLOW_MANIFEST_URI": uris["manifest_uri"],
            "WORKFLOW_OUTPUT_ROOT_URI": uris["output_root_uri"],
            "WORKFLOW_SUMMARY_URI": uris["summary_uri"],
            "WORKFLOW_CANCEL_LOCK": str(lock_path),
            "WORKFLOW_CANCEL_MARKER": str(cancel_path),
            "WORKFLOW_HELPER_DIR": str(self.config.helper_dir),
            "WORKFLOW_ALLOWED_FILE_ROOT": str(self.config.allowed_file_root),
        }
        request = self._job_request(
            workflow,
            generation=0,
            kind="master",
            script=self.scripts["master"],
            environment=environment,
            slurm_options=specification["slurm"],
        )
        self._submit_intent(snapshot, 0, "master", request)

    def _submit_array(self, snapshot: dict[str, Any]) -> None:
        workflow = snapshot["workflow"]
        generation = snapshot["generations"][0]
        lock_path, cancel_path = self._control_paths(workflow["workflow_id"])
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "WORKFLOW_ID": workflow["workflow_id"],
            "WORKFLOW_MANIFEST_URI": generation["manifest_uri"],
            "WORKFLOW_MANIFEST_SHA256": generation["manifest_sha256"],
            "WORKFLOW_CANCEL_LOCK": str(lock_path),
            "WORKFLOW_CANCEL_MARKER": str(cancel_path),
            "WORKFLOW_HELPER_DIR": str(self.config.helper_dir),
            "WORKFLOW_ALLOWED_FILE_ROOT": str(self.config.allowed_file_root),
        }
        request = self._job_request(
            workflow,
            generation=0,
            kind="array",
            script=self.scripts["array"],
            environment=environment,
            slurm_options=workflow["request"]["slurm"],
        )
        request["job"]["array"] = (
            f"0-{generation['chunk_count'] - 1}%{generation['max_parallel']}"
        )
        job_id = self._submit_intent(snapshot, 0, "array", request)
        if job_id:
            self._ensure_finalizer(self.store.get_workflow(workflow["workflow_id"]), job_id)

    def _ensure_finalizer(
        self, snapshot: dict[str, Any], array_job_id: str
    ) -> None:
        workflow = snapshot["workflow"]
        if workflow["cancel_requested_at"] is not None:
            return
        generation = snapshot["generations"][0]
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "WORKFLOW_ID": workflow["workflow_id"],
            "WORKFLOW_MANIFEST_URI": generation["manifest_uri"],
            "WORKFLOW_MANIFEST_SHA256": generation["manifest_sha256"],
            "WORKFLOW_SUMMARY_URI": generation["summary_uri"],
            "WORKFLOW_HELPER_DIR": str(self.config.helper_dir),
            "WORKFLOW_ALLOWED_FILE_ROOT": str(self.config.allowed_file_root),
            "WORKFLOW_CANCEL_LOCK": str(
                self._control_paths(workflow["workflow_id"])[0]
            ),
            "WORKFLOW_CANCEL_MARKER": str(
                self._control_paths(workflow["workflow_id"])[1]
            ),
        }
        request = self._job_request(
            workflow,
            generation=0,
            kind="finalizer",
            script=self.scripts["finalizer"],
            environment=environment,
            slurm_options=workflow["request"]["slurm"],
        )
        # afterany is essential: a failed array element must not strand the
        # durable workflow without a summary.
        request["job"]["dependency"] = f"afterany:{array_job_id}"
        self._submit_intent(snapshot, 0, "finalizer", request)

    def _submit_intent(
        self,
        snapshot: dict[str, Any],
        generation: int,
        kind: str,
        request: dict[str, Any],
    ) -> str | None:
        workflow = snapshot["workflow"]
        marker = request["job"]["comment"]
        intent_id = str(
            uuid.uuid5(
                uuid.UUID(workflow["workflow_id"]), f"{generation}:{kind}:{marker}"
            )
        )
        intent = self.store.prepare_intent(
            intent_id=intent_id,
            workflow_id=workflow["workflow_id"],
            generation=generation,
            kind=kind,
            marker=marker,
            job_name=request["job"]["name"],
            request=request,
        )
        if intent["state"] == "SUBMITTED":
            return intent["slurm_job_id"]
        if intent["state"] != "PREPARED":
            return None
        attempt = self.store.start_intent_attempt(intent_id)
        if not attempt.pop("_claimed", False):
            return None
        try:
            job_id = self.slurm.submit_job(
                request,
                identity=self.config.slurm_submit_user,
                correlation_id=workflow["correlation_id"],
            )
        except SlurmRejected as error:
            self.store.mark_intent(
                intent_id,
                state="REJECTED",
                error=_safe_error(error),
                workflow_status="FAILED",
                generation_status="FAILED" if kind != "master" else None,
            )
            if kind == "master":
                self.capabilities.remove(workflow["workflow_id"])
            return None
        except SlurmAmbiguous as error:
            self.store.mark_intent(
                intent_id,
                state="AMBIGUOUS",
                error=_safe_error(error),
                workflow_status=self._unknown_status(kind),
                generation_status=(self._unknown_status(kind) if kind != "master" else None),
            )
            return None
        self._record_submission(attempt, job_id)
        return job_id

    def _record_submission(self, intent: dict[str, Any], job_id: str) -> None:
        kind = intent["kind"]
        workflow_status = {
            "master": "MASTER_SUBMITTED",
            "array": "ARRAY_SUBMITTED",
            "finalizer": "RUNNING",
        }[kind]
        generation_status = {
            "array": "ARRAY_SUBMITTED",
            "finalizer": "RUNNING",
        }.get(kind)
        self.store.mark_intent(
            intent["intent_id"],
            state="SUBMITTED",
            slurm_job_id=str(job_id),
            workflow_status=workflow_status,
            generation_status=generation_status,
        )

    @staticmethod
    def _unknown_status(kind: str) -> str:
        return {
            "master": "MASTER_SUBMISSION_UNKNOWN",
            "array": "ARRAY_SUBMISSION_UNKNOWN",
            "finalizer": "FINALIZER_SUBMISSION_UNKNOWN",
        }[kind]

    def _job_request(
        self,
        workflow: dict[str, Any],
        *,
        generation: int,
        kind: str,
        script: str,
        environment: dict[str, str],
        slurm_options: dict[str, Any],
    ) -> dict[str, Any]:
        abbreviations = {"master": "m", "array": "a", "finalizer": "f"}
        name = (
            f"wf-{workflow['workflow_id'].replace('-', '')[:12]}-"
            f"{abbreviations[kind]}-g{generation}"
        )
        marker = (
            f"workflow={workflow['workflow_id']};generation={generation};role={kind}"
        )
        log_directory = self._ensure_job_log_directory(workflow["workflow_id"])
        log_names = {
            "master": (f"master-g{generation}-%j.out", f"master-g{generation}-%j.err"),
            # %A is the array's parent job ID and %a is the task index.  Using
            # both is necessary: a single shared array path would interleave
            # concurrent workers and make an individual task unauditable.
            "array": (
                f"worker-g{generation}-%A_%a.out",
                f"worker-g{generation}-%A_%a.err",
            ),
            "finalizer": (
                f"finalizer-g{generation}-%j.out",
                f"finalizer-g{generation}-%j.err",
            ),
        }
        standard_output, standard_error = log_names[kind]
        # Apply policy-validated resource options first.  Broker-controlled
        # identity, environment, working directory, and log paths then win
        # even if a future option parser accidentally admits a colliding key.
        job: dict[str, Any] = dict(slurm_options)
        job.update({
            "name": name,
            "comment": marker,
            # slurmrestd otherwise leaves Slurm to use a relative
            # ``slurm-%j.out`` path.  The broker deliberately sets cwd to `/`,
            # which is not writable by an unprivileged job, so every job must
            # receive explicit absolute paths on storage shared by controller
            # and compute nodes.  These paths are broker-derived rather than
            # caller-controlled to prevent arbitrary file writes.
            "standard_output": str(log_directory / standard_output),
            "standard_error": str(log_directory / standard_error),
            "current_working_directory": "/",
            "environment": [f"{key}={value}" for key, value in sorted(environment.items())],
        })
        return {"script": script, "job": job}

    def _ensure_job_log_directory(self, workflow_id: str) -> Path:
        """Create and verify a private shared log directory without symlink races.

        Jobs and the broker intentionally run as the same narrow service UID in
        the current deployment model.  Mode 0700 therefore gives the jobs write
        access while denying unrelated host and container identities.  openat
        operations anchored at the already-validated control directory prevent
        a pre-created symlink from redirecting Slurm output outside that trust
        boundary.
        """

        self._validate_workflow_id(workflow_id)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW

        control_descriptor = os.open(self.config.control_dir, directory_flags)
        try:
            log_root_descriptor = self._open_private_directory(
                control_descriptor, "job-logs", directory_flags
            )
            try:
                workflow_descriptor = self._open_private_directory(
                    log_root_descriptor, workflow_id, directory_flags
                )
                os.close(workflow_descriptor)
            finally:
                os.close(log_root_descriptor)
        finally:
            os.close(control_descriptor)
        return self.config.control_dir / "job-logs" / workflow_id

    @staticmethod
    def _open_private_directory(
        parent_descriptor: int, name: str, directory_flags: int
    ) -> int:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        descriptor = os.open(
            name, directory_flags, dir_fd=parent_descriptor
        )
        info = os.fstat(descriptor)
        # Never repair an unexpectedly owned or permissive path in place: it
        # could contain another principal's data.  Failing closed leaves an
        # operator-visible configuration error without overwriting anything.
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(descriptor)
            raise ValueError(
                "workflow job log directories must be owned by the broker "
                "and have mode 0700"
            )
        return descriptor

    def _remove_empty_job_log_directory(self, workflow_id: str) -> None:
        """Remove only a newly-created, still-empty losing workflow directory."""

        self._validate_workflow_id(workflow_id)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        try:
            control_descriptor = os.open(self.config.control_dir, directory_flags)
            try:
                log_root_descriptor = os.open(
                    "job-logs",
                    directory_flags,
                    dir_fd=control_descriptor,
                )
                try:
                    # rmdir cannot remove files or a non-empty directory.  It
                    # is deliberately safer than recursive cleanup here.
                    os.rmdir(workflow_id, dir_fd=log_root_descriptor)
                finally:
                    os.close(log_root_descriptor)
            finally:
                os.close(control_descriptor)
        except FileNotFoundError:
            return

    def _validate_create_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise BadRequest("request body must be an object")
        allowed = {"input_spec_uri", "workspace_uri", "max_parallel", "slurm"}
        unknown = set(request) - allowed
        if unknown:
            raise BadRequest("request contains unsupported fields")
        if "input_spec_uri" not in request or "workspace_uri" not in request:
            raise BadRequest("input_spec_uri and workspace_uri are required")
        input_spec_uri = self._validate_uri(request["input_spec_uri"], "input_spec_uri")
        workspace_uri = self._validate_uri(request["workspace_uri"], "workspace_uri")
        max_parallel = request.get("max_parallel", min(20, self.config.max_parallel))
        if (
            isinstance(max_parallel, bool)
            or not isinstance(max_parallel, int)
            or not 1 <= max_parallel <= self.config.max_parallel
        ):
            raise BadRequest("max_parallel is outside broker policy")
        slurm = request.get("slurm", {})
        if not isinstance(slurm, dict):
            raise BadRequest("slurm must be an object")
        allowed_slurm = {
            "partition",
            "account",
            "qos",
            "time_limit",
            "memory_per_node",
            "cpus_per_task",
        }
        if set(slurm) - allowed_slurm:
            raise BadRequest("unsupported Slurm job option")
        normalized_slurm: dict[str, Any] = {}
        for key in ("partition", "account", "qos"):
            if key in slurm:
                value = slurm[key]
                if not isinstance(value, str) or not SLURM_VALUE.fullmatch(value):
                    raise BadRequest(f"invalid Slurm {key}")
                normalized_slurm[key] = value
        limits = {
            "time_limit": (1, 7 * 24 * 60),
            "memory_per_node": (16, 1024 * 1024),
            "cpus_per_task": (1, 1024),
        }
        for key, (minimum, maximum) in limits.items():
            if key in slurm:
                value = slurm[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not minimum <= value <= maximum
                ):
                    raise BadRequest(f"Slurm {key} is outside broker policy")
                normalized_slurm[key] = value
        return {
            "input_spec_uri": input_spec_uri,
            "workspace_uri": workspace_uri.rstrip("/"),
            "max_parallel": max_parallel,
            "slurm": normalized_slurm,
        }

    def _validate_uri(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or len(value) > 2048 or any(
            character in value for character in "\r\n\x00"
        ):
            raise BadRequest(f"{field} is invalid")
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BadRequest(f"{field} cannot contain credentials/query/fragment")
        if parsed.scheme == "file":
            if parsed.netloc or not parsed.path.startswith("/"):
                raise BadRequest(f"{field} must be a local absolute file URI")
            path = Path(unquote(parsed.path)).resolve(strict=False)
            allowed = self.config.allowed_file_root.resolve(strict=False)
            try:
                path.relative_to(allowed)
            except ValueError as error:
                raise BadRequest(f"{field} is outside the allowed storage root") from error
            return path.as_uri()
        if parsed.scheme == "s3" and parsed.netloc and parsed.path:
            return value.rstrip("/")
        raise BadRequest(f"{field} uses an unsupported URI scheme")

    def _derived_uris(self, workspace_uri: str, workflow_id: str) -> dict[str, str]:
        parsed = urlsplit(workspace_uri)
        if parsed.scheme == "file":
            root = Path(unquote(parsed.path)) / workflow_id
            return {
                "manifest_uri": (root / "manifest.json").as_uri(),
                "output_root_uri": (root / "results").as_uri(),
                "summary_uri": (root / "summary.json").as_uri(),
            }
        path = parsed.path.rstrip("/") + "/" + quote(workflow_id, safe="")
        base = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        return {
            "manifest_uri": base + "/manifest.json",
            "output_root_uri": base + "/results",
            "summary_uri": base + "/summary.json",
        }

    def _refresh_from_summary(self, workflow_id: str) -> None:
        snapshot = self.store.get_workflow(workflow_id)
        workflow = snapshot["workflow"]
        if workflow["status"] in {"CANCEL_REQUESTED", "CANCELLED", "FAILED"}:
            return
        if not snapshot["generations"]:
            return
        summary_uri = snapshot["generations"][-1]["summary_uri"]
        parsed = urlsplit(summary_uri)
        if parsed.scheme != "file":
            return
        path = Path(unquote(parsed.path))
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("summary too large")
            summary = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, json.JSONDecodeError):
            self.store.set_workflow_status(
                workflow_id, "NEEDS_ATTENTION", "finalizer summary is invalid"
            )
            return
        if not isinstance(summary, dict) or summary.get("workflow_id") != workflow_id:
            self.store.set_workflow_status(
                workflow_id, "NEEDS_ATTENTION", "finalizer summary identity mismatch"
            )
            return
        if summary.get("status") == "SUCCEEDED":
            self.store.set_workflow_status(workflow_id, "SUCCEEDED")
        elif summary.get("status") == "INCOMPLETE":
            self.store.set_workflow_status(workflow_id, "FAILED", "one or more chunks failed")

    def _control_paths(self, workflow_id: str) -> tuple[Path, Path]:
        return (
            self.config.control_dir / f"{workflow_id}.lock",
            self.config.control_dir / f"{workflow_id}.cancelled",
        )

    @staticmethod
    def _create_cancel_marker(path: Path, workflow_id: str) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o440)
        except FileExistsError:
            return
        try:
            data = (workflow_id + "\n").encode("ascii")
            if os.write(descriptor, data) != len(data):
                raise OSError("short cancellation marker write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _validate_actor(tenant: str, owner: str) -> None:
        if not IDENTIFIER.fullmatch(tenant) or not IDENTIFIER.fullmatch(owner):
            raise BadRequest("authenticated tenant or owner has an invalid format")

    @staticmethod
    def _validate_workflow_id(workflow_id: str) -> None:
        try:
            parsed = uuid.UUID(workflow_id)
        except (ValueError, AttributeError) as error:
            raise BadRequest("workflow identifier is invalid") from error
        if str(parsed) != workflow_id:
            raise BadRequest("workflow identifier is invalid")
