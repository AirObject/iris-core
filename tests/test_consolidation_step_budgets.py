"""CO-07/08 independent durable budgets and bounded pair planning; no model IO."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from iris import consolidation, db
from iris.api import create_app
from iris.consolidation import Consolidation, _PersonaGateway, consolidation_settings, run_settings
from iris.db import Store, dumps
from iris.maintenance import Maintenance
from iris.memory_ops import operation
from iris.models import ModelError
from iris.settings_api import ConsolidationSettings
from conftest import login_admin
from test_consolidation import Model, pair
from test_lifecycle import Clock


BUDGETS = ('max_calls', 'entry_profile_max_calls', 'person_summary_max_calls', 'persona_max_calls')
PURPOSES = ('consolidation_merge', 'consolidation_entry_profile_observe',
            'consolidation_person_summary_check', 'persona_check')
MIGRATION = '034_consolidation_step_budgets.sql'


def owner_for(store, **settings):
    store.set_setting('consolidation', {**dict.fromkeys(BUDGETS, 2), **settings})
    clock = Clock()
    maintenance = Maintenance(store, gateway=Model(store), clock=clock)
    rid = maintenance.request()
    owner = Consolidation(store, maintenance.gateway, clock=clock)
    owner.run_id = rid
    with store.read() as conn:
        run = dict(conn.execute('SELECT * FROM maintenance_runs WHERE id=?', (rid,)).fetchone())
    return owner, maintenance, run


@pytest.mark.parametrize('precreated', [False, True])
def test_new_database_memory_call_budget_defaults_to_300(tmp_path, precreated):
    path = tmp_path / 'new.db'
    if precreated:
        path.touch()
    store = Store(path)
    try:
        with store.read() as conn:
            assert consolidation_settings(conn)['max_calls'] == 300
        assert store.setting('consolidation')['max_calls'] == 300
    finally:
        store.close()


@pytest.mark.parametrize('saved,value,expected', [(False, 50, 300), (True, 50, 50),
                                                 (True, 7, 7), (False, 7, 7), (True, 0, 0)])
def test_migration_preserves_admin_choices_and_existing_run_snapshots(tmp_path, monkeypatch, saved, value, expected):
    path = tmp_path / 'old.db'
    original = db.files
    def old_resources(package):
        root = original(package)
        def joinpath(name):
            target = root.joinpath(name)
            if name == 'migrations':
                return SimpleNamespace(iterdir=lambda: [p for p in target.iterdir() if p.name != MIGRATION])
            return target
        return SimpleNamespace(joinpath=joinpath)
    with monkeypatch.context() as patch:
        patch.setattr(db, 'files', old_resources)
        old = Store(path)
    settings = {'max_calls': value, 'merge_enabled': False, 'method': 'strict', 'extra_legacy_field': 'keep'}
    old.set_setting('consolidation', settings)
    with old.write() as conn:
        if saved:
            operation(conn, 'consolidation_settings_saved', 'settings', 'consolidation', {'max_calls': value})
    rid = Maintenance(old, clock=Clock()).request()
    with old.read() as conn:
        snapshot = conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?', (rid,)).fetchone()[0]
    old.close()
    for _ in range(2):
        upgraded = Store(path)
        try:
            assert upgraded.setting('consolidation') == {**settings, 'max_calls': expected}
            with upgraded.read() as conn:
                assert conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?', (rid,)).fetchone()[0] == snapshot
        finally:
            upgraded.close()


def test_any_prior_admin_save_preserves_50_even_when_only_switch_was_saved(tmp_path, monkeypatch):
    # A real settings PATCH saves the complete effective object, even if the
    # administrator only changes a switch. The migration must respect that act.
    path = tmp_path / 'old.db'
    old = Store(path)
    old.set_setting('consolidation', {'max_calls': 50})
    with old.write() as conn:
        conn.execute('DELETE FROM schema_migrations WHERE version=?', (MIGRATION,))
        operation(conn, 'consolidation_settings_saved', 'settings', 'consolidation', {'merge_enabled': False})
    old.close()
    reopened = Store(path)
    try:
        assert reopened.setting('consolidation')['max_calls'] == 50
    finally:
        reopened.close()


@pytest.mark.parametrize('key', BUDGETS)
@pytest.mark.parametrize('value', [0, 51, 1000])
def test_budget_range_accepts_integer_boundaries(store, key, value):
    assert getattr(ConsolidationSettings(**{key: value}), key) == value
    store.set_setting('consolidation', {key: value})
    with store.read() as conn:
        assert consolidation_settings(conn)[key] == value


@pytest.mark.parametrize('key', BUDGETS)
@pytest.mark.parametrize('value', [-1, 1001, True, 1.5, '2'])
def test_budget_range_rejects_invalid_values(store, key, value):
    with pytest.raises(ValidationError):
        ConsolidationSettings(**{key: value})
    store.set_setting('consolidation', {key: value})
    with store.read() as conn, pytest.raises(ValueError):
        consolidation_settings(conn)


def test_old_settings_missing_new_keys_and_patch_roundtrip(store):
    store.set_setting('consolidation', {'max_calls': 50})
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        login_admin(client)
        client.app.state.scheduler.stop()
        value = client.get('/admin/api/settings').json()['consolidation']
        assert value['max_calls'] == 50
        expected = {'entry_profile_max_calls': 60, 'person_summary_max_calls': 40, 'persona_max_calls': 30}
        for key, limit in expected.items():
            assert value[key] == limit
            assert getattr(ConsolidationSettings(), key) == limit
        response = client.patch('/admin/api/settings/consolidation', json={'persona_max_calls': 9})
        assert response.status_code == 200
        assert response.json()['consolidation']['persona_max_calls'] == 9
        assert response.json()['consolidation']['max_calls'] == 50
        assert store.setting('consolidation')['person_summary_max_calls'] == value['person_summary_max_calls']


@pytest.mark.parametrize('exhausted', range(4))
def test_every_step_has_independent_limit_and_report(store, exhausted):
    owner, maintenance, run = owner_for(store)
    for _ in range(2):
        owner.reserve({'id': None}, PURPOSES[exhausted])
    with pytest.raises(ModelError) as error:
        owner.reserve({'id': None}, PURPOSES[exhausted])
    assert error.value.reason == 'call_budget'
    for step, purpose in enumerate(PURPOSES):
        if step != exhausted:
            owner.reserve({'id': None}, purpose)
            owner.reserve({'id': None}, purpose)
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM consolidation_calls WHERE run_id=?', (run['id'],)).fetchone()[0] == 8
    reports = [i for i in maintenance.report(run['id'])['items'] if i['outcome'] == 'model_step_budget']
    assert len(reports) == 4
    assert all(i['details']['calls_used'] == 2 and i['details']['max_calls'] == 2 for i in reports)
    assert sum(i['reason'] == 'call_budget' for i in reports) == 1


def test_resume_uses_snapshot_and_counts_interrupted_calls_without_reserving_twice(store):
    owner, maintenance, run = owner_for(store)
    owner.reserve({'id': None}, 'persona_generate')
    owner.reserve({'id': None}, 'consolidation_merge')
    store.set_setting('consolidation', dict.fromkeys(BUDGETS, 1000))
    reopened = Consolidation(store, owner.gateway, clock=owner.clock)
    reopened.run_id = run['id']
    with store.read() as conn:
        settings = run_settings(conn, run['id'])
        assert all(settings[key] == 2 for key in BUDGETS)
        assert conn.execute("SELECT COUNT(*) FROM consolidation_calls WHERE result='interrupted'").fetchone()[0] == 2
    with pytest.raises(ModelError):
        reopened.reserve({'id': None}, 'persona_generate_repair')
    reopened.reserve({'id': None}, 'persona_check')
    reopened.reserve({'id': None}, 'consolidation_conflict_repair')
    with pytest.raises(ModelError):
        reopened.reserve({'id': None}, 'consolidation_dependency')
    assert maintenance.report(run['id'])['summary']['model_calls']['count'] == 4


@pytest.mark.parametrize('reason', ['usage_limit', 'paused'])
@pytest.mark.parametrize('purpose', PURPOSES)
def test_global_gate_precedes_empty_step_budget(store, reason, purpose):
    owner, _, run = owner_for(store, **dict.fromkeys(BUDGETS, 0))
    class Health:
        def check(self, kind, call_purpose, **kwargs):
            assert (kind, call_purpose) == ('chat', 'learning')
            raise ModelError('paused', reason, reason=reason, paused=True)
    owner.gateway.health = Health()
    gateway = _PersonaGateway(owner)
    with pytest.raises(ModelError) as error:
        gateway.chat([], purpose)
    assert error.value.reason == reason
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM consolidation_calls WHERE run_id=?', (run['id'],)).fetchone()[0] == 0


def test_persona_global_gate_precedes_empty_preflight_budget(store):
    from test_maintenance_cognition import persona_case, complete
    engine, model, _ = persona_case(store, budget=0, persona_max_calls=0)
    class Health:
        def check(self, *args, **kwargs):
            raise ModelError('paused', 'limit', reason='usage_limit', paused=True)
    model.health = Health()
    report = complete(engine)
    assert report['persona']['reason'] == 'usage_limit'
    assert not model.calls


def test_summary_global_gate_precedes_empty_preflight_budget(store):
    from iris.person_summary import update_summaries_for_run
    from test_person_summary_consolidation import prepare
    clock, model, gateway, run, config = prepare(store, budget=0)
    class Health:
        def check(self, *args, **kwargs):
            raise ModelError('paused', 'limit', reason='usage_limit', paused=True)
    gateway.health = Health()
    update_summaries_for_run(store, gateway, run, config, clock=clock)
    with store.read() as conn:
        assert conn.execute('SELECT reason FROM person_summary_attempts WHERE run_id=?', (run['id'],)).fetchone()[0] == 'usage_limit'
    assert not model.calls


def test_stop_precedes_budget_and_does_not_consume_calls(store):
    owner, _, _ = owner_for(store, **dict.fromkeys(BUDGETS, 0))
    with pytest.raises(ModelError) as error:
        _PersonaGateway(owner, stop=lambda: True).chat([], 'persona_generate')
    assert error.value.reason == 'interrupted'
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM consolidation_calls').fetchone()[0] == 0


def test_pair_backlog_keeps_processing_order_and_excludes_dependencies(store):
    owner, maintenance, run = owner_for(store)
    payload = {'pair_ids': [], 'memories': [], 'resolution': 'report_only_v1',
               'report_version': consolidation.PROMPT_VERSION, 'merge_version': consolidation.MERGE_PROMPT_VERSION}
    with store.write() as conn:
        for i in range(2003):
            conn.execute('''INSERT INTO consolidation_work(fingerprint,kind,payload_json,importance,changed_at,created_at,state)
                VALUES(?,'pair',?,?,?,?,?)''', (str(i), dumps(payload), i % 4,
                f'2026-10-{10 + i % 5:02}T00:00:00+00:00', Clock()().isoformat(),
                ('pending', 'classified', 'decided')[i % 3]))
        conn.execute("INSERT INTO consolidation_work(fingerprint,kind,payload_json,importance,changed_at,created_at) VALUES('dep','dependency','{}',0,'2020-01-01','2020-01-01')")
        expected = [r[0] for r in conn.execute("SELECT id FROM consolidation_work WHERE kind='pair' ORDER BY importance DESC,julianday(changed_at) DESC,id LIMIT 2000")]
        config = run_settings(conn, run['id'])
    owner.plan(run, config)
    with store.read() as conn:
        kept = [r[0] for r in conn.execute("SELECT id FROM consolidation_work WHERE kind='pair' AND state IN ('pending','classified','decided') ORDER BY importance DESC,julianday(changed_at) DESC,id")]
        assert kept == expected
        assert conn.execute("SELECT COUNT(*) FROM consolidation_work WHERE state='backlog_trimmed'").fetchone()[0] == 3
        assert conn.execute("SELECT state FROM consolidation_work WHERE fingerprint='dep'").fetchone()[0] == 'pending'
        assert conn.execute('SELECT COUNT(*) FROM consolidation_receipts').fetchone()[0] == 0
    items = [i for i in maintenance.report(run['id'])['items'] if i['reason'] == 'backlog_trimmed']
    assert len(items) == 1 and items[0]['details']['count'] == 3
    owner.plan(run, config)
    items = [i for i in maintenance.report(run['id'])['items'] if i['reason'] == 'backlog_trimmed']
    assert len(items) == 1 and items[0]['details']['count'] == 3


@pytest.mark.parametrize('changed_side', [0, 1])
def test_trimmed_pair_reenters_only_after_semantic_change(store, monkeypatch, changed_side):
    a, b = pair(store)
    owner, _, run = owner_for(store)
    with store.read() as conn:
        config = run_settings(conn, run['id'])
    monkeypatch.setattr(consolidation, 'MAX_PENDING_PAIRS', 0, raising=False)
    owner.plan(run, config)
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consolidation_work WHERE state='backlog_trimmed'").fetchone()[0] == 1
    monkeypatch.setattr(consolidation, 'MAX_PENDING_PAIRS', 2000)
    second, _, next_run = owner_for(store)
    second.plan(next_run, config)
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consolidation_work WHERE state='pending'").fetchone()[0] == 0
    with store.write() as conn:
        conn.execute("UPDATE memories SET content=content||'，补充细节',revision=revision+1,updated_at=? WHERE id=?",
                     (Clock()().isoformat(), (a, b)[changed_side]))
    third, _, final_run = owner_for(store)
    third.plan(final_run, config)
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consolidation_work WHERE state='pending'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM consolidation_work WHERE state='backlog_trimmed'").fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM consolidation_receipts').fetchone()[0] == 0



def test_legacy_run_snapshot_missing_step_keys_keeps_memory_cap_and_call_counts(store):
    owner, maintenance, run = owner_for(store)
    legacy = dumps({'max_calls': 50, 'merge_enabled': True})
    with store.write() as conn:
        conn.execute('UPDATE consolidation_runs SET settings_json=? WHERE run_id=?', (legacy, run['id']))
    owner.reserve({'id': None}, 'persona_generate')
    store.set_setting('consolidation', dict.fromkeys(BUDGETS, 0))
    resumed = Consolidation(store, owner.gateway, clock=owner.clock)
    resumed.run_id = run['id']
    resumed.reserve({'id': None}, 'persona_check')
    with store.read() as conn:
        settings = run_settings(conn, run['id'])
        assert settings['max_calls'] == 50
        assert all(settings[key] == consolidation.DEFAULTS[key] for key in BUDGETS[1:])
        assert conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?', (run['id'],)).fetchone()[0] == legacy
    assert maintenance.report(run['id'])['summary']['model_calls']['count'] == 2


def test_pair_backlog_does_not_starve_summary_or_persona_and_resume_does_not_repeat(store):
    from test_maintenance_cognition import persona_case, complete
    from test_person_summary import Model as SummaryModel, seed
    engine, persona, clock = persona_case(store, budget=2, max_calls=1,
        merge_enabled=True, conflict_enabled=True, person_summary_max_calls=2, entry_profile_enabled=False)
    seed(store, clock, count=16)
    memory, summary = Model(store), SummaryModel(store)
    class Combined:
        def chat(self, messages, purpose, max_tokens=16000, **kwargs):
            model = persona if purpose.startswith('persona_') else summary if purpose.startswith('consolidation_person_summary_') else memory
            return model.chat(messages, purpose, max_tokens, **kwargs)
    engine.gateway = Combined()
    report = complete(engine)
    assert report['state'] == 'completed'
    assert report['consolidation']['skip_reason'] == 'call_budget'
    assert report['persona']['status'] == 'published'
    assert any(i['outcome'] == 'person_summary' and i['details']['status'] == 'published' for i in report['items'])
    assert len(memory.requests) == 1 and len(summary.calls) == len(persona.calls) == 2
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consolidation_work WHERE kind='pair' AND state='pending'").fetchone()[0] > 20
        assert consolidation.step_call_counts(conn, report['id']) == {'memory': 1, 'entry_profile': 0, 'person_summary': 2, 'persona': 2}
    Maintenance(store, gateway=engine.gateway, clock=clock).run(report['id'])
    assert len(memory.requests) == 1 and len(summary.calls) == len(persona.calls) == 2
    assert engine.report(report['id'])['summary']['model_calls']['count'] == 5


@pytest.mark.parametrize('reason', ['usage_limit', 'paused', 'interrupted'])
@pytest.mark.parametrize('purpose', PURPOSES)
def test_global_stop_during_http_retry_precedes_step_budget_without_extra_reservation(store, reason, purpose):
    import httpx
    from iris.models import Gateway, ModelConfig
    owner, _, run = owner_for(store, **dict.fromkeys(BUDGETS, 1))
    hits = []
    class Health:
        def check(self, *args, **kwargs):
            if hits and reason != 'interrupted':
                raise ModelError('paused', reason, reason=reason, paused=True)
        def observe(self, *args, **kwargs):
            return False
    def respond(request):
        assert not store._writer.in_transaction
        hits.append(1)
        return httpx.Response(429, json={'error': {'code': 'RateLimitExceeded'}}, headers={'Retry-After': '0'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway = Gateway({'chat': ModelConfig('https://example.invalid', '', 'fake')}, store, client, sleeper=lambda _: None)
        gateway.health = Health()
        owner.gateway = gateway
        try:
            with pytest.raises(ModelError) as error:
                _PersonaGateway(owner, stop=lambda: reason == 'interrupted' and bool(hits)).chat([], purpose)
            assert error.value.reason == reason
        finally:
            gateway.close()
    assert len(hits) == 1
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM consolidation_calls WHERE run_id=?', (run['id'],)).fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 1
        assert conn.execute("SELECT reason FROM maintenance_items WHERE item_key=?", ('budget:'+consolidation.budget_step(purpose),)).fetchone()[0] == reason
