"""Stable community naming — identity & history core (PLAN_COMMUNITY_NAMING S1).

A community's identity is its weighted core (top-k most central members). Names
bind to cores, not to Louvain indices: while a rebuilt community's core matches
a historical fingerprint, it keeps its name and no model runs. The periphery
can churn freely — the core IS the community.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Dict, List, Optional, Sequence, Set, Tuple

CORE_K = 12
MATCH_THRESHOLD = 0.5


def core_fingerprint(
    ranked_members: Sequence[str],
    weights: Dict[str, float],
    k: int = CORE_K,
    exclude: Optional[Set[str]] = None,
) -> List[Tuple[str, float]]:
    """Top-k members with normalized weights. `ranked_members` is already
    sorted by centrality (the compute_communities ordering).

    `exclude` drops the OWNER before the core is taken. The owner is adjacent to
    almost everything, so they rank at or near the top of every community they
    appear in — consuming a core slot and, worse, carrying a large share of the
    normalized weight. A member common to two fingerprints raises their weighted
    Jaccard, so an ego left in the core makes unrelated communities look alike and
    biases matching toward false positives.

    Measured live 2026-08-26: only 1 of 127 active fingerprints contained the ego —
    but in that one it held 0.598 of the total weight across two core slots, i.e.
    the community's recorded identity was mostly "the owner".
    """
    exclude = exclude or set()
    picked = [str(m) for m in ranked_members if str(m) not in exclude][:k]
    core = [(m, max(float(weights.get(m, 0.0)), 1e-9)) for m in picked]
    total = sum(w for _, w in core) or 1.0
    return [(eid, w / total) for eid, w in core]


def weighted_jaccard(a: Sequence[Tuple[str, float]], b: Sequence[Tuple[str, float]]) -> float:
    da, db = dict(a), dict(b)
    keys = set(da) | set(db)
    if not keys:
        return 0.0
    inter = sum(min(da.get(x, 0.0), db.get(x, 0.0)) for x in keys)
    union = sum(max(da.get(x, 0.0), db.get(x, 0.0)) for x in keys)
    return inter / union if union else 0.0


def match_name(
    conn: sqlite3.Connection,
    fingerprint: Sequence[Tuple[str, float]],
    threshold: float = MATCH_THRESHOLD,
) -> Optional[Dict[str, object]]:
    """Best non-retired historical name whose core overlaps enough. Owner-sourced
    names win ties: a rename is the owner's word and outranks a derived name at
    equal similarity."""
    best: Optional[Dict[str, object]] = None
    best_key: Tuple[float, int] = (0.0, 0)
    try:
        rows = conn.execute(
            "SELECT name_id, name, fingerprint_json, source, times_matched"
            " FROM community_names WHERE retired_at IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return None          # pre-migration-67 node
    for name_id, name, fp_json, source, times in rows:
        try:
            fp = [(str(e), float(w)) for e, w in json.loads(fp_json or "[]")]
        except (ValueError, TypeError):
            continue
        sim = weighted_jaccard(fingerprint, fp)
        if sim < threshold:
            continue
        key = (sim, 1 if source == "owner" else 0)
        if key > best_key:
            best_key = key
            best = {"name_id": name_id, "name": name, "source": source,
                    "similarity": sim, "times_matched": times}
    return best


def record_name(
    conn: sqlite3.Connection,
    name: str,
    fingerprint: Sequence[Tuple[str, float]],
    *,
    source: str,
    model: Optional[str] = None,
) -> str:
    name_id = f"cmn_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO community_names (name_id, name, fingerprint_json, source, model,"
        " last_matched_at, times_matched) VALUES (?, ?, ?, ?, ?, datetime('now'), 1)",
        (name_id, name, json.dumps(list(fingerprint)), source, model),
    )
    return name_id


def touch_name(conn: sqlite3.Connection, name_id: str,
               fingerprint: Sequence[Tuple[str, float]]) -> None:
    """A match refreshes the stored core: communities drift slowly, and the
    fingerprint should follow the drift it just matched (ship-of-Theseus on
    purpose — continuity of identity IS gradual replacement survived)."""
    conn.execute(
        "UPDATE community_names SET last_matched_at=datetime('now'),"
        " times_matched=times_matched+1, fingerprint_json=? WHERE name_id=?",
        (json.dumps(list(fingerprint)), name_id),
    )


def rename_community(
    conn: sqlite3.Connection,
    fingerprint: Sequence[Tuple[str, float]],
    new_name: str,
) -> str:
    """Owner rename: retire any matching derived name, record the owner's."""
    current = match_name(conn, fingerprint, threshold=0.3)
    if current:
        conn.execute("UPDATE community_names SET retired_at=datetime('now')"
                     " WHERE name_id=?", (current["name_id"],))
    return record_name(conn, new_name, fingerprint, source="owner")
