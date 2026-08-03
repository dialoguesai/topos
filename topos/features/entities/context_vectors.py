"""Mention-context centroids for person entities (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §3.1).

The centroid of the embeddings of the records an entity is mentioned in — what
the entity is talked *about*.

Deliberately NOT ``entities.embedding_blob``: that column holds an embedding of
the canonical *name* (``ensure_name_embeddings``), so cosine over it says
"Sarah Chen" ≈ "Sara Chen". That is a dedup signal, already consumed by the
consolidation sweep; affinity built on it would only rediscover aliases.

Scope is ``person`` only (decision D3) and ``is_self`` is excluded, both enforced
here at build time so the cost is never paid for out-of-scope entities rather
than filtered downstream. Same 384-dim ``all-MiniLM-L6-v2`` space as
``signal_embeddings`` — no new model.

SOURCE DIVERSITY, NOT MENTION COUNT (§3.1a defect A). Counting mentions was the
original floor and it does not measure what it was asked to measure. One browser
page revisited three times is three distinct ``record_id``s carrying ONE
document; on the live node five people quoted on a single page of epigrams —
Woolf, Shakespeare, Aristotle, Voltaire, Hafiz — each got a centroid built from
that same record set, so their centroids came out byte-identical and every
pairwise cosine was exactly 1.0000. Twenty-six maximally-confident "latent
affinities" out of one page, the precise inverse of the signal this feature
exists to find. Deduping by ``record_id`` does not help: those ARE distinct
records. The unit that matters is the SOURCE DOCUMENT, so the centroid averages
one vector per source — a re-read cannot outvote a genuinely separate context —
and the floor counts sources.

``is_self`` exclusion (§3.1a defect B) mirrors ``ensure_name_embeddings`` in
``consolidation.py``, which has always excluded the owner from the
name-embedding path. Affinity is about relationships among *other* people; an
edge between the owner and their own handle is noise twice over.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..signal.vector_codec import VectorCodecError, decode_vector, encode_f32, normalize_vector

logger = logging.getLogger(__name__)

#: Only persons get centroids (D3).
CONTEXT_VECTOR_ENTITY_TYPE = "person"

#: PRIMARY gate (§3.1a): distinct source DOCUMENTS a centroid must span. Three
#: readings of one page describe one context, however many records they leave.
MIN_CONTEXT_SOURCES = 3

#: SECONDARY gate, kept at its shipped value: distinct mentioned records. Source
#: diversity is the real guard, but an entity whose sources are all one-line
#: records is standing on very little text and does not need a centroid.
MIN_CONTEXT_MENTIONS = 5

#: Two centroids closer than this are the same point (§3.1a): a clique artefact
#: from a shared record set, not two entities that independently resemble each
#: other. Both are dropped — the pair carries no information either way.
CENTROID_DEGENERACY_EPSILON = 1e-6

#: ``browser:<url>_<ISO timestamp>`` — the fallback source key for browser
#: records when ``content_hash`` is absent, collapsing a revisit to its URL.
_TRAILING_TIMESTAMP = re.compile(r"_\d{4}-\d{2}-\d{2}T[0-9:.+\-]+Z?$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_key(
    record_id: str, content_hash: Optional[str], conversation_id: Optional[str]
) -> str:
    """The source DOCUMENT a record belongs to.

    ``content_hash`` is the natural key and the one that discriminates on real
    data: it is per-record (never split across a record's chunks) and identical
    across re-reads, so the three epigram-page visits collapse to one source
    under it. The fallbacks exist only for rows predating the column — the URL
    portion of a browser ``record_id`` with its visit timestamp dropped, then
    the conversation, then the record itself, which degrades to the old
    per-record behaviour rather than to no gate at all.
    """
    if content_hash:
        return f"hash:{content_hash}"
    if record_id.startswith("browser:"):
        return f"url:{_TRAILING_TIMESTAMP.sub('', record_id)}"
    if conversation_id:
        return f"conversation:{conversation_id}"
    return f"record:{record_id}"


def _mean(vectors: Sequence[Sequence[float]]) -> List[float]:
    dims = len(vectors[0])
    totals = [0.0] * dims
    for vector in vectors:
        for i, value in enumerate(vector):
            totals[i] += value
    return [total / len(vectors) for total in totals]


def _context_vectors_for_entity(
    rows: Iterable[Sequence[Any]],
) -> Tuple[List[List[float]], int, Optional[str]]:
    """One vector per distinct source DOCUMENT, the record count, and the model.

    Two reductions, in this order:

      * embedding rows -> record. A record can carry several rows (chunks,
        dimensions); averaging them first stops a long record outvoting a short
        one on chunk count alone.
      * records -> source document. This is the §3.1a fix: a page read three
        times contributes one vector, not three, so the centroid is the mean of
        the entity's *contexts* rather than of its *reads*.

    Vectors whose width disagrees with the first are dropped — a mixed-model
    history must not silently produce a ragged centroid.
    """
    per_record: Dict[str, List[List[float]]] = {}
    record_source: Dict[str, str] = {}
    model: Optional[str] = None
    width: Optional[int] = None
    for record_id, blob, vector_format, row_model, content_hash, conversation_id in rows:
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
        key = str(record_id)
        per_record.setdefault(key, []).append(vector)
        record_source.setdefault(
            key,
            _source_key(
                key,
                str(content_hash) if content_hash else None,
                str(conversation_id) if conversation_id else None,
            ),
        )

    per_source: Dict[str, List[List[float]]] = {}
    for key, vectors in per_record.items():
        per_source.setdefault(record_source[key], []).append(_mean(vectors))
    return (
        [_mean(vectors) for vectors in per_source.values()],
        len(per_record),
        model,
    )


def _degenerate_entities(
    centroids: Dict[str, Sequence[float]], epsilon: float
) -> set:
    """Entity ids whose centroid coincides with another entity's (§3.1a).

    Quantising to the epsilon grid finds these in one pass instead of n²
    comparisons, which matters because the clique this catches is the one that
    makes n large. Quantisation is sufficient because a genuine clique artefact
    is *bit*-identical — the same source vectors reduced by the same arithmetic
    — and epsilon is only absorbing float drift, not measuring similarity.

    Every member of a coincident group is dropped, not all-but-one: there is no
    basis for picking a survivor, and a lone survivor would still be a centroid
    that describes a page rather than a person.
    """
    buckets: Dict[tuple, List[str]] = {}
    for entity_id, centroid in centroids.items():
        key = tuple(round(float(value) / epsilon) for value in centroid)
        buckets.setdefault(key, []).append(entity_id)
    return {
        entity_id
        for group in buckets.values()
        if len(group) > 1
        for entity_id in group
    }


def rebuild_entity_context_vectors(
    conn: sqlite3.Connection,
    *,
    min_sources: int = MIN_CONTEXT_SOURCES,
    min_mentions: int = MIN_CONTEXT_MENTIONS,
    epsilon: float = CENTROID_DEGENERACY_EPSILON,
    commit: bool = True,
) -> Dict[str, Any]:
    """Recompute every person centroid, replacing the previous set.

    Rebuild-and-replace, not incremental fold: a centroid is a snapshot of the
    current mention set, so a stale row for an entity that has dropped below a
    floor (or stopped being a person) must disappear rather than linger.

    Three gates, in order — source diversity, then record count, then
    degeneracy. The first two are per-entity and the third is only decidable
    once every centroid exists, which is why it runs after the loop rather than
    inside it.
    """
    candidates = conn.execute(
        """
        SELECT entity_id FROM entities
        WHERE entity_type = ? AND COALESCE(is_self, 0) = 0
        ORDER BY entity_id
        """,
        (CONTEXT_VECTOR_ENTITY_TYPE,),
    ).fetchall()

    skipped_below_source_floor = 0
    skipped_below_mention_floor = 0
    now = _now()
    built: Dict[str, tuple] = {}
    for (entity_id,) in candidates:
        mention_rows = conn.execute(
            """
            SELECT m.record_id, e.vector_blob, e.vector_format, e.model,
                   e.content_hash, e.conversation_id
            FROM entity_mentions m
            JOIN signal_embeddings e ON e.record_id = m.record_id
            WHERE m.entity_id = ? AND e.vector_blob IS NOT NULL
            """,
            (entity_id,),
        ).fetchall()
        if not mention_rows:
            continue
        contexts, mention_sample, model = _context_vectors_for_entity(mention_rows)
        if len(contexts) < min_sources:
            skipped_below_source_floor += 1
            continue
        if mention_sample < min_mentions:
            skipped_below_mention_floor += 1
            continue
        built[str(entity_id)] = (
            normalize_vector(_mean(contexts)),
            len(contexts),
            mention_sample,
            model,
        )

    degenerate = _degenerate_entities(
        {entity_id: row[0] for entity_id, row in built.items()}, epsilon
    )
    if degenerate:
        logger.warning(
            "dropped %d degenerate context centroids (within %g of another "
            "entity's): %s",
            len(degenerate),
            epsilon,
            sorted(degenerate),
        )

    rows_to_write = [
        (entity_id, encode_f32(centroid), mention_sample, sources, model, now)
        for entity_id, (centroid, sources, mention_sample, model) in sorted(built.items())
        if entity_id not in degenerate
    ]

    from ...storage.db.write_gate import with_db_write

    with with_db_write():
        conn.execute("DELETE FROM entity_context_vectors")
        if rows_to_write:
            conn.executemany(
                """
                INSERT INTO entity_context_vectors
                    (entity_id, centroid_blob, mention_sample, source_sample,
                     model_name, computed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows_to_write,
            )
        if commit:
            conn.commit()

    result = {
        "entities_considered": len(candidates),
        "centroids_written": len(rows_to_write),
        "skipped_below_floor": skipped_below_source_floor + skipped_below_mention_floor,
        "skipped_below_source_floor": skipped_below_source_floor,
        "skipped_below_mention_floor": skipped_below_mention_floor,
        "dropped_degenerate": len(degenerate),
        "min_sources": int(min_sources),
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
