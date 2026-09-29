"""Small command line surface for the M1 learning PR."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .db import Store
from .evaluation import run_learning_eval
from .learning import LearningEngine, PROMPT_VERSION
from .memory_ops import setup_role
from .models import Gateway, ModelError, load_test_models
from .queue import add_message, form_batch, get_batch


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="iris")
    parser.add_argument("--db", default="data/iris.db", help="SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Create initial role and persona")
    setup.add_argument("--name", default="Iris")
    setup.add_argument("--background", default="")
    setup.add_argument("--timezone", default="Asia/Shanghai")
    serve = commands.add_parser("serve", help="Serve the local host API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    models = commands.add_parser("models", help="Model connection commands")
    models.add_argument("action", choices=["check"])
    ingest = commands.add_parser("ingest", help="Add UTF-8 JSONL messages")
    ingest.add_argument("file", type=Path)
    learn = commands.add_parser("learn", help="Process an entry's pending messages")
    learn.add_argument("entry_id")
    learn.add_argument("--force", action="store_true", help="Run a waiting batch now")
    evaluation = commands.add_parser("eval", help="Run a frozen evaluation")
    evaluation.add_argument("kind", choices=["learning", "recall"])
    evaluation.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    evaluation.add_argument("--corpus", type=Path, help="UTF-8 JSONL corpus, including files outside the repository")
    evaluation.add_argument("--out", type=Path, help="Report output directory (default: evals/reports)")
    evaluation.add_argument("--judge-runs", type=int, choices=[1, 2], default=2)
    evaluation.add_argument("--calibrate", action="store_true", help="Calibrate recall thresholds on dev only")
    args = parser.parse_args(argv)
    if args.command == "serve":
        from .api import create_app, loopback_host
        import uvicorn
        try:
            host = loopback_host(args.host)
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be 1..65535")
            try:
                configs = load_test_models()
            except FileNotFoundError:
                configs = None
            uvicorn.run(create_app(args.db, configs=configs), host=host, port=args.port, workers=1)
            return 0
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if args.command == "eval":
        try:
            if args.kind == "recall":
                from .recall_evaluation import run_recall_eval
                try:
                    configs = load_test_models()
                except FileNotFoundError:
                    configs = {}
                path, _ = run_recall_eval(configs, Path.cwd(), args.split, corpus=args.corpus, out=args.out, calibrate=args.calibrate)
                print(f"Report: {path}")
                return 0
            if args.calibrate:
                raise ValueError("--calibrate is only available for recall")
            path, report = run_learning_eval(load_test_models(), Path.cwd(), args.split, corpus=args.corpus, out=args.out, judge_runs=args.judge_runs)
            print(f"Report: {path}")
            for split, metrics in report["metrics"].items():
                print(f"{split}: parse={metrics['parse_total']}, precision={metrics['precision']}, recall={metrics['fact_recall']}")
            return 0
        except (OSError, ValueError, ModelError, RuntimeError) as error:
            print(f"Evaluation failed: {error}", file=sys.stderr)
            return 1
    store = Store(args.db)
    try:
        if args.command == "setup":
            print(setup_role(store, args.name, args.background, args.timezone))
            return 0
        if args.command == "ingest":
            count = 0
            for line in args.file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                add_message(store, entry_id=item["entry_id"], entry_name=item.get("entry_name", item["entry_id"]),
                            platform=item["platform"], entry_kind=item.get("entry_kind", "group"),
                            kind=item.get("kind", "message"), sender=item["sender"], content=item["content"],
                            occurred_at=item["occurred_at"], dedupe_key=item["dedupe_key"],
                            account_id=item.get("account_id"), scene_identity=item.get("scene_identity"),
                            quote_author=item.get("quote_author"),
                            quote_author_account_id=item.get("quote_author_account_id"),
                            quote_content=item.get("quote_content"),
                            pace=item.get("pace", "standard"))
                count += 1
            print(f"accepted {count} message rows")
            return 0
        if args.command == "learn":
            store.recover_inflight()
        configs = load_test_models()
        gateway = Gateway(configs, store)
        try:
            if args.command == "models":
                results = []
                for kind in ("chat", "embedding"):
                    started = time.monotonic()
                    try:
                        if kind == "chat":
                            gateway.json_chat([{"role": "user", "content": "只输出 JSON：{\"ok\":true}"}], "connection_check", 3000)
                        else:
                            gateway.embedding("Iris 测试连接", "connection_check")
                        print(f"{kind}: success ({configs[kind].model}, {round((time.monotonic()-started)*1000)} ms)")
                        results.append(True)
                    except ModelError as error:
                        print(f"{kind}: {error.category} ({error.summary})")
                        results.append(False)
                return 0 if all(results) else 1
            if args.command == "learn":
                engine = LearningEngine(store, gateway)
                while True:
                    with store.read() as conn:
                        active = conn.execute("SELECT id FROM batches WHERE entry_id=? AND state='waiting' ORDER BY id LIMIT 1", (args.entry_id,)).fetchone()
                    batch = get_batch(store, active[0]) if active else form_batch(store, args.entry_id, PROMPT_VERSION)
                    if batch is None:
                        break
                    result = engine.run_batch(batch.id, force=args.force)
                    print(f"batch {batch.id}: {result}")
                    if get_batch(store, batch.id).state == "waiting":
                        break
                return 0
        finally:
            gateway.close()
    finally:
        store.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
