#!/usr/bin/env python3
"""Demo master: create an immutable chunk manifest, then request one array.

The program deliberately receives no Slurm JWT.  Its only credential is the
short-lived, one-workflow callback capability held in a mode-0400 file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

from common import (
    atomic_commit,
    canonical_json,
    file_uri_path,
    read_json,
    required_environment,
    sha256_hex,
)


CHUNK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _workflow_id() -> str:
    value = required_environment("WORKFLOW_ID")
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("WORKFLOW_ID is not a canonical UUID")
    return value


def _capability(path_text: str) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        raise ValueError("WORKFLOW_CAPABILITY_FILE must be absolute")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("workflow capability must be a regular non-symlink")
    if info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_size > 512:
        raise ValueError("workflow capability has unsafe ownership, mode, or size")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        value = os.read(descriptor, 513).decode("ascii").strip()
    finally:
        os.close(descriptor)
    if (
        not value.startswith("wfc_")
        or len(value) > 256
        or "\r" in value
        or "\n" in value
        or "\x00" in value
    ):
        raise ValueError("workflow capability file is invalid")
    return value


def _validated_chunks(specification: dict[str, object]) -> list[dict[str, object]]:
    if set(specification) != {"schema_version", "chunks"}:
        raise ValueError("input specification has unsupported fields")
    if specification["schema_version"] != 1:
        raise ValueError("input specification schema_version must be 1")
    chunks = specification["chunks"]
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= 1_000_000:
        raise ValueError("input specification must contain 1..1000000 chunks")
    normalized: list[dict[str, object]] = []
    identifiers: set[str] = set()
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {"chunk_id", "value"}:
            raise ValueError("each demo chunk requires only chunk_id and value")
        chunk_id = chunk["chunk_id"]
        value = chunk["value"]
        if not isinstance(chunk_id, str) or not CHUNK_ID.fullmatch(chunk_id):
            raise ValueError("chunk_id is invalid")
        if chunk_id in identifiers:
            raise ValueError(f"duplicate chunk_id: {chunk_id}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"chunk {chunk_id} value must be an integer")
        if not -(2**31) <= value < 2**31:
            raise ValueError(f"chunk {chunk_id} value is outside the demo limit")
        identifiers.add(chunk_id)
        normalized.append({"chunk_id": chunk_id, "value": value})
    return normalized


def _dispatch(
    broker_url: str,
    workflow_id: str,
    capability: str,
    body: bytes,
) -> None:
    parsed = urlsplit(broker_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("WORKFLOW_BROKER_URL is invalid")
    endpoint = broker_url.rstrip("/") + f"/v1/workflows/{workflow_id}/dispatch"
    # A transport failure may occur after the broker commits.  Repeating the
    # same body and capability is safe: the ledger recognizes an identical
    # replay and the array intent has its own unique key.
    for attempt in range(5):
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Correlation-ID": f"master-{workflow_id}",
                "X-Workflow-Capability": capability,
            },
        )
        try:
            with urlopen(request, timeout=15) as response:  # noqa: S310 - validated URL
                response.read(1024 * 1024 + 1)
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"broker returned HTTP {response.status}")
                return
        except HTTPError as error:
            # 4xx is definitive policy/authentication failure.  A 5xx may have
            # followed a committed mutation, so only that class is retried.
            error.read(1024 * 1024 + 1)
            if error.code < 500 or attempt == 4:
                raise RuntimeError(f"broker rejected dispatch ({error.code})") from error
        except (URLError, TimeoutError, OSError) as error:
            if attempt == 4:
                raise RuntimeError("broker dispatch transport failed") from error
        time.sleep(min(2**attempt, 8))


def main() -> None:
    workflow_id = _workflow_id()
    input_path = file_uri_path(required_environment("WORKFLOW_INPUT_SPEC_URI"))
    manifest_path = file_uri_path(required_environment("WORKFLOW_MANIFEST_URI"))
    output_root = file_uri_path(required_environment("WORKFLOW_OUTPUT_ROOT_URI"))
    specification = read_json(input_path, maximum_bytes=64 * 1024 * 1024)
    source_chunks = _validated_chunks(specification)

    chunks: list[dict[str, object]] = []
    for source in source_chunks:
        chunk_id = str(source["chunk_id"])
        chunks.append(
            {
                "chunk_id": chunk_id,
                "value": source["value"],
                "output_uri": (output_root / f"{chunk_id}.json").as_uri(),
            }
        )
    manifest = {
        "schema_version": 1,
        "algorithm": "integer-square-v1",
        "workflow_id": workflow_id,
        "chunks": chunks,
    }
    manifest_bytes = canonical_json(manifest)
    atomic_commit(manifest_path, manifest_bytes)

    dispatch_bytes = canonical_json(
        {
            "manifest_uri": manifest_path.as_uri(),
            "manifest_sha256": sha256_hex(manifest_bytes),
            "chunk_count": len(chunks),
        }
    )
    capability_path = required_environment("WORKFLOW_CAPABILITY_FILE")
    capability = _capability(capability_path)
    _dispatch(
        required_environment("WORKFLOW_BROKER_URL"),
        workflow_id,
        capability,
        dispatch_bytes,
    )
    # The broker removes its source after atomic consumption.  This unlink is
    # still useful for node-agent/tmpfs delivery modes where the job owns a
    # private copy.
    try:
        Path(capability_path).unlink()
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()
