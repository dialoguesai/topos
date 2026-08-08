"""Enrichment Lab message handlers."""
from __future__ import annotations

import asyncio

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    json,
)
from .registry import handles
from ...storage.db.write_gate import commit_connection, with_db_write


@handles("get_enrichment_lab_model_resolve")
async def handle_get_enrichment_lab_model_resolve(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import model_resolve

    model_id = str((message.get("payload") or {}).get("model_id") or "").strip()
    if not model_id:
        return {"id": req_id, "status": "error", "error": "model_id required"}
    result = await asyncio.to_thread(model_resolve.resolve_model, model_id)
    return {"id": req_id, "status": "ok", "payload": result}


@handles("get_enrichment_lab_bundles")
async def handle_get_enrichment_lab_bundles(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import bundles as el_bundles

    return {"id": req_id, "status": "ok", "payload": el_bundles.list_bundle_metadata()}


@handles("get_enrichment_lab_bundle_detail")
async def handle_get_enrichment_lab_bundle_detail(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import bundles as el_bundles

    bid = str((message.get("payload") or {}).get("bundle_id") or "").strip()
    if not bid:
        return {"id": req_id, "status": "error", "error": "bundle_id required"}
    data = el_bundles.get_bundle_preview(bid)
    if not data:
        return {"id": req_id, "status": "error", "error": "Bundle not found"}
    return {"id": req_id, "status": "ok", "payload": data}


@handles("get_enrichment_lab_node_sample")
async def handle_get_enrichment_lab_node_sample(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import service as el_service

    pl = message.get("payload") or {}
    source_id = str(pl.get("source_id") or "").strip()
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    try:
        limit = int(pl.get("limit") or el_service.DEFAULT_NODE_SAMPLE)
        records = el_service.sample_node_records(source_id, limit=limit)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"status": "ok", "source_id": source_id, "records": records},
        }
    except (ValueError, RuntimeError) as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("post_enrichment_lab_job_group")
async def handle_post_enrichment_lab_job_group(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import service as el_service

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        gid = el_service.create_job_group(
            job_id=str(payload.get("job_id") or "").strip(),
            models=list(payload.get("models") or []),
            dataset_kind=str(payload.get("dataset_kind") or "bundle").strip(),
            bundle_id=payload.get("bundle_id"),
            source_id=payload.get("source_id"),
            sample_limit=payload.get("sample_limit"),
            options=payload.get("options") if isinstance(payload.get("options"), dict) else None,
        )
        data = el_service.serialize_job_group(conn, gid)
        return {"id": req_id, "status": "ok", "payload": data}
    except (ValueError, RuntimeError) as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("get_enrichment_lab_job_group_detail")
async def handle_get_enrichment_lab_job_group_detail(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import service as el_service

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    group_id = str((message.get("payload") or {}).get("group_id") or "").strip()
    if not group_id:
        return {"id": req_id, "status": "error", "error": "group_id required"}
    try:
        data = el_service.serialize_job_group(conn, group_id)
        return {"id": req_id, "status": "ok", "payload": data}
    except KeyError:
        return {"id": req_id, "status": "error", "error": "Job group not found"}


@handles("list_enrichment_lab_job_groups")
async def handle_list_enrichment_lab_job_groups(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import service as el_service
    from ...enrichment_lab import store as el_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    job_id = str(pl.get("job_id") or "").strip()
    limit = min(max(int(pl.get("limit") or 20), 1), 100)
    offset = max(int(pl.get("offset") or 0), 0)
    el_store.prune_old_groups(conn, max_age_days=30)
    rows = el_store.list_groups(conn, job_id=job_id or None, limit=limit, offset=offset)
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
    el_service.enrich_job_groups_list_with_run_summaries(conn, groups)
    return {"id": req_id, "status": "ok", "payload": {"groups": groups, "limit": limit, "offset": offset}}


@handles("patch_enrichment_lab_job_group")
async def handle_patch_enrichment_lab_job_group(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import service as el_service
    from ...enrichment_lab import store as el_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    group_id = str(pl.get("group_id") or "").strip()
    body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
    if not group_id:
        return {"id": req_id, "status": "error", "error": "group_id required"}
    if not el_store.get_group(conn, group_id):
        return {"id": req_id, "status": "error", "error": "Job group not found"}
    if "preferred_model_tag" in body:
        p = body.get("preferred_model_tag")
        el_store.patch_group(conn, group_id, preferred_model_tag=p if p is None else str(p).strip() or None)
    if "group_notes" in body:
        el_store.patch_group(conn, group_id, group_notes=body.get("group_notes"))
    if "notes" in body:
        el_store.patch_group(conn, group_id, notes=body.get("notes"))
    try:
        data = el_service.serialize_job_group(conn, group_id)
        return {"id": req_id, "status": "ok", "payload": data}
    except KeyError:
        return {"id": req_id, "status": "error", "error": "Job group not found"}


@handles("patch_enrichment_lab_job_run")
async def handle_patch_enrichment_lab_job_run(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import service as el_service
    from ...enrichment_lab import store as el_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    group_id = str(pl.get("group_id") or "").strip()
    run_id = str(pl.get("run_id") or "").strip()
    body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
    if not group_id or not run_id:
        return {"id": req_id, "status": "error", "error": "group_id and run_id required"}
    runs = el_store.list_runs(conn, group_id)
    if not any(dict(r)["id"] == run_id for r in runs):
        return {"id": req_id, "status": "error", "error": "Run not found"}
    if "user_quality_score_0_10" in body:
        v = body.get("user_quality_score_0_10")
        if v is not None and (not isinstance(v, int) or v < 0 or v > 10):
            return {"id": req_id, "status": "error", "error": "user_quality_score_0_10 must be 0–10 or null"}
        el_store.patch_run(conn, run_id, user_quality_score_0_10=v)
    if "user_liked" in body:
        v = body.get("user_liked")
        if v is None:
            with with_db_write():
                conn.execute("UPDATE enrichment_lab_run SET user_liked = NULL WHERE id = ?", (run_id,))
                commit_connection(conn)
        else:
            el_store.patch_run(conn, run_id, user_liked=bool(v))
    if "user_note" in body:
        note = body.get("user_note")
        el_store.patch_run(conn, run_id, user_note=None if note is None else str(note)[:2000])
    try:
        data = el_service.serialize_job_group(conn, group_id)
        return {"id": req_id, "status": "ok", "payload": data}
    except KeyError:
        return {"id": req_id, "status": "error", "error": "Job group not found"}


@handles("post_enrichment_lab_apply_preferred")
async def handle_post_enrichment_lab_apply_preferred(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import service as el_service

    group_id = str((message.get("payload") or {}).get("group_id") or "").strip()
    if not group_id:
        return {"id": req_id, "status": "error", "error": "group_id required"}
    try:
        data = el_service.apply_preferred_model(group_id)
        return {"id": req_id, "status": "ok", "payload": data}
    except (ValueError, RuntimeError) as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("delete_enrichment_lab_job_group")
async def handle_delete_enrichment_lab_job_group(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...enrichment_lab import store as el_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    group_id = str((message.get("payload") or {}).get("group_id") or "").strip()
    if not group_id:
        return {"id": req_id, "status": "error", "error": "group_id required"}
    if not el_store.get_group(conn, group_id):
        return {"id": req_id, "status": "error", "error": "Job group not found"}
    el_store.delete_group(conn, group_id)
    return {"id": req_id, "status": "ok", "payload": {"status": "ok", "group_id": group_id, "deleted": True}}
