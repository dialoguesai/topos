"""Adapter + legacy dual-write helpers for signal derivation jobs (Phase 2)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from ..storage.adapters.factory import AdapterBundle
from .derived_tables import DerivedTablesManager

_LEGACY_TABLE_BY_JOB: Dict[str, str] = {
    "emo_27": "message_emotions",
    "entities": "message_entities",
    "embeddings": "message_embeddings",
    "topics": "message_topics",
    "sentiment": "message_sentiment",
    "goal_extraction": "user_goals",
    "relationship_edges": "relationship_edges",
    "dimension_summary": "signal_summaries",
    "url_classification": "signal_tags",
    "availability_scores": "signal_scores",
}


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
    row_id = str(record.get(id_field) or uuid.uuid4())
    payload = _merge_provenance({**record, id_field: row_id}, provenance)
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
    prov = dict(provenance or {})
    prov.setdefault("job_id", job_name)
    written = 0

    legacy_table = _LEGACY_TABLE_BY_JOB.get(job_name)
    if tables_manager and legacy_table:
        written = tables_manager.write_enrichment_batch(records, legacy_table)

    if conn is not None and job_name in (
        "entities",
        "topics",
        "sentiment",
        "emo_27",
        "goal_extraction",
    ):
        wiki_table = {
            "entities": ("message_entities", "entity_id"),
            "topics": ("message_topics", "topic_id"),
            "sentiment": ("message_sentiment", "sentiment_id"),
            "emo_27": ("message_emotions", "emotion_id"),
            "goal_extraction": ("user_goals", "goal_id"),
        }.get(job_name)
        if wiki_table:
            table, id_field = wiki_table
            for rec in records:
                _write_wiki_table(conn, table, rec, id_field=id_field, provenance=prov)
            conn.commit()

    if job_name == "embeddings":
        for rec in records:
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
                    "created_at": rec.get("created_at"),
                },
                prov,
            )
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
        for rec in records:
            adapters.signal.put_summary(
                _merge_provenance(
                    {
                        "dimension": rec.get("dimension", "memory"),
                        "source_id": rec.get("source_id"),
                        "summary_text": rec.get("summary_text"),
                    },
                    prov,
                )
            )
            written += 1
    elif job_name == "goal_extraction":
        for rec in records:
            adapters.signal.put_fact(
                _merge_provenance(
                    {
                        "dimension": "profile",
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

    return written or len(records)
