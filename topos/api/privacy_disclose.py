"""Platform Privacy Layer: batch PII disclosure via Topos Engine."""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_api_key
from ..sanitization.privacy_filter import PRIVACY_DISCLOSE_MAX_BATCH, redact_privacy_batch
from ..sanitization.nsfw_classifier import NSFW_CLASSIFY_MAX_BATCH, classify_nsfw_batch

logger = logging.getLogger("topos.api.privacy_disclose")

router = APIRouter(tags=["privacy"])


class PrivacyDiscloseItem(BaseModel):
    id: str = ""
    text: str = ""
    transform_id: Optional[str] = None


class PrivacyDiscloseRequest(BaseModel):
    items: List[PrivacyDiscloseItem] = Field(default_factory=list)
    transform_id: str = "pii_redaction"


@router.post("/v1/privacy/disclose", dependencies=[Depends(require_api_key)])
async def post_privacy_disclose(body: PrivacyDiscloseRequest = Body(...)) -> dict[str, Any]:
    if not body.items:
        raise HTTPException(status_code=400, detail="items required")
    if len(body.items) > PRIVACY_DISCLOSE_MAX_BATCH:
        raise HTTPException(
            status_code=413,
            detail=f"batch exceeds limit of {PRIVACY_DISCLOSE_MAX_BATCH}",
        )
    payload_items = [
        {
            "id": item.id,
            "text": item.text,
            "transform_id": item.transform_id or body.transform_id,
        }
        for item in body.items
    ]
    try:
        result = redact_privacy_batch(payload_items, transform_id=body.transform_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post_privacy_disclose failed: %s", exc)
        raise HTTPException(status_code=500, detail="privacy disclose failed") from exc
    status = str(result.get("status") or "ok")
    if status == "unavailable":
        raise HTTPException(status_code=503, detail=str(result.get("error") or "privacy filter unavailable"))
    if status == "too_large":
        raise HTTPException(status_code=413, detail=str(result.get("error") or "batch too large"))
    return {"status": "ok", **result}


class NsfwClassifyRequest(BaseModel):
    items: List[PrivacyDiscloseItem] = Field(default_factory=list)


@router.post("/v1/privacy/nsfw-classify", dependencies=[Depends(require_api_key)])
async def post_nsfw_classify(body: NsfwClassifyRequest = Body(...)) -> dict[str, Any]:
    if not body.items:
        raise HTTPException(status_code=400, detail="items required")
    if len(body.items) > NSFW_CLASSIFY_MAX_BATCH:
        raise HTTPException(
            status_code=413,
            detail=f"batch exceeds limit of {NSFW_CLASSIFY_MAX_BATCH}",
        )
    payload_items = [{"id": item.id, "text": item.text} for item in body.items]
    try:
        result = classify_nsfw_batch(payload_items)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post_nsfw_classify failed: %s", exc)
        raise HTTPException(status_code=500, detail="nsfw classify failed") from exc
    status = str(result.get("status") or "ok")
    if status == "disabled":
        return {"status": "ok", **result}
    if status == "too_large":
        raise HTTPException(status_code=413, detail=str(result.get("error") or "batch too large"))
    return {"status": "ok", **result}
