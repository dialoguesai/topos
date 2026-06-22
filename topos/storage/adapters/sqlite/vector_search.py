"""Vector similarity search backends (brute-force + optional sqlite-vec ANN)."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ....features.signal.vector_codec import decode_vector, encode_f32, similarity
from ....features.signal.vector_settings import vector_ann_mode

logger = logging.getLogger(__name__)

_VEC_TABLE = "signal_embeddings_vec"


def _sqlite_vec_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (_VEC_TABLE,),
    ).fetchone()
    return row is not None


def sync_vec_row(
    conn: sqlite3.Connection,
    *,
    embedding_id: str,
    vector: List[float],
) -> None:
    if not _sqlite_vec_ready(conn) or len(vector) != 384:
        return
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {_VEC_TABLE}(embedding_id, embedding) VALUES (?, ?)",
            (embedding_id, encode_f32(vector)),
        )
    except sqlite3.Error as exc:
        logger.debug("sqlite-vec sync skipped for %s: %s", embedding_id, exc)


def delete_vec_rows(conn: sqlite3.Connection, embedding_ids: List[str]) -> None:
    if not _sqlite_vec_ready(conn) or not embedding_ids:
        return
    for embedding_id in embedding_ids:
        try:
            conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE embedding_id = ?", (embedding_id,))
        except sqlite3.Error:
            pass


def search_similar_brute_force(
    conn: sqlite3.Connection,
    query_vector: List[float],
    *,
    source_id: Optional[str] = None,
    dimension: Optional[str] = None,
    model: Optional[str] = None,
    event_after: Optional[str] = None,
    event_before: Optional[str] = None,
    limit: int = 20,
    fetch_limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    fetch = fetch_limit or limit
    fetch = max(fetch, limit)
    query_sql = """
        SELECT provenance_json, vector_blob, vector_format
        FROM signal_embeddings
        WHERE vector_blob IS NOT NULL
    """
    params: List[Any] = []
    if source_id is not None:
        query_sql += " AND source_id=?"
        params.append(source_id)
    if dimension is not None:
        query_sql += " AND signal_dimension=?"
        params.append(dimension)
    if model is not None:
        query_sql += " AND model=?"
        params.append(model)
    if event_after is not None:
        query_sql += " AND (event_at IS NULL OR event_at >= ?)"
        params.append(event_after)
    if event_before is not None:
        query_sql += " AND (event_at IS NULL OR event_at <= ?)"
        params.append(event_before)

    rows = conn.execute(query_sql, params).fetchall()
    scored: List[tuple[float, Dict[str, Any]]] = []
    query_dims = len(query_vector)
    for prov_raw, blob, vector_format in rows:
        if not blob:
            continue
        try:
            stored = decode_vector(blob, vector_format or "json")
        except Exception:
            continue
        if len(stored) != query_dims:
            continue
        meta = json.loads(prov_raw)
        sim = similarity(query_vector, stored, normalized=True)
        meta["similarity"] = round(sim, 6)
        scored.append((sim, meta))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[:fetch]
    return [meta for _, meta in top], len(scored)


def search_similar_ann(
    conn: sqlite3.Connection,
    query_vector: List[float],
    *,
    source_id: Optional[str] = None,
    dimension: Optional[str] = None,
    model: Optional[str] = None,
    event_after: Optional[str] = None,
    event_before: Optional[str] = None,
    limit: int = 20,
    fetch_limit: Optional[int] = None,
) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    if len(query_vector) != 384 or not _sqlite_vec_ready(conn):
        return None
    fetch = max(fetch_limit or limit, limit)
    try:
        vec_rows = conn.execute(
            f"""
            SELECT v.embedding_id, v.distance
            FROM {_VEC_TABLE} v
            WHERE v.embedding MATCH ?
            ORDER BY distance
            LIMIT ?
            """,
            (encode_f32(query_vector), fetch * 5),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("sqlite-vec search failed: %s", exc)
        return None

    if not vec_rows:
        return None

    scored: List[tuple[float, Dict[str, Any]]] = []
    for embedding_id, distance in vec_rows:
        row = conn.execute(
            """
            SELECT provenance_json, source_id, signal_dimension, model, event_at
            FROM signal_embeddings
            WHERE embedding_id = ?
            """,
            (embedding_id,),
        ).fetchone()
        if not row:
            continue
        prov_raw, sid, dim, mdl, event_at = row
        if source_id is not None and sid != source_id:
            continue
        if dimension is not None and dim != dimension:
            continue
        if model is not None and mdl != model:
            continue
        if event_after is not None and event_at is not None and event_at < event_after:
            continue
        if event_before is not None and event_at is not None and event_at > event_before:
            continue
        meta = json.loads(prov_raw)
        sim = 1.0 - float(distance) if distance is not None else 0.0
        meta["similarity"] = round(sim, 6)
        scored.append((sim, meta))
        if len(scored) >= fetch:
            break

    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [meta for _, meta in scored[:fetch]], len(scored)


def search_similar(
    conn: sqlite3.Connection,
    query_vector: List[float],
    *,
    source_id: Optional[str] = None,
    dimension: Optional[str] = None,
    model: Optional[str] = None,
    event_after: Optional[str] = None,
    event_before: Optional[str] = None,
    limit: int = 20,
    fetch_limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    mode = vector_ann_mode()
    if mode in ("auto", "sqlite_vec"):
        ann = search_similar_ann(
            conn,
            query_vector,
            source_id=source_id,
            dimension=dimension,
            model=model,
            event_after=event_after,
            event_before=event_before,
            limit=limit,
            fetch_limit=fetch_limit,
        )
        if ann is not None:
            return ann
        if mode == "sqlite_vec":
            logger.warning("TOPOS_VECTOR_ANN=sqlite_vec but ANN unavailable; falling back to brute-force")
    return search_similar_brute_force(
        conn,
        query_vector,
        source_id=source_id,
        dimension=dimension,
        model=model,
        event_after=event_after,
        event_before=event_before,
        limit=limit,
        fetch_limit=fetch_limit,
    )
