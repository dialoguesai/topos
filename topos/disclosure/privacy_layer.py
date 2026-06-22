"""Platform Privacy Layer client and ingest hook (Database → Engine → Database)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from .canonical_writer import DISCLOSURE_MODEL_SETTING, upsert_disclosure_fields, upsert_nsfw_fields
from .field_registry import (
    CANONICAL_ID_COLUMN,
    canonical_table_for_message,
    disclosure_column,
    disclosure_hash_column,
    fields_for_table,
)
from ..sanitization.privacy_filter import PRIVACY_LAYER_VERSION

logger = logging.getLogger("topos.disclosure.privacy_layer")


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PrivacyLayerClient:
    """Calls Topos Engine for batch PII redaction (in-process or remote HTTP)."""

    def __init__(
        self,
        *,
        engine: Any = None,
        engine_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self._engine = engine
        self._engine_url = (engine_url or "").strip().rstrip("/") or None
        self._api_key = api_key

    @classmethod
    def from_settings(cls, engine: Any = None) -> "PrivacyLayerClient":
        from ..config.settings import settings

        return cls(
            engine=engine,
            engine_url=getattr(settings, "topos_engine_service_url", None),
            api_key=getattr(settings, "topos_key", None),
        )

    async def redact_batch(
        self,
        items: List[Dict[str, Any]],
        *,
        transform_id: str = "pii_redaction",
    ) -> Dict[str, Any]:
        if not items:
            return {"items": [], "model": DISCLOSURE_MODEL_SETTING, "privacy_layer_version": PRIVACY_LAYER_VERSION}
        if self._engine_url:
            return await self._redact_via_http(items, transform_id=transform_id)
        return await self._redact_via_engine(items, transform_id=transform_id)

    async def _redact_via_engine(
        self,
        items: List[Dict[str, Any]],
        *,
        transform_id: str,
    ) -> Dict[str, Any]:
        from ..engine import Engine
        from ..enrichment.jobs.canonical._engine_runner import run_engine_task

        engine = self._engine or Engine()
        record_ids = [str(i.get("id") or "") for i in items]
        result = await run_engine_task(
            engine,
            task_id=f"privacy_{record_ids[0] or 'batch'}",
            subtype="privacy_disclosure",
            source_id=None,
            record_ids=record_ids,
            input_payload={"items": items, "transform_id": transform_id},
            provider="huggingface",
            model=DISCLOSURE_MODEL_SETTING,
        )
        raw = getattr(result, "output", None) or {}
        if isinstance(raw, dict):
            return raw
        return {"items": [], "error": "invalid engine response", "status": "failed"}

    async def _redact_via_http(
        self,
        items: List[Dict[str, Any]],
        *,
        transform_id: str,
    ) -> Dict[str, Any]:
        url = f"{self._engine_url}/v1/privacy/disclose"
        headers: Dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {"items": items, "transform_id": transform_id}
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(url, json=body, headers=headers)
                if resp.status_code == 503:
                    return {"status": "unavailable", "error": resp.text, "items": []}
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and "items" in data:
                    return data
                return {"items": data.get("items", []), **{k: v for k, v in data.items() if k != "items"}}
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(0.25)
        logger.warning("privacy HTTP redact failed: %s", last_exc)
        return {"status": "failed", "error": str(last_exc), "items": []}

    async def classify_nsfw_batch(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not items:
            return {"items": [], "model": "", "status": "ok"}
        if self._engine_url:
            return await self._classify_nsfw_via_http(items)
        return await self._classify_nsfw_via_engine(items)

    async def _classify_nsfw_via_engine(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        from ..engine import Engine
        from ..enrichment.jobs.canonical._engine_runner import run_engine_task
        from ..config.settings import settings

        engine = self._engine or Engine()
        model = getattr(settings, "nsfw_classifier_model", "michellejieli/NSFW_text_classifier")
        record_ids = [str(i.get("id") or "") for i in items]
        result = await run_engine_task(
            engine,
            task_id=f"nsfw_{record_ids[0] or 'batch'}",
            subtype="content_nsfw_classification",
            source_id=None,
            record_ids=record_ids,
            input_payload={"items": items},
            provider="huggingface",
            model=model,
        )
        raw = getattr(result, "output", None) or {}
        return raw if isinstance(raw, dict) else {"items": [], "status": "failed"}

    async def _classify_nsfw_via_http(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        url = f"{self._engine_url}/v1/privacy/nsfw-classify"
        headers: Dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(url, json={"items": items}, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, dict) else {"items": []}
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(0.25)
        logger.warning("privacy HTTP nsfw classify failed: %s", last_exc)
        return {"status": "failed", "error": str(last_exc), "items": []}


async def run_privacy_disclosure_layer(
    conn,
    canonical_messages: List[Dict[str, Any]],
    *,
    source_group: Optional[str] = None,
    client: Optional[PrivacyLayerClient] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    nsfw_only: bool = False,
) -> Dict[str, Any]:
    """Mandatory post-canonical Platform Privacy Layer: PII disclosure + NSFW tags."""
    from ..config.settings import settings
    from ..storage.db.migrations.canonical_nsfw_v1 import apply_canonical_nsfw_v1_up

    apply_canonical_nsfw_v1_up(conn)

    if not getattr(settings, "platform_privacy_via_engine", True):
        logger.info("[PIPELINE:PRIVACY] skipped: platform_privacy_via_engine=false")
        return {"records_updated": 0, "skipped": True}

    if nsfw_only and not getattr(settings, "nsfw_classifier_enabled", True):
        logger.info("[PIPELINE:PRIVACY] nsfw-only backfill skipped: nsfw_classifier_enabled=false")
        return {"records_updated": 0, "nsfw_tagged": 0, "skipped": True}

    if not canonical_messages:
        return {"records_updated": 0, "nsfw_tagged": 0}

    privacy_client = client or PrivacyLayerClient.from_settings()
    started = time.perf_counter()
    updated = 0
    failed_batches = 0
    total = len(canonical_messages)

    if not nsfw_only:
        # Group pending redactions by batch
        pending_by_table: Dict[str, List[Dict[str, Any]]] = {}
        for idx, msg in enumerate(canonical_messages):
            table = canonical_table_for_message(msg, source_group=source_group)
            if not table:
                if progress_callback:
                    progress_callback(idx + 1, total)
                continue
            record_id = (
                msg.get("message_id")
                or msg.get("entry_id")
                or msg.get("record_id")
                or msg.get("event_id")
            )
            if not record_id:
                if progress_callback:
                    progress_callback(idx + 1, total)
                continue
            record_id = str(record_id)
            for field in fields_for_table(table):
                raw = msg.get(field)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                existing_hash = msg.get(disclosure_hash_column(field))
                if existing_hash == _content_hash(raw):
                    redacted = msg.get(disclosure_column(field))
                    if isinstance(redacted, str) and redacted.strip():
                        msg[field] = redacted
                    continue
                key = f"{table}:{record_id}:{field}"
                pending_by_table.setdefault(table, []).append(
                    {
                        "msg": msg,
                        "record_id": record_id,
                        "field": field,
                        "raw": raw,
                        "batch_key": key,
                    }
                )

        # Flatten and batch call Engine
        flat_pending: List[Dict[str, Any]] = []
        for table, entries in pending_by_table.items():
            for entry in entries:
                flat_pending.append({**entry, "table": table})

        from ..sanitization.privacy_filter import PRIVACY_DISCLOSE_MAX_BATCH

        for i in range(0, len(flat_pending), PRIVACY_DISCLOSE_MAX_BATCH):
            batch = flat_pending[i : i + PRIVACY_DISCLOSE_MAX_BATCH]
            items = [{"id": e["batch_key"], "text": e["raw"]} for e in batch]
            result = await privacy_client.redact_batch(items)
            if result.get("status") in ("unavailable", "failed"):
                failed_batches += 1
                logger.warning(
                    "[PIPELINE:PRIVACY] batch failed status=%s error=%s size=%d",
                    result.get("status"),
                    result.get("error"),
                    len(batch),
                )
                continue
            by_id = {str(it.get("id")): it for it in (result.get("items") or [])}
            model_id = str(result.get("model") or DISCLOSURE_MODEL_SETTING)
            for entry in batch:
                item = by_id.get(entry["batch_key"]) or {}
                redacted = item.get("text")
                if not isinstance(redacted, str):
                    continue
                msg = entry["msg"]
                field = entry["field"]
                table = entry["table"]
                patches = {
                    disclosure_column(field): redacted,
                    disclosure_hash_column(field): _content_hash(entry["raw"]),
                }
                msg[disclosure_column(field)] = redacted
                msg[disclosure_hash_column(field)] = patches[disclosure_hash_column(field)]
                msg[field] = redacted
                if upsert_disclosure_fields(
                    conn,
                    table,
                    entry["record_id"],
                    patches,
                    model_id=model_id,
                ):
                    updated += 1

    nsfw_tagged = 0
    nsfw_failed_batches = 0

    # NSFW classification on raw primary text (tag only — no sanitization)
    if getattr(settings, "nsfw_classifier_enabled", True):
        nsfw_pending: Dict[str, Dict[str, Any]] = {}
        for msg in canonical_messages:
            table = canonical_table_for_message(msg, source_group=source_group)
            if not table:
                continue
            record_id = (
                msg.get("message_id")
                or msg.get("entry_id")
                or msg.get("record_id")
                or msg.get("event_id")
            )
            if not record_id:
                continue
            record_id = str(record_id)
            primary_field = fields_for_table(table)[0] if fields_for_table(table) else "content"
            raw = msg.get(primary_field)
            if not isinstance(raw, str) or not raw.strip():
                continue
            nsfw_pending[f"{table}:{record_id}"] = {
                "msg": msg,
                "table": table,
                "record_id": record_id,
                "text": raw,
            }

        from ..sanitization.nsfw_classifier import NSFW_CLASSIFY_MAX_BATCH

        nsfw_items = list(nsfw_pending.items())
        for i in range(0, len(nsfw_items), NSFW_CLASSIFY_MAX_BATCH):
            batch = nsfw_items[i : i + NSFW_CLASSIFY_MAX_BATCH]
            items = [{"id": key, "text": entry["text"]} for key, entry in batch]
            nsfw_result = await privacy_client.classify_nsfw_batch(items)
            if nsfw_result.get("status") in ("failed",):
                nsfw_failed_batches += 1
                continue
            by_id = {str(it.get("id")): it for it in (nsfw_result.get("items") or [])}
            model_id = str(nsfw_result.get("model") or "")
            for key, entry in batch:
                item = by_id.get(key) or {}
                is_nsfw = bool(item.get("nsfw"))
                score = float(item.get("score") or 0.0)
                msg = entry["msg"]
                msg["content_nsfw"] = 1 if is_nsfw else 0
                msg["content_nsfw_score"] = score
                if model_id:
                    msg["content_nsfw_model"] = model_id
                if upsert_nsfw_fields(
                    conn,
                    entry["table"],
                    entry["record_id"],
                    is_nsfw=is_nsfw,
                    score=score,
                    model_id=model_id or None,
                ):
                    nsfw_tagged += 1

    if updated or nsfw_tagged:
        conn.commit()

    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "[PIPELINE:PRIVACY] platform_privacy_layer disclosure_updated=%d nsfw_tagged=%d "
        "failed_batches=%d nsfw_failed_batches=%d duration_ms=%d version=%s",
        updated,
        nsfw_tagged,
        failed_batches,
        nsfw_failed_batches,
        duration_ms,
        PRIVACY_LAYER_VERSION,
    )
    return {
        "records_updated": updated,
        "nsfw_tagged": nsfw_tagged,
        "failed_batches": failed_batches,
        "nsfw_failed_batches": nsfw_failed_batches,
        "privacy_layer_version": PRIVACY_LAYER_VERSION,
        "duration_ms": duration_ms,
    }
