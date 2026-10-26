#!/usr/bin/env python3
"""Submit and optionally verify the local integer-square workflow demo.

This is deliberately a local integration client, not a production identity
client.  A production caller obtains an OIDC access token from its identity
provider and talks to the TLS gateway.  Here we read the development-only
bearer from its mode-0600 file and connect only to a loopback-bound broker.

The credential is never passed to ``podman``, a child-process environment, or
an argument vector.  It exists only in this process and in the HTTP header sent
over loopback.  That distinction matters on multi-user systems, where command
arguments and environments can be exposed by diagnostics or audit tooling.
"""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any
import uuid


MAX_API_RESPONSE_BYTES = 1024 * 1024
MAX_CHUNKS = 10_000
TERMINAL_FAILURES = {"CANCELLED", "FAILED", "NEEDS_ATTENTION"}
TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ClientError(RuntimeError):
    """Expected failure that is safe to show without a traceback."""


class WaitTimeout(ClientError):
    """The workflow did not reach a terminal state within the caller's limit."""


# This helper executes inside the controller container.  It receives the demo
# JSON over stdin, so neither data nor credentials are encoded in a shell
# command.  O_EXCL plus a same-directory hard-link commit prevents accidental
# overwrite and avoids exposing a partially written input file.
STAGE_PROGRAM = r"""
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import uuid

MAXIMUM = 16 * 1024 * 1024
target = Path(sys.argv[1])
allowed_parent = Path('/shared/demo-inputs')
if target.parent != allowed_parent or not re.fullmatch(
    r'demo-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json',
    target.name,
):
    raise SystemExit('unsafe demo input path')

workflow = pwd.getpwnam('workflow')
workflow_uid = workflow.pw_uid
workflow_gid = workflow.pw_gid
if (workflow_uid, workflow_gid) != (64032, 64032):
    raise SystemExit('unexpected workflow identity')

shared = Path('/shared')
shared_info = shared.lstat()
if not stat.S_ISDIR(shared_info.st_mode) or stat.S_ISLNK(shared_info.st_mode):
    raise SystemExit('/shared is not a real directory')
if shared_info.st_mode & stat.S_IWOTH:
    raise SystemExit('/shared must not be world writable')

def prepare_directory(path):
    try:
        path.mkdir(mode=0o750)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit(f'{path} is not a real directory')
    if info.st_uid not in {0, workflow_uid} or info.st_mode & stat.S_IWOTH:
        raise SystemExit(f'{path} has unsafe ownership or permissions')
    os.chown(path, workflow_uid, workflow_gid)
    os.chmod(path, 0o750)

prepare_directory(allowed_parent)
prepare_directory(Path('/shared/workflows'))
if target.exists() or target.is_symlink():
    raise SystemExit('demo input already exists')

raw = sys.stdin.buffer.read(MAXIMUM + 1)
if not raw or len(raw) > MAXIMUM:
    raise SystemExit('demo input is empty or too large')
try:
    document = json.loads(raw)
except (UnicodeDecodeError, json.JSONDecodeError) as error:
    raise SystemExit('demo input is not JSON') from error
if not isinstance(document, dict) or set(document) != {'schema_version', 'chunks'}:
    raise SystemExit('demo input schema is invalid')

temporary = allowed_parent / f'.stage-{uuid.uuid4()}'
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, 'O_NOFOLLOW'):
    flags |= os.O_NOFOLLOW
descriptor = os.open(temporary, flags, 0o640)
try:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError('short write while staging demo input')
        view = view[written:]
    os.fchown(descriptor, workflow_uid, workflow_gid)
    os.fchmod(descriptor, 0o640)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
try:
    # link() fails if target appeared after the earlier check; unlike rename(),
    # it never replaces another submission's input.
    os.link(temporary, target, follow_symlinks=False)
finally:
    temporary.unlink(missing_ok=True)
directory = os.open(allowed_parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
"""


# Verification also happens inside the controller because the shared-data
# named volume is intentionally not bind-mounted onto the WSL host.  Every
# expected artifact is opened with O_NOFOLLOW and a size ceiling before its
# content is trusted.
VALIDATE_PROGRAM = r"""
import json
import os
from pathlib import Path
import stat
import sys
import uuid

workflow_id = sys.argv[1]
chunk_count = int(sys.argv[2])
if str(uuid.UUID(workflow_id)) != workflow_id or not 1 <= chunk_count <= 10000:
    raise SystemExit('invalid validation arguments')
root = Path('/shared/workflows') / workflow_id

for directory in (Path('/shared'), Path('/shared/workflows'), root, root / 'results'):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit(f'unsafe workflow directory: {directory}')
    if info.st_mode & stat.S_IWOTH:
        raise SystemExit(f'world-writable workflow directory: {directory}')

def read_json(path, maximum=1024 * 1024):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit(f'unsafe workflow artifact: {path}')
    if info.st_size <= 0 or info.st_size > maximum:
        raise SystemExit(f'workflow artifact has an unsafe size: {path}')
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        raw = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if len(raw) > maximum:
        raise SystemExit(f'workflow artifact exceeds limit: {path}')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f'workflow artifact is not JSON: {path}') from error
    if not isinstance(value, dict):
        raise SystemExit(f'workflow artifact is not an object: {path}')
    return value

manifest = read_json(root / 'manifest.json', maximum=16 * 1024 * 1024)
chunks = manifest.get('chunks')
if (
    manifest.get('schema_version') != 1
    or manifest.get('algorithm') != 'integer-square-v1'
    or manifest.get('workflow_id') != workflow_id
    or not isinstance(chunks, list)
    or len(chunks) != chunk_count
):
    raise SystemExit('manifest identity or chunk count is invalid')

for index, chunk in enumerate(chunks, start=1):
    chunk_id = f'chunk-{index:06d}'
    expected_uri = (root / 'results' / f'{chunk_id}.json').as_uri()
    if chunk != {'chunk_id': chunk_id, 'value': index, 'output_uri': expected_uri}:
        raise SystemExit(f'manifest entry {index} does not match the submitted input')
    output = read_json(root / 'results' / f'{chunk_id}.json')
    expected_output = {
        'schema_version': 1,
        'algorithm': 'integer-square-v1',
        'workflow_id': workflow_id,
        'chunk_id': chunk_id,
        'input': index,
        'result': index * index,
    }
    if output != expected_output:
        raise SystemExit(f'output validation failed for {chunk_id}')

summary = read_json(root / 'summary.json')
expected_summary = {
    'schema_version': 1,
    'workflow_id': workflow_id,
    'status': 'SUCCEEDED',
    'expected_chunks': chunk_count,
    'completed_chunks': chunk_count,
    'incomplete_chunk_ids': [],
}
if summary != expected_summary:
    raise SystemExit('workflow summary validation failed')
print(json.dumps(summary, sort_keys=True, separators=(',', ':')))
"""


def bounded_integer(name: str, minimum: int, maximum: int):
    """Return an argparse converter with a clear, policy-aware error."""

    def parse(value: str) -> int:
        try:
            parsed = int(value, 10)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from error
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit the protected local Slurm workflow demo.",
        epilog=(
            "The broker must already be running. Without --wait, the command "
            "returns after the durable master-job submission."
        ),
    )
    parser.add_argument(
        "--chunks",
        type=bounded_integer("chunks", 1, MAX_CHUNKS),
        default=20,
        help=f"number of integer chunks (default: 20; maximum: {MAX_CHUNKS})",
    )
    parser.add_argument(
        "--parallel",
        type=bounded_integer("parallel", 1, 100),
        default=5,
        help="maximum concurrent array tasks (default: 5; maximum: 100)",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="poll until terminal state and validate every output artifact",
    )
    parser.add_argument(
        "--timeout",
        type=bounded_integer("timeout", 1, 86_400),
        default=600,
        metavar="SECONDS",
        help="overall --wait deadline in seconds (default: 600)",
    )
    arguments = parser.parse_args(argv)
    if arguments.parallel > arguments.chunks:
        parser.error("--parallel cannot exceed --chunks")
    return arguments


def _validate_private_directory(path: Path, owner: int) -> None:
    """Reject path substitution and shared writable secret directories."""

    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ClientError(f"required secret directory is missing: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ClientError(f"secret path is not a real directory: {path}")
    if (
        info.st_uid != owner
        or info.st_mode & 0o077
        or info.st_mode & 0o700 != 0o700
    ):
        raise ClientError(f"secret directory must be owned by you with mode 0700: {path}")


def read_local_bearer(project_dir: Path) -> str:
    """Read the fixed local token through a no-follow descriptor.

    The path is intentionally not configurable.  Allowing an arbitrary token
    path would make it too easy to use this development client as a confused
    deputy for reading another file.
    """

    runtime_dir = project_dir / ".runtime"
    secret_dir = runtime_dir / "secrets"
    token_path = secret_dir / "broker-api.token"
    owner = os.getuid()
    _validate_private_directory(runtime_dir, owner)
    _validate_private_directory(secret_dir, owner)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(token_path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ClientError("broker API token must not be a symbolic link") from error
        raise ClientError(f"cannot open local broker API token: {token_path}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ClientError("broker API token is not a regular file")
        if info.st_uid != owner or info.st_mode & 0o077:
            raise ClientError("broker API token must be owned by you with mode 0600")
        if not 1 <= info.st_size <= 512:
            raise ClientError("broker API token has an unsafe size")
        raw = os.read(descriptor, 513)
    finally:
        os.close(descriptor)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ClientError("broker API token is not ASCII") from error
    if not TOKEN_PATTERN.fullmatch(token):
        raise ClientError("broker API token has an invalid development format")
    return token


def compose_command(project_dir: Path, *arguments: str) -> list[str]:
    """Build one shell-free Compose command using only repository paths."""

    compose_file = project_dir / "compose.yaml"
    env_file = project_dir / ".runtime" / "cluster.env"
    if not compose_file.is_file() or compose_file.is_symlink():
        raise ClientError(f"Compose file is missing or unsafe: {compose_file}")
    if not env_file.is_file() or env_file.is_symlink():
        raise ClientError(
            "local environment file is missing; run scripts/bootstrap-dev-secrets.sh"
        )
    return [
        "podman",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(compose_file),
        *arguments,
    ]


def _run_container_program(
    project_dir: Path,
    program: str,
    *arguments: str,
    standard_input: bytes | None = None,
    timeout: float = 60.0,
) -> str:
    command = compose_command(
        project_dir,
        "exec",
        "-T",
        "controller",
        "python3",
        "-c",
        program,
        *arguments,
    )
    try:
        result = subprocess.run(
            command,
            cwd=project_dir,
            input=standard_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise ClientError("podman was not found in PATH") from error
    except subprocess.TimeoutExpired as error:
        raise ClientError("timed out while accessing the shared-data volume") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-2000:]
        raise ClientError(
            "controller-side shared-volume operation failed"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.decode("utf-8", errors="strict").strip()


def make_input(chunk_count: int) -> bytes:
    document = {
        "schema_version": 1,
        "chunks": [
            {"chunk_id": f"chunk-{index:06d}", "value": index}
            for index in range(1, chunk_count + 1)
        ],
    }
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def stage_input(project_dir: Path, submission_id: str, chunk_count: int) -> str:
    container_path = f"/shared/demo-inputs/demo-{submission_id}.json"
    _run_container_program(
        project_dir,
        STAGE_PROGRAM,
        container_path,
        standard_input=make_input(chunk_count),
    )
    return "file://" + container_path


def broker_port() -> int:
    value = os.environ.get("WORKFLOW_PORT", "8088")
    try:
        port = int(value, 10)
    except ValueError as error:
        raise ClientError("WORKFLOW_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ClientError("WORKFLOW_PORT is outside 1..65535")
    return port


def _decoded_response(response: http.client.HTTPResponse) -> dict[str, Any]:
    length = response.getheader("Content-Length")
    if length is not None:
        try:
            if int(length, 10) > MAX_API_RESPONSE_BYTES:
                raise ClientError("broker response exceeds the client limit")
        except ValueError as error:
            raise ClientError("broker returned an invalid Content-Length") from error
    raw = response.read(MAX_API_RESPONSE_BYTES + 1)
    if len(raw) > MAX_API_RESPONSE_BYTES:
        raise ClientError("broker response exceeds the client limit")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClientError(f"broker returned non-JSON HTTP {response.status}") from error
    if not isinstance(payload, dict):
        raise ClientError("broker returned a non-object JSON response")
    return payload


def api_request(
    *,
    port: int,
    token: str,
    method: str,
    path: str,
    correlation_id: str,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    attempts: int = 4,
) -> dict[str, Any]:
    """Call only the loopback broker and retry ambiguous transient failures.

    POST retries use the exact same Idempotency-Key and canonical body, so a
    lost response cannot create a second workflow or a second Slurm master.
    """

    encoded = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Authenticated-Tenant": "local-demo",
        "X-Authenticated-User": "local-client",
        "X-Correlation-ID": correlation_id,
    }
    if body is not None:
        encoded = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key

    last_error: BaseException | None = None
    for attempt in range(attempts):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            payload = _decoded_response(response)
            if 200 <= response.status < 300:
                return payload
            message = "request rejected"
            error_body = payload.get("error")
            if isinstance(error_body, dict) and isinstance(
                error_body.get("message"), str
            ):
                message = error_body["message"][:500]
            if response.status < 500:
                raise ClientError(f"broker returned HTTP {response.status}: {message}")
            last_error = ClientError(
                f"broker transiently returned HTTP {response.status}: {message}"
            )
        except ClientError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            last_error = error
        finally:
            connection.close()
        if attempt + 1 < attempts:
            time.sleep(min(0.5 * (2**attempt), 4.0))
    raise ClientError("broker request failed after safe retries") from last_error


def create_workflow(
    *,
    port: int,
    token: str,
    submission_id: str,
    input_uri: str,
    parallel: int,
) -> dict[str, Any]:
    return api_request(
        port=port,
        token=token,
        method="POST",
        path="/v1/workflows",
        correlation_id=f"client-{submission_id}",
        idempotency_key=f"demo-{submission_id}",
        body={
            "input_spec_uri": input_uri,
            "workspace_uri": "file:///shared/workflows",
            "max_parallel": parallel,
            "slurm": {
                "partition": "debug",
                "time_limit": 10,
                "memory_per_node": 128,
                "cpus_per_task": 1,
            },
        },
    )


def _workflow_identity(payload: dict[str, Any]) -> tuple[str, str]:
    workflow_id = payload.get("workflow_id")
    status_value = payload.get("status")
    if not isinstance(workflow_id, str) or not isinstance(status_value, str):
        raise ClientError("broker response lacks workflow_id or status")
    try:
        parsed = uuid.UUID(workflow_id)
    except ValueError as error:
        raise ClientError("broker returned an invalid workflow_id") from error
    if str(parsed) != workflow_id:
        raise ClientError("broker returned a non-canonical workflow_id")
    return workflow_id, status_value


def wait_for_workflow(
    *,
    port: int,
    token: str,
    workflow_id: str,
    timeout: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    previous_status: str | None = None
    while True:
        payload = api_request(
            port=port,
            token=token,
            method="GET",
            path=f"/v1/workflows/{workflow_id}",
            correlation_id=f"poll-{workflow_id}",
        )
        returned_id, status_value = _workflow_identity(payload)
        if returned_id != workflow_id:
            raise ClientError("broker status response changed workflow identity")
        if status_value != previous_status:
            print(f"workflow {workflow_id}: {status_value}", flush=True)
            previous_status = status_value
        if status_value == "SUCCEEDED":
            return payload
        if status_value in TERMINAL_FAILURES:
            detail = payload.get("last_error")
            suffix = f" ({detail})" if isinstance(detail, str) and detail else ""
            raise ClientError(f"workflow reached {status_value}{suffix}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WaitTimeout(
                f"workflow {workflow_id} did not finish within {timeout} seconds"
            )
        time.sleep(min(2.0, remaining))


def validate_outputs(
    project_dir: Path, workflow_id: str, chunk_count: int, timeout: int
) -> dict[str, Any]:
    raw = _run_container_program(
        project_dir,
        VALIDATE_PROGRAM,
        workflow_id,
        str(chunk_count),
        timeout=min(max(float(timeout), 30.0), 120.0),
    )
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClientError("output validator returned invalid JSON") from error
    if not isinstance(result, dict) or result.get("status") != "SUCCEEDED":
        raise ClientError("output validator did not confirm success")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    project_dir = Path(__file__).resolve().parent.parent
    submission_id = str(uuid.uuid4())
    try:
        token = read_local_bearer(project_dir)
        input_uri = stage_input(project_dir, submission_id, arguments.chunks)
        result = create_workflow(
            port=broker_port(),
            token=token,
            submission_id=submission_id,
            input_uri=input_uri,
            parallel=arguments.parallel,
        )
        workflow_id, status_value = _workflow_identity(result)
        print(f"workflow_id={workflow_id}")
        print(f"status={status_value}")
        print(f"idempotency_key=demo-{submission_id}")
        if not arguments.wait:
            print(
                "Poll with: scripts/submit-workflow.py --wait creates a new demo; "
                f"this workflow is available at GET /v1/workflows/{workflow_id}"
            )
            return 0
        wait_for_workflow(
            port=broker_port(),
            token=token,
            workflow_id=workflow_id,
            timeout=arguments.timeout,
        )
        summary = validate_outputs(
            project_dir, workflow_id, arguments.chunks, arguments.timeout
        )
        print(
            "validated "
            f"{summary['completed_chunks']}/{summary['expected_chunks']} chunk outputs"
        )
        return 0
    except WaitTimeout as error:
        print(f"submit-workflow: {error}", file=sys.stderr)
        return 124
    except ClientError as error:
        print(f"submit-workflow: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
