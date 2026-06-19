"""Shared URL classification logic for canonical activity records."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ._batch_limits import MAX_JOB_MESSAGES, URL_CLASSIFICATION_BATCH_SIZE
from ._engine_runner import run_engine_task
from ....engine import Engine


async def classify_canonical_url_records(
    records: List[Dict[str, Any]],
    *,
    engine: Optional[Engine] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> List[Dict[str, Any]]:
    """Classify URLs from canonical activity payloads (event_id, url, title, source_id)."""
    engine = engine or Engine()
    messages = records[:MAX_JOB_MESSAGES]
    if len(records) > MAX_JOB_MESSAGES:
        messages = records[:MAX_JOB_MESSAGES]

    results: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    for msg in messages:
        url = msg.get("url") or ""
        title = msg.get("title") or ""
        record_id = msg.get("event_id") or msg.get("record_id") or msg.get("id")
        source_id = msg.get("source_id")
        category = msg.get("category")
        confidence = msg.get("confidence")
        if category:
            results.append(
                {
                    "record_id": record_id,
                    "event_id": record_id,
                    "source_id": source_id,
                    "url": url,
                    "title": title,
                    "category": category,
                    "confidence": confidence,
                    "provider": msg.get("provider", "huggingface"),
                    "model": msg.get("model"),
                }
            )
        elif isinstance(url, str) and url.strip():
            pending.append(
                {
                    "record_id": record_id,
                    "event_id": record_id,
                    "source_id": source_id,
                    "url": url,
                    "title": title,
                }
            )

    for start in range(0, len(pending), URL_CLASSIFICATION_BATCH_SIZE):
        batch = pending[start : start + URL_CLASSIFICATION_BATCH_SIZE]
        if not batch:
            continue
        result = await run_engine_task(
            engine,
            task_id=f"url_cls_batch_{start}",
            subtype="url_classification_batch",
            source_id=batch[0].get("source_id"),
            record_ids=[str(item["record_id"]) for item in batch if item.get("record_id")],
            input_payload={"items": batch},
        )
        if result.status == "completed":
            for item, row in zip(batch, result.output.get("items") or []):
                results.append(
                    {
                        "record_id": item.get("record_id"),
                        "event_id": item.get("record_id"),
                        "source_id": item.get("source_id"),
                        "url": item.get("url"),
                        "title": item.get("title"),
                        "category": row.get("category"),
                        "confidence": row.get("confidence"),
                        "provider": "huggingface",
                        "model": row.get("model") or result.output.get("model"),
                    }
                )

    if progress_callback:
        progress_callback(len(messages), len(messages))
    return results


def merge_url_classification_into_records(
    canonical_records: List[Dict[str, Any]],
    classified: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach category/confidence to canonical records so signal derive can skip re-classification."""
    by_id = {
        str(row.get("record_id") or row.get("event_id")): row
        for row in classified
        if row.get("record_id") or row.get("event_id")
    }
    merged: List[Dict[str, Any]] = []
    for rec in canonical_records:
        updated = dict(rec)
        key = str(rec.get("event_id") or rec.get("record_id") or "")
        hit = by_id.get(key)
        if hit:
            updated["category"] = hit.get("category")
            updated["confidence"] = hit.get("confidence")
            updated["model"] = hit.get("model")
            updated["provider"] = hit.get("provider")
        merged.append(updated)
    return merged
