"""Operation-local completion evidence without cancellation or storage authority.

Scopes collect only actual work started within their context, including retained
child tasks. Notifications carry no query, connection, receipt or business data.
An ended caller may wait for its own cleanup without waiting for unrelated work.
"""
from __future__ import annotations
import asyncio
from contextvars import ContextVar, Token
from typing import Callable
from collections.abc import Awaitable

_current: ContextVar[tuple[CompletionScope, ...]] = ContextVar('operation_completions', default=())


class CompletionScope:
    """Finite pending notifications owned by one admitted operation.

    Use inside an already bounded owner task. Completion never authorizes replay
    or proves a commit; it only proves the retained execution has actually ended.
    """
    def __init__(self) -> None:
        self._pending: set[asyncio.Future[object]] = set()
        self._token: Token[tuple[CompletionScope, ...]] | None = None
        self._callbacks: list[Callable[[], None]] = []

    def __enter__(self) -> CompletionScope:
        self._token = _current.set((*_current.get(), self))
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._token is not None
        _current.reset(self._token)
        self._token = None

    @property
    def pending(self) -> bool:
        """Whether this operation still holds actual work, with no global counts."""
        return any(not item.done() for item in self._pending)

    def _retain(self, item: asyncio.Future[object]) -> None:
        if item.done(): return
        self._pending.add(item)
        def ended(future: asyncio.Future[object]) -> None:
            self._pending.discard(future)
            if not self.pending:
                callbacks, self._callbacks = self._callbacks, []
                for callback in callbacks: callback()
        item.add_done_callback(ended)

    def when_ended(self, callback: Callable[[], None]) -> None:
        """After leaving the scope, attach one owner's bounded release callback."""
        if self.pending: self._callbacks.append(callback)
        else: callback()

    async def wait(self) -> None:
        """Join actual completion; cancellation never cancels a retained worker."""
        while self.pending:
            await asyncio.wait(tuple(self._pending))


def retain_completion[T](item: asyncio.Future[T]) -> None:
    """Execution owners publish only their own task's actual end notification."""
    # The notification never reads or writes the future value; erase only its
    # covariant observation type while preserving the original native future.
    from typing import cast
    for scope in _current.get(): scope._retain(cast(asyncio.Future[object], item))


async def finish_owned[T](operation: Awaitable[T]) -> T:
    """Keep an admitted task alive until its own descendant execution ends."""
    with CompletionScope() as completion:
        try: return await operation
        finally: await completion.wait()


def start_owned[T](operation: Awaitable[T]) -> tuple[asyncio.Task[T], asyncio.Future[T]]:
    """Separate the immutable logical result from the admitted task's actual tail.

    The caller retains the task in its bounded registry and waits only on the
    result. A confirmed receipt can be returned while its cleanup still owns a
    slot. Cancelling the result waiter never cancels underlying work.
    """
    result: asyncio.Future[T] = asyncio.get_running_loop().create_future()
    result.add_done_callback(lambda future: None if future.cancelled() else future.exception())
    async def owned() -> T:
        with CompletionScope() as completion:
            try:
                value = await operation
                result.set_result(value)
                return value
            except BaseException as failure:
                if not result.done(): result.set_exception(failure)
                raise
            finally: await completion.wait()
    return asyncio.create_task(owned()), result
