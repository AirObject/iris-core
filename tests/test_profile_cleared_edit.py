"""Cleared profile editing uses fresh admin text without restoring old evidence."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris import entry_profile, person_summary
from iris.api import create_app
from iris.db import dumps
from iris.memory_ops import purge_memory
from iris.persona import split_sentences
from test_entry_profile import TEXTS, memory
from test_entry_profile_v2 import item, run, setup
from test_person_summary import Clock, generate, seed


@pytest.fixture(params=['person_summary', 'entry_profile'])
def published_profile(store, request):
    clock = Clock()
    if request.param == 'person_summary':
        mid = seed(store, clock)[0]
        result, _ = generate(store, clock, items=[
            {'kind': 'fact', 'text': '正在学习木工。', 'memory_refs': ['M1']},
            {'kind': 'preference', 'text': '喜欢手工制作。', 'memory_refs': ['M2']},
        ])
        module, target = person_summary, 'a'
        base = '/admin/api/people/a/summary'
        content = '管理员记录的全新说明。\n另一条手写内容。'
    else:
        setup(store, clock)
        mid = memory(store, clock, [1], text='翻面指反看试印纸背面')
        result, _ = run(store, clock, [item(memory_refs=['M1'])])
        module, target = entry_profile, 'group'
        base = '/admin/api/entries/group/profile'
        content = '\n'.join(TEXTS)
    assert result['status'] == 'published'
    assert any(b.get('memory_id') == mid for s in result['version']['sentences'] for b in s['basis'])
    return SimpleNamespace(module=module, target=target, base=base, clock=clock,
                           memory_id=mid, version=result['version'], content=content)


@pytest.fixture
def client(store, published_profile):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        yield client


@pytest.mark.parametrize('via_api', [False, True], ids=['data-api', 'http'])
def test_admin_edit_after_purge_accepts_fresh_text(client, store, published_profile, via_api):
    profile = published_profile
    purge_memory(store, profile.memory_id, 1, confirm=True)
    current = client.get(profile.base).json()['current']
    cleared = client.get(profile.base + f'/versions/{current["version"]}').json()
    assert cleared['content'] == ''
    expected = cleared['version']
    # Person summaries keep redacted item metadata in place. Entry profiles
    # publish a successor with affected items removed altogether.
    if profile.module is person_summary:
        assert cleared['sentences'] and all(not s['text'] for s in cleared['sentences'])
    else:
        assert cleared['sentences'] == []
    stale = client.put(profile.base, json={'expected_version': expected - 1, 'content': profile.content})
    assert stale.status_code == 409
    if via_api:
        response = client.put(profile.base, json={'expected_version': expected, 'content': profile.content})
        assert response.status_code == 200, response.text
        edited = response.json()
    else:
        edited = profile.module.admin_edit(store, profile.target, profile.content,
                                          expected_version=expected, clock=profile.clock)
    assert edited['version'] == expected + 1
    assert edited['content'] == profile.content and edited['status'] == 'published'
    assert not edited['possibly_stale'] and not edited['stale_basis']
    assert [s['text'] for s in edited['sentences']] == split_sentences(profile.content)
    assert all(s['author'] == 'admin' and not s['basis'] and not s.get('kind') for s in edited['sentences'])
    saved = client.get(profile.base + f'/versions/{edited["version"]}').json()
    assert all(s['admin_written'] and not s['basis'] for s in saved['sentences'])
    old = client.get(profile.base + f'/versions/{profile.version["version"]}').json()
    assert old['content'] == '' and all(not s['text'] for s in old['sentences'])
    assert client.put(profile.base, json={'expected_version': expected, 'content': '过期写入。'}).status_code == 409
    assert client.post(profile.base + f'/versions/{profile.version["version"]}/rollback',
                       json={'expected_version': edited['version']}).status_code == 400
    assert client.get(profile.base).json()['current']['version'] == edited['version']
    if profile.module is person_summary:
        with store.read() as conn:
            assert conn.execute('''SELECT COUNT(*) FROM person_summary_version_memories d
                JOIN person_summary_versions v ON v.id=d.version_id
                WHERE v.subject_id=? AND v.version=?''', (profile.target, edited['version'])).fetchone()[0] == 0


@pytest.mark.parametrize('module, structure', [
    (person_summary, {'kind': 'fact'}),
    (entry_profile, {'format_version': 2}),
], ids=['person-summary', 'entry-profile'])
@pytest.mark.parametrize('keep_valid', [False, True], ids=['fully-cleared', 'mixed'])
def test_edit_segments_ignore_empty_structured_items(module, structure, keep_valid):
    valid = '原有条目（“引文中有句号。仍属同一条。”）。\n'
    fresh = '全新手写内容。\n补充说明。'
    previous = {'sentences': [{'text': '', **structure}]}
    if keep_valid:
        previous['sentences'].append({'text': valid, **structure})
    content = (valid if keep_valid else '') + fresh
    assert module._edit_segments(content, previous) == ([valid] if keep_valid else []) + split_sentences(fresh)


def test_entry_admin_edit_does_not_copy_empty_structured_evidence(store):
    clock = Clock()
    setup(store, clock)
    mid = memory(store, clock, [1], text='翻面指反看试印纸背面')
    result, _ = run(store, clock, [item(memory_refs=['M1'])])
    assert result['status'] == 'published'
    # Model the empty-item boundary directly: normal entry-profile purge
    # removes items, and its database triggers also scrub purged references.
    empty = {'text': '', 'format_version': 2, 'kind': 'saying', 'author': 'model',
             'basis': [{'memory_id': mid, 'revision': 1}], 'message_count': 0}
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='deleted',revision=revision+1 WHERE id=?", (mid,))
        conn.execute("""UPDATE entry_profile_versions SET content='',sentences_json=?,material_json='{}'
            WHERE entry_id='group' AND version=1""", (dumps([empty]),))
    cleared = entry_profile.current_profile(store, 'group')
    assert cleared['content'] == '' and cleared['sentences'] == [empty]
    assert cleared['possibly_stale']
    content = '\n'.join(TEXTS)
    edited = entry_profile.admin_edit(store, 'group', content, expected_version=cleared['version'], clock=clock)
    assert [s['text'] for s in edited['sentences']] == split_sentences(content)
    assert all(s['author'] == 'admin' and not s['basis'] for s in edited['sentences'])
    assert not edited['possibly_stale']


def test_summary_edit_keeps_valid_structured_evidence_and_rejects_changed_basis(store):
    clock = Clock()
    mids = seed(store, clock)
    result, _ = generate(store, clock)
    original = result['version']['sentences'][0]
    content = result['version']['content'] + '管理员补充。'
    edited = person_summary.admin_edit(store, 'a', content, expected_version=1, clock=clock)
    assert edited['sentences'][0] == original
    assert edited['sentences'][1]['author'] == 'admin' and not edited['sentences'][1]['basis']
    with store.write() as conn:
        conn.execute('UPDATE memories SET revision=revision+1 WHERE id=?', (mids[0],))
    with pytest.raises(person_summary.PersonSummaryConflict, match='evidence_changed'):
        person_summary.admin_edit(store, 'a', content, expected_version=2, clock=clock)
    assert person_summary.current_summary(store, 'a', clock=clock)['version'] == 2


# Historical document-path regression on the pre-switch schema.
from profile_legacy import legacy_schema
