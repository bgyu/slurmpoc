"""Capability and filesystem primitives.

Raw callback capabilities intentionally never enter SQLite.  A random value is
written once to a private file, while only a domain-separated digest is kept in
the durable record.  This makes a copied database insufficient to dispatch a
workflow.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat

from .errors import ServiceUnavailable


def new_capability() -> str:
    # 256 random bits makes the unsalted database digest safe against offline
    # guessing; the prefix also makes accidental secret classification easier.
    return "wfc_" + secrets.token_urlsafe(32)


def capability_digest(token: str) -> str:
    return hashlib.sha256(
        b"slurmpoc-workflow-capability-v1\x00" + token.encode("ascii")
    ).hexdigest()


def read_secret_file(
    path: Path | None, *, purpose: str, maximum_bytes: int = 16384
) -> str:
    """Read a rotating runtime secret without accepting unsafe file types.

    The broker deliberately reloads authentication files for every request so
    a secret manager can replace them atomically without restarting the
    service.  Environment variables and command-line options are unsuitable:
    they are routinely exposed by process inspection and support bundles.
    """

    if path is None:
        raise ServiceUnavailable(f"{purpose} secret is not configured")
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ServiceUnavailable(f"{purpose} secret is not a regular file")
        if info.st_mode & 0o077:
            raise ServiceUnavailable(f"{purpose} secret permissions are too broad")
        if not 1 <= info.st_size <= maximum_bytes:
            raise ServiceUnavailable(f"{purpose} secret has an invalid size")
        value = path.read_text(encoding="ascii").strip()
    except ServiceUnavailable:
        raise
    except (OSError, UnicodeError) as exc:
        raise ServiceUnavailable(f"{purpose} secret cannot be read") from exc
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ServiceUnavailable(f"{purpose} secret is invalid")
    return value


def secret_matches(candidate: str, expected_file: Path | None, *, purpose: str) -> bool:
    """Constant-time comparison for file-backed bearer credentials."""

    expected = read_secret_file(expected_file, purpose=purpose)
    # Reject absurd header sizes before compare_digest allocates/copies more
    # attacker-controlled data.  The server also imposes a total header limit.
    if not candidate or len(candidate) > 16384:
        return False
    return hmac.compare_digest(candidate, expected)


class CapabilityFiles:
    def __init__(self, private_dir: Path, job_visible_dir: Path) -> None:
        self.private_dir = private_dir
        self.job_visible_dir = job_visible_dir
        private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = private_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("capability directory must be a real directory")
        if info.st_mode & 0o077:
            raise ValueError("capability directory must not be group/world accessible")

    def private_path(self, workflow_id: str) -> Path:
        return self.private_dir / f"{workflow_id}.cap"

    def job_path(self, workflow_id: str) -> Path:
        return self.job_visible_dir / f"{workflow_id}.cap"

    def stage(self, workflow_id: str, token: str) -> Path:
        path = self.private_path(workflow_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o400)
        try:
            data = (token + "\n").encode("ascii")
            written = os.write(descriptor, data)
            if written != len(data):
                raise OSError("short capability write")
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
        finally:
            os.close(descriptor)
        self._fsync_directory()
        return self.job_path(workflow_id)

    def remove(self, workflow_id: str) -> None:
        try:
            self.private_path(workflow_id).unlink()
        except FileNotFoundError:
            return
        self._fsync_directory()

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.private_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
