"""Fixed-memory recall evaluation; provider embeddings cached by model, endpoint and text."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
import time
from dataclasses import replace
from decimal import Decimal
from contextlib import ExitStack
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
    returned_counts, relevant_correct, relevant_count = [], 0, 0
    for row in rows:
        relevant, returned = row["relevant"], row["returned"][:8]
        returned_counts.append(len(returned))
        selected_relevant = [mid for mid in returned if row.get('reasons', {}).get(mid, 'relevant') == 'relevant']
        relevant_correct += sum(mid in relevant for mid in selected_relevant)
        relevant_count += len(selected_relevant)
        if relevant:
            recalls.append(len(set(returned) & set(relevant)) / len(relevant))
            dcg = sum((2 ** relevant.get(mid, 0) - 1) / math.log2(rank + 2) for rank, mid in enumerate(returned))
            ideal = sum((2 ** grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(sorted(relevant.values(), reverse=True)[:8]))
            ndcgs.append(dcg / ideal)
        else:
            false_returns.append(bool(selected_relevant))
        if row.get("mode", "prepare") == "prepare":
            latency.append(row["local_ms"])
    mean = lambda values: float(np.mean(values)) if values else None
    return {"queries": len(rows), "answerable": len(recalls), "no_answer": len(false_returns),
            "recall_at_8": mean(recalls), "ndcg_at_8": mean(ndcgs), "irrelevant_return_rate": mean(false_returns),
            "average_returned": mean(returned_counts),
            "relevant_precision": relevant_correct / relevant_count if relevant_count else None,
            "relevant_returned": relevant_count, "relevant_correct": relevant_correct,
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
        if query.get('mode') == 'search' and query.get('text', '') is None:
            raise ValueError('null text is only supported by prepare')
        if query.get('text', '') is None and query.get('participants', []) is None:
            query['categories'] = list(dict.fromkeys([*query.get('categories', []), '对话中准备']))
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
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'eval',?)", (entry_id, entry_id, query.get('entry_kind', 'group')))
        for index, message in enumerate(query.get("recent_messages", [])):
            if message["id"] in messages:
                raise ValueError("recent message IDs must be globally unique")
            messages[message["id"]] = add_message(store, entry_id=entry_id, entry_name=entry_id, platform="eval",
                entry_kind=query.get('entry_kind', 'group'), kind=message.get("kind", "message"), sender=message.get("speaker", "访客"),
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
               "reasons": {reverse[m['id']]: m['reason'] for m in response['memories']},
               "returned": [reverse[m["id"]] for m in response["memories"]], "local_ms": local_ms}
        if details:
            row["query"] = q
            row["response"] = response
        rows.append(row)
    return rows


def select_trial(trials):
    """Choose against the global best quality, avoiding non-transitive tie chains."""
    trials = list(trials)
    def quality(trial):
        m = trial['metrics']
        return (Decimal(str(m['recall_at_8'] or 0)) + Decimal(str(m['ndcg_at_8'] or 0))) / 2
    best = max(map(quality, trials))
    tied = [trial for trial in trials if best - quality(trial) < Decimal('0.01')]
    # Missing precision (no relevant returns) is zero, never perfect precision.
    # Fully equal trials retain fixed grid order after the higher quality score.
    return max(tied, key=lambda trial: (trial['metrics']['relevant_precision'] or 0,
        -(trial['metrics']['irrelevant_return_rate'] or 0), quality(trial)))


def grouped_metrics(rows):
    result = {}
    for split in dict.fromkeys(row['split'] for row in rows):
        subset = [row for row in rows if row['split'] == split]
        result[split] = {'all': recall_metrics(subset), 'categories': {
            category: recall_metrics([row for row in subset if category in row['categories']])
            for category in sorted({c for row in subset for c in row['categories']})}}
    return result


PUBLIC_CORPORA = ('recall_v1.json', 'recall_v2.json', 'recall_conversation_v1.json')


def run_recall_eval(configs, root: Path, split='all', *, corpus: Path | None = None, out: Path | None = None,
                    calibrate=False, compare_embeddings=False) -> tuple[Path, dict]:
    root = root.resolve()
    public_paths = [(root / 'evals' / name).resolve() for name in PUBLIC_CORPORA]
    paths = [corpus.resolve()] if corpus else public_paths
    datasets = []
    for path in paths:
        data = load_corpus(path)
        # Respect frozen splits: the analysed v2 holdout is report-only in
        # round three, not an input to parameter selection or independent proof.
        datasets.append((path.stem, data))
    if split not in ('all', 'dev', 'holdout'):
        raise ValueError('invalid split')
    if (calibrate or compare_embeddings) and split != 'dev':
        raise ValueError('calibration and embedding comparison only support --split dev')
    selected = {name: [q for q in data['queries'] if split == 'all' or q['split'] == split] for name, data in datasets}
    count = sum(len(queries) for queries in selected.values())
    if not count:
        raise ValueError('no recall queries for requested split')
    reports = out.resolve() if out else root / 'evals/reports'
    reports.mkdir(parents=True, exist_ok=True)
    external = not reports.is_relative_to(root)
    cache_path = (reports if external else root / 'data') / 'recall-embeddings.db'
    started = time.perf_counter()
    variants, details, trials, usage = {}, {}, [], []
    evaluated = {}
    config = configs.get('embedding')
    vector_enabled = bool(config and config.model and config.base_url)
    dimensions = [1024, 2048] if compare_embeddings else [config.dimensions or DEFAULTS['embedding_dimensions']] if config else []
    prefixes = ['', '为这个问题检索能回答它的个人记忆：'] if calibrate or compare_embeddings else [DEFAULTS['query_prefix']]
    misses = 0

    def evaluate(contexts, gateway, tokenizer, prefix, dimension, name):
        choices = []
        grid = product([.35, .45, .55, .65], [.75, .85, .95], [.5, 1., 2.]) if calibrate and gateway else [
            (DEFAULTS['vector_min'], DEFAULTS['vector_relative'], DEFAULTS['vector_weight'])]
        retrievers = [(context, Retrieval(context['store'], gateway, tokenizer=tokenizer,
                      clock=context['clock'], query_prefix=prefix)) for context in contexts]
        for floor, relative, weight in grid:
            params = dict(tokenizer=tokenizer, vector_min=floor, vector_relative=relative,
                          vector_weight=weight, query_prefix=prefix)
            rows = []
            for context, retrieval in retrievers:
                retrieval.overrides.update(params)
                current = _run_queries(retrieval, context['queries'], context['ids'], context['entries'], details=external)
                rows.extend(dict(row, corpus=context['name']) for row in current)
            metrics = recall_metrics(rows)
            settings = {**params, 'embedding_dimensions': dimension, 'embedding_model': config.model if config else ''}
            if calibrate:
                trials.append({'variant': name, 'settings': settings, 'metrics': metrics})
            choices.append({'variant': name, 'settings': settings, 'metrics': metrics, 'rows': rows})
        evaluated[name] = choices
        best = select_trial(choices)
        variants[name] = {'settings': best['settings'], 'metrics': best['metrics'], 'groups': grouped_metrics(best['rows']),
            'corpora': {context['name']: grouped_metrics([r for r in best['rows'] if r['corpus'] == context['name']])
                        for context in contexts},
            'queries': [{k: r[k] for k in ('id', 'corpus', 'split', 'categories', 'relevant', 'returned', 'reasons', 'local_ms')}
                        for r in best['rows']],
            'index_bytes': sum(r.index.nbytes if r.index else 0 for _, r in retrievers)}
        details[name] = best['rows']
        m = best['metrics']
        print(f"{name}: Recall@8={m['recall_at_8']}, nDCG@8={m['ndcg_at_8']}, false={m['irrelevant_return_rate']}", flush=True)

    # Every corpus and dimension has its own database, so tuning on all dev
    # queries does not introduce unrelated memories from another corpus.
    for dimension in (dimensions if vector_enabled else [None]):
        with ExitStack() as stack:
            folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='ir-')))
            contexts = []
            for index, (name, data) in enumerate(datasets):
                if not selected[name]:
                    continue
                store = Store(folder / f'{index}.db')
                stack.callback(store.close)
                ids, entries = seed_corpus(store, data)
                contexts.append(dict(name=name, data=data, store=store, ids=ids, entries=entries, queries=selected[name],
                    clock=lambda stamp=data.get('as_of', '2026-09-29T12:00:00+00:00'): datetime.fromisoformat(stamp)))
            if dimension == (dimensions[0] if vector_enabled else None):
                for tokenizer in ('jieba', 'trigram'):
                    evaluate(contexts, None, tokenizer, '', None, tokenizer + '_fts')
            if vector_enabled:
                local_configs = {**configs, 'embedding': replace(config, dimensions=dimension)}
                texts = []
                for context in contexts:
                    store = context['store']
                    store.set_setting('retrieval', {**DEFAULTS, 'embedding_model': config.model, 'embedding_dimensions': dimension})
                    preparer = Retrieval(store)
                    texts.extend(m['content'] for m in context['data']['memories'])
                    for prefix in prefixes:
                        preparer.settings['query_prefix'] = prefix
                        for q in context['queries']:
                            if q.get('mode') == 'search':
                                text, kind = q.get('text', ''), None
                            else:
                                text, kind = preparer.prepare_query(context['entries'][q['id']], q.get('text', ''))
                            if text.strip():
                                texts.append(preparer.embedding_text(text, entry_kind=kind))
                gateway, new = cache_embeddings(local_configs, texts, contexts[0]['store'], cache_path)
                misses += new
                if any(len(v) != dimension for v in gateway.vectors.values()):
                    raise ValueError('provider did not return requested embedding dimensions')
                for context in contexts:
                    with context['store'].write() as conn:
                        for m in context['data']['memories']:
                            conn.execute('UPDATE memories SET embedding=?,embedding_model=? WHERE id=?',
                                (np.asarray(gateway.vectors[m['content']], dtype=np.float32).tobytes(), config.model, context['ids'][m['id']]))
                for prefix in prefixes:
                    for tokenizer in ('jieba', 'trigram'):
                        name = tokenizer + '_hybrid'
                        if calibrate or compare_embeddings:
                            name += f'_d{dimension}_' + ('instruction' if prefix else 'plain')
                        evaluate(contexts, gateway, tokenizer, prefix, dimension, name)
            for context in contexts:
                with context['store'].read() as conn:
                    usage.extend(dict(r) for r in conn.execute('SELECT purpose,model,result_category,duration_ms,prompt_tokens,completion_tokens FROM model_calls'))
    # Select from the FULL grid, not per-variant winners: a local 0.01 tie
    # band can differ from the global one. Keep the exact selected trial visible.
    chosen = select_trial(t for choices in evaluated.values() for t in choices) if calibrate else None
    recommended = chosen['variant'] if chosen else None
    if chosen:
        variant = variants[recommended]
        variant.update(settings=chosen['settings'], metrics=chosen['metrics'], groups=grouped_metrics(chosen['rows']),
            corpora={name: grouped_metrics([r for r in chosen['rows'] if r['corpus'] == name]) for name in variant['corpora']},
            queries=[{k: r[k] for k in ('id', 'corpus', 'split', 'categories', 'relevant', 'returned', 'reasons', 'local_ms')}
                     for r in chosen['rows']])
        details[recommended] = chosen['rows']
    sources = sorted(p for p in Path(__file__).parent.rglob('*') if p.suffix in ('.py', '.md', '.sql', '.json'))
    report = {'created_at': now(), 'split': split, 'elapsed_seconds': time.perf_counter() - started,
        'source_sha256': hashlib.sha256(b''.join(p.name.encode() + p.read_bytes() for p in sources)).hexdigest(),
        'corpus': {'memories': sum(len(data['memories']) for _, data in datasets), 'queries': count,
                  'datasets': [{'name': name, 'version': data.get('version'), 'memories': len(data['memories']),
                                'queries': len(selected[name]), 'sha256': hashlib.sha256(
                      json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()} for name, data in datasets]},
        'embedding_model': config.model if vector_enabled else 'unconfigured', 'dimensions': dimensions,
        'embedding_cache_misses': misses, 'embedding_calls': usage, 'variants': variants,
        'calibration': {'enabled': calibrate, 'compare_embeddings': compare_embeddings, 'trials': trials,
            'selection_rule': '(Recall@8+nDCG@8)/2 距全网格最高值不足 0.01 视为持平；取 relevant 标注精确率高者，再取无关误返率低者。其后质量高者优先，完全相同时保留固定网格顺序。仅 dev 选择，历史 holdout 不参与。',
            'recommended_variant': recommended, 'recommended_settings': variants[recommended]['settings'] if recommended else None},
        'm2_reference': {'recall_at_8': .85, 'ndcg_at_8': .75, 'irrelevant_return_rate': .10}}
    if external:
        report['details'] = {'memories': datasets[0][1]['memories'] if len(datasets) == 1 else {n: d['memories'] for n, d in datasets},
            'queries': [q for queries in selected.values() for q in queries], 'variants': details}
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    for model in configs.values():
        if model.api_key:
            serialized = serialized.replace(model.api_key, '[REDACTED]')
    report = json.loads(serialized)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = reports / f'recall-{stamp}-{split}.md'
    lines = ['# Iris 召回评测', '', f"固定记忆 {report['corpus']['memories']} 条，查询 {count} 条；各语料隔离入库，不调用生成模型。",
        f"embedding：{report['embedding_model']}，维度 {dimensions}；新请求 {misses} 次。缓存键含端点、模型、维度和实际输入文本。",
        'P95 为正式 prepare 路径，扣除查询 embedding 阶段；包含召回记录写入。', '',
        '| 方案 | 语料 | 分组 | 类别 | 查询数 | Recall@8 | nDCG@8 | 无关误返率 | 平均返回数 | relevant 标注精确率 | P95 ms |',
        '| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    fmt = lambda n: '—' if n is None else f'{n:.3f}'
    for name, variant in variants.items():
        for corpus_name, groups in [('全部', variant['groups']), *variant['corpora'].items()]:
            for group, values in groups.items():
                for category, m in [('全部', values['all']), *values['categories'].items()]:
                    lines.append(f"| {name} | {corpus_name} | {group} | {category} | {m['queries']} | {fmt(m['recall_at_8'])} | {fmt(m['ndcg_at_8'])} | {fmt(m['irrelevant_return_rate'])} | {fmt(m['average_returned'])} | {fmt(m['relevant_precision'])} | {fmt(m['prepare_p95_ms'])} |")
    lines.extend(['', 'M2 参考值：Recall@8 ≥0.85、nDCG@8 ≥0.75、无关误返率 ≤0.10。拒答留到 M2，本轮只报告。',
        '按冻结文件 split 使用 dev；v2 历史 holdout 仅作报告对照，不参与选择，也不作为未见验收。验收以规划者隐藏集为准。',
        '类别可重叠；Recall/nDCG 按所有返回（含人物要点）计算，只以有答案问题为分母。无关误返只检查无答案问题的 reason=relevant 返回。',
        '平均返回数只作诊断；relevant 标注精确率用于质量持平时比较，不设门槛；后者为标注相关的 relevant 返回数 / 全部 relevant 返回数，跨查询合并计数。空分母为 —。',
        report['calibration']['selection_rule'], f'本次推荐：{recommended or "未进行选择，使用已冻结参数"}。',
        '以下为各方案参数和索引块存储之和；参数网格、用量与逐条返回原因保存在同名 JSON。', ''])
    for name, variant in variants.items():
        lines.append(f"- {name}: {json.dumps(variant['settings'], ensure_ascii=False)}；块存储 {variant['index_bytes']/2**20:.3f} MiB。")
    if calibrate:
        lines.extend(['', '## 完整参数网格', '',
            'Q = (Recall@8+nDCG@8)/2；有前缀为“为这个问题检索能回答它的个人记忆：”。每行均在本报告全部 dev 上评测。', '',
            '| 方案 | 下限 | 相对比例 | 向量权重 | Recall@8 | nDCG@8 | Q | relevant 精确率 | 无关误返率 | 平均返回数 | 选定 |',
            '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |'])
        for trial in trials:
            p, m = trial['settings'], trial['metrics']
            chosen_row = trial['variant'] == recommended and p == report['calibration']['recommended_settings']
            quality = ((m['recall_at_8'] or 0) + (m['ndcg_at_8'] or 0)) / 2
            lines.append(f"| {trial['variant']} | {p['vector_min']} | {p['vector_relative']} | {p['vector_weight']} | {fmt(m['recall_at_8'])} | {fmt(m['ndcg_at_8'])} | {quality:.6f} | {fmt(m['relevant_precision'])} | {fmt(m['irrelevant_return_rate'])} | {fmt(m['average_returned'])} | {'是' if chosen_row else ''} |")
    lines.extend(['', '评测不会修改在用数据库。规划者将以隐藏召回集复核；本报告不能替代独立验收。', ''])
    path.write_text('\n'.join(lines), encoding='utf-8')
    path.with_suffix('.json').write_text(serialized, encoding='utf-8')
    return path, report
