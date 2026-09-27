"""Batch learning: evidence-bound model output, validation, and short commits."""

from __future__ import annotations

import difflib
import json
import re
import time
from datetime import datetime, timedelta
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from .db import Store, dumps, now
from .models import Gateway, ModelError
from .queue import Batch, estimate_tokens, get_batch, truncate_material


PROMPT_VERSION = "learning_v1"
PROMPT = files("iris").joinpath("prompts", PROMPT_VERSION + ".md").read_text(encoding="utf-8")
MEMORY_TYPES = {"事件", "事实", "偏好", "关系", "观点", "计划", "自我", "其他"}
STANCES = {"亲历", "转述", "推断", "观点"}
RETRY_DELAYS = (60, 300, 900)


def _score(value: Any, default: int) -> int:
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return default


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def _similar(a: str, b: str) -> bool:
    left, right = _normalize(a), _normalize(b)
    return bool(left and right and (left == right or difflib.SequenceMatcher(None, left, right).ratio() >= 0.88))


def normalize_event_time(value: Any, evidence_at: str, timezone_name: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    try:
        if len(raw) == 10:
            return datetime.fromisoformat(raw).date().isoformat()
        return datetime.fromisoformat(raw).isoformat()
    except ValueError:
        pass
    base = datetime.fromisoformat(evidence_at).astimezone(ZoneInfo(timezone_name))
    match = re.search(r"(下周|本周|这周|上周)([一二三四五六日天])", raw)
    if match:
        weekday = "一二三四五六日天".index(match.group(2))
        if weekday == 7:
            weekday = 6
        week_shift = {"上周": -1, "本周": 0, "这周": 0, "下周": 1}[match.group(1)]
        monday = base.date() - timedelta(days=base.weekday())
        return (monday + timedelta(weeks=week_shift, days=weekday)).isoformat()
    if raw in ("今天", "明天", "后天", "昨天"):
        return (base.date() + timedelta(days={"今天": 0, "明天": 1, "后天": 2, "昨天": -1}[raw])).isoformat()
    return raw


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row)


class LearningEngine:
    def __init__(self, store: Store, gateway: Gateway):
        self.store = store
        self.gateway = gateway

    def _snapshot(self, batch: Batch) -> dict[str, Any]:
        ids = batch.history_ids + batch.target_ids + batch.future_ids
        with self.store.read() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"""SELECT m.*,s.name AS sender_name,q.name AS quote_author_name
                FROM messages m JOIN subjects s ON s.id=m.sender_subject_id
                LEFT JOIN subjects q ON q.id=m.quote_author_subject_id WHERE m.id IN ({placeholders})""", ids).fetchall()
            memories = conn.execute("""SELECT m.*,s.name AS speaker_name FROM memories m
                JOIN subjects s ON s.id=m.speaker_subject_id WHERE m.lifecycle='active' ORDER BY m.importance DESC,m.id DESC""").fetchall()
            subjects = conn.execute("SELECT id,name,parent_id FROM subjects").fetchall()
            aliases = conn.execute("SELECT subject_id,alias FROM subject_aliases").fetchall()
            identities = conn.execute("SELECT subject_id,platform,account_id FROM platform_identities").fetchall()
            persona = conn.execute("SELECT content FROM persona_versions WHERE is_current=1 ORDER BY id DESC LIMIT 1").fetchone()
            about_rows = conn.execute("SELECT memory_id,subject_id FROM memory_subjects").fetchall()
        messages = {r["id"]: _row_dict(r) for r in rows}
        memory_list = [_row_dict(r) for r in memories]
        about: dict[int, set[str]] = {}
        for row in about_rows:
            about.setdefault(row["memory_id"], set()).add(row["subject_id"])
        return {"messages": messages, "memories": memory_list, "subjects": [_row_dict(r) for r in subjects],
                "aliases": [_row_dict(r) for r in aliases], "identities": [_row_dict(r) for r in identities],
                "persona": persona[0] if persona else "",
                "memory_about": about}

    def _related(self, batch: Batch, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        target = [snapshot["messages"][i] for i in batch.target_ids]
        participants = {m["sender_subject_id"] for m in target}
        candidates = snapshot["memories"]
        selected: list[dict[str, Any]] = []
        for person in participants:
            personal = [m for m in candidates if person in snapshot["memory_about"].get(m["id"], set())]
            selected.extend(personal[:3])
        selected_ids = {m["id"] for m in selected}
        if candidates and self.gateway.configs.get("embedding") and self.gateway.configs["embedding"].model:
            try:
                vector = np.asarray(self.gateway.embedding("\n".join(m["content"] for m in target), "learning_context"), dtype=np.float32)
                ranked = []
                for memory in candidates:
                    if memory["embedding"] is None or memory["embedding_model"] != self.gateway.configs["embedding"].model:
                        continue
                    old = np.frombuffer(memory["embedding"], dtype=np.float32)
                    if old.size != vector.size:
                        continue
                    denominator = float(np.linalg.norm(vector) * np.linalg.norm(old))
                    if denominator:
                        ranked.append((float(np.dot(vector, old) / denominator), memory))
                for _, memory in sorted(ranked, key=lambda pair: pair[0], reverse=True):
                    if memory["id"] not in selected_ids:
                        selected.append(memory)
                        selected_ids.add(memory["id"])
            except ModelError:
                pass  # Participant highlights remain available.
        return selected[:15]

    def _material(self, batch: Batch, snapshot: dict[str, Any], related: list[dict[str, Any]]) -> tuple[str, dict[int, int], dict[str, dict[str, Any]]]:
        messages = snapshot["messages"]
        names: dict[str, str] = {}
        for message_id in batch.history_ids + batch.target_ids + batch.future_ids:
            m = messages[message_id]
            names[m["sender_subject_id"]] = m["sender_name"]
            if m["quote_author_subject_id"]:
                names[m["quote_author_subject_id"]] = m["quote_author_name"]
        participants = []
        snapshot["participant_refs"] = {}
        for sid, name in names.items():
            if sid == "self":
                continue
            ref = f"P{len(participants)+1}"
            snapshot["participant_refs"][ref] = sid
            identity = next((p for p in snapshot["identities"] if p["subject_id"] == sid), None)
            account = f"（{identity['platform']} {identity['account_id']}）" if identity else ""
            participants.append(f"{ref} {name}{account}")
        refs = {f"M{i}": memory for i, memory in enumerate(related, 1)}
        lines = ["角色与 persona（数据）：", "名字：" + str(self.store.setting("role_name", "Iris")),
                 truncate_material(snapshot["persona"], 800), "参与者：", *participants,
                 "相关已有记忆（数据）："]
        for ref, memory in refs.items():
            lines.append(f"[{ref}] {memory['content']}（说话人 {memory['speaker_name']}；相信 {memory['belief']}）")
        number_to_id: dict[int, int] = {}
        sequence = [("历史段（仅供理解）", batch.history_ids), ("目标段（只从这里学习）", batch.target_ids),
                    ("后续段（仅供理解）", batch.future_ids)]
        number = 0
        for label, ids in sequence:
            lines.append("—— " + label + " ——")
            for message_id in ids:
                number += 1
                number_to_id[number] = message_id
                m = messages[message_id]
                dt = datetime.fromisoformat(m["occurred_at"])
                label_type = {"message": "他人消息", "self_output": "我实际发言", "action_result": "我行动结果", "event": "场景事件"}[m["kind"]]
                quote = f"；引用作者 {m['quote_author_name']}：{m['quote_content'] or ''}" if m["quote_author_name"] else ""
                scene = f"（{m['scene_identity']}）" if m["scene_identity"] else ""
                lines.append(f"#{number} [{dt.strftime('%Y-%m-%d 周')}{'一二三四五六日'[dt.weekday()]} {dt.strftime('%H:%M')}] "
                             f"[{label_type}] {m['sender_name']}{scene}{quote}：数据：{truncate_material(m['content'])}")
        material = "\n".join(lines)
        # Drop context from the beginning/end if the material exceeds its budget; target IDs stay fixed.
        if estimate_tokens(material) > 10000:
            kept = [line for line in lines if not line.startswith("#") or any(
                line.startswith(f"#{n} ") for n, mid in number_to_id.items() if mid in batch.target_ids)]
            material = "\n".join(kept)
            number_to_id = {n: mid for n, mid in number_to_id.items() if mid in batch.target_ids}
        return material, number_to_id, refs

    def _validate(self, output: dict[str, Any], batch: Batch, snapshot: dict[str, Any],
                  number_to_id: dict[int, int], refs: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        target = set(batch.target_ids)
        messages = snapshot["messages"]
        accepted: dict[str, list[dict[str, Any]]] = {key: [] for key in ("memories", "updates", "people", "goals", "questions")}
        dropped: list[dict[str, Any]] = []

        def evidence(item: dict[str, Any]) -> list[int]:
            values = item.get("evidence")
            if not isinstance(values, list):
                raise ValueError("missing evidence list")
            result = [number_to_id[n] for n in values if type(n) is int and n in number_to_id]
            if not result or not target.intersection(result):
                raise ValueError("no target-segment evidence")
            if len(result) != len(values):
                raise ValueError("unknown evidence number")
            return list(dict.fromkeys(result))

        for item in output.get("memories", []) if isinstance(output.get("memories", []), list) else []:
            try:
                if not isinstance(item, dict):
                    raise ValueError("not an object")
                content = str(item.get("content") or "").strip()
                if not content or len(content) > 1000:
                    raise ValueError("empty or overlong content")
                ev = evidence(item)
                stance = str(item.get("stance") or "")
                speaker = str(item.get("speaker") or "").strip()
                speaker_id = snapshot["participant_refs"].get(speaker)
                if speaker_id:
                    speaker = next(s["name"] for s in snapshot["subjects"] if s["id"] == speaker_id)
                if stance not in STANCES:
                    raise ValueError("invalid stance")
                if stance == "推断" and speaker != "我":
                    raise ValueError("inference speaker must be self")
                if speaker == "我" and stance in ("亲历", "观点") and not any(
                    messages[i]["kind"] in ("self_output", "action_result", "event") for i in ev
                ):
                    raise ValueError("self claim has no self evidence")
                if speaker != "我":
                    evidence_speakers = set()
                    for i in ev:
                        if messages[i]["sender_name"] == speaker:
                            evidence_speakers.add(messages[i]["sender_subject_id"])
                        if messages[i]["quote_author_name"] == speaker:
                            evidence_speakers.add(messages[i]["quote_author_subject_id"])
                    if speaker_id and speaker_id not in evidence_speakers:
                        raise ValueError("speaker reference is not in evidence")
                    if not speaker_id and len(evidence_speakers) != 1:
                        raise ValueError("speaker is absent or ambiguous in evidence")
                    speaker_id = speaker_id or next(iter(evidence_speakers))
                about = item.get("about") or []
                if not isinstance(about, list):
                    raise ValueError("about must be a list")
                about = [str(name).strip() for name in about if str(name).strip()][:10]
                for name in about:
                    if name in snapshot["participant_refs"] or name == "我":
                        continue
                    participants_matching = {m["sender_subject_id"] for m in messages.values() if m["sender_name"] == name}
                    participants_matching.update(m["quote_author_subject_id"] for m in messages.values() if m["quote_author_name"] == name)
                    if len(participants_matching) > 1:
                        raise ValueError("about subject is ambiguous; use participant number")
                tags = item.get("tags") or []
                if not isinstance(tags, list):
                    tags = []
                first_target = next(i for i in ev if i in target)
                derived = item.get("derived_from") or []
                if not isinstance(derived, list) or any(ref not in refs for ref in derived):
                    raise ValueError("unknown derived memory reference")
                accepted["memories"].append({"content": content, "kind": str(item.get("type") or "其他") if item.get("type") in MEMORY_TYPES else "其他",
                    "about": about, "tags": [str(tag).strip() for tag in tags if str(tag).strip()][:10],
                    "speaker": speaker, "speaker_id": speaker_id or "self", "stance": stance,
                    "belief": _score(item.get("belief"), 60),
                    "importance": _score(item.get("importance"), 50),
                    "event_time": normalize_event_time(item.get("event_time"), messages[first_target]["occurred_at"],
                                                         str(self.store.setting("timezone", "Asia/Shanghai"))),
                    "evidence": ev, "derived_from": derived})
            except ValueError as exc:
                dropped.append({"section": "memories", "item": item, "reason": str(exc)})
        accepted["memories"].sort(key=lambda item: item["importance"], reverse=True)
        for item in accepted["memories"][20:]:
            dropped.append({"section": "memories", "item": item, "reason": "batch memory limit 20"})
        accepted["memories"] = accepted["memories"][:20]

        for section in ("updates", "people", "goals"):
            values = output.get(section) or []
            if not isinstance(values, list):
                dropped.append({"section": section, "item": values, "reason": "not a list"})
                continue
            for item in values:
                try:
                    if not isinstance(item, dict):
                        raise ValueError("not an object")
                    ev = evidence(item)
                    if section == "updates":
                        if item.get("ref") not in refs:
                            raise ValueError("unknown memory reference")
                        if item.get("action") not in ("确认", "修正", "反驳"):
                            raise ValueError("invalid update action")
                        if item.get("action") != "确认" and not str(item.get("content") or "").strip():
                            raise ValueError("missing corrected content")
                    elif section == "people":
                        if not item.get("name") or not (item.get("same_as") or item.get("roleplay")):
                            raise ValueError("missing people relation")
                    elif not str(item.get("content") or "").strip():
                        raise ValueError("empty goal")
                    accepted[section].append({**item, "evidence": ev})
                except ValueError as exc:
                    dropped.append({"section": section, "item": item, "reason": str(exc)})
        for item in output.get("questions", []) if isinstance(output.get("questions", []), list) else []:
            if isinstance(item, str) and item.strip():
                accepted["questions"].append({"content": item.strip(), "evidence": [batch.target_ids[0]]})
        return accepted, dropped

    def _resolve_subject(self, conn: Any, name: str, snapshot: dict[str, Any]) -> str:
        if name == "我":
            return "self"
        if name in snapshot.get("participant_refs", {}):
            return snapshot["participant_refs"][name]
        participant_ids = {m["sender_subject_id"] for m in snapshot["messages"].values() if m["sender_name"] == name}
        participant_ids.update(m["quote_author_subject_id"] for m in snapshot["messages"].values() if m["quote_author_name"] == name)
        if len(participant_ids) == 1:
            return next(iter(participant_ids))
        known = [subject["id"] for subject in snapshot["subjects"] if subject["name"] == name]
        if len(known) == 1:
            return known[0]
        for alias in snapshot["aliases"]:
            if alias["alias"] == name:
                return alias["subject_id"]
        row = conn.execute("SELECT id FROM subjects WHERE name=? ORDER BY created_at LIMIT 1", (name,)).fetchone()
        if row:
            return row[0]
        import uuid
        parent_id = None
        if name.endswith("的妈妈"):
            parent_id = self._resolve_subject(conn, name[:-3], snapshot)
        subject_id = uuid.uuid4().hex
        conn.execute("INSERT INTO subjects(id,kind,name,parent_id,created_at) VALUES(?,'person',?,?,?)",
                     (subject_id, name, parent_id, now()))
        snapshot["subjects"].append({"id": subject_id, "name": name, "parent_id": parent_id})
        return subject_id

    def _source(self, conn: Any, memory_id: int, message_id: int) -> None:
        exists = conn.execute("SELECT 1 FROM sources WHERE memory_id=? AND kind='message' AND message_id=?", (memory_id, message_id)).fetchone()
        if not exists:
            conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",
                         (memory_id, message_id, now()))

    def _apply(self, batch: Batch, accepted: dict[str, list[dict[str, Any]]], dropped: list[dict[str, Any]],
               snapshot: dict[str, Any], refs: dict[str, dict[str, Any]], attempt: dict[str, Any]) -> dict[str, Any]:
        created: list[int] = []
        updated: list[int] = []
        confirmed: list[int] = []
        target = set(batch.target_ids)
        with self.store.write() as conn:
            row = conn.execute("SELECT state FROM batches WHERE id=?", (batch.id,)).fetchone()
            if not row or row[0] != "running":
                raise RuntimeError("batch state changed before commit")
            existing = conn.execute("SELECT * FROM memories WHERE lifecycle='active'").fetchall()
            existing_about = {r["memory_id"]: set() for r in conn.execute("SELECT memory_id FROM memory_subjects")}
            for r in conn.execute("SELECT memory_id,subject_id FROM memory_subjects"):
                existing_about.setdefault(r["memory_id"], set()).add(r["subject_id"])
            for item in accepted["memories"]:
                speaker_id = item["speaker_id"]
                about_ids = {self._resolve_subject(conn, name, snapshot) for name in item["about"]}
                duplicate = next((m for m in existing if m["speaker_subject_id"] == speaker_id and
                                  existing_about.get(m["id"], set()) == about_ids and _similar(m["content"], item["content"])), None)
                if duplicate:
                    conn.execute("UPDATE memories SET retention=MIN(100,retention+5),last_confirmed_at=? WHERE id=?",
                                 (now(), duplicate["id"]))
                    for message_id in item["evidence"]:
                        if message_id in target:
                            self._source(conn, duplicate["id"], message_id)
                    confirmed.append(duplicate["id"])
                    continue
                stamp = now()
                result = conn.execute("""INSERT INTO memories
                    (content,kind,speaker_subject_id,stance,belief,importance,retention,event_time,entry_id,world,
                     created_at,updated_at,first_confirmed_at,last_confirmed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,'real',?,?,?,?)""",
                    (item["content"], item["kind"], speaker_id, item["stance"], item["belief"], item["importance"],
                     round(30 + item["importance"] * 0.4), item["event_time"], batch.entry_id,
                     stamp, stamp, stamp, stamp))
                memory_id = int(result.lastrowid)
                for sid in about_ids:
                    conn.execute("INSERT INTO memory_subjects(memory_id,subject_id) VALUES(?,?)", (memory_id, sid))
                for tag in item["tags"]:
                    conn.execute("INSERT OR IGNORE INTO memory_tags(memory_id,tag) VALUES(?,?)", (memory_id, tag))
                for message_id in item["evidence"]:
                    self._source(conn, memory_id, message_id)
                for ref in item["derived_from"]:
                    source_memory = refs[ref]
                    conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,?,?)",
                                 (memory_id, source_memory["id"], source_memory["revision"], stamp))
                created.append(memory_id)
                new_memory = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
                existing.append(new_memory)
                existing_about[memory_id] = about_ids
            for item in accepted["updates"]:
                original = refs[item["ref"]]
                current = conn.execute("SELECT * FROM memories WHERE id=?", (original["id"],)).fetchone()
                if not current or current["revision"] != original["revision"] or current["lifecycle"] != "active":
                    dropped.append({"section": "updates", "item": item, "reason": "memory revision changed"})
                    continue
                if item["action"] == "确认":
                    conn.execute("UPDATE memories SET retention=MIN(100,retention+5),last_confirmed_at=? WHERE id=?",
                                 (now(), current["id"]))
                    confirmed.append(current["id"])
                else:
                    content = str(item.get("content") or "").strip()
                    if len(content) > 1000:
                        dropped.append({"section": "updates", "item": item, "reason": "overlong corrected content"})
                        continue
                    before = {key: current[key] for key in ("content", "kind", "stance", "belief", "importance", "event_time")}
                    belief = _score(item.get("belief"), current["belief"])
                    conn.execute("UPDATE memories SET content=?,belief=?,revision=revision+1,updated_at=? WHERE id=?",
                                 (content, belief, now(), current["id"]))
                    after = {**before, "content": content, "belief": belief}
                    conn.execute("""INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,
                        reason,actor,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (current["id"], current["revision"], current["revision"] + 1, dumps(before), dumps(after),
                         str(item.get("reason") or item["action"]), "learning", now()))
                    updated.append(current["id"])
                for message_id in item["evidence"]:
                    if message_id in target:
                        self._source(conn, current["id"], message_id)
            for item in accepted["people"]:
                a = self._resolve_subject(conn, str(item["name"]), snapshot)
                relation_name = str(item.get("same_as") or item.get("roleplay"))
                b = self._resolve_subject(conn, relation_name, snapshot)
                kind = "same_as" if item.get("same_as") else "roleplay"
                if a == b:
                    dropped.append({"section": "people", "item": item, "reason": "same subject on both sides"})
                    continue
                if kind == "same_as" and a > b:
                    a, b = b, a
                conn.execute("""INSERT INTO subject_links(subject_a,subject_b,kind,belief,world,source_message_id,created_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(subject_a,subject_b,kind) DO UPDATE SET belief=excluded.belief""",
                    (a, b, kind, _score(item.get("belief"), 60), item.get("world"), item["evidence"][0], now()))
            for section in ("goals", "questions"):
                for item in accepted[section]:
                    content = str(item["content"]).strip()
                    kind = "question" if section == "questions" else "normal"
                    old = conn.execute("SELECT id FROM goals WHERE kind=? AND state='open' AND content=?", (kind, content)).fetchone()
                    if old:
                        goal_id = old[0]
                    else:
                        goal_id = conn.execute("INSERT INTO goals(content,kind,deadline,created_at) VALUES(?,?,?,?)",
                                               (content, kind, item.get("deadline"), now())).lastrowid
                    for message_id in item["evidence"]:
                        conn.execute("INSERT OR IGNORE INTO goal_sources(goal_id,message_id) VALUES(?,?)", (goal_id, message_id))
            conn.executemany("UPDATE messages SET learning_state='learned' WHERE id=?", ((i,) for i in batch.target_ids))
            result = {"created": created, "updated": updated, "confirmed": confirmed, "dropped": dropped,
                      "parse_status": attempt["parse_status"]}
            conn.execute("UPDATE batches SET state='succeeded',attempt_count=attempt_count+1,finished_at=?,result_json=?,last_error=NULL WHERE id=?",
                         (now(), dumps(result), batch.id))
            conn.execute("DELETE FROM memory_gaps WHERE batch_id=?", (batch.id,))
            self._write_attempt(conn, batch.id, attempt)
        for memory_id in created + updated:
            self._embed_memory(memory_id)
        return result

    @staticmethod
    def _write_attempt(conn: Any, batch_id: int, attempt: dict[str, Any]) -> None:
        conn.execute("""INSERT INTO batch_attempts(batch_id,number,started_at,finished_at,raw_output,repair_output,
            parse_status,error,duration_ms) VALUES(?,?,?,?,?,?,?,?,?)""",
            (batch_id, attempt["number"], attempt["started_at"], now(), attempt.get("raw_output"),
             attempt.get("repair_output"), attempt["parse_status"], attempt.get("error"), attempt["duration_ms"]))

    def _embed_memory(self, memory_id: int) -> None:
        config = self.gateway.configs.get("embedding")
        if not config or not config.model:
            return
        with self.store.read() as conn:
            row = conn.execute("SELECT content,revision FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return
        try:
            vector = np.asarray(self.gateway.embedding(row["content"], "memory_embedding"), dtype=np.float32)
        except ModelError:
            return
        with self.store.write() as conn:
            conn.execute("UPDATE memories SET embedding=?,embedding_model=? WHERE id=? AND revision=?",
                         (vector.tobytes(), config.model, memory_id, row["revision"]))

    def _fail(self, batch: Batch, attempt: dict[str, Any], error: ModelError) -> str:
        with self.store.write() as conn:
            count = batch.attempt_count + 1
            if error.category == "content_rejection":
                state, message_state, gap = "refused", "refused", "content_rejection"
            elif count >= 4:
                state, message_state, gap = "abandoned", "abandoned", "attempts_exhausted"
            else:
                state, message_state, gap = "waiting", "batched", None
            attempt["error"] = error.summary
            attempt["parse_status"] = "failed"
            self._write_attempt(conn, batch.id, attempt)
            next_at = (datetime.fromisoformat(now()) + timedelta(seconds=RETRY_DELAYS[min(count - 1, 2)])).isoformat() if state == "waiting" else None
            conn.execute("UPDATE batches SET state=?,attempt_count=?,next_retry_at=?,last_error=?,finished_at=? WHERE id=?",
                         (state, count, next_at, error.summary, now() if state != "waiting" else None, batch.id))
            if state != "waiting":
                conn.executemany("UPDATE messages SET learning_state=? WHERE id=?", ((message_state, i) for i in batch.target_ids))
            if gap:
                ids = batch.target_ids
                placeholders = ",".join("?" for _ in ids)
                times = conn.execute(f"SELECT MIN(occurred_at),MAX(occurred_at) FROM messages WHERE id IN ({placeholders})", ids).fetchone()
                conn.execute("""INSERT OR REPLACE INTO memory_gaps(batch_id,entry_id,started_at,ended_at,reason,created_at)
                    VALUES(?,?,?,?,?,?)""", (batch.id, batch.entry_id, times[0], times[1], gap, now()))
            return state

    def run_batch(self, batch_id: int, *, force: bool = False) -> dict[str, Any]:
        batch = get_batch(self.store, batch_id)
        if batch.state != "waiting":
            raise ValueError(f"batch {batch_id} is {batch.state}")
        with self.store.write() as conn:
            row = conn.execute("SELECT next_retry_at,attempt_count FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row["attempt_count"] >= 4:
                raise ValueError("batch has exhausted attempts")
            if row["next_retry_at"] and not force and datetime.fromisoformat(row["next_retry_at"]) > datetime.fromisoformat(now()):
                raise ValueError("batch is waiting for retry time")
            conn.execute("UPDATE batches SET state='running',next_retry_at=NULL WHERE id=?", (batch_id,))
        started = time.monotonic()
        with self.store.read() as conn:
            attempt_number = conn.execute("SELECT COALESCE(MAX(number),0)+1 FROM batch_attempts WHERE batch_id=?", (batch_id,)).fetchone()[0]
        attempt = {"number": attempt_number, "started_at": now(), "raw_output": None,
                   "repair_output": None, "parse_status": "failed", "duration_ms": 0}
        try:
            snapshot = self._snapshot(batch)
            related = self._related(batch, snapshot)
            material, numbers, refs = self._material(batch, snapshot, related)
            output, raw, repair, parse_status, *details = self.gateway.json_chat(
                [{"role": "system", "content": PROMPT}, {"role": "user", "content": material}], "learning", max_tokens=6500)
            attempt.update(raw_output=raw, repair_output=repair, parse_status=parse_status,
                           error=details[0] if details else None)
            accepted, dropped = self._validate(output, batch, snapshot, numbers, refs)
            attempt["duration_ms"] = round((time.monotonic() - started) * 1000)
            return self._apply(batch, accepted, dropped, snapshot, refs, attempt)
        except ModelError as error:
            attempt["raw_output"] = attempt["raw_output"] or error.first_raw or error.raw_output
            if error.first_raw:
                attempt["repair_output"] = error.raw_output
            attempt["duration_ms"] = round((time.monotonic() - started) * 1000)
            state = self._fail(batch, attempt, error)
            return {"state": state, "error": error.summary}


def independent_evidence_count(store: Store, memory_id: int) -> int:
    with store.read() as conn:
        rows = conn.execute("""SELECT DISTINCT m.sender_subject_id FROM sources s JOIN messages m ON s.message_id=m.id
            WHERE s.memory_id=? AND s.kind='message'""", (memory_id,)).fetchall()
    return len(rows)
