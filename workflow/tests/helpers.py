from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from workflow.config import BrokerConfig


class Workspace:
    """Own a disposable, permission-correct broker filesystem layout."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.shared = self.root / "shared"
        self.shared.mkdir(mode=0o700)
        self.capabilities = self.root / "capabilities"
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        for name in ("master", "worker", "finalizer"):
            (self.scripts / f"{name}.sbatch").write_text(
                f"#!/bin/bash\n# {name}\n", encoding="utf-8"
            )

    def config(self, **overrides: Any) -> BrokerConfig:
        values: dict[str, Any] = {
            "database_path": self.root / "workflow.sqlite3",
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "public_base_url": "http://127.0.0.1:8088",
            "capability_dir": self.capabilities,
            "capability_job_dir": self.capabilities,
            "control_dir": self.shared / ".workflow-control",
            "allowed_file_root": self.shared,
            "master_script": self.scripts / "master.sbatch",
            "worker_script": self.scripts / "worker.sbatch",
            "finalizer_script": self.scripts / "finalizer.sbatch",
            "helper_dir": self.scripts,
            "slurm_rest_url": "http://slurmrestd:6820",
            "slurm_token_file": self.root / "slurm.jwt",
            "allow_insecure_slurm_http": True,
            "api_token_file": self.root / "api.token",
        }
        values.update(overrides)
        return BrokerConfig(**values)

    def secret(self, name: str, value: str) -> Path:
        path = self.root / name
        path.write_text(value + "\n", encoding="ascii")
        path.chmod(0o600)
        return path

    def close(self) -> None:
        self.temporary.cleanup()


class FakeSlurm:
    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, Any], str, str]] = []
        self.cancelled: list[str] = []
        self.markers: dict[str, list[str]] = {}
        self.next_job_id = 100
        self.submit_error: BaseException | None = None

    def submit_job(
        self, request: dict[str, Any], *, identity: str, correlation_id: str
    ) -> str:
        self.requests.append((request, identity, correlation_id))
        job_id = str(self.next_job_id)
        self.next_job_id += 1
        marker = request["job"]["comment"]
        if self.submit_error is not None:
            error = self.submit_error
            self.submit_error = None
            raise error
        self.markers.setdefault(marker, []).append(job_id)
        return job_id

    def cancel_job(
        self, job_id: str, *, identity: str, correlation_id: str
    ) -> None:
        self.cancelled.append(str(job_id))

    def find_jobs_by_marker(
        self, marker: str, *, identity: str, correlation_id: str
    ) -> list[str]:
        return list(self.markers.get(marker, []))
