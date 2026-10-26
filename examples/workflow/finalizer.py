#!/usr/bin/env python3
"""After-any finalizer for the demo worker array.

The finalizer does not infer success from its dependency.  ``afterany`` runs
after both successful and failed array elements, so it validates every
deterministic output and writes one immutable summary for the broker to read.
"""

from __future__ import annotations

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
    read_bytes,
    required_environment,
)


def _stop(_signum: int, _frame: object) -> None:
    raise Cancelled("finalizer received a termination signal")


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    workflow_id = canonical_workflow_id(required_environment("WORKFLOW_ID"))
    manifest, _raw = load_manifest(
        file_uri_path(required_environment("WORKFLOW_MANIFEST_URI")),
        expected_sha256=required_environment("WORKFLOW_MANIFEST_SHA256"),
        expected_workflow_id=workflow_id,
    )
    completed: list[str] = []
    incomplete: list[str] = []
    for chunk in manifest["chunks"]:
        expected = canonical_json(
            {
                "schema_version": 1,
                "algorithm": "integer-square-v1",
                "workflow_id": workflow_id,
                "chunk_id": chunk["chunk_id"],
                "input": chunk["value"],
                "result": chunk["value"] * chunk["value"],
            }
        )
        try:
            actual = read_bytes(
                file_uri_path(chunk["output_uri"]),
                maximum_bytes=max(len(expected), 1),
            )
        except (FileNotFoundError, OSError, ValueError):
            incomplete.append(chunk["chunk_id"])
            continue
        if actual == expected:
            completed.append(chunk["chunk_id"])
        else:
            incomplete.append(chunk["chunk_id"])

    summary = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "status": "SUCCEEDED" if not incomplete else "INCOMPLETE",
        "expected_chunks": len(manifest["chunks"]),
        "completed_chunks": len(completed),
        "incomplete_chunk_ids": incomplete,
    }
    lock_path = Path(required_environment("WORKFLOW_CANCEL_LOCK"))
    marker_path = Path(required_environment("WORKFLOW_CANCEL_MARKER"))
    with cancellation_guard(lock_path, marker_path):
        atomic_commit(
            file_uri_path(required_environment("WORKFLOW_SUMMARY_URI")),
            canonical_json(summary),
        )
    print(f"workflow={workflow_id} status={summary['status']}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Cancelled as error:
        print(str(error), flush=True)
        raise SystemExit(75) from error
