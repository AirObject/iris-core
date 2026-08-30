"""Active Surface Coordinator adapter (§25, ADR-0010).

The coordinator's state machine lives in
``iris_memory_core.application.surface.SurfaceCoordinatorService``; this
package marks the adapter boundary (ADR-0007: coordinator adapters implement
application ports). Storage-backed by default — a future remote coordinator
would implement the same service surface without changing callers. An
unreachable coordinator surfaces the stable ``not_ready`` domain error
(``NotReadyError``); no transport-specific exception type escapes the adapter.
"""

from iris_memory_core.application.surface import (
    AcquireOutcome,
    SurfaceCoordinatorService,
)

__all__ = [
    "AcquireOutcome",
    "SurfaceCoordinatorService",
]
