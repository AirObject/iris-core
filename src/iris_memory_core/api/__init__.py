"""Lazy HTTP transport; offline and embedded imports need no web framework."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from iris_memory_core.api.app import create_app as create_app

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from iris_memory_core.api.app import create_app

        return create_app
    raise AttributeError(name)
