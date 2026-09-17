"""Two frozen batches retain distinct profiles and secret revisions across activation.

All requests terminate at the explicit synthetic HTTP adapter. A rollback and
reopen retain both native Provider ledgers and the post-switch formal objects.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.memory.formats import record
from companion_memory.persistence import Ready, Committed
from companion_memory.provider.managed_versions import profiles
from companion_memory.provider.values import as_record
from companion_memory.runtime.content_learning import learn_batch
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_business import ManagedBusiness
from tests.daily_cognition.test_initial_persona_host import persona
from tests.daily_cognition.test_reasoning import responses
from tests.runtime.configuration_support import event
from .memory_support import formal_response
from .semantic_support import publish_fixture_persona
from .test_business import setup_draft, controlled_resources


class ProviderVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_batch_and_new_batch_keep_profiles_secrets_and_original_usage_after_rollback(self):
        with TemporaryDirectory() as directory, responses((persona, formal_response, formal_response)) as (port, requests, failures):
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': directory})
            resolutions: list[tuple[str, str, str]] = []
            original_requests = {}
            expected_objects = 0
            for reopen in (False, True):
                bootstrap = ManagedBootstrap(settings)
                business = None
                try:
                    self.assertIs(type(await bootstrap.open()), Ready)
                    owner = bootstrap.assembly.identity
                    assert owner is not None
                    business = ManagedBusiness(bootstrap, owner, resource_factory=controlled_resources(port, resolutions))
                    if reopen:
                        self.assertEqual((await business.recover())['state'], 'READY')
                    else:
                        await owner.save_draft('draft', None, setup_draft(root))
                        await business.initialize('initialize', 1)
                        await publish_fixture_persona(self, business)
                    host, manager = business.host, business.configuration
                    assert host is not None and manager is not None and host.runtime is not None and host.provider is not None
                    memory = host.assembly.memory.information
                    assert memory is not None and host.network is not None
                    if reopen:
                        self.assertFalse(business.sends_enabled)
                        self.assertEqual(len(requests), 3)
                        self.assertEqual(len(await memory.current_page('', 4)), expected_objects)
                        for request_id, expected in original_requests.items():
                            self.assertEqual(await host.provider.ledger.get('requests', request_id), expected)
                        continue
                    await host.network.wait_quiet(time.monotonic() + 60)
                    runtime = host.runtime
                    for entry in ('entry',):
                        native = runtime.bind_entry(entry)
                        for index in range(3):
                            supplied = event(entry + '-input-' + str(index), '合成测试的杯子在桌面。')
                            supplied['event_version'] = 2
                            self.assertIs(type(await native.accept_event(entry + '-input-' + str(index), supplied)), Committed)
                    async def select(entry, name):
                        selected = await runtime.execute('select_content_preparation', name + '-select',
                            {'entry_id': entry, 'preparation_id': name + '-preparation', 'batch_id': name + '-batch', 'run_id': name + '-run'})
                        self.assertIs(type(selected), Committed, selected)
                    await select('entry', 'old')
                    birth = manager.work.versions.birth
                    old_profile = next(profile for profile in profiles(birth.candidate) if profile['material_role'] == 'LEARNING')
                    changed_profiles = [dict(profile) | ({'max_input_units': cast(int, profile['max_input_units']) + 1024}
                        if profile['material_role'] == 'LEARNING' else {}) for profile in profiles(birth.candidate)]
                    wire = birth.candidate.text.record('provider.transport')
                    changed_wire = dict(wire) | {'roles': [dict(role) | ({'secret_revision': 'synthetic-rotated-v2'}
                        if role['role'] == 'LEARNING' else {}) for role in cast(tuple[dict, ...], wire['roles'])]}
                    patch: dict[str, object] = {'foundation': {'provider.profiles': changed_profiles}, 'text': {'provider.transport': changed_wire}}
                    _, plan = await manager.preview(0, patch)
                    saved = await manager.save('provider-change', 0, patch, plan['plan_digest'], actor='test-operator', reason='合成配置切换')
                    self.assertIs(type(saved), Committed, saved)
                    aid, vid = manager.versions.ids('provider-change')
                    self.assertEqual((await manager.activate(aid))['state'], 'APPLIED')
                    await host.resume_learning('learning-enabled')
                    for name in ('old', 'new'):
                        if name == 'new':
                            native = runtime.bind_entry('entry')
                            for index in range(2):
                                supplied = event('new-input-' + str(index), '合成测试的杯子在桌面。')
                                supplied['event_version'] = 2
                                self.assertIs(type(await native.accept_event('new-input-' + str(index), supplied)), Committed)
                            await select('entry', 'new')
                        for kind, revision in (('claim_content_preparation', 1), ('complete_content_preparation', 2), ('freeze_content_batch', 3)):
                            changed = await runtime.execute(kind, name + '-' + kind,
                                {'preparation_id': name + '-preparation', 'expected_revision': revision, 'owner_generation': 1})
                            self.assertIs(type(changed), Committed, changed)
                        frozen = await host.assembly.read_daily_batch(name + '-batch')
                        await host.network.wait_quiet(time.monotonic() + 60)
                        learned = await learn_batch(runtime, record(frozen['source']), fresh=True)
                        self.assertIs(type(learned), Committed, learned)
                        roots = await host.provider.ledger.read('requests_page', {'after': '', 'limit': 8})
                        request = next(item for item in roots if as_record(item['attribution'])['run_id'] == name + '-run')
                        selected_profile = as_record(as_record(request['execution_evidence'])['profile'])
                        self.assertEqual(selected_profile['max_input_units'], cast(int, old_profile['max_input_units']) + (1024 if name == 'new' else 0))
                        self.assertEqual(request['profile_revision'], birth.version_id if name == 'old' else vid)
                        self.assertEqual(resolutions[-1][1] == 'synthetic-rotated-v2', name == 'new')
                        original_requests[request['object_id']] = request
                    expected_objects = len(await memory.current_page('', 4))
                    self.assertEqual(expected_objects, 2)
                    _, rollback = await manager.preview_rollback(1, birth.version_id)
                    self.assertIs(type(await manager.save_rollback('rollback', 1, birth.version_id, rollback['plan_digest'],
                        actor='test-operator', reason='合成回退')), Committed)
                    self.assertEqual((await manager.activate(manager.versions.ids('rollback')[0]))['state'], 'APPLIED')
                    self.assertEqual(len(requests), 3)
                    self.assertFalse(failures)
                finally:
                    if bootstrap.assembly.identity is not None:
                        bootstrap.assembly.identity.close()
                    if business is not None:
                        self.assertTrue(await business.close())
                    self.assertTrue(await bootstrap.close())
