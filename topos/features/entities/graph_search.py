"""Vector-search-driven graph filtering (PLAN_GRAPH_VECTOR_SEARCH).

Meaning-level query → ranked graph entities, so the graph UI can highlight or
filter to what matters. Two evidence channels, combined per entity:

  * semantic records — the caller's ``search_fn`` (in production
    SignalService.search_vectors: hybrid ANN+FTS over signal_embeddings, with
    event-window params so the graph's time window rides along) returns scored
    records; each record's entities (via entity_mentions) inherit its score,
    summed across records so frequency and strength both count;
  * label match — materialized nodes (goals / topic hubs / conversations)
    aren't mentioned IN records, they're derived FROM them, so they match by
    token overlap against the query with a modest fixed bonus per token.

No raw vectors are exposed; snippets are the disclosure-filtered text_preview
column stamped at embed time.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List, Optional

# Modest per-token bonus for label matches on materialized nodes; capped so a
# wordy label can't outrank strong record evidence.
_LABEL_TOKEN_SCORE = 0.3
_LABEL_SCORE_CAP = 0.6
_EVIDENCE_PER_ENTITY = 3
# Node kinds that exist as derived labels rather than record mentions.
_LABEL_MATCH_TYPES = ("goal", "topic", "conversation")

SearchFn = Callable[..., Dict[str, Any]]


def _item_score(item: Dict[str, Any]) -> float:
    for key in ("hybrid_score", "similarity"):
        value = item.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


# Function words that would make label matching promiscuous ("and" matched
# every "X and Y" label). Content words only.
_STOPWORDS = frozenset({
    "and", "the", "for", "with", "from", "about", "into", "over", "that",
    "this", "these", "those", "what", "when", "where", "how", "who", "why",
    "are", "was", "were", "will", "would", "could", "should", "have", "has",
    "had", "not", "but", "all", "any", "our", "your", "their", "its",
})


def _query_tokens(query: str) -> List[str]:
    from .resolver import normalize_name

    return [
        t for t in normalize_name(query).split()
        if len(t) > 2 and t not in _STOPWORDS
    ]


def graph_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    search_fn: SearchFn,
    limit_records: int = 40,
    limit_entities: int = 30,
    event_after: Optional[str] = None,
    event_before: Optional[str] = None,
) -> Dict[str, Any]:
    """Rank graph entities by semantic evidence for ``query``."""
    q = str(query or "").strip()
    limit_records = max(1, min(int(limit_records), 100))
    limit_entities = max(1, min(int(limit_entities), 100))
    if not q:
        return {"query": "", "entities": [], "records_considered": 0}

    result = search_fn(
        query=q, limit=limit_records, event_after=event_after, event_before=event_before
    )
    items = [i for i in (result.get("items") or []) if isinstance(i, dict)]

    # record_id -> (score, item)
    scored_records: Dict[str, Dict[str, Any]] = {}
    for item in items:
        record_id = str(item.get("record_id") or "").strip()
        if not record_id:
            continue
        score = _item_score(item)
        prev = scored_records.get(record_id)
        if prev is None or score > prev["score"]:
            scored_records[record_id] = {"score": score, "item": item}

    # Normalize record scores so the top record = 1.0 — hybrid RRF scores live
    # on a tiny scale (~1/60) that the fixed label bonus would otherwise dwarf.
    max_score = max((r["score"] for r in scored_records.values()), default=0.0)
    if max_score > 0:
        for rec in scored_records.values():
            rec["score"] = rec["score"] / max_score

    entity_scores: Dict[str, float] = {}
    entity_evidence: Dict[str, List[Dict[str, Any]]] = {}

    if scored_records:
        record_ids = list(scored_records.keys())
        placeholders = ",".join("?" for _ in record_ids)
        rows = conn.execute(
            f"""
            SELECT DISTINCT entity_id, record_id FROM entity_mentions
            WHERE record_id IN ({placeholders})
            """,
            record_ids,
        ).fetchall()
        for entity_id, record_id in rows:
            rec = scored_records[str(record_id)]
            item = rec["item"]
            eid = str(entity_id)
            entity_scores[eid] = entity_scores.get(eid, 0.0) + rec["score"]
            entity_evidence.setdefault(eid, []).append(
                {
                    "kind": "record",
                    "record_id": str(record_id),
                    "snippet": str(item.get("text_preview") or "")[:200],
                    "source_id": item.get("source_id"),
                    "event_at": item.get("event_at"),
                    # Display the raw cosine when the item has one; the
                    # normalized contribution is what feeds the entity score.
                    "similarity": float(item.get("similarity") or rec["score"]),
                }
            )

    # Label matching for materialized nodes (their meaning lives in the label).
    tokens = _query_tokens(q)
    if tokens:
        type_ph = ",".join("?" for _ in _LABEL_MATCH_TYPES)
        for entity_id, name, normalized in conn.execute(
            f"SELECT entity_id, canonical_name, normalized_name FROM entities "
            f"WHERE entity_type IN ({type_ph})",
            _LABEL_MATCH_TYPES,
        ).fetchall():
            words = set(str(normalized or "").split())
            matched = [t for t in tokens if t in words]
            if not matched:
                continue
            bonus = min(_LABEL_TOKEN_SCORE * len(matched), _LABEL_SCORE_CAP)
            eid = str(entity_id)
            entity_scores[eid] = entity_scores.get(eid, 0.0) + bonus
            entity_evidence.setdefault(eid, []).append(
                {"kind": "label", "snippet": str(name or ""), "similarity": bonus}
            )

    # Hydrate labels/types and shape the response.
    entities: List[Dict[str, Any]] = []
    for eid, score in sorted(entity_scores.items(), key=lambda kv: kv[1], reverse=True)[:limit_entities]:
        row = conn.execute(
            "SELECT canonical_name, entity_type FROM entities WHERE entity_id=?", (eid,)
        ).fetchone()
        evidence = sorted(
            entity_evidence.get(eid, []),
            key=lambda ev: float(ev.get("similarity") or 0.0),
            reverse=True,
        )[:_EVIDENCE_PER_ENTITY]
        entities.append(
            {
                "entity_id": eid,
                "label": row[0] if row else eid,
                "entity_type": row[1] if row else None,
                "score": round(score, 4),
                "evidence": evidence,
            }
        )

    return {
        "query": q,
        "entities": entities,
        "records_considered": len(scored_records),
        **({"model": result.get("model")} if result.get("model") else {}),
    }
