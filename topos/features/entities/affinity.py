"""Latent semantic-affinity edges (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §3.2-§3.4a).

What this is for: surfacing pairs that co-mention structurally *cannot* see. A
``co_occurrence`` edge requires the two entities to appear together; an affinity
edge is interesting precisely when they never do — a former manager and a
current one occupy the same role-shape in the owner's life and are never in the
same message. That is why §3.4's co-occurrence suppression is not a tidiness
rule but the point: if a pair co-occurs, co-mention already found them and an
affinity edge would only double-count them.

Built over ``entity_context_vectors`` (mention-context centroids), NOT over
``entities.embedding_blob``. That column embeds the canonical *name*, so cosine
over it says "Sarah Chen" ≈ "Sara Chen" — a dedup signal already consumed by
the consolidation sweep. Affinity built on it would emit alias suggestions
wearing an affinity label; ``tests/features/test_entity_affinity.py`` holds that
door shut.

WEIGHT SCALE — read before comparing these weights to anything else.
``update_edge`` folds evidence over time: it decays the stored weight by
``0.5 ** (Δdays / half_life)`` and increments ``evidence_count``, so its weight
is an UNBOUNDED ACCUMULATING quantity whose magnitude means "how much evidence,
how recently". An affinity edge is a RECOMPUTED SNAPSHOT: its weight is a
cosine in ``[0, 1]`` and means "how alike", full stop. A 0.8 affinity edge and a
0.8 co-occurrence edge have nothing to do with one another, and ranking them in
one list is meaningless. Nothing in this module reconciles the two scales — what
keeps them from being compared is the §3.5 allow-list in
``features/complexity/projection.py``, which excludes ``semantic_affinity`` from
the influence graph entirely (decision D1). If that allow-list is ever widened
to admit affinity edges, the scales have to be reconciled first.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..signal.vector_codec import VectorCodecError, decode_vector, normalize_vector
from .edges import EDGE_CO_OCCURRENCE, EDGE_SEMANTIC_AFFINITY, _canonical_order, supersede_edge

logger = logging.getLogger(__name__)

#: Bumped whenever the scoring or capping rules change, so a stored edge can be
#: told apart from one written under different rules. Rides in metadata_json.
#: 2 — §3.1a: centroids now gate on source diversity and reject degeneracy,
#: merge candidates are suppressed, and evidence_count counts source documents
#: rather than mention records. A v1 edge is not comparable to a v2 one.
AFFINITY_SPEC_VERSION = 2

#: Top-N neighbours per entity, MUTUAL only (§3.4). Rank-based, therefore
#: scale-free: it means the same thing on a node with tight centroids as on one
#: with loose ones, which is why — unlike the cosine floor — it needs no
#: calibration. Mutuality halves the count and kills hub artefacts, where one
#: over-mentioned entity would otherwise appear in everybody's neighbourhood.
AFFINITY_TOPN = 8

#: Absolute backstop (§3.4a). A percentile ALWAYS admits something — even on a
#: node where nothing is genuinely similar, the top 0.5% of a flat distribution
#: is still 0.5%. This constant is what makes "this node has no affinity edges"
#: an expressible outcome rather than an unreachable one.
AFFINITY_FLOOR_ABS = 0.35

#: Default percentile of the node's OWN pair-cosine distribution used as the
#: floor. Stored per-node in engine_config, because the right *cosine* differs
#: per node (data volume, number of distinct life contexts) and drifts on a
#: single node over time, while the right *percentile* is portable. Shipping a
#: fixed AFFINITY_MIN_COSINE would be wrong everywhere except the node it was
#: tuned on, and eventually wrong there too.
AFFINITY_DEFAULT_PERCENTILE = 99.5

ENGINE_CONFIG_KEY_AFFINITY_PERCENTILE = "affinity_floor_percentile"

#: Affinity edges may be at most this fraction of all OTHER active edges (§3.4).
#: 3.2k entities against 3k existing edges: an uncapped mutual-top-8 would make
#: affinity ~90% of the graph and drown the precise edges in the plausible ones.
AFFINITY_CEILING_RATIO = 0.5

#: Anchor entities sampled to estimate the percentile on large nodes (§3.4a).
#: Each anchor contributes its full neighbourhood of cosines, so the estimate
#: rests on K*(n-1) samples without ever materialising all n*(n-1)/2 pairs.
AFFINITY_SAMPLE_ANCHORS = 200

#: Fixed so the estimated floor is reproducible across reruns of one dataset —
#: a threshold that jitters between rebuilds would churn the edge set and make
#: the drift series in affinity_recompute_log unreadable.
_SAMPLE_SEED = 20260731


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _percentile(sorted_values: Sequence[float], percentile: float) -> float:
    """Linear-interpolated percentile over an ascending sequence."""
    if not sorted_values:
        return 0.0
    p = max(0.0, min(100.0, float(percentile)))
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (p / 100.0) * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    frac = position - low
    return float(sorted_values[low]) + frac * (
        float(sorted_values[high]) - float(sorted_values[low])
    )


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine of two already-L2-normalised vectors, clamped to [0, 1].

    Negative cosines are clamped rather than kept: the weight column is
    documented as ``[0, 1]``, and a pair pointing away from each other is not
    "negatively affine", it is simply not affine.
    """
    dot = sum(x * y for x, y in zip(a, b))
    return max(0.0, min(1.0, dot))


def _load_centroids(conn: sqlite3.Connection) -> List[Tuple[str, List[float], int]]:
    """(entity_id, unit vector, source_sample), ordered for determinism.

    ``source_sample``, not ``mention_sample``, because that number becomes the
    edge's ``evidence_count`` and §3.1a is exactly the finding that a mention
    count is not evidence: thirty-eight records of one re-read page is one
    document's worth of context reported as thirty-eight. Distinct source
    documents is the count an owner reading ``evidence_count`` would mean.

    Rows whose width disagrees with the majority are dropped: a mixed-model
    history must not produce cosines between vectors from different spaces.
    """
    rows = conn.execute(
        """
        SELECT entity_id, centroid_blob, source_sample
        FROM entity_context_vectors
        WHERE centroid_blob IS NOT NULL
        ORDER BY entity_id
        """
    ).fetchall()
    decoded: List[Tuple[str, List[float], int]] = []
    widths: Dict[int, int] = {}
    for entity_id, blob, mention_sample in rows:
        try:
            vector = decode_vector(blob, "f32")
        except VectorCodecError:
            continue
        if not vector:
            continue
        widths[len(vector)] = widths.get(len(vector), 0) + 1
        decoded.append((str(entity_id), normalize_vector(vector), int(mention_sample or 0)))
    if not decoded:
        return []
    dominant = max(widths.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    return [row for row in decoded if len(row[1]) == dominant]


def _resolve_percentile(conn: sqlite3.Connection, override: Optional[float]) -> float:
    if override is not None:
        return max(0.0, min(100.0, float(override)))
    try:
        from ...core.state import get_engine_config_value

        raw = get_engine_config_value(conn, ENGINE_CONFIG_KEY_AFFINITY_PERCENTILE)
    except Exception:  # noqa: BLE001 — config lookup must never fail a rebuild
        raw = None
    if raw is None:
        return AFFINITY_DEFAULT_PERCENTILE
    try:
        return max(0.0, min(100.0, float(raw)))
    except (TypeError, ValueError):
        logger.warning(
            "engine_config[%s]=%r is not a number; falling back to P=%s",
            ENGINE_CONFIG_KEY_AFFINITY_PERCENTILE,
            raw,
            AFFINITY_DEFAULT_PERCENTILE,
        )
        return AFFINITY_DEFAULT_PERCENTILE


def _estimate_floor(
    centroids: Sequence[Tuple[str, List[float], int]],
    percentile: float,
    *,
    max_anchors: int = AFFINITY_SAMPLE_ANCHORS,
) -> float:
    """The cosine this node's own distribution puts the Pth percentile at.

    On a node small enough that every entity can be an anchor this is the exact
    percentile of the pair distribution (each pair is sampled twice, which is
    uniform and so leaves the percentile unchanged). Above ``max_anchors`` it is
    an estimate from K sampled neighbourhoods — the plan's explicit instruction
    not to materialise all pairs on large nodes.
    """
    if len(centroids) < 2:
        return 0.0
    if len(centroids) <= max_anchors:
        anchors = list(range(len(centroids)))
    else:
        anchors = random.Random(_SAMPLE_SEED).sample(range(len(centroids)), max_anchors)
    samples: List[float] = []
    for i in anchors:
        _, vector, _ = centroids[i]
        for j, (_, other, _) in enumerate(centroids):
            if i == j:
                continue
            samples.append(_cosine(vector, other))
    samples.sort()
    return _percentile(samples, percentile)


def _mutual_top_pairs(
    centroids: Sequence[Tuple[str, List[float], int]],
    *,
    topn: int,
    floor: float,
) -> List[Tuple[str, str, float, int]]:
    """Pairs where EACH endpoint ranks the other in its own top ``topn``.

    Top-N is taken on rank alone and the floor applied afterwards; because the
    floor does not reorder anything, that is the same set as filtering first,
    and it keeps the two caps independent — a pair can be rejected for being
    outranked or for being too far, and the log can tell which.
    """
    neighbours: Dict[str, set] = {}
    scores: Dict[Tuple[str, str], float] = {}
    for i, (entity_id, vector, _sample) in enumerate(centroids):
        ranked: List[Tuple[float, str]] = []
        for j, (other_id, other_vector, _s) in enumerate(centroids):
            if i == j:
                continue
            cosine = _cosine(vector, other_vector)
            # -other_id so ties break on id ascending, not on scan order.
            ranked.append((cosine, other_id))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        keep = set()
        for cosine, other_id in ranked[:topn]:
            if cosine < floor:
                continue
            keep.add(other_id)
            key = (entity_id, other_id) if entity_id < other_id else (other_id, entity_id)
            scores[key] = cosine
        neighbours[entity_id] = keep

    samples = {entity_id: sample for entity_id, _v, sample in centroids}
    pairs: List[Tuple[str, str, float, int]] = []
    for (a, b), cosine in scores.items():
        if b not in neighbours.get(a, ()) or a not in neighbours.get(b, ()):
            continue
        # evidence_count = the LESSER endpoint's source_sample: an edge is only
        # standing on as much context as its thinner side supplies.
        pairs.append((a, b, cosine, min(samples.get(a, 0), samples.get(b, 0))))
    pairs.sort(key=lambda item: (-item[2], item[0], item[1]))
    return pairs


def _active_co_occurrence_pairs(conn: sqlite3.Connection) -> set:
    rows = conn.execute(
        """
        SELECT src_entity_id, dst_entity_id FROM entity_edges
        WHERE edge_type = ? AND valid_to IS NULL
        """,
        (EDGE_CO_OCCURRENCE,),
    ).fetchall()
    pairs = set()
    for src, dst in rows:
        a, b = str(src), str(dst)
        pairs.add((a, b) if a < b else (b, a))
    return pairs


def _merge_candidate_pairs(conn: sqlite3.Connection) -> set:
    """Pairs consolidation would call one person (§3.1a defect B).

    On the live node BOTH surviving pairs at the shipped floor were this:
    ``Jonny ↔ Jonny Johnson`` and ``Jonny Johnson ↔ draftin1`` — the owner and
    their own unconsolidated aliases, scoring high precisely because two names
    for one person are mentioned in one person's contexts. That is a
    consolidation finding wearing an affinity label.

    Delegated to ``consolidation.merge_candidate_pairs`` rather than answered
    with a name check here: the sweep's rules are the definition of "same
    person" on this spine, and a second, subtly different definition living in
    this module is how the two drift apart. Never fatal — a rebuild that cannot
    consult consolidation still writes edges, it just suppresses less.
    """
    try:
        from .consolidation import merge_candidate_pairs

        return merge_candidate_pairs(conn)
    except Exception as exc:  # noqa: BLE001 — suppression must not fail a rebuild
        logger.warning("merge-candidate suppression unavailable: %s", exc)
        return set()


def _other_active_edge_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM entity_edges
            WHERE valid_to IS NULL AND edge_type != ?
            """,
            (EDGE_SEMANTIC_AFFINITY,),
        ).fetchone()[0]
    )


def rebuild_affinity_edges(
    conn: sqlite3.Connection,
    *,
    topn: int = AFFINITY_TOPN,
    percentile: Optional[float] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Recompute the whole ``semantic_affinity`` edge set, replacing the old one.

    Deliberately NOT ``update_edge`` (see the module docstring): these edges are
    a snapshot, so the previous set is CLOSED — ``valid_to`` stamped via
    ``supersede_edge``, rows kept as history — and a fresh set inserted, both in
    one transaction so no reader ever sees a half-rebuilt graph. Idempotent:
    running it twice on unchanged inputs yields the same active set, plus one
    superseded revision per edge.
    """
    centroids = _load_centroids(conn)
    resolved_percentile = _resolve_percentile(conn, percentile)
    resolved_cosine = _estimate_floor(centroids, resolved_percentile)
    # The backstop is a MAX, not a fallback: the percentile is allowed to be
    # stricter than 0.35 on a node with real spread, but never looser.
    floor = max(resolved_cosine, AFFINITY_FLOOR_ABS)

    pairs_considered = len(centroids) * (len(centroids) - 1) // 2
    candidates = _mutual_top_pairs(centroids, topn=max(1, int(topn)), floor=floor)

    co_occurring = _active_co_occurrence_pairs(conn)
    suppressed = [pair for pair in candidates if (pair[0], pair[1]) in co_occurring]
    candidates = [pair for pair in candidates if (pair[0], pair[1]) not in co_occurring]

    merge_candidates = _merge_candidate_pairs(conn)
    aliased = [pair for pair in candidates if (pair[0], pair[1]) in merge_candidates]
    candidates = [pair for pair in candidates if (pair[0], pair[1]) not in merge_candidates]

    other_active = _other_active_edge_count(conn)
    ceiling = int(other_active * AFFINITY_CEILING_RATIO)
    ceiling_hit = len(candidates) > ceiling
    if ceiling_hit:
        # Truncate the weakest, loudly. Writing past the ceiling would let
        # affinity edges dominate the graph, which is the failure the cap names.
        logger.warning(
            "affinity ceiling hit: %d candidate pairs truncated to %d "
            "(<= %.2f x %d other active edges)",
            len(candidates),
            ceiling,
            AFFINITY_CEILING_RATIO,
            other_active,
        )
        candidates = candidates[:ceiling]

    now = _now_iso()
    from ...storage.db.write_gate import with_db_write

    with with_db_write():
        existing = conn.execute(
            """
            SELECT src_entity_id, dst_entity_id FROM entity_edges
            WHERE edge_type = ? AND valid_to IS NULL
            """,
            (EDGE_SEMANTIC_AFFINITY,),
        ).fetchall()
        for src, dst in existing:
            supersede_edge(
                conn,
                src_entity_id=str(src),
                dst_entity_id=str(dst),
                edge_type=EDGE_SEMANTIC_AFFINITY,
                valid_to=now,
            )

        rows = []
        for a, b, cosine, evidence in candidates:
            src, dst = _canonical_order(a, b, EDGE_SEMANTIC_AFFINITY)
            rows.append(
                (
                    f"edg_{uuid.uuid4().hex[:16]}",
                    src,
                    dst,
                    EDGE_SEMANTIC_AFFINITY,
                    float(cosine),
                    int(evidence),
                    now,
                    json.dumps(
                        {
                            "kind": "affinity",
                            "cosine": round(float(cosine), 6),
                            "spec_version": AFFINITY_SPEC_VERSION,
                        }
                    ),
                )
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO entity_edges (
                    edge_id, src_entity_id, dst_entity_id, edge_type,
                    weight, evidence_count, valid_from, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        conn.execute(
            """
            INSERT INTO affinity_recompute_log (
                entities_considered, pairs_considered, edges_written,
                ceiling, ceiling_hit, percentile, resolved_cosine,
                floor_cosine, spec_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                len(centroids),
                pairs_considered,
                len(rows),
                ceiling,
                1 if ceiling_hit else 0,
                resolved_percentile,
                resolved_cosine,
                floor,
                AFFINITY_SPEC_VERSION,
            ),
        )
        # Persist the whole-recipe hash so a later boot can ask "has the derived
        # layer recipe moved?" without re-deriving the knobs (PLAN M4). The
        # integer AFFINITY_SPEC_VERSION above stays the local affinity-rules
        # stamp; the hash covers affinity + centroids + embedding + retrieval.
        from ..signal.derived_spec import persist_derived_spec_version

        derived_hash = persist_derived_spec_version(conn)
        if commit:
            conn.commit()

    result = {
        "entities_considered": len(centroids),
        "pairs_considered": pairs_considered,
        "edges_superseded": len(existing),
        "edges_written": len(rows),
        "suppressed_co_occurring": len(suppressed),
        "suppressed_merge_candidates": len(aliased),
        "other_active_edges": other_active,
        "ceiling": ceiling,
        "ceiling_hit": ceiling_hit,
        "percentile": resolved_percentile,
        "resolved_cosine": resolved_cosine,
        "floor_cosine": floor,
        "spec_version": AFFINITY_SPEC_VERSION,
        "derived_spec_version": derived_hash,
        "computed_at": now,
    }
    logger.info("semantic affinity edges rebuilt: %s", result)
    return result
