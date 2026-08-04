"""Read-only affinity quality scorecard (diffable across rebuilds).

This is a *mechanism + contamination* snapshot, not a human quality verdict.
It answers: how many latent edges exist, did the floor/recipe move, and what
fraction of surviving pairs look like the known failure modes (aliases,
shared-source cliques, near-identical centroids, co-occurrence residuals).

Human labels (useful / obvious / noise) are still required before claiming
"node quality improved." This module makes the automated half of that claim
cheap to re-run.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..signal.vector_codec import VectorCodecError, decode_vector, normalize_vector
from .affinity import (
    AFFINITY_FLOOR_ABS,
    ENGINE_CONFIG_KEY_AFFINITY_PERCENTILE,
)
from .context_vectors import (
    CENTROID_DEGENERACY_EPSILON,
    MIN_CONTEXT_MENTIONS,
    MIN_CONTEXT_SOURCES,
    _source_key,
)
from .edges import EDGE_CO_OCCURRENCE, EDGE_SEMANTIC_AFFINITY

#: v2: population coverage uses meaningful denominators (significant /
#: dual-floor eligible people), not "all person rows including NER residue".
SCORECARD_VERSION = 2

#: Surviving affinity pairs whose stored centroids are this close count as
#: near-identical (same family as §3.1a degeneracy, applied to edges).
NEAR_IDENTITY_COSINE = 1.0 - CENTROID_DEGENERACY_EPSILON


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _safe_rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _active_affinity_edges(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "entity_edges"):
        return []
    rows = conn.execute(
        """
        SELECT e.src_entity_id, e.dst_entity_id, e.weight, e.evidence_count,
               e.edge_id, e.valid_from,
               s.canonical_name, d.canonical_name
        FROM entity_edges e
        LEFT JOIN entities s ON s.entity_id = e.src_entity_id
        LEFT JOIN entities d ON d.entity_id = e.dst_entity_id
        WHERE e.edge_type = ? AND e.valid_to IS NULL
        ORDER BY e.weight DESC, e.src_entity_id, e.dst_entity_id
        """,
        (EDGE_SEMANTIC_AFFINITY,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for src, dst, weight, evidence, edge_id, valid_from, src_name, dst_name in rows:
        a, b = _canonical(str(src), str(dst))
        out.append(
            {
                "src_entity_id": a,
                "dst_entity_id": b,
                "weight": round(float(weight or 0.0), 6),
                "evidence_count": int(evidence or 0),
                "edge_id": edge_id,
                "valid_from": valid_from,
                "src_name": src_name or a,
                "dst_name": dst_name or b,
            }
        )
    return out


def _source_keys_for_entity(conn: sqlite3.Connection, entity_id: str) -> Set[str]:
    rows = conn.execute(
        """
        SELECT m.record_id, e.content_hash, e.conversation_id
        FROM entity_mentions m
        LEFT JOIN signal_embeddings e ON e.record_id = m.record_id
        WHERE m.entity_id = ?
        """,
        (entity_id,),
    ).fetchall()
    keys: Set[str] = set()
    for record_id, content_hash, conversation_id in rows:
        keys.add(
            _source_key(
                str(record_id),
                str(content_hash) if content_hash else None,
                str(conversation_id) if conversation_id else None,
            )
        )
    return keys


def _load_centroid_unit(conn: sqlite3.Connection, entity_id: str) -> Optional[List[float]]:
    if not _table_exists(conn, "entity_context_vectors"):
        return None
    row = conn.execute(
        "SELECT centroid_blob FROM entity_context_vectors WHERE entity_id=?",
        (entity_id,),
    ).fetchone()
    if row is None or not row[0]:
        return None
    try:
        return normalize_vector(decode_vector(row[0], "f32"))
    except VectorCodecError:
        return None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def _co_occurrence_pairs(conn: sqlite3.Connection) -> Set[Tuple[str, str]]:
    if not _table_exists(conn, "entity_edges"):
        return set()
    rows = conn.execute(
        """
        SELECT src_entity_id, dst_entity_id FROM entity_edges
        WHERE edge_type = ? AND valid_to IS NULL
        """,
        (EDGE_CO_OCCURRENCE,),
    ).fetchall()
    return {_canonical(str(a), str(b)) for a, b in rows}


def _distinct_source_count(conn: sqlite3.Connection, entity_id: str) -> int:
    """Distinct source documents among *embedded* mentions (centroid gate)."""
    if not _table_exists(conn, "entity_mentions"):
        return 0
    try:
        rows = conn.execute(
            """
            SELECT m.record_id, e.content_hash, e.conversation_id
            FROM entity_mentions m
            JOIN signal_embeddings e ON e.record_id = m.record_id
            WHERE m.entity_id = ? AND e.vector_blob IS NOT NULL
            """,
            (entity_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    keys = {
        _source_key(
            str(record_id),
            str(content_hash) if content_hash else None,
            str(conversation_id) if conversation_id else None,
        )
        for record_id, content_hash, conversation_id in rows
    }
    return len(keys)


def _population(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Centroid coverage against denominators that mean something.

    ``centroid_coverage_of_all_people`` will look tiny on every real node
    (NER residue). Prefer ``centroid_coverage_of_significant`` (≥ mention
    floor) and ``centroid_coverage_of_eligible`` (mention + source floors).
    """
    people = 0
    mentioned = 0
    significant = 0
    centroids = 0
    if _table_exists(conn, "entities"):
        people = int(
            conn.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_type='person' "
                "AND COALESCE(is_self, 0)=0"
            ).fetchone()[0]
        )
        mentioned = int(
            conn.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_type='person' "
                "AND COALESCE(is_self, 0)=0 AND mention_count >= 1"
            ).fetchone()[0]
        )
        significant = int(
            conn.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_type='person' "
                "AND COALESCE(is_self, 0)=0 AND mention_count >= ?",
                (MIN_CONTEXT_MENTIONS,),
            ).fetchone()[0]
        )
    if _table_exists(conn, "entity_context_vectors"):
        centroids = int(
            conn.execute("SELECT COUNT(*) FROM entity_context_vectors").fetchone()[0]
        )
    blackholed = 0
    if _table_exists(conn, "entity_blackholes"):
        try:
            blackholed = int(
                conn.execute(
                    "SELECT COUNT(*) FROM entity_blackholes "
                    "WHERE entity_id IS NOT NULL AND entity_id != ''"
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            blackholed = 0

    # Dual-floor eligible: mention_count floor AND ≥ MIN_CONTEXT_SOURCES
    # distinct embedded sources. Scoped to the significant set — cheap at
    # personal scale (tens/hundreds), not run over the whole NER long tail.
    eligible = 0
    if _table_exists(conn, "entities") and _table_exists(conn, "entity_mentions"):
        significant_ids = [
            str(r[0])
            for r in conn.execute(
                """
                SELECT entity_id FROM entities
                WHERE entity_type='person' AND COALESCE(is_self, 0)=0
                  AND mention_count >= ?
                ORDER BY entity_id
                """,
                (MIN_CONTEXT_MENTIONS,),
            ).fetchall()
        ]
        for entity_id in significant_ids:
            # Embedded-mention count is the secondary floor; mention_count on
            # the entity row can include non-embedded rows.
            emb_n = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM entity_mentions m
                    JOIN signal_embeddings e ON e.record_id = m.record_id
                    WHERE m.entity_id = ? AND e.vector_blob IS NOT NULL
                    """,
                    (entity_id,),
                ).fetchone()[0]
            )
            if emb_n < MIN_CONTEXT_MENTIONS:
                continue
            if _distinct_source_count(conn, entity_id) >= MIN_CONTEXT_SOURCES:
                eligible += 1

    return {
        "person_entities_non_self": people,
        "people_mentioned": mentioned,
        "people_significant": significant,
        "people_dual_floor_eligible": eligible,
        "context_centroids": centroids,
        "blackholed_entities_with_id": blackholed,
        "min_context_mentions": MIN_CONTEXT_MENTIONS,
        "min_context_sources": MIN_CONTEXT_SOURCES,
        # Primary: of people who cleared the mention floor on the entity row.
        "centroid_coverage_of_significant": _safe_rate(centroids, significant),
        # Strictest: of people who would pass both build-time floors today.
        "centroid_coverage_of_eligible": _safe_rate(centroids, eligible),
        # Of anyone ever mentioned (still includes thin one-offs).
        "centroid_coverage_of_mentioned": _safe_rate(centroids, mentioned),
        # Kept for continuity — usually tiny; do not treat as health.
        "centroid_coverage_of_all_people": _safe_rate(centroids, people),
        # Back-compat alias → significant coverage (not all-people).
        "centroid_coverage": _safe_rate(centroids, significant),
    }


def _latest_recompute_logs(conn: sqlite3.Connection, *, limit: int = 5) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "affinity_recompute_log"):
        return []
    rows = conn.execute(
        """
        SELECT created_at, entities_considered, pairs_considered, edges_written,
               ceiling, ceiling_hit, percentile, resolved_cosine, floor_cosine,
               spec_version
        FROM affinity_recompute_log
        ORDER BY recompute_id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "created_at": row[0],
                "entities_considered": row[1],
                "pairs_considered": row[2],
                "edges_written": row[3],
                "ceiling": row[4],
                "ceiling_hit": bool(row[5]),
                "percentile": row[6],
                "resolved_cosine": row[7],
                "floor_cosine": row[8],
                "spec_version": row[9],
            }
        )
    return out


def _read_engine_config(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """Read engine_config without creating the table (safe on query_only DBs)."""
    if not _table_exists(conn, "engine_config"):
        return None
    try:
        row = conn.execute(
            "SELECT value FROM engine_config WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else str(row[0])


def _configured_percentile(conn: sqlite3.Connection) -> float:
    from .affinity import AFFINITY_DEFAULT_PERCENTILE

    raw = _read_engine_config(conn, ENGINE_CONFIG_KEY_AFFINITY_PERCENTILE)
    if raw is None:
        return AFFINITY_DEFAULT_PERCENTILE
    try:
        return max(0.0, min(100.0, float(raw)))
    except (TypeError, ValueError):
        return AFFINITY_DEFAULT_PERCENTILE


def score_affinity_pairs(
    conn: sqlite3.Connection,
    edges: Sequence[Dict[str, Any]],
    *,
    merge_pairs: Optional[Set[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """Contamination / residual metrics over an active affinity edge set."""
    if merge_pairs is None:
        try:
            from .consolidation import merge_candidate_pairs

            merge_pairs = merge_candidate_pairs(conn)
        except Exception:  # noqa: BLE001 — scorecard must still emit
            merge_pairs = set()

    co_pairs = _co_occurrence_pairs(conn)
    alias_hits: List[Dict[str, Any]] = []
    shared_source_hits: List[Dict[str, Any]] = []
    near_identity_hits: List[Dict[str, Any]] = []
    co_residual_hits: List[Dict[str, Any]] = []

    source_cache: Dict[str, Set[str]] = {}
    centroid_cache: Dict[str, Optional[List[float]]] = {}

    for edge in edges:
        pair = _canonical(str(edge["src_entity_id"]), str(edge["dst_entity_id"]))
        a, b = pair
        item = {
            "src_entity_id": a,
            "dst_entity_id": b,
            "src_name": edge.get("src_name"),
            "dst_name": edge.get("dst_name"),
            "weight": edge.get("weight"),
        }

        if pair in merge_pairs:
            alias_hits.append(item)
        if pair in co_pairs:
            co_residual_hits.append(item)

        if a not in source_cache:
            source_cache[a] = _source_keys_for_entity(conn, a)
        if b not in source_cache:
            source_cache[b] = _source_keys_for_entity(conn, b)
        shared = source_cache[a] & source_cache[b]
        if shared:
            shared_source_hits.append({**item, "shared_sources": len(shared)})

        if a not in centroid_cache:
            centroid_cache[a] = _load_centroid_unit(conn, a)
        if b not in centroid_cache:
            centroid_cache[b] = _load_centroid_unit(conn, b)
        ca, cb = centroid_cache[a], centroid_cache[b]
        if ca and cb:
            cos = _cosine(ca, cb)
            if cos >= NEAR_IDENTITY_COSINE:
                near_identity_hits.append({**item, "centroid_cosine": round(cos, 6)})

    n = len(edges)
    return {
        "active_edges": n,
        "alias_collision_count": len(alias_hits),
        "alias_collision_rate": _safe_rate(len(alias_hits), n),
        "shared_source_count": len(shared_source_hits),
        "shared_source_rate": _safe_rate(len(shared_source_hits), n),
        "near_identity_count": len(near_identity_hits),
        "near_identity_rate": _safe_rate(len(near_identity_hits), n),
        "co_occurrence_residual_count": len(co_residual_hits),
        "co_occurrence_residual_rate": _safe_rate(len(co_residual_hits), n),
        "weight_min": min((e["weight"] for e in edges), default=None),
        "weight_max": max((e["weight"] for e in edges), default=None),
        "weight_mean": (
            round(sum(float(e["weight"]) for e in edges) / n, 6) if n else None
        ),
        # Compact samples for human labeling — not a verdict.
        "alias_collision_sample": alias_hits[:10],
        "shared_source_sample": shared_source_hits[:10],
        "near_identity_sample": near_identity_hits[:10],
        "co_occurrence_residual_sample": co_residual_hits[:10],
    }


def build_affinity_quality_snapshot(
    conn: sqlite3.Connection,
    *,
    sample_pairs: int = 20,
    log_limit: int = 5,
) -> Dict[str, Any]:
    """Full read-only scorecard for the current node state."""
    from ..signal.derived_spec import ENGINE_CONFIG_KEY as DERIVED_SPEC_KEY
    from ..signal.derived_spec import derived_spec_version

    edges = _active_affinity_edges(conn)
    pair_metrics = score_affinity_pairs(conn, edges)
    logs = _latest_recompute_logs(conn, limit=log_limit)
    latest = logs[0] if logs else None

    pairs_sample = [
        {
            "src_entity_id": e["src_entity_id"],
            "dst_entity_id": e["dst_entity_id"],
            "src_name": e["src_name"],
            "dst_name": e["dst_name"],
            "weight": e["weight"],
            "evidence_count": e["evidence_count"],
            "label": None,  # useful | obvious | noise | alias — fill by hand
        }
        for e in edges[: max(0, int(sample_pairs))]
    ]

    live_hash = derived_spec_version()
    stored_hash = _read_engine_config(conn, DERIVED_SPEC_KEY)

    # Plain-language status for operators — not a quality grade.
    if pair_metrics["active_edges"] == 0:
        status = "empty_edge_set"
        status_note = (
            "No active semantic_affinity edges. On a sparse node this can be a "
            "correct withhold (floor/backstop/anti-alias), not a failure."
        )
    elif (pair_metrics["alias_collision_rate"] or 0) > 0 or (
        pair_metrics["near_identity_rate"] or 0
    ) > 0:
        status = "contamination_present"
        status_note = (
            "Surviving edges include alias and/or near-identical-centroid pairs. "
            "Do not treat edge count as a quality win until labels clear these."
        )
    elif (pair_metrics["shared_source_rate"] or 0) > 0.25:
        status = "shared_source_heavy"
        status_note = (
            "Many affinity pairs still share source documents — review samples "
            "before claiming role-shape signal."
        )
    else:
        status = "edges_present_cleanish"
        status_note = (
            "Edges exist with low automated contamination. Human labeling is "
            "still required before claiming improvement."
        )

    snapshot = {
        "scorecard_version": SCORECARD_VERSION,
        "ts": _now(),
        "status": status,
        "status_note": status_note,
        "derived_spec": {
            "live": live_hash,
            "stored": stored_hash,
            "changed": stored_hash is None or stored_hash != live_hash,
        },
        "calibration": {
            "configured_percentile": _configured_percentile(conn),
            "floor_abs": AFFINITY_FLOOR_ABS,
            "latest_recompute": latest,
            "recompute_history": logs,
        },
        "population": _population(conn),
        "pair_metrics": pair_metrics,
        "pairs_sample": pairs_sample,
        "honest_claims": {
            "can_claim_mechanism_withhold": pair_metrics["active_edges"] == 0,
            "can_claim_low_automated_contamination": (
                pair_metrics["active_edges"] > 0
                and (pair_metrics["alias_collision_rate"] or 0) == 0
                and (pair_metrics["near_identity_rate"] or 0) == 0
                and (pair_metrics["co_occurrence_residual_rate"] or 0) == 0
            ),
            "can_claim_quality_improved": False,
            "quality_improved_requires": (
                "human labels on pairs_sample (useful/obvious/noise/alias) "
                "compared across two snapshots with differing derived_spec.live"
            ),
        },
    }
    return attach_active_edge_keys(snapshot, edges)


def compare_snapshots(
    current: Dict[str, Any], previous: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Diff two scorecards for append-only history."""
    if not previous or previous.get("scorecard_version") != current.get(
        "scorecard_version"
    ):
        return {"compared_to": None, "edge_set_jaccard": None}

    def _pair_keys(snap: Dict[str, Any]) -> Set[Tuple[str, str]]:
        keys: Set[Tuple[str, str]] = set()
        for row in snap.get("pairs_sample") or []:
            a, b = row.get("src_entity_id"), row.get("dst_entity_id")
            if a and b:
                keys.add(_canonical(str(a), str(b)))
        # Prefer full active set if present on a richer export.
        for row in (snap.get("pair_metrics") or {}).get("all_pairs") or []:
            a, b = row.get("src_entity_id"), row.get("dst_entity_id")
            if a and b:
                keys.add(_canonical(str(a), str(b)))
        return keys

    # Reconstruct edge keys from samples is incomplete when > sample_pairs.
    # Prefer explicit active_edge_keys if callers stash them.
    cur_keys = set(tuple(p) for p in current.get("active_edge_keys") or [])
    prev_keys = set(tuple(p) for p in previous.get("active_edge_keys") or [])
    if not cur_keys:
        cur_keys = _pair_keys(current)
    if not prev_keys:
        prev_keys = _pair_keys(previous)

    union = cur_keys | prev_keys
    jaccard = (
        round(len(cur_keys & prev_keys) / len(union), 4) if union else None
    )

    cur_m = current.get("pair_metrics") or {}
    prev_m = previous.get("pair_metrics") or {}

    def _delta(key: str) -> Optional[float]:
        a, b = cur_m.get(key), prev_m.get(key)
        if a is None or b is None:
            return None
        try:
            return round(float(a) - float(b), 4)
        except (TypeError, ValueError):
            return None

    return {
        "compared_to": previous.get("ts"),
        "derived_spec_changed": (
            (current.get("derived_spec") or {}).get("live")
            != (previous.get("derived_spec") or {}).get("live")
        ),
        "edge_set_jaccard": jaccard,
        "active_edges_delta": _delta("active_edges"),
        "alias_collision_rate_delta": _delta("alias_collision_rate"),
        "shared_source_rate_delta": _delta("shared_source_rate"),
        "near_identity_rate_delta": _delta("near_identity_rate"),
        "co_occurrence_residual_rate_delta": _delta("co_occurrence_residual_rate"),
        "status_from": previous.get("status"),
        "status_to": current.get("status"),
    }


def attach_active_edge_keys(
    snapshot: Dict[str, Any], edges: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """Store the full active pair key list for accurate Jaccard diffs."""
    snapshot = dict(snapshot)
    snapshot["active_edge_keys"] = [
        list(_canonical(str(e["src_entity_id"]), str(e["dst_entity_id"])))
        for e in edges
    ]
    return snapshot
