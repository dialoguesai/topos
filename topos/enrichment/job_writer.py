"""Adapter + legacy dual-write helpers for signal derivation jobs (Phase 2)."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from ..storage.adapters.factory import AdapterBundle
from ..storage.db.write_gate import batched_writes, commit_connection
from .derived_tables import DerivedTablesManager

logger = logging.getLogger(__name__)

_LEGACY_TABLE_BY_JOB: Dict[str, str] = {
    "emo_27": "message_emotions",
    "entities": "message_entities",
    "embeddings": "message_embeddings",
    "topics": "message_topics",
    "sentiment": "message_sentiment",
    "goal_extraction": "user_goals",
    "dimension_summary": "signal_summaries",
    "url_classification": "signal_tags",
    "availability_scores": "signal_scores",
}

# Jobs that persist via signal/graph adapters or dedicated tables — not DerivedTablesManager batches.
_SIGNAL_ONLY_JOBS = frozenset({
    "relationship_edges",
    "topic_clusters",
    "embeddings",
    "dimension_summary",
    "url_classification",
    "availability_scores",
})


def _merge_provenance(record: Dict[str, Any], provenance: Dict[str, Any]) -> Dict[str, Any]:
    merged = {**record}
    for key in ("provider", "model", "job_id", "sync_batch_id", "batch_id"):
        if key in provenance and key not in merged:
            merged[key] = provenance[key]
    merged.setdefault("provenance", provenance)
    return merged


def _write_wiki_table(
    conn: sqlite3.Connection,
    table: str,
    record: Dict[str, Any],
    *,
    id_field: str,
    provenance: Dict[str, Any],
) -> None:
    from .models.mvp_defaults import job_spec_version

    row_id = str(record.get(id_field) or uuid.uuid4())
    payload = _merge_provenance({**record, id_field: row_id}, provenance)
    job_id = str(payload.get("job_id") or provenance.get("job_id") or "")
    spec_v = payload.get("spec_version")
    if spec_v is None:
        spec_v = job_spec_version(job_id)
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "spec_version" in cols:
        conn.execute(
            f"""
            INSERT INTO {table} (
                {id_field}, record_id, source_id, model, provider, payload_json, spec_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT({id_field}) DO UPDATE SET
                payload_json=excluded.payload_json,
                model=excluded.model,
                provider=excluded.provider,
                spec_version=excluded.spec_version
            """,
            (
                row_id,
                record.get("record_id") or record.get("message_id"),
                record.get("source_id"),
                payload.get("model"),
                payload.get("provider"),
                json.dumps(payload),
                int(spec_v),
            ),
        )
        return
    conn.execute(
        f"""
        INSERT INTO {table} (
            {id_field}, record_id, source_id, model, provider, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT({id_field}) DO UPDATE SET
            payload_json=excluded.payload_json,
            model=excluded.model,
            provider=excluded.provider
        """,
        (
            row_id,
            record.get("record_id") or record.get("message_id"),
            record.get("source_id"),
            payload.get("model"),
            payload.get("provider"),
            json.dumps(payload),
        ),
    )


def _resolve_write_conn(
    adapters: AdapterBundle,
    conn: Optional[sqlite3.Connection],
) -> Optional[sqlite3.Connection]:
    if conn is not None:
        return conn
    for attr in ("signal", "vector", "graph", "canonical"):
        store = getattr(adapters, attr, None)
        store_conn = getattr(store, "_conn", None)
        if store_conn is not None:
            return store_conn
    return None


def write_signal_records(
    job_name: str,
    records: List[Dict[str, Any]],
    *,
    adapters: AdapterBundle,
    tables_manager: Optional[DerivedTablesManager] = None,
    provenance: Optional[Dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Write derivation output to signal adapters and optional legacy tables."""
    if not records:
        return 0
    write_conn = _resolve_write_conn(adapters, conn)

    def _write() -> int:
        return _write_signal_records_unlocked(
            job_name,
            records,
            adapters=adapters,
            tables_manager=tables_manager,
            provenance=provenance,
            conn=conn,
        )

    if write_conn is not None:
        with batched_writes(write_conn):
            return _write()
    return _write()


def _write_signal_records_unlocked(
    job_name: str,
    records: List[Dict[str, Any]],
    *,
    adapters: AdapterBundle,
    tables_manager: Optional[DerivedTablesManager] = None,
    provenance: Optional[Dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    from .models.mvp_defaults import job_spec_version

    prov = dict(provenance or {})
    prov.setdefault("job_id", job_name)
    prov.setdefault("spec_version", job_spec_version(job_name))
    written = 0
    for rec in records:
        rec.setdefault("spec_version", prov["spec_version"])
        rec.setdefault("job_id", job_name)

    legacy_table = _LEGACY_TABLE_BY_JOB.get(job_name)
    if tables_manager and legacy_table and job_name not in _SIGNAL_ONLY_JOBS:
        written = tables_manager.write_enrichment_batch(records, legacy_table)

    if conn is not None and job_name in (
        "entities",
        "topics",
        "sentiment",
        "goal_extraction",
    ):
        wiki_table = {
            "entities": ("message_entities", "entity_id"),
            "topics": ("message_topics", "topic_id"),
            "sentiment": ("message_sentiment", "sentiment_id"),
            "goal_extraction": ("user_goals", "goal_id"),
        }.get(job_name)
        if wiki_table:
            table, id_field = wiki_table
            for rec in records:
                _write_wiki_table(conn, table, rec, id_field=id_field, provenance=prov)
            commit_connection(conn)

    if job_name == "embeddings":
        vector_index = adapters.vector
        get_hashes = getattr(vector_index, "get_embedding_hashes", None)
        delete_chunks = getattr(vector_index, "delete_chunks_for_record", None)
        grouped: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        for rec in records:
            rid = str(rec.get("record_id") or rec.get("message_id") or "")
            model = str(rec.get("model") or prov.get("model") or "")
            if rid and model:
                grouped.setdefault((rid, model), []).append(rec)

        skipped_groups: set[tuple[str, str]] = set()
        for (rid, model), group in grouped.items():
            if delete_chunks is not None:
                keep = sorted({int(r.get("chunk_index") or 0) for r in group})
                delete_chunks(rid, model, keep_indices=keep)
            if get_hashes is not None:
                existing = get_hashes(rid, model)
                parent_hash = str(group[0].get("content_hash") or "")
                if parent_hash and existing and all(
                    existing.get(int(r.get("chunk_index") or 0)) == parent_hash for r in group
                ):
                    logger.debug("Skipping re-embed write for unchanged record %s", rid)
                    skipped_groups.add((rid, model))

        for rec in records:
            rid = str(rec.get("record_id") or rec.get("message_id") or "")
            model = str(rec.get("model") or prov.get("model") or "")
            if (rid, model) in skipped_groups:
                continue
            meta = _merge_provenance(
                {
                    "embedding_id": rec.get("embedding_id"),
                    "record_id": rec.get("record_id") or rec.get("message_id"),
                    "source_id": rec.get("source_id"),
                    "signal_dimension": rec.get("signal_dimension", "memory"),
                    "model": rec.get("model"),
                    "provider": rec.get("provider", "huggingface"),
                    "dims": rec.get("dims"),
                    "text_preview": (rec.get("text_preview") or "")[:200],
                    "search_text": (rec.get("search_text") or rec.get("text_preview") or "")[:2000],
                    "created_at": rec.get("created_at"),
                    "content_hash": rec.get("content_hash"),
                    "chunk_index": rec.get("chunk_index", 0),
                    "event_at": rec.get("event_at"),
                    "conversation_id": rec.get("conversation_id"),
                    "record_type": rec.get("record_type"),
                    "chunk_strategy": rec.get("chunk_strategy"),
                    "chunk_count": rec.get("chunk_count"),
                },
                prov,
            )
            if get_hashes is not None:
                rid = str(meta.get("record_id") or "")
                model = str(meta.get("model") or "")
                parent_hash = str(meta.get("content_hash") or "")
                chunk_index = int(meta.get("chunk_index") or 0)
                if rid and model and parent_hash:
                    existing = get_hashes(rid, model)
                    if existing.get(chunk_index) == parent_hash:
                        continue
            adapters.vector.upsert(meta, vector=rec.get("vector"))
            written += 1
    elif job_name == "entities":
        for rec in records:
            entity_text = rec.get("entity_text") or rec.get("text")
            if not entity_text:
                continue
            node_id = adapters.graph.upsert_node(
                _merge_provenance(
                    {
                        "node_type": "entity",
                        "label": entity_text,
                        "source_id": rec.get("source_id"),
                        "dimension": "relationships",
                        "record_id": rec.get("record_id") or rec.get("message_id"),
                    },
                    prov,
                )
            )
            adapters.signal.put_fact(
                _merge_provenance(
                    {
                        "dimension": "relationships",
                        "source_id": rec.get("source_id"),
                        "record_id": rec.get("record_id") or rec.get("message_id"),
                        "entity_text": entity_text,
                        "entity_type": rec.get("entity_type"),
                        "node_id": node_id,
                    },
                    prov,
                )
            )
            written += 1
    elif job_name == "emo_27":
        for rec in records:
            adapters.signal.put_score(
                _merge_provenance(
                    {
                        "dimension": "memory",
                        "source_id": rec.get("source_id"),
                        "record_id": rec.get("message_id"),
                        "score_type": "emotion",
                        "label": rec.get("emotion_label"),
                        "value": rec.get("confidence"),
                    },
                    prov,
                )
            )
            written += 1
    elif job_name == "sentiment":
        for rec in records:
            adapters.signal.put_score(
                _merge_provenance(
                    {
                        "dimension": "memory",
                        "source_id": rec.get("source_id"),
                        "record_id": rec.get("message_id"),
                        "score_type": "sentiment",
                        "label": rec.get("label"),
                        "value": rec.get("score"),
                    },
                    prov,
                )
            )
            written += 1
    elif job_name == "topics":
        for rec in records:
            adapters.signal.put_fact(
                _merge_provenance(
                    {
                        "dimension": "memory",
                        "source_id": rec.get("source_id"),
                        "record_id": rec.get("message_id"),
                        "topic": rec.get("topic") or rec.get("label"),
                        "confidence": rec.get("confidence"),
                    },
                    prov,
                )
            )
            written += 1
    elif job_name == "dimension_summary":
        # Briefs are written inside DimensionSummaryJob.enrich().
        written = len([r for r in records if r.get("_brief_updated")])
    elif job_name == "goal_extraction":
        for rec in records:
            adapters.signal.put_fact(
                _merge_provenance(
                    {
                        "dimension": "work",
                        "source_id": rec.get("source_id"),
                        "record_id": rec.get("message_id"),
                        "goal_text": rec.get("goal_text") or rec.get("text"),
                        "confidence": rec.get("confidence"),
                    },
                    prov,
                )
            )
            written += 1
    elif job_name == "relationship_edges":
        for rec in records:
            src = rec.get("src_node_id") or rec.get("src")
            dst = rec.get("dst_node_id") or rec.get("dst")
            if not src or not dst:
                continue
            prov_rec = _merge_provenance(
                {
                    "source_id": rec.get("source_id"),
                    "dimension": "relationships",
                },
                prov,
            )
            for node_id in (src, dst):
                adapters.graph.upsert_node(
                    {
                        **prov_rec,
                        "node_id": str(node_id),
                        "node_type": (
                            "contact"
                            if str(node_id).startswith("contact:")
                            else "conversation"
                            if str(node_id).startswith("conversation:")
                            else "entity"
                        ),
                        "label": (
                            "Unknown sender"
                            if str(node_id) == "contact:unknown"
                            else str(node_id).split(":", 1)[-1][:48]
                        ),
                    }
                )
            adapters.graph.upsert_edge(
                _merge_provenance(
                    {
                        "src_node_id": src,
                        "dst_node_id": dst,
                        "edge_type": rec.get("edge_type", "message_frequency"),
                        "weight": rec.get("weight", 1.0),
                        "source_id": rec.get("source_id"),
                        "dimension": "relationships",
                    },
                    prov,
                )
            )
            written += 1
    elif job_name == "availability_scores":
        for rec in records:
            adapters.signal.put_score(
                _merge_provenance(
                    {
                        "dimension": "time",
                        "source_id": rec.get("source_id"),
                        "score_type": "availability",
                        "label": rec.get("label", "availability"),
                        "value": rec.get("score", 0.0),
                    },
                    prov,
                )
            )
            written += 1
    elif job_name == "url_classification":
        for rec in records:
            adapters.signal.put_fact(
                _merge_provenance(
                    {
                        "dimension": "interests",
                        "source_id": rec.get("source_id"),
                        "record_id": rec.get("record_id"),
                        "tag": rec.get("category") or rec.get("tag"),
                        "confidence": rec.get("confidence"),
                    },
                    prov,
                )
            )
            written += 1
    elif job_name == "topic_clusters":
        # Persisted inside TopicClusterJob.enrich(); records are summary rows only.
        written = len(records)

    return written or len(records)
