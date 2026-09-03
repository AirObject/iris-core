"""Real FAISS adapter behind the indexing layer (§22.3, ADR-0015 §5).

The ``faiss`` library is imported lazily so a runtime without it degrades
the Vector capability instead of failing startup (the same probe discipline
as FTS5, ADR-0014 §1). numpy/faiss types never leave this module: inputs and
outputs are plain Python floats and ints. The index type is
``IndexIDMap2(IndexFlatIP)`` over L2-normalized vectors — inner product on
unit vectors IS cosine similarity, exact (no ANN approximation), fully
deterministic and serializable; search on an immutable handle is thread-safe
read-only per §22.4. ``IndexIDMap2`` (not ``IndexIDMap``) is required for
``reconstruct`` — the build-time self-recall verification depends on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from iris_memory_core.domain.vector import EmbeddingProviderError, validate_finite

#: faiss.METRIC_INNER_PRODUCT == 0; asserted on every load.
FAISS_INDEX_METRIC_INNER_PRODUCT = 0


def faiss_available() -> bool:
    """Probe whether the faiss module is importable in this runtime."""
    try:
        import faiss  # noqa: F401
    except Exception:
        return False
    return True


def _import_faiss() -> Any:
    try:
        import faiss
    except Exception as error:  # pragma: no cover - depends on deployment
        raise RuntimeError("faiss is not available in this runtime") from error
    return faiss


def _import_numpy() -> Any:
    try:
        import numpy
    except Exception as error:  # pragma: no cover - depends on deployment
        raise RuntimeError("numpy is not available in this runtime") from error
    return numpy


class FaissFlatCosineIndex:
    """An exact flat cosine index with explicit int64 ids.

    Build-then-serialize for new generations; load-then-search for serving.
    The object is treated as IMMUTABLE once written to disk — serving
    handles are never mutated (§22.4: no search concurrent with writes).
    """

    def __init__(self, dimension: int) -> None:
        if dimension < 1:
            raise ValueError("dimension must be >= 1")
        faiss = _import_faiss()
        self._dimension = dimension
        # Hold BOTH Python references: SWIG does not keep the inner index
        # alive on its own — losing the reference lets the GC free the C++
        # object and the IDMap silently points at freed memory.
        self._underlying: Any = faiss.IndexFlatIP(dimension)
        self._index: Any = faiss.IndexIDMap2(self._underlying)

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def count(self) -> int:
        return int(self._index.ntotal)

    def ids(self) -> tuple[int, ...]:
        """Every stored id, in add order (unique by builder construction)."""
        faiss = _import_faiss()
        if self._index.ntotal == 0:
            return ()
        return tuple(int(value) for value in faiss.vector_to_array(self._index.id_map))

    def add(self, vectors: Sequence[Sequence[float]], ids: Sequence[int]) -> None:
        """Add a batch; ids must be unique within the index (faiss enforces
        nothing — the builder allocates them monotonically per tenant)."""
        if len(vectors) != len(ids):
            raise ValueError("vectors and ids must have equal length")
        if not vectors:
            return
        numpy = _import_numpy()
        for vector in vectors:
            validate_finite(vector)
            if len(vector) != self._dimension:
                raise EmbeddingProviderError("embedding_dimension_mismatch", retryable=False)
        matrix = numpy.array([list(vector) for vector in vectors], dtype=numpy.float32)
        id_array = numpy.array([int(value) for value in ids], dtype=numpy.int64)
        self._index.add_with_ids(matrix, id_array)

    def search(self, query: Sequence[float], k: int) -> tuple[tuple[int, float], ...]:
        """Exact top-k; returns ((surrogate, cosine), ...) best-first.

        faiss returns id -1 for missing slots — dropped here. Results are
        deterministic for one immutable index and query.
        """
        numpy = _import_numpy()
        validate_finite(query)
        if len(query) != self._dimension:
            raise EmbeddingProviderError("embedding_dimension_mismatch", retryable=False)
        matrix = numpy.array([list(query)], dtype=numpy.float32)
        scores, ids = self._index.search(matrix, max(1, k))
        results: list[tuple[int, float]] = []
        for score, surrogate in zip(scores[0].tolist(), ids[0].tolist(), strict=True):
            if surrogate == -1:
                continue
            results.append((int(surrogate), round(float(score), 6)))
        return tuple(results)

    def reconstruct(self, surrogate_id: int) -> tuple[float, ...]:
        """Reconstruct one stored vector (verification/sampling only)."""
        numpy = _import_numpy()
        vector = numpy.zeros(self._dimension, dtype=numpy.float32)
        self._index.reconstruct(int(surrogate_id), vector)
        return tuple(float(value) for value in vector.tolist())

    def write(self, path: Path) -> None:
        faiss = _import_faiss()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(target))

    @classmethod
    def read(cls, path: Path) -> FaissFlatCosineIndex:
        """Deserialize + structurally validate (type/metric come from the
        persisted bytes; the caller verifies dimension and content against
        the manifest before trusting the handle)."""
        faiss = _import_faiss()
        raw = faiss.read_index(str(Path(path)))
        wrapper = faiss.downcast_index(raw)
        if not isinstance(wrapper, faiss.IndexIDMap2):
            raise ValueError("index file is not an IndexIDMap2")
        if int(wrapper.metric_type) != FAISS_INDEX_METRIC_INNER_PRODUCT:
            raise ValueError("index metric is not inner product")
        handle = cls.__new__(cls)
        handle._dimension = int(wrapper.d)
        handle._index = wrapper
        # The downcast wrapper shares the C++ object that ``raw`` OWNS —
        # both Python references must stay alive or the GC frees the index
        # from under the wrapper.
        handle._underlying = raw
        return handle


__all__ = [
    "FAISS_INDEX_METRIC_INNER_PRODUCT",
    "FaissFlatCosineIndex",
    "faiss_available",
]
