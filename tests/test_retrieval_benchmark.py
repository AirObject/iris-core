"""The retrieval benchmark's fake gateway also serves its real scheduler."""
import importlib.util
from pathlib import Path

from conftest import batch, msg


def test_benchmark_gateway_completes_learning_without_mutating_memories(store):
    path = Path(__file__).resolve().parents[1] / 'evals/benchmark_retrieval.py'
    spec = importlib.util.spec_from_file_location('retrieval_benchmark', path)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    msg(store, 1, '今晚整理天文观測记录，继续聊聊观星。')
    formed, result = batch(store, benchmark.Gateway(2048), count=1)
    assert result['created'] == []
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
        assert conn.execute('SELECT state FROM batches WHERE id=?', (formed.id,)).fetchone()[0] == 'succeeded'
        assert conn.execute('SELECT learning_state FROM messages').fetchone()[0] == 'learned'
