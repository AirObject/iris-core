"""Upgrading existing identity records preserves IDs, evidence and denial."""
import sqlite3
from contextlib import closing
from pathlib import Path

from iris.db import Store
from iris.search_text import segmented


def test_migration_010_folds_legacy_reverse_pair_without_losing_alias_or_denial(tmp_path):
    path = tmp_path / 'old.db'
    migrations = Path(__file__).parents[1] / 'src/iris/migrations'
    with sqlite3.connect(path) as conn:
        conn.create_function('iris_terms', 1, segmented)
        conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        for script in sorted(migrations.glob('00*.sql')):
            conn.executescript(script.read_text(encoding='utf-8'))
            conn.execute("INSERT INTO schema_migrations VALUES(?,'2026-10-08')", (script.name,))
        for sid in ('a', 'b'):
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,'2026-10-08')", (sid, sid))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('b','别名')")
        conn.execute("INSERT INTO subject_links(subject_a,subject_b,kind,belief,status,created_at) VALUES('b','a','same_as',40,'denied','2026-10-08')")
        conn.execute("INSERT INTO subject_links(subject_a,subject_b,kind,belief,status,created_at) VALUES('a','b','same_as',80,'possible','2026-10-08')")
    with closing(Store(path)) as store:
        with store.read() as conn:
            aliases = [dict(r) for r in conn.execute('SELECT * FROM subject_aliases')]
            assert len(aliases) == 1 and aliases[0]['alias'] == '别名' and aliases[0]['source_message_id'] is None
            links = [dict(r) for r in conn.execute('SELECT * FROM subject_links ORDER BY id')]
            assert len(links) == 2
            assert links[0]['id'] == 1 and links[0]['folded_into'] == 2 and links[0]['status'] == 'denied'
            assert links[1]['id'] == 2 and links[1]['folded_into'] is None and links[1]['status'] == 'denied'
            assert links[1]['belief'] == 40
            assert (links[1]['subject_a'], links[1]['subject_b']) == ('a', 'b')
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        with store.write() as conn:
            conn.execute("INSERT INTO subject_links(subject_a,subject_b,kind,belief,created_at) VALUES('b','a','same_as',99,'today')")
            conn.execute("UPDATE subject_links SET status='possible' WHERE id=2")
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM subject_links').fetchone()[0] == 2
            assert conn.execute('SELECT status FROM subject_links WHERE id=2').fetchone()[0] == 'denied'
    assert len(list(tmp_path.glob('old.db.*.bak'))) == 1
