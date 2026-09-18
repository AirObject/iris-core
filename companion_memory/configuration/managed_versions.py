"""Configuration-owned immutable candidates and original-key activation decisions.

Object birth identity is unchanged. Each decision names a separately retained
complete configuration version, and runtime consumer facts share its transaction.
Preparing resources and publishing consumers are runtime responsibilities outside
the SQLite transaction; a decided version cannot be silently abandoned.
"""
from __future__ import annotations
from hashlib import sha256
import time
from typing import cast, TYPE_CHECKING

from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence import (Field, RecordSchema, ScalarSchema, SequenceSchema,
    BoundedTextSchema, ResultBoundCommandDefinition, ResultBoundCommand, UnitOfWork,
    AuditFieldBinding, AuditResultBinding)
from companion_memory.persistence.daily_records import DailyRows, Record, ID, UINT, DIGEST, identity
from companion_memory.persistence.daily_results import FACT, TARGETS, target
from companion_memory.persistence.owned_statements import OwnerFailure
from .activation_records import CONFIGURATION_TABLES, CONSUMERS
if TYPE_CHECKING:
    from companion_memory.runtime.configuration_activation import RuntimeConfigurationActivation
from .managed_codec import InitializationValues, candidate_values, restore_candidate
from .managed_resolution import ManagedConfigurationCandidate, managed_snapshot_issue
from .content_codec import dump, entries_digest

VERSION_FACT = RecordSchema((Field('rows_changed', ScalarSchema('integer', 1, 256)), Field('targets', TARGETS)))
ENTRY = RecordSchema((Field('parameter_key', BoundedTextSchema(128)), Field('body', BoundedTextSchema(8192))))
DOMAIN = RecordSchema((Field('domain_id', ID), Field('digest', DIGEST), Field('entries', SequenceSchema(ENTRY, 1, 128))))
CONTENTS = RecordSchema((Field('catalog', BoundedTextSchema(8192)), Field('domains', SequenceSchema(DOMAIN, 6, 6))))


class ManagedVersions:
    def __init__(self, catalog, runtime: RuntimeConfigurationActivation):
        self.catalog, self.runtime = catalog, runtime
        self.binding = None
        self.prepared: tuple[str, str] | None = None
        self.published: tuple[str, str] | None = None
        commands = []
        layouts = (
            ('save_configuration_candidate', (Field('contents', CONTENTS), Field('reason', BoundedTextSchema(512)),
                Field('actor', ID), Field('rollback_of', ID, nullable=True), Field('plan_digest', DIGEST)), False),
            ('prepare_configuration_activation', (Field('plan_digest', DIGEST),), False),
            ('fail_configuration_preparation', (Field('plan_digest', DIGEST),), False),
            ('decide_configuration_activation', (Field('plan_digest', DIGEST),), True),
            ('acknowledge_configuration_consumer', (Field('consumer', ScalarSchema('enum', choices=CONSUMERS)),
                Field('failure', ID, nullable=True)), True),
            ('finish_configuration_activation', (), False),
        )
        for kind, extra, runtime_writer in layouts:
            owners = ('configuration', 'runtime') if runtime_writer else ('configuration',)
            requirements = tuple(AuditRequirement(owner, owner + '_activation', kind.upper(), 1, ('APPLY',),
                VERSION_FACT if owner == 'configuration' else FACT) for owner in owners)
            bindings = tuple(AuditResultBinding(r.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'),
                AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'),
                AuditFieldBinding('target_refs', 'RESULT', ('facts', r.owner_module, 'targets')),
                AuditFieldBinding('change', 'RESULT', ('facts', r.owner_module)))) for r in requirements)
            def handle(uow, values, operation=kind):
                return self.apply(operation, uow, values)
            commands.append(ResultBoundCommandDefinition('configuration', kind, 1,
                RecordSchema((Field('activation_id', ID), Field('version_id', ID), Field('expected_revision', UINT)) + extra),
                1, RecordSchema((Field('activation_id', ID), Field('version_id', ID), Field('state', ID),
                    Field('facts', RecordSchema(tuple(Field(owner, VERSION_FACT if owner == 'configuration' else FACT) for owner in owners))))),
                (catalog.definition, runtime.catalog.definition), requirements, handle,
                RecordSchema((Field('actor', ID),)), bindings))
        self.commands = tuple(commands)

    def bind(self, binding):
        if self.binding is not None:
            raise ValueError('Configuration versions already bound.')
        self.binding = binding
        self.rows = DailyRows(self.catalog, CONFIGURATION_TABLES, binding._storage,
            binding._lease.database_id, binding._scope_id, 'managed-versions')

    def base(self, object_id: str, old: Record | None = None) -> dict:
        now = time.time_ns() // 1000
        if old is not None:
            return dict(old) | {'revision': cast(int, old['revision']) + 1,
                'updated_at_us': max(now, cast(int, old['updated_at_us']))}
        return {'format_version': 1, 'object_id': object_id, 'revision': 1,
            'database_id': self.rows.database, 'instance_id': self.rows.instance,
            'config_snapshot_id': self.rows.snapshot, 'created_at_us': now, 'updated_at_us': now}

    def ids(self, key: str) -> tuple[str, str]:
        return (identity('configuration-activation', self.rows.database, self.rows.instance, key),
            identity('configuration-version', self.rows.database, self.rows.instance, key))

    def entry_id(self, version: str, domain: str, key: str) -> str:
        return identity('configuration-entry', self.rows.database, self.rows.instance, version, domain, key)

    def apply(self, kind: str, uow: UnitOfWork, values: Record):
        if self.binding is None or self.binding._closed:
            raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
        self.runtime.check(uow)
        aid, vid = cast(str, values['activation_id']), cast(str, values['version_id'])
        old = self.rows.get('managed_activations', uow, aid)
        active = self.rows.get('managed_active', uow, 'active-configuration')
        revision = cast(int, active['revision']) if active is not None else 0
        if active is not None and kind in ('save_configuration_candidate', 'prepare_configuration_activation', 'decide_configuration_activation'):
            previous_activation = self.rows.get('managed_activations', uow, cast(str, active['activation_id']))
            if previous_activation is None or previous_activation['state'] != 'APPLIED':
                raise OwnerFailure('RESOURCE_BUSY', 'configuration', 'ACTIVATION_PENDING')
        facts = {}
        targets = []
        changed = 0
        if kind == 'save_configuration_candidate':
            if old is not None or values['expected_revision'] != revision:
                raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'REVISION_CONFLICT')
            contents = self.thaw_contents(cast(Record, values['contents']))
            candidate = restore_candidate(contents, self.binding._bootstrap)
            if managed_snapshot_issue(candidate) is not None:
                raise OwnerFailure('INVALID_INPUT', 'configuration', 'CONFIGURATION_INVALID')
            rollback = values['rollback_of']
            if rollback is not None:
                previous = self.rows.get('managed_versions', uow, cast(str, rollback))
                original_birth = rollback == self.binding._birth_snapshot and contents == candidate_values(self.binding._bootstrap)
                if not original_birth and (previous is None or previous['content_digest'] != sha256(dump(contents).encode()).hexdigest()):
                    raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'ROLLBACK_CONTENT_CHANGED')
            row = self.base(vid) | {'parent_revision': revision, 'content_digest': sha256(dump(contents).encode()).hexdigest(),
                'domain_ids': tuple(d['domain_id'] for d in contents['domains']),
                'entry_count': sum(len(d['entries']) for d in contents['domains']), 'actor': values['actor'],
                'reason': values['reason'], 'rollback_of': rollback, 'catalog': contents['catalog']}
            self.rows.write('managed_versions', uow, row)
            targets.append(target(vid, 1))
            changed += 1
            for domain in contents['domains']:
                for entry in domain['entries']:
                    self.rows.write('managed_entries', uow, self.base(self.entry_id(vid, domain['domain_id'], entry['parameter_key'])) |
                        {'version_id': vid, 'domain_id': domain['domain_id'], 'key': entry['parameter_key'], 'body': entry['body']})
                    changed += 1
            next_state = 'CANDIDATE'
            activation = self.base(aid) | {'version_id': vid, 'previous_version_id': active['version_id'] if active else None,
                'expected_revision': revision, 'state': next_state, 'plan_digest': values['plan_digest'], 'decision_key': aid}
        else:
            if old is None or old['version_id'] != vid or old['expected_revision'] != values['expected_revision']:
                raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'ACTIVATION_CHANGED')
            if kind in ('prepare_configuration_activation', 'fail_configuration_preparation', 'decide_configuration_activation'):
                if old['plan_digest'] != values['plan_digest'] or revision != values['expected_revision']:
                    raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'REVISION_CONFLICT')
            next_state = cast(str, old['state'])
            if kind == 'prepare_configuration_activation':
                if old['state'] != 'CANDIDATE' or self.prepared != (aid, cast(str, values['plan_digest'])):
                    raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'RESOURCES_NOT_PREPARED')
                next_state = 'PREPARED'
            elif kind == 'fail_configuration_preparation':
                if old['state'] not in ('CANDIDATE', 'PREPARED'):
                    raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'ALREADY_DECIDED')
                next_state = 'PREPARATION_FAILED'
            elif kind == 'decide_configuration_activation':
                if old['state'] != 'PREPARED' or self.prepared != (aid, cast(str, values['plan_digest'])):
                    raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'RESOURCES_NOT_PREPARED')
                next_state = 'DECIDED'
                updated = self.base('active-configuration', active) | {'version_id': vid, 'activation_id': aid}
                self.rows.write('managed_active', uow, updated, revision if active else None)
                targets.append(target('active-configuration', revision + 1, revision if active else None))
                changed += 1
                facts['runtime'] = self.runtime.decide(uow, aid, vid)
            elif kind == 'acknowledge_configuration_consumer':
                consumer = cast(str, values['consumer'])
                if old['state'] != 'DECIDED' or active is None or active['activation_id'] != aid:
                    raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'ACTIVATION_CHANGED')
                if values['failure'] is None and self.published != (aid, consumer):
                    raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'CONSUMER_NOT_PUBLISHED')
                facts['runtime'] = self.runtime.acknowledge(uow, aid, consumer, failure=cast(str | None, values['failure']))
            elif kind == 'finish_configuration_activation':
                if old['state'] != 'DECIDED' or active is None or active['activation_id'] != aid or not self.runtime.all_bound(uow, aid):
                    raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'CONSUMERS_PENDING')
                next_state = 'APPLIED'
            else:
                raise OwnerFailure('INVALID_INPUT', 'configuration', 'UNSUPPORTED_OPERATION')
            activation = self.base(aid, old) | {'state': next_state}
        expected = cast(int, old['revision']) if old else None
        saved = self.rows.write('managed_activations', uow, activation, expected)
        targets.append(target(aid, cast(int, saved['revision']), expected))
        facts['configuration'] = {'rows_changed': changed + 1, 'targets': tuple(targets)}
        return {'activation_id': aid, 'version_id': vid, 'state': next_state, 'facts': facts}

    @staticmethod
    def thaw_contents(value: Record) -> InitializationValues:
        return {'catalog': cast(str, value['catalog']), 'domains': [
            {'domain_id': cast(str, d['domain_id']), 'digest': cast(str, d['digest']),
                'entries': [{'parameter_key': cast(str, e['parameter_key']), 'body': cast(str, e['body'])}
                    for e in cast(tuple[Record, ...], d['entries'])]} for d in cast(tuple[Record, ...], value['domains'])]}

    async def execute(self, kind: str, key: str, values: dict, actor: str):
        if self.binding is None:
            raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
        definition = next(d for d in self.commands if d.operation_kind == kind)
        operation = self.binding._storage.bind_operation(definition, self.rows.instance)
        return await operation.execute(key, ResultBoundCommand(1, values,
            {r.event_slot: {'actor': actor} for r in definition.required_audits}))

    async def save(self, key: str, candidate: ManagedConfigurationCandidate, expected_revision: int,
                   plan_digest: str, *, actor: str, reason: str, rollback_of: str | None = None):
        aid, vid = self.ids(key)
        return await self.execute('save_configuration_candidate', key,
            {'activation_id': aid, 'version_id': vid, 'expected_revision': expected_revision,
                'contents': candidate_values(candidate), 'plan_digest': plan_digest, 'actor': actor,
                'reason': reason, 'rollback_of': rollback_of}, actor)

    async def load(self, version_id: str) -> ManagedConfigurationCandidate:
        if self.binding is None:
            raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
        if version_id == self.binding._birth_snapshot:
            return self.binding._bootstrap
        version = await self.rows.read('managed_versions', version_id)
        if version is None:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'VERSION_MISSING')
        # The complete declared key set is fixed by this managed format. Point
        # loads prevent an unbounded scan of every retained historical version.
        domains = []
        from .communication_configuration import extend_candidate
        import json
        layout = extend_candidate(self.binding._bootstrap) if json.loads(cast(str, version['catalog'])).get('version') == 9 else self.binding._bootstrap
        for domain in candidate_values(layout)['domains']:
            entries = []
            for entry in domain['entries']:
                row = await self.rows.read('managed_entries', self.entry_id(version_id, domain['domain_id'], entry['parameter_key']))
                if row is None or row['version_id'] != version_id or row['domain_id'] != domain['domain_id'] or row['key'] != entry['parameter_key']:
                    raise OwnerFailure('STORAGE_FAILED', 'configuration', 'VERSION_INCOMPLETE')
                entries.append({'parameter_key': entry['parameter_key'], 'body': cast(str, row['body'])})
            domains.append({'domain_id': domain['domain_id'], 'digest': entries_digest(tuple((e['parameter_key'], e['body']) for e in entries)), 'entries': entries})
        contents: InitializationValues = {'catalog': cast(str, version['catalog']), 'domains': domains}
        if (sha256(dump(contents).encode()).hexdigest() != version['content_digest']
                or tuple(d['domain_id'] for d in domains) != version['domain_ids']
                or sum(len(d['entries']) for d in domains) != version['entry_count']):
            raise OwnerFailure('STORAGE_FAILED', 'configuration', 'CONTENT_MISMATCH')
        return restore_candidate(contents, self.binding._bootstrap)
