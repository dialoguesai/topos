"""Experimental discourse lenses → entity graph (transcript eval).

A lens assertion is a FACT, not a graph primitive. Owner-fact projection
refuses non-owner subjects (L4-8), so overheard captions never become
``Owner -[predicate]-> X`` edges. This lane mints a different shape:

    recording --discusses--> nugget (claim / event / program)
    nugget    --about-->     named entity in the evidence window
    entity    --windowed_with--> entity in a 45s caption window
    topic     --discusses-->     entities (and nuggets) in cluster members

So one extracted item is never a lone node: it hangs off the recording and
points at the names around it. Ambient only — no contacts, no owner beliefs.

Heuristic extractors on purpose: this is a look-at-the-graph experiment, not
a verified derivation pack. LLM packs can replace the span finders later
without changing the mint shape.

Claims / events / programs / windowed relations bind to the ``transcripts``
canonical group (YouTube now; meetings, sales calls, lectures when they share
that lane). Topic hubs are a different lane: journals and chats belong in
clusters, and a journal naming someone in a cluster is not a leak.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ...storage.db.write_gate import commit_connection, with_db_write
from ...sources.definitions import definition_from_payload, source_gets_discourse_lenses
from .edges import EDGE_DISCUSSES, EDGE_PART_OF
from .fact_materializer import _MZ_WEIGHT_FLOOR, _upsert_materialized_edge
from .graph_enrichers import EDGE_PARTICIPATES, _ensure_node, _table_exists

logger = logging.getLogger("topos.features.entities.discourse_graph")

EDGE_WINDOWED = "windowed_with"
EDGE_ABOUT = "about"
EDGE_WORKED_ON = "worked_on"

WINDOW_SEC = 45.0
MAX_CLAIMS_PER_TRANSCRIPT = 48
MAX_EVENTS_PER_TRANSCRIPT = 24
MAX_PROGRAMS_PER_TRANSCRIPT = 16
MAX_TOPIC_ENTITY_LINKS = 12
MIN_WINDOW_PAIR_COUNT = 1
MAX_CLAIM_LABEL = 96

_ASSERT_RE = re.compile(
    r"\b(is|are|was|were|means|because|argued|should|the fact|"
    r"so that|look,|we could|you're regulating|that's (?:a |the )?problem)\b",
    re.I,
)
_EVENT_RE = re.compile(
    r"\b(?:(?:19|20)\d{2})\b|"
    r"\b(?:meeting|interview|hearing|when I was|I was a|"
    r"started to|the day|that night)\b",
    re.I,
)
_PROGRAM_RE = re.compile(
    r"\b(DARPA|NASA|NHI|UAP|PIPA|CIA|FBI|NSA|DOJ|DOE|MIT|"
    r"Majestic[\s-]?12|Operation\s+[A-Z][A-Za-z]+|"
    r"crash retrieval|remote viewing|legacy program)\b|"
    r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+"
    r"(?:program|operations?|commission|retrieval|network))\b",
    re.I,
)


def _clip(text: str, n: int = MAX_CLAIM_LABEL) -> str:
    raw = " ".join((text or "").split())
    if len(raw) <= n:
        return raw
    return raw[: n - 1].rstrip() + "…"


def _sid(prefix: str, *parts: str) -> str:
    blob = "|".join(parts)
    digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def materialize_discourse_lenses_to_graph(
    conn: sqlite3.Connection,
    *,
    touched_edges: Optional[Set[str]] = None,
) -> Dict[str, int]:
    """Mint recording / claim / event / program / window / topic links.

    No-op when ``transcript_segments`` is absent, or when no source in the
    transcripts group is enabled for discourse lenses. Always ambient.
    """
    report = {
        "recordings": 0,
        "claims": 0,
        "events": 0,
        "programs": 0,
        "windowed_pairs": 0,
        "topic_links": 0,
        "edges": 0,
    }
    if not _table_exists(conn, "transcript_segments"):
        return report
    if not _table_exists(conn, "entities"):
        return report
    enabled = discourse_enabled_source_ids(conn)
    if not enabled:
        return report

    source_by_tid = _transcript_source_map(conn, enabled)
    with with_db_write():
        recs = _mint_recordings(conn, touched_edges, enabled, source_by_tid)
        report["recordings"] = recs
        report["claims"] = _mint_claims(conn, touched_edges, enabled, source_by_tid)
        report["events"] = _mint_events(conn, touched_edges, enabled, source_by_tid)
        report["programs"] = _mint_programs(conn, touched_edges, enabled, source_by_tid)
        report["windowed_pairs"] = _mint_windowed_relations(
            conn, touched_edges, enabled, source_by_tid
        )
        report["topic_links"] = _mint_topic_links(
            conn, touched_edges, enabled, source_by_tid
        )
        if touched_edges is not None:
            report["edges"] = len(touched_edges)
        commit_connection(conn)
    return report


def discourse_enabled_source_ids(conn: sqlite3.Connection) -> frozenset[str]:
    """Source ids whose transcript rows may mint discourse-lens edges.

    Registry + runtime installs: transcripts-group sources unless they opted
    out. Rows already on the transcripts tables inherit the lane unless their
    source_id is a known non-transcript source (journal, chat, …).
    """
    enabled: Set[str] = set()
    blocked: Set[str] = set()
    seen: Set[str] = set()

    def _consider(defn: object) -> None:
        sid = str(getattr(defn, "source_id", "") or "").strip()
        if not sid:
            return
        seen.add(sid)
        if source_gets_discourse_lenses(defn):
            enabled.add(sid)
            blocked.discard(sid)
        else:
            blocked.add(sid)
            enabled.discard(sid)

    try:
        from ...sources.registry import BUNDLED_REGISTRY, REGISTRY
    except Exception:
        REGISTRY = {}
        BUNDLED_REGISTRY = {}
    for defn in list(REGISTRY.values()) + list(BUNDLED_REGISTRY.values()):
        if str(getattr(defn, "source_id", "") or "") in seen:
            continue
        _consider(defn)
    for defn in _runtime_source_defs(conn):
        _consider(defn)

    for table in ("transcripts", "transcript_segments"):
        if not _table_exists(conn, table):
            continue
        try:
            rows = conn.execute(f"SELECT DISTINCT source_id FROM {table}").fetchall()
        except sqlite3.Error:
            continue
        for (sid,) in rows:
            sid_s = str(sid or "").strip()
            if sid_s and sid_s not in blocked:
                enabled.add(sid_s)
    return frozenset(enabled)


def _runtime_source_defs(conn: sqlite3.Connection) -> List[object]:
    if not _table_exists(conn, "source_runtime_installs"):
        return []
    try:
        rows = conn.execute(
            """
            SELECT source_id, source_definition_json FROM source_runtime_installs
            WHERE is_active=1 AND status IN ('installed', 'active', 'ready')
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    import json as _json

    out: List[object] = []
    for source_id, def_json in rows:
        try:
            payload = _json.loads(def_json) if isinstance(def_json, str) else (def_json or {})
        except (_json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload.setdefault("source_id", source_id)
        try:
            out.append(definition_from_payload(payload))
        except (TypeError, ValueError):
            continue
    return out


def _in_clause(column: str, ids: Sequence[str]) -> Tuple[str, Tuple[str, ...]]:
    cleaned = tuple(sorted({str(i) for i in ids if i}))
    if not cleaned:
        return "1=0", ()
    return f"{column} IN ({','.join('?' * len(cleaned))})", cleaned


def _touch(touched: Optional[Set[str]], edge_id: Optional[str]) -> None:
    if touched is not None and edge_id:
        touched.add(edge_id)


def _transcript_source_map(
    conn: sqlite3.Connection, enabled: frozenset[str]
) -> Dict[str, str]:
    """transcript_id -> source_id for discourse-enabled caption rows."""
    out: Dict[str, str] = {}
    clause, params = _in_clause("source_id", enabled)
    if not _table_exists(conn, "transcript_segments"):
        return out
    for tid, sid in conn.execute(
        f"SELECT DISTINCT transcript_id, source_id FROM transcript_segments WHERE {clause}",
        params,
    ):
        if tid and sid:
            out[str(tid)] = str(sid)
    return out


def _src(source_by_tid: Dict[str, str], tid: str, enabled: frozenset[str]) -> str:
    return source_by_tid.get(tid) or (next(iter(enabled), "") if enabled else "")


def _link(
    conn: sqlite3.Connection,
    *,
    src: str,
    dst: str,
    edge_type: str,
    statement: str,
    source_object_id: str,
    event_at: Optional[str] = None,
    evidence_count: int = 1,
    weight: Optional[float] = None,
    touched: Optional[Set[str]] = None,
    source_id: Optional[str] = None,
    source_mix: Optional[Dict[str, int]] = None,
) -> None:
    _touch(
        touched,
        _upsert_materialized_edge(
            conn,
            src=src,
            dst=dst,
            edge_type=edge_type,
            weight=float(weight if weight is not None else _MZ_WEIGHT_FLOOR),
            valid_from=event_at,
            valid_to=None,
            last_event_at=event_at,
            statement=statement,
            source_object_id=source_object_id,
            actor_role="ambient",
            evidence_count=evidence_count,
            source_id=source_id,
            source_mix=source_mix,
        ),
    )


def _transcripts(
    conn: sqlite3.Connection, enabled: frozenset[str]
) -> List[Tuple[str, str]]:
    clause, params = _in_clause("source_id", enabled)
    if not _table_exists(conn, "transcripts"):
        rows = conn.execute(
            f"SELECT DISTINCT transcript_id FROM transcript_segments WHERE {clause}",
            params,
        ).fetchall()
        return [(str(r[0]), str(r[0])) for r in rows if r[0]]
    return [
        (str(r[0]), str(r[1] or r[0]))
        for r in conn.execute(
            f"""
            SELECT transcript_id, COALESCE(NULLIF(title,''), transcript_id)
            FROM transcripts
            WHERE {clause}
            """,
            params,
        ).fetchall()
        if r[0]
    ]


def _recording_id(transcript_id: str) -> str:
    return f"transcript:{transcript_id}"


def _mint_recordings(
    conn: sqlite3.Connection,
    touched: Optional[Set[str]],
    enabled: frozenset[str],
    source_by_tid: Dict[str, str],
) -> int:
    n = 0
    for tid, title in _transcripts(conn, enabled):
        node_id = _recording_id(tid)
        first = conn.execute(
            "SELECT MIN(event_at) FROM transcript_segments WHERE transcript_id=?",
            (tid,),
        ).fetchone()
        last = conn.execute(
            "SELECT MAX(event_at) FROM transcript_segments WHERE transcript_id=?",
            (tid,),
        ).fetchone()
        sid = _src(source_by_tid, tid, enabled)
        meta = {"lens": "discourse.topics", "transcript_id": tid}
        if sid:
            meta["source_id"] = sid
        _ensure_node(
            conn,
            node_id,
            title if title != tid else f"Recording {tid}",
            "document",
            metadata=meta,
            first_at=first[0] if first else None,
            last_at=last[0] if last else None,
        )
        n += 1
    return n


def _segments_for(
    conn: sqlite3.Connection, transcript_id: str, enabled: frozenset[str]
) -> List[dict]:
    clause, params = _in_clause("source_id", enabled)
    rows = conn.execute(
        f"""
        SELECT segment_id, content, start_sec, event_at
        FROM transcript_segments
        WHERE transcript_id=? AND {clause}
        ORDER BY start_sec, segment_id
        """,
        (transcript_id, *params),
    ).fetchall()
    return [
        {
            "segment_id": row[0],
            "content": row[1],
            "start_sec": row[2],
            "event_at": row[3],
        }
        for row in rows
    ]


def _mentions_by_record(conn: sqlite3.Connection) -> Dict[str, List[Tuple[str, str]]]:
    """record_id -> [(entity_id, entity_type), ...]"""
    out: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    if not _table_exists(conn, "entity_mentions"):
        return out
    for record_id, entity_id, entity_type in conn.execute(
        """
        SELECT m.record_id, m.entity_id, COALESCE(e.entity_type, '')
        FROM entity_mentions m
        JOIN entities e ON e.entity_id = m.entity_id
        WHERE m.record_id IS NOT NULL
        """
    ):
        out[str(record_id)].append((str(entity_id), str(entity_type or "")))
    return out


def _entities_nearby(
    mentions: Dict[str, List[Tuple[str, str]]],
    segment_ids: Sequence[str],
    index: int,
    radius: int = 1,
) -> List[Tuple[str, str]]:
    seen: Dict[str, str] = {}
    lo = max(0, index - radius)
    hi = min(len(segment_ids), index + radius + 1)
    for sid in segment_ids[lo:hi]:
        for eid, etype in mentions.get(sid, ()):
            seen[eid] = etype
    return list(seen.items())


def _mint_claims(
    conn: sqlite3.Connection,
    touched: Optional[Set[str]],
    enabled: frozenset[str],
    source_by_tid: Dict[str, str],
) -> int:
    mentions = _mentions_by_record(conn)
    minted = 0
    for tid, _title in _transcripts(conn, enabled):
        segs = _segments_for(conn, tid, enabled)
        ids = [str(s["segment_id"]) for s in segs]
        scored: List[Tuple[int, dict, int]] = []
        for i, row in enumerate(segs):
            text = str(row["content"] or "")
            if len(text) < 80 or not _ASSERT_RE.search(text):
                continue
            n_ent = len(mentions.get(str(row["segment_id"]), ()))
            score = len(text) + 40 * n_ent
            scored.append((score, row, i))
        scored.sort(key=lambda t: t[0], reverse=True)
        rec_id = _recording_id(tid)
        src_id = _src(source_by_tid, tid, enabled)
        for score, row, i in scored[:MAX_CLAIMS_PER_TRANSCRIPT]:
            sid = str(row["segment_id"])
            node_id = _sid("claim", tid, sid)
            label = _clip(str(row["content"] or ""))
            event_at = row["event_at"]
            claim_meta = {"lens": "discourse.claims", "segment_id": sid, "transcript_id": tid}
            if src_id:
                claim_meta["source_id"] = src_id
            _ensure_node(
                conn,
                node_id,
                label,
                "claim",
                metadata=claim_meta,
                first_at=event_at,
                last_at=event_at,
            )
            _link(
                conn,
                src=rec_id,
                dst=node_id,
                edge_type=EDGE_DISCUSSES,
                statement="recording discusses claim",
                source_object_id=f"discourse:claim:{sid}",
                event_at=event_at,
                touched=touched,
                source_id=src_id or None,
            )
            nearby = _entities_nearby(mentions, ids, i, radius=1)
            if not nearby:
                nearby = mentions.get(sid, [])
            for eid, _etype in nearby[:8]:
                _link(
                    conn,
                    src=node_id,
                    dst=eid,
                    edge_type=EDGE_ABOUT,
                    statement="claim about named thing",
                    source_object_id=f"discourse:claim:{sid}",
                    event_at=event_at,
                    touched=touched,
                    source_id=src_id or None,
                )
            minted += 1
    return minted


def _mint_events(
    conn: sqlite3.Connection,
    touched: Optional[Set[str]],
    enabled: frozenset[str],
    source_by_tid: Dict[str, str],
) -> int:
    mentions = _mentions_by_record(conn)
    minted = 0
    for tid, _title in _transcripts(conn, enabled):
        segs = _segments_for(conn, tid, enabled)
        ids = [str(s["segment_id"]) for s in segs]
        scored: List[Tuple[int, dict, int]] = []
        for i, row in enumerate(segs):
            text = str(row["content"] or "")
            if len(text) < 60 or not _EVENT_RE.search(text):
                continue
            nearby = _entities_nearby(mentions, ids, i, radius=1)
            if not nearby:
                continue
            scored.append((len(text) + 20 * len(nearby), row, i))
        scored.sort(key=lambda t: t[0], reverse=True)
        rec_id = _recording_id(tid)
        src_id = _src(source_by_tid, tid, enabled)
        for _score, row, i in scored[:MAX_EVENTS_PER_TRANSCRIPT]:
            sid = str(row["segment_id"])
            node_id = _sid("event", tid, sid)
            label = _clip(str(row["content"] or ""), 80)
            event_at = row["event_at"]
            event_meta = {"lens": "discourse.events", "segment_id": sid, "transcript_id": tid}
            if src_id:
                event_meta["source_id"] = src_id
            _ensure_node(
                conn,
                node_id,
                label,
                "event",
                metadata=event_meta,
                first_at=event_at,
                last_at=event_at,
            )
            _link(
                conn,
                src=rec_id,
                dst=node_id,
                edge_type=EDGE_DISCUSSES,
                statement="recording recounts event",
                source_object_id=f"discourse:event:{sid}",
                event_at=event_at,
                touched=touched,
                source_id=src_id or None,
            )
            for eid, etype in _entities_nearby(mentions, ids, i, radius=1)[:8]:
                edge = EDGE_PARTICIPATES if etype == "person" else EDGE_ABOUT
                src, dst = (eid, node_id) if edge == EDGE_PARTICIPATES else (node_id, eid)
                _link(
                    conn,
                    src=src,
                    dst=dst,
                    edge_type=edge,
                    statement="named in recounted event",
                    source_object_id=f"discourse:event:{sid}",
                    event_at=event_at,
                    touched=touched,
                    source_id=src_id or None,
                )
            minted += 1
    return minted


def _mint_programs(
    conn: sqlite3.Connection,
    touched: Optional[Set[str]],
    enabled: frozenset[str],
    source_by_tid: Dict[str, str],
) -> int:
    mentions = _mentions_by_record(conn)
    minted_names: Set[str] = set()
    minted = 0
    for tid, _title in _transcripts(conn, enabled):
        segs = _segments_for(conn, tid, enabled)
        ids = [str(s["segment_id"]) for s in segs]
        found: List[Tuple[str, dict, int]] = []
        for i, row in enumerate(segs):
            text = str(row["content"] or "")
            for match in _PROGRAM_RE.finditer(text):
                name = " ".join((match.group(0) or "").split())
                if len(name) < 3:
                    continue
                found.append((name, row, i))
        # Prefer distinctive names; cap per video.
        uniq: List[Tuple[str, dict, int]] = []
        seen_here: Set[str] = set()
        for name, row, i in found:
            key = name.lower()
            if key in seen_here:
                continue
            seen_here.add(key)
            uniq.append((name, row, i))
        rec_id = _recording_id(tid)
        src_id = _src(source_by_tid, tid, enabled)
        for name, row, i in uniq[:MAX_PROGRAMS_PER_TRANSCRIPT]:
            node_id = _sid("program", name.lower())
            event_at = row["event_at"]
            sid = str(row["segment_id"])
            if name.lower() not in minted_names:
                prog_meta = {"lens": "discourse.programs"}
                if src_id:
                    prog_meta["source_id"] = src_id
                _ensure_node(
                    conn,
                    node_id,
                    name,
                    "program",
                    metadata=prog_meta,
                    first_at=event_at,
                    last_at=event_at,
                )
                minted_names.add(name.lower())
                minted += 1
            _link(
                conn,
                src=rec_id,
                dst=node_id,
                edge_type=EDGE_DISCUSSES,
                statement="recording discusses program",
                source_object_id=f"discourse:program:{sid}",
                event_at=event_at,
                touched=touched,
                source_id=src_id or None,
            )
            for eid, etype in _entities_nearby(mentions, ids, i, radius=1)[:6]:
                if etype == "person":
                    _link(
                        conn,
                        src=eid,
                        dst=node_id,
                        edge_type=EDGE_WORKED_ON,
                        statement="named near program",
                        source_object_id=f"discourse:program:{sid}",
                        event_at=event_at,
                        touched=touched,
                        source_id=src_id or None,
                    )
                elif etype == "org":
                    _link(
                        conn,
                        src=node_id,
                        dst=eid,
                        edge_type=EDGE_PART_OF,
                        statement="program related to org",
                        source_object_id=f"discourse:program:{sid}",
                        event_at=event_at,
                        touched=touched,
                        source_id=src_id or None,
                    )
                else:
                    _link(
                        conn,
                        src=node_id,
                        dst=eid,
                        edge_type=EDGE_ABOUT,
                        statement="program about named thing",
                        source_object_id=f"discourse:program:{sid}",
                        event_at=event_at,
                        touched=touched,
                        source_id=src_id or None,
                    )
    return minted


def _mint_windowed_relations(
    conn: sqlite3.Connection,
    touched: Optional[Set[str]],
    enabled: frozenset[str],
    source_by_tid: Dict[str, str],
) -> int:
    """Pairs of resolved names that share a 45s window, not just a line."""
    mentions = _mentions_by_record(conn)
    pair_count: Dict[Tuple[str, str], int] = defaultdict(int)
    pair_last: Dict[Tuple[str, str], Optional[str]] = {}
    pair_sources: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    for tid, _title in _transcripts(conn, enabled):
        segs = _segments_for(conn, tid, enabled)
        buckets: Dict[int, List[str]] = defaultdict(list)
        bucket_time: Dict[int, Optional[str]] = {}
        same_line: Set[Tuple[str, str]] = set()
        src_id = _src(source_by_tid, tid, enabled)
        for row in segs:
            try:
                start = float(row["start_sec"] or 0)
            except (TypeError, ValueError):
                start = 0.0
            bucket = int(start // WINDOW_SEC)
            names_here = [eid for eid, _t in mentions.get(str(row["segment_id"]), ())]
            for eid in names_here:
                if eid not in buckets[bucket]:
                    buckets[bucket].append(eid)
            bucket_time[bucket] = row["event_at"] or bucket_time.get(bucket)
            ordered = sorted(set(names_here))
            for a in range(len(ordered)):
                for b in range(a + 1, len(ordered)):
                    same_line.add((ordered[a], ordered[b]))
        for bucket, names in buckets.items():
            names = sorted(names)[:12]
            event_at = bucket_time.get(bucket)
            for a in range(len(names)):
                for b in range(a + 1, len(names)):
                    key = (names[a], names[b])
                    if key in same_line:
                        continue
                    pair_count[key] += 1
                    pair_last[key] = event_at or pair_last.get(key)
                    if src_id:
                        pair_sources[key].add(src_id)
    minted = 0
    for (src, dst), count in pair_count.items():
        if count < MIN_WINDOW_PAIR_COUNT:
            continue
        # Same-record co-occurrence already covers count-from-one-line pairs;
        # windowed_with is the extra beat. Weight scales with repeats, floor 2.0.
        weight = _MZ_WEIGHT_FLOOR + min(2.0, 0.15 * (count - MIN_WINDOW_PAIR_COUNT))
        contrib = pair_sources.get((src, dst), set())
        one = next(iter(contrib)) if len(contrib) == 1 else None
        mix = {s: 1 for s in contrib} if len(contrib) > 1 else None
        _link(
            conn,
            src=src,
            dst=dst,
            edge_type=EDGE_WINDOWED,
            statement=f"named within {int(WINDOW_SEC)}s ×{count}",
            source_object_id="discourse:windowed",
            event_at=pair_last.get((src, dst)),
            evidence_count=count,
            weight=weight,
            touched=touched,
            source_id=one,
            source_mix=mix,
        )
        minted += 1
    return minted


def _mint_topic_links(
    conn: sqlite3.Connection,
    touched: Optional[Set[str]],
    enabled: frozenset[str],
    source_by_tid: Dict[str, str],
) -> int:
    """Cluster hubs discuss entities named in any member, plus transcript claims.

    Topics are a cross-source memory map — journals and chats belong here.
    Claim nuggets stay on the transcripts allowlist because those nodes are
    minted from caption extractors, not from owner prose.
    """
    if not _table_exists(conn, "topic_cluster_members"):
        return 0
    if not _table_exists(conn, "topic_clusters"):
        return 0
    clause, src_params = _in_clause("s.source_id", enabled)
    links = 0
    clusters = conn.execute(
        "SELECT cluster_id, COALESCE(NULLIF(label,''), cluster_id) FROM topic_clusters"
    ).fetchall()
    for cluster_id, label in clusters:
        cid = str(cluster_id)
        topic_id = f"topic_{cid}"
        # Hub may already exist from top_topics materializer.
        _ensure_node(conn, topic_id, str(label), "topic", metadata={"lens": "discourse.topics"})
        rows = conn.execute(
            """
            SELECT e.entity_id, COUNT(*) AS n
            FROM topic_cluster_members m
            JOIN entity_mentions em ON em.record_id = m.record_id
            JOIN entities e ON e.entity_id = em.entity_id
            WHERE m.cluster_id=?
              AND e.entity_type NOT IN ('topic', 'claim', 'program', 'document')
            GROUP BY e.entity_id
            ORDER BY n DESC
            LIMIT ?
            """,
            (cid, MAX_TOPIC_ENTITY_LINKS),
        ).fetchall()
        for entity_id, n in rows:
            _link(
                conn,
                src=topic_id,
                dst=str(entity_id),
                edge_type=EDGE_DISCUSSES,
                statement=f"cluster member names ×{int(n)}",
                source_object_id=f"discourse:topic:{cid}",
                evidence_count=int(n),
                touched=touched,
            )
            links += 1
        claim_rows = conn.execute(
            f"""
            SELECT e.entity_id, s.source_id
            FROM topic_cluster_members m
            JOIN transcript_segments s ON s.segment_id = m.record_id
            JOIN entities e
              ON json_extract(e.metadata_json, '$.segment_id') = m.record_id
            WHERE m.cluster_id=? AND e.entity_type='claim' AND {clause}
            LIMIT 8
            """,
            (cid, *src_params),
        ).fetchall()
        for claim_id, claim_src in claim_rows:
            sid = str(claim_src or "").strip() or next(iter(source_by_tid.values()), "")
            _link(
                conn,
                src=topic_id,
                dst=str(claim_id),
                edge_type=EDGE_DISCUSSES,
                statement="claim drawn from cluster caption",
                source_object_id=f"discourse:topic:{cid}",
                touched=touched,
                source_id=sid or None,
            )
            links += 1
    return links
