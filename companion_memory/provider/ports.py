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
    from companion_memory.persistence import UnitOfWork


class _Port:
    __slots__ = ("_service", "__weakref__")
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
    def verify_request_in_transaction(self, uow: UnitOfWork, request_id: str, original_request: object):
        """Recheck this exact original request in an existing local transaction."""
        service = self._native()
        if service is None:
            return self._read_denied('lookup_request')
        from .transaction_evidence import request_in_transaction
        return request_in_transaction(service,self,uow,request_id,original_request)

    def consumers_ended(self) -> bool:
        """Whether this exact native work capability has no actual consumers remaining."""
        service = self._native()
        return service is not None and service.work_consumers_ended(self)

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
    async def lookup_request(self, operation: object, original_request: object):
        """Confirm an original scoped request; a miss never grants replay authority."""
        service = _Port._native(self)
        return _Port._read_denied(self,"lookup_request") if service is None else await service._lookup_request(self, operation, original_request)

    async def verify_unsent(self, operation: object, original_request: object):
        """Prove isolated no-dispatch status; lookup absence alone is insufficient."""
        service = _Port._native(self)
        if service is None: return _Port._read_denied(self, 'lookup_request')
        from .unsent_evidence import verify_unsent
        return await verify_unsent(service, self, operation, original_request)

    async def send_verified_first(self,evidence: object,original_request: object):
        """Send one original embedding request after native absence verification.

        This explicit entry cannot revive registered or zero-attempt terminal
        work. It consumes one issuer-bound seal and preserves its total deadline.
        """
        service=self._native()
        return self._denied('embed') if service is None else await service._send_verified_first(self,evidence,original_request)

    def verify_unsent_in_transaction(self, uow: UnitOfWork, evidence: object, original_request: object, *, retry: bool = False):
        """Recheck a held native absence seal; optionally check current retry cost."""
        service = self._native()
        if service is None:return self._read_denied('lookup_request')
        from .transaction_evidence import unsent_in_transaction
        return unsent_in_transaction(service,self,uow,evidence,original_request,retry=retry)


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
    def verify_completion_in_transaction(self, uow: UnitOfWork, completion: object, *, retry_work: WorkPort | None = None):
        """Check original completion and optional persona retry capacity locally.

        Only an existing native persona work capability may request the retry
        check. No budget mutation, new request or dispatch grant is produced.
        """
        service = self._native()
        if service is None:
            return self._read_denied('recover_result')
        from .transaction_evidence import completion_in_transaction
        return completion_in_transaction(service,self,uow,completion,retry_work)

    async def confirm_completion(self, terminal: object):
        """Confirm this native text terminal's original audited completion key.

        Uses the existing exact request allowlist and bounded local reads. It
        neither exposes audit history nor grants dispatch or retry permission.
        Failure and actual cleanup remain independent of remote outcome.
        """
        service = self._native()
        if service is None:
            return self._read_denied('recover_result')
        from .completion_evidence import confirm_completion
        return await confirm_completion(service, self, terminal)

    async def verify_terminal(self, request_id: object, original_request: object = None):
        """Obtain native evidence for this owner's audited durable terminal, without sending."""
        service = self._native()
        return self._read_denied('recover_result') if service is None else await service._verify_terminal(self, request_id, original_request)

    async def recover_result(self, request_id: object):
        service = self._native()
        return self._read_denied("recover_result") if service is None else await service._recover_result(self, request_id)
