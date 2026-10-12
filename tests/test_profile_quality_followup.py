"""Handwritten EF mechanism fixtures, not quality evaluation data."""
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import hashlib
import json

import pytest

from iris import entry_profile as ep, person_summary as ps


def message(mid, content, day=1, *, minute=0, speaker='a', quote=None):
    instant = datetime(2026, 10, day, 10, tzinfo=timezone.utc) + timedelta(minutes=minute)
    row = dict(message_id=mid, entry_id='group', date=instant.date().isoformat(),
               occurred_at=instant.isoformat(), content=content, speaker=speaker, sha256=str(mid))
    if quote:
        row['quote'] = {'speaker': 'original', 'content': quote}
    return row


def entry(rows, *, kind='topic', label='纸张试印', meaning='', refs=(), memories=(), quotes=None):
    evidence = {'entry': {'id': 'group'}, 'as_of': '2026-10-13T10:00:00+00:00',
                'messages': rows, 'memories': list(memories)}
    item = {'kind': kind, 'label': label, 'quotes': quotes or [
        {'message_id': row['message_id'], 'text': row['content']} for row in rows[:3]]}
    if meaning:
        item['meaning'] = meaning
    if refs:
        item['memory_refs'] = list(refs)
    return ep._resolve({'items': [item]}, evidence, None, quality_checks=True)


def test_default_remains_v2_and_candidate_is_explicit(store):
    assert ep.EntryProfileEngine(store, None).prompt_version == 'v2'
    assert ep.EntryProfileEngine(store, None, prompt_version='v7').prompt_version == 'v7'
    with pytest.raises(ValueError):
        ep.EntryProfileEngine(store, None, prompt_version='../v7')


@pytest.mark.parametrize('rows', [
    [message(1, '这周试印纸张吸水很快'), message(2, '这周试印纸张吸水很快', 2)],
    [message(1, '试印，纸张吸水很快！'), message(2, '试印 纸张吸水很快。', 2)],
    [message(1, '这批试印纸张的正面颜色很均匀'), message(2, '这批试印纸张的正面颜色非常均匀', 2)],
    [message(1, '纸张试印的边缘留白很清楚'), message(2, '转述上次的纸张试印：边缘留白清楚', 2)],
    [message(1, '纸张试印的边缘留白很清楚'), message(2, '我也报名纸张试印', 2)],
    [message(1, '纸张试印的边缘留白很清楚'), message(2, '接龙：2. 纸张试印 +1', 2)],
    [message(1, '纸张试印的边缘留白很清楚'), message(2, '纸张试印我也想看', 2, quote='纸张试印的边缘留白很清楚')],
])
def test_topic_rejects_copies_retellings_quote_replies_and_registration(rows):
    kept, deleted, *_ = entry(rows)
    assert not kept
    assert deleted[0]['reason'] == 'insufficient_independent_discussions'


def test_midnight_is_not_an_independent_discussion():
    rows = [message(1, '今晚试印的纸边洇开了'), message(2, '另一种纸张试印要换墨', minute=2)]
    rows[0]['date'], rows[1]['date'] = '2026-10-01', '2026-10-02'
    assert entry(rows)[1][0]['reason'] == 'insufficient_independent_discussions'


def test_distinct_discussions_pass_and_order_does_not_choose_bad_pair():
    rows = [message(1, '今天试印发现纸张的背面有渗墨'),
            message(2, '转述上次的纸张试印结果', 2),
            message(3, '新一轮纸张试印比较了三种纸的颜色', 4, speaker='b')]
    assert entry(rows)[0]
    assert entry(list(reversed(rows)))[0]


def test_same_quote_and_duplicate_id_never_supply_two_observations():
    rows = [message(1, '大家看原话', quote='收帆指暂停当前步骤'),
            message(2, '再转一次', 2, quote='收帆指暂停当前步骤')]
    quotes = [{'message_id': i, 'text': '收帆指暂停当前步骤'} for i in (1, 2)]
    assert not entry(rows, kind='saying', label='收帆', quotes=quotes)[0]
    rows = [message(1, '现在收帆，留到下次再试')]
    quotes = [{'message_id': 1, 'text': '收帆'}, {'message_id': 1, 'text': '留到下次再试'}]
    assert not entry(rows, kind='saying', label='收帆', quotes=quotes)[0]


@pytest.mark.parametrize('minute,speaker', [(0.5, 'b'), (2, 'a'), (29, 'a')])
def test_saying_rejects_same_minute_and_same_speaker_bursts(minute, speaker):
    rows = [message(1, '先收帆，等水温降下来'),
            message(2, '这一步收帆，我们下次再接着做', minute=minute, speaker=speaker)]
    assert entry(rows, kind='saying', label='收帆')[1][0]['reason'] == 'insufficient_independent_uses'


def test_saying_needs_the_label_in_each_independent_original():
    rows = [message(1, '这里说收帆就是暂停'), message(2, '今天暂停这一段，明天再动', 3)]
    assert not entry(rows, kind='saying', label='收帆')[0]
    rows[1]['content'] = '今天收帆，等明天工具到齐再动'
    assert entry(rows, kind='saying', label='收帆')[0]
    assert entry(rows, kind='saying', label='收帆')[0][0]['meaning'] == ''


def test_unbound_message_cannot_supply_a_second_use():
    rows = [message(1, '收帆就是暂停'), message(2, '收帆吧，等明天继续', 3)]
    assert not entry(rows, kind='saying', label='收帆',
                     quotes=[{'message_id': 1, 'text': rows[0]['content']}])[0]


@pytest.mark.parametrize('label,meaning', [('他喜欢的纸张', ''), ('她們的说法', ''),
                                         ('收帆', '让他等待'), ('收帆', 'She waits')])
def test_gender_pronouns_in_prose_are_removed(label, meaning):
    rows = [message(1, '收帆就是暂停'), message(2, '今天收帆，明天接着做', 3)]
    assert entry(rows, kind='saying', label=label, meaning=meaning)[1][0]['reason'] == 'gendered_pronoun'


def test_gender_filter_does_not_modify_quotes_or_nonpronoun_words():
    rows = [message(1, '其他纸张要试印；她说过这句'), message(2, '吉他谱也要选试印纸张', 3)]
    assert entry(rows, label='其他纸张与吉他谱')[0]
    # Privacy of the quotation is still the model checker's job.


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


def test_candidate_prompts_loaded_once_and_calls_record_the_exact_hash(store):
    from test_person_summary import Clock, Model, seed
    clock = Clock(); seed(store, clock)
    model = Model(store)
    engine = ps.PersonSummaryEngine(store, model, clock=clock, prompt_version='v2')
    result = engine.regenerate('a', expected_version=0)
    assert result['status'] == 'published'
    with store.read() as conn:
        calls = json.loads(conn.execute('SELECT calls_json FROM person_summary_attempts').fetchone()[0])
    for call, kind in zip(calls, ('generate', 'check'), strict=True):
        content = files('iris').joinpath(f'prompts/person_summary_{kind}_v2.md').read_bytes()
        assert call['prompt_version'] == f'person_summary_{kind}_v2'
        assert call['prompt_sha256'] == hashlib.sha256(content).hexdigest()


def test_continuous_same_speaker_burst_is_one_use_even_across_thirty_minutes():
    rows = [message(1, '先收帆，把工具放好'), message(2, '收帆吧，水还在流', minute=10),
            message(3, '仍然收帆，别动这一页', minute=20),
            message(4, '现在继续收帆，等它变干', minute=29),
            message(5, '这里收帆，我们回头再说', minute=40)]
    quotes = [{'message_id': i, 'text': rows[i - 1]['content']} for i in (1, 5)]
    assert not entry(rows, kind='saying', label='收帆', quotes=quotes)[0]


def test_named_topic_needs_two_independent_bound_mentions_not_just_two_dates():
    rows = [message(1, '《石湾图谱》这一页是蓝色'),
            message(2, '《石湾图谱》还在比较颜色', minute=2),
            message(3, '另一本图谱使用了红色', 4)]
    rows[1]['date'] = '2026-10-02'
    assert entry(rows, label='《石湾图谱》的颜色')[1][0]['reason'] == 'insufficient_named_topic_recurrence'


def test_unrelated_new_memory_cannot_extend_a_recent_wishs_expiry():
    rows = [summary_memory('M1', '甲想学陶艺', age=20), summary_memory('M2', '甲住在合肥')]
    kept, deleted, _ = summary('甲想学陶艺', rows, kind='recent', time_range='最近')
    assert not deleted
    assert kept[0]['expires_at'] == (NOW + timedelta(days=10)).isoformat()


@pytest.mark.parametrize('family,version', [('entry_profile','v7'), ('person_summary','v2')])
def test_runner_records_loaded_versions_in_fingerprinted_run_json(tmp_path, family, version):
    from pathlib import Path
    if family == 'entry_profile':
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


def test_v7_checks_privacy_after_deterministic_usage_validation_outside_transactions(store):
    from test_entry_profile import Clock, Model, messages
    from iris.models import ModelReply
    from iris.db import dumps
    clock = Clock()
    messages(store, clock, text='收帆，先等纸干')
    second = messages(store, clock, count=1, days_ago=2, text='这里收帆，明天再继续')[0]
    model = Model(store, bad=(0,), violations=('个人信息或隐私',))
    original = model.chat; seen = []
    def chat(request, purpose, **kwargs):
        assert not store._writer.in_transaction
        seen.append(request[0]['content'])
        reply = original(request, purpose, **kwargs)
        if purpose.endswith('generate'):
            return ModelReply(dumps({'items': [{'kind': 'saying', 'label': '收帆', 'quotes': [
                {'message_id': 1, 'text': '收帆，先等纸干'},
                {'message_id': second, 'text': '这里收帆，明天再继续'}]}]}), 'stop', {})
        return reply
    model.chat = chat
    result = ep.EntryProfileEngine(store, model, clock=clock, prompt_version='v7').regenerate('group', expected_version=0)
    assert result['status'] == 'rejected'
    assert result['version']['checks']['deleted_sentences'][0]['stage'] == 'model'
    assert ep.current_profile(store, 'group') is None
    assert seen == [files('iris').joinpath(f'prompts/entry_profile_{kind}_v7.md').read_text(encoding='utf-8')
                    for kind in ('generate', 'check')]
