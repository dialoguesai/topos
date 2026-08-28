"""Hybrid vector + FTS search with reciprocal rank fusion."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

_FTS_TABLE = "signal_embeddings_fts"


def _fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (_FTS_TABLE,),
    ).fetchone()
    return row is not None


# Function words carry no topic and match nearly every document, so OR-joining
# them hands the keyword budget to rows that share only grammar with the ask.
# Measured on the owner's node 2026-08-27 for "who did I eat with at the cafe":
# the full query returned 60 hits, "eat cafe" returned 36, and **35 of the 60
# matched no content word at all** — 58% of the budget spent on "who/did/with/
# at/the". The list is deliberately small and closed-class; anything topical
# stays searchable.
_STOPWORDS = frozenset(
    """
    the a an and or but if then than that this these those there here
    is am are was were be been being do does did done doing
    have has had having will would shall should can could may might must
    of in on at by to for from with without about into over under again
    me my mine you your yours we our ours they them their it its
    who whom whose what which when where why how
    as so too very just also any some all both each more most other such
    no nor not only own same s t don now
    """.split()
)


def _tokenize_query(query: str) -> str:
    tokens = re.findall(r"[a-z0-9]{2,}", (query or "").lower())
    if not tokens:
        return ""
    content = [t for t in tokens if t not in _STOPWORDS]
    # An ask made entirely of function words ("what is this about") has no
    # content to match on. Falling back to the raw tokens is worse than useless
    # there — it returns the whole corpus ranked by grammar — but returning
    # nothing lets the vector half answer alone, which is what it is for.
    return " OR ".join(content)


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    source_id: Optional[str] = None,
) -> List[str]:
    """Keyword half of the hybrid search.

    ``source_id`` mirrors the vector half's filter. Without it a source-scoped
    search returns scoped vectors fused with UNSCOPED keyword hits, so the
    scope silently applies to one contributor out of two — and the caller
    that surfaced this (the derived-object lane, which is scoped by source)
    would have spent most of its budget on rows it then discarded. Omitted
    means unfiltered, which is every existing call site.
    """
    if not _fts_available(conn) or not str(query or "").strip():
        return []
    fts_query = _tokenize_query(query)
    if not fts_query:
        return []
    source_clause = " AND e.source_id = ?" if source_id is not None else ""
    params: List[Any] = [fts_query]
    if source_id is not None:
        params.append(source_id)
    params.append(limit)
    try:
        # One row per distinct document, best-ranked wins. 2,634 of 9,429 live
        # embeddings (28%) are redundant copies of another's text, so without
        # this the budget buys the same document several times: 60 rows bought
        # 46 distinct documents on the worst measured query. `content_hash`
        # groups chunks of one record together, so it falls back to the text
        # when absent rather than collapsing a whole record to its first chunk.
        rows = conn.execute(
            f"""
            SELECT e.embedding_id
            FROM {_FTS_TABLE} f
            JOIN signal_embeddings e ON e.rowid = f.rowid
            WHERE f.search_text MATCH ?{source_clause}
            GROUP BY COALESCE(NULLIF(e.content_hash, ''), f.search_text),
                     COALESCE(e.chunk_index, 0)
            HAVING f.rank = MIN(f.rank)
            ORDER BY f.rank
            LIMIT ?
            """,
            params,
        ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in rows]


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]],
    *,
    k: int = 60,
) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _fetch_metadata(conn: sqlite3.Connection, embedding_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT provenance_json FROM signal_embeddings WHERE embedding_id=?",
        (embedding_id,),
    ).fetchone()
    if not row:
        return None
    return json.loads(row[0])


def merge_hybrid_results(
    conn: sqlite3.Connection,
    vector_items: List[Dict[str, Any]],
    fts_ids: List[str],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    vector_rank = [str(item.get("embedding_id")) for item in vector_items if item.get("embedding_id")]
    fused = reciprocal_rank_fusion([vector_rank, fts_ids])

    by_id = {str(item.get("embedding_id")): dict(item) for item in vector_items if item.get("embedding_id")}
    for embedding_id in fts_ids:
        if embedding_id not in by_id:
            meta = _fetch_metadata(conn, embedding_id)
            if meta:
                by_id[embedding_id] = meta

    merged: List[Dict[str, Any]] = []
    for embedding_id, score in sorted(fused.items(), key=lambda pair: pair[1], reverse=True):
        item = by_id.get(embedding_id)
        if not item:
            continue
        item = dict(item)
        item["hybrid_score"] = round(score, 6)
        # NB: similarity stays cosine-only; FTS-only hits carry hybrid_score alone.
        # Conflating the two lets similarity thresholds silently drop keyword matches.
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged
