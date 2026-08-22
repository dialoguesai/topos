"""Routine and routine-run message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    run_db_read,
    run_db_write,
)
from .registry import handles


@handles("list_routines")
async def handle_list_routines(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    engine_id = str(pl.get("engine_id") or "").strip()
    if not user_id or not engine_id:
        return {"id": req_id, "status": "error", "error": "user_id and engine_id required"}
    rows = await run_db_read(
        routines_store.list_routines, owner_user_id=user_id, engine_id=engine_id
    )
    return {"id": req_id, "status": "ok", "payload": {"routines": rows}}

@handles("get_routine")
async def handle_get_routine(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    routine_id = str(pl.get("routine_id") or "").strip()
    if not user_id or not routine_id:
        return {"id": req_id, "status": "error", "error": "user_id and routine_id required"}
    row = await run_db_read(
        routines_store.get_routine, owner_user_id=user_id, routine_id=routine_id
    )
    if not row:
        return {"id": req_id, "status": "error", "error": "Routine not found"}
    return {"id": req_id, "status": "ok", "payload": {"routine": row}}

@handles("get_routine_by_id")
async def handle_get_routine_by_id(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    routine_id = str(pl.get("routine_id") or "").strip()
    if not routine_id:
        return {"id": req_id, "status": "error", "error": "routine_id required"}
    row = await run_db_read(routines_store.get_routine_for_execution, routine_id)
    if not row:
        return {"id": req_id, "status": "error", "error": "Routine not found"}
    return {"id": req_id, "status": "ok", "payload": {"routine": row}}

@handles("create_routine")
async def handle_create_routine(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    engine_id = str(pl.get("engine_id") or "").strip()
    body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
    if not user_id or not engine_id:
        return {"id": req_id, "status": "error", "error": "user_id and engine_id required"}
    try:
        row = await run_db_write(
            routines_store.create_routine,
            owner_user_id=user_id,
            engine_id=engine_id,
            payload=body,
        )
        return {"id": req_id, "status": "ok", "payload": {"routine": row}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "error_code": str(exc)}

@handles("patch_routine")
async def handle_patch_routine(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    routine_id = str(pl.get("routine_id") or "").strip()
    body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
    if not user_id or not routine_id:
        return {"id": req_id, "status": "error", "error": "user_id and routine_id required"}
    row = await run_db_write(
        routines_store.patch_routine,
        owner_user_id=user_id,
        routine_id=routine_id,
        payload=body,
    )
    if not row:
        return {"id": req_id, "status": "error", "error": "Routine not found"}
    return {"id": req_id, "status": "ok", "payload": {"routine": row}}

@handles("delete_routine")
async def handle_delete_routine(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    routine_id = str(pl.get("routine_id") or "").strip()
    if not user_id or not routine_id:
        return {"id": req_id, "status": "error", "error": "user_id and routine_id required"}
    deleted = await run_db_write(
        routines_store.soft_delete_routine,
        owner_user_id=user_id,
        routine_id=routine_id,
    )
    if not deleted:
        return {"id": req_id, "status": "error", "error": "Routine not found"}
    return {"id": req_id, "status": "ok", "payload": {"deleted": True}}

@handles("list_routine_runs")
async def handle_list_routine_runs(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    routine_id = str(pl.get("routine_id") or "").strip()
    if not user_id or not routine_id:
        return {"id": req_id, "status": "error", "error": "user_id and routine_id required"}
    rows = await run_db_read(
        routines_store.list_runs, owner_user_id=user_id, routine_id=routine_id
    )
    return {"id": req_id, "status": "ok", "payload": {"runs": rows}}

@handles("get_routine_run")
async def handle_get_routine_run(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    routine_id = str(pl.get("routine_id") or "").strip()
    run_id = str(pl.get("run_id") or "").strip()
    if not user_id or not routine_id or not run_id:
        return {"id": req_id, "status": "error", "error": "user_id, routine_id, and run_id required"}
    row = await run_db_read(
        routines_store.get_run,
        owner_user_id=user_id,
        routine_id=routine_id,
        run_id=run_id,
    )
    if not row:
        return {"id": req_id, "status": "error", "error": "Run not found"}
    return {"id": req_id, "status": "ok", "payload": {"run": row}}

@handles("get_routine_run_by_id")
async def handle_get_routine_run_by_id(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    run_id = str(pl.get("run_id") or "").strip()
    if not run_id:
        return {"id": req_id, "status": "error", "error": "run_id required"}
    row = await run_db_read(routines_store.get_run_for_execution, run_id)
    if not row:
        return {"id": req_id, "status": "error", "error": "Run not found"}
    return {"id": req_id, "status": "ok", "payload": {"run": row}}

@handles("create_routine_run")
async def handle_create_routine_run(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    engine_id = str(pl.get("engine_id") or "").strip()
    routine_id = str(pl.get("routine_id") or "").strip()
    body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
    if not user_id or not engine_id or not routine_id:
        return {"id": req_id, "status": "error", "error": "user_id, engine_id, and routine_id required"}
    try:
        row = await run_db_write(
            routines_store.create_run,
            owner_user_id=user_id,
            engine_id=engine_id,
            routine_id=routine_id,
            run_payload=body,
        )
        return {"id": req_id, "status": "ok", "payload": {"run": row}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "error_code": str(exc)}

@handles("update_routine_run")
async def handle_update_routine_run(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    run_id = str(pl.get("run_id") or "").strip()
    patch = pl.get("patch") if isinstance(pl.get("patch"), dict) else {}
    if not run_id:
        return {"id": req_id, "status": "error", "error": "run_id required"}
    row = await run_db_write(routines_store.update_run, run_id, patch)
    if not row:
        return {"id": req_id, "status": "error", "error": "Run not found"}
    return {"id": req_id, "status": "ok", "payload": {"run": row}}

@handles("list_waiting_routine_runs")
async def handle_list_waiting_routine_runs(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    engine_id = str(pl.get("engine_id") or "").strip()
    if not user_id or not engine_id:
        return {"id": req_id, "status": "error", "error": "user_id and engine_id required"}
    rows = await run_db_read(
        routines_store.list_waiting_runs_for_owner,
        owner_user_id=user_id,
        engine_id=engine_id,
    )
    return {"id": req_id, "status": "ok", "payload": {"runs": rows}}

@handles("list_due_routines")
async def handle_list_due_routines(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    engine_id = str(pl.get("engine_id") or "").strip()
    if not engine_id:
        return {"id": req_id, "status": "error", "error": "engine_id required"}
    rows = await run_db_read(routines_store.list_due_scheduled_routines, engine_id=engine_id)
    return {"id": req_id, "status": "ok", "payload": {"routines": rows}}

@handles("routine_has_active_run")
async def handle_routine_has_active_run(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    routine_id = str(pl.get("routine_id") or "").strip()
    if not routine_id:
        return {"id": req_id, "status": "error", "error": "routine_id required"}
    active = await run_db_read(routines_store.has_active_run, routine_id)
    return {
        "id": req_id,
        "status": "ok",
        "payload": {"active": active},
    }

@handles("list_active_routine_runs")
async def handle_list_active_routine_runs(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    engine_id = str(pl.get("engine_id") or "").strip()
    if not engine_id:
        return {"id": req_id, "status": "error", "error": "engine_id required"}
    rows = await run_db_read(routines_store.list_active_runs, engine_id=engine_id)
    return {"id": req_id, "status": "ok", "payload": {"runs": rows}}

@handles("advance_routine_next_run_at")
async def handle_advance_routine_next_run_at(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...routines import store as routines_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    routine_id = str(pl.get("routine_id") or "").strip()
    next_run_at = pl.get("next_run_at")
    if not routine_id:
        return {"id": req_id, "status": "error", "error": "routine_id required"}
    await run_db_write(routines_store.advance_next_run_at, routine_id, next_run_at=next_run_at)
    return {"id": req_id, "status": "ok", "payload": {"advanced": True}}
