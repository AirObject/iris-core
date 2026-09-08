"""Validate the independently approved Python distribution before building."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


def check_selection(root: Path, distribution: str, expected: str, *, upload: bool) -> str:
    paths = {"core": "pyproject.toml", "python-sdk": "sdk/python/pyproject.toml"}
    if distribution not in paths:
        raise ValueError("select core or python-sdk")
    with (root / paths[distribution]).open("rb") as stream:
        project = tomllib.load(stream)["project"]
    version = str(project["version"])
    if (upload or expected) and expected != version:
        raise ValueError("selected distribution version does not match the reviewed version")
    return f"{project['name']}=={version}"


def main() -> None:
    upload = os.environ.get("RELEASE_UPLOAD", "false")
    if upload not in {"true", "false"}:
        raise ValueError("RELEASE_UPLOAD must be true or false")
    print(
        check_selection(
            Path.cwd(),
            os.environ.get("RELEASE_DISTRIBUTION", ""),
            os.environ.get("RELEASE_VERSION", ""),
            upload=upload == "true",
        )
    )


if __name__ == "__main__":
    main()
