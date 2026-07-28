"""Owner-set conversation context tag (work | personal).

Second labeling axis alongside source posture: posture says whose words the
content is; context_tag says which life domain the thread belongs to. Set at
the conversation level (the thread in `conversations`); every message in the
thread inherits it at read/derivation time. Owner-only; never inferred here.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..core.state import get_db_connection
from ..storage.db.migrations.conversation_context_v1 import CONTEXT_TAG_VALUES

router = APIRouter()


def _conversation_row(conn, conversation_id: str, dataset_id: str):
    return conn.execute(
        "SELECT conversation_id, dataset_id, context_tag, context_tag_source "
        "FROM conversations WHERE conversation_id=? AND dataset_id=?",
        (conversation_id, dataset_id),
    ).fetchone()


@router.get(
    "/conversations/{conversation_id}/context",
    dependencies=[Depends(require_api_key)],
)
async def get_conversation_context(conversation_id: str, dataset_id: str) -> dict:
    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database not available")
    row = _conversation_row(conn, conversation_id, dataset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    return {
        "status": "ok",
        "conversation_id": row[0],
        "dataset_id": row[1],
        "context_tag": row[2],
        "context_tag_source": row[3],
    }


@router.put(
    "/conversations/{conversation_id}/context",
    dependencies=[Depends(require_api_key)],
)
async def set_conversation_context(
    conversation_id: str,
    payload: dict = Body(default_factory=dict),
) -> dict:
    dataset_id = str(payload.get("dataset_id") or "").strip()
    if not dataset_id:
        raise HTTPException(status_code=422, detail="dataset_id is required")
    raw_tag = payload.get("context_tag")
    tag = str(raw_tag).strip().lower() if raw_tag is not None else None
    if tag is not None and tag not in CONTEXT_TAG_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"context_tag must be one of {sorted(CONTEXT_TAG_VALUES)} or null",
        )
    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database not available")
    if _conversation_row(conn, conversation_id, dataset_id) is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    # Owner decisions always win: stamped 'owner' even when clearing, so the
    # auto-classifier never refills a conversation the owner chose to untag.
    conn.execute(
        "UPDATE conversations SET context_tag=?, context_tag_source='owner', "
        "updated_at=datetime('now') WHERE conversation_id=? AND dataset_id=?",
        (tag, conversation_id, dataset_id),
    )
    conn.commit()
    return {
        "status": "ok",
        "conversation_id": conversation_id,
        "dataset_id": dataset_id,
        "context_tag": tag,
        "context_tag_source": "owner",
    }


# --- classifier model config (settings surface) -----------------------------------


@router.get("/conversation-context-llm-config", dependencies=[Depends(require_api_key)])
async def get_context_llm_config() -> dict:
    from ..config.conversation_context_llm import effective_config_for_api
    from ..config.settings import settings

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return {"status": "ok", **effective_config_for_api(settings, conn)}


@router.put("/conversation-context-llm-config", dependencies=[Depends(require_api_key)])
async def put_context_llm_config(payload: dict = Body(default=None)) -> dict:
    from ..config.conversation_context_llm import (
        ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_MODEL,
        effective_config_for_api,
        normalize_put_model,
    )
    from ..config.settings import settings
    from ..core.state import set_engine_config_value

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        model = normalize_put_model(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_MODEL, model)
    return {"status": "ok", **effective_config_for_api(settings, conn)}
