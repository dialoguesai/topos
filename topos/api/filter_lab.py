"""Filter Lab REST API (engine HTTP surface)."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..auth import require_api_key
from ..filter_lab import bundles as bundles_mod
from ..filter_lab import store
from ..filter_lab import service as lab_service
from ..storage.db.write_gate import batched_writes, commit_connection, with_db_write

logger = logging.getLogger("topos.api.filter_lab")

router = APIRouter(tags=["filter-lab"])


@router.get("/v1/filter-lab/bundles", dependencies=[Depends(require_api_key)])
async def filter_lab_list_bundles() -> List[Dict[str, Any]]:
    return bundles_mod.list_bundle_metadata()


@router.get("/v1/filter-lab/bundles/{bundle_id}", dependencies=[Depends(require_api_key)])
async def filter_lab_get_bundle(bundle_id: str) -> Dict[str, Any]:
    data = bundles_mod.get_bundle_preview(bundle_id)
    if not data:
        raise HTTPException(status_code=404, detail="Bundle not found")
    return data


@router.post("/v1/filter-lab/job-groups", dependencies=[Depends(require_api_key)])
async def filter_lab_create_job_group(body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        gid = lab_service.create_job_group(
            filter_id=str(body.get("filter_id") or "").strip(),
            bundle_id=str(body.get("bundle_id") or "").strip(),
            models=list(body.get("models") or []),
            options=body.get("options") if isinstance(body.get("options"), dict) else None,
        )
        return lab_service.serialize_job_group(conn, gid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/v1/filter-lab/job-groups", dependencies=[Depends(require_api_key)])
async def filter_lab_list_job_groups(
    filter_id: Optional[str] = Query(
        None,
        description="Omit to list all transforms; set to restrict history to one filter id.",
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    store.prune_old_groups(conn, max_age_days=30)
    fid = (filter_id or "").strip()
    if fid:
        rows = store.list_groups_for_filter(conn, fid, limit=limit, offset=offset)
    else:
        rows = store.list_all_job_groups(conn, limit=limit, offset=offset)
    groups = []
    for row in rows:
        g = dict(row)
        g["baseline_models"] = json.loads(g.pop("baseline_models_json") or "[]")
        g["pulled_models"] = json.loads(g.pop("pulled_models_json") or "[]")
        opt_raw = g.pop("options_json", "{}")
        try:
            g["options"] = json.loads(opt_raw) if isinstance(opt_raw, str) else {}
        except json.JSONDecodeError:
            g["options"] = {}
        groups.append(g)
    lab_service.enrich_job_groups_list_with_run_summaries(conn, groups)
    return {"groups": groups, "limit": limit, "offset": offset}


@router.get("/v1/filter-lab/job-groups/{group_id}", dependencies=[Depends(require_api_key)])
async def filter_lab_get_job_group(group_id: str) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        return lab_service.serialize_job_group(conn, group_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job group not found") from None


@router.patch("/v1/filter-lab/job-groups/{group_id}", dependencies=[Depends(require_api_key)])
async def filter_lab_patch_job_group(group_id: str, body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
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


@router.patch("/v1/filter-lab/job-groups/{group_id}/runs/{run_id}", dependencies=[Depends(require_api_key)])
async def filter_lab_patch_run(group_id: str, run_id: str, body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    runs = store.list_runs(conn, group_id)
    if not any(dict(r)["id"] == run_id for r in runs):
        raise HTTPException(status_code=404, detail="Run not found")
    rated = False
    if "user_quality_score_0_10" in body:
        v = body.get("user_quality_score_0_10")
        if v is not None and (not isinstance(v, int) or v < 0 or v > 10):
            raise HTTPException(status_code=400, detail="user_quality_score_0_10 must be 0–10 or null")
        store.patch_run(conn, run_id, user_quality_score_0_10=v)
        rated = True
    if "user_liked" in body:
        v = body.get("user_liked")
        if v is None:
            with with_db_write():
                conn.execute("UPDATE filter_lab_run SET user_liked = NULL WHERE id = ?", (run_id,))
                commit_connection(conn)
        else:
            store.patch_run(conn, run_id, user_liked=bool(v))
        rated = True
    if "user_note" in body:
        note = body.get("user_note")
        store.patch_run(conn, run_id, user_note=None if note is None else str(note)[:2000])
        rated = True
    if rated:
        with with_db_write():
            conn.execute(
                "UPDATE filter_lab_run SET rated_at = ? WHERE id = ?",
                (store.utc_now_iso(), run_id),
            )
            commit_connection(conn)
    return lab_service.serialize_job_group(conn, group_id)


@router.post("/v1/filter-lab/job-groups/{group_id}/apply-preferred", dependencies=[Depends(require_api_key)])
async def filter_lab_apply_preferred(group_id: str) -> Dict[str, Any]:
    try:
        return lab_service.apply_preferred_model(group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete("/v1/filter-lab/job-groups/{group_id}", dependencies=[Depends(require_api_key)])
async def filter_lab_delete_job_group(group_id: str) -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    if not store.get_group(conn, group_id):
        raise HTTPException(status_code=404, detail="Job group not found")
    store.delete_group(conn, group_id)
    return {"status": "ok", "group_id": group_id, "deleted": True}


@router.delete("/v1/filter-lab/data", dependencies=[Depends(require_api_key)])
async def filter_lab_clear_all() -> Dict[str, Any]:
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    store.ensure_schema(conn)
    with batched_writes(conn):
        conn.execute("DELETE FROM filter_lab_model_event")
        conn.execute("DELETE FROM filter_lab_run")
        conn.execute("DELETE FROM filter_lab_job_group")
    return {"status": "ok", "cleared": True}
