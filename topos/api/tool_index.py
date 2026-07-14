"""Tool-RAG endpoints: sync the tool index, retrieve top-K tools for a query.

PLAN_AGENT_QUALITY WS-3 (M2). The chat orchestration (topos-react-app) pushes
the gateway's tool list here after ``tools/list`` (hash-deduped upsert → cheap
no-op when unchanged) and calls ``/v1/tools/retrieve`` per turn to build a
focused tool doc sized to the model instead of a flat 52-tool dump.

Handlers are sync ``def`` on purpose: the embedding path is blocking HF
inference, and FastAPI runs sync handlers in its threadpool.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_api_key
from ..core.state import get_db_connection
from ..features.signal.tool_index import index_status, index_tools, retrieve_tools
from ..rate_limit import rate_limit

router = APIRouter()


class ToolSpecIn(BaseModel):
    name: str
    description: str = ""
    connector_id: Optional[str] = None


class IndexToolsRequest(BaseModel):
    tools: List[ToolSpecIn] = Field(default_factory=list, max_length=2000)
    # Prune index rows absent from this payload (full-list syncs only).
    prune_missing: bool = False


class RetrieveToolsRequest(BaseModel):
    query: str
    connector_scope: Optional[str] = None
    k: int = 8


def _require_conn():
    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return conn


@router.post(
    "/v1/tools/index",
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
def tools_index(body: IndexToolsRequest):
    conn = _require_conn()
    specs = [spec.model_dump() for spec in body.tools if str(spec.name or "").strip()]
    if not specs and not body.prune_missing:
        raise HTTPException(status_code=422, detail="tools must be a non-empty list")
    return index_tools(conn, specs, prune_missing=body.prune_missing)


@router.post(
    "/v1/tools/retrieve",
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
def tools_retrieve(body: RetrieveToolsRequest):
    if not str(body.query or "").strip():
        raise HTTPException(status_code=422, detail="query is required")
    conn = _require_conn()
    return retrieve_tools(
        conn,
        body.query,
        connector_scope=body.connector_scope,
        k=body.k,
    )


@router.get(
    "/v1/tools/index/status",
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
def tools_index_status():
    conn = _require_conn()
    return index_status(conn)
