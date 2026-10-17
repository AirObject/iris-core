"""Recover account fallback names only when a later intake supplies a name."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from iris import people
from iris.queue import add_message
from test_people import confirm, link
from test_retrieval import put


ACCOUNT = "account-a"
ACTION = "identity_fallback_name_updated"


def receive(store, key, *, sender=ACCOUNT, account_id=ACCOUNT, platform="test", **kwargs):
    return add_message(store, entry_id="room", entry_name="room", platform=platform,
                       entry_kind="group", kind="message", sender=sender, account_id=account_id,
                       content="用于验证接收事务的消息", occurred_at="2026-10-17T10:00:00+08:00",
                       dedupe_key=key, **kwargs)


def identity(store, account_id=ACCOUNT, platform="test"):
    with store.read() as conn:
        row = dict(conn.execute("SELECT * FROM platform_identities WHERE platform=? AND account_id=?",
                                (platform, account_id)).fetchone())
        sid = people.canonical_subject(conn, row["subject_id"])
        subject = conn.execute("SELECT name,revision FROM subjects WHERE id=?", (sid,)).fetchone()
        return {**row, "canonical_id": sid, "name": subject["name"], "revision": subject["revision"]}


def audits(store):
    with store.read() as conn:
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in conn.execute(
            "SELECT * FROM admin_operations WHERE action=? ORDER BY id", (ACTION,))]


@pytest.mark.parametrize("account", ["123456789", "opaque-account"])
def test_fallback_updates_name_once_and_preserves_memory_and_aliases(store, account):
    first = receive(store, "first", sender=account, account_id=account)
    original = identity(store, account)
    sid = original["canonical_id"]
    people.add_alias(store, sid, "原有别名", expected_revision=original["revision"])
    mid = put(store, f"{account} 喜欢木工", speaker=sid, about=[sid], evidence=[first])
    before = identity(store, account)
    with store.read() as conn:
        memory = dict(conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())
        aliases = [tuple(r) for r in conn.execute("SELECT * FROM subject_aliases")]
    received = receive(store, "named", sender="乔予", account_id=account)
    after = identity(store, account)
    assert after == {**before, "display_name": "乔予", "name": "乔予", "revision": before["revision"] + 1}
    with store.read() as conn:
        assert conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (received,)).fetchone()[0] == sid
        assert dict(conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()) == memory
        assert [tuple(r) for r in conn.execute("SELECT * FROM subject_aliases")] == aliases
        assert conn.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 0
    [audit] = audits(store)
    assert (audit["actor"], audit["object_type"], audit["object_id"]) == ("system", "subject", sid)
    assert audit["details"] == {"platform": "test", "account_id": account, "subject_name_updated": True,
                                "revision_before": before["revision"], "revision_after": after["revision"]}
    assert audit["created_at"]
    assert "乔予" not in audit["details_json"] and "接收事务" not in audit["details_json"]
    with pytest.raises(people.PeopleConflict):
        people.add_alias(store, sid, "过时的编辑", expected_revision=before["revision"])
    receive(store, "again", sender="乔予", account_id=account)
    assert identity(store, account) == after
    assert len(audits(store)) == 1


def test_merged_subject_with_a_name_only_updates_identity_display_name(store):
    receive(store, "fallback")
    receive(store, "known", sender="保留的名字", account_id="known-account")
    source = identity(store)["canonical_id"]
    target = identity(store, "known-account")["canonical_id"]
    confirm(store, link(store, source, target), target=target)
    before = identity(store)
    known_before = identity(store, "known-account")
    with store.read() as conn:
        aliases_before = [tuple(r) for r in conn.execute("SELECT * FROM subject_aliases ORDER BY id")]
        links_before = [tuple(r) for r in conn.execute("SELECT * FROM subject_links ORDER BY id")]
    receive(store, "named", sender="新来的昵称")
    assert identity(store) == {**before, "display_name": "新来的昵称"}
    assert identity(store, "known-account") == known_before
    with store.read() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM subject_aliases ORDER BY id")] == aliases_before
        assert [tuple(r) for r in conn.execute("SELECT * FROM subject_links ORDER BY id")] == links_before
        assert conn.execute("SELECT name FROM subjects WHERE id=?", (source,)).fetchone()[0] == ACCOUNT
    [audit] = audits(store)
    assert audit["object_id"] == target
    assert audit["details"] == {"platform": "test", "account_id": ACCOUNT, "subject_name_updated": False,
                                "revision_before": before["revision"], "revision_after": before["revision"]}


def test_canonical_resolution_updates_survivor_not_retired_subject(store):
    receive(store, "fallback")
    receive(store, "survivor", sender=ACCOUNT, account_id="survivor-account")
    source = identity(store)["canonical_id"]
    target = identity(store, "survivor-account")["canonical_id"]
    # Exercise a pre-existing canonical redirect without repointing the identity.
    with store.write() as conn:
        conn.execute("UPDATE subjects SET merged_into=? WHERE id=?", (target, source))
    before = identity(store)
    mid = receive(store, "named", sender="乔予")
    assert identity(store) == {**before, "display_name": "乔予", "name": "乔予", "revision": before["revision"] + 1}
    with store.read() as conn:
        assert conn.execute("SELECT name FROM subjects WHERE id=?", (source,)).fetchone()[0] == ACCOUNT
        assert conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (mid,)).fetchone()[0] == target
    assert audits(store)[0]["object_id"] == target


@pytest.mark.parametrize("original", ["原来的昵称", "", " account-a "])
def test_only_exact_account_display_name_is_a_fallback(store, original):
    receive(store, "first", sender=original)
    before = identity(store)
    receive(store, "new", sender="不同的群名片")
    assert identity(store) == before
    assert audits(store) == []


def test_same_nickname_on_different_accounts_does_not_merge(store):
    receive(store, "a")
    receive(store, "b", sender="account-b", account_id="account-b")
    ids = {identity(store, account)["canonical_id"] for account in (ACCOUNT, "account-b")}
    receive(store, "a-name", sender="同名")
    receive(store, "b-name", sender="同名", account_id="account-b")
    assert {identity(store, account)["canonical_id"] for account in (ACCOUNT, "account-b")} == ids
    assert len(ids) == 2
    assert all(identity(store, account)["name"] == "同名" for account in (ACCOUNT, "account-b"))
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subject_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM subject_aliases").fetchone()[0] == 0
    assert len(audits(store)) == 2


def test_same_account_on_different_platforms_stays_separate(store):
    receive(store, "a")
    receive(store, "b", platform="another-host")
    untouched = identity(store, platform="another-host")
    receive(store, "a-name", sender="乔予")
    assert identity(store)["name"] == "乔予"
    assert identity(store, platform="another-host") == untouched
    assert identity(store)["canonical_id"] != untouched["canonical_id"]


def test_quote_author_account_fallback_is_recovered(store):
    receive(store, "quoted", sender="转述者", account_id="forwarder",
            quote_author_account_id=ACCOUNT, quote_content="被引用的话")
    before = identity(store)
    mid = receive(store, "quoted-name", sender="转述者", account_id="forwarder",
                  quote_author="乔予", quote_author_account_id=ACCOUNT, quote_content="另一句引用")
    assert identity(store) == {**before, "display_name": "乔予", "name": "乔予", "revision": before["revision"] + 1}
    with store.read() as conn:
        assert conn.execute("SELECT quote_author_subject_id FROM messages WHERE id=?", (mid,)).fetchone()[0] == before["canonical_id"]
    assert len(audits(store)) == 1


def test_concurrent_messages_update_fallback_and_revision_only_once(store):
    receive(store, "first")
    before = identity(store)
    barrier = Barrier(2)
    def send(index):
        barrier.wait(timeout=5)
        return receive(store, f"concurrent-{index}", sender=f"昵称{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        mids = list(pool.map(send, (1, 2)))
    after = identity(store)
    assert after["name"] == after["display_name"] and after["name"] in {"昵称1", "昵称2"}
    assert after["revision"] == before["revision"] + 1
    assert len(mids) == len(set(mids)) == 2
    assert len(audits(store)) == 1


@pytest.mark.parametrize("name", ["", " \t\n", ACCOUNT, f" {ACCOUNT} "])
@pytest.mark.parametrize("quoted", [False, True])
def test_blank_or_account_name_does_not_trigger_recovery(store, name, quoted):
    receive(store, "first")
    before = identity(store)
    if quoted:
        receive(store, "next", sender="转述者", account_id="forwarder",
                quote_author=name, quote_author_account_id=ACCOUNT, quote_content="被引用的话")
    else:
        receive(store, "next", sender=name)
    assert identity(store) == before
    assert audits(store) == []


def test_replayed_message_does_not_rename_or_add_audit(store):
    mid = receive(store, "same-key")
    before = identity(store)
    assert receive(store, "same-key", sender="乔予") == mid
    assert identity(store) == before
    assert audits(store) == []


@pytest.mark.parametrize("failure", ["message", "audit"])
def test_failed_intake_rolls_back_name_revision_and_audit(store, failure):
    receive(store, "first")
    before = identity(store)
    table, condition = (("messages", "new.dedupe_key='rejected'") if failure == "message" else
                        ("admin_operations", f"new.action='{ACTION}'"))
    with store.write() as conn:
        conn.execute(f"""CREATE TRIGGER reject_recovery BEFORE INSERT ON {table}
            WHEN {condition} BEGIN SELECT RAISE(ABORT,'intake rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="intake rejected"):
        receive(store, "rejected", sender="乔予")
    assert identity(store) == before
    assert audits(store) == []
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
