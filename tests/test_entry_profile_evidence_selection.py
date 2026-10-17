"""EF-18: sufficient evidence is reviewed; omitted sources never become approved."""
import json

from iris import entry_profile as ep
from iris.db import dumps
from iris.memory_ops import entry_profile_source, write_entry_profile_memory, purge_memory
from test_entry_profile_incremental import NOW, Gateway, accept, add, fixed_projection_clock, profiles, projection, source
from test_entry_profile_incremental_quality import EVIDENCE, seed, check_seed


def frozen(store, mid):
    task = ep.IncrementalProfileTask(store, Gateway(store), clock=lambda: NOW)
    with store.write() as c:
        row = dict(c.execute('SELECT m.*,p.category FROM memories m JOIN entry_profile_memories p ON p.memory_id=m.id WHERE m.id=?', (mid,)).fetchone())
        sources = ep._inc_all_sources(c, row, NOW.isoformat())
        wid = task._queue_admission(c, row, sources)
        work = c.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?', (wid,)).fetchone()
    return task, dict(work) if work else None


def test_defaults_use_frozen_group_review_v6():
    p = ep.incremental_prompts()
    assert [(p[k]['name'], p[k]['sha256']) for k in ('observe', 'admit')] == [
        ('entry_profile_observe_v6', 'aa55d820d7b37163cced5edc2c89b32fa8f0c8ce30a20e4da2eaae0084aa3d85'),
        ('entry_profile_admit_v6', 'fedacee6636e3548e494f65d220fa44fc3c5b8eade7713202a222fe1908774dc')]


def test_only_earliest_and_latest_independent_sources_are_approved(store):
    mid, ids = seed(store, EVIDENCE)
    g = check_seed(store)
    assert len(g.calls) == 1
    assert [s['message_id'] for s in g.calls[0]['payload']['memory']['sources']] == [ids[0], ids[-1]]
    assert projection(store)['memory_ids'] == [mid]
    row = profiles(store)[0]
    assert {s['message']['position'] for s in row['sources']} == {ids[0], ids[-1]}
    assert {s['message']['position'] for s in row['pending_sources']} == set(ids[1:-1])


def test_latest_different_author_is_preferred_and_order_is_reproducible(store):
    mid, ids = seed(store, [(1, '甲', '望远镜支架铝材较轻。'), (4, '乙', '望远镜支架钢材较重。'),
                          (7, '甲', '望远镜支架木材怕潮。')])
    task, work = frozen(store, mid)
    payload = json.loads(work['payload_json'])
    assert [s['message_id'] for s in payload['memory']['sources']] == ids[:2]
    with store.write() as c:
        row = dict(c.execute('SELECT m.*,p.category FROM memories m JOIN entry_profile_memories p ON p.memory_id=m.id WHERE m.id=?', (mid,)).fetchone())
        sources = ep._inc_all_sources(c, row, NOW.isoformat())
        assert task._queue_admission(c, row, list(reversed(sources))) is None
        assert c.execute("SELECT COUNT(*) FROM entry_profile_incremental_work WHERE kind='admit'").fetchone()[0] == 1


def test_entity_witnesses_replace_redundant_generic_representatives(store):
    mid, ids = seed(store, [(1, '甲', '望远镜支架材料怎么选？'), (1, '乙', '在苏州比较AX1支架的钢材。'),
        (4, '丙', '望远镜支架重量如何？'), (4, '丁', '苏州的AX1支架也能用木材。'),
        (7, '戊', '望远镜支架的携带重量值得讨论。')], body='苏州AX1支架的选材。')
    g = check_seed(store)
    refs = g.calls[0]['payload']['memory']['sources']
    assert {s['message_id'] for s in refs} == {ids[1], ids[3]}
    assert all(t['independent_uses'] == 2 for t in g.calls[0]['payload']['memory']['topic_detail_review'])
    assert projection(store)['memory_ids'] == [mid]


def test_all_concrete_points_keep_their_two_witnesses_even_if_not_extremes(store):
    seed(store, [(1, '甲', 'AX1支架的底座采用钢材。'), (2, '乙', 'AX1支架的底座可用木材。'),
        (3, '丙', 'BX2支架的接头可以压紧。'), (4, '丁', 'BX2支架接头需检查松紧。'),
        (5, '戊', 'CX3支架的表面适合抛光。'), (6, '己', 'CX3支架的表面可以打磨。'),
        (7, '庚', '宽泛谈谈器材选用。'), (8, '辛', '另谈器材重量。')], body='讨论AX1、BX2、CX3支架。')
    g = check_seed(store)
    assert len(g.calls) == 1 and len(g.calls[0]['payload']['memory']['sources']) == 6
    assert all(t['independent_uses'] == 2 for t in g.calls[0]['payload']['memory']['topic_detail_review'])
    assert projection(store)


def test_single_occasion_detail_is_not_hidden_by_selection(store):
    mid, ids = seed(store, [(1, '甲', '望远镜支架铝材很轻。'), (3, '乙', '苏州的支架采用木材。'),
        (6, '丙', '望远镜支架的钢材更沉。'), (8, '丁', '望远镜支架的携带重量要平衡。')], body='苏州的望远镜支架选材。')
    def reject(p):
        hint = next(t for t in p['memory']['topic_detail_review'] if t['term'] == '苏州')
        assert hint['independent_uses'] == hint['all_evidence_uses'] == 1
        assert ids[1] in [s['message_id'] for s in p['memory']['sources']]
        return dict(decision='reject', violations=['单条消息泛化'], reason='苏州只出现一次。', safe_sources=[])
    check_seed(store, Gateway(store, checks=reject))
    assert not projection(store)


def test_required_definition_is_kept_but_redundant_definitions_are_not_all_sent(store):
    mid, ids = seed(store, [(1, '甲', '搭桥'), (3, '乙', '搭个桥'),
        (5, '丙', '搭桥就是衔接双方的句子。'), (7, '丁', '搭桥指把双方的句子衔接起来。'),
        (9, '戊', '搭桥呀')], body='搭桥：衔接双方的句子', category='saying')
    with store.write() as c:
        c.execute('UPDATE entry_profile_admissions SET definition_refs_json=? WHERE memory_id=?', (dumps(ids[2:4]), mid))
    g = check_seed(store)
    refs = g.calls[0]['payload']['memory']['sources']
    assert len(refs) <= 3
    assert len({r['message_id'] for r in refs} & set(ids[2:4])) == 1
    assert projection(store)


def test_shorter_equivalent_witnesses_can_fit_when_oldest_context_is_oversized(store):
    mid, ids = seed(store, [(1, '甲', '望远镜支架的材料很轻。'), (3, '乙', '望远镜支架木材很轻。'),
        (5, '丙', '望远镜支架铝材也轻。')])
    with store.write() as c:
        c.execute('UPDATE messages SET content=content || ? WHERE id=?', ('无关背景。' * 1000, ids[0]))
        # Bind again to the new full-message fingerprint, keeping the same
        # excerpt. This is oversized valid evidence, not an invalidated source.
        binding=entry_profile_source(c,ids[0],excerpt='望远镜支架的材料很轻。')
        c.execute('UPDATE sources SET note=? WHERE memory_id=? AND message_id=?', (dumps(binding),mid,ids[0]))
    g = check_seed(store)
    assert len(g.calls) == 1
    assert {s['message_id'] for s in g.calls[0]['payload']['memory']['sources']} == set(ids[1:])
    assert projection(store)


def test_omitted_source_changes_still_cancel_a_frozen_check(store):
    mid, ids = seed(store, EVIDENCE)
    task, work = frozen(store, mid)
    assert ids[3] not in [s['message_id'] for s in json.loads(work['payload_json'])['memory']['sources']]
    with store.write() as c:
        c.execute('UPDATE messages SET content=? WHERE id=?', ('内容已修改。', ids[3]))
    task._run_one(work)
    assert not task.gateway.calls and not projection(store)


def test_omitted_source_purge_clears_frozen_work(store):
    mid, ids = seed(store, EVIDENCE)
    task, work = frozen(store, mid)
    with store.read() as c:
        assert c.execute("SELECT 1 FROM purge_record_refs WHERE table_name='entry_profile_incremental_work' AND row_id=? AND ref_kind='message' AND ref_id=?", (str(work['id']), str(ids[3]))).fetchone()
    purge_memory(store, mid, profiles(store)[0]['revision'], confirm=True)
    task._run_one(work)
    with store.read() as c:
        row = c.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?', (work['id'],)).fetchone()
    assert row['state'] == 'cancelled' and '望远镜' not in row['payload_json']
    assert not task.gateway.calls


def test_more_than_writer_batch_limit_accumulated_sources_do_not_block_check(store):
    mid, ids = seed(store, EVIDENCE)
    # Mechanical cardinality fixture with distinct source hashes; these rows
    # are never model-evaluation corpus. Same-day copies add no topic days.
    for n, (day, sender, text) in enumerate(EVIDENCE * 4):
        message = add(store, text + ' 测试行号 ' + str(n) + '。', day, sender=sender, key='history-' + str(n))
        with store.write() as c:
            write_entry_profile_memory(store, 'group', op='confirm', memory_id=mid, expected_revision=1,
                sources=[entry_profile_source(c, message)], current=NOW, _conn=c)
            c.execute('UPDATE entry_profile_memory_state SET cursor=?,scan_cursor=? WHERE entry_id=?', (message, message, 'group'))
    g = check_seed(store)
    assert len(g.calls) == 1 and len(g.calls[0]['payload']['memory']['sources']) == 2
    assert projection(store)['memory_ids'] == [mid]
    assert len(profiles(store)[0]['pending_sources']) == 38


def test_revision_keeps_new_basis_and_validates_omitted_old_snapshot(store):
    mid, ids = seed(store, EVIDENCE)
    check_seed(store)
    message = add(store, '继续比较望远镜支架的钢材和木材。', 10, sender='壬')
    def observe(p):
        return {'observations': [dict(op='change', memory_id=mid, action='revise', category='topic',
            body='讨论望远镜支架的材料。', saying='', meaning='', definition_refs=[],
            source_refs=[source(p['messages'][0])])]}
    g = Gateway(store, observations=observe)
    task = ep.IncrementalProfileTask(store, g, clock=lambda: NOW)
    task.run('group')
    assert projection(store)['memory_ids'] == [mid]
    assert profiles(store)[0]['body'] == '讨论望远镜支架的材料。'
    checks = [call for call in g.calls if 'admit' in call['purpose']]
    assert len(checks) == 1 and len(checks[0]['payload']['memory']['sources']) <= 3
    assert message in [s['message_id'] for s in checks[0]['payload']['memory']['sources']]


def test_quote_bridge_is_not_lost_from_full_pool_detail_diagnostics(store):
    mid, ids = seed(store, [(1, '甲', '苏州的支架图纸谁还有？'), (3, '乙', '支架图纸我找找。'),
        (5, '丙', '苏州那份已经找到了。'), (7, '丁', '重新比较支架材料重量。')], body='苏州的支架材料。')
    with store.write() as c:
        for parent, child in ((ids[0], ids[1]), (ids[1], ids[2])):
            original = c.execute('SELECT sender_subject_id,content FROM messages WHERE id=?', (parent,)).fetchone()
            c.execute('UPDATE messages SET quote_author_subject_id=?,quote_content=? WHERE id=?', (*original, child))
    def check(p):
        hint = next(t for t in p['memory']['topic_detail_review'] if t['term'] == '苏州')
        assert hint['independent_uses'] == hint['all_evidence_uses'] == 1
        return dict(decision='reject', violations=['单条消息泛化'], reason='同一请求的后续。', safe_sources=[])
    check_seed(store, Gateway(store, checks=check))
    assert not projection(store)
