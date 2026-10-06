"""Small command line surface for the M1 learning PR."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .db import Store
from .evaluation import export_learning_judgments, run_learning_eval, score_learning_judgments
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
    evaluation.add_argument("kind", choices=["learning", "recall", "learning-export", "learning-score"])
    evaluation.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    evaluation.add_argument("--corpus", type=Path, help="UTF-8 JSONL corpus, including files outside the repository")
    evaluation.add_argument("--out", type=Path, help="Report output directory (default: evals/reports)")
    evaluation.add_argument("--judge-runs", type=int, choices=[1, 2],
                            help="Model preview judgments per case (default: 2)")
    evaluation.add_argument("--judge-mode", choices=["model", "external"], default="model",
                            help="Model preview (default), or learning only with external judging materials")
    evaluation.add_argument("--checkpoints", type=Path, help="Completed learning checkpoint signature directory")
    evaluation.add_argument("--checkpoint-report", type=Path, help="Original JSON report for checkpoints without metadata")
    evaluation.add_argument("--materials", type=Path, help="Exported judging material directory")
    evaluation.add_argument("--judgments", type=Path, action="append", help="One round's directory; repeat for two rounds")
    evaluation.add_argument("--judge-model", help="External executor model name (required for learning-score)")
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
            for option, kinds in (("checkpoints", ("learning-export",)),
                                  ("checkpoint_report", ("learning-export",)),
                                  ("materials", ("learning-score",)),
                                  ("judgments", ("learning-score",)),
                                  ("judge_model", ("learning-score",)),
                                  ("corpus", ("learning", "recall")),
                                  ("judge_runs", ("learning",))):
                if getattr(args, option) is not None and args.kind not in kinds:
                    raise ValueError(f"--{option.replace('_', '-')} is only available for {', '.join(kinds)}")
            if args.kind != "learning" and args.judge_mode != "model":
                raise ValueError("--judge-mode is only available for learning")
            if args.calibrate and args.kind != "recall":
                raise ValueError("--calibrate is only available for recall")
            if args.kind in ("learning-export", "learning-score") and args.split != "all":
                raise ValueError("offline export/scoring uses every case in the supplied run; --split is unavailable")
            if args.judge_mode == "external" and args.judge_runs is not None:
                raise ValueError("--judge-runs is for model preview; external rounds are supplied to learning-score")
            if args.kind == "learning-export":
                if args.checkpoints is None or args.out is None:
                    raise ValueError("learning-export requires --checkpoints and --out")
                path, _ = export_learning_judgments(args.checkpoints, args.out, checkpoint_report=args.checkpoint_report)
                print(f"Judging materials: {path}")
                return 0
            if args.kind == "learning-score":
                if args.materials is None or not args.judgments or not args.judge_model:
                    raise ValueError("learning-score requires --materials, --judgments and --judge-model")
                path, report = score_learning_judgments(args.materials, args.judgments, Path.cwd(),
                                                        judge_model=args.judge_model, out=args.out)
                print(f"Report: {path}")
                return 0
            if args.kind == "recall":
                from .recall_evaluation import run_recall_eval
                try:
                    configs = load_test_models()
                except FileNotFoundError:
                    configs = {}
                path, _ = run_recall_eval(configs, Path.cwd(), args.split, corpus=args.corpus, out=args.out, calibrate=args.calibrate)
                print(f"Report: {path}")
                return 0
            options = {"judge_mode": args.judge_mode} if args.judge_mode != "model" else {}
            path, report = run_learning_eval(load_test_models(), Path.cwd(), args.split, corpus=args.corpus,
                                             out=args.out, judge_runs=args.judge_runs or 2, **options)
            if args.judge_mode == "external":
                print(f"Judging materials: {path}")
                return 0
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
