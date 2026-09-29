"""Shared retrieval, snapshot reply preparation, and idempotent use feedback."""
from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from typing import Any

from .db import Store, dumps, now
from .models import Gateway, ModelError
from .queue import estimate_tokens
from .search_text import match_query
from .query_analysis import analyze, attribute_evidence

RRF_K = 60
CANDIDATES = 200
DEFAULTS = json.loads(files("iris").joinpath("retrieval_defaults.json").read_text(encoding="utf-8"))
MEMORY_COLUMNS = """m.id,m.content,m.kind,m.speaker_subject_id,m.stance,m.belief,m.importance,m.retention,
    m.event_time,m.lifecycle,m.revision,m.entry_id,m.world,m.created_at,m.updated_at,s.name AS speaker_name"""


def _people(conn, values: list[str]) -> list[str]:
    result = []
    for value in values:
        rows = conn.execute("""SELECT DISTINCT s.id FROM subjects s LEFT JOIN subject_aliases a ON a.subject_id=s.id
            WHERE s.id=? OR s.name=? OR a.alias=?""", (value, value, value)).fetchall()
        exact = next((r[0] for r in rows if r[0] == value), None)
        if exact:
            result.append(exact)
        elif len(rows) > 1:
            raise ValueError(f"ambiguous person: {value}; use subject ID")
        elif rows:
            result.append(rows[0][0])
        else:
            result.append("missing:" + value)
    return list(dict.fromkeys(result))


def _filters(conn, *, people=(), kinds=(), stances=(), time_from=None, time_to=None, include_forgotten=False):
    clauses = ["m.lifecycle IN ('active','forgotten')" if include_forgotten else "m.lifecycle='active'"]
    args: list[Any] = []
    if people:
        people = _people(conn, list(people))
        clauses.append("m.id IN (SELECT memory_id FROM memory_subjects WHERE subject_id IN (" + ",".join("?" for _ in people) + "))")
        args.extend(people)
    for column, values in (("kind", kinds), ("stance", stances)):
        if values:
            clauses.append(f"m.{column} IN (" + ",".join("?" for _ in values) + ")")
            args.extend(values)
    for operator, value in ((">=", time_from), ("<=", time_to)):
        if value is not None:
            datetime.fromisoformat(value)
            # A date upper bound includes its entire day; offset timestamps compare in UTC.
            if operator == "<=" and len(value) == 10:
                clauses.append("julianday(m.event_time)<julianday(?,'+1 day')")
            else:
                clauses.append(f"julianday(m.event_time){operator}julianday(?)")
            args.append(value)
    return " AND ".join(clauses), args


def latest_models(conn) -> list[dict]:
    return [dict(row) for row in conn.execute("""SELECT purpose,model,result_category,duration_ms,error_summary,created_at
        FROM model_calls WHERE id IN (SELECT MAX(id) FROM model_calls GROUP BY purpose) ORDER BY purpose""")]


def backlog(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("""SELECT e.id AS entry_id,
        SUM(CASE WHEN m.learning_state IN ('pending','batched') THEN 1 ELSE 0 END) AS pending_count,
        SUM(CASE WHEN m.learning_state IN ('abandoned','refused') THEN 1 ELSE 0 END) AS gap_message_count,
        MIN(CASE WHEN m.learning_state IN ('pending','batched') THEN m.occurred_at END) AS oldest_at,
        MAX(CASE WHEN m.learning_state IN ('pending','batched') THEN m.occurred_at END) AS latest_at
        FROM entries e LEFT JOIN messages m ON m.entry_id=e.id GROUP BY e.id ORDER BY e.id""")]


class Retrieval:
    def __init__(self, store: Store, gateway: Gateway | None = None, *, tokenizer: str | None = None,
                 vector_min: float | None = None, dtype: str | None = None,
                 vector_relative: float | None = None, vector_weight: float | None = None,
                 query_prefix: str | None = None,
                 clock=None):
        self.store, self.gateway = store, gateway
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.overrides = {k: v for k, v in dict(tokenizer=tokenizer, vector_min=vector_min,
                                              vector_relative=vector_relative, vector_weight=vector_weight,
                                              query_prefix=query_prefix, dtype=dtype).items() if v is not None}
        config = gateway.configs.get("embedding") if gateway else None
        self.model = config.model if config and config.model and config.base_url else ""
        self.settings = self._settings()
        if self.settings["tokenizer"] not in ("jieba", "trigram"):
            raise ValueError("unknown FTS tokenizer")
        self.index = store.vector_index(self.model, self.settings["dtype"]) if self.model else None
        self.last_embedding_ms = 0.0

    def _settings(self):
        saved = self.store.setting('retrieval', {})
        # Legacy installations contain 2048-dimensional vectors. Never reinterpret
        # them when the default for newly created databases changes.
        if saved:
            saved.setdefault('embedding_dimensions', 2048)
            saved.setdefault('query_prefix', '')
        return {**DEFAULTS, **saved, **self.overrides}

    def embedding_text(self, text: str, participants=()) -> str:
        with self.store.read() as conn:
            analysis = analyze(conn, text, _people(conn, list(participants)))
        return self.settings['query_prefix'] + analysis.text

    def _query_vector(self, text: str, participants=()):
        self.settings = self._settings()
        if self.settings["tokenizer"] not in ("jieba", "trigram"):
            raise ValueError("unknown FTS tokenizer")
        if self.model:
            self.index = self.store.vector_index(self.model, self.settings["dtype"])
        self.last_embedding_ms = 0.0
        if not text.strip() or not self.model:
            return None, [{"code": "embedding_unconfigured", "message": "未配置 embedding，本次使用全文检索。"}] if text.strip() else []
        dimensions = self.gateway.configs['embedding'].dimensions
        if (self.settings["embedding_model"] != self.model or dimensions not in (None, self.settings['embedding_dimensions'])) and "vector_min" not in self.overrides:
            return None, [{"code": "embedding_uncalibrated", "message": "embedding 模型已变化，需在召回 dev 集重新标定阈值；本次使用全文检索。"}]
        started = time.perf_counter()
        try:
            vector = self.gateway.embedding(self.embedding_text(text, participants), "retrieval_query")
            if self.index.dimension and len(vector) != self.index.dimension:
                raise ModelError("configuration", "embedding dimension changed")
            return vector, []
        except ModelError:
            return None, [{"code": "embedding_fallback", "message": "embedding 超时或失败，本次已退回全文检索。"}]
        finally:
            self.last_embedding_ms = (time.perf_counter() - started) * 1000

    def _hydrate(self, conn, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = {r["id"]: dict(r) for r in conn.execute(f"SELECT {MEMORY_COLUMNS} FROM memories m JOIN subjects s ON s.id=m.speaker_subject_id WHERE m.id IN ({placeholders})", ids)}
        for row in rows.values():
            row.update(about=[], tags=[], sources=[], _messages=set(), _other_source=False)
        for r in conn.execute(f"""SELECT ms.memory_id,s.id,s.name FROM memory_subjects ms JOIN subjects s ON s.id=ms.subject_id
            WHERE ms.memory_id IN ({placeholders}) ORDER BY s.id""", ids):
            rows[r["memory_id"]]["about"].append({"id": r["id"], "name": r["name"]})
        for r in conn.execute(f"SELECT memory_id,tag FROM memory_tags WHERE memory_id IN ({placeholders})", ids):
            rows[r["memory_id"]]["tags"].append(r["tag"])
        # UNION makes derived-source cycles finite. Only source metadata is read, never message prose.
        sql = f"""WITH RECURSIVE ancestry(root,id) AS (
            SELECT id,id FROM memories WHERE id IN ({placeholders})
            UNION SELECT a.root,s.source_memory_id FROM ancestry a JOIN sources s ON s.memory_id=a.id
                WHERE s.kind='memory' AND s.source_memory_id IS NOT NULL)
            SELECT a.root,s.kind,s.message_id,x.entry_id,x.occurred_at,e.name AS entry_name
            FROM ancestry a LEFT JOIN sources s ON s.memory_id=a.id
            LEFT JOIN messages x ON x.id=s.message_id LEFT JOIN entries e ON e.id=x.entry_id"""
        for source in conn.execute(sql, ids):
            row = rows[source["root"]]
            if source["kind"] == "message" and source["message_id"] is not None:
                row["_messages"].add(source["message_id"])
                summary = {k: source[k] for k in ("message_id", "entry_id", "entry_name", "occurred_at")}
                if summary not in row["sources"]:
                    row["sources"].append(summary)
            elif source["kind"] != "memory":
                row["_other_source"] = True
        return rows

    def _rank(self, conn, text: str, vector, *, participants=(), highlights=False, learning=False, limit=8, **filters) -> list[dict]:
        where, args = _filters(conn, **filters)
        scores: dict[int, float] = {}
        lexical_scores: dict[int, float] = {}
        vector_scores = {}
        analysis = analyze(conn, text, participants, self.settings['tokenizer'])
        participants = tuple(dict.fromkeys([*participants, *analysis.people])) if learning else analysis.people
        tokens = analysis.tokens
        table = "memory_fts_" + self.settings["tokenizer"]
        if tokens:
            lexical = conn.execute(f"""SELECT m.id,f.content,f.tags,bm25({table}) AS rank FROM {table} f
                JOIN memories m ON m.id=f.rowid WHERE {table} MATCH ? AND {where}
                ORDER BY rank,m.id LIMIT ?""", [match_query(tokens), *args, CANDIDATES]).fetchall()
            for row in lexical:
                haystack = (row["content"] + " " + row["tags"]).casefold()
                present = set(haystack.split()) if self.settings["tokenizer"] == "jieba" else None
                hits = sum(t in present if present is not None else t in haystack for t in tokens)
                if hits:
                    lexical_scores[row["id"]] = hits
            for rank, mid in enumerate(lexical_scores, 1):
                scores[mid] = 1 / (RRF_K + rank)
        if vector is not None and self.index:
            vector_scores = self.index.scores(vector, include_forgotten=bool(filters.get("include_forgotten")))
            # Apply structured filters before truncating vector ranks.
            if any(filters.get(k) for k in ("people", "kinds", "stances", "time_from", "time_to")):
                allowed = {r[0] for r in conn.execute("SELECT m.id FROM memories m WHERE " + where, args)}
                vector_scores = {mid: v for mid, v in vector_scores.items() if mid in allowed}
            best = max((pair[0] for pair in vector_scores.values()), default=0)
            cutoff = max(self.settings['vector_min'], best * self.settings['vector_relative'])
            ranked = sorted(((mid, pair) for mid, pair in vector_scores.items() if pair[0] >= cutoff), key=lambda p: (-p[1][0], p[0]))
            for rank, (mid, _) in enumerate(ranked[:CANDIDATES], 1):
                scores[mid] = scores.get(mid, 0) + self.settings['vector_weight'] / (RRF_K + rank)
        highlights_ids = []
        if highlights and (learning or not tokens):
            for sid in participants:
                highlights_ids.extend(r[0] for r in conn.execute("""SELECT m.id FROM memory_subjects ms
                    JOIN memories m ON m.id=ms.memory_id WHERE ms.subject_id=? AND m.lifecycle='active'
                    ORDER BY m.importance DESC,m.id LIMIT 3""", (sid,)))
        if not text.strip() and not highlights:
            for rank, row in enumerate(conn.execute("SELECT m.id FROM memories m WHERE " + where + " ORDER BY m.importance DESC,m.id LIMIT ?", [*args, CANDIDATES]), 1):
                scores[row[0]] = 1 / (RRF_K + rank)
        ids = list(dict.fromkeys([*highlights_ids, *scores]))
        rows = self._hydrate(conn, ids)
        clock = self.clock()
        allowed_lifecycles = {"active", "forgotten"} if filters.get("include_forgotten") else {"active"}
        for mid, memory in list(rows.items()):
            if not learning and not attribute_evidence(text, memory['content']):
                del rows[mid]
                continue
            if memory["lifecycle"] not in allowed_lifecycles:
                del rows[mid]
                continue
            if mid in vector_scores and vector_scores[mid][1] != memory["revision"] and mid not in lexical_scores and mid not in highlights_ids:
                del rows[mid]
                continue
            try:
                event = datetime.fromisoformat(memory["event_time"] or memory["created_at"])
                event = event.replace(tzinfo=timezone.utc) if event.tzinfo is None else event
                recency = 1 / (1 + abs((clock - event).total_seconds()) / (86400 * 30))
            except ValueError:
                recency = 0
            related_person = bool(set(participants) & {p["id"] for p in memory["about"]})
            if participants and not related_person:
                # A different person's same topic is a particularly dangerous
                # false answer. Both channels must independently be strong.
                similarity, revision = vector_scores.get(mid, (0, 0))
                if lexical_scores.get(mid, 0) < 2 or similarity < max(.7, self.settings['vector_min']) or revision != memory['revision']:
                    del rows[mid]
                    continue
            weight = 1 + .05 * memory["importance"] / 100 + .03 * memory["retention"] / 100 + .02 * recency + .05 * related_person
            memory["score"] = scores.get(mid, 0) * weight
        highlight_set = set(highlights_ids)
        ordered = [rows[mid] for mid in dict.fromkeys(highlights_ids) if mid in rows]
        ordered.extend(sorted((m for mid, m in rows.items() if mid not in highlight_set), key=lambda m: (-m["score"], m["id"])))
        return ordered

    def _duplicate(self, a: dict, b: dict) -> bool:
        if any(a[k] != b[k] for k in ("speaker_subject_id", "stance", "world", "event_time")) or a["about"] != b["about"]:
            return False
        normalize = lambda text: re.sub(r"[\W_]+", "", text.casefold())
        left, right = normalize(a["content"]), normalize(b["content"])
        # Similar numbers or opposite judgments are not duplicates.
        if re.findall(r"\d+", left) != re.findall(r"\d+", right):
            return False
        if re.findall(r"不|没|无|未|否", left) != re.findall(r"不|没|无|未|否", right):
            return False
        return difflib.SequenceMatcher(None, left, right).ratio() >= .88 or bool(self.index and self.index.similarity(a["id"], b["id"], (a['revision'], b['revision'])) >= .96)

    @staticmethod
    def _public(memory: dict) -> dict:
        return {k: v for k, v in memory.items() if not k.startswith("_")}

    def _select(self, candidates: list[dict], *, limit: int, token_budget: int | None = None,
                recent_ids=(), known_ids=()) -> list[dict]:
        selected = []
        recent_ids, known_ids = set(recent_ids), set(known_ids)
        for m in candidates:
            if len(selected) >= limit:
                break
            if m["id"] in known_ids or (m["_messages"] and not m["_other_source"] and m["_messages"] <= recent_ids):
                continue
            if any(self._duplicate(m, old) for old in selected):
                continue
            payload = [self._public(old) for old in selected] + [self._public(m)]
            if token_budget is not None and estimate_tokens(dumps(payload)) > token_budget:
                continue
            selected.append(m)
        return [self._public(m) for m in selected]

    def _record(self, entry_id: str | None, request: dict, memories: list[dict]) -> str:
        recall_id = uuid.uuid4().hex
        with self.store.write() as conn:
            conn.execute("INSERT INTO recalls VALUES(?,?,?,?)", (recall_id, entry_id, dumps(request), now()))
            conn.executemany("INSERT INTO recall_items(recall_id,memory_id,revision) VALUES(?,?,?)",
                             [(recall_id, m["id"], m["revision"]) for m in memories])
        return recall_id

    def search(self, *, text: str = "", people=(), kinds=(), stances=(), time_from=None, time_to=None,
               include_forgotten=False, limit=8, include_goals=False, include_state=False) -> dict:
        vector, hints = self._query_vector(text, people)
        request = dict(text=text, people=list(people), kinds=list(kinds), stances=list(stances), time_from=time_from,
                       time_to=time_to, include_forgotten=include_forgotten, limit=limit)
        with self.store.read() as conn:
            candidates = self._rank(conn, text, vector, **{k: v for k, v in request.items() if k != "text"})
            memories = self._select(candidates, limit=limit)
            hints.extend(self._model_hints(conn))
            result = {"memories": memories, "hints": hints}
            if include_goals:
                result["goals"] = self._goals(conn, None, 10)
            if include_state:
                result["state"] = {}
        result["recall_id"] = self._record(None, request, memories)
        return result

    def learning_context(self, text: str, participants: list[str], limit: int = 15) -> list[dict]:
        vector, _ = self._query_vector(text, participants)
        with self.store.read() as conn:
            candidates = self._rank(conn, text, vector, participants=_people(conn, participants), highlights=True, learning=True)
            return self._select(candidates, limit=limit)

    @staticmethod
    def _model_hints(conn) -> list[dict]:
        failed = [m for m in latest_models(conn) if m["result_category"] != "success"]
        return [{"code": "model_service", "message": "部分模型用途最近一次调用失败。", "calls": failed}] if failed else []

    @staticmethod
    def _goals(conn, entry_id: str | None, limit: int) -> list[dict]:
        query = "SELECT id,content,kind,state,deadline,entry_id FROM goals WHERE state IN ('open','in_progress')"
        args = []
        if entry_id is not None:
            query += " AND (entry_id=? OR entry_id IS NULL)"
            args.append(entry_id)
        goals = [dict(r) for r in conn.execute(query + " ORDER BY id LIMIT ?", [*args, limit])]
        for g in goals:
            try:
                due = datetime.fromisoformat(g["deadline"]) if g["deadline"] else None
                due = due.replace(tzinfo=timezone.utc) if due and due.tzinfo is None else due
                remaining = (due - datetime.now(timezone.utc)).total_seconds() if due else None
            except ValueError:
                remaining = None
            g["overdue"] = remaining is not None and remaining < 0
            g["due_soon"] = remaining is not None and 0 <= remaining <= 86400
        return goals

    def prepare(self, entry_id: str, *, text: str | None = None, participants: list[str] | None = None,
                known_memory_ids=(), recent_limit=20, memory_limit=8, token_budget=1500, goal_limit=10) -> dict:
        # A preliminary read only supplies an embedding hint. No transaction spans a model call.
        if text is None:
            with self.store.read() as conn:
                text = "\n".join(r[0] for r in reversed(conn.execute("SELECT content FROM messages WHERE entry_id=? ORDER BY id DESC LIMIT 5", (entry_id,)).fetchall()))
        # Resolve explicit participants for pronouns before the network call.
        # The final result still uses exactly one independent read snapshot.
        vector, hints = self._query_vector(text, participants or ())
        request = dict(text=text, participants=participants, known_memory_ids=list(known_memory_ids), recent_limit=recent_limit,
                       memory_limit=memory_limit, token_budget=token_budget, goal_limit=goal_limit)
        with self.store.read() as conn:
            if not conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone():
                raise KeyError(entry_id)
            recent = [dict(r) for r in reversed(conn.execute("""SELECT m.*,s.name AS sender_name,q.name AS quote_author_name
                FROM messages m JOIN subjects s ON s.id=m.sender_subject_id LEFT JOIN subjects q ON q.id=m.quote_author_subject_id
                WHERE m.entry_id=? ORDER BY m.id DESC LIMIT ?""", (entry_id, recent_limit)).fetchall())]
            for m in recent:
                m["unlearned"] = m["learning_state"] != "learned"
            participant_ids = _people(conn, participants) if participants is not None else list(dict.fromkeys(m["sender_subject_id"] for m in recent if m["sender_subject_id"] not in ("self", "scene")))
            candidates = self._rank(conn, text, vector, participants=participant_ids, highlights=True)
            memories = self._select(candidates, limit=min(memory_limit, 8), token_budget=min(token_budget, 1500),
                                    recent_ids=[m["id"] for m in recent], known_ids=known_memory_ids)
            persona = conn.execute("SELECT id AS version,content,created_at AS generated_at FROM persona_versions WHERE is_current=1 ORDER BY id DESC LIMIT 1").fetchone()
            gaps = [dict(r) for r in conn.execute("SELECT started_at,ended_at,reason FROM memory_gaps WHERE entry_id=? ORDER BY id", (entry_id,))]
            if gaps:
                hints.append({"code": "memory_gaps", "message": "本入口有尚未记住的消息区间。", "gaps": gaps})
            hints.extend(self._model_hints(conn))
            result = {"persona": dict(persona) if persona else {"version": None, "content": "", "generated_at": None},
                      "memories": memories, "recent_messages": recent, "state": {},
                      "goals": self._goals(conn, entry_id, min(goal_limit, 10)), "hints": hints}
        result["recall_id"] = self._record(entry_id, request, memories)
        return result

    def feedback(self, recall_id: str, memory_ids: list[int]) -> dict:
        ids = list(dict.fromkeys(memory_ids))
        with self.store.write() as conn:
            recall = conn.execute("SELECT created_at FROM recalls WHERE id=?", (recall_id,)).fetchone()
            if not recall:
                raise KeyError(recall_id)
            if datetime.now(timezone.utc) - datetime.fromisoformat(recall[0]) > timedelta(hours=24):
                raise ValueError("recall expired (24 hours)")
            for mid in ids:
                if not conn.execute("SELECT 1 FROM recall_items WHERE recall_id=? AND memory_id=?", (recall_id, mid)).fetchone():
                    raise ValueError("memory was not returned by this recall")
                if not conn.execute("SELECT 1 FROM memories WHERE id=? AND lifecycle!='deleted'", (mid,)).fetchone():
                    raise KeyError(mid)
            strengthened = []
            for mid in ids:
                changed = conn.execute("UPDATE recall_items SET used_at=? WHERE recall_id=? AND memory_id=? AND used_at IS NULL", (now(), recall_id, mid)).rowcount
                if changed:
                    conn.execute("UPDATE memories SET retention=MIN(100,retention+8) WHERE id=? AND lifecycle!='deleted'", (mid,))
                    strengthened.append(mid)
        return {"recall_id": recall_id, "accepted": ids, "strengthened": strengthened}
