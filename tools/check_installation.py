"""Install separate Core/SDK wheels in a clean venv and consume the real service."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("core", type=Path)
    parser.add_argument("sdk", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--allow-local-sqlite", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="iris-installation-") as name:
        root = Path(name)
        for label, wheel in (("embedded", args.core), ("sdk-only", args.sdk)):
            minimal = root / label
            subprocess.run(["uv", "venv", str(minimal), "--python", "3.12"], check=True)
            minimal_python = minimal / "bin/python"
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(minimal_python),
                    *(["--offline"] if args.offline else []),
                    str(wheel.resolve()),
                ],
                check=True,
            )
            if label == "embedded":
                subprocess.run(
                    [
                        str(minimal_python),
                        "-I",
                        str(ROOT / "tools/smoke_installed_embedded.py"),
                        *(["--allow-local-sqlite"] if args.allow_local_sqlite else []),
                    ],
                    cwd=root,
                    check=True,
                    timeout=120,
                )
            else:
                subprocess.run(
                    [
                        str(minimal_python),
                        "-I",
                        "-c",
                        "import importlib.util; from iris_memory_sdk import AsyncIrisMemoryClient; "
                        "assert all(importlib.util.find_spec(name) is None for name in "
                        "('iris_memory_core','faiss','numpy','fastapi','uvicorn')); "
                        "print('SDK-only dependency boundary: passed')",
                    ],
                    cwd=root,
                    check=True,
                )
        environment = root / "venv"
        subprocess.run(["uv", "venv", str(environment), "--python", "3.12"], check=True)
        python = environment / "bin/python"
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                *(["--offline"] if args.offline else []),
                str(args.core.resolve()) + "[server,vector,console]",
                str(args.sdk.resolve()),
            ],
            check=True,
        )
        subprocess.run(
            [str(python), "-I", str(ROOT / "tools/check_public_api.py"), "--installed"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            [
                str(python),
                "-I",
                str(ROOT / "tools/smoke_installed.py"),
                *(["--allow-local-sqlite"] if args.allow_local_sqlite else []),
                *(["--report", str(args.report.resolve())] if args.report else []),
            ],
            cwd=root,
            check=True,
        )
        subprocess.run(
            [
                str(python),
                "-I",
                str(ROOT / "tools/smoke_installed_recall.py"),
                *(["--allow-local-sqlite"] if args.allow_local_sqlite else []),
            ],
            cwd=root,
            check=True,
            timeout=180,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
