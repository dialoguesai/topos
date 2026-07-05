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


def _vec_table_dims(conn: sqlite3.Connection) -> int:
    """Dims declared in the vec0 DDL (0 when table missing/unparseable)."""
    from ...db.migrations.vector_storage_v4 import declared_vec_dims

    try:
        return declared_vec_dims(conn)
    except Exception:
        return 0


def sync_vec_row(
    conn: sqlite3.Connection,
    *,
    embedding_id: str,
    vector: List[float],
) -> None:
    if not _sqlite_vec_ready(conn) or len(vector) != _vec_table_dims(conn):
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
    if not _sqlite_vec_ready(conn) or len(query_vector) != _vec_table_dims(conn):
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

    # Batch-fetch metadata with filters pushed into SQL (avoids N+1 lookups).
    distance_by_id = {str(eid): dist for eid, dist in vec_rows}
    id_list = list(distance_by_id.keys())
    placeholders = ",".join("?" for _ in id_list)
    filter_sql = ""
    filter_params: List[Any] = []
    if source_id is not None:
        filter_sql += " AND source_id = ?"
        filter_params.append(source_id)
    if dimension is not None:
        filter_sql += " AND signal_dimension = ?"
        filter_params.append(dimension)
    if model is not None:
        filter_sql += " AND model = ?"
        filter_params.append(model)
    if event_after is not None:
        filter_sql += " AND (event_at IS NULL OR event_at >= ?)"
        filter_params.append(event_after)
    if event_before is not None:
        filter_sql += " AND (event_at IS NULL OR event_at <= ?)"
        filter_params.append(event_before)
    meta_rows = conn.execute(
        f"""
        SELECT embedding_id, provenance_json
        FROM signal_embeddings
        WHERE embedding_id IN ({placeholders}){filter_sql}
        """,
        (*id_list, *filter_params),
    ).fetchall()

    scored: List[tuple[float, Dict[str, Any]]] = []
    for embedding_id, prov_raw in meta_rows:
        distance = distance_by_id.get(str(embedding_id))
        meta = json.loads(prov_raw)
        sim = 1.0 - float(distance) if distance is not None else 0.0
        meta["similarity"] = round(sim, 6)
        scored.append((sim, meta))

    # ANN fetched the global top-N; if filters trimmed it below the requested
    # limit there may be qualifying rows outside the ANN window. Fall back to
    # exact search rather than silently returning a starved result set.
    has_filters = bool(filter_sql)
    if not scored or (has_filters and len(scored) < limit):
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
