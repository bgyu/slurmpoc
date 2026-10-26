#!/usr/bin/env python3
"""One idempotent demo array task: square the integer in its manifest entry."""

from __future__ import annotations

import os
from pathlib import Path
import signal

from common import (
    Cancelled,
    atomic_commit,
    cancellation_guard,
    canonical_json,
    canonical_workflow_id,
    file_uri_path,
    load_manifest,
    required_environment,
)


def _stop(_signum: int, _frame: object) -> None:
    # Raising before/during the shared cancellation section prevents the task
    # from committing output after scancel.  The API marker/lock handles the
    # stronger race where cancellation and commit happen concurrently.
    raise Cancelled("worker received a termination signal")


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    workflow_id = canonical_workflow_id(required_environment("WORKFLOW_ID"))
    try:
        index = int(required_environment("SLURM_ARRAY_TASK_ID"), 10)
    except ValueError as exc:
        raise ValueError("SLURM_ARRAY_TASK_ID is invalid") from exc
    if index < 0:
        raise ValueError("SLURM_ARRAY_TASK_ID must be non-negative")

    manifest, _raw = load_manifest(
        file_uri_path(required_environment("WORKFLOW_MANIFEST_URI")),
        expected_sha256=required_environment("WORKFLOW_MANIFEST_SHA256"),
        expected_workflow_id=workflow_id,
    )
    chunks = manifest["chunks"]
    if index >= len(chunks):
        raise ValueError("SLURM_ARRAY_TASK_ID is outside the manifest")
    chunk = chunks[index]
    value = chunk["value"]
    output = {
        "schema_version": 1,
        "algorithm": "integer-square-v1",
        "workflow_id": workflow_id,
        "chunk_id": chunk["chunk_id"],
        "input": value,
        "result": value * value,
    }
    lock_path = Path(required_environment("WORKFLOW_CANCEL_LOCK"))
    marker_path = Path(required_environment("WORKFLOW_CANCEL_MARKER"))
    with cancellation_guard(lock_path, marker_path):
        created = atomic_commit(
            file_uri_path(chunk["output_uri"]), canonical_json(output)
        )
    print(
        f"chunk={chunk['chunk_id']} result={'committed' if created else 'already-committed'}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Cancelled as error:
        print(str(error), flush=True)
        raise SystemExit(75) from error
