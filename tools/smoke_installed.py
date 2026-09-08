"""Run with an isolated, wheel-installed Python (-I), outside the source checkout.

Business operations use the separately installed SDK. Only trusted initialization
and worker lifecycle use the Core CLI; no storage imports or direct database IO.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from iris_memory_sdk import AsyncIrisMemoryClient
from iris_memory_sdk.client import IrisMemoryApiError


def command(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [sys.executable, "-I", "-m", "iris_memory_core", *arguments],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return result.stdout


async def consume(base: str, credential: dict[str, str]) -> None:
    client = AsyncIrisMemoryClient(base, bearer_token=credential["token"])
    capabilities = await client.capabilities()
    assert "recall.v1" in capabilities.capabilities
    assert capabilities.schema_version == 25
    negotiated = await client.negotiate()
    assert negotiated.schema_version == capabilities.schema_version
    focus = {"agent_id": credential["agent_id"], "kind": "goal", "summary": "Installed goal"}
    try:
        await client.create_focus_item(focus, idempotency_key="installed-focus")
        raise AssertionError("Required Focus accepted missing Lease proof")
    except IrisMemoryApiError as error:
        assert error.envelope.code == "lease_expired"
    lease = await client.acquire_surface_lease(
        credential["agent_id"],
        holder_app_instance_id="installed-client",
        ttl_us=60_000_000,
    )
    proof = {"lease_id": lease["lease_id"], "lease_epoch": lease["lease_epoch"]}
    await client.create_focus_item({**focus, **proof}, idempotency_key="installed-focus")
    record = {
        "agent_id": credential["agent_id"],
        "space_id": credential["space_id"],
        "kind": "idea",
        "title": "installed-wheel-note",
        "body": "Package smoke memory",
        **proof,
    }
    note = await client.create_note(record, idempotency_key="installed-note")
    replay = await client.create_note(record, idempotency_key="installed-note")
    assert note == replay
    observed = await client.observe_batch(
        [
            {
                "agent_id": credential["agent_id"],
                "space_id": credential["space_id"],
                "kind": "message.text",
                "role": "user",
                "idempotency_key": "installed-observation",
                "effect_state": "committed",
                "occurred_us": time.time_ns() // 1000,
                "committed_us": time.time_ns() // 1000,
                "source_stream": "smoke:client",
                "source_cursor": "1",
                "content": "Package smoke memory",
            }
        ],
        idempotency_key="installed-batch",
        lease_id=lease["lease_id"],
        lease_epoch=lease["lease_epoch"],
    )
    assert observed
    await client.source_cursor(credential["agent_id"], "smoke:client")
    persona = await client.current_persona(credential["agent_id"])
    assert persona
    recall = await client.recall(
        {
            "schema_version": 1,
            **proof,
            "request_id": "01a07600-0000-7000-8000-000000000001",
            "scope": {"agent_id": credential["agent_id"], "space_id": credential["space_id"]},
            "actors": [{"provider": "smoke", "external_id": "client"}],
            "topic": "Package smoke memory",
            "purpose": "reply",
            "token_budget": 2000,
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
        }
    )
    assert recall["request_id"] == "01a07600-0000-7000-8000-000000000001"


def run(root: Path, *, local_sqlite: bool) -> dict[str, object]:
    database = root / "data" / "core.sqlite3"
    credential_file = root / "client.json"
    flags = ["--allow-local-sqlite"] if local_sqlite else []
    migration = command(root, "migrate", str(database))
    assert "schema_version=25" in migration and "applied=25" in migration
    assert command(root, "schema-version", str(database)).strip() == "25"
    command(
        root,
        "init",
        "--database",
        str(database),
        "--tenant",
        "installed-smoke",
        "--agent-name",
        "Installed agent",
        "--app-instance",
        "installed-client",
        "--actor-provider",
        "smoke",
        "--actor-subject",
        "client",
        "--surface-mode",
        "required",
        "--credential-file",
        str(credential_file),
        *flags,
    )
    credential = json.loads(credential_file.read_text())
    assert credential_file.stat().st_mode & 0o777 == 0o600
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (root / "server.log").open("w") as logs:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "iris_memory_core",
                "serve",
                "--database",
                str(database),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                *flags,
            ],
            cwd=root,
            stdout=logs,
            stderr=logs,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError("installed service exited during startup")
                try:
                    with urlopen(base + "/health/live", timeout=1) as response:
                        assert response.status == 200
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("installed service startup timed out") from None
                    time.sleep(0.1)
            asyncio.run(consume(base, credential))
            for path in ("/v1/get_store", "/v1/sql", "/v1/queue/pop", "/console/v1/bootstrap"):
                try:
                    with urlopen(
                        Request(
                            base + path,
                            headers={
                                "Authorization": "Bearer " + credential["token"],
                            },
                        ),
                        timeout=5,
                    ):
                        raise AssertionError("private or management endpoint accepted client")
                except HTTPError as error:
                    assert error.code in {401, 403, 404, 405}
        finally:
            started = time.monotonic()
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
            shutdown = time.monotonic() - started
    assert process.returncode in {0, -signal.SIGTERM}
    # Uvicorn restores/re-raises SIGTERM after draining on supported releases.
    # A signal exit alone is not evidence that lifespan shutdown completed.
    assert "Application shutdown complete" in (root / "server.log").read_text()
    command(root, "worker", "--database", str(database), "--once", *flags)
    return {
        "core_version": version("iris-memory-core"),
        "sdk_version": version("iris-memory-sdk"),
        "schema_version": 25,
        "development_sqlite_override": local_sqlite,
        "shutdown_seconds": shutdown,
        "serve_exit_code": process.returncode,
        "public_sdk_smoke": "passed",
        "surface_mode": "required",
        "worker_once": "passed",
        "private_endpoint_rejections": 4,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-sqlite", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not sys.flags.isolated:
        raise RuntimeError("installed smoke requires python -I")
    # User Python paths and editable .pth files are not an installation proof.
    import iris_memory_sdk

    import iris_memory_core

    prefix = Path(sys.prefix).resolve()
    for module in (iris_memory_core, iris_memory_sdk):
        if module.__file__ is None or not Path(module.__file__).resolve().is_relative_to(prefix):
            raise RuntimeError("smoke must consume installed Core and SDK wheels")
    with tempfile.TemporaryDirectory(prefix="iris-installed-smoke-") as name:
        root = Path(name)
        os.chmod(root, 0o700)
        report = run(root, local_sqlite=args.allow_local_sqlite)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.write_text(encoded)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
