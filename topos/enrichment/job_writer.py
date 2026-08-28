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
    "availability_scores": "signal_scores",
}

# Jobs that persist via signal/graph adapters or dedicated tables — not DerivedTablesManager batches.
_SIGNAL_ONLY_JOBS = frozenset({
    "relationship_edges",
    "topic_clusters",
    "embeddings",
    "dimension_summary",
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
    typed_writer_ran: bool = False,
) -> bool:
    """Add provenance/spec_version to the row the typed writer just wrote.

    This is an UPSERT onto that row, not a second insert. It only works because
    ``DerivedTablesManager._stable_row_id`` stamps the id it minted back onto the
    shared record dict — without that both writers minted their own uuid and the
    table ended up with a typed row and an untyped twin for every record.

    ``typed_writer_ran`` closes the other half. The typed writers SKIP a record
    whose typed field is empty (``if not entity_text: continue``,
    ``if not goal_text: continue``), so an unstamped record here means "there is
    no row to add provenance to" — writing one anyway is how a bare row with a
    NULL typed column gets created. When the typed writer did not run at all
    (no ``tables_manager``), the old mint-and-insert behaviour is kept, because
    then this is the only writer and the row is not a duplicate of anything.

    Returns True when a row was written.
    """
    from .models.mvp_defaults import job_spec_version

    stamped = str(record.get(id_field) or "").strip()
    if typed_writer_ran and not stamped:
        return False
    row_id = stamped or str(uuid.uuid4())
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
        return True
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
    return True


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
    typed_writer_ran = False
    if tables_manager and legacy_table and job_name not in _SIGNAL_ONLY_JOBS:
        written = tables_manager.write_enrichment_batch(records, legacy_table)
        typed_writer_ran = True

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
                _write_wiki_table(
                    conn,
                    table,
                    rec,
                    id_field=id_field,
                    provenance=prov,
                    typed_writer_ran=typed_writer_ran,
                )
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
            # Activity rows dedupe ACROSS records: one vector per distinct
            # page text (browser visits repeat the same title dozens of times
            # a month, and every copy is a near-identical ANN neighbor).
            # Message-family rows are exempt — each message must stay
            # individually reachable through vector search.
            has_dup = getattr(vector_index, "has_duplicate_content", None)
            if (
                has_dup is not None
                and str(meta.get("record_type") or "") == "activity_event"
            ):
                rid = str(meta.get("record_id") or "")
                model = str(meta.get("model") or "")
                parent_hash = str(meta.get("content_hash") or "")
                if (
                    rid
                    and model
                    and parent_hash
                    and has_dup(
                        parent_hash,
                        model,
                        record_type="activity_event",
                        exclude_record_id=rid,
                    )
                ):
                    continue
            adapters.vector.upsert(meta, vector=rec.get("vector"))
            written += 1
    elif job_name == "entities":
        for rec in records:
            entity_text = rec.get("entity_text") or rec.get("text")
            if not entity_text:
                continue
            # No graph_nodes row for an extracted entity.
            #
            # This call passed no ``node_id``, so ``upsert_node`` minted a fresh
            # uuid4 for EVERY mention. Nothing ever resolved those ids: measured
            # on the owner's node 2026-08-27, 32,631 `entity` nodes existed, **0
            # matched a spine entity_id** and **0 were referenced by any edge**.
            # Only 365 of 32,996 graph_nodes rows were reachable at all, and all
            # 3,866 edges belong to the messenger `message_frequency` projection
            # between contact/conversation nodes.
            #
            # It was also writing to a table the codebase has already retired:
            # ``lifecycle/gc.py`` declares graph_nodes "superseded by entity graph
            # (entities + entity_edges)" and graph_edges "superseded by
            # entity_edges", and the product read path is
            # ``entities/reads.py`` -> ``edges.graph_snapshot``. Adding record
            # provenance to these rows — the obvious reading of "graph edges
            # carry no record_id" — would have been work in the direction of a
            # store that is on its way out. The entity graph carries the real
            # thing: ``entity_mentions`` links every entity to its record, and
            # ``entity_edges`` carries validity and evidence counts.
            #
            # The fact payload drops ``node_id`` with it. 32,039 signal_facts on
            # the owner's node carry one, and every value points at an orphan. A
            # key holding a dead id reads as provenance and is worse than no key.
            #
            # A RESOLVED identity is the exception, and it is exactly what PRD_04
            # ("relationship edges use person_id") asks for. When the caller has
            # already resolved this mention to a person, the node is keyed on that
            # id: joinable, stable across mentions, and not an orphan. The uuid4
            # path produced 32,631 unreferenced rows precisely BECAUSE it had no
            # identity to key on and invented one anyway — the gap test that
            # covers this passed on the strength of those invented ids while the
            # `person_id` it carefully resolved was never read.
            person_id = str(rec.get("person_id") or "").strip()
            node_id = (
                adapters.graph.upsert_node(
                    _merge_provenance(
                        {
                            "node_id": person_id,
                            "node_type": "person",
                            "label": entity_text,
                            "source_id": rec.get("source_id"),
                            "dimension": "relationships",
                            "record_id": rec.get("record_id") or rec.get("message_id"),
                        },
                        prov,
                    )
                )
                if person_id
                else None
            )
            # File the fact by what the entity IS, not by which job wrote it.
            # `entity_type` was already in this dict and the dimension beside it
            # said "relationships" regardless — so an ORG, a city and a calendar
            # date all landed in the relationships filter. Untypable mentions
            # fall back to the record's own dimension rather than a guess.
            from ..features.signal.dimension_registry import dimension_for_entity_type
            from ..features.signal.embed_context import dimension_for_record

            entity_type = rec.get("entity_type")
            adapters.signal.put_fact(
                _merge_provenance(
                    {
                        "dimension": dimension_for_entity_type(
                            entity_type, fallback=dimension_for_record(rec)
                        ),
                        "source_id": rec.get("source_id"),
                        "record_id": rec.get("record_id") or rec.get("message_id"),
                        "entity_text": entity_text,
                        "entity_type": entity_type,
                        **({"node_id": node_id} if node_id else {}),
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
    elif job_name == "topic_clusters":
        # Persisted inside TopicClusterJob.enrich(); records are summary rows only.
        written = len(records)

    return written or len(records)
