"""Observe real GraphRoute state and repository reads without implementing BFS."""

from __future__ import annotations

import sys
from collections import defaultdict
from types import FrameType
from typing import Any

import pytest

from iris_memory_core.application.recall import GraphRoute
from iris_memory_core.storage.projection import GraphRepository


class GraphExecutionProbe:
    def __init__(self) -> None:
        self.visited: set[tuple[str, str]] = set()
        self.fanout_by_depth: dict[int, int] = defaultdict(int)
        self.max_depth = 0
        self.candidate_count = 0
        self.reads: list[tuple[str, int | None, int]] = []
        self.calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        collect = GraphRoute.collect
        edges_for_source = GraphRepository.edges_for_source

        def observe(frame: FrameType, event: str, arg: Any) -> Any:
            if frame.f_code is not collect.__code__:
                return None
            values = frame.f_locals
            self.visited.update(values.get("visited", ()))
            depth = values.get("depth", 0)
            self.max_depth = max(self.max_depth, depth)
            self.fanout_by_depth[depth] = max(
                self.fanout_by_depth[depth], values.get("fanout_used", 0)
            )
            self.candidate_count = max(self.candidate_count, len(values.get("candidates", ())))
            return observe

        def traced_collect(route: GraphRoute, *args: Any, **kwargs: Any) -> Any:
            previous = sys.gettrace()
            self.calls += 1
            sys.settrace(observe)
            try:
                return collect(route, *args, **kwargs)
            finally:
                sys.settrace(previous)

        def counted_read(
            repository: GraphRepository,
            tenant_id: str,
            generation_id: str,
            node_id: str,
            *,
            node_kind: str,
            limit: int | None = None,
        ) -> Any:
            rows = edges_for_source(
                repository, tenant_id, generation_id, node_id, node_kind=node_kind, limit=limit
            )
            self.reads.append((node_id, limit, len(rows)))
            return rows

        monkeypatch.setattr(GraphRoute, "collect", traced_collect)
        monkeypatch.setattr(GraphRepository, "edges_for_source", counted_read)
