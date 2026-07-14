from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._engine_runner import run_engine_task
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.entities")

_BATCH_SIZE = 32
_MIN_RESOLVE_CONFIDENCE = 0.60


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

        from ....features.signal.embed_context import is_derivable_content

        eligible: List[Dict[str, Any]] = []
        for msg in canonical_messages:
            message_id = msg.get("message_id") or msg.get("id")
            content = msg.get("content", "")
            if message_id and content and is_derivable_content(str(content)):
                eligible.append(
                    {
                        "id": str(message_id),
                        "text": str(content),
                        "source_id": msg.get("source_id"),
                        "event_at": msg.get("event_at") or msg.get("ts") or msg.get("created_at"),
                        "canonical_table": msg.get("_table") or msg.get("canonical_table"),
                    }
                )

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
                self._resolve_into_spine(results, canonical_messages)
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
            EDGE_COMMUNICATES,
            update_edge,
        )
        from ....features.entities.resolver import EntityResolver, map_ner_type

        conn = get_db_connection()
        if conn is None:
            return
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()

        msg_by_id = {
            str(m.get("message_id") or m.get("id") or ""): m for m in canonical_messages
        }
        entities_by_record: Dict[str, List[str]] = {}

        for rec in ner_records:
            confidence = float(rec.get("confidence") or 0.0)
            surface = str(rec.get("entity_text") or "").strip()
            if not surface or confidence < _MIN_RESOLVE_CONFIDENCE:
                continue
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
            resolver.record_mention(
                entity_id,
                record_id=record_id,
                surface_text=surface,
                source_id=rec.get("source_id"),
                canonical_table=rec.get("canonical_table"),
                confidence=confidence,
                event_at=rec.get("event_at"),
            )
            entities_by_record.setdefault(record_id, []).append(entity_id)

            # Sender speaks about the mentioned entity -> communicates/discusses signal
            sender = str(msg.get("sender_id") or "").strip()
            if sender:
                try:
                    sender_id, _ = resolver.resolve(sender, entity_type="person")
                    update_edge(
                        conn,
                        src_entity_id=sender_id,
                        dst_entity_id=entity_id,
                        edge_type=EDGE_COMMUNICATES,
                        event_at=rec.get("event_at"),
                    )
                except ValueError:
                    pass

        # Co-occurrence within the same record
        for record_id, ids in entities_by_record.items():
            unique = list(dict.fromkeys(ids))
            event_at = (msg_by_id.get(record_id) or {}).get("event_at")
            for i in range(len(unique)):
                for j in range(i + 1, len(unique)):
                    update_edge(
                        conn,
                        src_entity_id=unique[i],
                        dst_entity_id=unique[j],
                        edge_type=EDGE_CO_OCCURRENCE,
                        event_at=event_at,
                    )
        conn.commit()
        refresh_dossiers(conn)
