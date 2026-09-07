"""Build fresh Core/SDK artifacts and verify boundaries plus installed consumption."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from tools.check_distribution import inspect_archive

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="iris-package-check-") as name:
        root = Path(name)
        core, sdk = root / "core", root / "sdk"
        for project, output in ((ROOT, core), (ROOT / "sdk/python", sdk)):
            subprocess.run(["uv", "build", str(project), "--out-dir", str(output)], check=True)
        archives = sorted((*core.glob("*.whl"), *core.glob("*.tar.gz")))
        if len(archives) != 2:
            raise RuntimeError("expected exactly one Core wheel and one sdist")
        for archive in archives:
            inspect_archive(archive)
        (core_wheel,) = core.glob("*.whl")
        (sdk_wheel,) = sdk.glob("*.whl")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.check_installation",
                str(core_wheel),
                str(sdk_wheel),
                "--allow-local-sqlite",
            ],
            cwd=ROOT,
            check=True,
        )
    print("fresh package build, archive boundaries and isolated installation: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
