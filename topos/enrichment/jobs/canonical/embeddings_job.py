from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._engine_runner import run_engine_task
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.embeddings")


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
        batch_texts: List[str] = []
        batch_meta: List[Dict[str, Any]] = []
        results: List[Dict[str, Any]] = []
        batch_size = 32

        def flush_batch() -> None:
            nonlocal batch_texts, batch_meta
            if not batch_texts:
                return
            # handled in async loop below

        total = len(canonical_messages)
        processed = 0
        for msg in canonical_messages:
            message_id = msg.get("message_id") or msg.get("id")
            content = msg.get("content", "")
            if not message_id or not content:
                processed += 1
                continue
            batch_texts.append(str(content))
            batch_meta.append(
                {
                    "message_id": message_id,
                    "record_id": message_id,
                    "source_id": msg.get("source_id"),
                    "text_preview": str(content)[:200],
                    "signal_dimension": "memory",
                }
            )
            if len(batch_texts) >= batch_size:
                results.extend(await self._embed_batch(batch_texts, batch_meta))
                batch_texts, batch_meta = [], []
            processed += 1
            if progress_callback and processed % 10 == 0:
                progress_callback(processed, total)

        if batch_texts:
            results.extend(await self._embed_batch(batch_texts, batch_meta))
        if progress_callback:
            progress_callback(total, total)
        return results

    async def _embed_batch(self, texts: List[str], meta: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = await run_engine_task(
            self._engine,
            task_id=f"embeddings_batch_{meta[0]['message_id']}",
            subtype="embedding",
            source_id=meta[0].get("source_id"),
            record_ids=[str(m["message_id"]) for m in meta],
            input_payload={"texts": texts, "batch_size": 32},
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
                }
            )
        return out
