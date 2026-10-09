"""Deterministic formatting repairs keep relation and evidence boundaries."""
import copy
import json

import pytest

from conftest import FakeGateway, batch, msg


@pytest.mark.parametrize(('relation', 'value'), [
    ('alias', '小棠'), ('same_as', '远客'), ('roleplay', '守桥人'),
])
def test_null_unused_relations_do_not_discard_a_valid_relation(store, relation, value):
    msg(store, 1, '大家叫我小棠。我扮演守桥人，远客可能也是我的账号。', sender='宋棠')
    item = {'name': '宋棠', 'alias': None, 'same_as': None, 'roleplay': None,
            'evidence': [1], 'world': '虚构故事'}
    item[relation] = value
    output = {'people': [item]}
    original = copy.deepcopy(output)
    formed, result = batch(store, FakeGateway(output), count=1)
    assert not result['dropped']
    with store.read() as conn:
        if relation == 'alias':
            assert [r[0] for r in conn.execute('SELECT alias FROM subject_aliases')] == [value]
        else:
            assert [r[0] for r in conn.execute('SELECT kind FROM subject_links')] == [relation]
        assert json.loads(conn.execute('SELECT raw_output FROM batch_attempts WHERE batch_id=?',
                                       (formed.id,)).fetchone()[0]) == original
    omitted = [n['field'] for n in result['normalizations']
               if n['reason'] == 'unused null relation omitted']
    assert set(omitted) == {'alias', 'same_as', 'roleplay'} - {relation}
    assert output == original


@pytest.mark.parametrize('relations', [
    {'alias': None, 'same_as': None, 'roleplay': None},
    {'alias': '', 'roleplay': '守桥人', 'same_as': None},
    {'alias': '小棠', 'roleplay': '守桥人', 'same_as': None},
])
def test_null_normalization_does_not_choose_between_invalid_relations(store, relations):
    msg(store, 1, '大家叫我小棠。我扮演守桥人。', sender='宋棠')
    _, result = batch(store, FakeGateway({'people': [
        {'name': '宋棠', **relations, 'evidence': [1]}
    ]}), count=1)
    assert len(result['dropped']) == 1
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM subject_links').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM subject_aliases').fetchone()[0] == 0


def test_null_normalization_does_not_relax_alias_own_evidence(store):
    msg(store, 1, '宋棠还叫小棠。', sender='闻舟')
    _, result = batch(store, FakeGateway({'people': [
        {'name': '宋棠', 'alias': '小棠', 'same_as': None, 'roleplay': None, 'evidence': [1]}
    ]}), count=1)
    assert [d['reason'] for d in result['dropped']] == ['alias subject has no own evidence in target segment']
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM subject_aliases').fetchone()[0] == 0


@pytest.mark.parametrize('name', ['宋棠', 'Mira Lane'])
@pytest.mark.parametrize('template', ['P1（{}）', 'P1({})', 'P1 （{}）', 'P1 ({})'])
def test_readable_parenthesized_display_label_is_written_once(store, name, template):
    from test_batches import memory
    msg(store, 1, '我喜欢旧书。', sender=name)
    label = template.format(name)
    item = memory(label + ' 喜欢旧书。', speaker='P1', about=['P1'])
    item['tags'] = [label]
    _, result = batch(store, FakeGateway({'memories': [item]}), count=1)
    assert not result['dropped']
    with store.read() as conn:
        saved = conn.execute('SELECT content FROM memories').fetchone()
        tags = [r[0] for r in conn.execute('SELECT tag FROM memory_tags')]
    assert saved['content'] == name + ' 喜欢旧书。'
    assert tags == [name]


def test_readable_parenthesis_which_is_not_a_display_name_is_preserved(store):
    from test_batches import memory
    msg(store, 1, '我喜欢旧书。', sender='宋棠')
    item = memory('P1（朋友）喜欢旧书。', speaker='P1', about=['P1'])
    _, result = batch(store, FakeGateway({'memories': [item]}), count=1)
    assert not result['dropped']
    with store.read() as conn:
        assert conn.execute('SELECT content FROM memories').fetchone()[0] == '宋棠（朋友）喜欢旧书。'
