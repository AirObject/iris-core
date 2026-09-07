"""Private runtime resources shared by source and installed distributions."""

from __future__ import annotations

import atexit
from contextlib import ExitStack
from functools import cache
from importlib.resources import as_file, files
from pathlib import Path

_EXTRACTED = ExitStack()
atexit.register(_EXTRACTED.close)


class RuntimeResourceError(RuntimeError):
    """The installed Core distribution is incomplete."""


@cache
def runtime_resource(relative: str) -> Path:
    """Resolve a bundled resource, retaining extracted files until process exit.

    Editable checkouts use the same source files that Hatch includes in wheels.
    An installed distribution never falls back to the caller's working directory.
    """
    name = Path(relative)
    if name.is_absolute() or ".." in name.parts:
        raise ValueError("invalid runtime resource name")
    bundled = files("iris_memory_core").joinpath("_data", *name.parts)
    if bundled.is_file() or bundled.is_dir():
        return _EXTRACTED.enter_context(as_file(bundled))
    package = Path(__file__).resolve().parent
    checkout = package.parent.parent
    if package.parent.name == "src" and (checkout / "pyproject.toml").is_file():
        source = checkout / name
        if source.exists():
            return source
    raise RuntimeResourceError(f"required Core runtime resource is missing: {relative}")
