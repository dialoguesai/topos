from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._engine_runner import run_engine_task
from ....engine import Engine
from ....features.entities.mention_lineage import (
    MIN_RESOLVE_CONFIDENCE,
    canonical_table_for_record,
)
from ....storage.db.write_gate import batched_writes

logger = logging.getLogger("topos.enrichment.jobs.entities")

_BATCH_SIZE = 32
#: One floor for the live writer and the lineage backfill (mention_lineage).
_MIN_RESOLVE_CONFIDENCE = MIN_RESOLVE_CONFIDENCE

# Canonical groups disagree on the id/time column (messages: message_id/event_at,
# activity events: event_id/occurred_at, journal entries: entry_id/entry_at, …).
# One key contract here, or whole sources silently skip extraction — the
# 2026-07-14 backfill lost browser_visits/grow_* exactly this way.
_RECORD_ID_FIELDS = (
    "message_id",
    "segment_id",
    "id",
    "record_id",
    "event_id",
    "entry_id",
    "transaction_id",
)
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
                # The lineage stamp. `_table`, then the record's own
                # `canonical_table`, then its record kind — never an id's shape.
                # A record that names no table is refused at write time.
                "canonical_table": canonical_table_for_record(msg),
            }
        )
    return out


def partition_by_lineage(
    ner_records: List[Dict[str, Any]],
    canonical_messages: List[Dict[str, Any]],
) -> tuple:
    """Split extracted mentions into (linkable, refused) by canonical table.

    A mention is linkable when the NER record, or the canonical message it was
    extracted from, names a canonical table; the resolved stamp is written
    back onto the record so the ``message_entities`` payload and the spine
    row carry the same one. Everything else is refused — from BOTH tables.
    A ``message_entities`` row without a spine link is the defect this
    exists to close (11,637 such rows on one node), and a spine link that
    names no table is the other (17,203). Refusing is loud (the caller logs
    the count) and cheap (the record is not marked processed by coverage, so
    the next backfill retries it once the lane stamps it).
    """
    msg_by_id = {
        record_key(m): m
        for m in canonical_messages
        if isinstance(m, dict) and record_key(m)
    }
    linkable: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    for rec in ner_records:
        if not isinstance(rec, dict):
            continue
        record_id = str(rec.get("record_id") or rec.get("message_id") or "")
        table = canonical_table_for_record(rec) or canonical_table_for_record(
            msg_by_id.get(record_id)
        )
        if not table:
            refused.append(rec)
            continue
        rec["canonical_table"] = table
        linkable.append(rec)
    return linkable, refused


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

        # The spine link is NOT written here. It is written by write_derived,
        # in the same transaction as the message_entities rows — enrich()
        # used to resolve into the spine on its own connection, under its own
        # commit, inside a try/except that logged and moved on, which is how
        # 11,637 conversation rows ended up extracted and never linked.
        if progress_callback:
            progress_callback(total, total)
        return results

    #: How many extracted mentions the last write_derived refused because no
    #: canonical table could be attributed to them. Diagnostic; tests read it.
    last_lineage_refusals: int = 0

    def write_derived(
        self,
        records: List[Dict[str, Any]],
        canonical_messages: List[Dict[str, Any]],
        *,
        tables_manager: Any,
    ) -> int:
        """Persist ``message_entities`` AND the spine link as one transaction.

        The orchestrator lanes call this instead of the generic derived-table
        write. Both halves land under one ``batched_writes`` hold on the
        manager's connection: an exception anywhere in the spine pass rolls the
        NER rows back with it and propagates, so a record is either extracted
        and linked or neither. Extracted mentions that name no canonical table
        are refused from both tables and counted (``last_lineage_refusals``).

        Managers without a connection (test fakes) fall through to the plain
        derived-table write: there is no spine to link against.
        """
        conn = getattr(tables_manager, "conn", None)
        write_rows = getattr(tables_manager, "write_message_entities_rows", None)
        if conn is None or write_rows is None:
            return int(
                tables_manager.write_enrichment_batch(records, self.get_derived_table()) or 0
            )
        linkable, refused = partition_by_lineage(records, canonical_messages)
        self.last_lineage_refusals = len(refused)
        if refused:
            sources = sorted({str(r.get("source_id") or "?") for r in refused})
            logger.warning(
                "entities: refused %d of %d extracted mentions — no canonical table could "
                "be attributed to their records (sources=%s); neither message_entities "
                "nor entity_mentions was written for them",
                len(refused),
                len(records),
                ",".join(sources),
            )
        if not linkable:
            return 0
        spine = entity_spine_enabled()
        # One hold, one commit, one rollback. write_message_entities_rows is
        # the gate-free row writer precisely so this block owns the
        # transaction; write_enrichment_batch would open its own and commit
        # the NER rows before the spine pass had run.
        with batched_writes(conn):
            written = int(write_rows(linkable) or 0)
            if spine:
                self._resolve_into_spine(linkable, canonical_messages, conn=conn)
        if spine:
            # Gates itself; must stay outside the hold above.
            from ....features.entities.dossier import refresh_dossiers

            refresh_dossiers(conn)
        return written

    def _resolve_into_spine(
        self,
        ner_records: List[Dict[str, Any]],
        canonical_messages: List[Dict[str, Any]],
        *,
        conn: Any,
    ) -> None:
        """Resolve NER output into the entity registry; update mentions + edges.

        Runs inside the caller's ``batched_writes`` hold on ``conn`` — it opens
        no transaction of its own and commits nothing. Every record it links
        has already been stamped with its canonical table by
        :func:`partition_by_lineage`; ``record_mention`` refuses anything else.
        """
        from ....features.entities.edges import (
            EDGE_CO_OCCURRENCE,
            update_edge,
        )
        from ....features.entities.resolver import EntityResolver, map_ner_type

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
        # SQLite's write lock at execute time); the caller holds the gate for
        # this pass and the message_entities rows together.
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
            table = canonical_table_for_record(rec) or canonical_table_for_record(msg)
            authored_flag = None
            if msg and table:
                from ....storage.db.migrations.entity_mentions_authored_v1 import (
                    authored_flag_for_row,
                )

                authored_flag = authored_flag_for_row(msg, table=table)
            # `canonical_table` is passed through as resolved; record_mention
            # raises MentionLineageError on an empty one, and that failure
            # is the whole transaction's — nothing half-written survives it.
            resolver.record_mention(
                entity_id,
                record_id=record_id,
                # A declared row may resolve on one string and be evidenced by
                # another: a cited host is the node, the full URL is the proof.
                surface_text=str(rec.get("surface_detail") or surface),
                source_id=rec.get("source_id"),
                canonical_table=table,
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
            # Tolerant lookup, like every other read of a record's time in
            # this file. `msg_by_id` holds RAW canonical messages, and the
            # canonical groups disagree on the column (see
            # _EVENT_AT_FIELDS) — reading only "event_at" left every
            # co-occurrence edge undated for any group that names it
            # otherwise. Measured on a real import: 9,242 of 9,242
            # co-occurrence edges had no time, while the declared lane
            # beside them, which already used this lookup, was fully dated.
            # Undated edges cannot be placed in a temporal view at all.
            msg_for_event = msg_by_id.get(record_id) or {}
            event_at = next(
                (msg_for_event.get(f) for f in _EVENT_AT_FIELDS if msg_for_event.get(f)),
                None,
            )
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
