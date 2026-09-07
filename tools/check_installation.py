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
                str(args.core.resolve()) + "[console]",
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
