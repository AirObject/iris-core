"""Own bounded model work over a durable, audited provider ledger.

Only the original execution owner may dispatch after a confirmed preparation.
Caller timeouts never release a live adapter or local transaction. Startup turns
unresolved preparations into conservative unknowns and never repeats a model call.
"""
from __future__ import annotations
from companion_memory.persistence.completion import finish_owned, CompletionScope
import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import math
import threading
from weakref import WeakKeyDictionary
from types import MappingProxyType
from typing import cast, TYPE_CHECKING
if TYPE_CHECKING:
    from .stored_media import StoredMediaAuthority, StoredMediaAuthorized
    from companion_memory.media.service import MediaError

from companion_memory.configuration import EffectiveSnapshot, PresentValue, provider_snapshot_issue
from companion_memory.logging_service import Logger
from companion_memory.persistence import Committed, NotCommitted, Unconfirmed
from .usage_only import continuation_ready
from .accounting import budget_key, check_budget, evidence_compatible, reserve_budget, revise, row, settle_budget
from .ledger import LedgerAssembly, LedgerBinding, LedgerFailure, Mutation
from .normalization import OUTCOMES, USAGE_FIELDS, input_units, keys, normalize_usage, result_payload, validate_attribution
from .ports import ObserverGrant, ObserverPort, ResultGrant, ResultOwnerPort, WorkPort
from .resources import AdapterResponse, AuthorizedMedia, CancellationToken, GateBinding, ProviderResources, SimulationAdapter, WorkGrant, native_issued
from .values import (Completed, Data, DataLimit, Failed, Found, Health, InvalidData, MAX_INTEGER, NotFound, Pending,
                     ProviderError, Ready, Record, RecoveryPending, Rejected, CloseReport, as_record, dump, fingerprint, freeze, is_identifier, load)
from .generation_resources import RealGenerationResources, ResourceLimits, GenerationBound, ChatGenerationAdapter, usage_observation
if TYPE_CHECKING:
    from companion_memory.configuration.text_persistence import StoredTextConfiguration

CAPABILITIES = {"generate": "GENERATION", "embed": "EMBEDDING", "rerank": "RERANK", "understand_media": "MEDIA_UNDERSTANDING"}
ROLES = ("LEARNING", "DREAM", "PERSONA", "MEDIA", "EMBEDDING", "RERANK", "GOAL", "DIAGNOSTIC")
OPTIONALS = ("parent_request_id", "trace_id", "batch_id", "dream_run_id", "prompt_revision")


def error(code: str, operation: str, field_name: str, reason: str, pending: bool = False) -> ProviderError:
    return ProviderError(code, operation, field_name, reason, pending)


def derived_id(kind: str, *values: Data) -> str:
    return kind+"-"+fingerprint(tuple(values))


def error_value(value: ProviderError | None) -> Data:
    if value is None:
        return None
    return MappingProxyType({"code": value.code, "field": value.field, "reason": value.reason})


@dataclass(slots=True, eq=False)
class _Job:
    operation: str
    grant: WorkGrant
    request: Record
    token: CancellationToken
    deadline: float
    request_id: str
    task: asyncio.Task | None = None
    account_id: str | None = None
    stored: Record | None = None
    attempt: Record | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    worker: threading.Thread | None = None
    first_error: ProviderError | None = None
    unknown: bool = False
    publication: asyncio.Future | None = None
    worker_completed_at: float | None = None
    worker_started_at: float | None = None
    worker_response: AdapterResponse | None = None
    worker_failure: BaseException | None = None
    reserved: int = 0
    port: object = None
    media_authorization: object = None
    partial_response: AdapterResponse | None = None
    local_read_pending: bool = False
    local_write_pending: bool = False
    transport_not_sent: bool = False
    completion: CompletionScope = field(default_factory=CompletionScope)


class _EvidenceConflict(Exception):
    """Late evidence disagrees with an already known immutable fact."""


class _AdmissionStopped(Exception):
    def __init__(self, cause: ProviderError):
        self.cause = cause
        super().__init__()


class _StoredPolicyMismatch(Exception):
    """Persistent account policy differs from this explicit initialization."""


class _WriteFailure(Exception):
    def __init__(self, reason: str, uncertain: bool = False, cleanup_pending: bool = False):
        self.reason, self.uncertain, self.cleanup_pending = reason, uncertain, cleanup_pending
        super().__init__()


class ProviderService:
    """No-I/O construction; trusted initialization borrows storage and resources."""
    def __init__(self, assembly: LedgerAssembly):
        if type(assembly) is not LedgerAssembly:
            raise TypeError("A native provider ledger assembly is required.")
        self._assembly = assembly
        self._state = "NEW"
        self._binding: LedgerBinding | None = None
        self._resources: ProviderResources | RealGenerationResources | None = None
        self._text_configuration: StoredTextConfiguration | None = None
        self._text_admission_blocked = False
        self._settings: Record = MappingProxyType({})
        self._profiles: dict[str, Record] = {}
        self._accounts: dict[str, Record] = {}
        self._ports: WeakKeyDictionary[object, WorkGrant | ObserverGrant | ResultGrant] = WeakKeyDictionary()
        self._media: WeakKeyDictionary[object, None] = WeakKeyDictionary()
        self._registration_limit: int | None = None
        from weakref import WeakValueDictionary
        self._terminal_evidence = WeakValueDictionary()
        self._completion_evidence = WeakValueDictionary()
        self._unsent_evidence = WeakValueDictionary()
        self._stored_authorities: WeakValueDictionary[int, StoredMediaAuthority] = WeakValueDictionary()
        self._stored_media: dict[AuthorizedMedia, tuple[StoredMediaAuthority, str, str]] = {}
        self._stored_pending: set[tuple[int, str, str]] = set()

        self._jobs: set[_Job] = set()
        self._lookup_tasks: set[asyncio.Task] = set()
        self._lookup_consumers: dict[asyncio.Task, AuthorizedMedia] = {}
        self._lookup_owners: dict[asyncio.Task, object] = {}
        self._serial = asyncio.Lock()
        self._init_task: asyncio.Task | None = None
        self._init_completion = CompletionScope()
        self._initialization_cleanup: asyncio.Task | None = None
        self._recovery = None
        self._close_report: CloseReport | None = None
        self._reason: str | None = None
        self._ledger_faulted = False
        self._unknown = 0
        self._execution_owner: str | None = None

    @property
    def _ledger(self) -> LedgerBinding:
        assert self._binding is not None
        return self._binding

    @property
    def _res(self) -> ProviderResources | RealGenerationResources:
        assert self._resources is not None
        return self._resources

    @property
    def _text(self) -> bool:
        return self._assembly.text_generation

    def bind_generation_resources(self, configuration: object, resources: object, limits: ResourceLimits) -> GenerationBound | Rejected:
        """Preflight fixed native generation resources without resolving a secret.

        The persisted configuration identity, transport and role resources must
        match exactly. A failed binding creates no work port or worker.
        """
        from companion_memory.configuration.text_persistence import StoredTextConfiguration, stored_text_configuration_issue
        from .credentials import CredentialResolver
        from .chat_protocol import ChatBinding
        from .chat_transport import ChatTransport
        from companion_memory.cognition.text_resources import output_schema
        if (not self._text or self._state != 'NEW' or stored_text_configuration_issue(configuration) is not None
                or type(resources) is not RealGenerationResources or type(limits) is not ResourceLimits):
            return Rejected(error('RESOURCE_FAILED', 'initialize', 'capability', 'RESOURCE_INVALID'))
        configured = cast(StoredTextConfiguration, configuration)
        if (sum(type(port) is WorkPort for port in self._ports) > limits.registered_work_limit
                or self._binding is not None and self._binding.text_configuration is not configured):
            return Rejected(error('RESOURCE_FAILED', 'initialize', 'capability', 'RESOURCE_INVALID'))
        try:
            if (not native_issued(resources.gate, GateBinding) or type(resources.adapter) is not ChatGenerationAdapter
                    or type(resources.credential_resolver) is not CredentialResolver
                    or type(resources.adapter.transport) is not ChatTransport
                    or resources.adapter.transport._resolver is not resources.credential_resolver
                    or resources.adapter.transport._settings != configured.candidate.text.record('provider.transport')
                    or resources.adapter.transport._monotonic is not resources.monotonic
                    or resources.logger is not None and type(resources.logger) is not Logger
                    or not all(callable(v) for v in (resources.monotonic, resources.utc_now, resources.new_id, resources.completed))):
                raise InvalidData()
            generation = configured.candidate.text.record('provider.generation')
            for role, name in (('LEARNING', 'text_learning'), ('PERSONA', 'initial_persona')):
                selected = generation if role == 'LEARNING' else configured.candidate.text.record('self_model.initial_persona')
                expected = ChatBinding(cast(str, generation['model_id']), cast(tuple[str, ...], generation['expected_reported_models']),
                    cast(str | None, generation['resolved_model_id']), cast(str, selected['schema_ref']), cast(str, selected['schema_digest']), name, output_schema(role))
                if resources.adapter.bindings[role] != expected: raise InvalidData()
        except (InvalidData, AttributeError, KeyError):
            return Rejected(error('RESOURCE_FAILED', 'initialize', 'capability', 'RESOURCE_INVALID'))
        self._text_configuration = configured
        self._registration_limit = limits.registered_work_limit
        return GenerationBound(configured.database_id, configured.snapshot_id)

    def _input_units(self, capability: str, payload: Record, profile: Record | None = None, role: str = 'LEARNING'):
        if not self._text:
            return input_units(capability, payload, profile)
        from .chat_protocol import encode_request
        if capability != 'GENERATION' or type(self._res) is not RealGenerationResources:
            return 0, 0, 'PROTOCOL_UNSUPPORTED'
        encode_request(payload, self._res.adapter.bindings[role])
        incoming, outgoing = cast(int, payload['reservation_input_bound']), cast(int, payload['output_tokens'])
        if profile is not None and (incoming != profile['max_input_units'] or outgoing != profile['max_output_units']):
            raise InvalidData()
        return incoming, outgoing, None

    def _usage(self, raw: object, profile: Record, amount: int, *, not_sent: bool = False) -> Record:
        if not self._text:
            return normalize_usage(raw, profile, amount)
        from .text_accounting import normalize
        from .chat_protocol import observe_usage
        observation = usage_observation(raw) if type(raw) is MappingProxyType else observe_usage(None)
        return normalize(observation, self._accounts[cast(str, profile['account_id'])], profile, amount, not_sent=not_sent)

    def _check_budget(self, budget: Record, amount: int) -> str | None:
        if self._text:
            from .text_accounting import check_budget as text_check
            return text_check(budget, amount)
        return check_budget(budget, amount)

    def _reserve_budget(self, budget: Record, amount: int) -> Record:
        if self._text:
            from .text_accounting import reserve_budget as text_reserve
            return text_reserve(budget, amount)
        return reserve_budget(budget, amount)

    def _settle_budget(self, budget: Record, reservation: Record, usage: Record) -> Record:
        if self._text:
            from .text_accounting import settle_budget as text_settle
            return text_settle(budget, reservation, usage)
        return settle_budget(budget, reservation, usage)

    def _result_payload(self, capability: str, raw: object, request: Record, profile: Record, limit: int, role: str) -> Record:
        if not self._text:
            return result_payload(capability, raw, request, profile, limit)
        from .chat_protocol import validate_structured_result
        resources = self._res
        if type(resources) is not RealGenerationResources or capability != 'GENERATION':
            raise InvalidData()
        binding = resources.adapter.bindings[role]
        if 'schema_ref' in as_record(request['payload']) and as_record(request['payload'])['schema_ref'] != binding.schema_ref:
            raise InvalidData()
        return validate_structured_result(raw, binding)

    def _number(self, name: str) -> int:
        return cast(int, self._settings[name])

    def _now(self) -> float:
        value = self._res.monotonic()
        if type(value) not in (int, float) or type(value) is int and not -MAX_INTEGER <= value <= MAX_INTEGER or not math.isfinite(value):
            raise InvalidData()
        return float(value)

    def _utc(self) -> str:
        value = self._res.utc_now()
        if type(value) is not datetime or value.tzinfo is not timezone.utc:
            raise InvalidData()
        return value.isoformat(timespec="microseconds")

    def _id(self) -> str:
        value = self._res.new_id()
        if not is_identifier(value):
            raise InvalidData()
        return value

    def _state_error(self, operation: str) -> ProviderError | None:
        if self._state == "READY":
            return error("INVALID_STATE", operation, "state", "ALREADY_INITIALIZED") if operation == "initialize" else None
        if self._state == "NEW":
            return None if operation == "initialize" else error("INVALID_STATE", operation, "state", "NOT_INITIALIZED")
        reason = "SERVICE_FAULTED" if self._state == "FAULTED" else "SERVICE_CLOSED"
        return error("INVALID_STATE", operation, "state", reason)

    async def initialize(self, snapshot: object, storage_binding: object, resources: object):
        """Check native assembly, configuration and stored policy before recovery.

        Recovery is local and idempotent. An incomplete initialization owns its
        pending task and lease; no other service may reclaim its preparations.
        """
        if self._text:
            if (issue := self._state_error('initialize')) is not None:
                return Rejected(issue)
            if self._init_task is not None and not self._init_task.done():
                return RecoveryPending(error('PERSISTENCE_FAILED', 'initialize', 'ledger', 'LEDGER_UNCONFIRMED', True))
            bound = self.bind_generation_resources(snapshot, resources, ResourceLimits(1, 1))
            if type(bound) is not GenerationBound:
                return cast(Rejected, bound)
        try:
            native_resources = (self._text and type(resources) is RealGenerationResources or type(resources) is ProviderResources and native_issued(resources.gate, GateBinding)
                                and native_issued(resources.adapter, SimulationAdapter)
                                and (resources.logger is None or type(resources.logger) is Logger)
                                and all(callable(value) for value in (resources.monotonic, resources.utc_now, resources.new_id)))
        except AttributeError:
            native_resources = False
        if self._text and type(resources) is RealGenerationResources:
            native_resources = True
        if not native_resources:
            return Rejected(error("RESOURCE_FAILED", "initialize", "capability", "RESOURCE_INVALID"))
        if not self._assembly.issued(storage_binding):
            return Rejected(error("ACCESS_DENIED", "initialize", "capability", "CAPABILITY_MISMATCH"))
        if (issue := self._state_error("initialize")) is not None:
            return Rejected(issue)
        if self._init_task is not None and not self._init_task.done():
            return RecoveryPending(error("PERSISTENCE_FAILED", "initialize", "ledger", "LEDGER_UNCONFIRMED", True))
        binding = cast(LedgerBinding, storage_binding)
        if binding.storage.get_health().lifecycle != "READY":
            return Rejected(error("ACCESS_DENIED", "initialize", "capability", "CAPABILITY_MISMATCH"))
        supplied_snapshot = self._text_configuration.candidate.foundation if self._text_configuration is not None else snapshot
        if not self._text and (issue := provider_snapshot_issue(snapshot)) is not None:
            return Rejected(error("CONFIGURATION_UNSUPPORTED", "initialize", "configuration", issue))
        if self._text and (binding.text_configuration is None or binding.text_configuration is not snapshot or self._text_configuration is None
                or binding.text_configuration.database_id != self._text_configuration.database_id):
            return Rejected(error("ACCESS_DENIED", "initialize", "capability", "CAPABILITY_MISMATCH"))
        if binding.snapshot is not supplied_snapshot:
            return Rejected(error("ACCESS_DENIED", "initialize", "capability", "CAPABILITY_MISMATCH"))
        if not binding.acquire(self):
            return Rejected(error("ACCESS_DENIED", "initialize", "capability", "CAPABILITY_MISMATCH"))
        if self._text_configuration is not None and self._text_configuration.database_id != binding.database_id:
            binding.release()
            return Rejected(error('ACCESS_DENIED', 'initialize', 'capability', 'CAPABILITY_MISMATCH'))
        self._binding, self._resources = binding, cast(ProviderResources | RealGenerationResources, resources)
        supplied = cast(EffectiveSnapshot, supplied_snapshot)
        self._settings = MappingProxyType({entry.definition.key.removeprefix("provider."): cast(Data, entry.state.value)
                                           for entry in supplied.list_entries() if entry.definition.key.startswith("provider.") and type(entry.state) is PresentValue})
        self._profiles = {cast(str, as_record(profile)["profile_id"]): as_record(profile) for profile in cast(tuple[Data, ...], self._settings["profiles"])}
        self._accounts = {cast(str, as_record(account)["account_id"]): as_record(account) for account in cast(tuple[Data, ...], self._settings["accounts"])}
        try:
            self._execution_owner = self._execution_owner or self._id()
            deadline = self._now()+self._number("request_timeout_ms")/1000
            self._init_task = asyncio.create_task(self._bootstrap_owned(deadline))
            self._init_task.add_done_callback(lambda task: self._finish_close())
            return await asyncio.wait_for(asyncio.shield(self._init_task), max(0, deadline-self._now()))
        except TimeoutError:
            return RecoveryPending(error("PERSISTENCE_FAILED", "initialize", "ledger", "LEDGER_UNCONFIRMED", True))
        except MemoryError:
            raise
        except Exception:
            self._state = "FAULTED"
            self._reason = "RESOURCE_FAILURE"
            return Rejected(error("RESOURCE_FAILED", "initialize", "state", "RESOURCE_FAILURE", True))

    async def _pages(self, table: str):
        after = ""
        while True:
            rows = await self._ledger.read(table+"_page", {"after": after, "limit": 4})
            for value in rows:
                yield value
            if len(rows) < 4:
                break
            after = cast(str, rows[-1]["object_id"])

    async def _bootstrap_owned(self, deadline: float):
        with self._init_completion:
            return await self._bootstrap(deadline)

    async def _bootstrap(self, deadline: float):
        if self._recovery is None:
            self._recovery = self._recovery_steps()
        try:
            while self._now() < deadline and self._state == "NEW" and not self._ledger_faulted:
                try:
                    await anext(self._recovery)
                except StopAsyncIteration:
                    self._recovery = None
                    if self._state != "NEW" or self._ledger_faulted:
                        break
                    self._state = "READY"
                    return Ready(cast(str,self._ledger.database_id))
            if self._ledger_faulted:
                if self._reason == "RESOURCE_FAILURE":
                    return RecoveryPending(error("RESOURCE_FAILED", "initialize", "state", "RESOURCE_FAILURE",
                                                 self._init_completion.pending))
                return RecoveryPending(error("PERSISTENCE_FAILED", "initialize", "ledger", "LEDGER_UNCONFIRMED"))
            return RecoveryPending(error("TIMEOUT", "initialize", "request", "DEADLINE_EXCEEDED"))
        except _StoredPolicyMismatch:
            self._recovery = None
            self._ledger.release()
            return Rejected(error("CONFIGURATION_UNSUPPORTED", "initialize", "configuration", "STORED_POLICY_MISMATCH"))
        except _WriteFailure as failure:
            self._recovery = None
            self._fault(failure.reason)
            return RecoveryPending(error("PERSISTENCE_FAILED", "initialize", "ledger", failure.reason, failure.cleanup_pending))
        except LedgerFailure:
            self._recovery = None
            await self._init_completion.wait()
            self._fault("LEDGER_READ_FAILED")
            return RecoveryPending(error("PERSISTENCE_FAILED", "initialize", "ledger", "LEDGER_READ_FAILED"))
        except InvalidData:
            self._recovery = None
            self._fault("LEDGER_INCONSISTENT")
            return Rejected(error("PERSISTENCE_FAILED", "initialize", "ledger", "LEDGER_INCONSISTENT"))
        except MemoryError:
            raise
        except Exception:
            self._recovery = None
            self._fault("RESOURCE_FAILURE")
            return Rejected(error("RESOURCE_FAILED", "initialize", "state", "RESOURCE_FAILURE"))

    async def _recovery_steps(self):
        """Retain bounded scan position across explicit initialization deadlines."""
        found: dict[str, Record] = {}
        async for budget in self._pages("budget_windows"):
            account = cast(str, budget["account_id"])
            if account not in self._accounts or budget["policy"] != self._accounts[account]:
                raise _StoredPolicyMismatch()
            found[account] = budget
            yield None
        yield None
        if found and set(found) != set(self._accounts):
            raise _StoredPolicyMismatch()
        if not found:
            existing = await self._ledger.read("requests_page", {"after": "", "limit": 1})
            yield None
            if existing:
                raise InvalidData()
            changes = []
            for account_id, policy in self._accounts.items():
                budget = row(budget_key(policy), account_id=account_id, window_id=policy["window_id"], policy=policy,
                             attempt_count=0, known_subtotal_atoms=0, held_atoms=0, risk_state="CLEAR")
                if self._text:
                    budget = as_record(freeze({**budget, 'format_version': 2, 'quota_reserved': 0, 'quota_known': 0, 'quota_held': 0}, 8192, owned=True))
                changes.append(Mutation("budget_windows", None, budget))
            await self._commit("initialize_budget", "initialize-budgets", tuple(changes), "provider-startup", None, None, "NONE", "CLEAR", True)
            yield None
        totals = {key: [0] * (6 if self._text else 3) for key in self._accounts}
        async for reservation in self._pages("reservations"):
            account_id = cast(str, reservation["account_id"])
            if account_id not in totals:
                raise InvalidData()
            totals[account_id][0] += 1
            totals[account_id][1] += cast(int, reservation["known_subtotal_atoms"])
            totals[account_id][2] += cast(int, reservation["held_atoms"])
            if self._text:
                for index, name in enumerate(('quota_reserved', 'quota_known', 'quota_held'), 3):
                    totals[account_id][index] += cast(int, reservation[name])
            yield None
        yield None
        for account_id, policy in self._accounts.items():
            budget = await self._ledger.get("budget_windows", budget_key(policy))
            yield None
            if budget is None or totals[account_id] != [budget[name] for name in
                    (('attempt_count', 'known_subtotal_atoms', 'held_atoms', 'quota_reserved', 'quota_known', 'quota_held') if self._text else ('attempt_count', 'known_subtotal_atoms', 'held_atoms'))]:
                raise InvalidData()
            if self._text and budget['risk_state'] != 'CLEAR':
                self._text_admission_blocked = True
        async for request in self._pages("requests"):
            if self._text and (request['phase'] == 'REMOTE_RESULT_UNKNOWN'
                    or request['first_error'] is not None and as_record(request['first_error'])['reason'] in ('AUTHENTICATION_FAILED', 'MODEL_BINDING_MISMATCH')):
                self._text_admission_blocked = True
            attempts = await self._ledger.read("attempts_for_request", {"request_id": request["object_id"]})
            yield None
            if len(attempts) != request["attempt_count"] or tuple(item["ordinal"] for item in attempts) != tuple(range(1, len(attempts)+1)):
                raise InvalidData()
            for attempt in attempts:
                reservation = await self._ledger.get("reservations", cast(str, attempt["object_id"]))
                yield None
                if reservation is None or reservation["account_id"] != request["account_id"]:
                    raise InvalidData()
                usage = as_record(attempt["usage"])
                if self._text and attempt['wire_protocol']!=self._profiles[cast(str,request['profile_id'])]['wire_protocol']:raise InvalidData()
                if self._text and (attempt['state'] in ('PREPARED', 'REMOTE_RESULT_UNKNOWN') or not continuation_ready(usage)):
                    self._text_admission_blocked = True
                if any(reservation[name] != usage[name] for name in ("known_subtotal_atoms", "held_atoms", "cost_complete", "known_cost_atoms")):
                    raise InvalidData()
                if any(attempt[name] != request[name] for name in ("account_id", "profile_id", "capability")):
                    raise InvalidData()
                if attempt["evidence_revision"]:
                    for part in cast(tuple[Data, ...], usage["items"]):
                        part = as_record(part)
                        cost = await self._ledger.get("cost_items", derived_id("cost",attempt["object_id"],part["item"]))
                        yield None
                        if cost is None or any(cost[name] != part[name] for name in (('quantity', 'price_numerator', 'price_denominator', 'cost_atoms') if self._text else ('quantity', 'price_atoms', 'cost_atoms'))) or cost["evidence_revision"] != attempt["evidence_revision"]:
                            raise InvalidData()
                    reported = await self._ledger.get("cost_items", derived_id("cost",attempt["object_id"],"reported"))
                    yield None
                    if reported is None or reported["cost_atoms"] != usage["reported_cost_atoms"]:
                        raise InvalidData()
            if request["phase"] == "TERMINAL":
                if self._text:
                    from .text_stored_schema import terminal_reason
                    terminal_reason(request, attempts)
                if request["outcome"] == "SUCCEEDED":
                    await self._handoff(request, attempts)
                    yield None
                continue
            if attempts and attempts[-1]["state"] == "PREPARED":
                attempt = attempts[-1]
                unknown = revise(attempt, updated_at=self._utc(), state="REMOTE_RESULT_UNKNOWN", ever_unknown=True, confirmed_started=None,
                                 first_error=attempt["first_error"] or MappingProxyType({"code": "PERSISTENCE_FAILED", "field": "ledger", "reason": "LEDGER_UNCONFIRMED"}))
                updated = revise(request, updated_at=self._utc(), phase="REMOTE_RESULT_UNKNOWN", ever_unknown=True, first_error=request["first_error"] or unknown["first_error"])
                await self._commit("recover", "recover-"+cast(str, attempt["object_id"]),
                                   (Mutation("requests", request, updated), Mutation("attempts", attempt, unknown)), "provider-startup",
                                   cast(str, request["object_id"]), cast(str, attempt["object_id"]), "OPEN", "REMOTE_RESULT_UNKNOWN", False)
                yield None
            elif request["phase"] == "OPEN":
                outcome = "FAILED" if not attempts else cast(str, attempts[-1]["logical_outcome"])
                updated = revise(request, updated_at=self._utc(), phase="TERMINAL", outcome=outcome)
                await self._commit("terminate", "recover-terminal-"+cast(str, request["object_id"]), (Mutation("requests", request, updated),),
                                   "provider-startup", cast(str, request["object_id"]), None, "OPEN", "TERMINAL", True)
                yield None

    def _issue[P: WorkPort | ObserverPort | ResultOwnerPort](self, kind: type[P], grant: WorkGrant | ObserverGrant | ResultGrant) -> P:
        registrations = sum(type(port) is WorkPort for port in self._ports) if self._text else len(self._ports)
        if self._registration_limit is not None and (not self._text or kind is WorkPort) and registrations >= self._registration_limit:
            raise ValueError('Native Provider capability capacity is occupied.')
        port = object.__new__(kind)
        object.__setattr__(port, "_service", self)
        self._ports[port] = grant
        return port

    @staticmethod
    def _ids(values: object, nonempty: bool = False) -> bool:
        return type(values) is tuple and len(values) <= 1024 and (bool(values) or not nonempty) and all(is_identifier(value) for value in values) and len(set(values)) == len(values)

    def bind_work(self, grant: WorkGrant) -> WorkPort:
        """Trusted assembly assigns real identity, permissions and business sources."""
        if (type(grant) is not WorkGrant or any(not is_identifier(value) for value in (grant.caller_module, grant.caller_scope, grant.result_owner, grant.actor_ref))
                or (grant.extension_id is not None and not is_identifier(grant.extension_id)) or grant.task_role not in ROLES
                or type(grant.internal_dream) is not bool or not self._ids(grant.profiles, True) or not self._ids(grant.run_ids, True)
                or not self._ids(grant.capabilities, True) or any(value not in CAPABILITIES.values() for value in grant.capabilities)
                or any(not self._ids(values) for values in (grant.entry_ids, grant.batch_ids, grant.dream_run_ids, grant.parent_request_ids, grant.trace_ids, grant.prompt_revisions))):
            raise ValueError("A complete native work grant is required.")
        if self._text and (grant.task_role not in ('LEARNING', 'PERSONA') or grant.capabilities != ('GENERATION',)):
            raise ValueError('Only the configured text generation roles can receive work authority.')
        return self._issue(WorkPort, grant)

    def bind_observer(self, grant: ObserverGrant) -> ObserverPort:
        """Trusted assembly grants finite visible scopes and optional account sums."""
        if type(grant) is not ObserverGrant or not self._ids(grant.caller_scopes, True) or not self._ids(grant.account_ids):
            raise ValueError("A complete native observer grant is required.")
        return self._issue(ObserverPort, grant)

    def bind_result_owner(self, grant: ResultGrant) -> ResultOwnerPort:
        """Trusted ownership authority grants only explicit request handoffs."""
        if type(grant) is not ResultGrant or not is_identifier(grant.owner_id) or not self._ids(grant.request_ids, True):
            raise ValueError("A complete native result grant is required.")
        return self._issue(ResultOwnerPort, grant)

    def revoke(self, port: object) -> None:
        """Trusted authority revokes an issued capability; role text cannot undo it."""
        if type(port) in (WorkPort, ObserverPort, ResultOwnerPort):
            self._ports.pop(port, None)

    def work_consumers_ended(self, port: WorkPort) -> bool:
        """Report only this issued capability's actual tasks; no commit inference."""
        if type(port) is not WorkPort or port not in self._ports: return False
        return not any(job.port is port for job in self._jobs) and not any(owner is port for owner in self._lookup_owners.values())

    def bind_stored_media_authority(self, media: object, caller_scope: str, result_owner: str) -> StoredMediaAuthority:
        """Trusted assembly binds the actual same-database media owner exclusively."""
        from companion_memory.media.service import MediaService
        from .stored_media import StoredMediaAuthority
        if (self._state != 'READY' or type(media) is not MediaService or self._binding is None
                or not media.matches_provider(self._ledger.storage, self._ledger.database_id, caller_scope) or result_owner != 'media'
                or len(self._stored_authorities) >= media.settings.integer('media.processing_concurrency')):
            raise ValueError('The native media owner, database, scope and result owner must match.')
        authority = object.__new__(StoredMediaAuthority)
        for name, value in (('_provider', self), ('_media', media), ('_scope', caller_scope), ('_owner', result_owner)):
            object.__setattr__(authority, name, value)
        self._stored_authorities[id(authority)] = authority
        return authority

    async def _authorize_stored(self, authority: StoredMediaAuthority, work_id: object, occurrence_id: object) -> StoredMediaAuthorized | MediaError:
        from companion_memory.media.processing_bytes import read_processing_bytes, ProcessingBytes
        from companion_memory.media.service import MediaError
        from .stored_media import StoredMediaAuthorized
        if self._state != 'READY': return MediaError('INVALID_STATE', 'authorize_stored_media', 'state', 'NOT_READY')
        if not is_identifier(work_id) or not is_identifier(occurrence_id):
            return MediaError('ACCESS_DENIED', 'authorize_stored_media', 'capability', 'BINDING_MISMATCH')
        for handle, (binding, work, occurrence) in self._stored_media.items():
            if binding is authority and work == work_id and occurrence == occurrence_id:
                return StoredMediaAuthorized(handle, handle._artifact)
        pending_key = (id(authority), cast(str, work_id), cast(str, occurrence_id))
        if pending_key in self._stored_pending or len(self._stored_media) + len(self._stored_pending) >= authority._media.settings.integer('media.processing_concurrency'):
            return MediaError('RESOURCE_BUSY', 'authorize_stored_media', 'state', 'ADMISSION_FULL')
        self._stored_pending.add(pending_key)
        completion = CompletionScope()
        def ended() -> None:
            self._stored_pending.discard(pending_key)
            self._finish_close()
        try:
            with completion:
                read = await authority._media.read_processing(work_id, occurrence_id)
        finally:
            # Caller cancellation can precede the logical read result. Its native
            # descendants still notify this owner after their actual completion.
            completion.when_ended(ended)
        data = read.result
        if type(data) is MediaError: return data
        assert type(data) is ProcessingBytes
        if self._state != 'READY' or self._stored_authorities.get(id(authority)) is not authority:
            return MediaError('INVALID_STATE', 'authorize_stored_media', 'state', 'SERVICE_CLOSED')
        handle = object.__new__(AuthorizedMedia)
        for name, value in (('_issuer', self), ('_scope', authority._scope), ('_owner', authority._owner),
                ('_artifact', data.artifact_id), ('_content', data.content), ('_modality', data.modality)):
            object.__setattr__(handle, name, value)
        self._media[handle] = None
        self._stored_media[handle] = authority, cast(str, work_id), cast(str, occurrence_id)
        return StoredMediaAuthorized(handle, data.artifact_id)

    def authorize_media(self, caller_scope: str, owner_id: str, artifact_id: str, content: bytes, modality: str) -> AuthorizedMedia:
        """Issue one exact synthetic byte source; no paths, URLs or blob-store writes."""
        if (any(not is_identifier(item) for item in (caller_scope, owner_id, artifact_id)) or type(content) is not bytes
                or not 1 <= len(content) <= 1048576 or type(modality) is not str or modality not in ("IMAGE", "AUDIO", "VIDEO")):
            raise ValueError("A bounded native synthetic media source is required.")
        if not self._settings or len(self._media) >= self._number('max_in_flight'):
            raise ValueError('Native media authorization capacity is occupied.')
        media = object.__new__(AuthorizedMedia)
        for name, value in (("_issuer", self), ("_scope", caller_scope), ("_owner", owner_id), ("_artifact", artifact_id), ("_content", content), ("_modality", modality)):
            object.__setattr__(media, name, value)
        self._media[media] = None
        return media

    def _access(self, port: object, operation: str, allowed: tuple[type, ...]) -> ProviderError | None:
        if type(port) not in allowed or port not in self._ports:
            return error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH")
        if (issue := self._state_error(operation)) is not None:
            return issue
        grant = self._ports[port]
        if type(grant) is WorkGrant and self._resources is not None:
            try:
                if not self._res.gate.authorized(grant):
                    return error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH")
            except MemoryError:
                raise
            except Exception:
                return error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH")
        return self._state_error(operation)

    def _inspect(self, port: object) -> Record:
        if type(port) not in (WorkPort, ObserverPort) or port not in self._ports:
            raise PermissionError("An issued provider capability is required.")
        grant = self._ports[port]
        profiles = tuple(value for key, value in sorted(self._profiles.items()) if type(grant) is not WorkGrant or key in grant.profiles)
        if self._text_configuration is not None:
            configuration = self._text_configuration
            return MappingProxyType({'source': 'REMOTE_PROVIDER', 'configuration_origin': 'PERSISTED_CONFIGURATION',
                'config_snapshot_id': configuration.snapshot_id,
                'profile_revision': derived_id('profile', configuration.snapshot_id, profiles[0]['profile_id']) if profiles else None,
                'price_revision': as_record(next(iter(self._accounts.values()))['price'])['revision_ref'],
                'profiles': profiles, 'streaming': False, 'tools': False})
        return MappingProxyType({"source": "SIMULATED", "configuration_origin": "UNVERSIONED_CONFIGURATION", "config_snapshot_id": None,
                                 "profile_revision": None, "price_revision": None, "profiles": profiles, "streaming": False, "tools": False})

    def _normalize(self, raw: object, grant: WorkGrant, operation: str, started: float) -> tuple[Record, CancellationToken, float]:
        if type(raw) is not dict or any(type(name) is not str for name in raw):
            raise InvalidData()
        required = {"operation_key", "run_id", "profile_id", "payload", "deadline", "cancellation"}
        if not required <= set(raw) or set(raw)-required-set(OPTIONALS)-{"entry_ids"}:
            raise InvalidData()
        token, deadline = raw["cancellation"], raw["deadline"]
        if (not native_issued(token, CancellationToken) or type(object.__getattribute__(token, "_event")) is not threading.Event
                or type(deadline) not in (float, int)
                or type(deadline) is int and not -MAX_INTEGER <= deadline <= MAX_INTEGER or not math.isfinite(deadline)):
            raise InvalidData()
        token = cast(CancellationToken, token)
        bounded_deadline = min(float(deadline), started+self._number("request_timeout_ms")/1000)
        def checkpoint():
            try:
                observed = self._now()
            except MemoryError:
                raise
            except Exception:
                raise _AdmissionStopped(error("RESOURCE_FAILED", operation, "state", "RESOURCE_FAILURE")) from None
            if observed >= bounded_deadline:
                raise _AdmissionStopped(error("TIMEOUT", operation, "request", "DEADLINE_EXCEEDED"))
            if token.cancelled:
                raise _AdmissionStopped(error("CANCELLED", operation, "request", "CANCEL_REQUESTED"))
        payload = raw["payload"]
        if operation == "understand_media":
            if type(payload) is not dict or any(type(name) is not str for name in payload):
                raise InvalidData()
            media = payload.get("media")
            if type(media) is not AuthorizedMedia or media not in self._media or media._scope != grant.caller_scope or payload.get("modality") != media._modality:
                raise PermissionError()
            payload = {**payload, "media": {"artifact_id": media._artifact, "owner_id": media._owner, "scope": media._scope,
                                          "byte_count": len(media._content), "sha256": hashlib.sha256(media._content).hexdigest()}}
        body = {name: raw.get(name) for name in OPTIONALS}
        body.update({name: raw[name] for name in ("operation_key", "run_id", "profile_id")})
        body.update(payload=payload, entry_ids=raw.get("entry_ids", ()))
        request = as_record(freeze(body, 1048576, checkpoint=checkpoint))
        if any(not is_identifier(request[name]) for name in ("operation_key", "run_id", "profile_id")):
            raise InvalidData()
        if any(request[name] is not None and not is_identifier(request[name]) for name in OPTIONALS):
            raise InvalidData()
        validate_attribution(request, grant)
        self._input_units(CAPABILITIES[operation], as_record(request["payload"]), role=grant.task_role)
        return request, cast(CancellationToken, token), float(deadline)

    async def _work(self, port: object, operation: str, raw: object):
        admitted=self._admit_work(port,operation,raw)
        return await self._wait_work(admitted) if type(admitted) is _Job else admitted

    async def _send_verified_first(self,port: object,evidence: object,raw: object):
        """Consume one original absence seal under registration exclusion.

        Admission retains the real job before releasing the serial gate. A
        concurrent consumer cannot observe a gap between the seal and the job.
        The original absolute deadline is an upper bound, never renewed here.
        """
        from .unsent_evidence import VerifiedUnsent,issued_unsent
        if type(evidence) is not VerifiedUnsent or not issued_unsent(evidence):
            return Rejected(error('ACCESS_DENIED','embed','capability','CAPABILITY_MISMATCH'))
        if self._serial.locked():
            return Rejected(error('RESOURCE_BUSY','embed','state','ADMISSION_BUSY'))
        async with self._serial:
            admitted=self._admit_work(port,'embed',raw,first_send=evidence)
        return await self._wait_work(admitted) if type(admitted) is _Job else admitted

    def _admit_work(self,port: object,operation: str,raw: object,*,first_send: object=None):
        """Retain one admitted worker synchronously, before any registration I/O."""
        if (issue := self._access(port, operation, (WorkPort,))) is not None:
            return Rejected(issue)
        grant = cast(WorkGrant, self._ports[port])
        if self._text_admission_blocked:
            # Original local confirmation remains available through lookup and
            # result-owner recovery; this check never creates a new request row.
            return Rejected(error('PAUSED_BUDGET', operation, 'budget', 'BILLING_EVIDENCE_MISSING'))
        if CAPABILITIES[operation] not in grant.capabilities:
            return Rejected(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
        try:
            start = self._now()
        except MemoryError:
            raise
        except Exception:
            return Rejected(error("RESOURCE_FAILED", operation, "state", "RESOURCE_FAILURE"))
        try:
            request, token, requested_deadline = self._normalize(raw, grant, operation, start)
        except _AdmissionStopped as failure:
            return Rejected(failure.cause)
        except PermissionError:
            return Rejected(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
        except InvalidData as failure:
            return Rejected(error("INVALID_INPUT", operation, "request", "LIMIT_EXCEEDED" if type(failure) is DataLimit else "INVALID_SHAPE"))
        except MemoryError:
            raise
        except Exception:
            return Rejected(error("RESOURCE_FAILED", operation, "state", "RESOURCE_FAILURE"))
        if request["profile_id"] not in grant.profiles:
            return Rejected(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
        from .unsent_evidence import request_key,issued_unsent,VerifiedUnsent
        original_key=request_key(grant,request)
        if first_send is not None:
            if (type(first_send) is not VerifiedUnsent or not issued_unsent(first_send)
                    or first_send._provider is not self or self._unsent_evidence.get(original_key) is not first_send
                    or first_send.conclusion!='REGISTRATION_ABSENT' or first_send.request_id is not None
                    or first_send.original_request!=request or first_send.capability!='EMBEDDING'
                    or first_send.result_owner!=grant.result_owner or requested_deadline>first_send.deadline):
                return Rejected(error('ACCESS_DENIED',operation,'capability','CAPABILITY_MISMATCH'))
        elif original_key in self._unsent_evidence:
            return Rejected(error('MODE_BLOCKED', operation, 'state', 'ORIGINAL_ADMISSION_CLOSED'))
        if len(self._jobs) >= self._number("max_in_flight"):
            return Rejected(error("RESOURCE_BUSY", operation, "state", "ADMISSION_BUSY"))
        deadline = min(requested_deadline, start+self._number("request_timeout_ms")/1000)
        try:
            expired = self._now() >= deadline
        except MemoryError:
            raise
        except Exception:
            return Rejected(error("RESOURCE_FAILED", operation, "state", "RESOURCE_FAILURE"))
        if expired:
            return Rejected(error("TIMEOUT", operation, "request", "DEADLINE_EXCEEDED"))
        if token.cancelled:
            return Rejected(error("CANCELLED", operation, "request", "CANCEL_REQUESTED"))
        try:
            job = _Job(operation, grant, request, token, deadline, self._id())
        except MemoryError:
            raise
        except Exception:
            return Rejected(error("RESOURCE_FAILED", operation, "state", "RESOURCE_FAILURE"))
        job.port = port
        if operation == 'understand_media' and type(raw) is dict:
            submitted = raw.get('payload')
            if type(submitted) is dict and submitted.get('media') in self._stored_media:
                job.media_authorization = submitted['media']
        job.publication = asyncio.get_running_loop().create_future()
        self._jobs.add(job)
        if first_send is not None:del self._unsent_evidence[original_key]
        job.task = asyncio.create_task(self._drive_owned(job))
        job.task.add_done_callback(lambda task: self._job_finished(job, task))
        return job

    async def _wait_work(self,job: _Job):
        """Wait within the original deadline while the job retains actual cleanup."""
        execution = job.task
        assert execution is not None and job.publication is not None
        deadline=job.deadline;operation=job.operation
        try:
            done, _ = await asyncio.wait((execution, job.publication), timeout=max(0, deadline-self._now()), return_when=asyncio.FIRST_COMPLETED)
            if job.publication in done:
                return job.publication.result()
            if execution in done:
                return execution.result()
            raise TimeoutError()
        except TimeoutError:
            job.stop.set()
            job.first_error = job.first_error or error("TIMEOUT", operation, "request", "DEADLINE_EXCEEDED", True)
            return self._pending(job, "REMOTE_RESULT_UNKNOWN" if job.unknown else "LOCAL_COMMIT_UNCONFIRMED")
        except MemoryError:
            raise
        except asyncio.CancelledError:
            job.stop.set()
            job.first_error = job.first_error or error("CANCELLED", operation, "request", "CANCEL_REQUESTED", True)
            raise
        except Exception:
            job.stop.set()
            job.first_error = job.first_error or error("RESOURCE_FAILED", operation, "state", "RESOURCE_FAILURE", True)
            return self._pending(job, "LOCAL_COMMIT_UNCONFIRMED")

    def _job_finished(self, job: _Job, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()
        if self._owns_work(job):
            job.task = asyncio.create_task(self._retain_worker(job))
            return
        self._jobs.discard(job)
        self._finish_close()

    def _owns_work(self, job: _Job) -> bool:
        return (job.worker is not None and job.worker.is_alive()) or job.completion.pending

    async def _retain_initialization_cleanup(self) -> None:
        await self._init_completion.wait()

    def _observe_write_cleanup(self, result: object, owner: _Job | None) -> bool:
        """Retain local ownership independently of committed/absent evidence."""
        health = self._ledger.storage.get_health()
        pending = getattr(getattr(result, "error", None), "cleanup_pending", False) is True or (owner.completion.pending if owner is not None else self._init_completion.pending)
        if pending:
            if owner is not None:
                owner.local_write_pending = True
            elif self._initialization_cleanup is None or self._initialization_cleanup.done():
                self._initialization_cleanup = asyncio.create_task(self._retain_initialization_cleanup())
                self._initialization_cleanup.add_done_callback(lambda task: self._finish_close())
        if type(result) is Committed and health.lifecycle != "READY":
            self._fault("RESOURCE_FAILURE")
            if owner is not None:
                owner.first_error = owner.first_error or error("RESOURCE_FAILED", owner.operation, "state", "RESOURCE_FAILURE", pending)
        return pending

    async def _retain_worker(self, job: _Job) -> None:
        while self._owns_work(job):
            await asyncio.sleep(0.01)
        self._jobs.discard(job)
        self._finish_close()

    def _reference(self, request_id: str, operation_key: Data) -> Record:
        return MappingProxyType({"database_id": self._ledger.database_id, "request_id": request_id, "operation_key": operation_key})

    def _pending(self, job: _Job, observation: str) -> Pending:
        return Pending(self._reference(job.request_id, job.request["operation_key"]), observation, job.first_error)

    def _semantic(self, job: _Job, evidence: Data) -> str:
        identity = MappingProxyType({"capability": CAPABILITIES[job.operation], "caller_module": job.grant.caller_module,
                                            "caller_scope": job.grant.caller_scope, "extension_id": job.grant.extension_id, "task_role": job.grant.task_role,
                                            "result_owner": job.grant.result_owner})
        return self._original_fingerprint(job.request, identity, evidence)

    def _original_fingerprint(self, request: Record, identity: Record, evidence: Data) -> str:
        values = {'request': request, 'execution_evidence': evidence,
            **{name: identity[name] for name in ('capability', 'caller_module', 'caller_scope', 'extension_id', 'task_role', 'result_owner')}}
        if self._text_configuration is not None:
            values.update(format_version=2, config_snapshot_id=self._text_configuration.snapshot_id,
                profile_revision=derived_id('profile', self._text_configuration.snapshot_id, request['profile_id']))
        return fingerprint(MappingProxyType(values), 2097152)

    async def _commit(self, kind: str, key: str, changes: tuple[Mutation, ...], actor: str, request_id: str | None,
                      attempt_id: str | None, before: str, after: str, complete: bool, *, owner: _Job | None = None) -> Committed:
        if self._ledger_faulted:
            raise _WriteFailure("LEDGER_UNCONFIRMED", True)
        key = derived_id(kind,key)
        result, command = await self._ledger.mutate(kind, key, changes, actor, request_id, attempt_id, before, after, complete)
        from companion_memory.persistence import Rejected as StorageRejected
        admission_deadline = owner.deadline if owner is not None else self._now() + self._number('request_timeout_ms') / 1000
        while (type(result) is StorageRejected and result.error.code == 'RESOURCE_BUSY'
                and result.error.reason == 'ADMISSION_BUSY' and self._now() < admission_deadline):
            # This native rejection occurs before the writer is admitted. Keep
            # the bounded owner and retry only the unchanged local command.
            await asyncio.sleep(min(0.01, max(0, admission_deadline - self._now())))
            if self._now() >= admission_deadline: break
            result = await self._ledger.operations[kind].execute(key, command)
        cleanup_pending = self._observe_write_cleanup(result, owner)
        if self._text and type(result) is NotCommitted and self._ledger.storage.get_health().lifecycle == 'READY' and self._now() < admission_deadline:
            # The original local transaction is reliably absent. At most one
            # unchanged local replay is allowed; the transport is never invoked.
            if cleanup_pending:
                if owner is not None: await owner.completion.wait()
                elif self._initialization_cleanup is not None: await asyncio.shield(self._initialization_cleanup)
            if self._now() < admission_deadline:
                result = await self._ledger.operations[kind].execute(key, command)
                cleanup_pending = self._observe_write_cleanup(result, owner)
        if (type(result) is StorageRejected and result.error.code == 'RESOURCE_BUSY'
                and result.error.reason == 'ADMISSION_BUSY'):
            raise _WriteFailure('LEDGER_ADMISSION_BUSY', cleanup_pending=cleanup_pending)
        if type(result) is Committed:
            return result
        if type(result) is Unconfirmed:
            self._fault("LEDGER_UNCONFIRMED")
            # Confirmation never invokes the handler. It cannot authorize a new
            # execution owner, and a failed confirmation never becomes a resend.
            if owner is not None: await owner.completion.wait()
            elif self._initialization_cleanup is not None: await asyncio.shield(self._initialization_cleanup)

            confirmed = await self._ledger.operations[kind].resolve_operation(result.recovery_handle)
            cleanup_pending = self._observe_write_cleanup(confirmed, owner)
            if type(confirmed) is Committed:
                return confirmed
            if type(confirmed) is NotCommitted:
                raise _WriteFailure("LEDGER_NOT_COMMITTED", cleanup_pending=cleanup_pending)
            raise _WriteFailure("LEDGER_UNCONFIRMED", True, cleanup_pending)
        raise _WriteFailure("LEDGER_NOT_COMMITTED" if type(result) is NotCommitted else "LEDGER_REJECTED", cleanup_pending=cleanup_pending)

    async def _drive_owned(self, job: _Job):
        with job.completion:
            return await self._drive(job)

    async def _drive(self, job: _Job):
        try:
            async with self._serial:
                old = await self._ledger.read("requests_find", {"caller_scope": job.grant.caller_scope, "caller_module": job.grant.caller_module,
                                                               "extension_id": job.grant.extension_id or "", "operation_key": job.request["operation_key"]})
                if old:
                    request = old[0]
                    if request["fingerprint"] != self._semantic(job, request["execution_evidence"]):
                        return Rejected(error("IDEMPOTENCY_CONFLICT", job.operation, "request", "CONTENT_MISMATCH"))
                    job.request_id = cast(str, request["object_id"])
                    return await self._present(request, "EXISTING")
                profile = self._profiles.get(cast(str, job.request["profile_id"]))
                roles = as_record(self._settings["role_profiles"])
                if (profile is None or job.request["profile_id"] not in job.grant.profiles or job.request["profile_id"] not in cast(tuple[Data, ...], roles.get(job.grant.task_role, ()))
                        or profile["capability"] != CAPABILITIES[job.operation]):
                    return await self._blocked(job, "UNSUPPORTED_CAPABILITY", error("UNSUPPORTED_CAPABILITY", job.operation, "configuration", "PROFILE_NOT_AVAILABLE"), None)
                freeze(job.request, self._number("request_max_bytes"), owned=True)
                units, output, unsupported = self._input_units(CAPABILITIES[job.operation], as_record(job.request["payload"]), profile, job.grant.task_role)
                if unsupported:
                    return await self._blocked(job, "UNSUPPORTED_CAPABILITY", error("UNSUPPORTED_CAPABILITY", job.operation, "capability", unsupported), profile)
                account_id = cast(str, profile["account_id"])
                if sum(item.account_id == account_id for item in self._jobs) >= cast(int, self._accounts[account_id]["max_in_flight"]):
                    return Rejected(error("RESOURCE_BUSY", job.operation, "state", "ADMISSION_BUSY"))
                job.account_id = account_id
                gate = self._gate(job)
                if gate is not None:
                    return await self._blocked(job, "MODE_BLOCKED", gate, profile)
                stopped = self._stopped(job)
                if stopped is not None:
                    return await self._blocked(job, "CANCELLED" if stopped.code == "CANCELLED" else "TIMED_OUT", stopped, profile)
                if self._text:
                    from .text_accounting import liability
                    amount, _ = liability(self._accounts[account_id], profile)
                else:
                    amount = units*cast(int, profile["input_price_atoms"])+output*cast(int, profile["output_price_atoms"])
                budget = await self._budget(profile)
                if (reason := self._check_budget(budget, amount)) is not None:
                    return await self._blocked(job, "PAUSED_BUDGET", error("PAUSED_BUDGET", job.operation, "budget", reason), profile)
                request = self._request_row(job, profile, "OPEN", None, None)
                await self._prepare(job, request, profile, budget, amount, initial=True)
            while True:
                if self._ledger_faulted:
                    return self._pending(job, "IN_PROGRESS")
                response, interrupted = await self._invoke(job, profile)
                if interrupted is not None:
                    async with self._serial:
                        await self._record_unknown(job, interrupted)
                    if job.publication is not None and not job.publication.done():
                        job.publication.set_result(self._pending(job, "REMOTE_RESULT_UNKNOWN"))
                    if response is None:
                        return self._pending(job, "REMOTE_RESULT_UNKNOWN")
                    # A timed-out worker retains this job and its account slot.
                    # Only this owner can publish its later evidence.
                    response = await cast(asyncio.Future, response)
                async with self._serial:
                    assert job.attempt is not None
                    outcome, cause, usage, payload = self._response(job, profile, response)
                    retry = (not job.unknown and outcome in ("TRANSIENT_FAILURE", "RATE_LIMITED") and usage["cost_complete"] is True
                             and cast(int, job.attempt["ordinal"]) < cast(int, profile["max_attempts"]) and self._stopped(job) is None)
                    await self._settle(job, profile, outcome, cause, usage, payload, retry)
                    if not retry:
                        if job.stored is not None and job.stored["phase"] == "TERMINAL":
                            return Completed(job.stored, payload, "NEW")
                        return await self._present(cast(Record, job.stored), "NEW")
                remaining = job.deadline-self._now()
                delay = self._number("retry_delay_ms")/1000
                if delay:
                    try:
                        await asyncio.wait_for(job.stop.wait(), min(delay, max(remaining, 0)))
                    except TimeoutError:
                        pass
                while job.worker is not None and job.worker.is_alive():
                    await asyncio.sleep(0.001)
                async with self._serial:
                    stopped = self._stopped(job)
                    gate = self._gate(job) if stopped is None else None
                    budget = await self._budget(profile)
                    reason = self._check_budget(budget, amount)
                    if stopped or gate or reason:
                        cause = stopped or gate or error("PAUSED_BUDGET", job.operation, "budget", cast(str, reason))
                        terminal = "CANCELLED" if cause.code == "CANCELLED" else "TIMED_OUT" if cause.code == "TIMEOUT" else cause.code
                        await self._terminate(job, terminal, cause)
                        return await self._present(cast(Record, job.stored), "NEW")
                    await self._prepare(job, cast(Record, job.stored), profile, budget, amount, initial=False)
        except _EvidenceConflict:
            self._fault("CONTENT_MISMATCH")
            cause = error("IDEMPOTENCY_CONFLICT", job.operation, "request", "CONTENT_MISMATCH")
            return Pending(self._reference(job.request_id, job.request["operation_key"]), "REMOTE_RESULT_UNKNOWN", job.first_error or cause)
        except InvalidData as failure:
            if job.stored is None:
                return Rejected(error("INVALID_INPUT", job.operation, "payload", "LIMIT_EXCEEDED" if type(failure) is DataLimit else "INVALID_SHAPE"))
            self._fault("LEDGER_INCONSISTENT")
            job.first_error = job.first_error or error("PERSISTENCE_FAILED", job.operation, "ledger", "LEDGER_INCONSISTENT")
            return self._pending(job, "LOCAL_COMMIT_UNCONFIRMED")
        except LedgerFailure as failure:
            job.local_read_pending = getattr(getattr(failure.result,"error",None),"cleanup_pending",False) is True
            if job.stored is not None:
                self._fault("LEDGER_READ_FAILED")
            cause = job.first_error or error("PERSISTENCE_FAILED", job.operation, "ledger", "LEDGER_READ_FAILED")
            return Rejected(cause) if job.stored is None else Pending(self._reference(job.request_id, job.request["operation_key"]), "LOCAL_COMMIT_UNCONFIRMED", cause)
        except _WriteFailure as failure:
            if failure.reason == 'LEDGER_ADMISSION_BUSY' and job.stored is None and job.worker is None:
                return Rejected(error('RESOURCE_BUSY', job.operation, 'ledger', 'ADMISSION_BUSY', failure.cleanup_pending))
            self._fault(failure.reason)
            job.first_error = job.first_error or error("PERSISTENCE_FAILED", job.operation, "ledger", failure.reason, failure.cleanup_pending)
            if failure.cleanup_pending:
                job.first_error = replace(job.first_error, cleanup_pending=True)
            return self._pending(job, "LOCAL_COMMIT_UNCONFIRMED") if job.stored is not None or failure.uncertain else Rejected(job.first_error)
        except MemoryError:
            raise
        except Exception:
            self._fault("RESOURCE_FAILURE")
            job.first_error = job.first_error or error("RESOURCE_FAILED", job.operation, "state", "RESOURCE_FAILURE", job.worker is not None)
            return Rejected(job.first_error) if job.stored is None and job.worker is None else self._pending(job, "REMOTE_RESULT_UNKNOWN" if job.worker is not None else "LOCAL_COMMIT_UNCONFIRMED")

    def _fault(self, reason: str) -> None:
        self._ledger_faulted = True
        if self._state != "CLOSING":
            self._state = "FAULTED"
        self._reason = self._reason or reason

    def _stopped(self, job: _Job) -> ProviderError | None:
        if job.token.cancelled or job.stop.is_set() or self._state != "READY" or job.port not in self._ports:
            return error("CANCELLED", job.operation, "request", "CANCEL_REQUESTED")
        if self._now() >= job.deadline:
            return error("TIMEOUT", job.operation, "request", "DEADLINE_EXCEEDED")
        return None

    def _gate(self, job: _Job) -> ProviderError | None:
        try:
            if self._res.gate.check(job.grant):
                return None
            return error("MODE_BLOCKED", job.operation, "gate", "GATE_DENIED")
        except MemoryError:
            raise
        except Exception:
            return error("MODE_BLOCKED", job.operation, "gate", "GATE_UNAVAILABLE")

    async def _budget(self, profile: Record) -> Record:
        result = await self._ledger.get("budget_windows", budget_key(self._accounts[cast(str, profile["account_id"])]))
        if result is None:
            raise InvalidData()
        return result

    def _request_row(self, job: _Job, profile: Record | None, phase: str, outcome: str | None, cause: ProviderError | None) -> Record:
        evidence = MappingProxyType({"profile": profile, "account": self._accounts[cast(str, profile["account_id"]) ] if profile else None,
                                     "request_timeout_ms": self._number("request_timeout_ms"), "retry_delay_ms": self._number("retry_delay_ms"),
                                     "request_max_bytes": self._number("request_max_bytes"), "result_max_bytes": self._number("result_max_bytes")})
        return row(job.request_id, caller_module=job.grant.caller_module, caller_scope=job.grant.caller_scope, extension_id=job.grant.extension_id,
                   operation_key=job.request["operation_key"], capability=CAPABILITIES[job.operation], task_role=job.grant.task_role, result_owner=job.grant.result_owner, format_version=2 if self._text else 1, fingerprint_version=2 if self._text else 1, updated_at=self._utc(),
                   profile_id=job.request["profile_id"], account_id=profile["account_id"] if profile else None, created_at=self._utc(),
                   attribution=MappingProxyType({name: job.request[name] for name in ("run_id", "entry_ids", *OPTIONALS)}),
                   source="REMOTE_PROVIDER" if self._text else "SIMULATED", configuration_origin="PERSISTED_CONFIGURATION" if self._text else "UNVERSIONED_CONFIGURATION",
                   config_snapshot_id=self._text_configuration.snapshot_id if self._text_configuration is not None else None,
                   profile_revision=derived_id('profile', self._text_configuration.snapshot_id, job.request['profile_id']) if self._text_configuration is not None else None,
                   price_revision=as_record(next(iter(self._accounts.values()))['price'])['revision_ref'] if self._text else None,
                   execution_evidence=evidence, fingerprint=self._semantic(job, evidence), phase=phase, outcome=outcome, first_error=error_value(cause),
                   attempt_count=0, ever_unknown=False, handoff_id=None)

    async def _blocked(self, job: _Job, outcome: str, cause: ProviderError, profile: Record | None):
        request = self._request_row(job, profile, "TERMINAL", outcome, cause)
        await self._commit("register", "register-"+job.request_id, (Mutation("requests", None, request),), job.grant.actor_ref, job.request_id, None, "NONE", "TERMINAL", True, owner=job)
        job.stored = request
        return Completed(request, None, "NEW")

    async def _prepare(self, job: _Job, request: Record, profile: Record, budget: Record, amount: int, *, initial: bool) -> None:
        attempt_id = self._id()
        usage = self._usage({}, profile, amount)
        attempt = row(attempt_id, request_id=job.request_id, ordinal=cast(int, request["attempt_count"])+1, state="PREPARED", logical_outcome=None,
                      account_id=profile["account_id"], profile_id=profile["profile_id"], capability=profile["capability"], wire_protocol=profile["wire_protocol"] if self._text else "SIMULATED",
                      execution_owner_id=self._execution_owner, created_at=self._utc(), updated_at=self._utc(), adapter_duration_ms=None, handoff_id=None,
                      confirmed_started=None, ever_unknown=False, first_error=None, usage=usage, result_fingerprint=None, evidence_revision=0)
        if self._text:
            attempt = MappingProxyType({**attempt, 'terminal_error': None})
        reservation = row(attempt_id, attempt_id=attempt_id, account_id=profile["account_id"], budget_id=budget["object_id"], reserved_atoms=amount,
                          known_subtotal_atoms=0, held_atoms=amount, known_cost_atoms=None, cost_complete=False)
        if self._text:
            from .text_accounting import liability
            _, quota = liability(self._accounts[cast(str, profile['account_id'])], profile)
            reservation = as_record(freeze({**reservation, 'format_version': 2, 'quota_reserved': quota, 'quota_known': 0, 'quota_held': 0}, 8192, owned=True))
        updated = revise(request, updated_at=self._utc(), attempt_count=attempt["ordinal"]) if not initial else MappingProxyType({**request, "attempt_count": attempt["ordinal"]})
        changes = (Mutation("requests", None if initial else request, updated), Mutation("attempts", None, attempt),
                   Mutation("reservations", None, reservation), Mutation("budget_windows", budget, self._reserve_budget(budget, amount)))
        await self._commit("register" if initial else "prepare", "prepare-"+attempt_id, changes, job.grant.actor_ref, job.request_id, attempt_id,
                           "NONE" if initial else "OPEN", "OPEN", False, owner=job)
        job.stored, job.attempt, job.reserved = updated, attempt, amount

    async def _invoke(self, job: _Job, profile: Record):
        assert job.attempt is not None
        stopped = self._stopped(job)
        job.worker_completed_at, job.worker_response, job.worker_failure = None, None, None
        attempt_id = cast(str, job.attempt["object_id"])
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        interim = loop.create_future()
        began = False
        def finished(response: AdapterResponse | None):
            if not future.done():
                future.set_result(response)
        def observe(response: AdapterResponse):
            def publish():
                if not interim.done():
                    interim.set_result(response)
            loop.call_soon_threadsafe(publish)
        def target():
            try:
                job.worker_started_at = self._now()
            except MemoryError as failure:
                job.worker_failure = failure
                loop.call_soon_threadsafe(finished, None)
                return
            except Exception:
                job.worker_failure = RuntimeError()
                loop.call_soon_threadsafe(finished, None)
                return
            try:
                resources = self._res
                if type(resources) is RealGenerationResources:
                    response = resources.adapter.invoke(job.request, job.grant.task_role, job.token, attempt_deadline)
                else:
                    response = cast(ProviderResources, resources).adapter.invoke(attempt_id, job.request, job.token, observe)
            except MemoryError as failure:
                job.worker_failure = failure
                loop.call_soon_threadsafe(finished, None)
                return
            except Exception:
                response = None
            if type(self._res) is RealGenerationResources:
                try:
                    self._res.completed(attempt_id)
                except MemoryError:
                    raise
                except Exception:
                    response = None
            job.worker_response = response
            try:
                job.worker_completed_at = self._now()
            except MemoryError as failure:
                job.worker_failure = failure
            except Exception:
                job.worker_failure = RuntimeError()
            try:
                loop.call_soon_threadsafe(finished, response)
            except RuntimeError:
                pass
        def start():
            nonlocal began
            if began or self._stopped(job) is not None:
                return
            worker = threading.Thread(target=target, name="provider-adapter", daemon=True)
            job.worker = worker
            worker.start()
            began = True
        attempt_deadline = min(job.deadline, self._now()+cast(int, profile["attempt_timeout_ms"])/1000)
        gate_error = None
        if stopped is None:
            try:
                granted = self._res.gate.dispatch(job.grant, start)
                if not granted:
                    gate_error = error("MODE_BLOCKED", job.operation, "gate", "GATE_DENIED")
            except MemoryError:
                raise
            except Exception:
                gate_error = error("MODE_BLOCKED", job.operation, "gate", "GATE_UNAVAILABLE")
        if not began:
            cause = stopped or gate_error or self._stopped(job) or error("MODE_BLOCKED", job.operation, "gate", "GATE_DENIED")
            async with self._serial:
                await self._not_sent(job, profile, cause)
            return AdapterResponse("NOT_SENT", None, None), None
        while not future.done():
            if job.worker_failure is not None:
                raise job.worker_failure
            if interim.done():
                job.partial_response = interim.result()
                return future, error("ADAPTER_FAILED", job.operation, "adapter", "ADAPTER_EXCEPTION", True)
            if job.worker_completed_at is not None and job.worker_completed_at <= attempt_deadline:
                return job.worker_response, None
            stopped = self._stopped(job)
            if stopped or self._now() >= attempt_deadline:
                cause = stopped or error("TIMEOUT", job.operation, "adapter", "ATTEMPT_TIMEOUT", True)
                return future, cause
            await asyncio.wait((future, interim), timeout=min(0.01, max(0, attempt_deadline-self._now())))
        if job.worker_failure is not None:
            raise job.worker_failure
        if job.worker_completed_at is None or job.worker_completed_at > attempt_deadline:
            return future, error("TIMEOUT", job.operation, "adapter", "ATTEMPT_TIMEOUT", True)
        return future.result(), None

    async def _not_sent(self, job: _Job, profile: Record, cause: ProviderError) -> None:
        assert job.attempt is not None
        raw = {"coverage": "COMPLETE", "billing_input_units": 0, "billing_output_units": 0, "known_cost_atoms": 0}
        usage = self._usage(raw, profile, 0, not_sent=True)
        outcome = "CANCELLED" if cause.code == "CANCELLED" else "TIMED_OUT" if cause.code == "TIMEOUT" else "MODE_BLOCKED"
        await self._settle(job, profile, outcome, cause, usage, None, False, not_sent=True)

    async def _record_unknown(self, job: _Job, cause: ProviderError) -> None:
        assert job.attempt is not None and job.stored is not None
        job.first_error = job.first_error or cause
        if job.partial_response is not None:
            profile = as_record(as_record(job.stored["execution_evidence"])["profile"])
            outcome, observed_cause, usage, payload = self._response(job, profile, job.partial_response)
            await self._settle(job, profile, outcome, observed_cause, usage, payload, False)
            return
        request = revise(job.stored, updated_at=self._utc(), phase="REMOTE_RESULT_UNKNOWN", outcome=None, ever_unknown=True, first_error=job.stored["first_error"] or error_value(job.first_error))
        attempt = revise(job.attempt, updated_at=self._utc(), state="REMOTE_RESULT_UNKNOWN", ever_unknown=True, confirmed_started=True, first_error=job.attempt["first_error"] or error_value(job.first_error))
        await self._commit("recover", "unknown-"+cast(str, attempt["object_id"]), (Mutation("requests", job.stored, request), Mutation("attempts", job.attempt, attempt)),
                           job.grant.actor_ref, job.request_id, cast(str, attempt["object_id"]), "OPEN", "REMOTE_RESULT_UNKNOWN", False, owner=job)
        job.stored, job.attempt, job.unknown = request, attempt, True
        self._unknown += 1
        if self._text:
            self._text_admission_blocked = True

    def _response(self, job: _Job, profile: Record, response: object):
        assert job.attempt is not None
        if type(response) is AdapterResponse and response.outcome == "NOT_SENT":
            return "NOT_SENT", None, cast(Record, job.attempt["usage"]), None
        if self._text and type(response) is AdapterResponse and response.outcome == 'LOCAL_NOT_SENT':
            job.transport_not_sent = True
            return 'CONFIGURATION_REJECTED', error('RESOURCE_FAILED', job.operation, 'capability', 'RESOURCE_INVALID'), self._usage({}, profile, 0, not_sent=True), None
        amount = job.reserved
        usage = self._usage(response.usage if type(response) is AdapterResponse else {}, profile, amount)
        outcome = response.outcome if type(response) is AdapterResponse and response.outcome in OUTCOMES else "ADAPTER_EXCEPTION"
        if self._text and type(response) is AdapterResponse and response.outcome == 'OUTPUT_LIMIT':
            # A complete length response is a known business failure even when
            # its independent usage evidence cannot settle the held liability.
            return 'OUTPUT_LIMIT', error('ADAPTER_FAILED', job.operation, 'adapter', 'OUTPUT_LIMIT'), usage, None
        if self._text and type(response) is AdapterResponse and response.outcome == 'MODEL_BINDING_MISMATCH':
            return 'CONFIGURATION_REJECTED', error('CONFIGURATION_REJECTED', job.operation, 'configuration', 'MODEL_BINDING_MISMATCH'), usage, None
        payload = None
        if not usage["valid"]:
            outcome = "INVALID_RESPONSE"
        if outcome == "SUCCEEDED":
            try:
                payload = self._result_payload(CAPABILITIES[job.operation], cast(AdapterResponse, response).payload, job.request, profile, self._number("result_max_bytes"), job.grant.task_role)
            except InvalidData:
                outcome = "INVALID_RESPONSE"
        cause = None
        if outcome in ("SENSITIVE_REFUSAL", "OTHER_REFUSAL"):
            cause = error("MODEL_REFUSAL", job.operation, "adapter", "SENSITIVE_INFORMATION" if outcome == "SENSITIVE_REFUSAL" else "OTHER_REFUSAL")
        elif outcome == "TIMED_OUT":
            cause = error("TIMEOUT", job.operation, "adapter", "ATTEMPT_TIMEOUT")
        elif outcome == "CANCELLED":
            cause = error("CANCELLED", job.operation, "request", "CANCEL_REQUESTED")
        elif outcome != "SUCCEEDED":
            cause = error("ADAPTER_FAILED", job.operation, "adapter", "ADAPTER_EXCEPTION" if outcome in ("ADAPTER_EXCEPTION", "REMOTE_RESULT_UNKNOWN") else outcome)
        return outcome, cause, usage, payload

    async def _settle(self, job: _Job, profile: Record, outcome: str, cause: ProviderError | None, usage: Record, payload: Record | None, retry: bool, *, not_sent: bool = False) -> None:
        assert job.attempt is not None and job.stored is not None
        if outcome == "NOT_SENT":
            return
        not_sent = not_sent or job.transport_not_sent
        if self._ledger_faulted:
            raise _WriteFailure("LEDGER_UNCONFIRMED", True)
        previous_usage = as_record(job.attempt["usage"])
        remote_unknown = outcome in ("REMOTE_RESULT_UNKNOWN", "ADAPTER_EXCEPTION")
        logical = outcome if outcome in ("SUCCEEDED", "SENSITIVE_REFUSAL", "OTHER_REFUSAL", "TIMED_OUT", "CANCELLED", "MODE_BLOCKED", "CONFIGURATION_REJECTED") else "FAILED"
        final_error = None if remote_unknown or logical == 'SUCCEEDED' else error_value(cause)
        known_state = "NOT_SENT" if not_sent else "COMPLETED"
        if job.attempt["state"] == "COMPLETED" or self._text and job.attempt['state']=='NOT_SENT':
            same_terminal = (not self._text or not remote_unknown and job.attempt['state']==known_state
                and job.attempt['logical_outcome']==logical and job.attempt['terminal_error']==final_error)
            if same_terminal and previous_usage == usage and job.attempt["result_fingerprint"] == (fingerprint(payload) if payload else None):
                return
            raise _EvidenceConflict()
        if job.unknown and not evidence_compatible(previous_usage, usage):
            raise _EvidenceConflict()
        budget = await self._budget(profile)
        reservation = await self._ledger.get("reservations", cast(str, job.attempt["object_id"]))
        if reservation is None:
            raise InvalidData()
        job.first_error = job.first_error or cause
        phase = "REMOTE_RESULT_UNKNOWN" if remote_unknown else "OPEN" if retry else "TERMINAL"
        attempt = revise(job.attempt, state="NOT_SENT" if not_sent else "REMOTE_RESULT_UNKNOWN" if remote_unknown else "COMPLETED", logical_outcome=None if remote_unknown else logical,
                         confirmed_started=False if not_sent else True, ever_unknown=job.attempt["ever_unknown"] or remote_unknown,
                         first_error=job.attempt["first_error"] or error_value(cause), usage=usage, result_fingerprint=fingerprint(payload) if payload else None,
                         evidence_revision=cast(int, job.attempt["evidence_revision"])+1, updated_at=self._utc(), handoff_id=job.request_id if payload else None,
                         adapter_duration_ms=max(0, int((job.worker_completed_at-job.worker_started_at)*1000)) if not remote_unknown and job.worker_completed_at is not None and job.worker_started_at is not None else None)
        if self._text:
            attempt = MappingProxyType({**attempt, 'terminal_error': final_error})
        request = revise(job.stored, updated_at=self._utc(), phase=phase, outcome=logical if phase == "TERMINAL" else None, first_error=job.stored["first_error"] or error_value(job.first_error),
                         ever_unknown=job.stored["ever_unknown"] or remote_unknown, handoff_id=job.request_id if payload else None)
        updated_reservation = revise(reservation, **{name: usage[name] for name in ("known_subtotal_atoms", "held_atoms", "known_cost_atoms", "cost_complete")})
        if self._text:
            updated_reservation = as_record(freeze({**updated_reservation, 'quota_reserved': 0,
                'quota_known': usage['quota_known'] or 0, 'quota_held': usage['quota_held']}, 8192, owned=True))
        changes = [Mutation("requests", job.stored, request), Mutation("attempts", job.attempt, attempt), Mutation("reservations", reservation, updated_reservation),
                   Mutation("budget_windows", budget, self._settle_budget(budget, reservation, usage))]
        for item in cast(tuple[Data, ...], usage["items"]):
            item = as_record(item)
            key = derived_id("cost",attempt["object_id"],item["item"])
            old = await self._ledger.get("cost_items", key)
            values = {"attempt_id": attempt["object_id"], **item, "evidence_revision": attempt["evidence_revision"], "source": "LOCALLY_ESTIMATED", "unit": "SIMULATED_"+cast(str,item["item"]).upper()+"_UNIT",
                      "known_subtotal_atoms": item["cost_atoms"] or 0, "cost_complete": item["cost_atoms"] is not None}
            if self._text:
                values.update(format_version=3 if usage['format_version']==3 else 2, source=('PROVIDER_REPORTED' if item['quantity'] is not None else 'UNAVAILABLE') if usage['format_version']==3 else 'LOCALLY_ESTIMATED' if item['cost_atoms'] is not None else 'UNAVAILABLE',
                    unit='SUBSCRIPTION_REQUEST' if item['item'] == 'subscription_request' else 'TOKEN')
            changes.append(Mutation("cost_items", old, row(key, **values) if old is None else revise(old, **values)))
        reported_key = derived_id("cost",attempt["object_id"],"reported")
        old_reported = await self._ledger.get("cost_items", reported_key)
        reported_values = {"attempt_id": attempt["object_id"], "item": "reported", "cost_atoms": usage["reported_cost_atoms"], "evidence_revision": attempt["evidence_revision"], "source": "SIMULATED_REPORTED", "unit": "TEST_ATOMS",
                           "known_subtotal_atoms": usage["reported_cost_atoms"] or 0, "cost_complete": usage["reported_cost_atoms"] is not None}
        if self._text:
            reported_values.update(format_version=3 if usage['format_version']==3 else 2, source='UNAVAILABLE', unit='CURRENCY_ATOM',
                quantity=None, price_numerator=None, price_denominator=None)
        changes.append(Mutation("cost_items", old_reported, row(reported_key, **reported_values) if old_reported is None else revise(old_reported, **reported_values)))
        if payload is not None:
            handoff = row(job.request_id, request_id=job.request_id, owner_id=job.grant.result_owner, checksum=fingerprint(payload), source="REMOTE_PROVIDER" if self._text else "SIMULATED", artifact_id=job.request_id, format_version=2 if self._text else 1, created_at=self._utc())
            handoff = MappingProxyType({**handoff, "payload": dump(payload)})
            changes.append(Mutation("handoffs", None, handoff))
        kind = "evidence" if job.unknown else "settle"
        await self._commit(kind, kind+"-"+cast(str, attempt["object_id"])+"-"+str(attempt["evidence_revision"]), tuple(changes), job.grant.actor_ref,
                           job.request_id, cast(str, attempt["object_id"]), cast(str, job.stored["phase"]), phase, cast(bool, usage["cost_complete"]), owner=job)
        job.stored, job.attempt = request, attempt
        if self._text and (remote_unknown or not continuation_ready(usage) or cause is not None and cause.reason in ('AUTHENTICATION_FAILED', 'MODEL_BINDING_MISMATCH')):
            self._text_admission_blocked = True
        if remote_unknown and not job.unknown:
            self._unknown += 1
            job.unknown = True
        self._diagnostic(job, logical)

    async def _terminate(self, job: _Job, outcome: str, cause: ProviderError) -> None:
        assert job.stored is not None
        updated = revise(job.stored, updated_at=self._utc(), phase="TERMINAL", outcome=outcome, first_error=job.stored["first_error"] or error_value(cause))
        await self._commit("terminate", "terminate-"+job.request_id, (Mutation("requests", job.stored, updated),), job.grant.actor_ref,
                           job.request_id, None, "OPEN", "TERMINAL", True, owner=job)
        job.stored = updated

    async def _handoff(self, request: Record, attempts: tuple[Record, ...] | None = None) -> Record:
        handoff = await self._ledger.get("handoffs", cast(str, request["object_id"]))
        if handoff is None or handoff["owner_id"] != request["result_owner"] or handoff["request_id"] != request["object_id"]:
            raise InvalidData()
        payload = load(handoff["payload"])
        if fingerprint(payload) != handoff["checksum"] or handoff["artifact_id"] != request["handoff_id"]:
            raise InvalidData()
        if attempts is None:
            attempts = await self._ledger.read("attempts_for_request", {"request_id": request["object_id"]})
        if not attempts or attempts[-1]["result_fingerprint"] != handoff["checksum"] or attempts[-1]["handoff_id"] != handoff["artifact_id"]:
            raise InvalidData()
        profile = as_record(as_record(request["execution_evidence"])["profile"])
        capability = cast(str,request["capability"])
        if capability == "GENERATION":
            self._result_payload(capability, payload, MappingProxyType({"payload": MappingProxyType({})}), profile, 8192, cast(str, request['task_role']))
        elif capability == "EMBEDDING":
            if type(payload.get("input_items")) is not int or not 1 <= cast(int,payload["input_items"]) <= 64:
                raise InvalidData()
            result_payload(capability, payload, MappingProxyType({"payload": MappingProxyType({"texts": tuple("" for _ in range(cast(int,payload["input_items"])))})}), profile, 8192)
        elif capability == "RERANK":
            ranked = payload.get("ranked")
            if type(ranked) is not tuple or not 1 <= len(ranked) <= 64:
                raise InvalidData()
            candidates = tuple(MappingProxyType({"candidate_id": as_record(item).get("candidate_id"), "text": ""}) for item in ranked)
            result_payload(capability, payload, MappingProxyType({"payload": MappingProxyType({"candidates": candidates, "top_n": len(ranked)})}), profile, 8192)
        else:
            result_payload(capability, payload, MappingProxyType({"payload": MappingProxyType({"modality": payload.get("modality"), "task": payload.get("task")})}), profile, 8192)
        return payload

    async def _present(self, request: Record, source: str):
        if request["phase"] == "TERMINAL":
            return Completed(request, await self._handoff(request) if request["outcome"] == "SUCCEEDED" else None, source)
        return Pending(self._reference(cast(str, request["object_id"]), request["operation_key"]),
                       "REMOTE_RESULT_UNKNOWN" if request["phase"] == "REMOTE_RESULT_UNKNOWN" else "IN_PROGRESS")

    def _diagnostic(self, job: _Job, outcome: str) -> None:
        if self._res.logger is None:
            return
        try:
            attributes: dict[str, object] = {"count": 1, "outcome": "SUCCESS" if outcome == "SUCCEEDED" else "FAILURE"}
            if outcome != "SUCCEEDED":
                code = job.first_error.code if job.first_error else None
                attributes["error_code"] = "TIMEOUT" if code == "TIMEOUT" else "IO_FAILURE" if code == "PERSISTENCE_FAILED" else "VALIDATION_FAILED" if code in ("INVALID_INPUT", "UNSUPPORTED_CAPABILITY") else "INTERNAL_FAILURE"
            cast(Logger, self._res.logger).emit({"level": "INFO" if outcome == "SUCCEEDED" else "ERROR", "event_code": "OPERATION_COMPLETED" if outcome == "SUCCEEDED" else "OPERATION_FAILED",
                                   "context": {"request_id": job.request_id, "provider_request_id": job.request_id, "attempt_id": cast(str, cast(Record,job.attempt)["object_id"]), "run_id": job.request["run_id"]}, "attributes": attributes})
        except MemoryError:
            raise
        except Exception:
            pass

    def get_health(self) -> Health:
        """Observe current owned work without waiting or repairing durable state."""
        pending = bool(self._jobs) or bool(self._lookup_tasks) or bool(self._stored_pending) or (self._init_task is not None and not self._init_task.done()) or (self._initialization_cleanup is not None and not self._initialization_cleanup.done())
        return Health(self._state, len(self._jobs), self._unknown, pending, self._ledger_faulted, self._reason)

    def _finish_close(self) -> None:
        if (self._state == "CLOSING" and not self._jobs and not self._lookup_tasks and not self._stored_pending and (self._init_task is None or self._init_task.done())
                and (self._initialization_cleanup is None or self._initialization_cleanup.done())):
            if self._binding is not None:
                self._binding.release()
            for handle in tuple(self._media): object.__setattr__(handle, '_content', b'')
            self._stored_media.clear(); self._media.clear()
            self._state = "CLOSED"

    async def close(self) -> CloseReport:
        """Stop admission and return one bounded immutable cleanup observation."""
        if self._close_report is not None:
            return self._close_report
        self._state = "CLOSING"
        for job in self._jobs:
            job.stop.set()
        tasks = [job.task for job in self._jobs if job.task is not None]+list(self._lookup_tasks)
        if self._init_task is not None and not self._init_task.done():
            tasks.append(self._init_task)
        if self._initialization_cleanup is not None and not self._initialization_cleanup.done():
            tasks.append(self._initialization_cleanup)
        if tasks:
            await asyncio.wait(tasks, timeout=self._number("close_timeout_ms")/1000)
        self._finish_close()
        pending = self._state != "CLOSED"
        self._close_report = CloseReport("INCOMPLETE" if pending else "CLOSED", error("RESOURCE_FAILED", "close", "state", "CLOSE_INCOMPLETE", True) if pending else None, pending)
        return self._close_report

    def _read_failure(self, operation: str, failure: Exception) -> Failed:
        inconsistent = type(failure) is InvalidData
        if inconsistent:
            self._fault("LEDGER_INCONSISTENT")
        return Failed(error("PERSISTENCE_FAILED", operation, "query", "LEDGER_INCONSISTENT" if inconsistent else "LEDGER_READ_FAILED"))

    async def _lookup_request(self, port: object, action: object, raw: object):
        operation = "lookup_request"
        if (issue := self._access(port, operation, (WorkPort,))) is not None:
            return Failed(issue)
        grant = cast(WorkGrant, self._ports[port])
        if type(action) is not str or action not in CAPABILITIES:
            return Failed(error("INVALID_INPUT", operation, "query", "INVALID_SHAPE"))
        if CAPABILITIES[action] not in grant.capabilities:
            return Failed(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
        try:
            request, token, deadline = self._normalize(raw, grant, action, self._now())
            if request["profile_id"] not in grant.profiles:
                return Failed(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
        except _AdmissionStopped as failure:
            cause = failure.cause
            return Failed(error(cause.code, operation, cause.field, cause.reason))
        except PermissionError:
            return Failed(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
        except InvalidData as failure:
            return Failed(error("INVALID_INPUT", operation, "request", "LIMIT_EXCEEDED" if type(failure) is DataLimit else "INVALID_SHAPE"))
        except MemoryError:
            raise
        except Exception:
            return Failed(error("RESOURCE_FAILED", operation, "state", "RESOURCE_FAILURE"))
        if len(self._lookup_tasks)>=self._number("max_in_flight"):
            return Failed(error("RESOURCE_BUSY", operation, "state", "ADMISSION_BUSY"))
        task=asyncio.create_task(finish_owned(self._lookup_original(port,action,request,token,deadline)))
        if action == "understand_media":
            self._lookup_consumers[task] = cast(dict, cast(dict, raw)["payload"])["media"]
        self._lookup_owners[task] = port
        self._lookup_tasks.add(task);task.add_done_callback(self._lookup_finished)
        while not task.done():
            issue=self._lookup_wait_issue(port,token,deadline, cleanup_pending=not task.done())
            if issue is not None:return Failed(issue)
            try:
                remaining=max(0,deadline-self._now())
            except MemoryError:raise
            except Exception:return Failed(error("RESOURCE_FAILED",operation,"state","RESOURCE_FAILURE",True))
            await asyncio.wait((task,),timeout=min(0.01,remaining))
        result=task.result()
        if type(result) is Found or type(result) is NotFound:
            issue=self._lookup_wait_issue(port,token,deadline, cleanup_pending=not task.done())
            if issue is not None:return Failed(issue)
        return result

    def _lookup_wait_issue(self, port: object, token: CancellationToken, deadline: float, *, cleanup_pending: bool = False) -> ProviderError | None:
        operation="lookup_request"
        if (issue:=self._access(port,operation,(WorkPort,))) is not None:return replace(issue, cleanup_pending=cleanup_pending or issue.cleanup_pending)
        try:
            if self._now()>=deadline:return error("TIMEOUT",operation,"request","DEADLINE_EXCEEDED",cleanup_pending)
            if token.cancelled:return error("CANCELLED",operation,"request","CANCEL_REQUESTED",cleanup_pending)
        except MemoryError:raise
        except Exception:return error("RESOURCE_FAILED",operation,"state","RESOURCE_FAILURE",cleanup_pending)
        return None

    def _lookup_finished(self,task:asyncio.Task) -> None:
        if not task.cancelled():task.exception()
        self._lookup_tasks.discard(task)
        self._lookup_consumers.pop(task, None)
        self._lookup_owners.pop(task, None)
        self._finish_close()

    async def _lookup_original(self,port:object,action:str,request:Record,token:CancellationToken,deadline:float):
        operation="lookup_request"
        grant=cast(WorkGrant,self._ports[port])
        try:
            rows = await self._ledger.read("requests_find", {
                "caller_scope": grant.caller_scope, "caller_module": grant.caller_module,
                "extension_id": grant.extension_id or "", "operation_key": request["operation_key"],
            })
            if (issue:=self._lookup_wait_issue(port,token,deadline)) is not None:return Failed(issue)
            if not rows:
                return NotFound()
            stored = rows[0]
            job = _Job(action, grant, request, token, deadline, cast(str, stored["object_id"]))
            if stored["fingerprint"] != self._semantic(job, stored["execution_evidence"]):
                return Failed(error("IDEMPOTENCY_CONFLICT", operation, "request", "CONTENT_MISMATCH"))
            attempts = await self._ledger.read("attempts_for_request", {"request_id": stored["object_id"]})
            return Found(MappingProxyType({"request": stored, "attempts": attempts, "handoff_present": stored["handoff_id"] is not None}))
        except (LedgerFailure, InvalidData) as failure:
            return self._read_failure(operation, failure)

    async def _get_request(self, port: object, request_id: object):
        operation = "get_request"
        if (issue := self._access(port, operation, (WorkPort, ObserverPort))) is not None:
            return Failed(issue)
        if not is_identifier(request_id):
            return Failed(error("INVALID_INPUT", operation, "query", "INVALID_IDENTIFIER"))
        try:
            grant = self._ports[port]
            scopes = grant.caller_scopes if type(grant) is ObserverGrant else (cast(WorkGrant, grant).caller_scope,)
            visible = await self._ledger.read("requests_visible", {"object_id": request_id, "scopes": dump(scopes),
                "caller_module": grant.caller_module if type(grant) is WorkGrant else None,
                "extension_id": grant.extension_id or "" if type(grant) is WorkGrant else None})
            if not visible:
                return NotFound()
            request = visible[0]
            attempts = await self._ledger.read("attempts_for_request", {"request_id": request_id})
            return Found(MappingProxyType({"request": request, "attempts": attempts, "handoff_present": request["handoff_id"] is not None}))
        except (LedgerFailure, InvalidData) as failure:
            return self._read_failure(operation, failure)

    async def _verify_terminal(self, port: object, request_id: object, original_request: object = None):
        """Bound the whole attestation and retain unfinished lower readers on timeout."""
        operation = 'recover_result'
        denied = self._access(port, operation, (ResultOwnerPort,))
        if denied is not None: return Failed(denied)
        if len(self._lookup_tasks) >= self._number('max_in_flight'):
            return Failed(error('RESOURCE_BUSY', operation, 'state', 'ADMISSION_BUSY'))
        task = asyncio.create_task(finish_owned(self._verify_terminal_owned(port, request_id, original_request)))
        self._lookup_tasks.add(task); task.add_done_callback(self._lookup_finished)
        done, _ = await asyncio.wait((task,), timeout=self._number('request_timeout_ms') / 1000)
        if not done: return Failed(error('TIMEOUT', operation, 'request', 'DEADLINE_EXCEEDED', True))
        denied = self._access(port, operation, (ResultOwnerPort,))
        if denied is not None: return Failed(denied)
        return task.result()

    async def _verify_terminal_owned(self, port: object, request_id: object, original_request: object = None):
        """Attest only this result owner's complete audited original terminal."""
        operation = 'recover_result'
        denied = self._access(port, operation, (ResultOwnerPort,))
        if denied is not None:
            return Failed(denied)
        grant = self._ports[port]
        if type(grant) is not ResultGrant or not is_identifier(request_id) or request_id not in grant.request_ids:
            return Failed(error('ACCESS_DENIED', operation, 'capability', 'CAPABILITY_MISMATCH'))
        result = await self._recover_result(port, request_id)
        if type(result) is not Found:
            return result
        try:
            request = await self._ledger.get('requests', cast(str, request_id))
            if request is None or request['phase'] != 'TERMINAL' or request['result_owner'] != grant.owner_id:
                return Failed(error('ACCESS_DENIED', operation, 'capability', 'CAPABILITY_MISMATCH'))
            if len(self._terminal_evidence) >= self._number('max_in_flight'):
                return Failed(error('RESOURCE_BUSY', operation, 'state', 'ADMISSION_BUSY'))
            normalized = None
            if original_request is not None:
                normalized = as_record(freeze(original_request, 131072 if self._text else 65536, owned=True))
                expected = self._original_fingerprint(normalized, request, request['execution_evidence'])
                if expected != request['fingerprint']:
                    return Failed(error('ACCESS_DENIED', operation, 'capability', 'CAPABILITY_MISMATCH'))
            from .terminal_evidence import VerifiedTerminal, TerminalVerified
            evidence = object.__new__(VerifiedTerminal)
            object.__setattr__(evidence, '_provider', self)
            object.__setattr__(evidence, 'database_id', self._ledger.database_id)
            object.__setattr__(evidence, 'request', request)
            object.__setattr__(evidence, 'result', as_record(result.value)['result'])
            object.__setattr__(evidence, 'original_request', normalized)
            attempts = await self._ledger.read('attempts_for_request', {'request_id': request['object_id']})
            object.__setattr__(evidence, 'confirmed_sent', any(a['confirmed_started'] is True for a in attempts))
            terminal_reason = 'SENSITIVE_INFORMATION' if request['outcome'] == 'SENSITIVE_REFUSAL' else None
            if self._text:
                from .text_stored_schema import terminal_reason as actual_terminal_reason
                from .terminal_evidence import _retain_text_attempt
                terminal_reason = actual_terminal_reason(request, attempts)
                _retain_text_attempt(evidence, attempts[0] if attempts else None)
            object.__setattr__(evidence, 'terminal_reason', terminal_reason)
            self._terminal_evidence[id(evidence)] = evidence
            return TerminalVerified(evidence)
        except (LedgerFailure, InvalidData) as failure:
            return self._read_failure(operation, failure)

    async def _recover_result(self, port: object, request_id: object):
        operation = "recover_result"
        if (issue := self._access(port, operation, (ResultOwnerPort,))) is not None:
            return Failed(issue)
        if not is_identifier(request_id):
            return Failed(error("INVALID_INPUT", operation, "query", "INVALID_IDENTIFIER"))
        grant = cast(ResultGrant, self._ports[port])
        if request_id not in grant.request_ids:
            return Failed(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
        try:
            request = await self._ledger.get("requests", cast(str, request_id))
            if request is None:
                return NotFound()
            if request["result_owner"] != grant.owner_id:
                return Failed(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
            result = await self._present(request, "EXISTING")
            return Found(MappingProxyType({"outcome": result.record["outcome"], "result": result.result})) if type(result) is Completed else result
        except (LedgerFailure, InvalidData) as failure:
            return self._read_failure(operation, failure)

    async def _get_budget_state(self, port: object):
        operation = "get_budget_state"
        if (issue := self._access(port, operation, (ObserverPort,))) is not None:
            return Failed(issue)
        grant = cast(ObserverGrant, self._ports[port])
        if not grant.account_ids:
            return Failed(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
        try:
            budgets = await self._ledger.read("budget_windows_page", {"after": "", "limit": 4})
            return Found(tuple(MappingProxyType({**budget, "available_atoms": cast(int, as_record(budget["policy"])["cost_limit_atoms"])-cast(int, budget["known_subtotal_atoms"])-cast(int, budget["held_atoms"])})
                               for budget in budgets if budget["account_id"] in grant.account_ids))
        except (LedgerFailure, InvalidData) as failure:
            return self._read_failure(operation, failure)

    async def _query_usage(self, port: object, raw: object):
        operation = "query_usage"
        if (issue := self._access(port, operation, (ObserverPort,))) is not None:
            return Failed(issue)
        grant = cast(ObserverGrant, self._ports[port])
        try:
            query = as_record(freeze(raw, 8192))
            if not keys(query, {"start", "end", "caller_scope", "capability", "task_role", "profile_id", "account_id", "group_by"}):
                raise InvalidData()
            if query["group_by"] not in ("NONE", "CAPABILITY", "TASK_ROLE", "PROFILE", "ACCOUNT"):
                raise InvalidData()
            for name in ("start", "end"):
                if type(query[name]) is not str or len(cast(str, query[name])) > 40:
                    raise InvalidData()
                parsed = datetime.fromisoformat(cast(str, query[name]))
                if parsed.tzinfo is not timezone.utc:
                    raise InvalidData()
            if datetime.fromisoformat(cast(str, query["start"])) >= datetime.fromisoformat(cast(str, query["end"])):
                raise InvalidData()
            for name in ("caller_scope", "capability", "task_role", "profile_id", "account_id"):
                if query[name] is not None and not is_identifier(query[name]):
                    raise InvalidData()
            if query["capability"] is not None and query["capability"] not in CAPABILITIES.values():
                raise InvalidData()
            if query["task_role"] is not None and query["task_role"] not in ROLES:
                raise InvalidData()
            if query["caller_scope"] is not None and query["caller_scope"] not in grant.caller_scopes:
                return Failed(error("ACCESS_DENIED", operation, "capability", "CAPABILITY_MISMATCH"))
            scopes = grant.caller_scopes if query["caller_scope"] is None else (cast(str, query["caller_scope"]),)
            parameters: dict[str, object] = {name: query[name] for name in ("capability", "task_role", "profile_id", "account_id", "group_by")}
            parameters.update({name: datetime.fromisoformat(cast(str, query[name])).isoformat(timespec="microseconds") for name in ("start", "end")})
            parameters.update(scopes=dump(scopes), limit=self._number("query_row_limit")+1)
        except (InvalidData, ValueError):
            return Failed(error("INVALID_INPUT", operation, "query", "INVALID_SHAPE"))
        try:
            rows = await self._ledger.read("usage_aggregate", parameters)
            if any(item.get("invalid_count") != 0 or any(type(value) is not int or value < 0 for name,value in item.items() if name != "group_id") for item in rows):
                raise InvalidData()
            if len(rows) > self._number("query_row_limit"):
                return Failed(error("INVALID_INPUT", operation, "query", "LIMIT_EXCEEDED"))
        except (LedgerFailure, InvalidData) as failure:
            return self._read_failure(operation, failure)
        try:
            observed = self._utc()
        except MemoryError:
            raise
        except Exception:
            return Failed(error("PERSISTENCE_FAILED", operation, "query", "LEDGER_READ_FAILED"))
        return Found(MappingProxyType({"range": query, "as_of": observed, "sample_count": sum(cast(int, item["request_count"]) for item in rows), "source": "REMOTE_PROVIDER" if self._text else "SIMULATED", "rows": rows}))
