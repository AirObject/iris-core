import json

from iris.cli import main
from iris.db import Store


def test_cli_ingest_utf8_jsonl_and_setup_without_model_config(tmp_path):
    path = tmp_path / "iris.db"
    messages = tmp_path / "messages.jsonl"
    messages.write_text(json.dumps({"entry_id": "group-a", "platform": "fictional", "sender": "小林",
        "content": "我喜欢桂花乌龙", "occurred_at": "2026-09-28T09:00:00+08:00", "dedupe_key": "one"},
        ensure_ascii=False) + "\n", encoding="utf-8")
    assert main(["--db", str(path), "setup", "--name", "Iris"]) == 0
    assert main(["--db", str(path), "ingest", str(messages)]) == 0
    store = Store(path)
    try:
        with store.read() as conn:
            assert conn.execute("SELECT content FROM messages").fetchone()[0] == "我喜欢桂花乌龙"
    finally:
        store.close()
