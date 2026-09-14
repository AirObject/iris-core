"""Provider-owned atomic ledger changes over restricted persistence statements.

Every write includes one required audit and original receipt. Immutable row
revisions are checked inside the transaction. Input bodies are never copied into
the generic receipt; only stable object references and revisions are returned.
"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import cast, TYPE_CHECKING

from companion_memory.configuration import EffectiveSnapshot
if TYPE_CHECKING:
    from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.logging_service import AuditBound, AuditErr, AuditRequirement, bind_audit
from companion_memory.persistence import (
    BoundedTextSchema, CommandDefinition, Committed, Field, LocalCommand, ModuleOwnerLease,
    PersistenceService, RecordSchema, ScalarSchema, SequenceSchema, Staged, UnitOfWork, Value,
    create_provider_repository,
)
from companion_memory.persistence import Failed as StorageFailed, Found as StorageFound, NotFound as StorageNotFound
from .accounting import validate_row
from .values import Data, InvalidData, Record, as_record, dump, freeze, is_identifier, load, plain

TABLES = ("requests", "attempts", "budget_windows", "reservations", "cost_items", "handoffs")
STATES = ("NONE", "OPEN", "TERMINAL", "REMOTE_RESULT_UNKNOWN", "PREPARED", "COMPLETED", "NOT_SENT", "CLEAR", "RESERVATION_OVERRUN")
KINDS = {
    "register": ("PROVIDER_REQUEST_REGISTERED", "REGISTER"),
    "prepare": ("PROVIDER_ATTEMPT_PREPARED", "PREPARE"),
    "settle": ("PROVIDER_ATTEMPT_SETTLED", "SETTLE"),
    "terminate": ("PROVIDER_REQUEST_TERMINATED", "TERMINATE"),
    "recover": ("PROVIDER_UNKNOWN_RECORDED", "RECOVER"),
    "evidence": ("PROVIDER_EVIDENCE_RECORDED", "LATE_EVIDENCE"),
    "initialize_budget": ("PROVIDER_BUDGET_INITIALIZED", "INITIALIZE"),
}
IDENTIFIER = ScalarSchema("identifier")
INTEGER = ScalarSchema("integer")
CHANGE = RecordSchema((Field("request_id", IDENTIFIER, nullable=True), Field("attempt_id", IDENTIFIER, nullable=True),
                       Field("previous_revision", INTEGER, nullable=True), Field("revision", INTEGER),
                       Field("previous_state", ScalarSchema("enum", choices=STATES)),
                       Field("state", ScalarSchema("enum", choices=STATES)), Field("cost_complete", ScalarSchema("boolean"))))


class LedgerFailure(Exception):
    """Safe internal failure carrying a structured persistence result only."""
    def __init__(self, result: object):
        self.result = result
        super().__init__()


@dataclass(frozen=True, slots=True)
class Mutation:
    """One provider row replacement, guarded by an exact previous revision."""
    table: str
    previous: Record | None
    current: Record


class LedgerAssembly:
    """Trusted declarations required before creating a new provider database."""
    def __init__(self, *, text_generation: bool = False) -> None:
        if type(text_generation) is not bool:
            raise TypeError('An exact static provider format is required.')
        self.text_generation = text_generation
        self.repository = create_provider_repository(text_generation=text_generation)
        self._bindings: dict[int, LedgerBinding] = {}
        self._active: LedgerBinding | None = None
        mutation_schema = RecordSchema((Field("table", ScalarSchema("enum", choices=TABLES)), Field("object_id", IDENTIFIER),
                                        Field("expected_revision", INTEGER, nullable=True), Field("body", BoundedTextSchema(8192)), Field("payload", BoundedTextSchema(8192), nullable=True)))
        input_schema = RecordSchema((Field("changes", SequenceSchema(mutation_schema, 1, 16)),
                                     Field("event", BoundedTextSchema(2048))))
        result_schema = RecordSchema((Field("object_id", IDENTIFIER), Field("revision", INTEGER)))
        commands = []
        self.requirements = {}
        for kind, (event_code, reason) in KINDS.items():
            change_schema = CHANGE
            if text_generation:
                change_schema = RecordSchema(CHANGE.fields + (
                    Field('billing_mode', ScalarSchema('enum', choices=('TOKEN_METERED', 'SUBSCRIPTION','USAGE_ONLY_TRIAL'))),
                    Field('currency', ScalarSchema('enum', choices=('CNY', 'USD'))),
                    Field('quota_known', INTEGER, nullable=True), Field('quota_held', INTEGER), Field('config_snapshot_id', IDENTIFIER)))
            requirement = AuditRequirement("provider", "provider_change", event_code, 1, (reason,), change_schema)
            self.requirements[kind] = requirement
            def handler(uow: UnitOfWork, values: MappingProxyType[str, Value], action=kind):
                binding = self._active
                if binding is None:
                    raise InvalidData()
                return binding._handle(action, uow, values)
            definition = CommandDefinition("provider", kind, 2 if text_generation else 1, input_schema, 1, result_schema,
                                              (self.repository.definition,), (requirement,), handler)
            if text_generation:
                from .text_command_policy import issue
                definition = replace(definition, input_policy=issue(self))
            commands.append(definition)
        self.commands = tuple(commands)
        self.repositories = (self.repository.definition,)

    def bind(self, storage: PersistenceService, snapshot: EffectiveSnapshot | StoredTextConfiguration) -> LedgerBinding:
        """Issue restricted statements; acquiring unique execution ownership is separate."""
        if type(storage) is not PersistenceService:
            raise TypeError("Native storage and configuration bindings are required.")
        from companion_memory.configuration.text_persistence import StoredTextConfiguration, stored_text_configuration_issue
        stored = None
        if self.text_generation:
            if stored_text_configuration_issue(snapshot) is not None:
                raise TypeError('Native persisted text configuration is required.')
            stored = cast(StoredTextConfiguration, snapshot)
            snapshot = stored.candidate.foundation
        elif type(snapshot) is not EffectiveSnapshot:
            raise TypeError('The simulation configuration format is required.')
        binding = LedgerBinding(self, storage, cast(EffectiveSnapshot, snapshot), text_configuration=stored)
        self._bindings[id(binding)] = binding
        return binding

    def issued(self, binding: object) -> bool:
        return type(binding) is LedgerBinding and self._bindings.get(id(binding)) is binding


class LedgerBinding:
    """Private-to-service ledger capability with no arbitrary SQL or connection API."""
    def __init__(self, assembly: LedgerAssembly, storage: PersistenceService, snapshot: EffectiveSnapshot,
                 *, text_configuration: StoredTextConfiguration | None = None):
        self.assembly, self.storage, self.snapshot = assembly, storage, snapshot
        self.text_configuration = text_configuration
        self.statements = {name: storage.bind_statement(assembly.repository.definition, statement, "provider")
                           for name, statement in assembly.repository.statements}
        self.operations = {definition.operation_kind: storage.bind_operation(definition, "provider") for definition in assembly.commands}
        self.audits = {}
        self.lease: ModuleOwnerLease | None = None
        self._executor: object | None = None
        for kind, requirement in assembly.requirements.items():
            bound = bind_audit(snapshot, storage.bind_audit_writer(requirement, "provider"))
            if type(bound) is not AuditBound:
                raise ValueError("The required audit binding is unavailable.")
            self.audits[kind] = bound.value

    def acquire(self, executor: object) -> bool:
        if self.lease is not None:
            return self._executor is executor and self.lease.is_active()
        if self.assembly._active is not None:
            return False
        lease = self.storage.claim_module_owner(self.assembly.repository.definition)
        if lease is None:
            return False
        self.lease = lease
        self._executor = executor
        self.assembly._active = self
        return True

    def release(self) -> bool:
        if self.lease is None:
            return True
        if self.assembly._active is self:
            self.assembly._active = None
        released = self.lease.release()
        self.lease = None
        self._executor = None
        return released

    @property
    def database_id(self) -> str | None:
        return self.lease.database_id if self.lease is not None else None

    def _handle(self, kind: str, uow: UnitOfWork, values: MappingProxyType[str, Value]) -> object:
        changes = cast(tuple[MappingProxyType[str, Value], ...], values["changes"])
        first: Record | None = None
        for change in changes:
            table, key, expected = cast(str, change["table"]), cast(str, change["object_id"]), change["expected_revision"]
            observed = self.statements[table + "_get"].participate(uow, {"object_id": key})
            if type(observed) is not Staged or type(observed.value) is not tuple:
                raise LedgerFailure(observed)
            existing = self._decode(table, cast(MappingProxyType[str, Value], observed.value[0])) if observed.value else None
            if (expected is None and existing is not None) or (expected is not None and (existing is None or existing["revision"] != expected)):
                raise InvalidData()
            row = load(change["body"])
            self._validate_row(table, row)
            immutable = {"requests": ("object_id", "caller_scope", "caller_module", "extension_id", "operation_key", "fingerprint", "execution_evidence", "attribution", "created_at", "profile_id", "account_id", "result_owner", "capability", "task_role"),
                         "attempts": ("object_id", "request_id", "ordinal", "account_id", "profile_id", "capability", "execution_owner_id", "created_at"),
                         "budget_windows": ("object_id", "account_id", "window_id", "policy"), "reservations": ("object_id", "attempt_id", "account_id", "budget_id", "reserved_atoms"),
                         "cost_items": ("object_id", "attempt_id", "item", "source", "unit"), "handoffs": tuple(row)}[table]
            if self.assembly.text_generation:
                if table == 'requests':
                    if existing is not None and existing['first_error'] is not None and existing['first_error']!=row['first_error']:raise InvalidData()
                    immutable += ('source', 'configuration_origin', 'config_snapshot_id', 'profile_revision', 'price_revision', 'format_version', 'fingerprint_version')
                elif table == 'attempts' and existing is not None:
                    if (existing['first_error'] is not None and existing['first_error']!=row['first_error']
                            or existing['state'] in ('COMPLETED','NOT_SENT') and any(existing[name]!=row[name]
                                for name in ('terminal_error','state','logical_outcome','usage','result_fingerprint','evidence_revision','handoff_id','confirmed_started'))):
                        raise InvalidData()
                elif table == 'cost_items':
                    immutable = ('object_id', 'attempt_id', 'item', 'unit', 'format_version', 'price_numerator', 'price_denominator')
                    if existing is not None and (existing['source'] != 'UNAVAILABLE' and existing['source'] != row['source']
                            or any(existing[name] is not None and existing[name] != row[name] for name in ('quantity', 'cost_atoms'))):
                        raise InvalidData()
            if existing is not None and any(existing[name] != row[name] for name in immutable):
                raise InvalidData()
            if row["object_id"] != key or type(row["revision"]) is not int or row["revision"] != (0 if expected is None else cast(int, expected)+1):
                raise InvalidData()
            parameters: dict[str, object] = {"object_id": key, "revision": row["revision"], "body": change["body"]}
            if table == "handoffs":
                parameters["payload"] = change["payload"]
            if expected is None:
                extras = {"requests": ("caller_scope", "caller_module", "extension_id", "operation_key"), "attempts": ("request_id", "ordinal"),
                          "budget_windows": ("account_id", "window_id"), "reservations": ("attempt_id",),
                          "cost_items": ("attempt_id", "item"), "handoffs": ("request_id",)}[table]
                parameters.update({name: row[name] for name in extras})
                if table == "requests" and parameters["extension_id"] is None:
                    parameters["extension_id"] = ""
                operation = table + "_insert"
            else:
                parameters["expected_revision"] = expected
                operation = table + "_update"
            staged = self.statements[operation].participate(uow, parameters)
            if type(staged) is not Staged or type(staged.value) is not tuple or len(staged.value) != 1:
                raise LedgerFailure(staged)
            if first is None:
                first = row
        event = load(values["event"], 2048)
        audit = self.audits[kind].append_audit(uow, plain(event))
        if type(audit) is AuditErr:
            raise LedgerFailure(audit)
        assert first is not None
        return {"object_id": first["object_id"], "revision": first["revision"]}

    def _validate_row(self, table: str, row: Record) -> None:
        if self.assembly.text_generation:
            from .text_stored_schema import validate
            validate(table, row)
            if self.text_configuration is None:
                raise InvalidData()
            from companion_memory.configuration import PresentValue
            from .service import derived_id
            values = {entry.definition.key: entry.state.value for entry in self.text_configuration.candidate.foundation.list_entries() if type(entry.state) is PresentValue}
            accounts = cast(tuple[Data, ...], values['provider.accounts'])
            account = as_record(accounts[0])
            profiles = cast(tuple[Data, ...], values['provider.profiles'])
            if table == 'budget_windows' and row['policy'] != account:
                raise InvalidData()
            if table == 'requests':
                if (row['config_snapshot_id'] != self.text_configuration.snapshot_id
                        or row['profile_revision'] != derived_id('profile', self.text_configuration.snapshot_id, row['profile_id'])
                        or row['price_revision'] != as_record(account['price'])['revision_ref']):
                    raise InvalidData()
                evidence = as_record(row['execution_evidence'])
                if evidence['profile'] is not None and (evidence['profile'] not in profiles or evidence['account'] != account):
                    raise InvalidData()
        else:
            validate_row(table, row)

    def _decode(self, table: str, raw: MappingProxyType[str, Value]) -> Record:
        """Validate physical identity and stored schema for reads and mutations."""
        record = load(raw["body"])
        for name in raw:
            if name in ("body", "payload"):
                continue
            expected = record.get(name)
            if name == "extension_id" and expected is None:
                expected = ""
            if raw[name] != expected or type(raw[name]) is not type(expected):
                raise InvalidData()
        if table != "usage_aggregate":
            self._validate_row(table, record)
        if "payload" in raw:
            record = MappingProxyType({**record, "payload": cast(str, raw["payload"])})
        return record

    async def read(self, statement: str, parameters: dict[str, object]) -> tuple[Record, ...]:
        result = await self.statements[statement].read_object(parameters)
        if type(result) is StorageNotFound:
            return ()
        if type(result) is not StorageFound or type(result.value) is not tuple:
            raise LedgerFailure(result)
        collected = []
        for raw in result.value:
            raw = cast(MappingProxyType[str, Value], raw)
            table = statement.rsplit("_", 1)[0]
            if statement in ("requests_find", "requests_visible"):
                table = "requests"
            elif statement == "attempts_for_request":
                table = "attempts"
            if statement == "usage_aggregate":
                table = statement
            record = self._decode(table, raw)
            collected.append(record)
        rows = tuple(collected)
        for row in rows:
            if statement == "usage_aggregate":
                continue
            if type(row.get("revision")) is not int or not is_identifier(row.get("object_id")):
                raise InvalidData()
        return rows

    async def get(self, table: str, key: str) -> Record | None:
        rows = await self.read(table + "_get", {"object_id": key})
        return rows[0] if rows else None

    async def mutate(self, kind: str, key: str, changes: tuple[Mutation, ...], actor: str, request_id: str | None,
                     attempt_id: str | None, previous_state: str, state: str, cost_complete: bool):
        """Execute one complete command; callers decide evidence and never auto-send."""
        first = changes[0]
        event = {"event_version": 1, "actor_kind": "SYSTEM", "actor_ref": actor, "reason_code": KINDS[kind][1],
                 "target_refs": [{"object_id": first.current["object_id"], "previous_revision": None if first.previous is None else first.previous["revision"],
                                  "revision": first.current["revision"]}],
                 "change": {"request_id": request_id, "attempt_id": attempt_id,
                            "previous_revision": None if first.previous is None else first.previous["revision"], "revision": first.current["revision"],
                            "previous_state": previous_state, "state": state, "cost_complete": cost_complete}}
        if kind == "initialize_budget":
            event["target_refs"] = [{"object_id": change.current["object_id" if self.assembly.text_generation else "account_id"],
                                     "previous_revision": None if change.previous is None else change.previous["revision"],
                                     "revision": change.current["revision"]} for change in changes]
        if self.assembly.text_generation:
            assert self.text_configuration is not None
            configured = next(entry.state for entry in self.text_configuration.candidate.foundation.list_entries() if entry.definition.key == 'provider.accounts')
            from companion_memory.configuration import PresentValue
            if type(configured) is not PresentValue:
                raise InvalidData()
            account = as_record(cast(tuple[Data, ...], configured.value)[0])
            usage = next((as_record(change.current['usage']) for change in changes if change.table == 'attempts'), None)
            event['change'].update({'billing_mode': account['billing_mode'], 'currency': account['currency'],
                'quota_known': usage['quota_known'] if usage is not None else 0,
                'quota_held': usage['quota_held'] if usage is not None else 0,
                'config_snapshot_id': self.text_configuration.snapshot_id})
        frozen_event = as_record(freeze(event, 2048))
        values = {"changes": [{"table": change.table, "object_id": change.current["object_id"],
                                "expected_revision": None if change.previous is None else change.previous["revision"], "body": dump(MappingProxyType({name: value for name, value in change.current.items() if name != "payload"})),
                                "payload": change.current.get("payload")} for change in changes],
                  "event": dump(frozen_event, 2048)}
        command = LocalCommand(2 if self.assembly.text_generation else 1, values, {"provider_change": event})
        result = await self.operations[kind].execute(key, command)
        return result, command
