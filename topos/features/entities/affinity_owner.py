"""Owner-facing affinity status, config, recompute, and pair labeling.

Graph recipe controls (percentile / too-few·too-many / recompute) and the
People review queue read through this module. Weight scales stay separated
from Complexity (D1); this is display + calibration only.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from .affinity import (
    AFFINITY_DEFAULT_PERCENTILE,
    AFFINITY_FLOOR_ABS,
    AFFINITY_TOPN,
    ENGINE_CONFIG_KEY_AFFINITY_PERCENTILE,
    _active_co_occurrence_pairs,
    _estimate_floor,
    _load_centroids,
    _merge_candidate_pairs,
    _mutual_top_pairs,
    _resolve_percentile,
    rebuild_affinity_edges,
)
from .edges import EDGE_SEMANTIC_AFFINITY, _canonical_order

logger = logging.getLogger(__name__)

NudgeDirection = Literal["fewer", "more", "ok"]
LabelValue = Literal["useful", "obvious", "wrong", "same_person"]

PERCENTILE_MIN = 90.0
PERCENTILE_MAX = 99.9
NUDGE_STEP = 0.5

VALID_LABELS = frozenset({"useful", "obvious", "wrong", "same_person"})

#: Cap near-miss rows returned alongside active unlabeled edges.
NEAR_MISS_LIMIT = 24


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entity_names(conn: sqlite3.Connection, ids: List[str]) -> Dict[str, str]:
    if not ids:
        return {}
    out: Dict[str, str] = {}
    for chunk_start in range(0, len(ids), 400):
        chunk = ids[chunk_start : chunk_start + 400]
        rows = conn.execute(
            f"""
            SELECT entity_id, canonical_name FROM entities
            WHERE entity_id IN ({",".join("?" * len(chunk))})
            """,
            chunk,
        ).fetchall()
        for entity_id, name in rows:
            out[str(entity_id)] = str(name or entity_id)
    return out


def current_percentile(conn: sqlite3.Connection) -> float:
    return _resolve_percentile(conn, None)


def set_affinity_percentile(conn: sqlite3.Connection, percentile: float) -> float:
    """Clamp and persist P; returns the value stored."""
    from ...core.state import set_engine_config_value

    p = max(PERCENTILE_MIN, min(PERCENTILE_MAX, float(percentile)))
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_AFFINITY_PERCENTILE, str(p))
    return p


def nudge_percentile(conn: sqlite3.Connection, direction: NudgeDirection) -> Dict[str, Any]:
    """Outcome-based density control. fewer → lower P; more → raise P."""
    before = current_percentile(conn)
    if direction == "fewer":
        after = set_affinity_percentile(conn, before - NUDGE_STEP)
    elif direction == "more":
        after = set_affinity_percentile(conn, before + NUDGE_STEP)
    elif direction == "ok":
        after = before
    else:
        raise ValueError(f"unknown nudge direction: {direction!r}")
    return {
        "nudge": direction,
        "percentile_before": before,
        "percentile": after,
        "changed": after != before,
        "recompute_needed": after != before,
    }


def _last_log(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    try:
        row = conn.execute(
            """
            SELECT recompute_id, entities_considered, pairs_considered, edges_written,
                   ceiling, ceiling_hit, percentile, resolved_cosine, floor_cosine,
                   spec_version, created_at
            FROM affinity_recompute_log
            ORDER BY recompute_id DESC LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {
        "recompute_id": row[0],
        "entities_considered": row[1],
        "pairs_considered": row[2],
        "edges_written": row[3],
        "ceiling": row[4],
        "ceiling_hit": bool(row[5]),
        "percentile": row[6],
        "resolved_cosine": row[7],
        "floor_cosine": row[8],
        "spec_version": row[9],
        "created_at": row[10],
    }


def _active_affinity_count(conn: sqlite3.Connection) -> int:
    try:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM entity_edges
                WHERE edge_type = ? AND valid_to IS NULL
                """,
                (EDGE_SEMANTIC_AFFINITY,),
            ).fetchone()[0]
        )
    except sqlite3.OperationalError:
        return 0


def _verdict(*, edges: int, entities: int, ceiling_hit: bool) -> str:
    if edges == 0:
        return "empty"
    if ceiling_hit or edges > max(20, entities):
        return "noisy"
    if edges < 3:
        return "sparse"
    return "balanced"


def get_affinity_status(conn: sqlite3.Connection) -> Dict[str, Any]:
    percentile = current_percentile(conn)
    log = _last_log(conn)
    active = _active_affinity_count(conn)
    entities = int((log or {}).get("entities_considered") or 0)
    ceiling_hit = bool((log or {}).get("ceiling_hit"))
    verdict = _verdict(edges=active, entities=entities, ceiling_hit=ceiling_hit)
    return {
        "percentile": percentile,
        "default_percentile": AFFINITY_DEFAULT_PERCENTILE,
        "floor_abs": AFFINITY_FLOOR_ABS,
        "active_edges": active,
        "verdict": verdict,
        "last_recompute": log,
        "resolved_cosine": (log or {}).get("resolved_cosine"),
        "floor_cosine": (log or {}).get("floor_cosine"),
        "ceiling_hit": ceiling_hit,
        "copy": {
            "empty": "No latent affinity links yet — the node may be sparse, or the floor is strict.",
            "sparse": "Few latent links. Try Too few if you expect more role-shape neighbours.",
            "balanced": "Latent affinity looks in a usable range.",
            "noisy": "Many latent links (or ceiling hit). Try Too many to tighten.",
        }.get(verdict, ""),
    }


def apply_affinity_config(
    conn: sqlite3.Connection,
    *,
    percentile: Optional[float] = None,
    nudge: Optional[NudgeDirection] = None,
) -> Dict[str, Any]:
    nudge_result: Optional[Dict[str, Any]] = None
    if nudge is not None:
        nudge_result = nudge_percentile(conn, nudge)
    elif percentile is not None:
        before = current_percentile(conn)
        after = set_affinity_percentile(conn, percentile)
        nudge_result = {
            "nudge": None,
            "percentile_before": before,
            "percentile": after,
            "changed": after != before,
            "recompute_needed": after != before,
        }
    status = get_affinity_status(conn)
    if nudge_result:
        status["config_change"] = nudge_result
    return status


def recompute_affinity_now(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Rebuild centroids then affinity edges; return status + rebuild stats."""
    from .context_vectors import rebuild_entity_context_vectors

    centroids = rebuild_entity_context_vectors(conn, commit=False)
    affinity = rebuild_affinity_edges(conn, commit=True)
    status = get_affinity_status(conn)
    return {
        "status": status,
        "centroids": centroids,
        "affinity": affinity,
    }


def _labeled_pair_keys(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute(
            "SELECT src_entity_id, dst_entity_id FROM affinity_pair_labels"
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {(str(a), str(b)) for a, b in rows}


def _pair_key(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a < b else (b, a)


def diagnose_suppressed_pairs(
    conn: sqlite3.Connection,
    *,
    topn: int = AFFINITY_TOPN,
    limit: int = NEAR_MISS_LIMIT,
) -> List[Dict[str, Any]]:
    """Near-misses with suppress_reason, for the owner review queue."""
    from ..lifecycle.blackhole import blackholed_entity_ids

    blocked = blackholed_entity_ids(conn)
    centroids = _load_centroids(conn)
    if len(centroids) < 2:
        return []
    percentile = _resolve_percentile(conn, None)
    resolved = _estimate_floor(centroids, percentile)
    floor = max(resolved, AFFINITY_FLOOR_ABS)

    # Mutual top-N with no floor — then classify why each pair would not write.
    mutual = _mutual_top_pairs(centroids, topn=max(1, int(topn)), floor=0.0)
    co_occurring = _active_co_occurrence_pairs(conn)
    merge_candidates = _merge_candidate_pairs(conn)

    suppressed: List[Dict[str, Any]] = []
    for a, b, cosine, evidence in mutual:
        if a in blocked or b in blocked:
            reason = "blackhole_frozen"
        elif (a, b) in co_occurring:
            reason = "co_occurrence"
        elif (a, b) in merge_candidates:
            reason = "merge_candidate"
        elif cosine < floor:
            reason = "below_floor"
        else:
            continue
        src, dst = _pair_key(a, b)
        suppressed.append(
            {
                "src_entity_id": src,
                "dst_entity_id": dst,
                "cosine": round(float(cosine), 4),
                "evidence_count": int(evidence),
                "suppress_reason": reason,
                "kind": "near_miss",
            }
        )
    # Prefer stronger cosines first; keep a mix of reasons if possible.
    suppressed.sort(key=lambda row: (-float(row["cosine"]), row["src_entity_id"]))
    return suppressed[: max(0, int(limit))]


def list_affinity_pairs_for_review(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> Dict[str, Any]:
    labeled = _labeled_pair_keys(conn)
    active_rows = conn.execute(
        """
        SELECT src_entity_id, dst_entity_id, weight, evidence_count
        FROM entity_edges
        WHERE edge_type = ? AND valid_to IS NULL
        ORDER BY weight DESC
        """,
        (EDGE_SEMANTIC_AFFINITY,),
    ).fetchall()

    items: List[Dict[str, Any]] = []
    for src, dst, weight, evidence in active_rows:
        key = _pair_key(str(src), str(dst))
        if key in labeled:
            continue
        items.append(
            {
                "src_entity_id": key[0],
                "dst_entity_id": key[1],
                "cosine": round(float(weight or 0.0), 4),
                "evidence_count": int(evidence or 0),
                "suppress_reason": None,
                "kind": "active",
            }
        )

    near = diagnose_suppressed_pairs(conn)
    for row in near:
        key = (row["src_entity_id"], row["dst_entity_id"])
        if key in labeled:
            continue
        # Skip near-misses that are already active (shouldn't happen).
        if any(
            i["src_entity_id"] == key[0] and i["dst_entity_id"] == key[1] for i in items
        ):
            continue
        items.append(row)

    items = items[: max(1, int(limit))]
    ids = sorted(
        {i["src_entity_id"] for i in items} | {i["dst_entity_id"] for i in items}
    )
    names = _entity_names(conn, ids)
    for item in items:
        item["src_name"] = names.get(item["src_entity_id"], item["src_entity_id"])
        item["dst_name"] = names.get(item["dst_entity_id"], item["dst_entity_id"])
        item["pair_id"] = f"{item['src_entity_id']}::{item['dst_entity_id']}"

    return {"items": items, "total": len(items), "status": get_affinity_status(conn)}


def label_affinity_pair(
    conn: sqlite3.Connection,
    *,
    entity_a: str,
    entity_b: str,
    label: str,
    note: Optional[str] = None,
    cosine: Optional[float] = None,
) -> Dict[str, Any]:
    if label not in VALID_LABELS:
        raise ValueError(
            f"label must be one of {sorted(VALID_LABELS)}, got {label!r}"
        )
    a = str(entity_a or "").strip()
    b = str(entity_b or "").strip()
    if not a or not b or a == b:
        raise ValueError("two distinct entity ids are required")
    src, dst = _canonical_order(a, b, EDGE_SEMANTIC_AFFINITY)

    if cosine is None:
        row = conn.execute(
            """
            SELECT weight FROM entity_edges
            WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=?
              AND valid_to IS NULL
            """,
            (src, dst, EDGE_SEMANTIC_AFFINITY),
        ).fetchone()
        cosine = float(row[0]) if row else None

    label_id = f"apl_{uuid.uuid4().hex[:16]}"
    now = _now_iso()
    from ...storage.db.write_gate import with_db_write

    with with_db_write():
        conn.execute(
            """
            INSERT INTO affinity_pair_labels
                (label_id, src_entity_id, dst_entity_id, cosine, label, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(src_entity_id, dst_entity_id) DO UPDATE SET
                label=excluded.label,
                note=excluded.note,
                cosine=COALESCE(excluded.cosine, affinity_pair_labels.cosine),
                created_at=excluded.created_at,
                label_id=excluded.label_id
            """,
            (label_id, src, dst, cosine, label, note, now),
        )
        conn.commit()

    return {
        "label_id": label_id,
        "src_entity_id": src,
        "dst_entity_id": dst,
        "label": label,
        "note": note,
        "cosine": cosine,
        "created_at": now,
    }
