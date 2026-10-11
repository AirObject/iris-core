"""Trial diagnostics observe one preparation and survive reply reuse / restart."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGateway, login_admin
from iris.api import create_app
from iris.models import ModelError
from iris.trial import TrialReplies
from test_admin_ui import trial, send
from test_retrieval import put


class JudgeGateway(FakeGateway):
    def __init__(self, store, reply, *, degraded=False):
        super().__init__(reply)
        self.store, self.degraded, self.judged = store, degraded, 0

    def recall_judge(self, messages, ids, **kwargs):
        assert not self.store._writer.in_transaction
        self.judged += 1
        if self.degraded:
            raise ModelError('timeout', 'private provider detail', reason='timeout')
        return {'scores': [83, 24][:len(ids)], 'queue_ms': 0, 'network_ms': 1}


@pytest.mark.parametrize('reply', [{}, {'reply': ' '}, ModelError('retryable', 'private provider detail')])
@pytest.mark.parametrize('degraded', [False, True])
def test_failure_has_candidates_scores_and_reason_without_extra_recall(store, reply, degraded):
    a = put(store, '天文摄影需要赤道仪跟踪星星')
    b = put(store, '天文摄影的观测地点在郊外')
    gateway = JudgeGateway(store, reply, degraded=degraded)
    with TestClient(create_app(store=store, gateway=gateway), base_url='http://127.0.0.1', client=('127.0.0.1', 1000)) as c:
        c.app.state.scheduler.stop()
        login_admin(c)
        e = trial(c)
        mid = send(c, e, '天文摄影').json()['message_id']
        result = c.post(f"/admin/api/trial/entries/{e['id']}/reply", json={'message_id': mid})
        assert result.status_code == 502 and 'private provider detail' not in result.text
        diagnostic = result.json()['result']['diagnostic']
        assert diagnostic['message_id'] == mid and diagnostic['reply_message_id'] is None
        assert {m['id'] for m in diagnostic['candidates']} == {a, b}
        assert {m['revision'] for m in diagnostic['candidates']} == {1}
        assert diagnostic['judgment']['status'] == ('degraded' if degraded else 'applied')
        assert diagnostic['judgment']['reason'] == ('timeout' if degraded else None)
        assert [m['support'] for m in diagnostic['candidates']] == ([None, None] if degraded else [83, 24])
        assert gateway.judged == 1 and len(gateway.requests) == 1
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM recalls').fetchone()[0] == 1
            assert conn.execute('SELECT COUNT(*) FROM recall_items WHERE used_at IS NOT NULL').fetchone()[0] == 0
            assert {row[0] for row in conn.execute('SELECT retention FROM memories')} == {50}
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE kind='self_output'").fetchone()[0] == 0
            receipt = json.loads(conn.execute('SELECT request_json FROM recalls').fetchone()[0])
            assert '天文摄影' not in json.dumps(receipt, ensure_ascii=False)


def test_reuse_reads_original_receipt_even_after_another_preparation_and_restart(store):
    put(store, '天文摄影需要赤道仪跟踪星星')
    gateway = JudgeGateway(store, {'reply': '可以先看看赤道仪。'})
    with TestClient(create_app(store=store, gateway=gateway), base_url='http://127.0.0.1', client=('127.0.0.1', 1000)) as c:
        c.app.state.scheduler.stop()
        login_admin(c)
        e = trial(c)
        mid = send(c, e, '天文摄影').json()['message_id']
        url = f"/admin/api/trial/entries/{e['id']}/reply"
        first = c.post(url, json={'message_id': mid}).json()
        c.post(f"/admin/api/trial/entries/{e['id']}/prepare")
        c.app.state.trial_replies = TrialReplies(store, gateway, c.app.state.health)
        with store.read() as conn:
            recalls = conn.execute('SELECT COUNT(*) FROM recalls').fetchone()[0]
        calls = gateway.judged
        second = c.post(url, json={'message_id': mid}).json()
        assert second['reused'] and second['prepared'] is None
        assert second['diagnostic'] == first['diagnostic']
        assert second['diagnostic']['reply_message_id'] == first['message']['id']
        assert gateway.judged == calls and len(gateway.requests) == 1
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM recalls').fetchone()[0] == recalls
            assert conn.execute('SELECT COUNT(*) FROM recall_items WHERE used_at IS NOT NULL').fetchone()[0] == 0
        # Receipt retention/legacy replies must not synthesize a new diagnosis.
        with store.write() as conn:
            conn.execute('DELETE FROM recalls')
        old = c.post(url, json={'message_id': mid}).json()
        assert old['reused'] and old['diagnostic'] is None
        assert gateway.judged == calls
