"""Small command line surface for the M1 learning PR."""

from __future__ import annotations

import argparse
import json
import os
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
    parser.add_argument("--db", help="Explicit SQLite database path (legacy/development override)")
    parser.add_argument("--data-dir", type=Path, help="Data directory; overrides IRIS_DATA_DIR and iris.toml")
    parser.add_argument("--config", type=Path, help="Deployment iris.toml path")
    parser.set_defaults(host=None, port=None, learning_concurrency=None, models_config=None, no_open=False)
    commands = parser.add_subparsers(dest="command")
    setup = commands.add_parser("setup", help="Create initial role and persona")
    setup.add_argument("--name", default="Iris")
    setup.add_argument("--background", default="")
    setup.add_argument("--timezone", default="Asia/Shanghai")
    serve = commands.add_parser("serve", help="Serve the local host API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--models-config", type=Path, help="External read-only model TOML; overrides IRIS_TEST_MODELS")
    serve.add_argument("--no-open", action="store_true", help="Do not open a browser on first startup")
    serve.add_argument("--learning-concurrency", type=int, help="Concurrent learning batches, default 2 (1..32)")
    models = commands.add_parser("models", help="Model connection commands")
    models.add_argument("action", choices=["check", "import"])
    models.add_argument("--from", dest="import_path", type=Path, help="Import model TOML; defaults to IRIS_TEST_MODELS")
    ingest = commands.add_parser("ingest", help="Add UTF-8 JSONL messages")
    ingest.add_argument("file", type=Path)
    learn = commands.add_parser("learn", help="Process an entry's pending messages")
    learn.add_argument("entry_id")
    learn.add_argument("--force", action="store_true", help="Run a waiting batch now")
    evaluation = commands.add_parser("eval", help="Run a frozen evaluation")
    evaluation.add_argument("kind", choices=["learning", "recall", "learning-export", "learning-score", "e2e", "e2e-score"])
    evaluation.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    evaluation.add_argument("--corpus", type=Path, help="UTF-8 JSONL corpus, including files outside the repository")
    evaluation.add_argument("--out", type=Path, help="Report output directory (default: evals/reports)")
    evaluation.add_argument("--judge-runs", type=int, choices=[1, 2],
                            help="Model preview judgments per case (default: 2)")
    evaluation.add_argument("--judge-mode", choices=["model", "external"], default="model",
                            help="Model preview (default), or collect learning/E2E materials for external judging")
    evaluation.add_argument("--checkpoints", type=Path, help="Completed learning checkpoint signature directory")
    evaluation.add_argument("--checkpoint-report", type=Path, help="Original JSON report for checkpoints without metadata")
    evaluation.add_argument("--materials", type=Path, help="Exported judging material directory")
    evaluation.add_argument("--judgments", type=Path, action="append", help="One round's directory; repeat for two rounds")
    evaluation.add_argument("--wait-timeout", type=float, help="E2E natural learning drain deadline, seconds")
    evaluation.add_argument("--script", dest="script_ids", action="append", help="E2E script ID; repeat to select scripts")
    evaluation.add_argument("--judge-model", help="External executor model name (required for learning-score/e2e-score)")
    evaluation.add_argument("--calibrate", action="store_true", help="Calibrate recall thresholds on dev only")
    evaluation.add_argument("--compare-embeddings", action="store_true", help="Compare 1024/2048 dimensions and query prefix on dev only")
    args = parser.parse_args(argv)
    args.command = args.command or "serve"
    from .configuration import RuntimeConfig, deployment
    try:
        deploy = deployment(config=args.config, data_dir=args.data_dir, host=args.host, port=args.port)
        args.db = str(Path(args.db).resolve()) if args.db else str(deploy['data_dir'] / 'iris.db')
        data_dir = Path(args.db).parent
    except (ValueError, OSError):
        print("部署配置无效，请检查数据目录、监听地址和端口。", file=sys.stderr)
        return 1
    if args.command == "models" and args.action == "import":
        path = args.import_path or os.environ.get('IRIS_TEST_MODELS')
        if not path:
            print("请指定 IRIS_TEST_MODELS 或 models import --from。", file=sys.stderr)
            return 1
        try:
            configs = load_test_models(path)
            store = Store(args.db)
            try:
                RuntimeConfig(store).save({kind: config if config.base_url and config.model else None
                                          for kind, config in configs.items()}, actor='local_import')
            finally:
                store.close()
            print("已导入模型配置；密钥仅保存在数据目录的 secrets.json，不显示。")
            return 0
        except (ValueError, OSError):
            print("模型配置导入失败，请检查输入文件及数据目录权限。", file=sys.stderr)
            return 1
    if args.command == "serve":
        from .api import create_app, loopback_host
        import uvicorn
        try:
            host = loopback_host(deploy['host'])
            port = deploy['port']
            if not 1 <= port <= 65535:
                raise ValueError("port must be 1..65535")
            path = args.models_config or os.environ.get('IRIS_TEST_MODELS')
            configs = load_test_models(path) if path else None
            if args.learning_concurrency is not None and not 1 <= args.learning_concurrency <= 32:
                raise ValueError("learning concurrency must be 1..32")
            from .runtime_logging import configure_logging
            configure_logging(data_dir, configs or {})
            loader = (lambda: load_test_models(path)) if path else None
            # External evaluation subprocesses do not open a browser.
            browser_url = None if args.no_open or path else f"http://{'['+host+']' if ':' in host else host}:{port}"
            print(f"Iris 已启动： http://{'['+host+']' if ':' in host else host}:{port}")
            uvicorn.run(create_app(args.db, configs=configs, config_loader=loader,
                                  learning_concurrency=args.learning_concurrency, open_browser_url=browser_url),
                        host=host, port=port, workers=1, proxy_headers=False)
            return 0
        except (ValueError, OSError):
            print("服务配置无效：请检查回环监听地址、端口、并发和模型文件。", file=sys.stderr)
            return 1
    if args.command == "eval":
        try:
            for option, kinds in (("checkpoints", ("learning-export",)),
                                  ("checkpoint_report", ("learning-export",)),
                                  ("materials", ("learning-score", "e2e-score")),
                                  ("judgments", ("learning-score", "e2e-score")),
                                  ("judge_model", ("learning-score", "e2e-score")),
                                  ("corpus", ("learning", "recall", "e2e")),
                                  ("judge_runs", ("learning", "e2e")),
                                  ("script_ids", ("e2e",)), ("wait_timeout", ("e2e",))):
                if getattr(args, option) is not None and args.kind not in kinds:
                    raise ValueError(f"--{option.replace('_', '-')} is only available for {', '.join(kinds)}")
            if args.kind not in ("learning", "e2e") and args.judge_mode != "model":
                raise ValueError("--judge-mode is only available for learning/e2e")
            if (args.calibrate or args.compare_embeddings) and args.kind != "recall":
                raise ValueError("--calibrate/--compare-embeddings are only available for recall")
            if args.kind in ("learning-export", "learning-score", "e2e-score") and args.split != "all":
                raise ValueError("offline export/scoring uses every case in the supplied run; --split is unavailable")
            if args.judge_mode == "external" and args.judge_runs is not None:
                raise ValueError("--judge-runs is for model preview; external rounds are supplied to learning-score/e2e-score")
            if args.kind == "learning-export":
                if args.checkpoints is None or args.out is None:
                    raise ValueError("learning-export requires --checkpoints and --out")
                path, _ = export_learning_judgments(args.checkpoints, args.out, checkpoint_report=args.checkpoint_report)
                print(f"Judging materials: {path}")
                return 0
            if args.kind == "e2e-score":
                from .e2e_evaluation import score_e2e_judgments
                if args.materials is None or not args.judgments or not args.judge_model:
                    raise ValueError("e2e-score requires --materials, --judgments and --judge-model")
                path, _ = score_e2e_judgments(args.materials, args.judgments, Path.cwd(),
                                             judge_model=args.judge_model, out=args.out)
                print(f"Report: {path}")
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
                path, _ = run_recall_eval(configs, Path.cwd(), args.split, corpus=args.corpus, out=args.out,
                                         calibrate=args.calibrate, compare_embeddings=args.compare_embeddings)
                print(f"Report: {path}")
                return 0
            if args.kind == "e2e":
                from .e2e_evaluation import run_e2e_eval
                path, report = run_e2e_eval(load_test_models(), Path.cwd(), args.split, corpus=args.corpus,
                                          out=args.out, judge_runs=args.judge_runs or 2,
                                          wait_timeout=args.wait_timeout if args.wait_timeout is not None else 900,
                                          judge_mode=args.judge_mode, script_ids=args.script_ids)
                if args.judge_mode == "external":
                    print(f"Judging materials: {path}")
                    return 0
                print(f"Report: {path}")
                print(f"E2E: {report['passed']}/{report['total']}; judge runs={report['judge_runs']}")
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
    from .process_lock import StoreLease
    lease = StoreLease(args.db)
    try:
        lease.__enter__()
        store = Store(args.db)
    except (ValueError, OSError) as error:
        lease.__exit__()
        print(str(error), file=sys.stderr)
        return 1
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
                    if kind == "chat":
                        effort = configs[kind].reasoning_effort
                        print(f"chat: reasoning_effort={effort if effort is not None else '未配置（不发送）'}")
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
        lease.__exit__()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
