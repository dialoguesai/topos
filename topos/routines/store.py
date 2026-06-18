"""SQLite blob store for local-node routines (validated by control plane before proxy)."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .schema import ensure_routines_schema

ACTIVE_RUN_STATUSES = frozenset({"queued", "waiting_for_engine", "running"})
MAX_ROUTINES_PER_OWNER = 10
MAX_RUNS_PER_OWNER_24H = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _json_load(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _routine_row_to_api(row: Dict[str, Any], *, last_run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = _json_load(row.get("payload_json"))
    payload["id"] = row["id"]
    payload["owner_user_id"] = row["owner_user_id"]
    payload["enabled"] = bool(row.get("enabled", True))
    payload["trigger_type"] = row.get("trigger_type") or payload.get("trigger_type") or "manual"
    payload["next_run_at"] = row.get("next_run_at")
    payload["updated_at"] = row.get("updated_at")
    payload["created_at"] = payload.get("created_at") or row.get("updated_at")
    payload["last_run"] = last_run
    return payload


def _run_row_to_api(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = _json_load(row.get("payload_json"))
    payload["id"] = row["id"]
    payload["routine_id"] = row["routine_id"]
    payload["owner_user_id"] = row["owner_user_id"]
    payload["status"] = row.get("status") or payload.get("status")
    payload["updated_at"] = row.get("updated_at")
    return payload


def _routine_index_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    trigger = str(payload.get("trigger_type") or "manual").strip()
    if trigger == "weekly":
        trigger = "scheduled"
    return {
        "enabled": 1 if payload.get("enabled", True) else 0,
        "trigger_type": trigger,
        "next_run_at": payload.get("next_run_at"),
    }


def _latest_run_for_routine(conn, routine_id: str) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, status, payload_json, updated_at
        FROM routine_runs
        WHERE routine_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (str(routine_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    payload = _json_load(data.get("payload_json"))
    return {
        "id": data["id"],
        "status": data.get("status") or payload.get("status"),
        "completed_at": payload.get("completed_at"),
    }


def list_routines(conn, *, owner_user_id: str, engine_id: str) -> List[Dict[str, Any]]:
    ensure_routines_schema(conn)
    uid = str(owner_user_id).strip()
    eid = str(engine_id).strip()
    cur = conn.execute(
        """
        SELECT id, owner_user_id, engine_id, enabled, trigger_type, next_run_at, deleted_at, payload_json, updated_at
        FROM routines
        WHERE owner_user_id = ? AND engine_id = ? AND deleted_at IS NULL
        ORDER BY updated_at DESC
        """,
        (uid, eid),
    )
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        data = dict(row)
        last_run = _latest_run_for_routine(conn, data["id"])
        out.append(_routine_row_to_api(data, last_run=last_run))
    return out


def get_routine(conn, *, owner_user_id: str, routine_id: str) -> Optional[Dict[str, Any]]:
    ensure_routines_schema(conn)
    cur = conn.execute(
        """
        SELECT id, owner_user_id, engine_id, enabled, trigger_type, next_run_at, deleted_at, payload_json, updated_at
        FROM routines
        WHERE id = ? AND owner_user_id = ?
        """,
        (str(routine_id), str(owner_user_id).strip()),
    )
    row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    if data.get("deleted_at"):
        return None
    last_run = _latest_run_for_routine(conn, data["id"])
    return _routine_row_to_api(data, last_run=last_run)


def get_routine_for_execution(conn, routine_id: str) -> Optional[Dict[str, Any]]:
    ensure_routines_schema(conn)
    cur = conn.execute(
        """
        SELECT id, owner_user_id, engine_id, enabled, trigger_type, next_run_at, deleted_at, payload_json, updated_at
        FROM routines
        WHERE id = ? AND deleted_at IS NULL
        """,
        (str(routine_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return _routine_row_to_api(dict(row))


def count_routines_for_owner(conn, *, owner_user_id: str, engine_id: str) -> int:
    ensure_routines_schema(conn)
    cur = conn.execute(
        """
        SELECT COUNT(*) AS c FROM routines
        WHERE owner_user_id = ? AND engine_id = ? AND deleted_at IS NULL
        """,
        (str(owner_user_id).strip(), str(engine_id).strip()),
    )
    row = cur.fetchone()
    return int(row[0] if row else 0)


def create_routine(
    conn,
    *,
    owner_user_id: str,
    engine_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    ensure_routines_schema(conn)
    uid = str(owner_user_id).strip()
    eid = str(engine_id).strip()
    if count_routines_for_owner(conn, owner_user_id=uid, engine_id=eid) >= MAX_ROUTINES_PER_OWNER:
        raise ValueError("ROUTINE_LIMIT")
    rid = str(payload.get("id") or uuid4())
    now = _now_iso()
    body = deepcopy(payload)
    body["id"] = rid
    body["owner_user_id"] = uid
    body["created_at"] = body.get("created_at") or now
    body["updated_at"] = now
    index = _routine_index_fields(body)
    conn.execute(
        """
        INSERT INTO routines (
            id, owner_user_id, engine_id, enabled, trigger_type, next_run_at, deleted_at, payload_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            rid,
            uid,
            eid,
            index["enabled"],
            index["trigger_type"],
            index["next_run_at"],
            json.dumps(body, separators=(",", ":"), default=str),
            now,
        ),
    )
    conn.commit()
    row = get_routine(conn, owner_user_id=uid, routine_id=rid)
    if not row:
        raise RuntimeError("Failed to persist routine")
    return row


def patch_routine(
    conn,
    *,
    owner_user_id: str,
    routine_id: str,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    ensure_routines_schema(conn)
    existing = get_routine(conn, owner_user_id=owner_user_id, routine_id=routine_id)
    if not existing:
        return None
    merged = {**existing, **payload}
    now = _now_iso()
    merged["updated_at"] = now
    index = _routine_index_fields(merged)
    conn.execute(
        """
        UPDATE routines SET
            enabled = ?,
            trigger_type = ?,
            next_run_at = ?,
            payload_json = ?,
            updated_at = ?
        WHERE id = ? AND owner_user_id = ?
        """,
        (
            index["enabled"],
            index["trigger_type"],
            index["next_run_at"],
            json.dumps(merged, separators=(",", ":"), default=str),
            now,
            str(routine_id),
            str(owner_user_id).strip(),
        ),
    )
    conn.commit()
    return get_routine(conn, owner_user_id=owner_user_id, routine_id=routine_id)


def soft_delete_routine(conn, *, owner_user_id: str, routine_id: str) -> bool:
    ensure_routines_schema(conn)
    now = _now_iso()
    cur = conn.execute(
        """
        UPDATE routines SET deleted_at = ?, enabled = 0, updated_at = ?
        WHERE id = ? AND owner_user_id = ? AND deleted_at IS NULL
        """,
        (now, now, str(routine_id), str(owner_user_id).strip()),
    )
    conn.commit()
    return cur.rowcount > 0


def list_due_scheduled_routines(conn, *, engine_id: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    ensure_routines_schema(conn)
    instant = (now or _now_dt()).isoformat()
    cur = conn.execute(
        """
        SELECT id, owner_user_id, engine_id, enabled, trigger_type, next_run_at, deleted_at, payload_json, updated_at
        FROM routines
        WHERE engine_id = ?
          AND deleted_at IS NULL
          AND enabled = 1
          AND trigger_type IN ('weekly', 'scheduled')
          AND next_run_at IS NOT NULL
          AND next_run_at <= ?
        """,
        (str(engine_id).strip(), instant),
    )
    return [_routine_row_to_api(dict(row)) for row in cur.fetchall()]


def advance_next_run_at(conn, routine_id: str, *, next_run_at: str) -> None:
    ensure_routines_schema(conn)
    now = _now_iso()
    cur = conn.execute("SELECT payload_json FROM routines WHERE id = ?", (str(routine_id),))
    row = cur.fetchone()
    if not row:
        return
    payload = _json_load(row[0])
    payload["next_run_at"] = next_run_at
    payload["updated_at"] = now
    conn.execute(
        """
        UPDATE routines SET next_run_at = ?, payload_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (next_run_at, json.dumps(payload, separators=(",", ":"), default=str), now, str(routine_id)),
    )
    conn.commit()


def has_active_run(conn, routine_id: str) -> bool:
    ensure_routines_schema(conn)
    placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
    cur = conn.execute(
        f"""
        SELECT 1 FROM routine_runs
        WHERE routine_id = ? AND status IN ({placeholders})
        LIMIT 1
        """,
        (str(routine_id), *sorted(ACTIVE_RUN_STATUSES)),
    )
    return cur.fetchone() is not None


def count_runs_last_24h(conn, *, owner_user_id: str, engine_id: str) -> int:
    ensure_routines_schema(conn)
    cutoff = (_now_dt() - timedelta(hours=24)).isoformat()
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM routine_runs
        WHERE owner_user_id = ? AND engine_id = ? AND updated_at >= ?
        """,
        (str(owner_user_id).strip(), str(engine_id).strip(), cutoff),
    )
    row = cur.fetchone()
    return int(row[0] if row else 0)


def create_run(
    conn,
    *,
    owner_user_id: str,
    engine_id: str,
    routine_id: str,
    run_payload: Dict[str, Any],
) -> Dict[str, Any]:
    ensure_routines_schema(conn)
    uid = str(owner_user_id).strip()
    eid = str(engine_id).strip()
    if count_runs_last_24h(conn, owner_user_id=uid, engine_id=eid) >= MAX_RUNS_PER_OWNER_24H:
        raise ValueError("RUN_RATE_LIMIT")
    if not get_routine(conn, owner_user_id=uid, routine_id=routine_id):
        raise ValueError("NOT_FOUND")
    if has_active_run(conn, routine_id):
        raise ValueError("ACTIVE_RUN")
    idempotency_key = run_payload.get("idempotency_key")
    if idempotency_key:
        cur = conn.execute(
            "SELECT id FROM routine_runs WHERE idempotency_key = ? LIMIT 1",
            (str(idempotency_key),),
        )
        if cur.fetchone():
            raise ValueError("IDEMPOTENCY_CONFLICT")
    run_id = str(run_payload.get("id") or uuid4())
    now = _now_iso()
    body = deepcopy(run_payload)
    body["id"] = run_id
    body["routine_id"] = str(routine_id)
    body["owner_user_id"] = uid
    body["created_at"] = body.get("created_at") or now
    body["updated_at"] = now
    status = str(body.get("status") or "queued")
    conn.execute(
        """
        INSERT INTO routine_runs (
            id, routine_id, owner_user_id, engine_id, status, idempotency_key, payload_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            str(routine_id),
            uid,
            eid,
            status,
            idempotency_key,
            json.dumps(body, separators=(",", ":"), default=str),
            now,
        ),
    )
    conn.commit()
    return _run_row_to_api(
        {
            "id": run_id,
            "routine_id": str(routine_id),
            "owner_user_id": uid,
            "status": status,
            "payload_json": json.dumps(body),
            "updated_at": now,
        }
    )


def get_run(conn, *, owner_user_id: str, routine_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    ensure_routines_schema(conn)
    cur = conn.execute(
        """
        SELECT id, routine_id, owner_user_id, engine_id, status, idempotency_key, payload_json, updated_at
        FROM routine_runs
        WHERE id = ? AND owner_user_id = ? AND routine_id = ?
        """,
        (str(run_id), str(owner_user_id).strip(), str(routine_id)),
    )
    row = cur.fetchone()
    return _run_row_to_api(dict(row)) if row else None


def get_run_for_execution(conn, run_id: str) -> Optional[Dict[str, Any]]:
    ensure_routines_schema(conn)
    cur = conn.execute(
        """
        SELECT id, routine_id, owner_user_id, engine_id, status, idempotency_key, payload_json, updated_at
        FROM routine_runs
        WHERE id = ?
        """,
        (str(run_id),),
    )
    row = cur.fetchone()
    return _run_row_to_api(dict(row)) if row else None


def list_runs(conn, *, owner_user_id: str, routine_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_routines_schema(conn)
    cur = conn.execute(
        """
        SELECT id, routine_id, owner_user_id, engine_id, status, idempotency_key, payload_json, updated_at
        FROM routine_runs
        WHERE owner_user_id = ? AND routine_id = ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (str(owner_user_id).strip(), str(routine_id), int(limit)),
    )
    return [_run_row_to_api(dict(row)) for row in cur.fetchall()]


def update_run(conn, run_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ensure_routines_schema(conn)
    cur = conn.execute(
        "SELECT id, routine_id, owner_user_id, engine_id, status, idempotency_key, payload_json, updated_at FROM routine_runs WHERE id = ?",
        (str(run_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    payload = _json_load(data.get("payload_json"))
    payload.update(patch)
    now = _now_iso()
    payload["updated_at"] = now
    status = str(payload.get("status") or data.get("status") or "queued")
    conn.execute(
        """
        UPDATE routine_runs SET status = ?, payload_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, json.dumps(payload, separators=(",", ":"), default=str), now, str(run_id)),
    )
    conn.commit()
    data["status"] = status
    data["payload_json"] = json.dumps(payload)
    data["updated_at"] = now
    return _run_row_to_api(data)


def list_waiting_runs_for_owner(conn, *, owner_user_id: str, engine_id: str) -> List[Dict[str, Any]]:
    ensure_routines_schema(conn)
    cur = conn.execute(
        """
        SELECT id, routine_id, owner_user_id, engine_id, status, idempotency_key, payload_json, updated_at
        FROM routine_runs
        WHERE owner_user_id = ? AND engine_id = ? AND status = 'waiting_for_engine'
        """,
        (str(owner_user_id).strip(), str(engine_id).strip()),
    )
    return [_run_row_to_api(dict(row)) for row in cur.fetchall()]
