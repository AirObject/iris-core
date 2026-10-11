"""Learning-only PR #4 selection, independent of reply preparation calibration."""
from datetime import datetime, timezone

from .memory_ops import Visibility
from .retrieval import Retrieval, _filters, RRF_K, CANDIDATES
from .search_text import learning_query_terms, match_query

LEARNING_DEFAULTS = {"tokenizer": "jieba", "vector_min": .65, "lexical_min": .5,
                     "embedding_model": "doubao-embedding-vision", "dtype": "float32",
                     "embedding_dimensions": 2048, "query_prefix": ""}


class LearningRetrieval(Retrieval):
    def _settings(self):
        return {**LEARNING_DEFAULTS, **self.store.setting('learning_retrieval', {})}

    def embedding_text(self, text, participants=(), *, entry_kind=None):
        return text

    def context(self, text, participants, limit, *, entry_id=None):
        vector, _ = self._query_vector(text)
        with self.store.read() as conn:
            candidates = self._rank(conn, text, vector, participants=participants, highlights=True, entry_id=entry_id)
            return self._select(candidates, limit=limit)

    def _rank(self, conn, text: str, vector, *, participants=(), highlights=False, limit=8, entry_id=None, **filters) -> list[dict]:
        where, args = _filters(conn, **filters)
        visibility = Visibility(conn)
        where += " AND " + visibility.memory_sql(entry_id)
        scores: dict[int, float] = {}
        lexical_scores: dict[int, float] = {}
        vector_scores = {}
        tokens = learning_query_terms(text, self.settings["tokenizer"])
        table = "memory_fts_" + self.settings["tokenizer"]
        if tokens:
            lexical = conn.execute(f"""SELECT m.id,f.content,f.tags,bm25({table}) AS rank FROM {table} f
                JOIN memories m ON m.id=f.rowid WHERE {table} MATCH ? AND {where}
                ORDER BY rank,m.id LIMIT ?""", [match_query(tokens), *args, CANDIDATES]).fetchall()
            for row in lexical:
                haystack = (row["content"] + " " + row["tags"]).casefold()
                present = set(haystack.split()) if self.settings["tokenizer"] == "jieba" else None
                coverage = sum(t in present if present is not None else t in haystack for t in tokens) / len(tokens)
                if coverage >= self.settings["lexical_min"]:
                    lexical_scores[row["id"]] = coverage
            for rank, mid in enumerate(lexical_scores, 1):
                scores[mid] = 1 / (RRF_K + rank)
        if vector is not None and self.index:
            vector_scores = self.index.scores(vector, include_forgotten=bool(filters.get("include_forgotten")))
            # Apply structured filters before truncating vector ranks.
            if any(filters.get(k) for k in ("people", "kinds", "stances", "time_from", "time_to")):
                allowed = {r[0] for r in conn.execute("SELECT m.id FROM memories m WHERE " + where, args)}
                vector_scores = {mid: v for mid, v in vector_scores.items() if mid in allowed}
            ranked = sorted(((mid, pair) for mid, pair in vector_scores.items() if pair[0] >= self.settings["vector_min"] and visibility.memory_visible(mid, entry_id)), key=lambda p: (-p[1][0], p[0]))
            for rank, (mid, _) in enumerate(ranked[:CANDIDATES], 1):
                scores[mid] = scores.get(mid, 0) + 1 / (RRF_K + rank)
        highlights_ids = []
        if highlights:
            for sid in participants:
                highlights_ids.extend(r[0] for r in conn.execute(f"""SELECT m.id FROM memory_subjects ms
                    JOIN memories m ON m.id=ms.memory_id WHERE ms.subject_id=? AND {where}
                    ORDER BY m.importance DESC,m.id LIMIT 3""", (sid,*args)))
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
            related_person = bool(set(participants) & {p["id"] for p in memory["about"]})
            weight = 1 + .05 * memory["importance"] / 100 + .03 * memory["retention"] / 100 + .02 * recency + .05 * related_person
            memory["score"] = scores.get(mid, 0) * weight
        highlight_set = set(highlights_ids)
        ordered = [rows[mid] for mid in dict.fromkeys(highlights_ids) if mid in rows]
        ordered.extend(sorted((m for mid, m in rows.items() if mid not in highlight_set), key=lambda m: (-m["score"], m["id"])))
        return ordered
