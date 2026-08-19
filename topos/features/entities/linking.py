"""Query-time entity linking: match query text against the entity registry.

Deterministic (alias table + token containment) — no model call, so it can
run on every query. Returns entities ordered by match quality then mention
count.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List

from .resolver import normalize_name

_MAX_LINKED = 5

# Function/framing words that NER junk entities are named after ('IS', 'Go',
# 'and', 'of', 'At', 'Am', 'In', 'The One'). An entity whose name is one of
# these matches virtually every query ("which gym does the owner GO to" linked
# entity 'Go' at 1.0) and floods the top-5 with dossier/mention filler.
_LINK_STOPWORDS = frozenset(
    {
        "a", "an", "and", "or", "of", "at", "am", "is", "are", "was", "were",
        "be", "been", "in", "on", "to", "the", "it", "its", "as", "by", "for",
        "with", "from", "this", "that", "these", "those", "he", "she", "they",
        "we", "you", "i", "me", "my", "do", "did", "does", "go", "went", "get",
        "got", "has", "have", "had", "not", "no", "yes", "so", "if", "but",
        "one", "all", "any", "can", "will", "just", "now", "new", "old", "out",
        "up", "down", "here", "there", "place", "thing", "things", "some",
        "more", "most", "other", "into", "about", "over", "under", "our",
        # time words — NER mints entities like 'Time', 'Long Time', 'Tomorrow'
        "time", "times", "long", "day", "days", "week", "weeks", "month",
        "months", "year", "years", "today", "tomorrow", "yesterday", "night",
        "morning", "evening", "hour", "hours",
        # speech/description words — junk entities 'Bio', 'Public Cha',
        # 'Mean What We Say' linked on query framing ("what does my public
        # bio say about…")
        "what", "say", "says", "said", "mean", "means", "public", "bio",
    }
)


def _linkable_candidate(cand: str) -> bool:
    """A candidate name is linkable only if it carries at least one real word:
    ≥3 chars and not a function word. 'Luc' links; 'IS', 'Go', 'The One' do not."""
    words = [w for w in cand.split() if w]
    if not words:
        return False
    return any(len(w) >= 3 and w not in _LINK_STOPWORDS for w in words)


def link_query_entities(conn: sqlite3.Connection, query_text: str) -> List[Dict[str, Any]]:
    query_norm = normalize_name(query_text)
    if not query_norm:
        return []
    # Single-character tokens ("a", "i") must never count toward a link —
    # "…for a contact" matching half of "VoxTerm A" is how ghosts get linked.
    query_tokens = {t for t in query_norm.split() if len(t) > 1}
    if not query_tokens:
        return []

    try:
        rows = conn.execute(
            """
            SELECT entity_id, entity_type, canonical_name, normalized_name,
                   aliases_json, mention_count
            FROM entities WHERE mention_count > 0 OR contact_id IS NOT NULL
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    scored: List[Dict[str, Any]] = []
    padded_query = f" {query_norm} "
    for entity_id, etype, name, normalized, aliases_json, mention_count in rows:
        candidates = [str(normalized or "")]
        try:
            candidates += [normalize_name(a) for a in json.loads(aliases_json or "[]")]
        except json.JSONDecodeError:
            pass
        best = 0.0
        for cand in candidates:
            if not cand or not _linkable_candidate(cand):
                continue
            if f" {cand} " in padded_query:
                best = max(best, 1.0)  # full name appears in query
                continue
            cand_tokens = {
                t for t in cand.split() if len(t) >= 3 and t not in _LINK_STOPWORDS
            }
            if not cand_tokens:
                continue
            overlap = query_tokens & cand_tokens
            if not overlap:
                continue
            # Single-token surnames/short names must match a full token
            best = max(best, len(overlap) / len(cand_tokens))
        if best >= 0.5:
            scored.append(
                {
                    "entity_id": str(entity_id),
                    "entity_type": str(etype),
                    "canonical_name": str(name),
                    "match_score": round(best, 3),
                    "mention_count": int(mention_count or 0),
                }
            )
    scored.sort(key=lambda e: (e["match_score"], e["mention_count"]), reverse=True)
    return scored[:_MAX_LINKED]


def _closed_edge_label(edge: Dict[str, Any]) -> str:
    """Render an ended edge with the staleness marker (T7 contract).

    The literal "no longer current" is load-bearing — the T7 oracle greps it —
    and the form mirrors the fact lane's marker
    ("… (no longer current — superseded YYYY-MM-DD)")."""
    return (
        f"{edge['entity_name']} "
        f"(no longer current — superseded {str(edge.get('valid_to') or '')[:10]})"
    )


def entity_context_items(
    conn: sqlite3.Connection,
    linked: List[Dict[str, Any]],
    *,
    manifest: Any,
    max_per_entity: int = 4,
    temporal_shift: str | None = None,
) -> List[Dict[str, Any]]:
    """Ordered summary items for linked entities: dossier line, then recent mentions.

    temporal_shift='past' (planner belief-revision signal) widens the edge
    read to CLOSED revisions: ended relationships render with the
    "no longer current" marker instead of being invisible (B2.2/T7).

    ``manifest`` is REQUIRED, and it bounds the mention lane. A mention row is a
    POINTER — it names a canonical table and a record id without ever reading
    the record — and this lane emitted one for whatever table the graph happened
    to know about, whatever the grant authorized. Measured: ``availability:read``
    declares ``canonical_tables=[]`` and still received a row reading
    ``"Anthropic in conversation_messages"`` carrying that record's id. A pointer
    is a disclosure: it says the record exists, which table it lives in, and when
    it happened.

    So the mention lane scans the manifest's tables and no others — the same
    bound ``_load_entity_thread_items`` applies one join further on — and the
    pointer it emits now carries the keys the other planes match on:
    ``canonical_table`` (what an exclusion category tests) and
    ``object_type``/``disclosure`` (what the tier filter tests, and which mention
    items previously declared neither of, so they crossed the tier untouched).

    A mention whose ``canonical_table`` is NULL is dropped rather than offered
    to every table. The thread lane can afford to carry an untabled mention
    because a record-id intersection against already-disclosed rows decides its
    membership; a pointer has no such second opinion, so a mention that cannot
    say which table it came from cannot show it came from an authorized one.
    """
    from .dossier import ensure_dossier, format_connection_label, load_dossier_for_entity
    from .edges import EDGE_SEMANTIC_AFFINITY, top_edges

    allowed_tables = {
        str(t).strip()
        for t in (getattr(manifest, "canonical_tables", None) or [])
        if str(t).strip()
    }
    include_closed = temporal_shift == "past"
    items: List[Dict[str, Any]] = []
    for entity in linked:
        entity_id = entity["entity_id"]
        closed_edges: List[Dict[str, Any]] = []
        if include_closed:
            widened = top_edges(conn, entity_id, limit=6, include_closed=True)
            closed_edges = [
                e
                for e in widened
                if e.get("valid_to") and e.get("edge_type") != EDGE_SEMANTIC_AFFINITY
            ]
        # Prefer the live store row; if refresh skipped this entity (below
        # SIGNIFICANT_MENTIONS) or supersede left no active revision, materialize
        # so named person asks stay on entity_dossier rather than thin graph.
        dossier = load_dossier_for_entity(conn, entity_id)
        if dossier is None:
            try:
                dossier = ensure_dossier(conn, entity)
            except Exception:
                dossier = None
        if dossier:
            text = dossier.get("summary_text") or ""
            connections = dossier.get("top_connections") or []
            if connections:
                labels = [format_connection_label(c) for c in connections[:3]]
                text += " Top connections: " + "; ".join(labels)
            affinity = dossier.get("affinity_connections") or []
            if affinity:
                labels = [format_connection_label(c) for c in affinity[:3]]
                text += (
                    " Similar role-shape (latent affinity, not co-mentioned): "
                    + "; ".join(labels)
                )
            for stat_line in dossier.get("stat_lines") or []:
                text += f" {stat_line}"
            if closed_edges:
                text += " Formerly connected to: " + ", ".join(
                    _closed_edge_label(e) for e in closed_edges[:3]
                ) + "."
            items.append(
                {
                    "topic": dossier.get("canonical_name"),
                    "summary_text": text.strip(),
                    "entity_id": entity_id,
                    "retrieval_source": "entity_dossier",
                    "object_type": "entity_dossier",
                    "disclosure": "owner_only",
                }
            )
        else:
            active_edges = top_edges(conn, entity_id, limit=6)
            observed = [
                e for e in active_edges if e.get("edge_type") != EDGE_SEMANTIC_AFFINITY
            ][:3]
            affinity = [
                e for e in active_edges if e.get("edge_type") == EDGE_SEMANTIC_AFFINITY
            ][:3]
            names = [e["entity_name"] for e in observed]
            names += [_closed_edge_label(e) for e in closed_edges[:3]]
            text_bits = []
            if names:
                text_bits.append(
                    f"{entity['canonical_name']}: connected to " + ", ".join(names)
                )
            if affinity:
                text_bits.append(
                    "similar role-shape (latent affinity): "
                    + ", ".join(e["entity_name"] for e in affinity)
                )
            if text_bits:
                items.append(
                    {
                        "topic": entity["canonical_name"],
                        "summary_text": ". ".join(text_bits),
                        "entity_id": entity_id,
                        "retrieval_source": "entity_graph",
                        "object_type": "entity_dossier",
                        "disclosure": "owner_only",
                    }
                )

        if not allowed_tables:
            continue
        placeholders = ",".join("?" for _ in allowed_tables)
        mention_rows = conn.execute(
            f"""
            SELECT record_id, canonical_table, surface_text, event_at, source_id
            FROM entity_mentions
            WHERE entity_id=? AND canonical_table IN ({placeholders})
            ORDER BY COALESCE(event_at, created_at) DESC LIMIT ?
            """,
            (entity_id, *sorted(allowed_tables), max_per_entity),
        ).fetchall()
        for record_id, table, surface, event_at, source_id in mention_rows:
            items.append(
                {
                    "topic": f"{entity['canonical_name']} in {table}",
                    "summary_text": f"{str(event_at or '')[:10]} — {surface}",
                    "record_id": record_id,
                    "source_id": source_id,
                    "entity_id": entity_id,
                    "canonical_table": table,
                    "retrieval_source": "entity_mention",
                    "object_type": "entity_mention",
                    "disclosure": "owner_only",
                }
            )
    return items
