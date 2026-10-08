"""Shared retrieval, snapshot reply preparation, and idempotent use feedback."""
from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from itertools import islice, zip_longest
from typing import Any

from .db import Store, dumps, now
from .models import Gateway, ModelError
from .people import annotate_memories, canonical_subject
from .queue import estimate_tokens
from .search_text import match_query, terms, words
from .query_analysis import SubjectNames, analyze

RRF_K = 60
CANDIDATES = 200
CONVERSATION_QUERIES = ('latest_1', 'latest_2', 'latest_3', 'window_5',
                        'session_6', 'speaker_6', 'adaptive_6')
CONVERSATION_QUERY = 'adaptive_6'
# Grammatical references only; no domain vocabulary or answer-attribute list.
CONTEXT_REFERENCE = re.compile(r'(?:[他她它]们?|[这那](?:个|些|里|边|儿|[位家段件种次份条张台杯本只座辆部间双场])?)')
DEFAULTS = json.loads(files("iris").joinpath("retrieval_defaults.json").read_text(encoding="utf-8"))
MEMORY_COLUMNS = """m.id,m.content,m.kind,m.speaker_subject_id,m.stance,m.belief,m.importance,m.retention,
    m.event_time,m.lifecycle,m.revision,m.entry_id,m.world,m.created_at,m.updated_at,s.name AS speaker_name"""


def _people(conn, values: list[str]) -> list[str]:
    result = []
    for value in values:
        exact_id = conn.execute("SELECT id FROM subjects WHERE id=?", (value,)).fetchone()
        if exact_id:
            result.append(canonical_subject(conn, value))
            continue
        rows = conn.execute("""SELECT DISTINCT s.id FROM subjects s LEFT JOIN subject_aliases a ON a.subject_id=s.id
            AND a.folded_into IS NULL WHERE s.merged_into IS NULL AND (s.id=? OR s.name=? OR a.alias=?)""", (value, value, value)).fetchall()
        if len(rows) > 1:
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
                 query_prefix: str | None = None, lexical_min: float | None = None,
                 lexical_max_df: int | None = None, fallback_tokenizer: str | None = None,
                 fallback_lexical_min: float | None = None, fallback_lexical_max_df: int | None = None,
                 conversation_query: str | None = None, clock=None):
        self.store, self.gateway = store, gateway
        self.conversation_query = conversation_query or CONVERSATION_QUERY
        if self.conversation_query not in CONVERSATION_QUERIES:
            raise ValueError('unknown conversation query composition')
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.overrides = {k: v for k, v in dict(tokenizer=tokenizer, vector_min=vector_min,
                                              vector_relative=vector_relative, vector_weight=vector_weight,
                                              query_prefix=query_prefix, lexical_min=lexical_min,
                                              lexical_max_df=lexical_max_df, dtype=dtype,
                                              fallback_tokenizer=fallback_tokenizer,
                                              fallback_lexical_min=fallback_lexical_min,
                                              fallback_lexical_max_df=fallback_lexical_max_df).items() if v is not None}
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
        # New fallback defaults apply only to new databases. Even an empty
        # legacy settings object keeps the interpretation of its shared fields.
        for key in ('tokenizer', 'lexical_min', 'lexical_max_df'):
            saved.setdefault('fallback_' + key, saved.get(key, DEFAULTS[key]))
        return {**DEFAULTS, **saved, **self.overrides}

    def embedding_text(self, text: str, participants=(), *, entry_kind=None) -> str:
        with self.store.read() as conn:
            analysis = analyze(conn, text, tokenizer=self.settings['tokenizer'], entry_kind=entry_kind)
        return self.settings['query_prefix'] + analysis.text

    def _query_vector(self, text: str, participants=(), *, entry_kind=None):
        self.settings = self._settings()
        if self.settings["tokenizer"] not in ("jieba", "trigram"):
            raise ValueError("unknown FTS tokenizer")
        if self.model:
            self.index = self.store.vector_index(self.model, self.settings["dtype"])
        self.last_embedding_ms = 0.0
        if not text.strip() or not self.model:
            self._fulltext_fallback()
            return None, [{"code": "embedding_unconfigured", "message": "未配置 embedding，本次使用全文检索。"}] if text.strip() else []
        dimensions = self.gateway.configs['embedding'].dimensions
        if (self.settings["embedding_model"] != self.model or dimensions not in (None, self.settings['embedding_dimensions'])) and "vector_min" not in self.overrides:
            self._fulltext_fallback()
            return None, [{"code": "embedding_uncalibrated", "message": "embedding 模型已变化，需在召回 dev 集重新标定阈值；本次使用全文检索。"}]
        started = time.perf_counter()
        try:
            vector = self.gateway.embedding(self.embedding_text(text, participants, entry_kind=entry_kind), "retrieval_query")
            if self.index.dimension and len(vector) != self.index.dimension:
                raise ModelError("configuration", "embedding dimension changed")
            return vector, []
        except ModelError:
            self._fulltext_fallback()
            return None, [{"code": "embedding_fallback", "message": "embedding 超时或失败，本次已退回全文检索。"}]
        finally:
            self.last_embedding_ms = (time.perf_counter() - started) * 1000

    def _fulltext_fallback(self):
        # Explicit lane overrides remain useful for controlled comparisons.
        # Normal queries select all three independently calibrated FTS fields;
        # learning has no fallback fields and keeps its own settings unchanged.
        for key in ('tokenizer', 'lexical_min', 'lexical_max_df'):
            fallback = 'fallback_' + key
            if fallback in self.settings and (key not in self.overrides or fallback in self.overrides):
                self.settings[key] = self.settings[fallback]
        if self.settings['tokenizer'] not in ('jieba', 'trigram'):
            raise ValueError('unknown FTS tokenizer')

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

    def _rank(self, conn, text: str, vector, *, participants=(), highlights=False, entry_kind=None,
              anchor_text: str | None = None, limit=8, **filters) -> list[dict]:
        where, args = _filters(conn, **filters)
        scores: dict[int, float] = {}
        lexical_scores: dict[int, float] = {}
        vector_scores = {}
        analysis = analyze(conn, text, tokenizer=self.settings['tokenizer'], entry_kind=entry_kind)
        # Only ranked lane candidates reach the name matcher. Stream until the
        # existing per-lane cap is filled, so unrelated people cannot exhaust it.
        # Cache the predicate across FTS, vector and participant highlights.
        anchor_matches = {}
        # Automatic preparation may use earlier prose for context, but only
        # the latest other person's message can restrict the subject scope.
        anchor_analysis = (analysis if anchor_text is None else
                           analyze(conn, anchor_text, tokenizer=self.settings['tokenizer'], entry_kind=entry_kind))
        anchors = set(anchor_analysis.people)
        # Reject prose without any anchor label before the full longest-label
        # matcher. Same regex case rules; the final matcher still enforces word
        # boundaries and prevents a short name matching a longer known name.
        anchor_label = re.compile('|'.join(re.escape(label) for label, ids in analysis.names.labels.items()
                                          if anchors.intersection(ids)), re.I) if anchors else None
        direct_anchors = set()
        if anchors:
            marks = ','.join('?' for _ in anchors)
            direct_anchors = {row[0] for row in conn.execute(f"""SELECT memory_id FROM memory_subjects
                WHERE subject_id IN ({marks}) UNION SELECT id FROM memories
                WHERE speaker_subject_id IN ({marks})""", [*anchors, *anchors])}

        def matches_anchor(mid, content):
            if mid not in anchor_matches:
                anchor_matches[mid] = bool(mid in direct_anchors or
                    (anchor_label.search(content) and anchors.intersection(analysis.names.mentioned(content))))
            return anchor_matches[mid]

        if anchors:
            # The same exact, cached predicate filters FTS matches before BM25
            # sorting. Only FTS-matching rows reach it, and index statistics are
            # unchanged. This avoids sorting every person's matches and then
            # repeatedly hydrating batches merely to find 200 anchored results.
            conn.create_function('iris_anchor', 2, matches_anchor, deterministic=True)

        def anchored(candidates):
            if not anchors:
                yield from candidates
                return
            candidates = iter(candidates)
            while batch := list(islice(candidates, CANDIDATES)):
                missing = list(dict.fromkeys(row[0] for row in batch if row[0] not in anchor_matches))
                if missing:
                    ids = ','.join('?' for _ in missing)
                    matches = conn.execute(f"""SELECT m.id,m.content FROM memories m
                        WHERE m.id IN ({ids}) AND {where}""", [*missing, *args])
                    for row in matches:
                        matches_anchor(row['id'], row['content'])
                    for mid in missing:
                        anchor_matches.setdefault(mid, False)
                yield from (row for row in batch if anchor_matches[row[0]])
        boosted = set(participants) | ({'self'} if analysis.refers_to_self else set())
        def lexical_lane(tokenizer, tokens, *, phrases=False):
            if not tokens:
                return []
            table = "memory_fts_" + tokenizer
            anchor_where = ' AND iris_anchor(m.id,m.content)' if anchors else ''
            lexical = conn.execute(f"""SELECT m.id,bm25({table}) AS rank FROM {table} f
                JOIN memories m ON m.id=f.rowid WHERE {table} MATCH ? AND {where}{anchor_where}
                ORDER BY rank,m.id LIMIT ?""", [match_query(tokens), *args, CANDIDATES])
            ids = [row[0] for row in lexical]
            if phrases:
                # MATCH checks the indexed name phrase; anchored checks the
                # original prose with longest-label and word-boundary rules.
                return ids
            marks = ','.join('?' for _ in ids)
            lexical_text = {row[0]: row for row in conn.execute(
                f'SELECT rowid,content,tags FROM {table} WHERE rowid IN ({marks})', ids)}
            # A rare long fragment can still identify the subject of a long
            # conversation. Count across the whole index, before filters and
            # the candidate cap; fetching at most max_df+1 avoids a table scan.
            # One/two-character terms never bypass whole-query coverage.
            rare_ids = set()
            max_df = self.settings['lexical_max_df']
            if ids and max_df:
                for token in tokens:
                    if len(token) < 3:
                        continue
                    matches = conn.execute(f'SELECT rowid FROM {table} WHERE {table} MATCH ? LIMIT ?',
                                           (match_query([token]), max_df + 1)).fetchall()
                    if len(matches) <= max_df:
                        rare_ids.update(row[0] for row in matches)
            kept = []
            for mid in ids:
                row = lexical_text[mid]
                haystack = (row["content"] + " " + row["tags"]).casefold()
                present = set(haystack.split()) if tokenizer == "jieba" else None
                # Measure against the whole non-name query, not just the
                # short-word lane. A long query cannot pass on one incidental
                # two-character hit; a single short keyword still covers 100%.
                coverage = sum(t in present if present is not None else t in haystack
                               for t in analysis.coverage_tokens) / max(1, len(analysis.coverage_tokens))
                if coverage >= self.settings['lexical_min'] or mid in rare_ids:
                    kept.append(mid)
            return kept

        lanes = [lexical_lane(self.settings['tokenizer'], analysis.tokens)]
        if analysis.short_tokens:
            # Trigrams cannot match one/two-character terms. Reuse the existing
            # jieba index, including when longer terms are present as well.
            lanes.append(lexical_lane('jieba', analysis.short_tokens))
        if anchor_analysis.name_only:
            marks = ','.join('?' for _ in anchors)
            direct = conn.execute(f"""SELECT m.id FROM memories m WHERE m.id IN (
                SELECT memory_id FROM memory_subjects WHERE subject_id IN ({marks})
                UNION SELECT id FROM memories WHERE speaker_subject_id IN ({marks})) AND {where}
                ORDER BY m.importance DESC,m.id LIMIT ?""", [*anchors, *anchors, *args, CANDIDATES])
            lanes.append([row[0] for row in direct])
            labels = list(dict.fromkeys(' '.join(terms(label)) for label, ids in analysis.names.labels.items()
                                       if anchors.intersection(ids) and terms(label)))
            lanes.append(lexical_lane('jieba', labels, phrases=True))
        # Interleave lane ranks, deduplicate, then keep the existing total cap.
        # Each memory receives one lexical rank, never an extra score for
        # matching both indexes; BM25 values across indexes are not comparable.
        lexical_ids = dict.fromkeys(mid for row in zip_longest(*lanes) for mid in row if mid is not None)
        for rank, mid in enumerate(islice(lexical_ids, CANDIDATES), 1):
            lexical_scores[mid] = scores[mid] = 1 / (RRF_K + rank)
        if vector is not None and self.index:
            vector_scores = self.index.scores(vector, include_forgotten=bool(filters.get("include_forgotten")))
            # Apply structured filters before truncating vector ranks.
            if any(filters.get(k) for k in ("people", "kinds", "stances", "time_from", "time_to")):
                allowed = {r[0] for r in conn.execute("SELECT m.id FROM memories m WHERE " + where, args)}
                vector_scores = {mid: v for mid, v in vector_scores.items() if mid in allowed}
            # The absolute floor defines vector candidates before any prose is
            # read. The relative cutoff still uses the best ANCHORED candidate.
            ranked = sorted(((mid, pair) for mid, pair in vector_scores.items()
                             if pair[0] >= self.settings['vector_min']), key=lambda p: (-p[1][0], p[0]))
            cutoff = None
            for rank, (mid, pair) in enumerate(islice(anchored(ranked), CANDIDATES), 1):
                if cutoff is None:
                    cutoff = max(self.settings['vector_min'], pair[0] * self.settings['vector_relative'])
                if pair[0] < cutoff:
                    break
                scores[mid] = scores.get(mid, 0) + self.settings['vector_weight'] / (RRF_K + rank)
        highlights_ids = []
        if highlights:
            queues = []
            for sid in dict.fromkeys(participants):
                if sid in ('self', 'scene'):
                    continue
                queue = conn.execute(f"""SELECT m.id FROM memory_subjects ms
                    JOIN memories m ON m.id=ms.memory_id WHERE ms.subject_id=? AND {where}
                    ORDER BY m.importance DESC,m.id LIMIT ?""", [sid, *args, -1 if anchors else CANDIDATES])
                queues.append([r[0] for r in islice(anchored(queue), CANDIDATES)])
            highlights_ids = list(dict.fromkeys(mid for row in zip_longest(*queues) for mid in row if mid is not None))
        if not text.strip() and not highlights:
            for rank, row in enumerate(conn.execute("SELECT m.id FROM memories m WHERE " + where + " ORDER BY m.importance DESC,m.id LIMIT ?", [*args, CANDIDATES]), 1):
                scores[row[0]] = 1 / (RRF_K + rank)
        ids = list(dict.fromkeys([*highlights_ids, *scores]))
        rows = self._hydrate(conn, ids)
        clock = self.clock()
        allowed_lifecycles = {"active", "forgotten"} if filters.get("include_forgotten") else {"active"}
        for mid, memory in list(rows.items()):
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
            related_person = bool(boosted & {p["id"] for p in memory["about"]})
            weight = 1 + .05 * memory["importance"] / 100 + .03 * memory["retention"] / 100 + .02 * recency + .05 * related_person
            memory["score"] = scores.get(mid, 0) * weight
            memory['reason'] = 'relevant' if mid in scores else 'person_highlight'
        ordered = sorted((m for mid, m in rows.items() if mid in scores), key=lambda m: (-m['score'], m['id']))
        ordered.extend(rows[mid] for mid in highlights_ids if mid in rows and mid not in scores)
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
        highlight_count = 0
        recent_ids, known_ids = set(recent_ids), set(known_ids)
        for m in candidates:
            if len(selected) >= limit:
                break
            if m.get('reason') == 'person_highlight' and highlight_count >= 3:
                continue
            if m["id"] in known_ids or (m["_messages"] and not m["_other_source"] and m["_messages"] <= recent_ids):
                continue
            if any(self._duplicate(m, old) for old in selected):
                continue
            payload = [self._public(old) for old in selected] + [self._public(m)]
            if token_budget is not None and estimate_tokens(dumps(payload)) > token_budget:
                continue
            selected.append(m)
            highlight_count += m.get('reason') == 'person_highlight'
        return [self._public(m) for m in selected]

    def _record(self, entry_id: str | None, request: dict, memories: list[dict], *, _conn=None) -> str:
        if _conn is None:
            with self.store.write() as conn:
                return self._record(entry_id, request, memories, _conn=conn)
        recall_id = uuid.uuid4().hex
        _conn.execute("INSERT INTO recalls VALUES(?,?,?,?)", (recall_id, entry_id, dumps(request), now()))
        _conn.executemany("INSERT INTO recall_items(recall_id,memory_id,revision) VALUES(?,?,?)",
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
            annotate_memories(conn, memories)
            hints.extend(self._model_hints(conn))
            result = {"memories": memories, "hints": hints}
            if include_goals:
                result["goals"] = self._goals(conn, None, 10)
            if include_state:
                result["state"] = {}
        result["recall_id"] = self._record(None, request, memories)
        return result

    def learning_context(self, text: str, participants: list[str], limit: int = 15) -> list[dict]:
        from .learning_retrieval import LearningRetrieval
        return LearningRetrieval(self.store, self.gateway, clock=self.clock).context(text, participants, limit)

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

    def _prepare_context(self, entry_id: str, text: str | None) -> tuple[str, str, str | None]:
        """Freeze composition and anchor prose together before the model call."""
        with self.store.read() as conn:
            entry = conn.execute('SELECT kind FROM entries WHERE id=?', (entry_id,)).fetchone()
            if entry is None:
                raise KeyError(entry_id)
            if text is not None:
                return text, entry['kind'], None
            other = "kind='message' AND sender_subject_id NOT IN ('self','scene')"
            if self.conversation_query == 'window_5':
                recent = conn.execute("""SELECT content,kind,sender_subject_id FROM messages
                    WHERE entry_id=? ORDER BY id DESC LIMIT 5""", (entry_id,)).fetchall()
                selected = [r for r in recent if r['kind'] == 'message' and r['sender_subject_id'] not in ('self', 'scene')]
                latest = conn.execute(f"SELECT content FROM messages WHERE entry_id=? AND {other} ORDER BY id DESC LIMIT 1",
                                      (entry_id,)).fetchone()
                anchor_text = latest[0] if latest else ''
            elif self.conversation_query.startswith('latest_'):
                count = int(self.conversation_query.rsplit('_', 1)[1])
                selected = conn.execute(f"SELECT content FROM messages WHERE entry_id=? AND {other} ORDER BY id DESC LIMIT ?",
                                        (entry_id, count)).fetchall()
                anchor_text = selected[0]['content'] if selected else ''
            else:
                recent = conn.execute(f"""SELECT content,sender_subject_id,occurred_at FROM messages
                    WHERE entry_id=? AND {other} ORDER BY id DESC LIMIT 20""", (entry_id,)).fetchall()
                anchor_text = recent[0]['content'] if recent else ''
                selected = self._conversation_suffix(conn, recent, entry['kind'])
            return "\n".join(r['content'] for r in reversed(selected)), entry['kind'], anchor_text

    def _conversation_suffix(self, conn, recent, entry_kind):
        """Bound context by elapsed message time; never infer a topic from names."""
        if not recent:
            return []

        def timestamp(row):
            stamp = datetime.fromisoformat(row['occurred_at'])
            return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp

        selected = [recent[0]]
        try:
            latest = previous = timestamp(recent[0])
            for row in recent[1:]:
                stamp = timestamp(row)
                if stamp > previous or (latest - stamp).total_seconds() > 300:
                    break
                selected.append(row)
                previous = stamp
        except (TypeError, ValueError):
            # Keep the valid suffix, including the latest message even if its
            # own timestamp is invalid. Do not cross an unknown time boundary.
            pass
        if len(selected) <= 2:
            return selected
        if self.conversation_query == 'speaker_6':
            speaker = recent[0]['sender_subject_id']
            own = [row for row in selected if row['sender_subject_id'] == speaker]
            if len(own) > 1:
                selected = [row for i, row in enumerate(selected)
                            if row['sender_subject_id'] == speaker or i == 1]
        elif self.conversation_query == 'adaptive_6':
            text = recent[0]['content']
            analysis = analyze(conn, text, entry_kind=entry_kind)
            reference = any(CONTEXT_REFERENCE.fullmatch(word) for word in words(text))
            if len(analysis.coverage_tokens) >= 6 and not reference:
                return selected[:1]
        return selected[:6]

    def prepare_query(self, entry_id: str, text: str | None = None) -> tuple[str, str]:
        """The same query derivation is used by prepare and evaluation prefetch."""
        query, kind, _ = self._prepare_context(entry_id, text)
        return query, kind

    def prepare(self, entry_id: str, *, text: str | None = None, participants: list[str] | None = None,
                known_memory_ids=(), recent_limit=20, memory_limit=8, token_budget=1500, goal_limit=10,
                judge=True, judge_budget_seconds=10.0) -> dict:
        from .recall_judge import judge as apply_judgment, validate_budget
        validate_budget(judge_budget_seconds)
        if type(judge) is not bool:
            raise ValueError('judge must be a boolean')
        known_memory_ids = tuple(known_memory_ids)
        participants = list(participants) if participants is not None else None
        request = dict(text=text, participants=participants, known_memory_ids=list(known_memory_ids), recent_limit=recent_limit,
                       memory_limit=memory_limit, token_budget=token_budget, goal_limit=goal_limit,
                       judge=judge, judge_budget_seconds=judge_budget_seconds)
        # No read or write transaction spans the model call. The final response
        # uses one independent snapshot; arrivals during embedding are allowed.
        text, entry_kind, anchor_text = self._prepare_context(entry_id, text)
        vector, hints = self._query_vector(text, entry_kind=entry_kind)
        with self.store.read() as conn:
            if not conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone():
                raise KeyError(entry_id)
            recent = self._recent(conn, entry_id, recent_limit)
            participant_ids = _people(conn, participants) if participants is not None else list(dict.fromkeys(
                canonical_subject(conn, r[0]) for r in conn.execute('SELECT sender_subject_id FROM messages WHERE entry_id=? ORDER BY id DESC LIMIT 20', (entry_id,))
                if r[0] not in ('self', 'scene')))
            candidates = self._rank(conn, text, vector, participants=participant_ids, highlights=True,
                                    entry_kind=entry_kind, anchor_text=anchor_text)
            memories = self._select(candidates, limit=min(memory_limit, 8), token_budget=min(token_budget, 1500),
                                    recent_ids=[m["id"] for m in recent], known_ids=known_memory_ids)
            aliases = self._judge_aliases(conn, memories, participant_ids, recent, text, entry_kind)
            persona = conn.execute("SELECT id AS version,content,created_at AS generated_at FROM persona_versions WHERE is_current=1 ORDER BY id DESC LIMIT 1").fetchone()
            gaps = [dict(r) for r in conn.execute("SELECT started_at,ended_at,reason FROM memory_gaps WHERE entry_id=? ORDER BY id", (entry_id,))]
            if gaps:
                hints.append({"code": "memory_gaps", "message": "本入口有尚未记住的消息区间。", "gaps": gaps})
            hints.extend(self._model_hints(conn))
            result = {"persona": dict(persona) if persona else {"version": None, "content": "", "generated_at": None},
                      "memories": memories, "recent_messages": recent, "state": {},
                      "goals": self._goals(conn, entry_id, min(goal_limit, 10)), "hints": hints}
        diagnostic = apply_judgment(self.gateway, self.store, memories, {
            'role_name': self.store.setting('role_name', 'Iris'), 'query_hint': request['text'],
            'retrieval_query': text, 'participants': participants or [], 'subject_aliases': aliases,
            'recent_messages': [{k: m.get(k) for k in ('kind', 'sender_name', 'content', 'occurred_at')} for m in recent],
        }, enabled=judge, budget_seconds=judge_budget_seconds)
        self.last_judgment_network_ms = diagnostic['network_ms']
        # Recheck and record in the SAME short write transaction: no mutation can
        # slip between revision/lifecycle/context validation and feedback eligibility.
        with self.store.write() as conn:
            recent = self._recent(conn, entry_id, recent_limit)
            current = self._hydrate(conn, [m['id'] for m in memories])
            recent_ids = {m['id'] for m in recent}
            final, stale = [], []
            for old in memories:
                m = current.get(old['id'])
                if (not m or m['revision'] != old['revision'] or m['lifecycle'] != 'active'
                        or m['id'] in known_memory_ids
                        or (m['_messages'] and not m['_other_source'] and m['_messages'] <= recent_ids)):
                    stale.append(old['id'])
                    continue
                if old['id'] not in diagnostic['removed_memory_ids']:
                    final.append({**old, **self._public(m)})
            if stale:
                diagnostic['stale_memory_ids'] = stale
                diagnostic['removed_memory_ids'] = [mid for mid in diagnostic['removed_memory_ids'] if mid not in stale]
            # A setting switched off while the call was in flight must take effect.
            row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='recall_judge'").fetchone()
            if judge and row and not json.loads(row[0]).get('enabled', True):
                diagnostic.update(status='disabled', reason='configuration_disabled', removed_memory_ids=[])
                final = [{**old, **self._public(current[old['id']])} for old in memories if old['id'] not in stale]
            annotate_memories(conn, final)
            result.update(memories=final, recent_messages=recent, judgment=diagnostic)
            result['hints'].append({'code': 'recall_judgment', **diagnostic,
                'message': {'applied': '召回判断已完成。', 'disabled': '召回判断已关闭。',
                            'degraded': '召回判断降级，保留仍有效的原候选。'}[diagnostic['status']]})
            request['judgment'] = diagnostic
            result['recall_id'] = self._record(entry_id, request, final, _conn=conn)
        return result

    @staticmethod
    def _recent(conn, entry_id, limit):
        recent = [dict(r) for r in reversed(conn.execute("""SELECT m.*,s.name AS sender_name,q.name AS quote_author_name
            FROM messages m JOIN subjects s ON s.id=m.sender_subject_id LEFT JOIN subjects q ON q.id=m.quote_author_subject_id
            WHERE m.entry_id=? ORDER BY m.id DESC LIMIT ?""", (entry_id, limit)).fetchall())]
        for m in recent:
            m['unlearned'] = m['learning_state'] != 'learned'
        return recent

    @staticmethod
    def _judge_aliases(conn, memories, participant_ids, recent, text, entry_kind):
        # Restrict the payload, using the same longest-name/boundary matcher as
        # retrieval. A shared name still includes every matching subject.
        people = set(participant_ids) | {m['sender_subject_id'] for m in recent}
        people.update(SubjectNames(conn, ('主播',) if entry_kind == 'live' else ()).mentioned(text))
        for memory in [m for m in memories if m['reason'] == 'relevant'][:8]:
            people.add(memory['speaker_subject_id'])
            people.update(p['id'] for p in memory['about'])
        people = {canonical_subject(conn, sid) for sid in people}
        if not people:
            return []
        aliases = {}
        marks = ','.join('?' for _ in people)
        for row in conn.execute(f"""SELECT s.id,s.name,a.alias FROM subject_aliases a
                JOIN subjects s ON s.id=a.subject_id WHERE s.id IN ({marks}) AND a.folded_into IS NULL AND s.merged_into IS NULL ORDER BY s.id,a.alias""", sorted(people)):
            aliases.setdefault(row['id'], {'name': row['name'], 'aliases': []})['aliases'].append(row['alias'])
        return list(aliases.values())

    def feedback(self, recall_id: str, memory_ids: list[int]) -> dict:
        from .memory_ops import adjust_retention, lifecycle_settings, operation
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
                    adjust_retention(self.store, mid, lifecycle_settings(conn)["feedback_increment"], _conn=conn)
                    strengthened.append(mid)
            operation(conn, "feedback", "recall", recall_id, {"memory_ids": ids, "strengthened": strengthened}, actor="host")
        return {"recall_id": recall_id, "accepted": ids, "strengthened": strengthened}
