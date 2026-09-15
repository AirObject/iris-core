"""Preserve local confirmation evidence across semantic coordination layers.

Successful helpers retain their existing values. A local failure is returned
unchanged, including its original recovery handle; it is never a work snapshot
or a remote failure. Internal unwinding only stops dependent side effects.
"""
from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
from companion_memory.persistence import NotCommitted, Unconfirmed, Rejected, Failed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.ledger import LedgerFailure
from .results import Failed as RuntimeFailed, RuntimeError

type LocalFailure = NotCommitted | Unconfirmed | Rejected | Failed | RuntimeFailed


class LocalConfirmation(Exception):
    """Carry only a checked fixed result to the current public boundary."""
    def __init__(self, result: object):
        if type(result) not in (NotCommitted, Unconfirmed, Rejected, Failed, RuntimeFailed):
            raise TypeError('A fixed local failure result is required.')
        self.result = result
        super().__init__()


def outcome[**P, T](function: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[object, object, T | LocalFailure]]:
    """Return original local outcomes without erasing successful value types."""
    @wraps(function)
    async def call(*args: P.args, **kwargs: P.kwargs) -> T | LocalFailure:
        try:
            return await function(*args, **kwargs)
        except (LocalConfirmation, LedgerFailure) as failure:
            result = failure.result
            if isinstance(result, (NotCommitted, Unconfirmed, Rejected, Failed, RuntimeFailed)):
                return result
            raise TypeError('Unsupported local confirmation result.') from None
        except OwnerFailure as failure:
            return RuntimeFailed(RuntimeError(failure.code, function.__name__, failure.field,
                                             failure.reason, failure.cleanup_pending))
    return call
