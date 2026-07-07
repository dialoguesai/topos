"""Enrichment Lab REST API (engine HTTP surface)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..auth import require_api_key
from ..enrichment_lab import bundles as bundles_mod
from ..enrichment_lab import model_resolve
from ..enrichment_lab import service as lab_service
from ..enrichment_lab import store

logger = logging.getLogger("topos.api.enrichment_lab")

router = APIRouter(tags=["enrichment-lab"])


@router.get("/v1/enrichment-lab/bundles", dependencies=[Depends(require_api_key)])
async def enrichment_lab_list_bundles() -> List[Dict[str, Any]]:
    return bundles_mod.list_bundle_metadata()


@router.get("/v1/enrichment-lab/bundles/{bundle_id}", dependencies=[Depends(require_api_key)])
async def enrichment_lab_get_bundle(bundle_id: str) -> Dict[str, Any]:
    data = bundles_mod.get_bundle_preview(bundle_id)
    if not data:
        raise HTTPException(status_code=404, detail="Bundle not found")
    return data


@router.get("/v1/enrichment-lab/models/resolve", dependencies=[Depends(require_api_key)])
async def enrichment_lab_resolve_model(model_id: str = Query(...)) -> Dict[str, Any]:
    """Resolve a pasted HuggingFace model id against the hub (task, size, fit).

    Always 200: outcome is in the payload's ``status`` field ("ok",
    "not_found", "unauthorized", "invalid", "unreachable") so the UI can
    render each case instead of handling transport errors.
    """
    return await asyncio.to_thread(model_resolve.resolve_model, model_id)


@router.get("/v1/enrichment-lab/node-sample", dependencies=[Depends(require_api_key)])
async def enrichment_lab_node_sample(
    source_id: str = Query(...),
    limit: int = Query(lab_service.DEFAULT_NODE_SAMPLE, ge=1, le=lab_service.MAX_NODE_SAMPLE),
) -> Dict[str, Any]:
    """Read-only preview of real node records available as lab input."""
    try:
        records = lab_service.sample_node_records(source_id, limit=limit)
        return {"status": "ok", "source_id": source_id, "records": records}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/v1/enrichment-lab/job-groups", dependencies=[Depends(require_api_key)])
async def enrichment_lab_create_job_group(
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        gid = lab_service.create_job_group(
            job_id=str(body.get("job_id") or "").strip(),
            models=list(body.get("models") or []),
            dataset_kind=str(body.get("dataset_kind") or "bundle").strip(),
            bundle_id=body.get("bundle_id"),
            source_id=body.get("source_id"),
            sample_limit=body.get("sample_limit"),
            options=body.get("options") if isinstance(body.get("options"), dict) else None,
        )
        return lab_service.serialize_job_group(conn, gid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/v1/enrichment-lab/job-groups", dependencies=[Depends(require_api_key)])
async def enrichment_lab_list_job_groups(
    job_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    store.prune_old_groups(conn, max_age_days=30)
    rows = store.list_groups(conn, job_id=(job_id or "").strip() or None, limit=limit, offset=offset)
    groups = []
    for row in rows:
        g = dict(row)
        g["models"] = json.loads(g.pop("models_json") or "[]")
        opt_raw = g.pop("options_json", "{}")
        try:
            g["options"] = json.loads(opt_raw) if isinstance(opt_raw, str) else {}
        except json.JSONDecodeError:
            g["options"] = {}
        groups.append(g)
    lab_service.enrich_job_groups_list_with_run_summaries(conn, groups)
    return {"groups": groups, "limit": limit, "offset": offset}


@router.get("/v1/enrichment-lab/job-groups/{group_id}", dependencies=[Depends(require_api_key)])
async def enrichment_lab_get_job_group(group_id: str) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        return lab_service.serialize_job_group(conn, group_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job group not found") from None


@router.patch("/v1/enrichment-lab/job-groups/{group_id}", dependencies=[Depends(require_api_key)])
async def enrichment_lab_patch_job_group(
    group_id: str, body: Dict[str, Any] = Body(default_factory=dict)
) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    if not store.get_group(conn, group_id):
        raise HTTPException(status_code=404, detail="Job group not found")
    if "preferred_model_tag" in body:
        p = body.get("preferred_model_tag")
        store.patch_group(conn, group_id, preferred_model_tag=p if p is None else str(p).strip() or None)
    if "group_notes" in body:
        store.patch_group(conn, group_id, group_notes=body.get("group_notes"))
    if "notes" in body:
        store.patch_group(conn, group_id, notes=body.get("notes"))
    return lab_service.serialize_job_group(conn, group_id)


@router.patch(
    "/v1/enrichment-lab/job-groups/{group_id}/runs/{run_id}",
    dependencies=[Depends(require_api_key)],
)
async def enrichment_lab_patch_run(
    group_id: str, run_id: str, body: Dict[str, Any] = Body(default_factory=dict)
) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    runs = store.list_runs(conn, group_id)
    if not any(dict(r)["id"] == run_id for r in runs):
        raise HTTPException(status_code=404, detail="Run not found")
    if "user_quality_score_0_10" in body:
        v = body.get("user_quality_score_0_10")
        if v is not None and (not isinstance(v, int) or v < 0 or v > 10):
            raise HTTPException(status_code=400, detail="user_quality_score_0_10 must be 0–10 or null")
        store.patch_run(conn, run_id, user_quality_score_0_10=v)
    if "user_liked" in body:
        v = body.get("user_liked")
        if v is None:
            conn.execute("UPDATE enrichment_lab_run SET user_liked = NULL WHERE id = ?", (run_id,))
            conn.commit()
        else:
            store.patch_run(conn, run_id, user_liked=bool(v))
    if "user_note" in body:
        note = body.get("user_note")
        store.patch_run(conn, run_id, user_note=None if note is None else str(note)[:2000])
    return lab_service.serialize_job_group(conn, group_id)


@router.post(
    "/v1/enrichment-lab/job-groups/{group_id}/apply-preferred",
    dependencies=[Depends(require_api_key)],
)
async def enrichment_lab_apply_preferred(group_id: str) -> Dict[str, Any]:
    try:
        return lab_service.apply_preferred_model(group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete("/v1/enrichment-lab/job-groups/{group_id}", dependencies=[Depends(require_api_key)])
async def enrichment_lab_delete_job_group(group_id: str) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    if not store.get_group(conn, group_id):
        raise HTTPException(status_code=404, detail="Job group not found")
    store.delete_group(conn, group_id)
    return {"status": "ok", "group_id": group_id, "deleted": True}
