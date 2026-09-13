"""Persisted complete text configuration and one real loopback Provider ledger.

Only synthetic credentials are issued. Reopening reconstructs the same original
configuration and request key; bootstrap never opens a socket or resolves a key.
"""
from datetime import datetime, timezone
from pathlib import Path
import time
import uuid
from companion_memory.configuration.text_persistence import TextConfigurationAssembly
from companion_memory.configuration.text_persistent_results import ConfigurationCommitted
from companion_memory.persistence import PersistenceService, DatabaseResources, Ready as StorageReady
from companion_memory.provider import LedgerAssembly, ProviderService, CancellationSource, WorkGrant
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.chat_protocol import ChatBinding
from companion_memory.provider.credentials import CredentialLease, CredentialResolver, Available
from companion_memory.provider.generation_resources import RealGenerationResources, ChatGenerationAdapter
from companion_memory.provider.values import as_record, Record
from companion_memory.cognition.text_resources import output_schema
from tests.provider.support import Gate
from tests.persistence.support import Hooks
from tests.text_learning.configuration_support import candidate
from typing import cast


class Fixture:
    """One instance with explicit static assembly, binding and cleanup ownership."""
    def __init__(self, root: Path, port: int, grant: WorkGrant | None = None):
        self.root, self.port = root.resolve(), port
        self.candidate, self.inputs = candidate(self.root)
        self.configuration = TextConfigurationAssembly()
        self.assembly = LedgerAssembly(text_generation=True)
        self.storage = PersistenceService(self.configuration.repositories + self.assembly.repositories,
            self.configuration.commands + self.assembly.commands, assembly_format='MODEL_TEXT_LEARNING_V1')
        self.hooks = Hooks()
        self.storage_resources = DatabaseResources('text-provider-database',
            lambda identity, path: identity == 'text-provider-database' and path == str(self.root/'database'/'runtime.sqlite3'), connect=self.hooks.connect)
        self.configuration_binding = None
        self.stored = None
        self.service = ProviderService(self.assembly)
        self.gate = Gate()
        self.leases: list[CredentialLease] = []
        self.ended: list[str] = []
        def resolve(secret, revision, account):
            assert (secret, revision, account) == ('fixture_secret', 'fixture_secret_revision', 'fixture_account')
            lease = CredentialLease(b'independent-ledger-fixture')
            self.leases.append(lease)
            return Available(lease)
        self.resolver = CredentialResolver(resolve)
        transport = ChatTransport.controlled_loopback(as_record(cast(Record, self.candidate.text.record('provider.transport'))),
            self.resolver, time.monotonic, port)
        generation = self.candidate.text.record('provider.generation')
        bindings = []
        for role, name in (('LEARNING', 'text_learning'), ('PERSONA', 'initial_persona')):
            selected = generation if role == 'LEARNING' else self.candidate.text.record('self_model.initial_persona')
            bindings.append(ChatBinding(cast(str, generation['model_id']), cast(tuple[str, ...], generation['expected_reported_models']),
                cast(str | None, generation['resolved_model_id']), cast(str, selected['schema_ref']), cast(str, selected['schema_digest']), name, output_schema(role)))
        self.adapter = ChatGenerationAdapter(transport, *bindings)
        self.resources = RealGenerationResources(self.gate.binding, self.adapter, self.resolver, time.monotonic,
            lambda: datetime.now(timezone.utc), lambda: str(uuid.uuid4()), self.ended.append)
        self.grant = grant or WorkGrant('cognition', 'instance', None, 'LEARNING', ('fixture_generation',), ('GENERATION',),
            'cognition', 'scheduler', ('run',), prompt_revisions=('learning_prompt',))
        self.work = self.service.bind_work(self.grant)
        self.cancellation = CancellationSource()
        self.binding = None

    async def initialize(self, mode='CREATE_NEW'):
        result = await self.storage.initialize(self.candidate.foundation, self.storage_resources, mode)
        assert type(result) is StorageReady, result
        self.configuration_binding = self.configuration.bind(self.storage, 'instance', self.candidate)
        result = await self.configuration_binding.persist_text_learning_configuration('configuration', self.candidate,
            actor='bootstrap', protected_directories=self.inputs[6])
        assert type(result) is ConfigurationCommitted and result.configuration is not None, result
        self.stored = result.configuration
        self.binding = self.assembly.bind(self.storage, self.stored)
        return await self.service.initialize(self.stored, self.binding, self.resources)

    def request(self, key='original'):
        selected = self.candidate.text.record('provider.generation')
        return {'operation_key': key, 'run_id': 'run', 'profile_id': 'fixture_generation', 'prompt_revision': 'learning_prompt',
            'deadline': time.monotonic()+3, 'cancellation': self.cancellation.token,
            'payload': {'format_version': 2, 'messages': [{'role': 'SYSTEM', 'text': 'Use supplied facts.'}, {'role': 'USER', 'text': 'Independent fixture input.'}],
                'schema_ref': selected['schema_ref'], 'schema_digest': selected['schema_digest'], 'output_tokens': 2048,
                'reservation_input_bound': 128000, 'context_digest': 'c'*64}}

    async def close(self):
        await self.service.close()
        if self.configuration_binding is not None: self.configuration_binding.close()
        await self.storage.close()
        if self.configuration_binding is not None: self.configuration_binding.close()
