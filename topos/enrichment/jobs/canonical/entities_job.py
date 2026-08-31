from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._engine_runner import run_engine_task
from ....engine import Engine
from ....storage.db.write_gate import batched_writes

logger = logging.getLogger("topos.enrichment.jobs.entities")

_BATCH_SIZE = 32
_MIN_RESOLVE_CONFIDENCE = 0.60

# Canonical groups disagree on the id/time column (messages: message_id/event_at,
# activity events: event_id/occurred_at, journal entries: entry_id/entry_at, …).
# One key contract here, or whole sources silently skip extraction — the
# 2026-07-14 backfill lost browser_visits/grow_* exactly this way.
_RECORD_ID_FIELDS = ("message_id", "id", "record_id", "event_id", "entry_id", "transaction_id")
_EVENT_AT_FIELDS = ("event_at", "ts", "occurred_at", "entry_at", "starts_at", "created_at")


def record_key(msg: Dict[str, Any]) -> str:
    for field in _RECORD_ID_FIELDS:
        value = msg.get(field)
        if value:
            return str(value)
    return ""


def eligible_ner_records(canonical_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows with extractable text for NER.

    Same content contract as the embeddings job (embeddable_content): `content`
    when derivable, else the descriptive-field fallback (title/organization/
    description/url/place_name/…) — activity and profile records have no
    content column at all.
    """
    from ....features.signal.embed_context import embeddable_content

    out: List[Dict[str, Any]] = []
    for msg in canonical_messages:
        rid = record_key(msg)
        if not rid:
            continue
        text = embeddable_content(msg)
        if not text:
            continue
        event_at = next((msg.get(f) for f in _EVENT_AT_FIELDS if msg.get(f)), None)
        out.append(
            {
                "id": rid,
                "text": text,
                "source_id": msg.get("source_id"),
                "event_at": event_at,
                "canonical_table": msg.get("_table") or msg.get("canonical_table"),
            }
        )
    return out


def entity_spine_enabled() -> bool:
    return os.environ.get("TOPOS_ENTITY_SPINE", "on").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


class EntitiesJob(BaseEnrichmentJob):
    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

    def get_derived_table(self) -> str:
        return "message_entities"

    def get_job_name(self) -> str:
        return "entities"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        total = len(canonical_messages)

        # Declared mappings first (§5a cap 4): structured sources mint their
        # entities from record fields; NER is suppressed for them — it guesses
        # (and misclassified repos as people) where the record already knows.
        from ....features.entities.declared_mappings import (
            extract_declared_entities,
            ner_suppressed_source_ids,
        )

        suppressed = ner_suppressed_source_ids()
        for msg in canonical_messages:
            results.extend(
                extract_declared_entities(
                    msg,
                    record_id=record_key(msg),
                    event_at=next(
                        (msg.get(f) for f in _EVENT_AT_FIELDS if msg.get(f)), None
                    ),
                )
            )

        eligible = [
            r
            for r in eligible_ner_records(canonical_messages)
            if str(r.get("source_id") or "") not in suppressed
        ]

        processed = total - len(eligible)
        if progress_callback and processed:
            progress_callback(processed, total)

        for start in range(0, len(eligible), _BATCH_SIZE):
            batch = eligible[start : start + _BATCH_SIZE]
            result = await run_engine_task(
                self._engine,
                task_id=f"entities_batch_{batch[0]['id']}",
                subtype="entity_extraction_batch",
                source_id=batch[0].get("source_id"),
                record_ids=[item["id"] for item in batch],
                input_payload={"items": [{"id": item["id"], "text": item["text"]} for item in batch]},
            )
            if result.status == "completed":
                from ....features.entities.resolver import is_valid_entity_surface

                by_id = {str(item["id"]): item for item in batch}
                for out_item in result.output.get("items") or []:
                    src = by_id.get(str(out_item.get("id")))
                    if src is None:
                        continue
                    for ent in out_item.get("entities") or []:
                        # Drop wordpiece fragments ('##dy') and other NER
                        # artifacts before they reach storage or the registry.
                        if not is_valid_entity_surface(ent.get("entity_text")):
                            continue
                        results.append(
                            {
                                "message_id": src["id"],
                                "record_id": src["id"],
                                "source_id": src.get("source_id"),
                                "event_at": src.get("event_at"),
                                "canonical_table": src.get("canonical_table"),
                                "entity_text": ent.get("entity_text"),
                                "entity_type": ent.get("entity_type"),
                                "confidence": ent.get("confidence"),
                                "provider": result.output.get("provider", "huggingface"),
                                "model": result.output.get("model"),
                            }
                        )
            else:
                logger.debug(
                    "entities batch %s status=%s; skipping %d messages",
                    batch[0]["id"],
                    result.status,
                    len(batch),
                )
            processed += len(batch)
            if progress_callback:
                progress_callback(min(processed, total), total)

        if entity_spine_enabled() and results:
            try:
                # Spine resolution upserts entities, mentions, edges and
                # dossiers under the write gate — a blocking OS lock that on
                # the event loop stalls every coroutine, including the
                # control-plane keepalive. It resolves its own connection, so
                # on a worker thread it binds that thread's handle.
                await asyncio.to_thread(
                    self._resolve_into_spine, results, canonical_messages
                )
            except Exception as exc:
                logger.warning("entity spine resolution failed: %s", exc)

        if progress_callback:
            progress_callback(total, total)
        return results

    def _resolve_into_spine(
        self,
        ner_records: List[Dict[str, Any]],
        canonical_messages: List[Dict[str, Any]],
    ) -> None:
        """Resolve NER output into the entity registry; update mentions + edges."""
        from ....core.state import get_db_connection
        from ....features.entities.dossier import refresh_dossiers
        from ....features.entities.edges import (
            EDGE_CO_OCCURRENCE,
            update_edge,
        )
        from ....features.entities.resolver import EntityResolver, map_ner_type

        conn = get_db_connection()
        if conn is None:
            return
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()

        def self_entity_id() -> Optional[str]:
            try:
                row = conn.execute(
                    "SELECT entity_id FROM entities WHERE is_self=1"
                    " ORDER BY (SELECT COUNT(*) FROM signal_objects o"
                    "   WHERE o.object_type='fact' AND o.object_key LIKE"
                    "   'fact:' || entities.entity_id || ':%') DESC, entity_id ASC LIMIT 1"
                ).fetchone()
            except Exception:
                return None
            return str(row[0]) if row else None

        msg_by_id = {record_key(m): m for m in canonical_messages if record_key(m)}
        entities_by_record: Dict[str, List[str]] = {}

        # Resolution mints entities/mentions/edges as it goes (writes take
        # SQLite's write lock at execute time) — the whole pass holds the gate
        # with a single commit at exit. refresh_dossiers gates itself and must
        # stay outside.
        with batched_writes(conn):
            for rec in ner_records:
                declared = rec.get("provider") == "declared"
                confidence = float(rec.get("confidence") or 0.0)
                surface = str(rec.get("entity_text") or "").strip()
                if not surface or confidence < _MIN_RESOLVE_CONFIDENCE:
                    continue
                if declared:
                    # Declared types are already spine types (project/organization);
                    # map_ner_type only understands NER label vocabularies.
                    entity_type = str(rec.get("entity_type") or "").strip() or None
                else:
                    entity_type = map_ner_type(rec.get("entity_type"))
                if entity_type is None:
                    # Value labels (dates, money, cardinals) — not spine entities.
                    continue
                record_id = str(rec.get("record_id") or "")
                msg = msg_by_id.get(record_id, {})
                try:
                    entity_id, _tier = resolver.resolve(
                        surface,
                        entity_type=entity_type,
                        record_id=record_id,
                    )
                except ValueError:
                    continue
                authored_flag = None
                if msg:
                    from ....storage.db.migrations.entity_mentions_authored_v1 import (
                        authored_flag_for_row,
                    )

                    table = str(
                        rec.get("canonical_table")
                        or msg.get("_table")
                        or msg.get("canonical_table")
                        or ""
                    )
                    authored_flag = authored_flag_for_row(msg, table=table)
                resolver.record_mention(
                    entity_id,
                    record_id=record_id,
                    # A declared row may resolve on one string and be evidenced by
                    # another: a cited host is the node, the full URL is the proof.
                    surface_text=str(rec.get("surface_detail") or surface),
                    source_id=rec.get("source_id"),
                    canonical_table=rec.get("canonical_table"),
                    confidence=confidence,
                    event_at=rec.get("event_at"),
                    authored_by_owner=authored_flag,
                )
                entities_by_record.setdefault(record_id, []).append(entity_id)

                # Declared owner edge: self -> worked_on -> entity, positioned at
                # the record's event time so temporal views place it correctly.
                edge_type = str(rec.get("self_edge") or "").strip()
                if declared and edge_type:
                    owner = self_entity_id()
                    if owner:
                        update_edge(
                            conn,
                            src_entity_id=owner,
                            dst_entity_id=entity_id,
                            edge_type=edge_type,
                            event_at=rec.get("event_at"),
                        )

                # P3.2: do NOT write communicates_with for sender→NER-mention.
                # Mention-only third parties (IMB7 Odile) are not talked-to partners;
                # co-participation is folded below from conversation senders.

            # Mentions from DECLARED structured columns (a journal entry's
            # place_name). Folded into entities_by_record BEFORE co-occurrence, so
            # the person named in the prose and the place named in the column land
            # in one bucket instead of two — and so the record that carries the
            # evidence is the one a black hole blocks on.
            try:
                from ....features.entities.structured_fields import (
                    record_structured_mentions,
                )

                for record_id, ids in record_structured_mentions(
                    conn, resolver, canonical_messages
                ).items():
                    entities_by_record.setdefault(record_id, []).extend(ids)
            except Exception as exc:  # noqa: BLE001
                logger.warning("structured-field mentions skipped: %s", exc)

            # Co-occurrence within the same record, through the SHARED fold —
            # see edges.record_cooccurrence_pairs for why there is only one.
            from ....features.entities.edges import record_cooccurrence_pairs

            for record_id, ids in entities_by_record.items():
                event_at = (msg_by_id.get(record_id) or {}).get("event_at")
                for src, dst in record_cooccurrence_pairs(ids):
                    update_edge(
                        conn,
                        src_entity_id=src,
                        dst_entity_id=dst,
                        edge_type=EDGE_CO_OCCURRENCE,
                        event_at=event_at,
                    )

            # Thread co-participation → communicates_with (talked-to vs mentioned).
            conv_ids = {
                str(m.get("conversation_id") or m.get("chat_id") or "").strip()
                for m in msg_by_id.values()
            }
            conv_ids.discard("")
            if conv_ids:
                from ....features.entities.maintenance import fold_communicates_with_edges

                fold_communicates_with_edges(conn, conversation_ids=conv_ids)

        refresh_dossiers(conn)
