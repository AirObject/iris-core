"""Real SQLite version decisions, complete reconstruction and retained original keys.

These exercise the configuration/runtime persistence participants. Test-issued
preparation acknowledgements are not evidence of live consumer publication.
"""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.configuration.managed_codec import candidate_values
from companion_memory.configuration.managed_persistence import ManagedConfigurationAssembly, StoredManagedConfiguration
from companion_memory.persistence import Ready, Committed
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_business import ManagedBusiness
from .test_business import setup_draft, synthetic_resources


class ConfigurationVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_consumer_failure_keeps_decision_and_preparation_failure_keeps_old_version(self):
        from unittest.mock import patch as inject
        from companion_memory.persistence.owned_statements import OwnerFailure
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                await identity.save_draft('draft', None, setup_draft(root))
                await business.initialize('initialize', 1)
                manager, host = business.configuration, business.host
                assert manager is not None and host is not None and host.runtime is not None
                change: dict[str, object] = {'platform': {'platforms.sample_platform.buffer.target_count': 1}}
                _, plan = await manager.preview(0, change)
                saved = await manager.save('partial-consumers', 0, change, plan['plan_digest'], actor='test-operator', reason='合成消费者故障注入')
                self.assertIs(type(saved), Committed, saved)
                aid, vid = manager.versions.ids('partial-consumers')
                with inject.object(manager.coordinator.consumers['retrieval'], 'publish', side_effect=OSError('synthetic publication interruption')):
                    interrupted = await manager.activate(aid)
                self.assertEqual(interrupted['state'], 'RECOVERING', interrupted)
                status = await manager.status()
                self.assertEqual(status['authoritative_version'], vid)
                self.assertEqual(status['published_version'], manager.work.versions.birth.version_id)
                self.assertTrue(status['admission_closed'])
                self.assertEqual(sum(row['state'] == 'BOUND' for row in status['consumers']), 3)
                with self.assertRaises(OwnerFailure):
                    await manager.preview(1, change)
                continued = await manager.activate(aid)
                self.assertEqual(continued['state'], 'APPLIED', continued)
                self.assertEqual((await manager.status())['published_version'], vid)
                reverse: dict[str, object] = {'platform': {'platforms.sample_platform.buffer.target_count': 2}}
                _, second_plan = await manager.preview(1, reverse)
                prepared = await manager.save('failed-prepare', 1, reverse, second_plan['plan_digest'], actor='test-operator', reason='合成资源准备故障注入')
                self.assertIs(type(prepared), Committed, prepared)
                failed_id, _ = manager.versions.ids('failed-prepare')
                with inject.object(manager.coordinator.consumers['media'], 'prepare', side_effect=OSError('synthetic resource preparation failure')):
                    failed = await manager.activate(failed_id)
                self.assertEqual(failed['state'], 'PREPARATION_FAILED', failed)
                self.assertFalse(failed['cleanup_pending'])
                status = await manager.status()
                self.assertEqual(status['revision'], 1)
                self.assertEqual(status['published_version'], vid)
                self.assertFalse(status['admission_closed'])
                with self.assertRaises(OwnerFailure):
                    await manager.preview(1, {'platform': {'unregistered': 1}})
                focus = await business.persona('prepare', {'key': 'focus', 'self_revision': 1, 'epoch': host.runtime.gate.epoch})
                self.assertIs(type(focus), Committed, focus)
                with self.assertRaises(OwnerFailure):
                    await manager.preview(1, reverse)
                self.assertFalse(business.sends_enabled)
            finally:
                if business is not None:
                    self.assertTrue(await business.close())
                self.assertTrue(await bootstrap.close())

    async def test_dream_versions_retain_policy_and_new_run_uses_active_policy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                draft = setup_draft(root)
                await identity.save_draft('draft', None, draft)
                await business.initialize('initialize', 1)
                host, manager = business.host, business.configuration
                assert host is not None and manager is not None and host.runtime is not None
                control = host.combination.dream
                assert control is not None
                started = await control.execute('start_background_dream', 'old-start', {
                    'run_id': 'old-dream', 'expected_revision': 1, 'mode_epoch': host.runtime.gate.epoch,
                    'trigger': 'MANUAL', 'local_date': None}, actor='test-operator')
                self.assertIs(type(started), Committed, started)
                policy = dict(draft['configuration']['text']['self_model.initial_persona'])
                policy['supervision_prompt'] = '合成新监管规则，下一新任务使用。'
                maintenance = dict(draft['configuration']['text']['memory.long_term_maintenance'])
                maintenance['decay_enabled'] = False
                patch: dict[str, object] = {'text': {'self_model.initial_persona': policy,
                    'memory.long_term_maintenance': maintenance}}
                _, plan = await manager.preview(0, patch)
                saved = await manager.save('policy-change', 0, patch, plan['plan_digest'], actor='test-operator', reason='合成 run 版本隔离验证')
                self.assertIs(type(saved), Committed, saved)
                aid, vid = manager.versions.ids('policy-change')
                self.assertEqual((await manager.activate(aid))['state'], 'APPLIED')
                old = await manager.work.load('DREAM', 'old-dream')
                self.assertEqual(old.version_id, manager.work.versions.birth.version_id)
                memory = host.assembly.memory.long_term
                assert memory is not None
                with manager.work.versions.use(old):
                    self.assertTrue(memory.settings['decay_enabled'])
                    self.assertNotEqual(control.execution_configuration().text.record('self_model.initial_persona')['supervision_prompt'], policy['supervision_prompt'])
                self.assertFalse(memory.settings['decay_enabled'])
                ended = await control.execute('abort_background_dream', 'old-abort', {
                    'run_id': 'old-dream', 'expected_revision': 1, 'mode_epoch': host.runtime.gate.epoch}, actor='test-operator')
                self.assertIs(type(ended), Committed, ended)
                schedule = await control.schedule()
                assert schedule is not None
                started = await control.execute('start_background_dream', 'new-start', {
                    'run_id': 'new-dream', 'expected_revision': schedule['revision'], 'mode_epoch': host.runtime.gate.epoch,
                    'trigger': 'MANUAL', 'local_date': None}, actor='test-operator')
                self.assertIs(type(started), Committed, started)
                self.assertEqual((await manager.work.load('DREAM', 'new-dream')).version_id, vid)
                self.assertFalse(business.sends_enabled)
            finally:
                if business is not None:
                    self.assertTrue(await business.close())
                self.assertTrue(await bootstrap.close())

    async def test_complete_versions_decision_and_reopen_preserve_birth_and_original_receipt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            original_receipt = None
            birth_id = None
            draft = setup_draft(root)
            for attempt in range(2):
                bootstrap = ManagedBootstrap(settings)
                business = None
                try:
                    self.assertIs(type(await bootstrap.open()), Ready)
                    identity = bootstrap.assembly.identity
                    assert identity is not None
                    business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                    if attempt == 0:
                        self.assertIs(type(await identity.save_draft('draft', None, draft)), Committed)
                        await business.initialize('initialize', 1)
                    else:
                        await business.recover()
                    host = business.host
                    assert host is not None and type(host.stored) is StoredManagedConfiguration
                    assembly = host.combination.configuration
                    assert type(assembly) is ManagedConfigurationAssembly and assembly.versions is not None
                    versions = assembly.versions
                    edited = setup_draft(root)
                    edited['configuration']['text']['dream.schedule']['local_time'] = '07:35'
                    candidate = business.candidate(edited)
                    plan = sha256(b'synthetic finite preparation plan').hexdigest()
                    saved = await versions.save('schedule-change', candidate, 0, plan, actor='test-operator', reason='合成配置版本持久化验证')
                    self.assertIs(type(saved), Committed, saved)
                    assert type(saved) is Committed
                    aid, vid = versions.ids('schedule-change')
                    if attempt == 0:
                        original_receipt = saved.receipt
                        birth_id = host.stored.snapshot_id
                        self.assertIsNone(await versions.rows.read('managed_active', 'active-configuration'))
                        values = {'activation_id': aid, 'version_id': vid, 'expected_revision': 0, 'plan_digest': plan}
                        refused = await versions.execute('decide_configuration_activation', 'early-decision', values, 'test-operator')
                        self.assertIsNot(type(refused), Committed)
                        versions.prepared = (aid, plan)
                        self.assertIs(type(await versions.execute('prepare_configuration_activation', 'prepare', values, 'test-operator')), Committed)
                        decided = await versions.execute('decide_configuration_activation', 'decision', values, 'test-operator')
                        self.assertIs(type(decided), Committed, decided)
                        consumers = await versions.runtime.status(aid)
                        self.assertEqual(len(consumers), 6)
                        self.assertTrue(all(row['state'] == 'PENDING' for row in consumers))
                        premature = await versions.execute('finish_configuration_activation', 'premature',
                            {key: value for key, value in values.items() if key != 'plan_digest'}, 'test-operator')
                        self.assertIsNot(type(premature), Committed)
                    else:
                        self.assertEqual(saved.receipt, original_receipt)
                        self.assertEqual(host.stored.snapshot_id, birth_id)
                        self.assertFalse(business.sends_enabled)
                        active = await versions.rows.read('managed_active', 'active-configuration')
                        assert active is not None
                        self.assertEqual(active['version_id'], vid)
                        activation = await versions.rows.read('managed_activations', aid)
                        assert activation is not None
                        self.assertEqual(activation['state'], 'APPLIED')
                        self.assertEqual(host.execution_configuration.text.record('dream.schedule')['local_time'], '07:35')
                    restored = await versions.load(vid)
                    self.assertEqual(candidate_values(restored), candidate_values(candidate))
                    self.assertNotEqual(candidate_values(restored), candidate_values(host.stored.candidate))
                finally:
                    if business is not None:
                        self.assertTrue(await business.close())
                    self.assertTrue(await bootstrap.close())

    async def test_native_activation_keeps_selected_batch_window_and_new_selection_uses_new_version(self):
        from tests.runtime.configuration_support import event
        from companion_memory.memory.formats import record, sequence
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                await identity.save_draft('draft', None, setup_draft(root))
                await business.initialize('initialize', 1)
                host = business.host
                manager = business.configuration
                assert host is not None and host.runtime is not None and manager is not None
                self.assertIs(type(await host.register_entry('entry-two', 'second-entry', 'host', 'sample_platform', 'second-conversation')), Committed)
                for entry in ('entry', 'second-entry'):
                    # This owner-level fixture registers both synthetic entries;
                    # host HTTP token admission is covered separately.
                    port = host.runtime.bind_entry(entry)
                    for n in range(3):
                        supplied = event(entry + '-event-' + str(n), '合成窗口验证。')
                        supplied['event_version'] = 2
                        self.assertIs(type(await port.accept_event(entry + '-event-' + str(n), supplied)), Committed)
                runtime = host.runtime
                original = await runtime.execute('select_content_preparation', 'select-old',
                    {'entry_id': 'entry', 'preparation_id': 'old-preparation', 'batch_id': 'old-batch', 'run_id': 'old-run'})
                self.assertIs(type(original), Committed, original)
                patch: dict[str, object] = {'platform': {'platforms.sample_platform.buffer.target_count': 1}}
                candidate, plan = await manager.preview(0, patch)
                saved = await manager.save('change-window', 0, patch, plan['plan_digest'], actor='test-operator', reason='合成冻结窗口验证')
                self.assertIs(type(saved), Committed, saved)
                aid, vid = manager.versions.ids('change-window')
                applied = await manager.activate(aid)
                self.assertEqual(applied['state'], 'APPLIED', applied)
                repeated = await manager.save('change-window', 0, patch, plan['plan_digest'], actor='test-operator', reason='合成冻结窗口验证')
                self.assertIs(type(repeated), Committed, repeated)
                assert type(repeated) is Committed and type(saved) is Committed
                self.assertEqual(repeated.receipt, saved.receipt)
                self.assertEqual(host.execution_configuration.platform('sample_platform').count('target_count'), 1)
                for entry, prefix in (('entry', 'old'), ('second-entry', 'new')):
                    if prefix == 'new':
                        selected = await runtime.execute('select_content_preparation', 'select-new',
                            {'entry_id': entry, 'preparation_id': 'new-preparation', 'batch_id': 'new-batch', 'run_id': 'new-run'})
                        self.assertIs(type(selected), Committed, selected)
                    for kind, revision in (('claim_content_preparation', 1), ('complete_content_preparation', 2), ('freeze_content_batch', 3)):
                        result = await runtime.execute(kind, prefix + '-' + kind,
                            {'preparation_id': prefix + '-preparation', 'expected_revision': revision, 'owner_generation': 1})
                        self.assertIs(type(result), Committed, result)
                    frozen = await host.assembly.read_daily_batch(prefix + '-batch')
                    source = record(frozen['source'])
                    self.assertEqual(sum(record(member)['role'] == 'T' for member in sequence(source['ordered_members'])), 2 if prefix == 'old' else 1)
                    work_version = await manager.work.load('BATCH', prefix + '-batch')
                    self.assertEqual(work_version.version_id, manager.work.versions.birth.version_id if prefix == 'old' else vid)
                self.assertFalse(business.sends_enabled)
                _, rollback_plan = await manager.preview_rollback(1, manager.work.versions.birth.version_id)
                rolled = await manager.save_rollback('restore-window', 1, manager.work.versions.birth.version_id,
                    rollback_plan['plan_digest'], actor='test-operator', reason='合成回退保留业务记录验证')
                self.assertIs(type(rolled), Committed, rolled)
                rollback_id, rollback_version = manager.versions.ids('restore-window')
                rollback_result = await manager.activate(rollback_id)
                self.assertEqual(rollback_result['state'], 'APPLIED', rollback_result)
                self.assertEqual(host.execution_configuration.platform('sample_platform').count('target_count'), 2)
                self.assertEqual((await manager.work.load('BATCH', 'new-batch')).version_id, vid)
                self.assertNotEqual(rollback_version, manager.work.versions.birth.version_id)
                self.assertEqual((await manager.status())['revision'], 2)
            finally:
                if business is not None:
                    self.assertTrue(await business.close())
                self.assertTrue(await bootstrap.close())
