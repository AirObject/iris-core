"""Fixed-memory recall evaluation; provider embeddings cached by model, endpoint and text."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
import time
from dataclasses import replace
from itertools import product
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .db import Store, now
from .memory_ops import setup_role
from .models import Gateway
from .queue import add_message
from .retrieval import DEFAULTS, Retrieval


def recall_metrics(rows: list[dict]) -> dict:
    recalls, ndcgs, false_returns, latency = [], [], [], []
    for row in rows:
        relevant, returned = row["relevant"], row["returned"][:8]
        if relevant:
            recalls.append(len(set(returned) & set(relevant)) / len(relevant))
            dcg = sum((2 ** relevant.get(mid, 0) - 1) / math.log2(rank + 2) for rank, mid in enumerate(returned))
            ideal = sum((2 ** grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(sorted(relevant.values(), reverse=True)[:8]))
            ndcgs.append(dcg / ideal)
        else:
            false_returns.append(bool(returned))
        if row.get("mode", "prepare") == "prepare":
            latency.append(row["local_ms"])
    mean = lambda values: float(np.mean(values)) if values else None
    return {"queries": len(rows), "answerable": len(recalls), "no_answer": len(false_returns),
            "recall_at_8": mean(recalls), "ndcg_at_8": mean(ndcgs), "irrelevant_return_rate": mean(false_returns),
            "prepare_p95_ms": float(np.percentile(latency, 95)) if latency else None}


def load_corpus(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        data = {"memories": [], "queries": [], "subjects": []}
        for row in records:
            if "memories" in row:
                data['subjects'].extend(row.get('subjects', []))
                data["memories"].extend(row["memories"])
                data["queries"].extend(row.get("queries", []))
            elif row.get("record_type") == "memory":
                data["memories"].append({k: v for k, v in row.items() if k != "record_type"})
            else:
                data["queries"].append({k: v for k, v in row.items() if k != "record_type"})
    memories, queries = data["memories"], data["queries"]
    ids = {m["id"] for m in memories}
    if len(ids) != len(memories) or len({q["id"] for q in queries}) != len(queries):
        raise ValueError("duplicate memory or query IDs")
    for query in queries:
        query.setdefault("split", "dev")
        if isinstance(query["relevant"], list):
            query["relevant"] = dict.fromkeys(query["relevant"], 1)
        if not set(query["relevant"]) <= ids or any(type(g) is not int or not 1 <= g <= 3 for g in query["relevant"].values()):
            raise ValueError("relevance must reference existing memories with grades 1..3")
        if set(query.get("known_memory_ids", [])) & set(query["relevant"]):
            raise ValueError("known-context memory must not count as required recall")
    return data


def seed_corpus(store: Store, data: dict) -> tuple[dict, dict]:
    """Direct fixed-memory insertion. No LearningEngine or generation calls."""
    setup_role(store, "Iris")
    messages, entries, ids = {}, {}, {}
    for query in data["queries"]:
        entry_id = "eval:" + query["id"]
        entries[query["id"]] = entry_id
        with store.write() as conn:
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'eval','group')", (entry_id, entry_id))
        for index, message in enumerate(query.get("recent_messages", [])):
            if message["id"] in messages:
                raise ValueError("recent message IDs must be globally unique")
            messages[message["id"]] = add_message(store, entry_id=entry_id, entry_name=entry_id, platform="eval",
                entry_kind="group", kind=message.get("kind", "message"), sender=message.get("speaker", "访客"),
                content=message["content"], occurred_at=message.get("occurred_at", "2026-09-29T12:00:00+00:00"), dedupe_key=str(index))
    stamp = data.get("as_of", "2026-09-29T12:00:00+00:00")
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('archive','固定记忆来源','eval','group')")
        def subject(name):
            if name in ("我", "self", "Iris"):
                return "self"
            existing = conn.execute('SELECT id FROM subjects WHERE name=?', (name,)).fetchall()
            if len(existing) == 1:
                return existing[0][0]
            sid = "person:" + name
            conn.execute("INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (sid, name, stamp))
            return sid
        for person in data.get('subjects', []):
            sid = subject(person['name'])
            for alias in person.get('aliases', []):
                conn.execute('INSERT OR IGNORE INTO subject_aliases(subject_id,alias) VALUES(?,?)', (sid, alias))
        for m in data["memories"]:
            mid = conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
                event_time,lifecycle,entry_id,world,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                VALUES(?,?,?,?,?,?,?,?,?,'archive',?,?,?,?,?)""", (m["content"], m.get("kind", "事实"), subject(m.get("speaker", "我")),
                m.get("stance", "亲历"), m.get("belief", 75), m.get("importance", 60), m.get("retention", 55), m.get("event_time"),
                m.get("lifecycle", "active"), m.get("world", "real"), stamp, stamp, stamp, stamp)).lastrowid
            ids[m["id"]] = mid
            for name in m.get("about", []):
                conn.execute("INSERT OR IGNORE INTO memory_subjects VALUES(?,?)", (mid, subject(name)))
            for tag in m.get("tags", []):
                conn.execute("INSERT OR IGNORE INTO memory_tags VALUES(?,?)", (mid, tag))
            for source in m.get("source_message_ids", []):
                if source not in messages:
                    raise ValueError("unknown source_message_id: " + str(source))
                conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)", (mid, messages[source], stamp))
    return ids, entries


class CachedEmbeddings:
    def __init__(self, configs, vectors):
        self.configs, self.vectors = configs, vectors

    def embedding(self, text, purpose="embedding"):
        return self.vectors[text]


def cache_embeddings(configs, texts: list[str], store: Store, cache_path: Path):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(cache_path)
    connection.execute("CREATE TABLE IF NOT EXISTS embeddings(key TEXT PRIMARY KEY,vector BLOB NOT NULL)")
    config = configs["embedding"]
    vectors, missing = {}, []
    def key(text):
        return hashlib.sha256((config.base_url + "\0" + config.model + "\0" + str(config.dimensions) + "\0" + text).encode("utf-8")).hexdigest()
    for text in dict.fromkeys(texts):
        row = connection.execute("SELECT vector FROM embeddings WHERE key=?", (key(text),)).fetchone()
        if row:
            vectors[text] = np.frombuffer(row[0], dtype=np.float32).tolist()
        else:
            missing.append(text)
    gateway = Gateway(configs, store)
    failures = []
    def request(text):
        time.sleep(1)
        return gateway.embedding(text, 'recall_eval_embedding')
    try:
        # Keep provider pressure modest; retries and every attempted call are
        # recorded, while completed vectors survive an interrupted dev run.
        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = {pool.submit(request, text): text for text in missing}
            for completed, future in enumerate(as_completed(futures), 1):
                text = futures[future]
                try:
                    vector = np.asarray(future.result(), dtype=np.float32)
                except Exception as error:
                    failures.append(error)
                    continue
                if vector.ndim != 1 or not vector.size or not np.all(np.isfinite(vector)) or not np.linalg.norm(vector):
                    raise ValueError("invalid evaluation embedding")
                vectors[text] = vector.tolist()
                connection.execute("INSERT OR REPLACE INTO embeddings VALUES(?,?)", (key(text), vector.tobytes()))
                connection.commit()
                if completed % 10 == 0 or completed == len(missing):
                    print(f"embedding cache: {completed}/{len(missing)} new", flush=True)
        if failures:
            raise failures[0]
    finally:
        connection.execute('CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, dimension INTEGER, data_json TEXT NOT NULL)')
        with store.read() as reader:
            for row in reader.execute('SELECT purpose,model,result_category,duration_ms,prompt_tokens,completion_tokens FROM model_calls'):
                connection.execute('INSERT INTO calls(dimension,data_json) VALUES(?,?)', (config.dimensions, json.dumps(dict(row))))
        connection.commit()
        gateway.close()
        connection.close()
    return CachedEmbeddings(configs, vectors), len(missing)


def _run_queries(retrieval, queries, ids, entries, *, details=False):
    reverse = {mid: key for key, mid in ids.items()}
    rows = []
    for q in queries:
        start = time.perf_counter()
        if q.get("mode") == "search":
            response = retrieval.search(text=q.get("text", ""), limit=8, **q.get("filters", {}))
        else:
            response = retrieval.prepare(entries[q["id"]], text=q.get("text", ""), participants=q.get("participants", []),
                known_memory_ids=[ids[key] for key in q.get("known_memory_ids", [])],
                recent_limit=q.get("recent_limit", 20))
        local_ms = max(0, (time.perf_counter() - start) * 1000 - retrieval.last_embedding_ms)
        row = {"id": q["id"], "split": q['split'], "categories": q.get('categories', []),
               "mode": q.get("mode", "prepare"), "relevant": q["relevant"],
               "returned": [reverse[m["id"]] for m in response["memories"]], "local_ms": local_ms}
        if details:
            row["query"] = q
            row["response"] = response
        rows.append(row)
    return rows


def _selection_score(metric):
    recall, ndcg, error = metric["recall_at_8"] or 0, metric["ndcg_at_8"] or 0, metric["irrelevant_return_rate"] or 0
    return ((recall + ndcg) / 2 - error, recall, ndcg, -error)


def grouped_metrics(rows):
    result = {}
    for split in dict.fromkeys(row['split'] for row in rows):
        subset = [row for row in rows if row['split'] == split]
        result[split] = {'all': recall_metrics(subset), 'categories': {
            category: recall_metrics([row for row in subset if category in row['categories']])
            for category in sorted({c for row in subset for c in row['categories']})}}
    return result


def run_recall_eval(configs, root: Path, split='all', *, corpus: Path | None = None, out: Path | None = None,
                    calibrate=False, compare_embeddings=False) -> tuple[Path, dict]:
    root = root.resolve()
    data = load_corpus(corpus.resolve() if corpus else root / 'evals/recall_v2.json')
    if split not in ('all', 'dev', 'holdout'):
        raise ValueError('invalid split')
    if (calibrate or compare_embeddings) and split != 'dev':
        raise ValueError('calibration and embedding comparison only support --split dev')
    queries = [q for q in data['queries'] if split == 'all' or q['split'] == split]
    if not queries:
        raise ValueError('no recall queries for requested split')
    reports = out.resolve() if out else root / 'evals/reports'
    reports.mkdir(parents=True, exist_ok=True)
    external = not reports.is_relative_to(root)
    cache_path = (reports if external else root / 'data') / 'recall-embeddings.db'
    started = time.perf_counter()
    variants, details, trials, usage = {}, {}, [], []
    config = configs.get('embedding')
    vector_enabled = bool(config and config.model and config.base_url)
    dimensions = [1024, 2048] if compare_embeddings else [config.dimensions or DEFAULTS['embedding_dimensions']] if config else []
    prefixes = ['', '为这个问题检索能回答它的个人记忆：'] if compare_embeddings else [DEFAULTS['query_prefix']]
    misses = 0
    clock = lambda: datetime.fromisoformat(data.get('as_of', '2026-09-29T12:00:00+00:00'))
    def evaluate(store, gateway, ids, entries, tokenizer, prefix, dimension, name):
        best = None
        grid = product([.35, .45, .55, .65], [.75, .85, .95], [.5, 1., 2.]) if calibrate and gateway else [
            (DEFAULTS['vector_min'], DEFAULTS['vector_relative'], DEFAULTS['vector_weight'])]
        retrieval = Retrieval(store, gateway, tokenizer=tokenizer, clock=clock, query_prefix=prefix)
        for floor, relative, weight in grid:
            params = dict(tokenizer=tokenizer, vector_min=floor, vector_relative=relative,
                          vector_weight=weight, query_prefix=prefix)
            retrieval.overrides.update(params)
            rows = _run_queries(retrieval, queries, ids, entries, details=external)
            metrics = recall_metrics(rows)
            settings = {**params, 'embedding_dimensions': dimension, 'embedding_model': config.model if config else ''}
            if calibrate:
                trials.append({'variant': name, 'settings': settings, 'metrics': metrics})
            if best is None or _selection_score(metrics) > _selection_score(best['metrics']):
                best = {'settings': settings, 'metrics': metrics, 'rows': rows}
        variants[name] = {'settings': best['settings'], 'metrics': best['metrics'], 'groups': grouped_metrics(best['rows']),
            'queries': [{k: r[k] for k in ('id', 'split', 'categories', 'relevant', 'returned', 'local_ms')} for r in best['rows']],
            'index_bytes': retrieval.index.nbytes if retrieval.index else 0}
        details[name] = best['rows']
        m = best['metrics']
        print(f"{name}: Recall@8={m['recall_at_8']}, nDCG@8={m['ndcg_at_8']}, false={m['irrelevant_return_rate']}", flush=True)

    # Dimension-specific databases avoid mixing vectors in an already loaded index.
    for dimension in (dimensions if vector_enabled else [None]):
        with tempfile.TemporaryDirectory(prefix='ir-') as folder:
            store = Store(Path(folder) / 'i.db')
            try:
                ids, entries = seed_corpus(store, data)
                if dimension == (dimensions[0] if vector_enabled else None):
                    for tokenizer in ('jieba', 'trigram'):
                        evaluate(store, None, ids, entries, tokenizer, '', None, tokenizer + '_fts')
                if vector_enabled:
                    local_configs = {**configs, 'embedding': replace(config, dimensions=dimension)}
                    store.set_setting('retrieval', {**DEFAULTS, 'embedding_model': config.model, 'embedding_dimensions': dimension})
                    preparer = Retrieval(store)
                    texts = [m['content'] for m in data['memories']]
                    for prefix in prefixes:
                        preparer.settings['query_prefix'] = prefix
                        for q in queries:
                            if q.get('text'):
                                texts.append(preparer.embedding_text(q['text'], q.get('participants', q.get('filters', {}).get('people', []))))
                    gateway, new = cache_embeddings(local_configs, texts, store, cache_path)
                    misses += new
                    if any(len(v) != dimension for v in gateway.vectors.values()):
                        raise ValueError('provider did not return requested embedding dimensions')
                    with store.write() as conn:
                        for m in data['memories']:
                            conn.execute('UPDATE memories SET embedding=?,embedding_model=? WHERE id=?',
                                (np.asarray(gateway.vectors[m['content']], dtype=np.float32).tobytes(), config.model, ids[m['id']]))
                    for prefix in prefixes:
                        for tokenizer in ('jieba', 'trigram'):
                            name = tokenizer + '_hybrid'
                            if compare_embeddings:
                                name += f'_d{dimension}_' + ('instruction' if prefix else 'plain')
                            evaluate(store, gateway, ids, entries, tokenizer, prefix, dimension, name)
                with store.read() as conn:
                    usage.extend(dict(r) for r in conn.execute('SELECT purpose,model,result_category,duration_ms,prompt_tokens,completion_tokens FROM model_calls'))
            finally:
                store.close()
    recommended = max(variants, key=lambda name: _selection_score(variants[name]['metrics'])) if calibrate else None
    sources = sorted(p for p in Path(__file__).parent.rglob('*') if p.suffix in ('.py', '.md', '.sql', '.json'))
    report = {'created_at': now(), 'split': split, 'elapsed_seconds': time.perf_counter() - started,
        'source_sha256': hashlib.sha256(b''.join(p.name.encode() + p.read_bytes() for p in sources)).hexdigest(),
        'corpus': {'version': data.get('version'), 'memories': len(data['memories']), 'queries': len(queries),
                   'sha256': hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()},
        'embedding_model': config.model if vector_enabled else 'unconfigured', 'dimensions': dimensions,
        'embedding_cache_misses': misses, 'embedding_calls': usage, 'variants': variants,
        'calibration': {'enabled': calibrate, 'compare_embeddings': compare_embeddings, 'trials': trials,
            'selection_rule': '(Recall@8+nDCG@8)/2-无关误返率；平分优先召回、nDCG、较低误返。仅 dev 选择，holdout 不排序挑方案。',
            'recommended_variant': recommended, 'recommended_settings': variants[recommended]['settings'] if recommended else None},
        'm2_reference': {'recall_at_8': .85, 'ndcg_at_8': .75, 'irrelevant_return_rate': .10}}
    if external:
        report['details'] = {'memories': data['memories'], 'queries': queries, 'variants': details}
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    for model in configs.values():
        if model.api_key:
            serialized = serialized.replace(model.api_key, '[REDACTED]')
    report = json.loads(serialized)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = reports / f'recall-{stamp}-{split}.md'
    lines = ['# Iris 召回评测', '', f"固定记忆 {len(data['memories'])} 条，查询 {len(queries)} 条；直接入库，不调用生成模型。",
        f"embedding：{report['embedding_model']}，维度 {dimensions}；新请求 {misses} 次。缓存键含端点、模型、维度和实际输入文本。",
        'P95 为正式 prepare 路径，扣除外部 embedding 网络等待；包含召回记录写入。', '',
        '| 方案 | 分组 | 类别 | 查询数 | Recall@8 | nDCG@8 | 无关误返率 | P95 ms |',
        '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |']
    fmt = lambda n: '—' if n is None else f'{n:.3f}'
    for name, variant in variants.items():
        for group, values in variant['groups'].items():
            for category, m in [('全部', values['all']), *values['categories'].items()]:
                lines.append(f"| {name} | {group} | {category} | {m['queries']} | {fmt(m['recall_at_8'])} | {fmt(m['ndcg_at_8'])} | {fmt(m['irrelevant_return_rate'])} | {fmt(m['prepare_p95_ms'])} |")
    lines.extend(['', 'M2 目标：Recall@8 ≥0.85、nDCG@8 ≥0.75、无关误返率 ≤0.10。未调整门槛。',
        '类别可重叠；Recall/nDCG 只以有答案问题为分母，无关误返率只以无答案问题为分母；空分母为 —。',
        report['calibration']['selection_rule'], f'本次推荐：{recommended or "未进行选择，使用已冻结参数"}。',
        '以下为各方案参数和索引块存储；参数网格、用量与逐条结果保存在同名 JSON。', ''])
    for name, variant in variants.items():
        lines.append(f"- {name}: {json.dumps(variant['settings'], ensure_ascii=False)}；块存储 {variant['index_bytes']/2**20:.3f} MiB。")
    lines.extend(['', '评测不会修改在用数据库。规划者将以隐藏召回集复核；本报告不能替代独立验收。', ''])
    path.write_text('\n'.join(lines), encoding='utf-8')
    path.with_suffix('.json').write_text(serialized, encoding='utf-8')
    return path, report
