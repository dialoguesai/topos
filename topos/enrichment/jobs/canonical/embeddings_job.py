from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._engine_runner import run_engine_task
from ....engine import Engine
from ....features.signal.chunking import DEFAULT_STRATEGY, chunk_text
from ....features.signal.embed_context import (
    build_embed_text,
    dimension_for_record,
    embeddable_content,
)
from ....features.signal.vector_settings import vector_chunking_enabled

logger = logging.getLogger("topos.enrichment.jobs.embeddings")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _event_at(msg: Dict[str, Any]) -> Optional[str]:
    for field in ("event_at", "ts", "entry_at", "occurred_at", "starts_at", "created_at"):
        value = msg.get(field)
        if value:
            return str(value)
    return None


def _record_type(msg: Dict[str, Any]) -> Optional[str]:
    explicit = msg.get("record_type")
    if explicit:
        return str(explicit)
    table = str(msg.get("_table") or msg.get("canonical_table") or "")
    mapping = {
        "ai_chat_messages": "ai_chat_message",
        "activity_events": "activity_event",
        "journal_entries": "journal_entry",
        "conversation_messages": "conversation_message",
        "profile_records": "profile_record",
        "calendar_events": "calendar_event",
    }
    if table in mapping:
        return mapping[table]
    # Reloaded activity batches (load_canonical_records_for_signal) carry no
    # table stamp; activity_type is the marker that survives the round-trip.
    if msg.get("activity_type"):
        return "activity_event"
    return None


class EmbeddingsJob(BaseEnrichmentJob):
    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

    def get_derived_table(self) -> str:
        return "message_embeddings"

    def get_job_name(self) -> str:
        return "embeddings"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        pending_texts: List[str] = []
        pending_meta: List[Dict[str, Any]] = []
        results: List[Dict[str, Any]] = []
        batch_size = 32

        excluded: set = set()
        try:
            from ....core.state import get_db_connection
            from ....features.lifecycle.exclusions import excluded_record_ids

            conn = get_db_connection()
            if conn is not None:
                excluded = excluded_record_ids(conn)
        except Exception:  # noqa: BLE001 — exclusion check must never block embedding
            excluded = set()

        total = len(canonical_messages)
        processed = 0
        # Activity rows (browser visits above all) repeat the same title/url
        # many times per batch; one vector per distinct text is the policy —
        # revisits must not multiply near-identical neighbors in the index.
        # Message-family rows are never deduped: each message needs its own
        # vector row or it is unreachable through retrieval.
        seen_activity_hashes: set = set()
        for msg in canonical_messages:
            message_id = msg.get("message_id") or msg.get("id")
            record_id = msg.get("event_id") or msg.get("record_id") or message_id
            content = embeddable_content(msg)
            if not record_id or not content or str(record_id) in excluded:
                processed += 1
                continue

            record_type = _record_type(msg)
            parent_hash = _content_hash(str(content))
            if record_type == "activity_event":
                if parent_hash in seen_activity_hashes:
                    processed += 1
                    continue
                seen_activity_hashes.add(parent_hash)
            specs = (
                chunk_text(str(content))
                if vector_chunking_enabled()
                else chunk_text(str(content), max_tokens=10_000)
            )
            for spec in specs:
                pending_texts.append(
                    build_embed_text(msg, spec.text, record_type=record_type)
                )
                pending_meta.append(
                    {
                        "message_id": record_id,
                        "record_id": record_id,
                        "source_id": msg.get("source_id"),
                        "text_preview": spec.text[:200],
                        "search_text": spec.text[:2000],
                        "signal_dimension": dimension_for_record(msg, record_type=record_type),
                        "chunk_index": spec.chunk_index,
                        "content_hash": parent_hash,
                        "event_at": _event_at(msg),
                        "conversation_id": msg.get("conversation_id") or msg.get("thread_id"),
                        "record_type": record_type,
                        "chunk_strategy": DEFAULT_STRATEGY,
                        "chunk_count": len(specs),
                    }
                )
                if len(pending_texts) >= batch_size:
                    results.extend(await self._embed_batch(pending_texts, pending_meta))
                    pending_texts, pending_meta = [], []

            processed += 1
            if progress_callback and processed % 10 == 0:
                progress_callback(processed, total)

        if pending_texts:
            results.extend(await self._embed_batch(pending_texts, pending_meta))
        if progress_callback:
            progress_callback(total, total)
        return results

    async def _embed_batch(self, texts: List[str], meta: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = await run_engine_task(
            self._engine,
            task_id=f"embeddings_batch_{meta[0]['message_id']}_{meta[0].get('chunk_index', 0)}",
            subtype="embedding",
            source_id=meta[0].get("source_id"),
            record_ids=[str(m["message_id"]) for m in meta],
            input_payload={"texts": texts, "batch_size": 32, "input_role": "passage"},
        )
        if result.status != "completed":
            return []
        vectors = result.output.get("vectors") or []
        out: List[Dict[str, Any]] = []
        for row_meta, vector in zip(meta, vectors):
            out.append(
                {
                    **row_meta,
                    "vector": vector,
                    "dims": result.output.get("dims"),
                    "provider": result.output.get("provider", "huggingface"),
                    "model": result.output.get("model"),
                    "normalized": result.output.get("normalized", True),
                }
            )
        return out
