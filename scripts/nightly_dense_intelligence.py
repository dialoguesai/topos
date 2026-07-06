"""Nightly dense-intelligence run.

Operates ONLY on a backup copy of the node database (sqlite backup API;
the live file is opened read-only and never written).

Steps:
  1. copy       — snapshot the live DB into the nightly workdir
  2. migrate    — apply migrations to the copy (idempotent)
  3. entities   — seed from contacts, resolve historical NER mentions,
                  build decayed edges, refresh dossiers
  4. stats      — fold all canonical history, promote stat insights
  5. facts      — rules extraction over profile/journal history
  6. timeline   — backfill the temporal projection
  7. clusters   — full recompute with stable IDs + top_topics facts
  8. reembed    — bge-small-en-v1.5 passage embeddings alongside MiniLM
  9. compare    — probe-query A/B between embedding namespaces

Writes nightly_report.json into the workdir and prints a summary.
Report contains aggregate numbers only — no personal content beyond
short truncated labels.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

CANONICAL_TABLES = (
    "ai_chat_messages",
    "conversation_messages",
    "journal_entries",
    "calendar_events",
    "profile_records",
    "activity_events",
    "financial_transactions",
    "location_events",
)

BGE_MODEL = "BAAI/bge-small-en-v1.5"
MINILM_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

GENERIC_PROBES = [
    "what have I been working on recently",
    "who do I talk to the most",
    "how do I spend my money",
    "what are my plans for next week",
    "how is my health and sleep",
    "what topics am I researching",
]


def log(step: str, message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{step}] {message}", flush=True)


def dict_rows(conn: sqlite3.Connection, query: str, params=()) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.row_factory = None
    return rows


# ---------------------------------------------------------------- steps


def step_copy(source: Path, dest: Path) -> Dict[str, Any]:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    dst = sqlite3.connect(str(dest))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return {"copied_to": str(dest), "size_mb": round(dest.stat().st_size / 1e6, 1)}


def step_migrate(conn: sqlite3.Connection) -> Dict[str, Any]:
    from topos.storage.db.migrations import apply_all_migrations

    apply_all_migrations(conn)
    return {"ok": True}


def _entity_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("payload_json") or "{}")
    except json.JSONDecodeError:
        return {}


def step_entities(conn: sqlite3.Connection) -> Dict[str, Any]:
    from topos.features.entities.dossier import refresh_dossiers
    from topos.features.entities.edges import EDGE_CO_OCCURRENCE, update_edge
    from topos.features.entities.resolver import (
        AUTO_MERGE_SCORE,
        EntityResolver,
        is_valid_entity_surface,
        map_ner_type,
        normalize_name,
        token_set_similarity,
    )

    resolver = EntityResolver(conn)
    seeded = resolver.seed_from_contacts()
    log("entities", f"seeded {seeded} entities from contacts")

    # In-memory blocking index for batch resolution (the per-mention resolver
    # is O(entities) on fuzzy; blocking by shared token keeps this tractable).
    name_index: Dict[tuple, str] = {}
    token_index: Dict[str, List[tuple]] = {}
    contact_person_index: Dict[str, List[str]] = {}  # token -> contact person ids
    contact_person_names: Dict[str, str] = {}  # normalized full name -> id

    def index_entity(
        entity_id: str, etype: str, normalized: str, contact_id: Optional[str] = None
    ) -> None:
        name_index[(etype, normalized)] = entity_id
        for token in normalized.split():
            token_index.setdefault(token, []).append((entity_id, etype, normalized))
            if etype == "person" and contact_id:
                bucket = contact_person_index.setdefault(token, [])
                if entity_id not in bucket:
                    bucket.append(entity_id)
        if etype == "person" and contact_id:
            contact_person_names[normalized] = entity_id

    for entity_id, etype, normalized, contact_id in conn.execute(
        "SELECT entity_id, entity_type, normalized_name, contact_id FROM entities"
    ).fetchall():
        index_entity(str(entity_id), str(etype), str(normalized), contact_id)

    mentions = dict_rows(
        conn,
        """
        SELECT record_id, source_id, entity_text, payload_json
        FROM message_entities WHERE entity_text IS NOT NULL
        """,
    )
    resolved = 0
    created = 0
    skipped = 0
    by_record: Dict[str, List[str]] = {}
    t0 = time.time()
    for row in mentions:
        payload = _entity_payload(row)
        confidence = float(payload.get("confidence") or 0.0)
        surface = str(row.get("entity_text") or "").strip()
        if not surface or confidence < 0.6 or not is_valid_entity_surface(surface):
            skipped += 1
            continue
        etype = map_ner_type(payload.get("entity_type"))
        normalized = normalize_name(surface)
        if not normalized:
            skipped += 1
            continue
        # Contact-seeded people outrank NER typing (mirrors resolver tier 1.5):
        # a unique contact match wins even when NER said "place"/"org".
        entity_id = contact_person_names.get(normalized)
        if entity_id is None and " " not in normalized:
            contact_candidates = contact_person_index.get(normalized, [])
            if len(contact_candidates) == 1:
                entity_id = contact_candidates[0]
        if entity_id is not None:
            etype = "person"
        if entity_id is None:
            entity_id = name_index.get((etype, normalized))
        if entity_id is None and etype == "person" and " " not in normalized:
            candidates = {
                eid
                for eid, et, name in token_index.get(normalized, [])
                if et == "person" and normalized in name.split()
            }
            if len(candidates) == 1:
                entity_id = next(iter(candidates))
        if entity_id is None:
            blocked = {
                (eid, name)
                for token in normalized.split()
                for eid, et, name in token_index.get(token, [])
                if et == etype
            }
            best_id, best_score, ties = None, 0.0, 0
            for eid, name in blocked:
                score = token_set_similarity(normalized, name)
                if score >= AUTO_MERGE_SCORE:
                    ties += 1
                if score > best_score:
                    best_id, best_score = eid, score
            if best_id and best_score >= AUTO_MERGE_SCORE and ties == 1:
                entity_id = best_id
        if entity_id is None:
            entity_id = resolver._create_entity(surface, etype)
            index_entity(entity_id, etype, normalized)
            created += 1
        resolver.record_mention(
            entity_id,
            record_id=str(row.get("record_id") or ""),
            surface_text=surface,
            source_id=row.get("source_id"),
            confidence=confidence,
        )
        resolved += 1
        by_record.setdefault(str(row.get("record_id") or ""), []).append(entity_id)

    edges = 0
    for record_id, ids in by_record.items():
        unique = list(dict.fromkeys(ids))[:8]
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                update_edge(
                    conn,
                    src_entity_id=unique[i],
                    dst_entity_id=unique[j],
                    edge_type=EDGE_CO_OCCURRENCE,
                )
                edges += 1
    conn.commit()
    dossiers = refresh_dossiers(conn)
    total_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    reviews = conn.execute("SELECT COUNT(*) FROM entity_review").fetchone()[0]
    return {
        "seeded_from_contacts": seeded,
        "mentions_resolved": resolved,
        "entities_created_from_mentions": created,
        "mentions_skipped_low_confidence": skipped,
        "edges_updated": edges,
        "dossiers_written": dossiers,
        "entities_total": total_entities,
        "review_queue": reviews,
        "seconds": round(time.time() - t0, 1),
    }


def _canonical_batches(conn: sqlite3.Connection, batch_size: int = 500):
    for table in CANONICAL_TABLES:
        try:
            rows = dict_rows(conn, f"SELECT * FROM {table}")
        except sqlite3.OperationalError:
            continue
        for row in rows:
            row["_table"] = table
        for start in range(0, len(rows), batch_size):
            yield table, rows[start : start + batch_size]


def step_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    from topos.features.stats.engine import StatsEngine
    from topos.storage.adapters.factory import AdapterFactory

    engine = StatsEngine(conn)
    folded = 0
    skipped = 0
    t0 = time.time()
    for _table, batch in _canonical_batches(conn):
        result = engine.fold_batch(batch)
        folded += result["rows_folded"]
        skipped += result["rows_skipped_seen"]
    bundle = AdapterFactory.create("local_database", conn=conn)
    promoted = engine.promote_insights(bundle)
    states = conn.execute("SELECT COUNT(*) FROM stat_state").fetchone()[0]
    return {
        "rows_folded": folded,
        "rows_skipped_seen": skipped,
        "insights_promoted": promoted,
        "stat_state_rows": states,
        "seconds": round(time.time() - t0, 1),
    }


def step_facts(conn: sqlite3.Connection) -> Dict[str, Any]:
    from topos.features.facts.extract import extract_facts_from_batch

    rows: List[Dict[str, Any]] = []
    for table in ("profile_records", "journal_entries"):
        try:
            table_rows = dict_rows(conn, f"SELECT * FROM {table}")
        except sqlite3.OperationalError:
            continue
        for row in table_rows:
            row["_table"] = table
        rows.extend(table_rows)
    written = extract_facts_from_batch(conn, rows)
    active = conn.execute(
        "SELECT COUNT(*) FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
    ).fetchone()[0]
    conflicts = conn.execute("SELECT COUNT(*) FROM fact_conflicts").fetchone()[0]
    return {"assertions": written, "active_facts": active, "conflicts_queued": conflicts}


def step_timeline(conn: sqlite3.Connection) -> Dict[str, Any]:
    from topos.features.stats.definitions import row_event_ts

    written = 0
    for table, batch in _canonical_batches(conn, batch_size=2000):
        for row in batch:
            ts = row_event_ts(row)
            record_id = str(
                row.get("record_id") or row.get("message_id") or row.get("event_id") or ""
            )
            if ts is None or not record_id:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO timeline (
                    event_at, record_id, source_id, canonical_table, record_type
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (ts.isoformat(), record_id, row.get("source_id"), table, row.get("record_type")),
            )
            written += 1
    conn.commit()
    return {"rows": written}


def step_clusters(conn: sqlite3.Connection) -> Dict[str, Any]:
    from topos.features.signal.topic_clustering import (
        recompute_topic_clusters,
        write_top_topics_signal_facts,
    )
    from topos.storage.adapters.factory import AdapterFactory

    t0 = time.time()
    result = recompute_topic_clusters(conn, min_records=3)
    facts = 0
    if result.get("status") == "completed":
        bundle = AdapterFactory.create("local_database", conn=conn)
        facts = write_top_topics_signal_facts(bundle, conn)
    return {
        "status": result.get("status"),
        "clusters_written": result.get("clusters_written"),
        "ids_preserved": result.get("ids_preserved"),
        "records_loaded": result.get("records_loaded"),
        "top_topics_facts": facts,
        "seconds": round(time.time() - t0, 1),
    }


def step_reembed(conn: sqlite3.Connection) -> Dict[str, Any]:
    from topos.engine.backends.huggingface import HuggingFaceAdapter
    from topos.enrichment.job_writer import write_signal_records
    from topos.storage.adapters.factory import AdapterFactory

    rows = dict_rows(
        conn,
        """
        SELECT embedding_id, record_id, source_id, signal_dimension, text_preview,
               search_text, event_at, conversation_id, record_type, content_hash,
               chunk_index, provenance_json
        FROM signal_embeddings WHERE model = ? AND vector_blob IS NOT NULL
        """,
        (MINILM_MODEL,),
    )
    adapter = HuggingFaceAdapter()
    bundle = AdapterFactory.create("local_database", conn=conn)
    t0 = time.time()
    written = 0
    failures = 0
    for start in range(0, len(rows), 64):
        batch = rows[start : start + 64]
        texts = [str(r.get("search_text") or r.get("text_preview") or "") for r in batch]
        out = adapter.run_inference(
            {"texts": texts, "input_role": "passage"},
            {"subtype": "embedding", "model": BGE_MODEL},
        )
        vectors = out.get("vectors") or []
        if len(vectors) != len(batch):
            failures += len(batch)
            continue
        records = []
        for row, vector in zip(batch, vectors):
            records.append(
                {
                    "record_id": row["record_id"],
                    "message_id": row["record_id"],
                    "source_id": row["source_id"],
                    "signal_dimension": row["signal_dimension"],
                    "text_preview": row["text_preview"],
                    "search_text": row["search_text"],
                    "event_at": row["event_at"],
                    "conversation_id": row["conversation_id"],
                    "record_type": row["record_type"],
                    "content_hash": row["content_hash"],
                    "chunk_index": row["chunk_index"] or 0,
                    "vector": vector,
                    "dims": len(vector),
                    "model": BGE_MODEL,
                    "provider": "huggingface",
                    "normalized": True,
                }
            )
        written += write_signal_records(
            "embeddings",
            records,
            adapters=bundle,
            provenance={"job_id": "nightly_reembed", "model": BGE_MODEL},
            conn=conn,
        )
    seconds = time.time() - t0
    return {
        "source_rows": len(rows),
        "written": written,
        "failures": failures,
        "seconds": round(seconds, 1),
        "rows_per_sec": round(written / seconds, 1) if seconds > 0 else None,
    }


_REENRICH_TABLES = (
    # (table, id_column) — content-bearing canonical tables
    ("conversation_messages", "message_id"),
    ("ai_chat_messages", "message_id"),
    ("journal_entries", "entry_id"),
)
_REENRICH_EMBED_BATCH = 64
_REENRICH_NER_BATCH = 32
_REENRICH_MIN_CONTENT = 6


def _reenrich_candidates(conn: sqlite3.Connection, table: str, id_column: str, *, missing_from: str) -> List[Dict[str, Any]]:
    """Rows with content that lack coverage in the given derived table."""
    other = (
        "SELECT DISTINCT record_id FROM signal_embeddings"
        if missing_from == "embeddings"
        else "SELECT DISTINCT record_id FROM message_entities"
    )
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT * FROM {table}
            WHERE content IS NOT NULL AND LENGTH(content) > {_REENRICH_MIN_CONTENT}
              AND {id_column} NOT IN ({other})
            """
        ).fetchall()
    ]
    conn.row_factory = None
    from topos.features.signal.embed_context import is_derivable_content

    rows = [r for r in rows if is_derivable_content(str(r.get("content") or ""))]
    for row in rows:
        row["_table"] = table
        row["message_id"] = row.get(id_column)
        row["record_id"] = row.get(id_column)
    return rows


def step_reenrich(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Backfill embeddings + NER coverage for canonical rows the old pipeline skipped.

    Embeddings go through the production EmbeddingsJob (chunking, context
    headers, passage prefixes, write_signal_records incl. ANN sync) so the
    result is byte-identical to what live incremental enrichment produces.
    NER runs batched and lands in message_entities; the entities step
    afterwards resolves everything into the spine.
    """
    import asyncio

    from topos.engine.backends.huggingface import HuggingFaceAdapter
    from topos.enrichment.job_writer import write_signal_records
    from topos.enrichment.jobs.canonical.embeddings_job import EmbeddingsJob
    from topos.features.entities.resolver import is_valid_entity_surface
    from topos.storage.adapters.factory import AdapterFactory

    report: Dict[str, Any] = {}
    bundle = AdapterFactory.create("local_database", conn=conn)

    # ---- embeddings ----
    t0 = time.time()
    job = EmbeddingsJob()
    embedded_records = 0
    for table, id_column in _REENRICH_TABLES:
        candidates = _reenrich_candidates(conn, table, id_column, missing_from="embeddings")
        log("reenrich", f"{table}: {len(candidates)} rows to embed")
        for start in range(0, len(candidates), 500):
            chunk = candidates[start : start + 500]
            records = asyncio.run(job.enrich(chunk))
            if records:
                write_signal_records(
                    "embeddings",
                    records,
                    adapters=bundle,
                    provenance={"job_id": "reenrich_backfill"},
                    conn=conn,
                )
            embedded_records += len(chunk)
            if start and start % 2000 == 0:
                log("reenrich", f"{table}: embedded {start}/{len(candidates)}")
    report["embed_rows"] = embedded_records
    report["embed_seconds"] = round(time.time() - t0, 1)

    # ---- NER -> message_entities ----
    t0 = time.time()
    adapter = HuggingFaceAdapter()
    ner_rows = 0
    mentions_written = 0
    for table, id_column in _REENRICH_TABLES:
        candidates = _reenrich_candidates(conn, table, id_column, missing_from="entities")
        log("reenrich", f"{table}: {len(candidates)} rows for NER")
        for start in range(0, len(candidates), _REENRICH_NER_BATCH):
            batch = candidates[start : start + _REENRICH_NER_BATCH]
            out = adapter.run_inference(
                {"items": [
                    {"id": str(r["record_id"]), "text": str(r.get("content") or "")[:2000]}
                    for r in batch
                ]},
                {"subtype": "entity_extraction_batch"},
            )
            by_id = {str(r["record_id"]): r for r in batch}
            for item in out.get("items") or []:
                src_row = by_id.get(str(item.get("id")))
                if src_row is None:
                    continue
                for ent in item.get("entities") or []:
                    surface = str(ent.get("entity_text") or "").strip()
                    if not is_valid_entity_surface(surface):
                        continue
                    payload = {
                        "record_id": src_row["record_id"],
                        "entity_text": surface,
                        "entity_type": ent.get("entity_type"),
                        "confidence": ent.get("confidence"),
                        "event_at": src_row.get("event_at") or src_row.get("entry_at"),
                        "canonical_table": table,
                    }
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO message_entities (
                            entity_id, record_id, source_id, entity_text, model, provider, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"ent_{src_row['record_id']}_{abs(hash(surface)) % 10**10}",
                            src_row["record_id"],
                            src_row.get("source_id"),
                            surface,
                            out.get("model"),
                            "huggingface",
                            json.dumps(payload),
                        ),
                    )
                    mentions_written += 1
            ner_rows += len(batch)
            conn.commit()
            if start and start % 1600 == 0:
                log("reenrich", f"{table}: NER {start}/{len(candidates)} ({mentions_written} mentions so far)")
    report["ner_rows"] = ner_rows
    report["ner_mentions_written"] = mentions_written
    report["ner_seconds"] = round(time.time() - t0, 1)
    return report


def step_cleanjunk(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Remove derived artifacts built from serialization-garbage content.

    Some parsers leak undecoded rich-text payloads (iMessage attributedBody ->
    'streamtyped' NSKeyedArchiver blobs) into canonical content. Those rows
    stay canonical (the event happened; timeline/stats keep them) but their
    embeddings and NER mentions are junk and are removed here; entities are
    recounted, orphans dropped, edges rebuilt, dossiers refreshed.
    """
    from topos.features.lifecycle.derived_scrub import (
        _delete_orphan_entities,
        _rebuild_entity_edges,
        _recount_entity_mentions,
    )
    from topos.features.signal.embed_context import is_derivable_content
    from topos.storage.adapters.sqlite.vector_search import delete_vec_rows

    junk_ids: List[str] = []
    for table, id_column in _REENRICH_TABLES:
        try:
            rows = conn.execute(
                f"SELECT {id_column}, content FROM {table} WHERE content IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        junk_ids.extend(str(r[0]) for r in rows if not is_derivable_content(str(r[1] or "")))

    report: Dict[str, Any] = {"junk_records": len(junk_ids)}
    embeddings_removed = 0
    mentions_removed = 0
    for start in range(0, len(junk_ids), 200):
        chunk = junk_ids[start : start + 200]
        placeholders = ",".join("?" for _ in chunk)
        emb_ids = [
            str(r[0])
            for r in conn.execute(
                f"SELECT embedding_id FROM signal_embeddings WHERE record_id IN ({placeholders})",
                chunk,
            ).fetchall()
        ]
        if emb_ids:
            conn.execute(
                f"DELETE FROM signal_embeddings WHERE record_id IN ({placeholders})", chunk
            )
            delete_vec_rows(conn, emb_ids)
            embeddings_removed += len(emb_ids)
        cursor = conn.execute(
            f"DELETE FROM entity_mentions WHERE record_id IN ({placeholders})", chunk
        )
        mentions_removed += int(cursor.rowcount or 0)
    conn.commit()

    report["embeddings_removed"] = embeddings_removed
    report["mentions_removed"] = mentions_removed
    report["entities_recounted"] = _recount_entity_mentions(conn)
    report["entities_removed"] = len(_delete_orphan_entities(conn))
    report["edges_rebuilt"] = _rebuild_entity_edges(conn)
    conn.commit()
    try:
        from topos.features.entities.dossier import refresh_dossiers

        report["dossiers_refreshed"] = refresh_dossiers(conn)
    except Exception as exc:  # noqa: BLE001
        report["dossiers_refreshed"] = f"failed: {exc}"
    return report


def step_statsreset(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Clear folded stat state so the next ``stats`` step refolds all history.

    Needed once after changing stat definitions in a way that alters bucketing
    (e.g. enabling d30/d90 windows: rows folded before the change live only in
    the all-time bucket and are dedup-skipped by stat_seen, so daily buckets
    would otherwise cover new data only). Derived + rebuildable: safe.
    """
    counts = {}
    for table in ("stat_state", "stat_seen"):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            counts[table] = 0
    conn.commit()
    return {"cleared": counts}


def step_gc(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Derived-layer garbage collection (stale objects, brief compaction,
    audit retention, junk embeddings). See topos.features.lifecycle.gc."""
    from topos.features.lifecycle.gc import run_gc

    return run_gc(conn)


def _probe_queries(conn: sqlite3.Connection) -> List[str]:
    probes = list(GENERIC_PROBES)
    for (name,) in conn.execute(
        "SELECT canonical_name FROM entities WHERE entity_type='person' AND is_self=0"
        " ORDER BY mention_count DESC LIMIT 5"
    ).fetchall():
        probes.append(f"what do I know about {name}")
    for (label,) in conn.execute(
        "SELECT label FROM topic_clusters ORDER BY member_count DESC LIMIT 4"
    ).fetchall():
        probes.append(str(label).replace(" / ", " "))
    return probes


def step_compare(conn: sqlite3.Connection) -> Dict[str, Any]:
    from topos.engine.backends.huggingface import HuggingFaceAdapter, apply_embedding_prefix
    from topos.storage.adapters.sqlite.vector_search import search_similar_brute_force

    adapter = HuggingFaceAdapter()
    probes = _probe_queries(conn)
    per_probe: List[Dict[str, Any]] = []
    overlaps: List[float] = []
    sims: Dict[str, List[float]] = {MINILM_MODEL: [], BGE_MODEL: []}
    latencies: Dict[str, List[float]] = {MINILM_MODEL: [], BGE_MODEL: []}

    for probe in probes:
        results: Dict[str, List[str]] = {}
        for model in (MINILM_MODEL, BGE_MODEL):
            text = apply_embedding_prefix([probe], model_name=model, input_role="query")[0]
            out = adapter.run_inference(
                {"texts": [text]}, {"subtype": "embedding", "model": model}
            )
            vector = (out.get("vectors") or [[]])[0]
            if not vector:
                results[model] = []
                continue
            t0 = time.time()
            hits, _total = search_similar_brute_force(
                conn, [float(x) for x in vector], model=model, limit=10
            )
            latencies[model].append(time.time() - t0)
            results[model] = [str(h.get("record_id")) for h in hits[:10]]
            top3 = [float(h.get("similarity") or 0.0) for h in hits[:3]]
            if top3:
                sims[model].append(sum(top3) / len(top3))
        a, b = set(results[MINILM_MODEL]), set(results[BGE_MODEL])
        jaccard = len(a & b) / len(a | b) if (a | b) else 0.0
        overlaps.append(jaccard)
        per_probe.append(
            {
                "probe": probe[:60],
                "top10_jaccard": round(jaccard, 3),
                "minilm_hits": len(results[MINILM_MODEL]),
                "bge_hits": len(results[BGE_MODEL]),
            }
        )

    def _mean(values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 4) if values else None

    return {
        "probes": len(probes),
        "mean_top10_jaccard": _mean(overlaps),
        "mean_top3_similarity": {m: _mean(v) for m, v in sims.items()},
        "mean_search_latency_ms": {
            m: round(1000 * sum(v) / len(v), 1) if v else None for m, v in latencies.items()
        },
        "per_probe": per_probe,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=str(Path.home() / ".topos" / "database.db"))
    parser.add_argument("--workdir", default=str(Path.home() / ".topos" / "nightly"))
    parser.add_argument(
        "--steps",
        default="copy,migrate,entities,stats,facts,timeline,clusters,reembed,compare",
    )
    parser.add_argument("--tag", default="", help="suffix for report/copy filenames")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if args.tag:
        stamp = f"{stamp}_{args.tag}"
    workdir = Path(args.workdir)
    copy_path = workdir / f"nightly_{stamp}.db"
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    # Without the copy step, operate directly on the source DB (used to
    # promote a validated backfill to the live database while the node is
    # stopped). With it, all work happens on the snapshot.
    target_db = copy_path if "copy" in steps else Path(args.source_db)
    report: Dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_db": args.source_db,
        "target_db": str(target_db),
        "steps": {},
    }

    if "copy" in steps:
        log("copy", f"backing up {args.source_db}")
        report["steps"]["copy"] = step_copy(Path(args.source_db), copy_path)
        log("copy", str(report["steps"]["copy"]))

    conn = sqlite3.connect(str(target_db))
    step_fns = {
        "migrate": step_migrate,
        "reenrich": step_reenrich,
        "cleanjunk": step_cleanjunk,
        "gc": step_gc,
        "statsreset": step_statsreset,
        "entities": step_entities,
        "stats": step_stats,
        "facts": step_facts,
        "timeline": step_timeline,
        "clusters": step_clusters,
        "reembed": step_reembed,
        "compare": step_compare,
    }
    for name in steps:
        fn = step_fns.get(name)
        if fn is None:
            continue
        log(name, "starting")
        t0 = time.time()
        try:
            result = fn(conn)
            result.setdefault("seconds", round(time.time() - t0, 1))
            report["steps"][name] = result
            log(name, json.dumps(result)[:300])
        except Exception as exc:  # keep the nightly going; report the failure
            report["steps"][name] = {"error": f"{type(exc).__name__}: {exc}"}
            log(name, f"FAILED: {exc}")
    conn.close()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report_path = workdir / f"nightly_report_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2))
    log("done", f"report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
