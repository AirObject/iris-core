"""Explicit content transaction assembly with real data owners.

Configuration, ingress, buffers, cognition, memory and restricted object history
have separate repositories. This assembly coordinates short local transactions;
Provider execution and media file I/O must finish outside these handlers.
"""
from __future__ import annotations
import hashlib
import time
from collections.abc import Callable
from types import MappingProxyType
from typing import cast, TYPE_CHECKING
if TYPE_CHECKING:
    from .candidate_goals import CandidateGoalEffects
    from companion_memory.media.daily_work import DailyImageWork
from companion_memory.configuration.content_persistence import StoredContentConfiguration
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.configuration.text_persistence import StoredTextConfiguration, stored_text_configuration_issue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration, stored_semantic_configuration_issue
from companion_memory.persistence import (
    AuditFieldBinding, AuditResultBinding, BoundedTextSchema, Field, RecordSchema,
    RepositoryDefinition, ResultBoundCommandDefinition, SequenceSchema,
    StatementDefinition, TableDefinition, UnitOfWork, PersistenceService, Value,
)
from companion_memory.persistence.schema import InvalidValue, ScalarSchema, freeze_value
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import BoundStatements, StatementCatalog, OwnerFailure
from companion_memory.logging_service import AuditRequirement
from companion_memory.logging_service.object_history import history_catalog, HistoryBinding, HISTORY_REFS
from companion_memory.memory.repository import memory_catalog
from companion_memory.memory.formats import ID, INT, REVISION, enum, record, sequence
from companion_memory.memory.sources import isolate_source, source_digest, decode_source
from companion_memory.memory.transactions import MemoryTransactions, ApplyScope, applied_counts
from companion_memory.cognition.candidates import candidate_catalog, CandidateBinding, isolate_candidate
from companion_memory.ingress.content_storage import ingress_content_catalog, information_ingress_catalog, ContentIngressTransactions, ContentMediaOwnership
from .preparation_format import isolate_preparation, decode_preparation
from companion_memory.buffers.content_storage import buffer_content_catalog, ContentBufferTransactions

AUDIT_REFERENCES = SequenceSchema(RecordSchema((Field('name', ID), Field('object_id', ID), Field('revision', INT, nullable=True))), 0, 20)
AUDIT_COUNTS = SequenceSchema(RecordSchema((Field('name', ID), Field('count', INT))), 0, 16)
FACT = RecordSchema((Field('rows_changed', INT), Field('state', ID),
    Field('references', AUDIT_REFERENCES), Field('counts', AUDIT_COUNTS), Field('provenance', enum('NONE', 'EXTERNAL_REPORT', 'PROVIDER_CONFIRMED')),
    Field('guard_created', ScalarSchema('boolean'), nullable=True), Field('reused', ScalarSchema('boolean'), nullable=True)))
def owner_fact(owner: str) -> RecordSchema:
    """Media-only protection facts do not create placeholders in other owners."""
    if owner == 'media': return FACT
    fields = tuple(f for f in FACT.fields if f.name not in ('provenance', 'guard_created', 'reused'))
    if owner == 'logging_service': fields += (Field('history_ids', SequenceSchema(ID, 0, 8)),)
    return RecordSchema(fields)


OBJECT_REFS = SequenceSchema(RecordSchema((Field('object_id', ID), Field('previous_revision', INT, nullable=True),
    Field('revision', REVISION), Field('lifecycle', enum('ACTIVE', 'FORGOTTEN', 'DELETED')))), 0, 8)
TARGETS = SequenceSchema(RecordSchema((Field('object_id', ID), Field('previous_revision', INT, nullable=True), Field('revision', INT))), 1, 16)
RESULT = RecordSchema((Field('operation_id', ID), Field('entry_id', ID), Field('batch_id', ID, nullable=True),
    Field('candidate_id', ID, nullable=True), Field('source_id', ID, nullable=True), Field('terminal', ID),
    Field('object_refs', OBJECT_REFS), Field('history', SequenceSchema(ID, 0, 8)), Field('retired_source_ids', SequenceSchema(ID, 0, 16)), Field('targets', TARGETS),
    Field('storage_execution', enum('ACTUAL')), Field('model_adapter', enum('SIMULATED')),
    Field('candidate_origin', enum('SYNTHETIC')), Field('facts', RecordSchema(tuple(Field(owner, FACT) for owner in
        ('runtime', 'ingress', 'buffers', 'cognition', 'memory', 'logging_service', 'media'))))))


def result_schema(owners: tuple[str, ...], *, text_format: bool = False) -> RecordSchema:
    """Each command carries only its statically declared owner summaries."""
    fields = tuple(Field(field.name, SequenceSchema(ID, 0, 0)) if field.name in ('object_refs', 'history', 'retired_source_ids') and 'memory' not in owners else field for field in RESULT.fields if field.name != 'facts')
    if text_format:
        fields=tuple(Field(field.name,enum('REMOTE_PROVIDER' if field.name=='model_adapter' else 'MODEL_VALIDATED'))
            if field.name in ('model_adapter','candidate_origin') else field for field in fields)
    return RecordSchema(fields +
        (Field('facts', RecordSchema(tuple(Field(owner, owner_fact(owner)) for owner in owners))),))


def stable(kind: str, *parts: Value) -> str:
    """Retain deterministic internal operation identity before any side effect."""
    return kind + ':' + hashlib.sha256(encode_content(tuple(parts), 8192)).hexdigest()


def runtime_content_catalog(*,daily_format:bool=False) -> StatementCatalog:
    """Declare mode, preparation and original work association as separate records."""
    tables = []; statements = []
    layouts = {
        'members': (Field('member_key', ID), Field('owner_kind', enum('preparations', 'batches')), Field('owner_id', ID), Field('ordinal', ScalarSchema('integer', 0, 3)), Field('body', BoundedTextSchema(2048))),
        'mode': (Field('mode_id', ID), Field('state', enum('NORMAL', 'DREAM_PREPARING', 'DREAM_FOCUSED', 'DRAINING', 'FAULTED')), Field('epoch', REVISION), Field('run_id', ID, nullable=True), Field('publication_id', ID, nullable=True), Field('deadline_at_us', INT, nullable=True)),
        'preparations': (Field('preparation_id', ID), Field('entry_id', ID), Field('batch_id', ID), Field('run_id', ID),
            Field('revision', REVISION), Field('owner_generation', INT), Field('phase', ID), Field('deadline_at_us', INT), Field('manifest', BoundedTextSchema(4096)),
            Field('preparation_version', REVISION), Field('trigger_key', ID), Field('mode_epoch', REVISION),
            Field('checked_entry_revision', REVISION), Field('window_digest', ID), Field('started_at_us', INT),
            Field('spent_ms', INT), Field('last_observed_at_us', INT), Field('parked_from_phase', ID, nullable=True)),
        'batches': (Field('batch_id', ID), Field('entry_id', ID), Field('run_id', ID), Field('terminal', ID), Field('manifest', BoundedTextSchema(4096))),
        'learning_triggers': (Field('trigger_key', ID), Field('batch_id', ID)),
        'learning_admissions': (Field('admission_id', ID), Field('batch_id', ID), Field('admission_generation', REVISION),
            Field('operation_key', ID), Field('request_id', ID, nullable=True), Field('request_digest', ID), Field('conclusion', ID),
            Field('closed_at_us', INT), Field('mode_epoch', REVISION)),
        'work': (Field('batch_id', ID), Field('admission_generation', REVISION), Field('admission_trigger', ID), Field('generation', REVISION), Field('revision', REVISION), Field('phase', ID),
            Field('provider_operation_key', ID, nullable=True), Field('provider_request_id', ID, nullable=True),
            Field('handoff_ref', ID, nullable=True), Field('candidate_id', ID, nullable=True),
            Field('model_binding', BoundedTextSchema(8192), nullable=True)),
    }
    for name, fields in layouts.items():
        table = 'runtime_content_' + name; key = fields[0].name; columns = ','.join(f.name for f in fields)
        ddl = ','.join(f.name + (' INTEGER' if type(f.schema) is ScalarSchema and f.schema.kind == 'integer' else ' TEXT') + ('' if f.nullable else ' NOT NULL') for f in fields)
        tables.append(TableDefinition(table, 'CREATE TABLE ' + table + ' (scope_id TEXT NOT NULL,' + ddl + ',PRIMARY KEY(scope_id,' + key + '))'))
        row = RecordSchema(fields); keys = RecordSchema((fields[0],))
        for suffix, sql, params, result, writes in (
            ('get', 'SELECT ' + columns + ' FROM ' + table + ' WHERE scope_id=:scope_id AND ' + key + '=:' + key, keys, row, False),
            ('insert', 'INSERT INTO ' + table + ' VALUES(:scope_id,' + ','.join(':' + f.name for f in fields) + ') RETURNING ' + columns, row, row, True),
            ('update', 'UPDATE ' + table + ' SET ' + ','.join(f.name + '=:' + f.name for f in fields[1:]) + ' WHERE scope_id=:scope_id AND ' + key + '=:' + key + ' RETURNING ' + columns, row, row, True),
        ):
            statements.append((name + '_' + suffix, StatementDefinition(sql, params, result, writes)))
    if daily_format:
        fields=layouts['preparations']
        statements.append(('daily_preparation_for_batch',StatementDefinition(
            'SELECT '+','.join(f.name for f in fields)+' FROM runtime_content_preparations WHERE scope_id=:scope_id AND batch_id=:batch_id LIMIT 2',
            RecordSchema((Field('batch_id',ID),)),RecordSchema(fields),False)))
    for name in ('preparations', 'batches', 'work'):
        fields = layouts[name]; key = fields[0].name
        metadata = tuple(f for f in fields if f.name in (key, 'phase', 'terminal'))
        parameters = RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 128))))
        statements.append((name + '_page', StatementDefinition('SELECT ' + ','.join(f.name for f in metadata) +
            ' FROM runtime_content_' + name + ' WHERE scope_id=:scope_id AND ' + key + '>:after ORDER BY ' + key + ' LIMIT :limit', parameters, RecordSchema(metadata), False)))
    tables.extend((TableDefinition('runtime_content_preparations_entry', 'CREATE INDEX runtime_content_preparations_entry ON runtime_content_preparations(scope_id,entry_id,phase)'),
        TableDefinition('runtime_content_batches_entry', 'CREATE INDEX runtime_content_batches_entry ON runtime_content_batches(scope_id,entry_id,terminal)')))
    tables.append(TableDefinition('runtime_content_members_owner', 'CREATE UNIQUE INDEX runtime_content_members_owner ON runtime_content_members(scope_id,owner_kind,owner_id,ordinal)'))
    statements.append(('members_count', StatementDefinition('SELECT count(*) AS count FROM runtime_content_members WHERE scope_id=:scope_id AND owner_kind=:owner_kind AND owner_id=:owner_id', RecordSchema((Field('owner_kind', ID), Field('owner_id', ID))), RecordSchema((Field('count', INT),)), False)))
    statements.append(('observe_entry', StatementDefinition("SELECT (SELECT count(*) FROM runtime_content_preparations WHERE scope_id=:scope_id AND entry_id=:entry_id AND phase IN ('SELECTED','CLAIMED','MEDIA_READY','PARKED')) AS preparations,"
        "(SELECT count(*) FROM runtime_content_batches WHERE scope_id=:scope_id AND entry_id=:entry_id AND terminal='FROZEN') AS active_batches,"
        "(SELECT count(*) FROM runtime_content_batches WHERE scope_id=:scope_id AND entry_id=:entry_id AND terminal='SUCCEEDED') AS succeeded_batches,"
        "(SELECT count(*) FROM runtime_content_batches WHERE scope_id=:scope_id AND entry_id=:entry_id AND terminal='FAILED_DROPPED') AS failed_batches,"
        "(SELECT count(*) FROM runtime_content_batches WHERE scope_id=:scope_id AND entry_id=:entry_id AND terminal='SENSITIVE_DROPPED') AS refused_batches,"
        "(SELECT max(revision) FROM runtime_content_preparations WHERE scope_id=:scope_id AND entry_id=:entry_id) AS preparation_revision",
        RecordSchema((Field('entry_id', ID),)), RecordSchema(tuple(Field(k, INT) for k in ('preparations', 'active_batches', 'succeeded_batches', 'failed_batches', 'refused_batches')) + (Field('preparation_revision', INT, nullable=True),)), False)))
    statements.append(('focus_blockers', StatementDefinition("SELECT (SELECT count(*) FROM runtime_content_work WHERE scope_id=:scope_id AND phase NOT IN ('TERMINAL','FROZEN','PARKED','WAITING_ADMISSION')) AS work,(SELECT count(*) FROM runtime_content_preparations WHERE scope_id=:scope_id AND phase IN ('SELECTED','CLAIMED','MEDIA_READY')) AS preparations", RecordSchema(()), RecordSchema((Field('work', INT), Field('preparations', INT))), False)))
    tables.append(TableDefinition('runtime_content_work_phase', 'CREATE INDEX runtime_content_work_phase ON runtime_content_work(scope_id,phase)'))
    from .preparation_disposal import declarations
    extra_tables, extra_statements = declarations()
    tables.extend(extra_tables); statements.extend(extra_statements)
    return StatementCatalog(RepositoryDefinition('runtime', 2, tuple(tables), tuple(s for _, s in statements)), tuple(statements))


class ContentAssembly:
    """Construct all explicit content owners; media is supplied as a real participant."""
    def __init__(self, media: ContentMediaOwnership | None = None,
                 media_repositories: tuple[RepositoryDefinition, ...] = (), *, publication=None, utc_now_us: Callable[[], int] = lambda: time.time_ns() // 1000,
                 information_format: bool = False, text_format: bool = False, semantic_format: bool = False, daily_format: bool = False):
        if (media is None) != (not media_repositories):
            raise ValueError('A media owner and its declarations must be supplied together.')
        self.media = media; self.publication = publication
        self.utc_now_us = utc_now_us
        if (type(information_format) is not bool or type(text_format) is not bool or type(semantic_format) is not bool
                or type(daily_format) is not bool or text_format and not information_format or semantic_format and (not information_format or text_format)
                or daily_format and (not information_format or text_format or semantic_format)):
            raise ValueError('The assembly format must be selected explicitly.')
        self.information_format = information_format
        self.text_format = text_format
        self.semantic_format = semantic_format or daily_format
        self.daily_format = daily_format
        self.daily_input_failures: DailyImageWork | None = None
        self.goal_effects: CandidateGoalEffects | None = None
        from companion_memory.memory.information_repository import information_memory_catalog
        self.catalogs = (information_ingress_catalog() if information_format else ingress_content_catalog(), buffer_content_catalog(), runtime_content_catalog(daily_format=daily_format),
                         candidate_catalog(information_format=information_format,daily_format=daily_format), information_memory_catalog(semantic_format=self.semantic_format,daily_format=daily_format) if information_format else memory_catalog(), history_catalog())
        if self.semantic_format:
            from companion_memory.persistence.text_records import extend_catalog
            from companion_memory.memory.initial_self import initial_self_catalog
            from companion_memory.memory.semantic_repository import semantic_memory_catalog
            from companion_memory.cognition.fixed_memory_repository import fixed_memory_catalog
            self.catalogs=tuple(extend_catalog(c,fixed_memory_catalog(),4) if c.definition.owner_module=='cognition' else
                extend_catalog(extend_catalog(c,initial_self_catalog(),3),semantic_memory_catalog(),4)
                if c.definition.owner_module=='memory' else c for c in self.catalogs)
        if text_format:
            from companion_memory.persistence.text_records import extend_catalog
            from companion_memory.memory.initial_self import initial_self_catalog
            from companion_memory.cognition.text_context import context_catalog
            self.catalogs=tuple(extend_catalog(c,context_catalog(),3) if c.definition.owner_module=='cognition' else
                extend_catalog(c,initial_self_catalog(),3) if c.definition.owner_module=='memory' else c for c in self.catalogs)
        if daily_format:
            from companion_memory.persistence.text_records import extend_catalog
            from companion_memory.memory.subject_origins import subject_origin_catalog
            from companion_memory.memory.daily_application import application_catalog
            from companion_memory.cognition.reasoning_records import reasoning_catalog
            from companion_memory.cognition.daily_material import context_catalog as daily_context_catalog
            from .daily_schedule_records import schedule_catalog
            from .daily_initialization import initialization_catalog
            additions={'memory':extend_catalog(subject_origin_catalog(),application_catalog(),5),'cognition':extend_catalog(reasoning_catalog(),daily_context_catalog(),5),'runtime':extend_catalog(schedule_catalog(),initialization_catalog(),5)}
            self.catalogs=tuple(extend_catalog(c,additions[c.definition.owner_module],5) if c.definition.owner_module in additions else c for c in self.catalogs)
        self.repositories = tuple(c.definition for c in self.catalogs) + media_repositories + (publication.repositories if publication is not None else ())
        self._bound = False
        self._verified_terminals = {}
        self._unsent_learning = {}
        from .causes import CommandCauses
        self.causes = CommandCauses()
        ids = {r.owner_module: r for r in self.repositories}
        key = (Field('operation_id', ID),)
        inputs = {
            'initialize_content_runtime': (),
            'register_content_entry': (Field('entry_id', ID), Field('host_id', ID), Field('platform_id', ID), Field('external_entry_id', BoundedTextSchema(512))),
            'accept_media_event': (Field('entry_id', ID), Field('event', BoundedTextSchema(8192))),
            'select_content_preparation': (Field('entry_id', ID), Field('preparation_id', ID), Field('batch_id', ID), Field('run_id', ID)),
            'claim_content_preparation': (Field('preparation_id', ID), Field('expected_revision', REVISION), Field('owner_generation', REVISION)),
            'complete_content_preparation': (Field('preparation_id', ID), Field('expected_revision', REVISION), Field('owner_generation', REVISION)),
            'freeze_content_batch': (Field('preparation_id', ID), Field('expected_revision', REVISION), Field('owner_generation', REVISION)),
            'associate_content_request': (Field('batch_id', ID), Field('expected_revision', REVISION), Field('generation', REVISION), Field('provider_operation_key', ID), Field('model_binding', BoundedTextSchema(8192))),
            'confirm_content_request': (Field('batch_id', ID), Field('generation', REVISION), Field('expected_revision', REVISION), Field('request_id', ID)),
            'close_learning_admission': (Field('batch_id', ID), Field('generation', REVISION), Field('expected_revision', REVISION)),
            'reopen_learning_admission': (Field('batch_id', ID), Field('generation', REVISION), Field('expected_revision', REVISION), Field('trigger_key', ID), Field('expected_epoch', REVISION)),
            'park_content_work': (Field('batch_id', ID), Field('generation', REVISION), Field('expected_revision', REVISION)),
            'store_content_candidate': (Field('batch_id', ID), Field('expected_revision', REVISION), Field('generation', REVISION),
                Field('manifest', BoundedTextSchema(8192 if daily_format else 4096)), Field('leaves', SequenceSchema(BoundedTextSchema(8192), 0, 8))),
        }
        participants = {
            'initialize_content_runtime': ('runtime',), 'register_content_entry': ('ingress', 'buffers'),
            'accept_media_event': ('ingress', 'buffers') + (('media',) if media else ()),
            'select_content_preparation': ('runtime', 'buffers', 'ingress') + (('media',) if media else ()),
            'close_learning_admission': ('runtime',), 'reopen_learning_admission': ('runtime',),
            'confirm_content_request': ('runtime',), 'park_content_work': ('runtime',),
            'complete_content_preparation': ('runtime',) + (('media',) if media else ()),
            'claim_content_preparation': ('runtime',), 'freeze_content_batch': ('runtime', 'buffers', 'ingress') + (('media',) if media else ()),
            'associate_content_request': ('runtime',), 'store_content_candidate': ('runtime', 'cognition', 'ingress') + (('media',) if media else ()),
        }
        for terminal in ('published', 'without_objects'):
            name = 'commit_content_' + terminal
            inputs[name] = (Field('batch_id', ID), Field('candidate_id', ID), Field('expected_revision', REVISION), Field('generation', REVISION),
                Field('readable_objects', SequenceSchema(ID, 0, 128)), Field('readable_subjects', SequenceSchema(ID, 0, 128)))
            participants[name] = ('runtime', 'cognition', 'ingress', 'buffers') + (('memory',) if terminal == 'published' else ()) + (('media',) if media else ())
        if media:
            for name in tuple(inputs):
                if 'media' in participants[name]:
                    participants[name] = tuple(owner for owner in participants[name] if owner != 'media')
                    inputs[name + '_with_media'] = inputs[name]
                    participants[name + '_with_media'] = participants[name] + ('media',)
        definitions = []
        for name, fields in inputs.items():
            owners = participants[name]
            requirements = tuple(AuditRequirement(owner, owner + '_content', name.upper(), 1, ('APPLY',), owner_fact(owner)) for owner in owners)
            bindings = tuple(AuditResultBinding(r.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
                AuditFieldBinding('change', 'RESULT', ('facts', r.owner_module)),
            )) for r in requirements)
            def handler(uow, values, operation=name):
                return self.run_handler(operation, uow, values, self._handle)
            definitions.append(ResultBoundCommandDefinition('runtime', name, 1, RecordSchema(key + fields), 1,
                result_schema(owners,text_format=text_format and name in ('associate_content_request','store_content_candidate','commit_content_published','commit_content_without_objects')),
                tuple(ids.values()), requirements, handler, RecordSchema((Field('actor', ID),)), bindings))
        from companion_memory.memory.maintenance import MaintenanceAssembly
        self.maintenance = MaintenanceAssembly(self)
        from .media_commands import MediaCommands
        self.media_commands = MediaCommands(self) if media is not None else None
        from .preparation_disposal import PreparationDisposal
        self.preparation_disposal = PreparationDisposal(self)
        from .content_modes import ContentModeCommands
        self.modes = ContentModeCommands(self)
        from .content_transfer import ContentTransfer
        self.transfers = ContentTransfer(self)
        from .candidate_application import CandidateApplication
        self.candidate_application = CandidateApplication(self)
        self.commands = tuple(definitions) + self.candidate_application.commands + self.transfers.commands + self.modes.commands + self.preparation_disposal.commands + self.maintenance.commands + (self.media_commands.commands if self.media_commands else ())
        self._semantic_definitions = {d.operation_kind: d for d in self.commands}
        if daily_format:
            from dataclasses import replace
            def daily_result(definition):
                fields=tuple(Field(field.name,enum('REMOTE_PROVIDER' if field.name=='model_adapter' else 'MODEL_VALIDATED'))
                    if field.name in ('model_adapter','candidate_origin') else field for field in definition.result_schema.fields)
                return replace(definition,result_schema=RecordSchema(fields))
            self._semantic_definitions={name:daily_result(definition) for name,definition in self._semantic_definitions.items()}
            self.commands=tuple(self._semantic_definitions.values())
        if information_format:
            from .information_content import extend_commands
            changed = frozenset(d.operation_kind for d in tuple(definitions) + self.candidate_application.commands + self.maintenance.commands)
            self._semantic_definitions = extend_commands(self._semantic_definitions, changed, self)
            self.commands = tuple(self._semantic_definitions.values())
        if text_format:
            from .text_context_commands import TextContextCommands
            self.text_commands=TextContextCommands(self)
            self.repositories+=(self.text_commands.catalog.definition,)
            self._semantic_definitions['stage_learning_context']=self.text_commands.definition
            self.commands+=(self.text_commands.definition,)

    def command_definition(self, semantic_kind: str) -> ResultBoundCommandDefinition:
        """Resolve a fixed semantic operation to this assembly's actual command."""
        definition = self._semantic_definitions.get(semantic_kind)
        if definition is None:
            raise OwnerFailure('INVALID_INPUT', 'input', 'UNSUPPORTED_VERSION')
        return definition

    def replace_static_command(self, semantic_kind: str, definition: ResultBoundCommandDefinition) -> None:
        """Install an explicit native variant before any owner is bound."""
        previous = self.command_definition(semantic_kind)
        if self._bound or definition.operation_kind != previous.operation_kind or definition.owner_namespace != previous.owner_namespace:
            raise ValueError('A static replacement must preserve its native operation identity.')
        self._semantic_definitions[semantic_kind] = definition
        self.commands = tuple(self._semantic_definitions.values())

    def bind(self, storage: PersistenceService, configuration: StoredContentConfiguration | StoredTextConfiguration | StoredSemanticConfiguration | StoredDailyConfiguration, instance_id: str) -> ContentAssembly:
        """Bind the unique real owners after full configuration persistence is confirmed."""
        valid=(stored_daily_configuration_issue(configuration) is None and cast(StoredDailyConfiguration,configuration).scope_id==instance_id) if self.daily_format else (stored_semantic_configuration_issue(configuration) is None) if self.semantic_format else (stored_text_configuration_issue(configuration) is None) if self.text_format else type(configuration) is StoredContentConfiguration
        if self._bound or not valid:
            raise ValueError('Content assembly requires native stored configuration and one binding.')
        self.storage, self.configuration, self.instance_id = storage, configuration, instance_id
        self.source_revisions=tuple(item for item in configuration.revisions if not (self.text_format or self.daily_format) or item[0] not in ('information','text_learning','daily_cognition'))
        catalogs = {c.definition.owner_module: c for c in self.catalogs}
        settings = configuration.candidate.content
        self.history = HistoryBinding(catalogs['logging_service'], storage, instance_id,
            settings.integer('audit.history_items_per_operation'), settings.integer('audit.history_item_max_bytes'), configuration.candidate.foundation,text_format=self.text_format or self.semantic_format)
        self.ingress = ContentIngressTransactions(catalogs['ingress'], storage, configuration, instance_id, self.media)
        self.buffers = ContentBufferTransactions(catalogs['buffers'], storage, instance_id, self.ingress)
        self.memory = MemoryTransactions(catalogs['memory'], storage, configuration, instance_id, self.history, self.ingress)
        self.cognition = CandidateBinding(catalogs['cognition'], storage, instance_id, configuration.database_id,
            settings.integer('cognition.candidate_item_limit'), settings.integer('cognition.candidate_item_max_bytes'), settings.integer('cognition.candidate_max_bytes'),
            allow_goals=self.information_format and not self.text_format,text_format=self.text_format,daily_format=self.daily_format)
        from .source_rows import RuntimeSourceRows
        self.rows = RuntimeSourceRows(catalogs['runtime'], storage, instance_id)
        self._lease = storage.claim_module_owner(catalogs['runtime'].definition)
        if self._lease is None: raise ValueError('Runtime owner is unavailable.')
        self.operations = {name: storage.bind_operation(d, instance_id) for name, d in self._semantic_definitions.items()}
        self._bound = True
        if self.text_format:
            from .text_content import TextContentTransactions
            self.text_transactions=TextContentTransactions(self)
            self.text_commands.bind()
        if self.publication is not None: self.publication.bind(storage, instance_id)
        return self

    def retain_learning_terminal(self, evidence: object) -> None:
        """Trusted local coordination retains one native result-owner attestation."""
        from companion_memory.provider.terminal_evidence import issued_terminal, VerifiedTerminal
        if not issued_terminal(evidence) or type(evidence) is not VerifiedTerminal or not self._bound:
            raise OwnerFailure('ACCESS_DENIED', 'candidate', 'BINDING_MISMATCH')
        request = evidence.request
        if (evidence._provider._ledger.storage is not self.storage or evidence.database_id != self.configuration.database_id
                or request['caller_module'] != 'cognition' or request['extension_id'] is not None or request['caller_scope'] != self.instance_id
                or request['result_owner'] != 'cognition' or request['capability'] != 'GENERATION' or request['task_role'] != 'LEARNING'):
            raise OwnerFailure('ACCESS_DENIED', 'candidate', 'BINDING_MISMATCH')
        if request['object_id'] not in self._verified_terminals and len(self._verified_terminals) >= self.configuration.candidate.runtime.integer('runtime.max_active_entries'):
            raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL')
        self._verified_terminals[request['object_id']] = evidence

    def _result(self, uow: UnitOfWork, values: MappingProxyType[str, Value], state: str, entry: str,
                *, audit: dict[str, dict[str, Value]] | None = None, text_result: bool = False,
                extra_targets: tuple[MappingProxyType[str,Value],...] = (), **changes: Value) -> dict[str, Value]:
        row_counts = self.storage.transaction_row_changes(uow)
        owners = self.storage.transaction_audit_owners(uow)
        if any(row_counts.get(owner, 0) == 0 for owner in owners):
            raise OwnerFailure('STORAGE_FAILED', 'audit', 'AUDIT_REQUIRED')
        # Each owner records the durable identities of its actual effects. The
        # enclosing result already carries the shared batch/candidate identity.
        reference_names = {
            'runtime': ('preparation_id', 'batch_id', 'work_id'),
            'cognition': ('candidate_id', 'request_id'),
            'memory': ('source_id', 'root_id', 'plan_id'),
            'logging_service': ('root_id', 'plan_id'),
            'ingress': ('preparation_id', 'batch_id', 'plan_id'),
            'media': ('preparation_id', 'work_id', 'request_id', 'plan_id'),
            'buffers': ('preparation_id', 'batch_id'),
        }
        complete = {**values, **changes}
        facts = {owner: MappingProxyType((audit or {})[owner]) if owner == 'goals' else MappingProxyType({'rows_changed': row_counts[owner], 'state': state,
            'references': tuple(MappingProxyType({'name': key, 'object_id': complete[key], 'revision': None})
                for key in reference_names[owner] if type(complete.get(key)) is str),
            'counts': (),
            **({'provenance': 'NONE', 'guard_created': None, 'reused': None} if owner == 'media' else {}),
            **({'history_ids': tuple(record(item)['history_id'] for item in sequence(changes.get('history', ())))} if owner == 'logging_service' else {}),
            **((audit or {}).get(owner, {}))}) for owner in owners}
        objects = tuple(record(ref) for ref in sequence(changes.get('object_refs', ())))
        targets = tuple(MappingProxyType({k: ref[k] for k in ('object_id', 'previous_revision', 'revision')}) for ref in objects)
        if text_result:
            targets+=self.text_transactions.audit_targets(uow,values,changes,state)+extra_targets
        return {'operation_id': values['operation_id'], 'entry_id': entry, 'batch_id': None, 'candidate_id': None,
            'source_id': None, 'terminal': state, 'object_refs': (), 'history': (), 'retired_source_ids': (),
            'targets': targets or (MappingProxyType({'object_id': changes.get('operation_id', values['operation_id']), 'previous_revision': None, 'revision': 1}),),
            'storage_execution': 'ACTUAL', 'model_adapter': 'REMOTE_PROVIDER' if text_result or self.daily_format else 'SIMULATED', 'candidate_origin': 'MODEL_VALIDATED' if text_result or self.daily_format else 'SYNTHETIC',
            'facts': MappingProxyType(facts), **changes, 'history': tuple(record(item)['history_id'] for item in sequence(changes.get('history', ())))}

    def _get(self, name: str, uow: UnitOfWork, key: str, value: Value) -> MappingProxyType[str, Value]:
        rows = self.rows.stage(name + '_get', uow, {key: value})
        if not rows: raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'SOURCE_CHANGED')
        return rows[0]

    def run_handler(self, name, uow, values, handler):
        """Retain only the first bounded owner cause for an active command waiter."""
        from .records import DomainFailure
        try: return handler(name, uow, values)
        except OwnerFailure as failure:
            self.causes.record(name, values, DomainFailure(failure.code, failure.field, failure.reason, failure.cleanup_pending))
            raise

    def with_transaction_time(self, values: MappingProxyType[str, Value]) -> MappingProxyType[str, Value]:
        """Observe a trusted clock during execution without changing command identity."""
        now = self.utc_now_us()
        if type(now) is not int or not 0 <= now < 2**63:
            raise OwnerFailure('RESOURCE_FAILED', 'state', 'CLOCK_UNAVAILABLE')
        return MappingProxyType({**values, 'now_us': now})

    def _handle(self, name: str, uow: UnitOfWork, v: MappingProxyType[str, Value]):
        v = self.with_transaction_time(v)
        name = name.removesuffix('_with_media')
        if not self._bound: raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        if name == 'initialize_content_runtime':
            self.rows.stage('mode_insert', uow, {'mode_id': 'instance_mode', 'state': 'NORMAL', 'epoch': 1, 'run_id': None, 'publication_id': None, 'deadline_at_us': None})
            return self._result(uow, v, 'NORMAL', self.instance_id)
        mode = self._get('mode', uow, 'mode_id', 'instance_mode')
        if name not in ('accept_media_event', 'store_content_candidate', 'commit_content_published', 'commit_content_without_objects', 'confirm_content_request', 'park_content_work', 'close_learning_admission') and mode['state'] not in ('NORMAL', 'DRAINING'):
            raise OwnerFailure('MODE_BLOCKED', 'state', 'DREAMING')
        if name == 'register_content_entry':
            eid = cast(str, v['entry_id'])
            self.configuration.candidate.platform(cast(str, v['platform_id']))
            self.ingress.register(uow, eid, cast(str, v['host_id']), cast(str, v['platform_id']), cast(str, v['external_entry_id']))
            self.buffers.rows.stage('register', uow, {'entry_id': eid})
            return self._result(uow, v, 'REGISTERED', eid)
        if name == 'accept_media_event':
            eid = cast(str, v['entry_id']); state = self.buffers.current(uow, eid)
            mid = self.ingress.accept(uow, eid, cast(str, v['event']).encode(), cast(int, state['next_sequence']), cast(int, v['now_us']))
            staged = self.buffers.rows.stage('state_count', uow, {'entry_id': eid, 'state': 'STAGED'})[0]['count']
            self.buffers.append(uow, eid, mid, 'NORMAL' if mode['state'] in ('NORMAL', 'DRAINING') and not staged else 'STAGED')
            audit = {}
            if self.media is not None:
                audit['media'] = dict(self.media.acceptance_fact(uow, eid, mid))
            return self._result(uow, v, 'ACCEPTED', eid, operation_id=mid, audit=audit)
        if name == 'select_content_preparation':
            eid = cast(str, v['entry_id']); entries = self.ingress.rows.stage('entry', uow, {'entry_id': eid})
            if not entries: raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
            entry = entries[0]; platform = self.configuration.candidate.platform(cast(str, entry['platform_id']))
            selected = self.buffers.select(uow, eid, platform.count('history_context_count'), platform.count('target_count'), platform.count('recent_context_count'))
            if not selected: raise OwnerFailure('PRECONDITION_FAILED', 'source', 'WINDOW_CHANGED')
            members = []
            for role, mid in selected:
                event = self.ingress.event(uow, mid)
                payload = self.ingress.rows.stage('payload', uow, {'message_id': mid})
                if not payload: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                from companion_memory.ingress.media_events import decode_media_event
                raw = decode_media_event(cast(str, payload[0]['body']).encode(), 8192, occurrence_limit=2, text_limit=512)
                selections = ()
                if sequence(raw['media']):
                    if self.media is None: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'media', 'OWNER_MISSING')
                    selections = self.media.preparation_selections(uow, eid, mid)
                members.append(MappingProxyType({'role': role, 'message_id': mid, 'entry_seq': event['entry_seq'],
                    'payload_digest': event['digest'], 'received_at_us': event['received_at_us'],
                    'transferred_at_us': event['transferred_at_us'], 'media': selections}))
            source: dict[str, Value] = {'source_version': 1, 'source_id': stable('source', self.configuration.database_id, eid, v['batch_id']),
                'batch_id': v['batch_id'], 'run_id': v['run_id'], 'entry_id': eid, 'host_id': entry['host_id'], 'platform_id': entry['platform_id'],
                'config_snapshot_id': self.configuration.snapshot_id, 'domain_revisions': tuple(MappingProxyType({'domain_id': k, 'revision': r}) for k, r in self.source_revisions),
                'material_contract_ref': 'DAILY_CONTEXT_V1' if self.daily_format else 'TEXT_CONTEXT_V1' if self.text_format else 'complete_source_base64:1', 'frozen_at_us': v['now_us'], 'ordered_members': tuple(members), 'digest': 'pending'}
            source['digest'] = source_digest(MappingProxyType(source)); manifest = isolate_preparation(source)
            self.buffers.reserve(uow, eid, cast(str, v['preparation_id']))
            for _, mid in selected: self.ingress.retain_payload(uow, mid, 'PREPARATION', cast(str, v['preparation_id']))
            if self.media and any(sequence(m['media']) for m in members):
                self.media.retain_consumer(uow, 'PREPARATION', cast(str, v['preparation_id']), tuple(members))
            self.rows.stage('preparations_insert', uow, {'preparation_id': v['preparation_id'], 'entry_id': eid, 'batch_id': v['batch_id'],
                'run_id': v['run_id'], 'revision': 1, 'owner_generation': 0, 'phase': 'SELECTED',
                'deadline_at_us': cast(int, v['now_us']) + self.configuration.candidate.content.integer('media.preparation_total_timeout_ms') * 1000,
                'manifest': encode_content(manifest, 8192).decode(), 'preparation_version': 1, 'trigger_key': v['operation_id'],
                'mode_epoch': mode['epoch'], 'checked_entry_revision': self.buffers.current(uow, eid)['revision'], 'window_digest': source['digest'],
                'started_at_us': v['now_us'], 'spent_ms': 0, 'last_observed_at_us': v['now_us'], 'parked_from_phase': None})
            return self._result(uow, v, 'SELECTED', eid, batch_id=v['batch_id'])
        if name in ('claim_content_preparation', 'complete_content_preparation', 'freeze_content_batch'):
            preparation = self._get('preparations', uow, 'preparation_id', v['preparation_id'])
            if (preparation['revision'] != v['expected_revision'] or cast(int, v['now_us']) >= cast(int, preparation['deadline_at_us'])
                or cast(int, v['now_us']) < cast(int, preparation['last_observed_at_us']) or preparation['mode_epoch'] != mode['epoch']):
                raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
            if name == 'claim_content_preparation':
                if preparation['phase'] != 'SELECTED': raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
                self.rows.stage('preparations_update', uow, {**preparation, 'revision': cast(int, preparation['revision']) + 1,
                    'phase': 'CLAIMED', 'owner_generation': v['owner_generation']})
                return self._result(uow, v, 'CLAIMED', cast(str, preparation['entry_id']), batch_id=preparation['batch_id'])
            if preparation['phase'] not in (('CLAIMED', 'MEDIA_READY') if name == 'complete_content_preparation' else ('MEDIA_READY',)) or preparation['owner_generation'] != v['owner_generation']:
                raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
            manifest = decode_preparation(cast(str, preparation['manifest'])); eid = cast(str, preparation['entry_id'])
            platform = self.configuration.candidate.platform(cast(str, manifest['platform_id']))
            selected = self.buffers.select(uow, eid, platform.count('history_context_count'), platform.count('target_count'), platform.count('recent_context_count'))
            members = tuple(record(m) for m in sequence(manifest['ordered_members']))
            if selected != tuple((cast(str, m['role']), cast(str, m['message_id'])) for m in members):
                raise OwnerFailure('PRECONDITION_FAILED', 'source', 'WINDOW_CHANGED')
            completed = []
            for member in members:
                selections = self.media.current_selections(uow, eid, cast(str, member['message_id'])) if self.media and sequence(member['media']) else sequence(member['media'])
                if name == 'freeze_content_batch' and selections != sequence(member['media']):
                    raise OwnerFailure('PRECONDITION_FAILED', 'interpretation', 'SELECTION_CHANGED')
                completed.append(MappingProxyType({**member, 'media': selections}))
            updated = {**manifest, 'ordered_members': tuple(completed), 'frozen_at_us': v['now_us']}
            updated['digest'] = source_digest(MappingProxyType(updated)); manifest = isolate_source(updated)
            members = tuple(completed)
            if name == 'complete_content_preparation':
                changed = self.media.pin_preparation_versions(uow, cast(str, v['preparation_id']), members) if self.media else 0
                owners = self.storage.transaction_audit_owners(uow)
                if bool(changed) != ('media' in owners): raise OwnerFailure('PRECONDITION_FAILED', 'interpretation', 'SELECTION_CHANGED')
                self.rows.stage('preparations_update', uow, {**preparation, 'revision': cast(int, preparation['revision']) + 1,
                    'phase': 'MEDIA_READY', 'manifest': encode_content(manifest, 8192).decode(), 'last_observed_at_us': v['now_us'],
                    'spent_ms': max(cast(int, preparation['spent_ms']), (cast(int, v['now_us']) - cast(int, preparation['started_at_us'])) // 1000)})
                return self._result(uow, v, 'MEDIA_READY', eid, batch_id=manifest['batch_id'])
            for member in members:
                self.ingress.verify_member(uow, eid, member)
                mid = cast(str, member['message_id'])
                self.ingress.retain_payload(uow, mid, 'BATCH', cast(str, manifest['batch_id']))
                event = self.ingress.event(uow, mid)
                self.ingress.release_payload(uow, mid, 'PREPARATION', cast(str, v['preparation_id']), cast(int, event['references_revision']))
            if self.media and any(sequence(m['media']) for m in members):
                self.media.retain_consumer(uow, 'BATCH', cast(str, manifest['batch_id']), members)
                self.media.release_consumer(uow, 'PREPARATION', cast(str, v['preparation_id']), members)
            self.buffers.freeze(uow, eid, cast(str, v['preparation_id']), cast(str, manifest['batch_id']))
            self.rows.stage('preparations_update', uow, {**preparation, 'revision': cast(int, preparation['revision']) + 1, 'phase': 'FROZEN', 'manifest': encode_content(manifest, 8192).decode(),
                'checked_entry_revision': self.buffers.current(uow, eid)['revision'], 'last_observed_at_us': v['now_us'],
                'spent_ms': (cast(int, v['now_us']) - cast(int, preparation['started_at_us'])) // 1000})
            self.rows.stage('batches_insert', uow, {'batch_id': manifest['batch_id'], 'entry_id': eid, 'run_id': manifest['run_id'],
                'terminal': 'FROZEN', 'manifest': encode_content(manifest, 8192).decode()})
            self.rows.stage('work_insert', uow, {'batch_id': manifest['batch_id'], 'admission_generation': 1, 'admission_trigger': preparation['trigger_key'], 'generation': v['owner_generation'], 'revision': 1,
                'phase': 'FROZEN', 'provider_operation_key': None, 'provider_request_id': None, 'handoff_ref': None, 'candidate_id': None, 'model_binding': None})
            return self._result(uow, v, 'FROZEN', eid, batch_id=manifest['batch_id'])
        batch = self._get('batches', uow, 'batch_id', v['batch_id'])
        if self.media and name in ('associate_content_request', 'store_content_candidate', 'commit_content_published', 'commit_content_without_objects'):
            frozen = decode_source(cast(str, batch['manifest']))
            for member_value in sequence(frozen['ordered_members']):
                member = record(member_value)
                self.media.verify_selections(uow, cast(str, batch['entry_id']), cast(str, member['message_id']), tuple(record(s) for s in sequence(member['media'])))
        work = self._get('work', uow, 'batch_id', v['batch_id'])
        if work['revision'] != v['expected_revision'] or work['generation'] != v['generation']:
            raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
        eid = cast(str, batch['entry_id'])
        if name == 'close_learning_admission':
            from .learning_admissions import close_admission
            return close_admission(self, uow, work, batch, v)
        if name == 'reopen_learning_admission':
            from .learning_admissions import reopen_admission
            return reopen_admission(self, uow, work, batch, v)
        if name == 'park_content_work':
            if work['phase'] != 'FROZEN' or work['provider_operation_key'] is not None:
                raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
            self.rows.stage('work_update', uow, {**work, 'revision': cast(int, work['revision']) + 1, 'phase': 'PARKED'})
            return self._result(uow, v, 'PARKED', eid, batch_id=v['batch_id'])
        if name == 'confirm_content_request':
            if work['phase'] != 'REQUEST_ASSOCIATED' or work['provider_request_id'] is not None:
                raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
            self.rows.stage('work_update', uow, {**work, 'revision': cast(int, work['revision']) + 1, 'provider_request_id': v['request_id']})
            return self._result(uow, v, 'REQUEST_ASSOCIATED', eid, batch_id=v['batch_id'])
        if name == 'associate_content_request':
            if work['phase'] not in ('FROZEN', 'PARKED'): raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
            if self.text_format:self.text_transactions.verify_association(uow,v,work,batch)
            self.rows.stage('work_update', uow, {**work, 'revision': cast(int, work['revision']) + 1,
                'phase': 'REQUEST_ASSOCIATED', 'provider_operation_key': v['provider_operation_key'], 'model_binding': v['model_binding']})
            return self._result(uow, v, 'REQUEST_ASSOCIATED', eid, batch_id=v['batch_id'],text_result=self.text_format)
        if name == 'store_content_candidate':
            if work['phase'] != 'REQUEST_ASSOCIATED': raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
            settings = self.configuration.candidate.content
            candidate = self.cognition.isolate(decode_content(cast(str, v['manifest']).encode(), self.cognition.manifest_bytes),
                tuple(decode_content(cast(str, leaf).encode(), 8192) for leaf in sequence(v['leaves'])))
            m = candidate.manifest
            binding = decode_content(cast(str, work['model_binding']).encode(), 8192)
            if type(binding) is not dict or binding.get('transform_version') != m['transform_version']:
                raise OwnerFailure('ACCESS_DENIED', 'candidate', 'BINDING_MISMATCH')
            evidence = self._verified_terminals.get(m['provider_request_id'])
            from companion_memory.provider.terminal_evidence import issued_terminal,VerifiedTerminal
            if not issued_terminal(evidence): raise OwnerFailure('ACCESS_DENIED', 'candidate', 'BINDING_MISMATCH')
            evidence=cast(VerifiedTerminal,evidence)
            request = evidence.request
            frozen_manifest = decode_source(cast(str, batch['manifest']))
            proposed = 'SUCCEEDED' if request['outcome'] == 'SUCCEEDED' else 'SENSITIVE_DROPPED' if request['outcome'] == 'SENSITIVE_REFUSAL' else 'FAILED_DROPPED'
            if self.text_format:
                self.text_transactions.verify_candidate(uow,work,batch,candidate,evidence)
                proposed=cast(str,m['terminal_proposal'])
            if (work['provider_request_id'] is not None and work['provider_request_id'] != m['provider_request_id'] or request['operation_key'] != work['provider_operation_key'] or record(cast(Value,request['attribution']))['batch_id'] != v['batch_id']
                    or record(cast(Value,request['attribution']))['run_id'] != batch['run_id'] or not self.text_format and request['handoff_id'] != m['handoff_ref']
                    or m['terminal_proposal'] != proposed or m['source_id'] != frozen_manifest['source_id']
                    or m['config_snapshot_id'] != self.configuration.snapshot_id):
                raise OwnerFailure('ACCESS_DENIED', 'candidate', 'BINDING_MISMATCH')
            self.cognition.stage(uow, candidate, batch_id=cast(str, v['batch_id']), run_id=cast(str, batch['run_id']),
                work_generation=cast(int, work['generation']), request_id=cast(str, m['provider_request_id']), handoff_ref=cast(str | None, m['handoff_ref']))
            members = tuple(record(member) for member in sequence(frozen_manifest['ordered_members']))
            for member in members:
                self.ingress.retain_payload(uow, cast(str, member['message_id']), 'CANDIDATE', cast(str, m['candidate_id']))
            if self.media and any(sequence(member['media']) for member in members):
                self.media.retain_consumer(uow, 'CANDIDATE', cast(str, m['candidate_id']), members)
            self.rows.stage('work_update', uow, {**work, 'revision': cast(int, work['revision']) + 1, 'phase': 'CANDIDATE_STORED',
                'provider_request_id': m['provider_request_id'], 'handoff_ref': m['handoff_ref'], 'candidate_id': m['candidate_id']})
            return self._result(uow, v, 'CANDIDATE_STORED', eid, batch_id=v['batch_id'], candidate_id=m['candidate_id'],
                audit={'cognition': {'counts': (MappingProxyType({'name': 'candidate_leaves', 'count': len(candidate.leaves)}),)}},text_result=self.text_format)
        if name.startswith('commit_content_'):
            return self.finish_candidate(uow, v, name)
        raise InvalidValue()

    async def read_daily_batch(self,batch_id:str):
        """Read one complete original daily source and its actual runtime work."""
        if not self.daily_format:raise InvalidValue()
        batches=await self.rows.read('batches_get',{'batch_id':batch_id})
        work=await self.rows.read('work_get',{'batch_id':batch_id})
        if len(batches)!=1 or len(work)!=1:raise InvalidValue()
        source=decode_source(cast(str,batches[0]['manifest']))
        if source['batch_id']!=batch_id or work[0]['batch_id']!=batch_id:raise InvalidValue()
        return MappingProxyType({'source':source,'work':work[0],'batch':batches[0]})

    def participate_daily_work(self,uow:UnitOfWork,batch_id:str):
        """Return the exact native batch and work in one transaction."""
        if not self.daily_format:raise InvalidValue()
        return self._get('batches',uow,'batch_id',batch_id),self._get('work',uow,'batch_id',batch_id)

    async def read_daily_preparation(self,batch_id:str):
        """Read the original preparation start and mode; never restart its clock."""
        if not self.daily_format:raise InvalidValue()
        rows=await self.rows.read('daily_preparation_for_batch',{'batch_id':batch_id})
        if len(rows)!=1:raise InvalidValue()
        return rows[0]

    def participate_daily_preparation(self,uow:UnitOfWork,batch_id:str):
        """Compare the same original preparation inside the caller's native UoW."""
        if not self.daily_format:raise InvalidValue()
        rows=self.rows.stage('daily_preparation_for_batch',uow,{'batch_id':batch_id})
        if len(rows)!=1:raise InvalidValue()
        return rows[0]

    def participate_daily_source(self,uow:UnitOfWork,batch_id:str,generation:int,revision:int):
        """Verify a frozen or staged daily batch without changing its identity."""
        batch,work=self.participate_daily_work(uow,batch_id)
        if (work['generation']!=generation or work['revision']!=revision or work['phase'] not in ('FROZEN','PARKED','CANDIDATE_STORED')
                or batch['terminal']!='FROZEN'):raise OwnerFailure('PRECONDITION_FAILED','batch','WORK_FENCED')
        return decode_source(cast(str,batch['manifest']))

    def stage_daily_candidate(self,uow:UnitOfWork,candidate,work,route_ids):
        """Transfer original source protection to the complete checked candidate."""
        manifest=candidate.manifest;source=self.participate_daily_source(uow,cast(str,manifest['batch_id']),cast(int,work['generation']),cast(int,work['revision']))
        self.cognition.stage(uow,candidate,batch_id=cast(str,manifest['batch_id']),run_id=cast(str,manifest['run_id']),work_generation=cast(int,work['generation']),
            request_id=cast(str,manifest['provider_request_id']),handoff_ref=cast(str|None,manifest['handoff_ref']))
        members=tuple(record(m) for m in sequence(source['ordered_members']))
        for member in members:
            self.ingress.verify_member(uow,cast(str,source['entry_id']),member)
            self.ingress.retain_payload(uow,cast(str,member['message_id']),'CANDIDATE',cast(str,manifest['candidate_id']))
        if self.media is not None and any(member['media'] for member in members):self.media.retain_consumer(uow,'CANDIDATE',cast(str,manifest['candidate_id']),members)
        changed=dict(work)|{'revision':cast(int,work['revision'])+1,'phase':'CANDIDATE_STORED','provider_request_id':manifest['provider_request_id'],
            'handoff_ref':manifest['handoff_ref'],'candidate_id':manifest['candidate_id'],'model_binding':encode_content(MappingProxyType({'goal_route_ids':route_ids,'entry_id':source['entry_id']}),8192).decode()}
        self.rows.stage('work_update',uow,changed)
        return MappingProxyType(changed)

    def daily_source_audit_targets(self,uow:UnitOfWork,source,*,include_buffers:bool,release=None,before=None):
        """Name actual reference and FIFO revisions, including old source media."""
        from companion_memory.persistence.daily_results import target
        result={};mid=cast(str,record(sequence(source['ordered_members'])[0])['message_id'])
        event=self.ingress.event(uow,mid);result['ingress']=(target(mid,cast(int,event['references_revision'])),)
        if include_buffers:
            entry=self.buffers.current(uow,cast(str,source['entry_id']))
            result['buffers']=(target(cast(str,source['entry_id']),cast(int,entry['revision'])),)
        blob_ids={cast(str,record(selected)['blob_id']) for raw in sequence(source['ordered_members']) for selected in sequence(record(raw)['media'])}
        if release is not None:blob_ids.update(release.media_blob_ids())
        if blob_ids:
            if self.media is None:raise InvalidValue()
            from companion_memory.media.service import MediaService
            if type(self.media) is not MediaService:raise InvalidValue()
            result['media']=self.media.daily_reference_targets(uow,tuple(sorted(blob_ids))[:1])
        if before is not None:
            for owner,refs in result.items():
                original=before[owner][0]
                if refs[0]['object_id']!=original['object_id'] or refs[0]['revision']<=original['revision']:raise InvalidValue()
                refs[0]['previous_revision']=original['revision']
        return result

    def expire_daily_work(self,uow:UnitOfWork,batch,work,terminal:str):
        """Consume a proven failed original batch without inventing Provider output."""
        if not self.daily_format or batch['terminal']!='FROZEN' or work['phase'] not in ('FROZEN','PARKED','CANDIDATE_STORED'):
            raise OwnerFailure('PRECONDITION_FAILED','batch','WORK_FENCED')
        if terminal not in ('FAILED_DROPPED','SENSITIVE_DROPPED'):raise InvalidValue()
        source=decode_source(cast(str,batch['manifest']));members=tuple(record(m) for m in sequence(source['ordered_members']))
        cid=cast(str|None,work['candidate_id'])
        if cid is not None:
            candidate=self.cognition.load(uow,cid)
            if candidate.manifest['batch_id']!=batch['batch_id']:raise InvalidValue()
            for member in members:
                mid=cast(str,member['message_id']);event=self.ingress.event(uow,mid)
                self.ingress.release_payload(uow,mid,'CANDIDATE',cid,cast(int,event['references_revision']))
            if self.media is not None:self.media.release_consumer(uow,'CANDIDATE',cid,members)
            self.cognition.dispose(uow,candidate)
        self.buffers.terminate(uow,cast(str,batch['entry_id']),cast(str,batch['batch_id']),
            tuple((cast(str,m['role']),cast(str,m['message_id'])) for m in members),terminal,
            self.configuration.candidate.platform(cast(str,source['platform_id'])).count('history_context_count'))
        if self.media is not None:self.media.release_consumer(uow,'BATCH',cast(str,batch['batch_id']),members)
        self.rows.stage('work_update',uow,dict(work)|{'revision':cast(int,work['revision'])+1,'phase':'TERMINAL'})
        self.rows.stage('batches_update',uow,dict(batch)|{'terminal':terminal})

    def participate_daily_batch(self,uow:UnitOfWork,batch_id:str,generation:int,revision:int):
        """Cognition freezes the actual original runtime batch under the same UoW."""
        if not self.daily_format:raise InvalidValue()
        batch=self._get('batches',uow,'batch_id',batch_id);work=self._get('work',uow,'batch_id',batch_id)
        if work['generation']!=generation or work['revision']!=revision or work['phase'] not in ('FROZEN','PARKED') or batch['terminal']!='FROZEN':
            raise OwnerFailure('PRECONDITION_FAILED','batch','WORK_FENCED')
        source=decode_source(cast(str,batch['manifest']))
        for member in sequence(source['ordered_members']):self.ingress.verify_member(uow,cast(str,source['entry_id']),record(member))
        return source

    def finish_candidate(self, uow, v, name, release=None, scope_override=None):
        """Apply real owners and rotate once under the coordinator's fixed audit mask."""
        batch = self._get('batches', uow, 'batch_id', v['batch_id'])
        work = self._get('work', uow, 'batch_id', v['batch_id'])
        if work['revision'] != v['expected_revision'] or work['generation'] != v['generation']:
            raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
        eid = cast(str, batch['entry_id'])
        if work['phase'] != 'CANDIDATE_STORED' or work['candidate_id'] != v['candidate_id']:
            raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
        candidate = self.cognition.load(uow, cast(str, v['candidate_id'])); m = candidate.manifest
        memory_leaves = tuple(leaf for leaf in candidate.leaves if leaf['action'] != 'CREATE_GOAL')
        goal_leaves = tuple(leaf for leaf in candidate.leaves if leaf['action'] == 'CREATE_GOAL')
        terminal = cast(str, m['terminal_proposal']); expected = 'published' if candidate.leaves else 'without_objects'
        if name != 'commit_content_' + expected: raise InvalidValue()
        frozen_source = decode_source(cast(str, batch['manifest']))
        if self.media:
            for raw_member in sequence(frozen_source['ordered_members']):
                member = record(raw_member)
                self.media.verify_selections(uow, eid, cast(str, member['message_id']), tuple(record(s) for s in sequence(member['media'])))
        applied = None; new_source = None
        if self.text_format:
            scope_override=self.text_transactions.apply_scope(uow,v,work,batch,candidate)
        if memory_leaves:
            scope = scope_override or ApplyScope(self.instance_id, cast(str, m['candidate_id']), cast(str, m['batch_id']),
                frozenset(cast(tuple[str, ...], v['readable_objects'])), frozenset(cast(tuple[str, ...], v['readable_subjects'])),
                frozenset(cast(str, leaf['target_id']) for leaf in memory_leaves), frozenset())
            if any(leaf['links'] is not None and any(record(link)['source_id'] == frozen_source['source_id'] for link in sequence(record(leaf['links'])['sources'])) for leaf in memory_leaves):
                new_source = frozen_source
            applied = self.memory.apply_change_set(uow, scope, memory_leaves, new_source, release, cast(int, v['now_us']), cast(str, v['operation_id']), 'LEARNING')
        goal_fact = None
        if goal_leaves:
            if self.goal_effects is None or scope_override is None:
                raise OwnerFailure('ACCESS_DENIED', 'candidate', 'OPERATION_NOT_GRANTED')
            goal_fact = self.goal_effects.apply(uow, candidate, scope_override, work, cast(int, v['now_us']))
        members = tuple((cast(str, record(member)['role']), cast(str, record(member)['message_id'])) for member in sequence(frozen_source['ordered_members']))
        platform = self.configuration.candidate.platform(cast(str, frozen_source['platform_id']))
        for _, mid in members:
            event = self.ingress.event(uow, mid)
            self.ingress.release_payload(uow, mid, 'CANDIDATE', cast(str, v['candidate_id']), cast(int, event['references_revision']))
        if self.media:
            self.media.release_consumer(uow, 'CANDIDATE', cast(str, v['candidate_id']), tuple(record(member) for member in sequence(frozen_source['ordered_members'])))
        retained_history, deleted_payloads = self.buffers.terminate(uow, eid, cast(str, v['batch_id']), members, terminal, platform.count('history_context_count'))
        if self.media:
            self.media.release_consumer(uow, 'BATCH', cast(str, v['batch_id']), tuple(record(m) for m in sequence(frozen_source['ordered_members'])))
        self.cognition.dispose(uow, candidate)
        context_targets=(self.text_transactions.release(uow,v,candidate),) if self.text_format else ()
        self.rows.stage('work_update', uow, {**work, 'revision': cast(int, work['revision']) + 1, 'phase': 'TERMINAL'})
        self.rows.stage('batches_update', uow, {**batch, 'terminal': terminal})
        audit: dict[str, dict[str, Value]] = {'memory': {'counts': applied_counts(applied)}} if applied else {}
        if goal_fact is not None: audit['goals'] = dict(goal_fact)
        if applied:
            audit['logging_service'] = {'counts': (MappingProxyType({'name': 'history_items', 'count': len(applied.history)}),)}
        if release is not None:
            audit['ingress'] = {'counts': tuple(MappingProxyType({'name': key, 'count': count}) for key, count in (
                ('source_payload_references_released', release.payload_references_released), ('source_payloads_deleted', release.payloads_deleted)))}
            audit['media'] = {'counts': tuple(MappingProxyType({'name': key, 'count': count}) for key, count in (
                ('source_blob_references_released', release.blob_references_released), ('source_interpretation_references_released', release.interpretation_references_released)))}
        target_count = sum(role == 'T' for role, _ in members)
        audit['buffers'] = {'counts': tuple(MappingProxyType({'name': key, 'count': count}) for key, count in (('targets_consumed', target_count), ('history_retained', retained_history)))}
        audit.setdefault('ingress', {})['counts'] = cast(tuple, audit.get('ingress', {}).get('counts', ())) + tuple(MappingProxyType({'name': key, 'count': count}) for key, count in (('targets_consumed', target_count), ('rotation_payloads_deleted', deleted_payloads)))
        return self._result(uow, v, terminal, eid, batch_id=v['batch_id'], candidate_id=v['candidate_id'],
            source_id=frozen_source['source_id'] if new_source is not None else None, retired_source_ids=applied.retired_sources if applied else (), object_refs=applied.objects if applied else (), history=applied.history if applied else (),
            audit=audit,text_result=self.text_format,extra_targets=context_targets)

    def close(self) -> bool:
        """Close each owner without prematurely releasing an unfinished storage job."""
        if not self._bound: return True
        results = [owner.close() for owner in (self.memory, self.cognition, self.buffers, self.ingress, self.history)]
        if self.text_format:results.append(self.text_commands.close())
        assert self._lease is not None
        results.append(self._lease.release())
        return all(results)
