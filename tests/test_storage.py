import sqlite3
from pathlib import Path

import pytest

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


def test_message_rejects_invalid_time_before_queueing(store):
    with pytest.raises(ValueError, match="timezone"):
        add_message(store, entry_id="A", entry_name="A", platform="test", entry_kind="group",
                    kind="message", sender="小林", content="你好", occurred_at="2026-09-28T09:00:00", dedupe_key="1")


def test_migration_002_moves_existing_events_to_fixed_scene_subject(tmp_path):
    path = tmp_path / "old.db"
    script = (Path(__file__).resolve().parents[1] / "src/iris/migrations/001_learning_core.sql").read_text(encoding="utf-8")
    connection = sqlite3.connect(path)
    connection.executescript(script)
    connection.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)")
    connection.execute("INSERT INTO schema_migrations VALUES('001_learning_core.sql','2026-09-28')")
    connection.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('self','self','我','2026-09-28')")
    connection.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','test','group')")
    connection.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key)
        VALUES('A','event','self','门打开了','2026-09-28','2026-09-28','one')""")
    connection.commit()
    connection.close()
    upgraded = Store(path)
    try:
        with upgraded.read() as conn:
            row = conn.execute("SELECT sender_subject_id FROM messages WHERE kind='event'").fetchone()
            scene = conn.execute("SELECT name FROM subjects WHERE id='scene'").fetchone()
            assert row[0] == "scene" and scene[0] == "场景"
            assert "entry_id" in {column[1] for column in conn.execute("PRAGMA table_info(goals)")}
    finally:
        upgraded.close()


def test_quote_author_matches_account_or_unique_entry_participant(store):
    common = dict(entry_id="A", entry_name="A", platform="test", entry_kind="group",
                  kind="message", occurred_at="2026-09-28T09:00:00+08:00")
    author = add_message(store, sender="小林", account_id="lin-one", content="我喜欢猫", dedupe_key="one", **common)
    with_account = add_message(store, sender="小王", account_id="wang", content="引用一下", dedupe_key="two",
                               quote_author="小林", quote_author_account_id="lin-one", **common)
    by_name = add_message(store, sender="小王", account_id="wang", content="又引用", dedupe_key="three",
                          quote_author="小林", **common)
    add_message(store, sender="小林", account_id="lin-two", content="我是另一个小林", dedupe_key="four", **common)
    ambiguous = add_message(store, sender="小王", account_id="wang", content="不知道哪个", dedupe_key="five",
                            quote_author="小林", **common)
    with store.read() as conn:
        author_id = conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (author,)).fetchone()[0]
        first = conn.execute("SELECT quote_author_subject_id FROM messages WHERE id=?", (with_account,)).fetchone()[0]
        second = conn.execute("SELECT quote_author_subject_id FROM messages WHERE id=?", (by_name,)).fetchone()[0]
        unknown = conn.execute("SELECT quote_author_subject_id FROM messages WHERE id=?", (ambiguous,)).fetchone()[0]
        assert first == second == author_id
        assert unknown != author_id
        assert conn.execute("SELECT COUNT(*) FROM platform_identities WHERE subject_id=?", (unknown,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM platform_identities WHERE account_id LIKE 'quoted:%'").fetchone()[0] == 0


def test_event_intake_uses_scene_but_action_result_uses_self(store):
    common = dict(entry_id="A", entry_name="A", platform="test", entry_kind="group",
                  sender="我", occurred_at="2026-09-28T09:00:00+08:00")
    event = add_message(store, kind="event", content="石门打开", dedupe_key="event", **common)
    action = add_message(store, kind="action_result", content="拿到钥匙", dedupe_key="action", **common)
    with store.read() as conn:
        assert conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (event,)).fetchone()[0] == "scene"
        assert conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (action,)).fetchone()[0] == "self"
