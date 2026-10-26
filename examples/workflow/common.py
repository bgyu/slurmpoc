"""Filesystem and JSON primitives shared by the intentionally simple demo jobs.

The demo uses ``file://`` URIs so it can run without a cloud SDK.  Production
applications can replace these helpers with an S3 adapter while retaining the
manifest contract.  Every commit happens in the target directory and uses a
hard-link create, providing atomic no-overwrite semantics on a normal local or
shared POSIX filesystem.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit
import uuid


class Cancelled(RuntimeError):
    """Raised when the broker's durable cancellation marker is visible."""


CHUNK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"required environment variable {name} is missing or invalid")
    return value


def allowed_root() -> Path:
    root = Path(required_environment("WORKFLOW_ALLOWED_FILE_ROOT"))
    if not root.is_absolute():
        raise ValueError("WORKFLOW_ALLOWED_FILE_ROOT must be absolute")
    return root.resolve(strict=True)


def checked_path(path: Path) -> Path:
    """Normalize a path, prove containment, and reject symlink traversal."""

    if not path.is_absolute():
        raise ValueError("workflow file path must be absolute")
    root = allowed_root()
    # abspath removes ``..`` lexically without following attacker-controlled
    # links.  We then inspect every existing component with lstat.
    normalized = Path(os.path.abspath(path))
    try:
        relative = normalized.relative_to(root)
    except ValueError as exc:
        raise ValueError("workflow file is outside the allowed storage root") from exc
    current = root
    for component in relative.parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"workflow path traverses a symbolic link: {current}")
    return normalized


def file_uri_path(uri: str) -> Path:
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "the bundled demo accepts only absolute file:// URIs; use an "
            "application storage adapter for S3"
        )
    return checked_path(Path(unquote(parsed.path)))


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_workflow_id(value: str) -> str:
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("workflow identifier is not a canonical UUID")
    return value


def read_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    path = checked_path(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"workflow input is not a regular file: {path}")
    if info.st_size > maximum_bytes:
        raise ValueError(f"workflow input exceeds {maximum_bytes} bytes")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > maximum_bytes:
            raise ValueError(f"workflow input exceeds {maximum_bytes} bytes")
        return value
    finally:
        os.close(descriptor)


def read_json(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    try:
        value = json.loads(read_bytes(path, maximum_bytes=maximum_bytes))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"workflow JSON is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"workflow JSON must be an object: {path}")
    return value


def load_manifest(
    path: Path, *, expected_sha256: str, expected_workflow_id: str
) -> tuple[dict[str, Any], bytes]:
    """Load and fully validate the immutable demo manifest."""

    if not SHA256.fullmatch(expected_sha256):
        raise ValueError("manifest SHA-256 is invalid")
    raw = read_bytes(path, maximum_bytes=64 * 1024 * 1024)
    if sha256_hex(raw) != expected_sha256:
        raise ValueError("manifest SHA-256 does not match")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest JSON is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "algorithm",
        "workflow_id",
        "chunks",
    }:
        raise ValueError("manifest has an invalid shape")
    if (
        manifest["schema_version"] != 1
        or manifest["algorithm"] != "integer-square-v1"
        or manifest["workflow_id"] != expected_workflow_id
    ):
        raise ValueError("manifest identity or version is invalid")
    chunks = manifest["chunks"]
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= 1_000_000:
        raise ValueError("manifest chunk count is invalid")
    seen: set[str] = set()
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {
            "chunk_id",
            "value",
            "output_uri",
        }:
            raise ValueError("manifest chunk has an invalid shape")
        chunk_id = chunk["chunk_id"]
        value = chunk["value"]
        output_uri = chunk["output_uri"]
        if (
            not isinstance(chunk_id, str)
            or not CHUNK_ID.fullmatch(chunk_id)
            or chunk_id in seen
        ):
            raise ValueError("manifest chunk identifier is invalid or duplicated")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("manifest chunk value is not an integer")
        if not isinstance(output_uri, str):
            raise ValueError("manifest chunk output_uri is invalid")
        # Resolve it now so every task applies the same storage-root policy.
        file_uri_path(output_uri)
        seen.add(chunk_id)
    return manifest, raw


def _existing_matches(path: Path, expected: bytes) -> bool:
    try:
        existing = read_bytes(path, maximum_bytes=max(len(expected), 1))
    except FileNotFoundError:
        return False
    return existing == expected


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_commit(path: Path, value: bytes) -> bool:
    """Create ``path`` atomically and never replace a different result.

    Returns ``True`` for a new commit and ``False`` for an idempotent replay of
    the exact bytes.  A conflicting pre-existing result is treated as data
    corruption instead of being silently overwritten.
    """

    path = checked_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = checked_path(path)  # re-check after creating parents
    if _existing_matches(path, value):
        return False
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to replace a different result: {path}")

    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short workflow result write")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)

    try:
        # link(2) fails if the destination exists, unlike replace(2).  This is
        # the filesystem equivalent of a conditional object-store PUT.
        os.link(temporary, path, follow_symlinks=False)
        _fsync_directory(path.parent)
        return True
    except FileExistsError:
        if _existing_matches(path, value):
            return False
        raise FileExistsError(f"concurrent writer committed different data: {path}")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def cancellation_guard(lock_path: Path, marker_path: Path) -> Iterator[None]:
    """Serialize the final cancellation check with result commit.

    The API takes this lock exclusively before it commits CANCEL_REQUESTED and
    creates the marker.  Workers/finalizers take it shared immediately before
    committing.  Therefore no output can be committed *after* cancellation is
    durable, even when an API call races a finishing worker.
    """

    lock_path = checked_path(lock_path)
    marker_path = checked_path(marker_path)
    lock_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o640)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        if marker_path.exists() or marker_path.is_symlink():
            raise Cancelled("workflow cancellation was requested")
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
