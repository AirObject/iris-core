import sqlite3

from iris.db import Store
from iris.queue import add_message


def test_migration_backs_up_existing_database_before_change(tmp_path):
    path = tmp_path / "iris.db"
    sqlite3.connect(path).close()
    store = Store(path)
    store.close()
    backups = list(tmp_path.glob("iris.db.*.bak"))
    assert len(backups) == 1
    assert path.exists()


def test_message_dedupe_key_is_per_entry_and_preserves_original(store):
    common = dict(entry_name="entry", platform="test", entry_kind="group", kind="message",
                  sender="小林", account_id="account", occurred_at="2026-09-28T09:00:00+08:00",
                  dedupe_key="host-key")
    first = add_message(store, entry_id="A", content="原文", **common)
    again = add_message(store, entry_id="A", content="更改的正文", **common)
    other = add_message(store, entry_id="B", content="另一个入口", **common)
    assert first == again and other != first
    with store.read() as conn:
        assert conn.execute("SELECT content FROM messages WHERE id=?", (first,)).fetchone()[0] == "原文"
