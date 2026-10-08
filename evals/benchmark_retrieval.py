"""Reproducible Windows/native benchmark. Synthetic performance data is not a quality corpus.

uv run python evals/benchmark_retrieval.py --out evals/reports
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import psutil
from fastapi.testclient import TestClient

from iris.api import create_app
from iris.db import Store
from iris.models import ModelConfig
from iris.queue import add_message
from iris.retrieval import DEFAULTS

STAMP = "2026-09-29T12:00:00+00:00"


def build(path, size, dimension):
    if path.exists():
        return
    store = Store(path)
    rng = np.random.default_rng(20260929)
    try:
        with store.write() as conn:
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('bench','bench','perf','group')")
            for i in range(100):
                conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (f"p{i}", f"参与者{i}", STAMP))
        for base in range(0, size, 500):
            vectors = rng.normal(size=(min(500, size - base), dimension)).astype(np.float32)
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            with store.write() as conn:
                for offset, vector in enumerate(vectors):
                    i = base + offset
                    mid = conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,
                        retention,entry_id,embedding,embedding_model,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                        VALUES(?,'事实',?,'亲历',75,60,55,'bench',?,'perf-vector',?,?,?,?)""",
                        (f"参与者{i%100}的天文观測记录第{i}号，地点南山，计划继续观星。", f"p{i%100}", vector.tobytes(), STAMP, STAMP, STAMP, STAMP)).lastrowid
                    conn.execute("INSERT INTO memory_subjects VALUES(?,?)", (mid, f"p{i%100}"))
        store._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        store.close()


class Gateway:
    configs = {"embedding": ModelConfig("local-cached", "", "perf-vector")}

    def __init__(self, dimension):
        self.vector = np.random.default_rng(4321).normal(size=dimension).astype(np.float32)

    def embedding(self, text, purpose="embedding"):
        return self.vector

    def json_chat(self, messages, purpose, max_tokens=16000, *, batch_id=None):
        # TestClient starts the real scheduler. Synthetic pending messages may
        # become due, but must not change the fixed performance memory corpus.
        if purpose != "learning":
            raise ValueError("benchmark gateway only supports empty learning")
        output = {"memories": []}
        return output, json.dumps(output), None, "direct"


def worker(path, size, dtype, repeats, dimension, default_config=False):
    process = psutil.Process()
    load_before = psutil.getloadavg()
    store = Store(path)
    settings = {**DEFAULTS, "embedding_model": "perf-vector", "dtype": dtype, "embedding_dimensions": dimension}
    if not default_config:
        settings.update(vector_min=0.0, vector_relative=0.0)
    store.set_setting("retrieval", settings)
    if default_config:
        # Exercise null query / inferred participants on the normal prepare path.
        # These are synthetic performance messages, not quality annotations.
        with store.write() as conn:
            for i in range(3):
                conn.execute("INSERT OR IGNORE INTO platform_identities(subject_id,platform,account_id,display_name) VALUES(?,'perf',?,?)",
                             (f'p{i}', f'bench{i}', f'参与者{i}'))
        for i in range(3):
            add_message(store, entry_id='bench', entry_name='bench', platform='perf', entry_kind='group',
                        kind='message', sender=f'参与者{i}', account_id=f'bench{i}', content='今晚整理天文观測记录，继续聊聊观星。',
                        occurred_at=STAMP, dedupe_key=f'prepare-default-{i}')
    gateway = Gateway(dimension)
    rss_before = process.memory_info().rss
    start = time.perf_counter()
    with TestClient(create_app(store=store, gateway=gateway), base_url="http://127.0.0.1") as client:
        retrieval = client.app.state.retrieval
        load_seconds = time.perf_counter() - start
        rss_loaded = process.memory_info().rss
        queries = {
            'unnamed': {} if default_config else {'text': '天文观測记录', 'participants': []},
            'named': {'text': '参与者1的天文观測记录', **({} if default_config else {'participants': []})}}
        timings = {}
        for name, query in queries.items():
            direct, http, vector_times = [], [], []
            for _ in range(5):
                retrieval.prepare('bench', **query)
            for _ in range(repeats):
                started = time.perf_counter()
                retrieval.index.scores(gateway.vector)
                vector_times.append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                retrieval.prepare('bench', **query)
                direct.append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                response = client.post('/api/v1/entries/bench/prepare', json=query)
                response.raise_for_status()
                http.append((time.perf_counter() - started) * 1000)
            timings[name] = {'request': query, 'samples': repeats,
                'vector_p50_ms': float(np.percentile(vector_times, 50)),
                'vector_p95_ms': float(np.percentile(vector_times, 95)),
                'prepare_p50_ms': float(np.percentile(direct, 50)),
                'prepare_p95_ms': float(np.percentile(direct, 95)),
                'http_prepare_p50_ms': float(np.percentile(http, 50)),
                'http_prepare_p95_ms': float(np.percentile(http, 95)),
                'samples_ms': {'prepare': direct, 'http_prepare': http, 'vector': vector_times},
                'returned': [{'id': m['id'], 'reason': m['reason']} for m in response.json()['memories']]}
        # Identical deterministic queries across fresh float32 / float16 worker processes.
        rng = np.random.default_rng(7788)
        rankings = []
        for _ in range(20):
            scores = retrieval.index.scores(rng.normal(size=dimension).astype(np.float32))
            rankings.append([mid for mid, _ in sorted(scores.items(), key=lambda p: (-p[1][0], p[0]))[:8]])
        info = process.memory_info()
        result = {"memories": size, "dimension": dimension, "dtype": dtype, "settings": settings, "queries": timings,
                  "load_seconds": load_seconds, "load_average_before": load_before, "load_average_after": psutil.getloadavg(), "matrix_storage_mib": retrieval.index.nbytes / 2**20,
                  "rss_before_mib": rss_before / 2**20, "rss_after_load_mib": rss_loaded / 2**20,
                  "rss_load_delta_mib": (rss_loaded - rss_before) / 2**20,
                  "rss_after_queries_mib": info.rss / 2**20,
                  "peak_working_set_mib": getattr(info, "peak_wset", info.rss) / 2**20,
                  "top8": rankings}
    store.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("evals/reports"))
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--size", type=int, default=5000)
    parser.add_argument("--dtype", default="float32")
    parser.add_argument("--dimension", type=int, default=2048)
    parser.add_argument("--repeats", type=int, default=60)
    parser.add_argument("--default-config", action="store_true", help="Only 5k/50k using current default dimension, dtype and cutoffs")
    args = parser.parse_args()
    folder = Path("data/performance")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"memories-{args.size}-{args.dimension}.db"
    if args.worker:
        result = worker(path, args.size, args.dtype, args.repeats, args.dimension, args.default_config)
        (folder / f"result-{args.size}-{args.dimension}-{args.dtype}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return
    results = []
    for dimension in ([DEFAULTS['embedding_dimensions']] if args.default_config else (1024, 2048)):
        for size in (5000, 50000):
            build(folder / f"memories-{size}-{dimension}.db", size, dimension)
            for dtype in ([DEFAULTS['dtype']] if args.default_config else ("float32", "float16")):
                subprocess.run([sys.executable, "-X", "utf8", __file__, "--worker", "--size", str(size), "--dimension", str(dimension), "--dtype", dtype, "--repeats", str(args.repeats)] + (["--default-config"] if args.default_config else []), check=True)
                row = json.loads((folder / f"result-{size}-{dimension}-{dtype}.json").read_text(encoding="utf-8"))
                results.append(row)
                for name, timing in row['queries'].items():
                    print(f"{size} d{dimension} {dtype} {name}: prepare P95 {timing['prepare_p95_ms']:.1f} ms, HTTP P95 {timing['http_prepare_p95_ms']:.1f} ms", flush=True)
            rows = [r for r in results if r["memories"] == size and r['dimension'] == dimension]
            if len(rows) == 2:
                rows[1]["top8_overlap_with_float32"] = float(np.mean([len(set(a) & set(b)) / 8 for a,b in zip(rows[0]["top8"],rows[1]["top8"], strict=True)]))
    for row in results:
        row.pop("top8")
    report = {"platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "numpy": np.__version__, "logical_cpus": os.cpu_count(), "physical_memory_gib": psutil.virtual_memory().total / 2**30,
              "note": "Fresh process per size/dtype; precomputed synthetic query embedding; 5 warmups per query (unnamed and named); FTS, vector ranking, metadata, JSON and recall record included. HTTP uses in-process ASGI TestClient; no external embedding network.",
              "results": results}
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / ("retrieval-performance-pr5-r3.json" if args.default_config else "retrieval-performance-comparison-r3.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2),encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
