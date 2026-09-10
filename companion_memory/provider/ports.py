"""Separate native capabilities for model work, ledger observation and handoffs.

Trusted startup issues these handles. Possession of an arbitrary role string,
request ID or another handle never creates a broader capability.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING
from .values import Completed, Failed, Found, NotFound, Pending, ProviderError, Rejected
if TYPE_CHECKING:
    from .service import ProviderService


class _Port:
    __slots__ = ("_service",)
    _service: ProviderService
    def __new__(cls):
        raise TypeError("Obtain a native capability from trusted provider assembly.")
    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Provider capabilities are immutable.")
    def _native(self):
        from .service import ProviderService
        if type(self) not in (WorkPort, ObserverPort, ResultOwnerPort):
            return None
        try:
            service = object.__getattribute__(self, "_service")
        except AttributeError:
            return None
        return service if type(service) is ProviderService else None
    def _denied(self, operation: str):
        failure = ProviderError("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH")
        return Rejected(failure)
    def _read_denied(self, operation: str):
        return Failed(ProviderError("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))


class WorkPort(_Port):
    """One caller's bounded model work and its own request observations."""
    __slots__ = ()
    async def generate(self, request: object):
        """Generate nonstreaming text under one durable logical operation key."""
        service = self._native()
        return self._denied("generate") if service is None else await service._work(self, "generate", request)
    async def embed(self, request: object):
        """Return aligned vectors only after their complete result is committed."""
        service = self._native()
        return self._denied("embed") if service is None else await service._work(self, "embed", request)
    async def rerank(self, request: object):
        """Rank original candidate IDs; no hidden batching or provider retries."""
        service = self._native()
        return self._denied("rerank") if service is None else await service._work(self, "rerank", request)
    async def understand_media(self, request: object):
        """Understand one authorized synthetic media handle without storing input."""
        service = self._native()
        return self._denied("understand_media") if service is None else await service._work(self, "understand_media", request)
    def inspect_capabilities(self):
        """Read the current local capability declaration without model or ledger I/O."""
        return self._service._inspect(self)
    async def get_request(self, request_id: object):
        """Read only the calling identity's own request, without a result body."""
        service = self._native()
        return self._read_denied("get_request") if service is None else await service._get_request(self, request_id)


@dataclass(frozen=True, slots=True)
class ObserverGrant:
    """Finite visible scopes, plus a separately explicit account-summary grant."""
    caller_scopes: tuple[str, ...]
    account_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResultGrant:
    """Only this owner's explicitly authorized request IDs may recover bodies."""
    owner_id: str
    request_ids: tuple[str, ...]


class ObserverPort(_Port):
    """Read-only scoped metrics; no model, audit history or result-body access."""
    __slots__ = ()
    def inspect_capabilities(self):
        return self._service._inspect(self)
    async def get_request(self, request_id: object):
        service = self._native()
        return self._read_denied("get_request") if service is None else await service._get_request(self, request_id)
    async def query_usage(self, query: object):
        service = self._native()
        return self._read_denied("query_usage") if service is None else await service._query_usage(self, query)
    async def get_budget_state(self):
        service = self._native()
        return self._read_denied("get_budget_state") if service is None else await service._get_budget_state(self)


class ResultOwnerPort(_Port):
    """Exact request allowlist for recovering committed owner-bound handoffs."""
    __slots__ = ()
    async def recover_result(self, request_id: object):
        service = self._native()
        return self._read_denied("recover_result") if service is None else await service._recover_result(self, request_id)
