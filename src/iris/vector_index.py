"""Bounded numpy blocks: no full matrix copy on append or per-query float16 cast."""
from __future__ import annotations

import numpy as np
import threading

BLOCK_SIZE = 1024


class VectorIndex:
    def __init__(self, model: str, dtype: str = "float32"):
        if dtype not in ("float32", "float16"):
            raise ValueError("vector dtype must be float32 or float16")
        self.model, self.dtype = model, np.dtype(dtype)
        self.dimension = 0
        self.blocks: list[np.ndarray] = []
        self.ids: list[np.ndarray] = []
        self.revisions: list[np.ndarray] = []
        self.slots: dict[int, tuple[int, int]] = {}
        self.free: list[tuple[int, int]] = []
        self.lifecycles: dict[int, str] = {}
        self._lock = threading.RLock()

    def snapshot(self) -> VectorIndex:
        """Pin immutable blocks briefly; scoring never holds a writer/index lock.

        A subsequent update copies only a touched block (at most 1024 rows).
        Multiple readers share the same arrays, including during numpy scoring.
        """
        with self._lock:
            result = VectorIndex(self.model, self.dtype.name)
            result.dimension = self.dimension
            result.blocks, result.ids, result.revisions = self.blocks[:], self.ids[:], self.revisions[:]
            for array in result.blocks + result.ids + result.revisions:
                array.flags.writeable = False
            result.slots, result.lifecycles = self.slots.copy(), self.lifecycles.copy()
            return result

    def _writable(self, block: int) -> None:
        if not self.blocks[block].flags.writeable:
            self.blocks[block] = self.blocks[block].copy()
            self.ids[block] = self.ids[block].copy()
            self.revisions[block] = self.revisions[block].copy()

    @property
    def nbytes(self) -> int:
        return sum(a.nbytes for a in self.blocks + self.ids + self.revisions)

    def contains(self, memory_id: int) -> bool:
        with self._lock:
            return memory_id in self.slots

    def remove(self, memory_id: int) -> None:
        with self._lock:
            self._remove(memory_id)

    def _remove(self, memory_id: int) -> None:
        slot = self.slots.pop(memory_id, None)
        self.lifecycles.pop(memory_id, None)
        if slot is not None:
            block, offset = slot
            self._writable(block)
            self.ids[block][offset] = 0
            self.free.append(slot)

    def upsert(self, row) -> None:
        with self._lock:
            self._upsert(row)

    def _upsert(self, row) -> None:
        mid = row["id"]
        if row["lifecycle"] == "deleted" or not row["embedding"] or row["embedding_model"] != self.model:
            self.remove(mid)
            return
        try:
            vector = np.frombuffer(row["embedding"], dtype=np.float32)
        except ValueError:
            self.remove(mid)
            return
        norm = np.linalg.norm(vector)
        if not vector.size or not np.isfinite(norm) or not norm or (self.dimension and vector.size != self.dimension):
            self.remove(mid)
            return
        self.dimension = vector.size
        if mid not in self.slots:
            if not self.free:
                block = len(self.blocks)
                self.blocks.append(np.zeros((BLOCK_SIZE, self.dimension), dtype=self.dtype))
                self.ids.append(np.zeros(BLOCK_SIZE, dtype=np.int64))
                self.revisions.append(np.zeros(BLOCK_SIZE, dtype=np.int64))
                self.free.extend((block, i) for i in reversed(range(BLOCK_SIZE)))
            self.slots[mid] = self.free.pop()
        block, offset = self.slots[mid]
        self._writable(block)
        self.blocks[block][offset] = vector / norm
        self.ids[block][offset] = mid
        self.revisions[block][offset] = row["revision"]
        self.lifecycles[mid] = row["lifecycle"]

    def scores(self, vector, *, include_forgotten=False) -> dict[int, tuple[float, int]]:
        return self.snapshot()._scores(vector, include_forgotten=include_forgotten)

    def _scores(self, vector, *, include_forgotten=False) -> dict[int, tuple[float, int]]:
        query = np.asarray(vector, dtype=np.float32)
        norm = np.linalg.norm(query)
        if query.ndim != 1 or query.size != self.dimension or not np.isfinite(norm) or not norm:
            return {}
        query = query / norm
        result = {}
        for block, ids, revisions in zip(self.blocks, self.ids, self.revisions, strict=True):
            # einsum avoids BLAS thread-pool overhead for these small matrix/vector products.
            scores = np.einsum("ij,j->i", block, query, dtype=np.float32, optimize=False)
            for offset in np.flatnonzero(ids):
                mid = int(ids[offset])
                if include_forgotten or self.lifecycles[mid] == "active":
                    result[mid] = (float(scores[offset]), int(revisions[offset]))
        return result

    def similarity(self, a: int, b: int, revisions=None) -> float:
        with self._lock:
            if a not in self.slots or b not in self.slots:
                return 0.0
            ba, ia = self.slots[a]
            bb, ib = self.slots[b]
            if revisions and (self.revisions[ba][ia], self.revisions[bb][ib]) != revisions:
                return 0.0
            left = self.blocks[ba][ia].astype(np.float32)
            right = self.blocks[bb][ib].astype(np.float32)
        return float(np.dot(left, right))
