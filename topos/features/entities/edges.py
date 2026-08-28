"""Typed, time-decayed entity edges with validity intervals (B2.2).

Weight update on new evidence:  w <- w * 0.5^(dt / half_life) + increment
O(1) per edge, no rescans; stale relationships fade instead of accumulating
forever (raw counts made 2021 coworkers look as close as current ones).

Validity (B2.2): each (src, dst, type) triple has at most ONE active row
(valid_to IS NULL — enforced by a partial unique index); ended relationships
are CLOSED via supersede_edge (valid_to stamped, row kept), so edge history
accumulates and past-tense queries can surface former connections with an
explicit staleness marker.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..stats.fold import parse_ts

DEFAULT_HALF_LIFE_DAYS = 60.0

EDGE_CO_OCCURRENCE = "co_occurrence"
EDGE_COMMUNICATES = "communicates_with"
EDGE_DISCUSSES = "discusses"
EDGE_LOCATED_AT = "located_at"
EDGE_PART_OF = "part_of"  # directed: product/sub-unit -> parent org

#: Latent affinity between two entities occupying the same role-shape in the
#: owner's life (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §3.2). Written ONLY by
#: features/entities/affinity.py:rebuild_affinity_edges, never by ``update_edge``
#: — its weight is a bounded cosine snapshot, not folded evidence.
EDGE_SEMANTIC_AFFINITY = "semantic_affinity"

#: Edge types for which (A,B) and (B,A) are the same fact and must share a row.
#: ``semantic_affinity`` belongs here because cosine is symmetric: without a
#: canonical order one rebuild would write A->B where the last wrote B->A, so
#: the earlier row would never be superseded and duplicate pairs would pile up.
_SYMMETRIC_EDGE_TYPES = (EDGE_CO_OCCURRENCE, EDGE_COMMUNICATES, EDGE_SEMANTIC_AFFINITY)

#: Upper bound on entities folded into co-occurrence for ONE record.
#:
#: A bound is prudent — the fold is O(n^2) and a long-document connector could
#: hand it a hundred entities — but it must be the same bound everywhere, and 8
#: was not. Measured on the owner's node 2026-08-27: 3,372 records carry <=8
#: entities and the cap never touches them; exactly 5 exceed it, the largest at
#: 13. So 8 bought no protection worth having and cost 166 pair-observations.
#: 32 leaves today's data entirely untouched while still refusing a pathological
#: record.
CO_OCCURRENCE_MAX_ENTITIES_PER_RECORD = 32


def record_cooccurrence_pairs(entity_ids):
    """Every unordered pair of distinct entities named in one record.

    THE single definition, because there were two and they disagreed. The
    rebuild in ``maintenance.rebuild_evidence_edges`` truncated each record to 8
    entities with the comment "(mirrors the ingest path)"; the ingest path in
    ``entities_job._resolve_into_spine`` had no cap at all. Since the rebuild
    DELETEs the whole active co-occurrence set before re-inserting, and does so
    unconditionally with no ``valid_to`` tombstone, every maintenance run
    silently destroyed the edges ingest had created for any record above the cap
    — 66 of them, measured. The graph was not a function of the evidence; it was
    a function of which writer ran last, and nothing recorded the difference.

    The truncation was also arbitrary in a way a cap should never be. Insertion
    order comes from the read query's ``ORDER BY COALESCE(event_at, created_at)``,
    which orders ACROSS records; within a record every mention shares an
    event_at, so the surviving 8 were decided by SQLite's tie-break — in
    practice the order the extractor emitted spans. "Who shows up alongside X"
    was answered by paragraph position, and would change under a model swap with
    no schema change to signal it. Sorting makes the retained set deterministic
    at least, so two runs over the same evidence agree.
    """
    unique = sorted(dict.fromkeys(str(e) for e in entity_ids if str(e).strip()))
    if len(unique) > CO_OCCURRENCE_MAX_ENTITIES_PER_RECORD:
        unique = unique[:CO_OCCURRENCE_MAX_ENTITIES_PER_RECORD]
    return [
        (unique[i], unique[j])
        for i in range(len(unique))
        for j in range(i + 1, len(unique))
    ]



def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_order(src_entity_id: str, dst_entity_id: str, edge_type: str):
    """Undirected edge types get a canonical order so A-B and B-A share a row."""
    if edge_type in _SYMMETRIC_EDGE_TYPES and dst_entity_id < src_entity_id:
        return dst_entity_id, src_entity_id
    return src_entity_id, dst_entity_id


def fold_edge_observation(
    weight: Any,
    evidence_count: Any,
    last_event_at: Optional[str],
    event_at: Optional[str],
    *,
    increment: float = 1.0,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> tuple[float, int, Optional[str]]:
    """Fold one observation into an edge's (weight, evidence_count, last_event_at).

    The single source of the decay-then-add rule, shared by :func:`update_edge`
    (row-at-a-time ingest) and the rebuild's in-memory accumulator — the two
    must converge on identical weights or a rebuild would silently rescale the
    graph.
    """
    decayed = float(weight or 0.0)
    prev_ts, new_ts = parse_ts(last_event_at), parse_ts(event_at)
    if prev_ts is not None and new_ts is not None and new_ts > prev_ts:
        dt_days = (new_ts - prev_ts).total_seconds() / 86400.0
        decayed *= math.pow(0.5, dt_days / half_life_days)
    latest = max(filter(None, [last_event_at, event_at]), default=None)
    return decayed + float(increment), int(evidence_count or 0) + 1, latest


def update_edge(
    conn: sqlite3.Connection,
    *,
    src_entity_id: str,
    dst_entity_id: str,
    edge_type: str,
    event_at: Optional[str] = None,
    increment: float = 1.0,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> None:
    if not src_entity_id or not dst_entity_id or src_entity_id == dst_entity_id:
        return
    src_entity_id, dst_entity_id = _canonical_order(src_entity_id, dst_entity_id, edge_type)

    # Only the ACTIVE row folds new evidence; closed revisions are history.
    row = conn.execute(
        """
        SELECT edge_id, weight, evidence_count, last_event_at FROM entity_edges
        WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=? AND valid_to IS NULL
        """,
        (src_entity_id, dst_entity_id, edge_type),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO entity_edges (
                edge_id, src_entity_id, dst_entity_id, edge_type,
                weight, evidence_count, last_event_at, valid_from
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                f"edg_{uuid.uuid4().hex[:16]}",
                src_entity_id,
                dst_entity_id,
                edge_type,
                float(increment),
                event_at,
                _now_iso(),  # belief validity: when the edge started being held
            ),
        )
        return

    edge_id, weight, evidence_count, last_event_at = row
    new_weight, new_count, latest = fold_edge_observation(
        weight,
        evidence_count,
        last_event_at,
        event_at,
        increment=increment,
        half_life_days=half_life_days,
    )
    conn.execute(
        """
        UPDATE entity_edges SET weight=?, evidence_count=?, last_event_at=?, updated_at=datetime('now')
        WHERE edge_id=?
        """,
        (new_weight, new_count, latest, edge_id),
    )


def _fold_weights(
    keep_weight: Any,
    keep_last: Optional[str],
    absorb_weight: Any,
    absorb_last: Optional[str],
):
    """Combine two edge weights under the half-life model.

    Decays the earlier-dated weight forward to the later timestamp, then sums —
    the same decay-then-add rule as ``update_edge``, so a merge and a fresh
    re-observation converge on the same weight. Returns (weight, last_event_at).
    """
    wk = float(keep_weight or 0.0)
    wa = float(absorb_weight or 0.0)
    tk, ta = parse_ts(keep_last), parse_ts(absorb_last)
    if tk is not None and ta is not None:
        if ta > tk:
            wk *= math.pow(0.5, (ta - tk).total_seconds() / 86400.0 / DEFAULT_HALF_LIFE_DAYS)
            return wk + wa, absorb_last
        if tk > ta:
            wa *= math.pow(0.5, (tk - ta).total_seconds() / 86400.0 / DEFAULT_HALF_LIFE_DAYS)
            return wk + wa, keep_last
    return wk + wa, max(filter(None, [keep_last, absorb_last]), default=None)


def merge_entity_edges(
    conn: sqlite3.Connection,
    *,
    keep_id: str,
    absorb_id: str,
) -> None:
    """Rewrite ``absorb_id`` -> ``keep_id`` across entity_edges on a merge.

    A blanket ``UPDATE ... SET src_entity_id=keep WHERE src_entity_id=absorb``
    raises IntegrityError when both entities hold an ACTIVE edge of the same
    type to the same third entity — the active-row partial unique index
    (idx_entity_edges_active) forbids two active rows for one (src, dst, type).
    So per edge: canonicalise the rewritten endpoints, drop edges that collapse
    to a self-loop, and — for an active edge that now collides with an existing
    active twin — fold weight/evidence_count/last_event_at into the survivor and
    delete the duplicate. Closed rows (valid_to set) are unconstrained and
    rewrite in place.
    """
    if not keep_id or not absorb_id or keep_id == absorb_id:
        return
    rows = conn.execute(
        """
        SELECT edge_id, src_entity_id, dst_entity_id, edge_type,
               weight, evidence_count, last_event_at, valid_to
        FROM entity_edges
        WHERE src_entity_id=? OR dst_entity_id=?
        """,
        (absorb_id, absorb_id),
    ).fetchall()
    for edge_id, src, dst, edge_type, weight, evidence, last_at, valid_to in rows:
        new_src = keep_id if src == absorb_id else src
        new_dst = keep_id if dst == absorb_id else dst
        new_src, new_dst = _canonical_order(new_src, new_dst, edge_type)
        if new_src == new_dst:
            # merged endpoints collapsed to a self-loop — meaningless, drop it.
            conn.execute("DELETE FROM entity_edges WHERE edge_id=?", (edge_id,))
            continue
        if valid_to is None:
            twin = conn.execute(
                """
                SELECT edge_id, weight, evidence_count, last_event_at
                FROM entity_edges
                WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=?
                  AND valid_to IS NULL AND edge_id<>?
                """,
                (new_src, new_dst, edge_type, edge_id),
            ).fetchone()
            if twin is not None:
                twin_id, twin_weight, twin_evidence, twin_last = twin
                folded_weight, folded_last = _fold_weights(
                    twin_weight, twin_last, weight, last_at
                )
                conn.execute(
                    """
                    UPDATE entity_edges
                    SET weight=?, evidence_count=?, last_event_at=?, updated_at=datetime('now')
                    WHERE edge_id=?
                    """,
                    (
                        folded_weight,
                        int(twin_evidence or 0) + int(evidence or 0),
                        folded_last,
                        twin_id,
                    ),
                )
                conn.execute("DELETE FROM entity_edges WHERE edge_id=?", (edge_id,))
                continue
        conn.execute(
            """
            UPDATE entity_edges
            SET src_entity_id=?, dst_entity_id=?, updated_at=datetime('now')
            WHERE edge_id=?
            """,
            (new_src, new_dst, edge_id),
        )


def supersede_edge(
    conn: sqlite3.Connection,
    *,
    src_entity_id: str,
    dst_entity_id: str,
    edge_type: str,
    valid_to: Optional[str] = None,
    successor: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Close the ACTIVE edge for the triple; optionally insert a successor.

    The closed row keeps its weight/evidence history and gains ``valid_to``
    (default: now) — history is never deleted, so past-tense queries can
    surface the ended relationship with a staleness marker (T7).

    ``successor`` (optional) inserts a new ACTIVE edge chained at the close
    instant (successor.valid_from == closed.valid_to, the FactStore pattern).
    Keys: ``edge_type`` / ``dst_entity_id`` / ``src_entity_id`` (default: the
    closed edge's), ``weight`` (default 1.0), ``event_at`` (default None).

    Returns the closed edge_id, or None when no active row matched.
    """
    if not src_entity_id or not dst_entity_id:
        return None
    src_entity_id, dst_entity_id = _canonical_order(src_entity_id, dst_entity_id, edge_type)
    row = conn.execute(
        """
        SELECT edge_id FROM entity_edges
        WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=? AND valid_to IS NULL
        """,
        (src_entity_id, dst_entity_id, edge_type),
    ).fetchone()
    if row is None:
        return None
    closed_at = valid_to or _now_iso()
    conn.execute(
        "UPDATE entity_edges SET valid_to=?, updated_at=datetime('now') WHERE edge_id=?",
        (closed_at, row[0]),
    )
    if successor:
        s_type = str(successor.get("edge_type") or edge_type)
        s_src = str(successor.get("src_entity_id") or src_entity_id)
        s_dst = str(successor.get("dst_entity_id") or dst_entity_id)
        s_src, s_dst = _canonical_order(s_src, s_dst, s_type)
        conn.execute(
            """
            INSERT INTO entity_edges (
                edge_id, src_entity_id, dst_entity_id, edge_type,
                weight, evidence_count, last_event_at, valid_from
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                f"edg_{uuid.uuid4().hex[:16]}",
                s_src,
                s_dst,
                s_type,
                float(successor.get("weight") or 1.0),
                successor.get("event_at"),
                closed_at,
            ),
        )
    return str(row[0])


def top_edges(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    edge_type: Optional[str] = None,
    limit: int = 20,
    include_closed: bool = False,
) -> List[Dict[str, Any]]:
    params: List[Any] = [entity_id, entity_id]
    type_clause = ""
    if edge_type:
        type_clause = " AND edge_type=?"
        params.append(edge_type)
    closed_clause = "" if include_closed else " AND e.valid_to IS NULL"
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT e.src_entity_id, e.dst_entity_id, e.edge_type, e.weight, e.evidence_count,
               e.last_event_at, e.valid_from, e.valid_to
        FROM entity_edges e
        WHERE (e.src_entity_id=? OR e.dst_entity_id=?){type_clause}{closed_clause}
        ORDER BY (e.valid_to IS NULL) DESC, e.weight DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    out = []
    for src, dst, etype, weight, evidence, last_at, valid_from, valid_to in rows:
        other = dst if src == entity_id else src
        name_row = conn.execute(
            "SELECT canonical_name, entity_type FROM entities WHERE entity_id=?", (other,)
        ).fetchone()
        out.append(
            {
                "entity_id": other,
                "entity_name": name_row[0] if name_row else other,
                "entity_type": name_row[1] if name_row else None,
                "edge_type": etype,
                "weight": round(float(weight or 0.0), 4),
                "evidence_count": evidence,
                "last_event_at": last_at,
                "valid_from": valid_from,
                "valid_to": valid_to,
            }
        )
    return out


def _allocate_edge_budget(type_counts, window: int) -> Dict[str, int]:
    """How many slots each edge type gets in a page of ``window`` edges.

    Proportional to the type's size, with a floor so a small relation is never
    squeezed to nothing, then leftovers handed back to the largest types. Never
    allocates more than a type actually has.

    The floor is what stops this being just a slower version of the bug it
    fixes: without it, proportional allocation still hands almost everything to
    the biggest relation.
    """
    total = sum(n for _t, n in type_counts)
    if not type_counts or total <= 0 or window <= 0:
        return {}
    ordered = sorted(type_counts, key=lambda tn: -tn[1])
    floor = max(1, window // (len(ordered) * 3))
    budgets: Dict[str, int] = {}
    for edge_type, n in ordered:
        share = max(floor, round(window * (n / total)))
        budgets[edge_type] = min(share, n)
    # Rounding plus the floor can over-subscribe: three types over a 300 page
    # summed to 309. The SQL LIMIT would then drop the excess by GLOBAL weight,
    # handing those slots straight back to the heaviest relation — the bias this
    # function exists to remove, reappearing in the last nine rows. Trim from the
    # largest budgets first so the cut lands where there is most to spare.
    used = sum(budgets.values())
    while used > window:
        edge_type = max(budgets, key=lambda t: budgets[t])
        if budgets[edge_type] <= 1:
            break
        take = min(budgets[edge_type] - 1, used - window)
        budgets[edge_type] -= take
        used -= take

    for edge_type, n in ordered:
        if used >= window:
            break
        extra = min(n - budgets[edge_type], window - used)
        budgets[edge_type] += extra
        used += extra
    return budgets


def graph_snapshot(
    conn: sqlite3.Connection,
    *,
    limit_nodes: int = 100,
    limit_edges: int = 300,
    min_weight: float = 0.0,
    include_closed: bool = False,
    as_of: Optional[str] = None,
    selection: str = "weight",
    offset: int = 0,
) -> Dict[str, Any]:
    """Entity graph in the legacy list_graph shape (nodes/edges dicts).

    Validity fields ride first-class on each edge (graph-UI audit): active
    edges carry valid_to=None; include_closed=True adds ended revisions.

    ``as_of`` (ISO date/timestamp) returns the graph AS IT STOOD at that instant
    — edges whose validity window covers it (valid_from <= as_of < valid_to) —
    driving a temporal scrubber. It supersedes include_closed (a point-in-time
    view is neither "active now" nor "all history").

    ``selection``:
      - ``weight`` (default): ORDER BY weight DESC — MCP / firewall-safe slice.
      - ``all``: ORDER BY recency (last_event_at/valid_from) — owner UI full graph.

    ``offset`` pages the ordered edge list for Load more. Response includes
    ``meta`` with truncation flags and ``next_offset`` when more edges remain.
    """
    sel = str(selection or "weight").strip().lower()
    if sel not in ("weight", "all"):
        sel = "weight"
    off = max(0, int(offset or 0))
    lim_e = max(1, int(limit_edges))
    lim_n = max(1, int(limit_nodes))

    # Affinity weights are cosines in [0, 1] (see affinity.py); the caller's
    # min_weight is calibrated for accumulating-count spine edges. Applying the
    # same floor would hide every latent edge. Quality is already gated at write
    # time (AFFINITY_FLOOR_ABS), so semantic_affinity is exempt from this filter.
    where_params: List[Any] = [EDGE_SEMANTIC_AFFINITY, min_weight]
    if as_of:
        clause = (
            " AND (valid_from IS NULL OR valid_from <= ?)"
            " AND (valid_to IS NULL OR valid_to > ?)"
        )
        where_params.extend([as_of, as_of])
    elif include_closed:
        clause = ""
    else:
        clause = " AND valid_to IS NULL"

    where_sql = (
        f"FROM entity_edges WHERE (edge_type = ? OR weight >= ?){clause}"
    )
    total_edges_matching = int(
        conn.execute(f"SELECT COUNT(*) {where_sql}", tuple(where_params)).fetchone()[0]
    )

    if sel == "all":
        order_sql = (
            "ORDER BY COALESCE(last_event_at, valid_from) DESC, edge_id DESC"
        )
    else:
        order_sql = "ORDER BY weight DESC, edge_id DESC"

    # Allocate the page ACROSS edge types before ordering within them.
    #
    # A single global `ORDER BY weight DESC` ranks scales that are not
    # comparable. `communicates_with` accumulates message counts and reaches
    # 2,772; `co_occurrence` accumulates co-mention counts and tops out near 7;
    # `located_at` is a computed 2.25-10 band. Sorting them together let one
    # relation swallow the page: measured on the owner's node 2026-08-27,
    # `communicates_with` is 4.9% of the graph (267 of 5,445 edges) and took
    # **203 of 300 slots**, while `relates_to` — the LARGEST relation at 2,030
    # edges, 37% of the graph — got **zero**, along with `discusses` and
    # `participates_in`.
    #
    # Each type now gets a share proportional to its size, with a floor so a
    # small relation is never squeezed out entirely, and the caller's
    # `selection` still orders WITHIN each type. Weight keeps its meaning —
    # "strongest first" — it just stops being compared across units.
    #
    # Deliberately NOT a fix for weak edges. A single co-occurrence is weight
    # 1.0 and still will not reach a top-N-by-strength overview; that is what
    # the entity-scoped view is for, and surfacing it here would mean surfacing
    # every weight-1 edge — a different view, not a better ranking.
    type_counts = [
        (str(r[0] or ""), int(r[1]))
        for r in conn.execute(
            f"SELECT edge_type, COUNT(*) {where_sql} GROUP BY edge_type",
            tuple(where_params),
        ).fetchall()
    ]
    window = off + lim_e
    budgets = _allocate_edge_budget(type_counts, window)

    if budgets:
        cases = " ".join(
            f"WHEN ? THEN {int(n)}" for _t, n in sorted(budgets.items())
        )
        budget_params = [t for t, _n in sorted(budgets.items())]
        edge_params = list(where_params) + budget_params + [lim_e, off]
        edge_rows = conn.execute(
            f"""
            SELECT edge_id, src_entity_id, dst_entity_id, edge_type, weight, evidence_count,
                   last_event_at, valid_from, valid_to, metadata_json
            FROM (
                SELECT edge_id, src_entity_id, dst_entity_id, edge_type, weight,
                       evidence_count, last_event_at, valid_from, valid_to, metadata_json,
                       ROW_NUMBER() OVER (
                           PARTITION BY edge_type {order_sql}
                       ) AS _rn
                {where_sql}
            )
            WHERE _rn <= CASE edge_type {cases} ELSE 0 END
            {order_sql} LIMIT ? OFFSET ?
            """,
            tuple(edge_params),
        ).fetchall()
    else:
        edge_params = list(where_params) + [lim_e, off]
        edge_rows = conn.execute(
            f"""
            SELECT edge_id, src_entity_id, dst_entity_id, edge_type, weight, evidence_count,
                   last_event_at, valid_from, valid_to, metadata_json
            {where_sql} {order_sql} LIMIT ? OFFSET ?
            """,
            tuple(edge_params),
        ).fetchall()

    def _edge_metadata(evidence, last_at, stored_json) -> str:
        """Stored edge metadata (provenance role_mix, fact statement, mz tag)
        merged with the synthesized evidence fields the UI has always read."""
        try:
            merged = json.loads(stored_json or "{}")
            if not isinstance(merged, dict):
                merged = {}
        except (TypeError, ValueError):
            merged = {}
        merged["evidence_count"] = evidence
        merged["last_event_at"] = last_at
        return json.dumps(merged)

    # Collect node ids INTERLEAVED across edge types, not in global weight order.
    #
    # The cap below is what the caller's `limit_nodes` buys, and a diverse edge
    # page touches far more distinct nodes than a homogeneous one: 203
    # `communicates_with` edges reuse the same handful of contacts, while 300
    # edges spread over 21 relations reach hundreds of entities. Walking the
    # edges in weight order and then truncating would hand the whole node budget
    # back to the heaviest relation — re-introducing, one layer down, exactly the
    # bias the per-type allocation above removes. Measured before this change:
    # `relates_to` recovered 91 slots in the edge query and then collapsed to 6
    # in the response.
    #
    # Round-robin by type keeps the node budget representative, so the edges that
    # survive the cap are a proportional slice rather than the top of one pile.
    by_type: Dict[str, List[tuple]] = {}
    for row in edge_rows:
        by_type.setdefault(str(row[3] or ""), []).append(row)
    node_ids: List[str] = []
    seen_nodes = set()
    lanes = list(by_type.values())
    depth = max((len(v) for v in lanes), default=0)
    for i in range(depth):
        for lane in lanes:
            if i >= len(lane):
                continue
            for node in (lane[i][1], lane[i][2]):
                if node not in seen_nodes:
                    seen_nodes.add(node)
                    node_ids.append(node)
    nodes_before_cap = len(node_ids)
    node_ids = node_ids[:lim_n]
    # Event-time birth per node (earliest mention) so temporal UIs can plot
    # life history rather than derivation history (PLAN_TIMELINE_UNIFIED.md G7).
    first_seen: Dict[str, str] = {}
    if node_ids:
        try:
            for chunk_start in range(0, len(node_ids), 400):
                chunk = node_ids[chunk_start : chunk_start + 400]
                for fs_row in conn.execute(
                    "SELECT entity_id, MIN(event_at) FROM entity_mentions "
                    "WHERE event_at IS NOT NULL AND event_at > '2000-01-01' "
                    f"AND entity_id IN ({','.join('?' * len(chunk))}) GROUP BY entity_id",
                    chunk,
                ):
                    if fs_row[1]:
                        first_seen[str(fs_row[0])] = str(fs_row[1])
        except sqlite3.OperationalError:
            first_seen = {}
    nodes = []
    for entity_id in node_ids:
        row = conn.execute(
            "SELECT canonical_name, entity_type, mention_count, metadata_json, is_self "
            "FROM entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if row:
            # Stored entity metadata (community_id, mz tag) merged with the
            # synthesized mention_count — same passthrough rule as edges.
            try:
                meta = json.loads(row[3] or "{}")
                if not isinstance(meta, dict):
                    meta = {}
            except (TypeError, ValueError):
                meta = {}
            meta["mention_count"] = row[2]
            # Owner marker so graph UIs can pin/label the self node.
            if row[4]:
                meta["is_self"] = True
            nodes.append(
                {
                    "node_id": entity_id,
                    "node_type": row[1],
                    "label": row[0],
                    "first_event_at": first_seen.get(entity_id),
                    "metadata_json": json.dumps(meta),
                }
            )
    node_id_set = set(node_ids)
    edges = [
        {
            # Legacy synthesized id for active rows; closed revisions get the
            # storage edge_id appended so ids stay unique across history.
            "edge_id": (
                f"{src}->{dst}:{etype}" if valid_to is None else f"{src}->{dst}:{etype}:{eid}"
            ),
            "src_node_id": src,
            "dst_node_id": dst,
            "edge_type": etype,
            "weight": round(float(weight or 0.0), 4),
            "last_event_at": last_at,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "metadata_json": _edge_metadata(evidence, last_at, stored_json),
        }
        for eid, src, dst, etype, weight, evidence, last_at, valid_from, valid_to, stored_json in edge_rows
        if src in node_id_set and dst in node_id_set
    ]

    sql_page_len = len(edge_rows)
    truncated_edges = (off + sql_page_len) < total_edges_matching
    truncated_nodes = nodes_before_cap > lim_n
    next_offset = (off + sql_page_len) if truncated_edges else None

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "selection": sel,
            "limit_nodes": lim_n,
            "limit_edges": lim_e,
            "offset": off,
            "total_edges_matching": total_edges_matching,
            "returned_edges": len(edges),
            "returned_nodes": len(nodes),
            "truncated_edges": truncated_edges,
            "truncated_nodes": truncated_nodes,
            "next_offset": next_offset,
        },
    }
