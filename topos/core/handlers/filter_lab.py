"""Filter Lab message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    json,
)
from .registry import handles
from ...storage.db.write_gate import batched_writes, commit_connection, with_db_write


@handles("get_filter_lab_bundles")
async def handle_get_filter_lab_bundles(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import bundles as fl_bundles

    return {"id": req_id, "status": "ok", "payload": fl_bundles.list_bundle_metadata()}

@handles("get_filter_lab_bundle_detail")
async def handle_get_filter_lab_bundle_detail(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import bundles as fl_bundles

    bid = str((message.get("payload") or {}).get("bundle_id") or "").strip()
    if not bid:
        return {"id": req_id, "status": "error", "error": "bundle_id required"}
    data = fl_bundles.get_bundle_preview(bid)
    if not data:
        return {"id": req_id, "status": "error", "error": "Bundle not found"}
    return {"id": req_id, "status": "ok", "payload": data}

@handles("post_filter_lab_job_group")
async def handle_post_filter_lab_job_group(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import service as fl_service

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        gid = fl_service.create_job_group(
            filter_id=str(payload.get("filter_id") or "").strip(),
            bundle_id=str(payload.get("bundle_id") or "").strip(),
            models=list(payload.get("models") or []),
            options=payload.get("options") if isinstance(payload.get("options"), dict) else None,
        )
        data = fl_service.serialize_job_group(conn, gid)
        return {"id": req_id, "status": "ok", "payload": data}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except RuntimeError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_filter_lab_job_group_detail")
async def handle_get_filter_lab_job_group_detail(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import service as fl_service

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    group_id = str((message.get("payload") or {}).get("group_id") or "").strip()
    if not group_id:
        return {"id": req_id, "status": "error", "error": "group_id required"}
    try:
        data = fl_service.serialize_job_group(conn, group_id)
        return {"id": req_id, "status": "ok", "payload": data}
    except KeyError:
        return {"id": req_id, "status": "error", "error": "Job group not found"}

@handles("list_filter_lab_job_groups")
async def handle_list_filter_lab_job_groups(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import service as fl_service
    from ...filter_lab import store as fl_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    fid = str(pl.get("filter_id") or "").strip()
    limit = min(max(int(pl.get("limit") or 20), 1), 100)
    offset = max(int(pl.get("offset") or 0), 0)
    fl_store.prune_old_groups(conn, max_age_days=30)
    if fid:
        rows = fl_store.list_groups_for_filter(conn, fid, limit=limit, offset=offset)
    else:
        rows = fl_store.list_all_job_groups(conn, limit=limit, offset=offset)
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
    fl_service.enrich_job_groups_list_with_run_summaries(conn, groups)
    return {"id": req_id, "status": "ok", "payload": {"groups": groups, "limit": limit, "offset": offset}}

@handles("patch_filter_lab_job_group")
async def handle_patch_filter_lab_job_group(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import service as fl_service
    from ...filter_lab import store as fl_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    group_id = str(pl.get("group_id") or "").strip()
    body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
    if not group_id:
        return {"id": req_id, "status": "error", "error": "group_id required"}
    if not fl_store.get_group(conn, group_id):
        return {"id": req_id, "status": "error", "error": "Job group not found"}
    if "preferred_model_tag" in body:
        p = body.get("preferred_model_tag")
        fl_store.patch_group(conn, group_id, preferred_model_tag=p if p is None else str(p).strip() or None)
    if "group_notes" in body:
        fl_store.patch_group(conn, group_id, group_notes=body.get("group_notes"))
    if "notes" in body:
        fl_store.patch_group(conn, group_id, notes=body.get("notes"))
    try:
        data = fl_service.serialize_job_group(conn, group_id)
        return {"id": req_id, "status": "ok", "payload": data}
    except KeyError:
        return {"id": req_id, "status": "error", "error": "Job group not found"}

@handles("patch_filter_lab_job_run")
async def handle_patch_filter_lab_job_run(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import service as fl_service
    from ...filter_lab import store as fl_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    group_id = str(pl.get("group_id") or "").strip()
    run_id = str(pl.get("run_id") or "").strip()
    body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
    if not group_id or not run_id:
        return {"id": req_id, "status": "error", "error": "group_id and run_id required"}
    runs = fl_store.list_runs(conn, group_id)
    if not any(dict(r)["id"] == run_id for r in runs):
        return {"id": req_id, "status": "error", "error": "Run not found"}
    rated = False
    if "user_quality_score_0_10" in body:
        v = body.get("user_quality_score_0_10")
        if v is not None and (not isinstance(v, int) or v < 0 or v > 10):
            return {"id": req_id, "status": "error", "error": "user_quality_score_0_10 must be 0–10 or null"}
        fl_store.patch_run(conn, run_id, user_quality_score_0_10=v)
        rated = True
    if "user_liked" in body:
        v = body.get("user_liked")
        if v is None:
            with with_db_write():
                conn.execute("UPDATE filter_lab_run SET user_liked = NULL WHERE id = ?", (run_id,))
                commit_connection(conn)
        else:
            fl_store.patch_run(conn, run_id, user_liked=bool(v))
        rated = True
    if "user_note" in body:
        note = body.get("user_note")
        fl_store.patch_run(conn, run_id, user_note=None if note is None else str(note)[:2000])
        rated = True
    if rated:
        with with_db_write():
            conn.execute(
                "UPDATE filter_lab_run SET rated_at = ? WHERE id = ?",
                (fl_store.utc_now_iso(), run_id),
            )
            commit_connection(conn)
    try:
        data = fl_service.serialize_job_group(conn, group_id)
        return {"id": req_id, "status": "ok", "payload": data}
    except KeyError:
        return {"id": req_id, "status": "error", "error": "Job group not found"}

@handles("post_filter_lab_apply_preferred")
async def handle_post_filter_lab_apply_preferred(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import service as fl_service

    group_id = str((message.get("payload") or {}).get("group_id") or "").strip()
    if not group_id:
        return {"id": req_id, "status": "error", "error": "group_id required"}
    try:
        data = fl_service.apply_preferred_model(group_id)
        return {"id": req_id, "status": "ok", "payload": data}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except RuntimeError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("delete_filter_lab_job_group")
async def handle_delete_filter_lab_job_group(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import store as fl_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    group_id = str((message.get("payload") or {}).get("group_id") or "").strip()
    if not group_id:
        return {"id": req_id, "status": "error", "error": "group_id required"}
    if not fl_store.get_group(conn, group_id):
        return {"id": req_id, "status": "error", "error": "Job group not found"}
    fl_store.delete_group(conn, group_id)
    return {"id": req_id, "status": "ok", "payload": {"status": "ok", "group_id": group_id, "deleted": True}}

@handles("delete_filter_lab_all_data")
async def handle_delete_filter_lab_all_data(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...filter_lab import store as fl_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    fl_store.ensure_schema(conn)
    with batched_writes(conn):
        conn.execute("DELETE FROM filter_lab_model_event")
        conn.execute("DELETE FROM filter_lab_run")
        conn.execute("DELETE FROM filter_lab_job_group")
    return {"id": req_id, "status": "ok", "payload": {"status": "ok", "cleared": True}}
