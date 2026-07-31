"""Mention-context centroids for person entities (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §3.1).

The centroid of the embeddings of the records an entity is mentioned in — what
the entity is talked *about*.

Deliberately NOT ``entities.embedding_blob``: that column holds an embedding of
the canonical *name* (``ensure_name_embeddings``), so cosine over it says
"Sarah Chen" ≈ "Sara Chen". That is a dedup signal, already consumed by the
consolidation sweep; affinity built on it would only rediscover aliases.

Scope is ``person`` only (decision D3), enforced here at build time so the cost
is never paid for out-of-scope entities rather than filtered downstream. Same
384-dim ``all-MiniLM-L6-v2`` space as ``signal_embeddings`` — no new model.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..signal.vector_codec import VectorCodecError, decode_vector, encode_f32, normalize_vector

logger = logging.getLogger(__name__)

#: Only persons get centroids (D3).
CONTEXT_VECTOR_ENTITY_TYPE = "person"

#: Fewer contexts than this and the centroid is noise wearing a number.
MIN_CONTEXT_MENTIONS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mean(vectors: Sequence[Sequence[float]]) -> List[float]:
    dims = len(vectors[0])
    totals = [0.0] * dims
    for vector in vectors:
        for i, value in enumerate(vector):
            totals[i] += value
    return [total / len(vectors) for total in totals]


def _context_vectors_for_entity(
    rows: Iterable[Sequence[Any]],
) -> tuple[List[List[float]], Optional[str]]:
    """One vector per distinct mentioned record, plus the model that produced them.

    A record can carry several embedding rows (chunks, dimensions); those are
    averaged into a single context first so a long record does not outvote a
    short one. Vectors whose width disagrees with the first are dropped — a
    mixed-model history must not silently produce a ragged centroid.
    """
    per_record: Dict[str, List[List[float]]] = {}
    model: Optional[str] = None
    width: Optional[int] = None
    for record_id, blob, vector_format, row_model in rows:
        if not blob:
            continue
        try:
            vector = decode_vector(blob, vector_format or "json")
        except VectorCodecError:
            continue
        if not vector:
            continue
        if width is None:
            width = len(vector)
        elif len(vector) != width:
            continue
        if model is None and row_model:
            model = str(row_model)
        per_record.setdefault(str(record_id), []).append(vector)
    return [_mean(vectors) for vectors in per_record.values()], model


def rebuild_entity_context_vectors(
    conn: sqlite3.Connection,
    *,
    min_mentions: int = MIN_CONTEXT_MENTIONS,
    commit: bool = True,
) -> Dict[str, Any]:
    """Recompute every person centroid, replacing the previous set.

    Rebuild-and-replace, not incremental fold: a centroid is a snapshot of the
    current mention set, so a stale row for an entity that has dropped below the
    floor (or stopped being a person) must disappear rather than linger.
    """
    candidates = conn.execute(
        """
        SELECT entity_id FROM entities
        WHERE entity_type = ?
        ORDER BY entity_id
        """,
        (CONTEXT_VECTOR_ENTITY_TYPE,),
    ).fetchall()

    written = 0
    skipped_below_floor = 0
    now = _now()
    rows_to_write: List[tuple] = []
    for (entity_id,) in candidates:
        mention_rows = conn.execute(
            """
            SELECT m.record_id, e.vector_blob, e.vector_format, e.model
            FROM entity_mentions m
            JOIN signal_embeddings e ON e.record_id = m.record_id
            WHERE m.entity_id = ? AND e.vector_blob IS NOT NULL
            """,
            (entity_id,),
        ).fetchall()
        if not mention_rows:
            continue
        contexts, model = _context_vectors_for_entity(mention_rows)
        if len(contexts) < min_mentions:
            skipped_below_floor += 1
            continue
        centroid = normalize_vector(_mean(contexts))
        rows_to_write.append(
            (str(entity_id), encode_f32(centroid), len(contexts), model, now)
        )

    from ...storage.db.write_gate import with_db_write

    with with_db_write():
        conn.execute("DELETE FROM entity_context_vectors")
        if rows_to_write:
            conn.executemany(
                """
                INSERT INTO entity_context_vectors
                    (entity_id, centroid_blob, mention_sample, model_name, computed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows_to_write,
            )
            written = len(rows_to_write)
        if commit:
            conn.commit()

    result = {
        "entities_considered": len(candidates),
        "centroids_written": written,
        "skipped_below_floor": skipped_below_floor,
        "min_mentions": int(min_mentions),
        "computed_at": now,
    }
    logger.info("entity context centroids rebuilt: %s", result)
    return result


def load_context_centroid(
    conn: sqlite3.Connection, entity_id: str
) -> Optional[List[float]]:
    """Decode one stored centroid, or None if the entity has no row."""
    row = conn.execute(
        "SELECT centroid_blob FROM entity_context_vectors WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()
    if row is None or not row[0]:
        return None
    return decode_vector(row[0], "f32")
