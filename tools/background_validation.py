"""Run one bounded validation batch independently of the interactive task.

Usage: python -m tools.background_validation RUN_DIR TIMEOUT_SECONDS COMMAND [ARG ...]
Results and logs survive task suspension. Never put credentials in arguments.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def save(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def now() -> str:
    return datetime.now(UTC).isoformat()


def candidate_files() -> dict[str, Any]:
    names = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        )
        .decode()
        .split("\0")
    )
    files: dict[str, str | None] = {}
    for name in sorted(set(names) - {""}):
        path = Path(name)
        if path.is_file():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            files[name] = None
    return files


def execute(root: Path) -> int:
    spec = json.loads((root / "task.json").read_text())
    result: dict[str, Any] = {"started_at": now(), "supervisor_pid": os.getpid()}
    try:
        with (root / "output.log").open("ab", buffering=0) as log:
            child = subprocess.Popen(
                spec["command"],
                cwd=spec["cwd"],
                env={**os.environ, "IRIS_VALIDATION_RUN_DIR": str(root)},
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            result["child_pid"] = child.pid
            save(root / "running.json", result)
            try:
                result["exit_code"] = child.wait(timeout=spec["timeout_seconds"])
                result["status"] = "passed" if result["exit_code"] == 0 else "failed"
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                result.update(status="timed_out", exit_code=124)
    except Exception as error:
        result.update(status="failed_to_run", error_type=type(error).__name__, exit_code=125)
    result["finished_at"] = now()
    save(root / "result.json", result)
    return int(result["exit_code"])


def main() -> int:
    if sys.argv[1] == "--execute":
        return execute(Path(sys.argv[2]))
    root = Path(sys.argv[1]).resolve()
    timeout = int(sys.argv[2])
    command = sys.argv[3:]
    if timeout <= 0 or not command:
        raise ValueError("positive timeout and command required")
    root.mkdir(parents=True, exist_ok=False)
    spec = {
        "command": command,
        "cwd": str(Path.cwd()),
        "timeout_seconds": timeout,
        "created_at": now(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    save(root / "candidate-files.json", candidate_files())
    (root / "candidate.patch").write_bytes(subprocess.check_output(["git", "diff", "HEAD"]))
    save(root / "task.json", spec)
    with (root / "supervisor.log").open("ab", buffering=0) as log:
        supervisor = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--execute", str(root)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    spec["supervisor_pid"] = supervisor.pid
    save(root / "task.json", spec)
    print(json.dumps({"run_directory": str(root), "supervisor_pid": supervisor.pid}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
