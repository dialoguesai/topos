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


def _tokenize_query(query: str) -> str:
    tokens = re.findall(r"[a-z0-9]{2,}", (query or "").lower())
    if not tokens:
        return ""
    return " OR ".join(tokens)


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> List[str]:
    if not _fts_available(conn) or not str(query or "").strip():
        return []
    fts_query = _tokenize_query(query)
    if not fts_query:
        return []
    try:
        rows = conn.execute(
            f"""
            SELECT e.embedding_id
            FROM {_FTS_TABLE} f
            JOIN signal_embeddings e ON e.rowid = f.rowid
            WHERE f.search_text MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit),
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
