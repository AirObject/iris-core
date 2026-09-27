"""Actual bootstrap-to-host attachment with synthetic material and no model sends."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
import time
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import CredentialResolver, CredentialLease, Available
from companion_memory.provider.values import Record
from companion_memory.memory.formats import record
from companion_memory.persistence import Found
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .memory_support import shared_formal_response, exercise_memory

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.configuration.managed_bootstrap import STORAGE_DEFAULTS
from companion_memory.persistence import Ready, Committed
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_business import ManagedBusiness
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.runtime.managed_host_resources import host_resources
from .test_native_host import managed_inputs


def setup_draft(root: Path) -> dict[str, Any]:
    # Legacy value fixtures create their own historical directory layout. Keep
    # those disposable fixture directories outside the managed product volume.
    with TemporaryDirectory() as fixture_directory:
        raw = managed_inputs(Path(fixture_directory))
    values = {name: raw[index]['explicit_values'] for name, index in
        (('foundation', 0), ('runtime', 1), ('content', 3), ('information', 4), ('text', 5))}
    values['platform'] = raw[2][0]['explicit_values']
    values['foundation'].update(STORAGE_DEFAULTS)
    values['foundation'].update({'storage.database_file': str(root / 'db/memory.sqlite3'),
        'logging.file_directory': str(root / 'logs/runtime')})
    values['content'].update({'media.root_directory': str(root / 'blobs'), 'media.staging_directory': str(root / 'upload_staging')})
    values['text']['retrieval.semantic_storage']['index_root'] = str(root / 'indexes')
    return {'role_name': '合成工程角色', 'initial_material': '这是合成测试参与者提供的初始材料。',
        'platform_id': 'sample_platform', 'entry_id': 'entry', 'host_id': 'host', 'conversation_id': 'conversation',
        'timezone': 'Asia/Shanghai', 'timezone_confirmed': True, 'configuration': values}


def synthetic_resources(resources, candidate, key, name, authorize):
    original = host_resources(resources, candidate, key, name, authorize)
    return replace(original, initial_self=replace(original.initial_self, input_origin='SYNTHETIC_FIXTURE'))


def controlled_resources(port: int, resolutions: list[tuple[str, str, str]] | None = None):
    def create(resources, candidate, key, name, authorize):
        original = synthetic_resources(resources, candidate, key, name, authorize)
        def resolve(reference, revision, account):
            if resolutions is not None:
                resolutions.append((reference, revision, account))
            return Available(CredentialLease(b'synthetic-managed-only'))
        resolver = CredentialResolver(resolve)
        transports = {cast(str, setting['role']):
            (ChatTransport.controlled_dream_loopback if setting['role'] in
                ('DREAM_REVIEW', 'PERSONA_DREAM', 'PERSONA_REVIEW') else ChatTransport.controlled_daily_loopback)(
                    setting, resolver, time.monotonic, port)
            for setting in cast(tuple[Record, ...], candidate.text.record('provider.transport')['roles'])}
        return replace(original, transports=transports, version_transports=lambda candidate:
            create(resources, candidate, key, name, authorize).transports)
    return create


class BusinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_draft_receipts_remain_confirmable_after_material_is_fixed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                first = setup_draft(root)
                second = {**first, 'role_name': '已确认的合成角色'}
                self.assertIs(type(await identity.save_draft('first-draft', None, first)), Committed)
                self.assertIs(type(await identity.save_draft('second-draft', 1, second)), Committed)
                self.assertIs(type(await identity.advance_wizard('confirm-material', 2,
                    'VALIDATED', 'confirm-material')), Committed)
                before = await identity.read_draft()
                repeated = await identity.save_draft('first-draft', None, first)
                self.assertIs(type(repeated), Committed)
                assert type(repeated) is Committed
                self.assertEqual(repeated.source, 'EXISTING')
                for changed in ({**second, 'role_name': '另一个角色'},
                        {**second, 'initial_material': '不同的初始材料'}, {**second, 'timezone': 'UTC'}):
                    with self.assertRaises(OwnerFailure) as frozen:
                        await identity.save_draft('change-confirmed-material', 3, changed)
                    self.assertEqual(frozen.exception.reason, 'INITIALIZATION_STARTED')
                with self.assertRaises(OwnerFailure) as reused:
                    await identity.save_draft('first-draft', None, second)
                self.assertEqual(reused.exception.reason, 'CONTENT_MISMATCH')
                self.assertEqual(await identity.read_draft(), before)
            finally:
                if bootstrap.assembly.identity is not None:
                    bootstrap.assembly.identity.close()
                self.assertTrue(await bootstrap.close())

    async def test_basic_setup_waits_for_actual_provider_configuration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            bootstrap = ManagedBootstrap(settings)
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                draft = setup_draft(root)
                for name in ('entry_id', 'host_id', 'conversation_id'):
                    del draft[name]
                complete_configuration = draft['configuration']
                draft['configuration'] = {**complete_configuration,
                    'foundation': {**complete_configuration['foundation'], 'provider.profiles': None}}
                self.assertIs(type(await identity.save_draft('save-basic', None, draft)), Committed)
                business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                confirmed = await business.initialize('confirm-basic', 1)
                assert type(confirmed) is dict
                self.assertEqual(confirmed['state'], 'CONFIGURATION_REQUIRED')
                self.assertFalse(business.initialized)
                self.assertEqual((await identity.read_draft())['state'], 'VALIDATED')
                repeated = await business.initialize('confirm-basic', 1)
                assert type(repeated) is dict
                self.assertEqual(repeated['state'], 'CONFIGURATION_REQUIRED')
                with self.assertRaises(OwnerFailure) as reused:
                    await business.initialize('confirm-basic', 2)
                self.assertEqual(reused.exception.reason, 'CONTENT_MISMATCH')
                self.assertTrue(await business.close())
                self.assertTrue(await bootstrap.close())
                bootstrap = ManagedBootstrap(settings)
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                recovered = await business.recover()
                assert type(recovered) is dict
                self.assertEqual(recovered['state'], 'CONFIGURATION_REQUIRED')
                saved = await identity.save_draft('save-actual-configuration', 2,
                    {**draft, 'configuration': complete_configuration})
                self.assertIs(type(saved), Committed)
                started = await business.initialize('initialize-business', 3)
                assert type(started) is dict
                self.assertEqual(started['state'], 'AWAITING_REVIEW')
                assert business.host is not None
                self.assertEqual(business.host.configured_entries(), ())
                frozen = await identity.read_draft()
                repeated = await business.initialize('confirm-basic', 1)
                assert type(repeated) is dict
                self.assertEqual(repeated['state'], 'CONFIGURATION_REQUIRED')
                with self.assertRaises(OwnerFailure) as conflict:
                    await business.initialize('initialize-business', 2)
                self.assertEqual(conflict.exception.reason, 'CONTENT_MISMATCH')
                self.assertEqual(await identity.read_draft(), frozen)
                self.assertTrue(await business.close())
                identity.close()
                self.assertTrue(await bootstrap.close())
                bootstrap = ManagedBootstrap(settings)
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                recovered = await business.recover()
                assert type(recovered) is dict
                self.assertEqual(recovered['state'], 'AWAITING_REVIEW')
                repeated = await business.initialize('confirm-basic', 1)
                assert type(repeated) is dict
                self.assertEqual(repeated['state'], 'CONFIGURATION_REQUIRED')
                self.assertEqual(await identity.read_draft(), frozen)
            finally:
                if bootstrap.assembly.identity is not None:
                    bootstrap.assembly.identity.close()
                if business is not None:
                    self.assertTrue(await business.close())
                self.assertTrue(await bootstrap.close())

    async def test_setup_without_entry_then_register_external_connection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            draft = setup_draft(root)
            for name in ('entry_id', 'host_id', 'conversation_id'):
                del draft[name]
            bootstrap = ManagedBootstrap(settings)
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                application = ManagedApplication(bootstrap, resource_factory=synthetic_resources)
                business = application.business
                from companion_memory.management.connection_management import request_connections
                unavailable = await request_connections(application, 'overview', {}, None)
                assert type(unavailable) is dict
                self.assertEqual(unavailable['state'], 'BOOTSTRAP')
                self.assertFalse(unavailable['initialized'])
                self.assertTrue(unavailable['notifications_paused'])
                self.assertIs(type(await identity.save_draft('draft-unbound', None, draft)), Committed)
                result = await business.initialize('initialize-unbound', 1)
                assert type(result) is dict
                self.assertEqual(result['state'], 'AWAITING_REVIEW')
                host = business.host
                assert host is not None
                self.assertEqual(host.configured_entries(), ())
                self.assertEqual(await host.assembly.ingress.registered_entries(), ())
                with self.assertRaises(OwnerFailure) as missing:
                    await application.operations.selected_binding(host, draft, {})
                self.assertEqual(missing.exception.reason, 'BINDING_REQUIRED')
                registered = await request_connections(application,
                    'hosts/register', {'key': 'external-binding', 'entry_id': 'entry', 'host_id': 'host',
                        'external_entry_id': 'conversation'}, None)
                self.assertIs(type(registered), Committed)
                self.assertEqual(host.configured_entries(), ('entry',))
                self.assertEqual(await application.operations.selected_binding(host, draft, {}),
                    ('host', 'entry', {}))
                second = await request_connections(application,
                    'hosts/register', {'key': 'second-binding', 'entry_id': 'second', 'host_id': 'other-host',
                        'external_entry_id': 'other-conversation'}, None)
                self.assertIs(type(second), Committed)
                with self.assertRaises(OwnerFailure) as ambiguous:
                    await application.operations.selected_binding(host, draft, {})
                self.assertEqual(ambiguous.exception.reason, 'BINDING_REQUIRED')
                self.assertEqual(await application.operations.selected_binding(host, draft,
                    {'host_id': 'other-host', 'entry_id': 'second', 'input': {'cursor': None}}),
                    ('other-host', 'second', {'cursor': None}))
                with self.assertRaises(OwnerFailure) as mismatch:
                    await application.operations.selected_binding(host, draft,
                        {'host_id': 'host', 'entry_id': 'second', 'input': {}})
                self.assertEqual(mismatch.exception.reason, 'BINDING_MISMATCH')
            finally:
                if bootstrap.assembly.identity is not None:
                    bootstrap.assembly.identity.close()
                if business is not None:
                    self.assertTrue(await business.close())

    async def test_explicit_persona_review_publication_and_focused_reopen(self):
        with TemporaryDirectory() as directory, responses((persona, shared_formal_response)) as (port, requests, failures):
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            for attempt in range(4):
                bootstrap = ManagedBootstrap(settings)
                business = None
                try:
                    self.assertIs(type(await bootstrap.open()), Ready)
                    identity = bootstrap.assembly.identity
                    assert identity is not None
                    business = ManagedBusiness(bootstrap, identity, resource_factory=controlled_resources(port))
                    if attempt == 0:
                        self.assertIs(type(await identity.save_draft('draft', None, setup_draft(root))), Committed)
                        await business.initialize('initialize', 1)
                        host = business.host
                        assert host is not None and host.runtime is not None
                        prepared = await business.persona('prepare', {'key': 'prepare', 'self_revision': 1, 'epoch': host.runtime.gate.epoch})
                        self.assertIs(type(prepared), Committed, prepared)
                        self.assertFalse(requests)
                    else:
                        result = await business.recover()
                        self.assertEqual(result['state'], 'AWAITING_REVIEW' if attempt == 1 else 'READY', result)
                        self.assertFalse(business.sends_enabled)
                        if attempt == 1:
                            disclosure = business.dispatch_disclosure()
                            enabled = await business.set_dispatch('enable', None, True, str(disclosure['digest']))
                            self.assertIs(type(enabled), Committed, enabled)
                            pending = await business.persona('pending', {})
                            self.assertIs(type(pending), Found, pending)
                            assert type(pending) is Found
                            run = record(pending.value['run'])
                            await business.persona('generate', {'key': run['provider_operation_key'], 'generation': run['generation']})
                            assert business.host is not None
                            await business.host.combination.initial_persona.control.wait_actual()
                            pending = await business.persona('pending', {})
                            assert type(pending) is Found
                            run, candidate = record(pending.value['run']), record(pending.value['candidate'])
                            self.assertIsNotNone(candidate, pending)
                            reviewed = await business.persona('review', {'key': 'approve', 'run_revision': run['revision'],
                                'candidate_id': candidate['object_id'], 'candidate_revision': candidate['revision'],
                                'candidate_digest': pending.value['candidate_digest'], 'decision': 'APPROVE'})
                            self.assertIs(type(reviewed), Committed, reviewed)
                            pending = await business.persona('pending', {})
                            assert type(pending) is Found
                            run, candidate = record(pending.value['run']), record(pending.value['candidate'])
                            assert business.host.runtime is not None
                            published = await business.persona('publish', {'key': 'publish', 'run_revision': run['revision'],
                                'candidate_id': candidate['object_id'], 'candidate_revision': candidate['revision'],
                                'candidate_digest': pending.value['candidate_digest'], 'epoch': business.host.runtime.gate.epoch})
                            self.assertIs(type(published), Committed, published)
                            self.assertTrue(await business.business_ready())
                        self.assertEqual(len(requests), 1 if attempt < 3 else 2)
                        if attempt == 2:
                            from .http_support import exercise_host_http
                            await exercise_host_http(self, bootstrap, business, root)
                            await exercise_memory(self, business)
                            self.assertEqual(len(requests), 2)
                finally:
                    if bootstrap.assembly.identity is not None:
                        bootstrap.assembly.identity.close()
                    if business is not None:
                        self.assertTrue(await business.close())
                    self.assertTrue(await bootstrap.close())
            self.assertFalse(failures)

    async def test_bootstrap_attachment_and_recovery_keep_original_self(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            instance = None
            for create in (True, False):
                bootstrap = ManagedBootstrap(settings)
                business = None
                try:
                    self.assertIs(type(await bootstrap.open()), Ready)
                    identity = bootstrap.assembly.identity
                    resources = bootstrap.resources
                    assert identity is not None and resources is not None
                    if create:
                        instance = resources.instance_id
                        saved = await identity.save_draft('draft', None, setup_draft(root))
                        self.assertIs(type(saved), Committed, saved)
                    self.assertEqual(resources.instance_id, instance)
                    business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                    result = await business.initialize('initialize', 1) if create else await business.recover()
                    self.assertEqual(result, {'state': 'AWAITING_REVIEW', 'startup_sends': 0, 'business_ready': False})
                    host = business.host
                    assert host is not None and host.provider is not None
                    self.assertFalse(host.resources.send_authorized('anything'))
                    self.assertEqual((await identity.read_draft())['state'], 'AWAITING_REVIEW')
                finally:
                    if bootstrap.assembly.identity is not None:
                        bootstrap.assembly.identity.close()
                    if business is not None:
                        self.assertTrue(await business.close())
                    self.assertTrue(await bootstrap.close())


if __name__ == '__main__':
    unittest.main()
