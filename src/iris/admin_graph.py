"""On-demand administrator graph. No storage, model calls or recall bookkeeping.

Memory edges use about only; node counts use about OR speaker, like the people
memory list. Identity/roleplay links are independent annotations, not invented
memory evidence. An entry filter never follows derived memory sources.
"""
from __future__ import annotations

import json
from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GraphQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str | None = Field(default=None, min_length=1, max_length=200)
    person_id: str | None = Field(default=None, min_length=1, max_length=200)
    hops: int = Field(default=1, ge=1, le=2)
    min_memories: int = Field(default=1, ge=1, le=1000000)
    node_limit: int = Field(default=60, ge=2, le=100)
    edge_limit: int = Field(default=100, ge=1, le=200)


class EdgeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=200)
    entry_id: str | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=30, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1000000)

    @model_validator(mode="after")
    def distinct(self):
        if self.source_id == self.target_id:
            raise ValueError("请选择两个不同的主体")
        return self


def _scope(conn, entry_id):
    where = "m.lifecycle='active' AND m.purged_at IS NULL"
    args = []
    if entry_id is not None:
        if conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone() is None:
            raise KeyError(entry_id)
        where += """ AND EXISTS (SELECT 1 FROM sources s JOIN messages msg ON msg.id=s.message_id
            WHERE s.memory_id=m.id AND s.kind='message' AND msg.entry_id=?)"""
        args.append(entry_id)
    return where, args


def _subject(conn, sid):
    row = conn.execute("SELECT id FROM subjects WHERE id=? AND id!='scene' AND merged_into IS NULL", (sid,)).fetchone()
    if row is None:
        raise KeyError(sid)


def _links(conn, entry_id, pair=None):
    # Read folded evidence through its current root. A denial/resolution on the
    # root suppresses every old row; roleplay retains direction and each world.
    pair_where, args = "", []
    if pair:
        pair_where = " AND MIN(l.subject_a,l.subject_b)=? AND MAX(l.subject_a,l.subject_b)=?"
        args.extend(sorted(pair))
    entry_where = ""
    if entry_id is not None:
        entry_where = " WHERE msg.entry_id=?"
        args.append(entry_id)
    rows = conn.execute(f"""WITH RECURSIVE roots AS (
        SELECT l.* FROM subject_links l
        JOIN subjects a ON a.id=l.subject_a JOIN subjects b ON b.id=l.subject_b
        WHERE l.folded_into IS NULL AND l.resolved_at IS NULL
        AND a.merged_into IS NULL AND b.merged_into IS NULL
        AND a.id!='scene' AND b.id!='scene' AND a.id!=b.id
        AND ((l.kind='same_as' AND l.status='possible') OR (l.kind='roleplay' AND l.status!='denied'))
        {pair_where}
    ), evidence(root_id,id,source_message_id,world) AS (
        SELECT id,id,source_message_id,world FROM roots
        UNION ALL
        SELECT e.root_id,l.id,l.source_message_id,l.world FROM subject_links l JOIN evidence e ON l.folded_into=e.id
    ) SELECT r.id,r.subject_a,r.subject_b,r.kind,r.belief,e.source_message_id,e.world
        FROM roots r JOIN evidence e ON e.root_id=r.id
        LEFT JOIN messages msg ON msg.id=e.source_message_id {entry_where} ORDER BY r.id,e.id""", args)
    links = {}
    for row in rows:
        item = links.setdefault(row["id"], {
            "id": row["id"], "kind": row["kind"], "source": row["subject_a"], "target": row["subject_b"],
            "belief": row["belief"], "worlds": [], "message_ids": [],
        })
        if row["world"] not in item["worlds"]:
            item["worlds"].append(row["world"])
        if row["source_message_id"] is not None and row["source_message_id"] not in item["message_ids"]:
            item["message_ids"].append(row["source_message_id"])
    return list(links.values())


def _public_link(link):
    return {**{k: v for k, v in link.items() if k != "message_ids"},
            "evidence_message_count": len(link["message_ids"])}


def _edge(a, b):
    return {"source": a, "target": b, "memory_count": 0, "relationship_count": 0,
            "relationship_summaries": [], "links": []}


def people_graph(store, *, entry_id=None, person_id=None, hops=1, min_memories=1, node_limit=60, edge_limit=100):
    # Keep all component queries on one read-only SQLite snapshot. Store.read
    # does not acquire Store._lock, so a writer cannot queue this projection.
    with store.read() as conn:
        where, args = _scope(conn, entry_id)
        if person_id is not None:
            _subject(conn, person_id)
        nodes = {r["id"]: dict(r) for r in conn.execute(f"""WITH scoped AS (
            SELECT m.id,m.speaker_subject_id FROM memories m WHERE {where}
        ), related AS (
            SELECT ms.subject_id,ms.memory_id FROM memory_subjects ms JOIN scoped m ON m.id=ms.memory_id
            UNION SELECT speaker_subject_id,id FROM scoped
        ), counts AS (SELECT subject_id,COUNT(*) AS memory_count FROM related GROUP BY subject_id),
        accounts AS (SELECT subject_id,COUNT(*) AS account_count FROM platform_identities GROUP BY subject_id)
        SELECT s.id,s.name,s.kind,COALESCE(c.memory_count,0) AS memory_count,
            COALESCE(a.account_count,0) AS account_count FROM subjects s
        LEFT JOIN counts c ON c.subject_id=s.id LEFT JOIN accounts a ON a.subject_id=s.id
        WHERE s.merged_into IS NULL AND s.id!='scene' ORDER BY s.id""", args)}
        edges = {}
        for row in conn.execute(f"""SELECT a.subject_id AS source,b.subject_id AS target,COUNT(*) AS memory_count,
            SUM(m.kind='关系') AS relationship_count FROM memories m
            JOIN memory_subjects a ON a.memory_id=m.id JOIN memory_subjects b ON b.memory_id=m.id AND a.subject_id<b.subject_id
            JOIN subjects sa ON sa.id=a.subject_id JOIN subjects sb ON sb.id=b.subject_id
            WHERE {where} AND sa.merged_into IS NULL AND sb.merged_into IS NULL
            AND sa.id!='scene' AND sb.id!='scene' GROUP BY a.subject_id,b.subject_id""", args):
            edges[(row["source"], row["target"])] = {**_edge(row["source"], row["target"]), **dict(row)}
        for link in _links(conn, entry_id):
            pair = tuple(sorted((link["source"], link["target"])))
            edges.setdefault(pair, _edge(*pair))["links"].append(_public_link(link))
        edges = {pair: e for pair, e in edges.items() if e["memory_count"] >= min_memories or e["links"]}
        eligible = {sid for sid, n in nodes.items() if n["memory_count"]}
        for pair in edges:
            eligible.update(pair)
        if person_id is not None:
            neighbors = defaultdict(set)
            for a, b in edges:
                neighbors[a].add(b)
                neighbors[b].add(a)
            eligible, frontier = {person_id}, {person_id}
            for _ in range(hops):
                frontier = {other for sid in frontier for other in neighbors[sid]} - eligible
                eligible.update(frontier)
            edges = {pair: e for pair, e in edges.items() if set(pair) <= eligible}
        total_nodes, total_edges = len(eligible), len(edges)
        ordered = sorted(edges.values(), key=lambda e: (-e["memory_count"], -e["relationship_count"], e["source"], e["target"]))
        chosen = {person_id} if person_id else set()
        selected_edges = []
        for edge in ordered:
            if len(selected_edges) >= edge_limit:
                break
            endpoints = {edge["source"], edge["target"]}
            if len(chosen | endpoints) <= node_limit:
                chosen.update(endpoints)
                selected_edges.append(edge)
        for sid in sorted(eligible, key=lambda sid: (-nodes[sid]["memory_count"], sid)):
            if len(chosen) >= node_limit:
                break
            chosen.add(sid)
        selected_nodes = [nodes[sid] for sid in sorted(chosen, key=lambda sid: (-nodes[sid]["memory_count"], sid))]
        aliases = defaultdict(list)
        for row in conn.execute("""SELECT subject_id,alias FROM subject_aliases WHERE folded_into IS NULL
            AND subject_id IN (SELECT value FROM json_each(?)) ORDER BY alias,id""", (json.dumps(sorted(chosen)),)):
            aliases[row["subject_id"]].append(row["alias"])
        for node in selected_nodes:
            node["aliases"] = aliases[node["id"]]
        # At most three excerpts per displayed pair. Fetch no excluded prose.
        if selected_edges:
            pairs_json = json.dumps([[e["source"], e["target"]] for e in selected_edges])
            summaries = conn.execute(f"""WITH pairs AS (
                SELECT json_extract(value,'$[0]') AS a,json_extract(value,'$[1]') AS b FROM json_each(?)
            ), ranked AS (
                SELECT p.a,p.b,m.id,substr(m.content,1,80) || CASE WHEN length(m.content)>80 THEN '…' ELSE '' END AS summary,
                ROW_NUMBER() OVER (PARTITION BY p.a,p.b ORDER BY m.id DESC) AS rank
                FROM pairs p JOIN memory_subjects a ON a.subject_id=p.a
                JOIN memory_subjects b ON b.subject_id=p.b AND b.memory_id=a.memory_id
                JOIN memories m ON m.id=a.memory_id WHERE {where} AND m.kind='关系'
            ) SELECT a,b,id,summary FROM ranked WHERE rank<=3 ORDER BY a,b,rank""", [pairs_json, *args])
            for row in summaries:
                edges[(row["a"], row["b"])]["relationship_summaries"].append({"id": row["id"], "summary": row["summary"]})
        return {"nodes": selected_nodes, "edges": selected_edges, "total_nodes": total_nodes, "total_edges": total_edges,
                "truncated": len(selected_nodes) < total_nodes or len(selected_edges) < total_edges,
                "node_limit": node_limit, "edge_limit": edge_limit}


def edge_evidence(store, *, source_id, target_id, entry_id=None, limit=30, offset=0):
    if source_id == target_id:
        raise ValueError("请选择两个不同的主体")
    with store.read() as conn:
        where, args = _scope(conn, entry_id)
        _subject(conn, source_id)
        _subject(conn, target_id)
        links = _links(conn, entry_id, (source_id, target_id))
        message_ids = sorted({mid for link in links for mid in link["message_ids"]})
        cte = f"""WITH candidates AS (
            SELECT m.id,m.content,m.kind,m.revision,
                (EXISTS(SELECT 1 FROM memory_subjects ms WHERE ms.memory_id=m.id AND ms.subject_id=?)
                 AND EXISTS(SELECT 1 FROM memory_subjects ms WHERE ms.memory_id=m.id AND ms.subject_id=?)) AS cooccurrence,
                EXISTS(SELECT 1 FROM sources s WHERE s.memory_id=m.id AND s.kind='message'
                    AND s.message_id IN (SELECT value FROM json_each(?))) AS link_evidence
            FROM memories m WHERE {where}
        ), matched AS (SELECT * FROM candidates WHERE cooccurrence OR link_evidence)"""
        params = [source_id, target_id, json.dumps(message_ids), *args]
        total = conn.execute(cte + " SELECT COUNT(*) FROM matched", params).fetchone()[0]
        items = [dict(r) for r in conn.execute(cte + " SELECT * FROM matched ORDER BY id DESC LIMIT ? OFFSET ?",
                                             [*params, limit, offset])]
        for item in items:
            item["cooccurrence"] = bool(item["cooccurrence"])
            item["link_evidence"] = bool(item["link_evidence"])
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "links": [_public_link(link) for link in links]}
