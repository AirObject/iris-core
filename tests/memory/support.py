"""Explicit synthetic candidate input atop real owners, Provider and SQLite.

No business owner is substituted. The fixture retains configuration identity and
uses a Provider simulation for model execution, then builds an explicit proposal
from its confirmed original handoff and the actual frozen source manifest.
"""
from pathlib import Path
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.content_persistence import ContentConfigurationAssembly
from companion_memory.configuration.content_persistent_results import ConfigurationCommitted
from companion_memory.persistence import PersistenceService, DatabaseResources, Ready, ResultBoundCommand, Committed, Value
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.runtime.content_assembly import ContentAssembly
from companion_memory.memory.formats import record, sequence
from companion_memory.memory.sources import decode_source
from companion_memory.memory.service import MemoryService
from companion_memory.cognition.candidates import stable_identity, manifest_digest
from companion_memory.provider import LedgerAssembly, ProviderService, ProviderResources, SimulationAdapter
from tests.configuration.content_support import candidate
from tests.provider.support import Gate, success
from tests.persistence.support import Hooks
import hashlib


class Fixture:
    def __init__(self, root: Path, media=None, candidate_input=None, runtime_changes=None, foundation_changes=None, publication=None, content_changes=None, media_policy=None):
        self.root = root.resolve(); self.candidate, self.supplied = candidate(self.root)
        if runtime_changes or foundation_changes or content_changes:
            from companion_memory.configuration.content_resolution import resolve_content_configuration, ContentConfigurationOk
            self.supplied[1]['explicit_values'].update(runtime_changes or {})
            self.supplied[0]['explicit_values'].update(foundation_changes or {})
            self.supplied[3]['explicit_values'].update(content_changes or {})
            resolved = resolve_content_configuration(*self.supplied)
            assert type(resolved) is ContentConfigurationOk, resolved
            self.candidate = resolved.value
        self.now_us = 1000000
        self.config_assembly = ContentConfigurationAssembly(); self.assembly = ContentAssembly(media, media.repositories if media else (), publication=publication, utc_now_us=lambda: self.now_us)
        self.media = media; self.media_policy = media_policy
        self.candidate_input = candidate_input; self.runtime = None
        self.ledger = LedgerAssembly(); self.provider = ProviderService(self.ledger)
        self.storage = PersistenceService(self.config_assembly.repositories + self.assembly.repositories + self.ledger.repositories,
            self.config_assembly.commands + self.assembly.commands + self.ledger.commands + (media.commands if media else ()) + (publication.commands if publication else ()))
        self.hooks = Hooks(); self.gate = Gate(); self.adapter = SimulationAdapter((success(), success(), success()))
        self.config = None; self.memory = None
        self.path = self.root / 'database' / 'runtime.sqlite3'
        self.expected_id = 'content-database'

    async def initialize(self, mode='CREATE_NEW'):
        ready = await self.storage.initialize(self.candidate.foundation, DatabaseResources(self.expected_id,
            lambda identity, path: identity == self.expected_id and path == str(self.path), connect=self.hooks.connect), mode)
        assert type(ready) is Ready, ready
        self.config = self.config_assembly.bind(self.storage, 'instance')
        config = await self.config.persist_content_configuration('content-config', self.candidate, actor='bootstrap', protected_directories=self.supplied[4])
        assert type(config) is ConfigurationCommitted and config.configuration is not None, config
        if self.media:
            self.media.bind(self.storage, config.configuration, 'instance')
        self.assembly.bind(self.storage, config.configuration, 'instance')
        self.memory = MemoryService(self.assembly.memory, lambda: None if self.gate.mode == 'NORMAL' else 'DREAMING')
        gate = self.gate.binding
        if self.candidate_input is not None:
            from companion_memory.runtime.content_service import ContentRuntimeService
            from companion_memory.runtime.content_media import MediaPolicy
            self.runtime = ContentRuntimeService(self.assembly, self.provider, self.candidate_input, 'sample_learning',
                (self.media_policy or MediaPolicy('domain', 'sample_media', 'describe:1')) if self.media else None)
            gate = self.runtime.gate.binding
            self.memory = self.runtime.memory
        await self.provider.initialize(self.candidate.foundation, self.ledger.bind(self.storage, self.candidate.foundation), ProviderResources(gate, self.adapter))
        if self.media:
            from companion_memory.media.service import MediaResources
            ready = await self.media.initialize(MediaResources('media-root', lambda rid, db, path: (rid, db, path) == ('media-root', self.expected_id, str(self.root / 'media'))), mode)
            from companion_memory.persistence import Found
            assert type(ready) is Found, ready
        if mode == 'CREATE_NEW':
            await self.execute('initialize_content_runtime', 'initialize', {})
            await self.execute('register_content_entry', 'register', {'entry_id': 'entry', 'host_id': 'host', 'platform_id': 'sample_platform', 'external_entry_id': 'conversation'})
        if self.runtime: await self.runtime.initialize()
        return self

    async def execute(self, kind: str, key: str, values: dict[str, object]):
        if self.media:
            if kind == 'accept_media_event' and cast(dict, decode_content(cast(str, values['event']).encode(), 8192))['media']:
                kind += '_with_media'
            elif kind in ('select_content_preparation', 'freeze_content_batch'):
                kind += '_with_media'
            elif kind.startswith('commit_content_') or kind == 'store_content_candidate':
                batch = (await self.assembly.rows.read('batches_get', {'batch_id': values['batch_id']}))[0]
                source = decode_source(cast(str, batch['manifest']))
                if any(sequence(record(m)['media']) for m in sequence(source['ordered_members'])): kind += '_with_media'
        definition = next(d for d in self.assembly.commands if d.operation_kind == kind)
        command = ResultBoundCommand(1, {'operation_id': key, **values},
            {r.event_slot: {'actor': 'scheduler'} for r in definition.required_audits})
        result = await self.assembly.operations[kind].execute(key, command)
        assert type(result) is Committed, result
        return result

    async def freeze(self, batch='batch'):
        await self.execute('select_content_preparation', 'select:' + batch, {'entry_id': 'entry', 'preparation_id': 'prepare:' + batch, 'batch_id': batch, 'run_id': 'run:' + batch})
        await self.execute('claim_content_preparation', 'claim:' + batch, {'preparation_id': 'prepare:' + batch, 'expected_revision': 1, 'owner_generation': 1})
        from companion_memory.runtime.content_media import complete_preparation
        completed = await complete_preparation(self.assembly, self.execute, 'prepare:' + batch)
        assert type(completed) is Committed, completed
        await self.execute('freeze_content_batch', 'freeze:' + batch, {'preparation_id': 'prepare:' + batch, 'expected_revision': 3, 'owner_generation': 1})
        row = (await self.assembly.rows.read('batches_get', {'batch_id': batch}))[0]
        return decode_source(cast(str, row['manifest']))

    async def learn(self, source: MappingProxyType[str, Value], count: int):
        from companion_memory.buffers.content_material import SourceMember, build_content_material
        from companion_memory.provider import WorkGrant, CancellationSource, Completed
        import time
        batch = cast(str, source['batch_id'])
        await self.execute('associate_content_request', 'associate:' + batch, {'batch_id': batch, 'expected_revision': 1, 'generation': 1, 'provider_operation_key': 'model:' + batch, 'model_binding': '{"transform_version":"explicit_candidate:1"}'})
        members = []
        for item in sequence(source['ordered_members']):
            m = record(item); payload = (await self.assembly.ingress.rows.read('payload', {'message_id': m['message_id']}))[0]
            interpretations = []
            for selected in sequence(m['media']):
                assert self.media is not None
                row = (await self.media.rows.read('interpretations_get', {'interpretation_id': record(selected)['interpretation_id']}))[0]
                interpretations.append(cast(str, row['body']).encode())
            members.append(SourceMember(cast(str, m['role']), MappingProxyType({k: m[k] for k in ('message_id', 'entry_seq', 'received_at_us', 'transferred_at_us', 'payload_digest')} | {'interpretation_ids': tuple(record(v)['interpretation_id'] for v in sequence(m['media']))}), cast(str, payload['body']).encode(), tuple(interpretations)))
        messages = build_content_material(('instance', 'host', 'sample_platform', 'entry', batch, cast(str, source['run_id']), cast(str, source['config_snapshot_id'])), tuple(members), event_limit=2048, interpretation_limit=2048, material_limit=49152)
        grant = WorkGrant('cognition', 'instance', None, 'LEARNING', ('sample_learning',), ('GENERATION',), 'cognition', 'scheduler',
            (cast(str, source['run_id']),), ('entry',), (batch,), prompt_revisions=('target_source_records:1',))
        work = self.provider.bind_work(grant)
        result = await work.generate({'operation_key': 'model:' + batch, 'run_id': source['run_id'], 'profile_id': 'sample_learning',
            'deadline': time.monotonic() + 30, 'cancellation': CancellationSource().token, 'batch_id': batch, 'entry_ids': ['entry'],
            'prompt_revision': 'target_source_records:1', 'payload': {'messages': [dict(m) for m in messages], 'input_units_limit': 49152, 'output_units_limit': 2048}})
        assert type(result) is Completed and result.record['outcome'] == 'SUCCEEDED', result
        request = cast(str, result.record['object_id']); handoff = cast(str, result.record['handoff_id'])
        from companion_memory.provider import ResultGrant
        from companion_memory.provider.terminal_evidence import TerminalVerified
        terminal = await self.provider.bind_result_owner(ResultGrant('cognition', (request,))).verify_terminal(request)
        assert type(terminal) is TerminalVerified, terminal
        self.assembly.retain_learning_terminal(terminal.value)
        cid = stable_identity('candidate', self.expected_id, batch, handoff, 'explicit_candidate:1', 0, 'CANDIDATE')
        target = next(record(m) for m in sequence(source['ordered_members']) if record(m)['role'] == 'T')
        leaves = []
        for ordinal in range(count):
            oid = stable_identity('object', self.expected_id, batch, handoff, 'explicit_candidate:1', ordinal, 'MEMORY')
            raw = {'object_version': 1, 'object_id': oid, 'instance_id': 'instance', 'kind': 'MEMORY', 'revision': 1,
                'created_at_us': 1000000, 'modified_at_us': 1000000, 'lifecycle': 'ACTIVE', 'forgotten_since_us': None,
                'retention_policy_ref': 'acceptance_policy:1', 'content': {'category': 'FACT', 'body': 'Explicit synthetic result ' + str(ordinal),
                    'subject_ids': [], 'speaker_subject_id': None, 'stance': 'UNCERTAIN', 'world_scope': {'kind': 'REAL', 'context_id': None}, 'occurred_range': None, 'applicable_range': None},
                'scores': {'belief': 50, 'retention': self.candidate.content.integer('memory.initial_retention'), 'scale_id': 'acceptance_100_v1',
                    'belief_reason': 'Explicit undecided belief.', 'retention_reason': 'Configured initial retention.', 'score_basis': []},
                'origin': {'kind': 'DIRECT_LEARNING', 'candidate_id': cid, 'batch_id': batch, 'actor_ref': 'scheduler', 'model_origin': 'SIMULATED', 'candidate_origin': 'SYNTHETIC'}}
            links = {'sources': [{'object_id': oid, 'object_revision': 1, 'source_id': source['source_id'], 'link_role': 'DIRECT',
                'target_anchors': [{'message_id': target['message_id'], 'part': 'BODY', 'item_index': None, 'start_utf8': None, 'end_utf8': None, 'occurrence_id': None, 'interpretation_id': None}], 'auxiliary_refs': []}], 'bases': []}
            from companion_memory.memory.changes import isolate_change
            leaves.append(isolate_change({'change_version': 1, 'action': 'CREATE_MEMORY', 'target_id': oid, 'expected_revision': None, 'proposed_value': raw, 'links': links}, 8192))
        manifest: dict[str, Value] = {'candidate_version': 1, 'candidate_id': cid, 'batch_id': batch, 'run_id': source['run_id'],
            'work_generation': 1, 'config_snapshot_id': source['config_snapshot_id'], 'provider_request_id': request, 'handoff_ref': handoff,
            'transform_version': 'explicit_candidate:1', 'source_id': source['source_id'], 'manifest_digest': 'pending', 'terminal_proposal': 'SUCCEEDED',
            'ordered_change_refs': tuple(MappingProxyType({'ordinal': i, 'target_id': leaf['target_id'], 'action': leaf['action'], 'digest': hashlib.sha256(encode_content(leaf, 8192)).hexdigest()}) for i, leaf in enumerate(leaves)),
            'origin': MappingProxyType({'storage_execution': 'ACTUAL', 'model_adapter': 'SIMULATED', 'candidate_origin': 'SYNTHETIC', 'database_id': self.expected_id})}
        manifest['manifest_digest'] = manifest_digest(MappingProxyType(manifest))
        await self.execute('store_content_candidate', 'stage:' + batch, {'batch_id': batch, 'expected_revision': 2, 'generation': 1,
            'manifest': encode_content(MappingProxyType(manifest), 4096).decode(), 'leaves': [encode_content(leaf, 8192).decode() for leaf in leaves]})
        return await self.execute('commit_content_published' if leaves else 'commit_content_without_objects', 'finish:' + batch, {'batch_id': batch, 'candidate_id': cid,
            'expected_revision': 3, 'generation': 1, 'readable_objects': [], 'readable_subjects': []})

    async def close(self):
        if self.runtime: await self.runtime.close()
        await self.provider.close()
        if self.memory: self.memory.close()
        self.assembly.close()
        if self.config: self.config.close()
        if self.media: await self.media.close()
        await self.storage.close()
        if self.config: self.config.close()
        self.assembly.close()

    async def maintenance(self, key: str, change: MappingProxyType[str, Value], previous_plan: str | None = None):
        planned = await self.execute('plan_memory_change', 'plan:' + key + (':' + previous_plan[-8:] if previous_plan else ''),
            {'root_id': key, 'change': encode_content(change, 8192).decode(), 'authorized_sources': [], 'authorized_objects': [], 'authorized_subjects': [], 'previous_plan': previous_plan})
        plan_id = record(planned.receipt.result)['operation_id']
        plan = (await self.assembly.memory.rows.read('release_plans_get', {'plan_id': plan_id}))[0]
        return await self.execute(cast(str, plan['command_kind']), cast(str, plan['execution_key']), {'plan_id': plan_id})
