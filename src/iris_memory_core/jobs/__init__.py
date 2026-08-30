"""Durable job adapters: the outbox worker runtime (§16).

Job-kind registry lives in ``iris_memory_core.domain.jobs``; handlers are
injected so only kinds with existing, safe handlers are claimable.
"""

from iris_memory_core.jobs.worker import DEFAULT_HANDLERS, OutboxWorker, selfcheck_handler

__all__ = ["DEFAULT_HANDLERS", "OutboxWorker", "selfcheck_handler"]
