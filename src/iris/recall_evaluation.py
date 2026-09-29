"""Fixed-memory recall evaluation; provider embeddings cached by model, endpoint and text."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
import time
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
        data = {"memories": [], "queries": []}
        for row in records:
            if "memories" in row:
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
            sid = "person:" + name
            conn.execute("INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (sid, name, stamp))
            return sid
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
        return hashlib.sha256((config.base_url + "\0" + config.model + "\0" + text).encode("utf-8")).hexdigest()
    for text in dict.fromkeys(texts):
        row = connection.execute("SELECT vector FROM embeddings WHERE key=?", (key(text),)).fetchone()
        if row:
            vectors[text] = np.frombuffer(row[0], dtype=np.float32).tolist()
        else:
            missing.append(text)
    gateway = Gateway(configs, store)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(gateway.embedding, text, "recall_eval_embedding"): text for text in missing}
            for completed, future in enumerate(as_completed(futures), 1):
                text = futures[future]
                vector = np.asarray(future.result(), dtype=np.float32)
                if vector.ndim != 1 or not vector.size or not np.all(np.isfinite(vector)) or not np.linalg.norm(vector):
                    raise ValueError("invalid evaluation embedding")
                vectors[text] = vector.tolist()
                connection.execute("INSERT OR REPLACE INTO embeddings VALUES(?,?)", (key(text), vector.tobytes()))
                connection.commit()
                if completed % 10 == 0 or completed == len(missing):
                    print(f"embedding cache: {completed}/{len(missing)} new", flush=True)
    finally:
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
        row = {"id": q["id"], "mode": q.get("mode", "prepare"), "relevant": q["relevant"],
               "returned": [reverse[m["id"]] for m in response["memories"]], "local_ms": local_ms}
        if details:
            row["query"] = q
            row["response"] = response
        rows.append(row)
    return rows


def _selection_score(metric):
    recall, ndcg, error = metric["recall_at_8"] or 0, metric["ndcg_at_8"] or 0, metric["irrelevant_return_rate"] or 0
    return ((recall + ndcg) / 2 - error, recall, ndcg, -error)


def run_recall_eval(configs, root: Path, split="all", *, corpus: Path | None = None, out: Path | None = None,
                    calibrate=False) -> tuple[Path, dict]:
    root = root.resolve()
    data = load_corpus(corpus.resolve() if corpus else root / "evals/recall_v1.json")
    if split not in ("all", "dev", "holdout"):
        raise ValueError("invalid split")
    queries = [q for q in data["queries"] if split == "all" or q["split"] == split]
    if not queries:
        raise ValueError("no recall queries for requested split")
    if calibrate and (split != "dev" or any(q["split"] != "dev" for q in queries)):
        raise ValueError("calibration only supports --split dev")
    reports = out.resolve() if out else root / "evals/reports"
    reports.mkdir(parents=True, exist_ok=True)
    external = not reports.is_relative_to(root)
    cache_path = (reports if external else root / "data") / "recall-embeddings.db"
    started = time.perf_counter()
    variants, details, trials = {}, {}, []
    config = configs.get("embedding")
    vector_enabled = bool(config and config.model and config.base_url)
    with tempfile.TemporaryDirectory(prefix="iris-recall-") as folder:
        store = Store(Path(folder) / "iris.db")
        try:
            ids, entries = seed_corpus(store, data)
            gateway, misses, dimension = None, 0, None
            if vector_enabled:
                texts = [m["content"] for m in data["memories"]] + [q.get("text", "") for q in queries if q.get("text")]
                gateway, misses = cache_embeddings(configs, texts, store, cache_path)
                dimension = len(gateway.vectors[texts[0]])
                with store.write() as conn:
                    for m in data["memories"]:
                        conn.execute("UPDATE memories SET embedding=?,embedding_model=? WHERE id=?",
                            (np.asarray(gateway.vectors[m["content"]], dtype=np.float32).tobytes(), config.model, ids[m["id"]]))
            for tokenizer in ("jieba", "trigram"):
                for use_vector in (False, True) if vector_enabled else (False,):
                    name = tokenizer + ("_hybrid" if use_vector else "_fts")
                    best = None
                    for lexical_min in ([.25, .5, .75, 1.] if calibrate else [DEFAULTS["lexical_min"]]):
                        for vector_min in ([.35, .45, .55, .65, .75, .85] if calibrate and use_vector else [DEFAULTS["vector_min"]]):
                            retrieval = Retrieval(store, gateway if use_vector else None, tokenizer=tokenizer,
                                                  vector_min=vector_min, lexical_min=lexical_min,
                                                  clock=lambda: datetime.fromisoformat(data.get("as_of", "2026-09-29T12:00:00+00:00")))
                            rows = _run_queries(retrieval, queries, ids, entries, details=external)
                            metrics = recall_metrics(rows)
                            params = {"tokenizer": tokenizer, "vector_min": vector_min, "lexical_min": lexical_min}
                            if calibrate:
                                trials.append({"variant": name, "settings": params, "metrics": metrics})
                            if best is None or _selection_score(metrics) > _selection_score(best["metrics"]):
                                best = {"settings": params, "metrics": metrics, "rows": rows}
                    variants[name] = {"settings": best["settings"], "metrics": best["metrics"],
                                      "queries": [{k: r[k] for k in ("id", "returned", "local_ms")} for r in best["rows"]]}
                    details[name] = best["rows"]
                    print(f"{name}: Recall@8={best['metrics']['recall_at_8']}, nDCG@8={best['metrics']['ndcg_at_8']}", flush=True)
            with store.read() as conn:
                usage = [dict(r) for r in conn.execute("SELECT purpose,model,result_category,duration_ms,prompt_tokens,completion_tokens FROM model_calls")]
        finally:
            store.close()
    recommended = max(variants, key=lambda name: _selection_score(variants[name]["metrics"]))
    report = {"created_at": now(), "split": split, "elapsed_seconds": time.perf_counter() - started,
              "corpus": {"memories": len(data["memories"]), "queries": len(queries),
                         "sha256": hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()},
              "embedding_model": config.model if vector_enabled else "unconfigured", "dimension": dimension,
              "embedding_cache_misses": misses, "embedding_calls": usage, "variants": variants,
              "calibration": {"enabled": calibrate, "trials": trials,
                  "selection_rule": "最大化 (Recall@8+nDCG@8)/2-无关误返率；相同分数依次按召回、nDCG、较低误返率。仅 dev；M2 门槛只作参考，不作为本 PR 的参数筛选硬约束。",
                  "recommended_variant": recommended, "recommended_settings": variants[recommended]["settings"]},
              "m2_reference": {"recall_at_8": .85, "ndcg_at_8": .75, "irrelevant_return_rate": .10}}
    if external:
        report["details"] = {"memories": data["memories"], "queries": queries, "variants": details}
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    for config in configs.values():
        if config.api_key:
            serialized = serialized.replace(config.api_key, "[REDACTED]")
    report = json.loads(serialized)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = reports / f"recall-{stamp}-{split}.md"
    lines = ["# Iris 召回评测", "", f"固定记忆 {len(data['memories'])} 条，查询 {len(queries)} 条；直接入库，没有学习或生成调用。",
        f"embedding：{report['embedding_model']}，维度 {dimension}；新请求 {misses} 次，其余使用按端点／模型／文本哈希缓存的真实向量。",
        "本地 P95 包含生产回复准备与召回记录写入，扣除 embedding 调用耗时。首次向量获取用量与耗时单列在 JSON；并非网络 SLA 测试。",
        "", "| 方案 | Recall@8 | nDCG@8 | 无关误返率 | prepare P95 ms | 文本阈值 | 向量阈值 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, value in variants.items():
        m, p = value["metrics"], value["settings"]
        fmt = lambda n: "—" if n is None else f"{n:.3f}"
        lines.append(f"| {name} | {fmt(m['recall_at_8'])} | {fmt(m['ndcg_at_8'])} | {fmt(m['irrelevant_return_rate'])} | {fmt(m['prepare_p95_ms'])} | {p['lexical_min']} | {p['vector_min']} |")
    lines.extend(["", "M2 参考：Recall@8 ≥0.85，nDCG@8 ≥0.75，无关误返率 ≤0.10；本 PR 不以这三项作达标门槛。",
        "Recall 与 nDCG 对有答案查询取宏平均；nDCG 使用 2^相关等级−1 和 log2 排名折损。无关误返率只以无答案查询为分母。",
        f"本次{'进行了 dev 标定' if calibrate else '使用已保存的默认阈值进行对照'}；推荐：{recommended}。",
        report["calibration"]["selection_rule"],
        "评测命令不修改运行中的数据库设置。完整阈值试验在 JSON 中；外部 --out 另外保存每条固定记忆、查询与完整返回。",
        "学习和召回的最终验收仍需规划者运行隐藏集。", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    path.with_suffix(".json").write_text(serialized, encoding="utf-8")
    return path, report
