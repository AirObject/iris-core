"""Handwritten person-summary and prompt-provenance fixtures, not quality data."""
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import hashlib
import json

import pytest

from iris import person_summary as ps


def summary_memory(ref, text, *, age=0, kind='事实', event_time=None):
    return {'ref': ref, 'memory_id': int(ref[1:]), 'revision': 1, 'sha256': ref,
            'content': text, 'kind': kind, 'stance': '亲历', 'event_time': event_time,
            'created_at': (NOW - timedelta(days=age)).isoformat()}


NOW = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)


def summary(text, memories, *, kind='fact', **extra):
    return ps._resolve({'items': [{'kind': kind, 'text': text,
        'memory_refs': [m['ref'] for m in memories], **extra}]},
        {'memories': memories}, None, NOW, quality_checks=True)


def test_undated_old_wish_cannot_become_a_fact_or_gain_an_invented_deadline():
    rows = [summary_memory('M1', '甲想学陶艺', age=31)]
    assert summary('甲想学陶艺', rows)[1][0]['reason'] == 'expired_undated_intent'
    assert summary('甲想学陶艺', rows, kind='recent', time_range='近期',
                   valid_until='2026-12-30')[1][0]['reason'] == 'expired_undated_intent'


def test_unrelated_new_fact_and_confirmation_do_not_refresh_old_wish():
    old = summary_memory('M1', '甲想学陶艺', age=50)
    old['last_confirmed_at'] = NOW.isoformat()
    rows = [old, summary_memory('M2', '甲住在合肥')]
    assert not summary('甲想学陶艺', rows, kind='recent', time_range='近期')[0]


def test_wish_cannot_be_upgraded_even_when_memory_kind_is_plan():
    rows = [summary_memory('M1', '甲想尝试陶艺', kind='计划')]
    assert summary('甲计划学习陶艺', rows, kind='recent', time_range='近期')[1][0]['reason'] == 'intent_strengthened'


def test_dated_plan_must_be_recent_with_a_deadline():
    rows = [summary_memory('M1', '甲计划2026年10月20日前交木工图', kind='计划')]
    assert summary('甲计划交木工图', rows)[1][0]['reason'] == 'intent_requires_recent'
    assert summary('甲计划交木工图', rows, kind='recent', time_range='10月20日前')[1][0]['reason'] == 'dated_intent_requires_deadline'
    kept, deleted, _ = summary('甲计划交木工图', rows, kind='recent',
                               time_range='10月20日前', valid_until='2026-10-20')
    assert kept[0]['valid_until'] == '2026-10-20' and not deleted


def test_recent_undated_wish_expires_from_its_own_event_time():
    rows = [summary_memory('M1', '甲想学陶艺', age=3)]
    kept, deleted, _ = summary('甲想学陶艺', rows, kind='recent', time_range='最近')
    assert not deleted and kept[0]['expires_at'] == (NOW + timedelta(days=27)).isoformat()
    rows[0]['event_time'] = (NOW - timedelta(days=40)).isoformat()
    assert not summary('甲想学陶艺', rows, kind='recent', time_range='最近')[0]


def test_factual_join_year_and_preference_reason_are_not_wishes():
    rows = [summary_memory('M1', '甲自称是老成员，2021年加入读书群', age=50)]
    assert summary('甲2021年加入读书群', rows)[0]
    rows = [summary_memory('M1', '甲喜欢原声电影，因为想听演员原音', age=50)]
    assert summary('甲偏好原声电影', rows, kind='preference')[0]


@pytest.mark.parametrize('selected,version', [(None, 'v2'), ('v1', 'v1'), ('v2', 'v2')])
def test_default_and_explicit_prompts_record_the_exact_sent_hash(store, selected, version):
    from test_person_summary import Clock, Model, seed
    clock = Clock(); seed(store, clock)
    model = Model(store)
    sent_hashes = []; original = model.chat
    def chat(request, purpose, **kwargs):
        sent_hashes.append(hashlib.sha256(request[0]['content'].encode('utf-8')).hexdigest())
        return original(request, purpose, **kwargs)
    model.chat = chat
    engine = ps.PersonSummaryEngine(store, model, clock=clock, prompt_version=selected)
    result = engine.regenerate('a', expected_version=0)
    assert result['status'] == 'published'
    with store.read() as conn:
        calls = json.loads(conn.execute('SELECT calls_json FROM person_summary_attempts').fetchone()[0])
    for call, kind, sent_hash in zip(calls, ('generate', 'check'), sent_hashes, strict=True):
        content = files('iris').joinpath(f'prompts/person_summary_{kind}_{version}.md').read_bytes()
        assert call['prompt_version'] == f'person_summary_{kind}_{version}'
        assert call['prompt_sha256'] == hashlib.sha256(content).hexdigest() == sent_hash


def test_unrelated_new_memory_cannot_extend_a_recent_wishs_expiry():
    rows = [summary_memory('M1', '甲想学陶艺', age=20), summary_memory('M2', '甲住在合肥')]
    kept, deleted, _ = summary('甲想学陶艺', rows, kind='recent', time_range='最近')
    assert not deleted
    assert kept[0]['expires_at'] == (NOW + timedelta(days=10)).isoformat()


@pytest.mark.parametrize('family,version', [('entry_profile','v2'), ('person_summary','v2')])
def test_runner_records_loaded_versions_in_fingerprinted_run_json(tmp_path, family, version, monkeypatch):
    from pathlib import Path
    if family == 'entry_profile':
        from profile_legacy import use_legacy_schema
        use_legacy_schema(monkeypatch)
        from test_entry_profile_eval import _runner as runner, corpus
        from test_entry_profile import Model
        path = corpus(tmp_path)
    else:
        from test_person_summary_eval import runner, fixture_case
        from test_person_summary import Model
        path = tmp_path / 'fixture.json'
        path.write_text(json.dumps({'format_version': 1, 'cases': [fixture_case()]}, ensure_ascii=False), encoding='utf-8')
    captured = []
    def gateway(configs, store):
        fake = Model(store); original = fake.chat
        def chat(request, purpose, **kwargs):
            captured.append(hashlib.sha256(request[0]['content'].encode('utf-8')).hexdigest())
            return original(request, purpose, **kwargs)
        fake.chat = chat
        return fake
    report = runner.run_eval(path, tmp_path/'run', gateway_factory=gateway, prompt_version=version)
    materials = Path(report['materials'])
    run = json.loads((materials/'run.json').read_text())
    assert report['prompt_versions'] == run['prompt_versions'] == [f'{family}_{kind}_{version}' for kind in ('generate', 'check')]
    assert report['prompt_sha256'] == run['prompt_sha256']
    assert captured and all(sha in run['prompt_sha256'].values() for sha in captured)
    runner._load_materials(materials)  # Input/run/case fingerprint consistency.


@pytest.mark.parametrize('version,status', [(None, 'rejected'), ('v1', 'published')])
def test_default_checks_old_wishes_while_explicit_v1_preserves_baseline(store, version, status):
    from test_person_summary import Clock, Model, memory, seed
    clock = Clock(); seed(store, clock)
    memory(store, clock, '甲同学想学习陶艺。', days_ago=40, importance=100)
    model = Model(store, items=[{'kind': 'fact', 'text': '甲同学想学习陶艺。',
                                'memory_refs': ['M1']}])
    result = ps.PersonSummaryEngine(store, model, clock=clock, prompt_version=version).regenerate('a', expected_version=0)
    assert result['status'] == status
    assert '甲同学想学习陶艺。' == model.calls[0][1]['evidence']['memories'][0]['content']
    if version is None:
        assert result['version']['checks']['deleted_sentences'][0]['reason'] == 'expired_undated_intent'
        assert len(model.calls) == 1
    else:
        assert len(model.calls) == 2
