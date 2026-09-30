"""Owner maintenance: see a source's runtime installs and retire a duplicate one, by the owner's choice.

A source with more than one active install has no posture a permissions reader
can resolve: ``evidence._source_posture`` refuses it outright, so every row of the
source is withheld as ``source_posture_unknown`` whatever its proof. That is the
state one owner's ChatGPT export source (``chatgpt_file_ingestion``) reached: two
active installs, both dataset-scoped, from 31 Aug and 9 Sep. Which one stays is
the owner's call (they differ in declared posture and scope), so nothing here
picks one: :func:`installs` lists them, :func:`deactivate` retires the one the
owner names, as a dry run unless told otherwise.

The listing carries ids, dates, status, declared posture and scope only, never a
source definition's other fields or any row of the source. Deactivation never
removes the last active install and never deletes: the row stays as history with
``status = 'superseded'``, exactly as the install service retires a stale row.

Retiring an install moves the ingest source clock (``ingest_provenance``, source
clock v2 watches ``source_runtime_installs``), which stales every native
enrollment and so every native owner proof until the owner's next refresh
(``permissions_v2/NATIVE_EVIDENCE_REFRESH.md``). The dry run reports whether that
will happen and how many active enrollments it stales, so the owner can plan the
refresh and the grant Sync that follow.

``install_service.install_source`` now refuses to create a second active install
of a source for the same owner in another scope, so this state does not recur.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

TABLE = "source_runtime_installs"
LIVE = ("installed", "active", "ready")
REASON = "owner_deactivated_duplicate_install"


class MaintenanceError(ValueError):
    """A bounded refusal code, with no row data in it."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value and value == value.strip() and len(value) <= 256 else None


def _present(conn) -> bool:
    found = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (TABLE,)).fetchmany(2)
    return len(found) == 1 and found[0][0] == "table"


def _scope(scope_key: Any) -> Optional[dict]:
    try:
        parsed = json.loads(scope_key) if isinstance(scope_key, str) else None
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {field: parsed.get(field, parsed.get("app_id") if field == "topos_id" else None)
            for field in ("user_id", "device_id", "topos_id", "dataset_id")}


def _posture(definition: Any) -> Optional[str]:
    try:
        parsed = json.loads(definition) if isinstance(definition, str) else None
    except ValueError:
        return None
    posture = parsed.get("posture") if isinstance(parsed, dict) else None
    return posture if isinstance(posture, str) else None


def _rows(conn, source_id: str) -> list:
    return conn.execute(
        f"SELECT install_id, scope_key, status, is_active, source_definition_json, created_at, updated_at "
        f"FROM {TABLE} WHERE source_id=? ORDER BY created_at, install_id", (source_id,)).fetchall()


def _active(is_active: Any) -> bool:
    # ``evidence._source_posture`` counts every row whose flag is not 0 as live, so this does too.
    return is_active is not None and is_active != 0


def installs(conn, *, source_id: Any) -> dict:
    """Every install row of this source (history included): ids, dates, status, declared posture, scope."""
    source = _text(source_id)
    if source is None:
        raise MaintenanceError("source_id_required")
    if not _present(conn):
        return {"source_id": source, "active_count": 0, "installs": []}
    listed = []
    for install_id, scope_key, status, is_active, definition, created_at, updated_at in _rows(conn, source):
        listed.append({"install_id": install_id, "active": _active(is_active), "status": status,
                       "declared_posture": _posture(definition), "scope": _scope(scope_key),
                       "created_at": created_at, "updated_at": updated_at})
    return {"source_id": source, "active_count": sum(1 for item in listed if item["active"]), "installs": listed}


def _clock(conn) -> dict:
    """Whether a change to the install table advances the ingest source clock, and what it stales."""
    watched = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
                           "AND name GLOB 'ingest_provenance_*'", (TABLE,)).fetchone()[0]
    enrollments = 0
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_provenance_enrollments'"
                    ).fetchone():
        enrollments = conn.execute("SELECT COUNT(*) FROM ingest_provenance_enrollments WHERE state='active'"
                                   ).fetchone()[0]
    return {"ingest_source_clock_advances": bool(watched), "native_enrollments_staled": enrollments if watched else 0}


def deactivate(conn, *, owner_id: Any, source_id: Any, install_id: Any, dry_run: Any = True, confirm: Any = False,
               now: Optional[str] = None) -> dict:
    """Retire one active install of a source that has more than one. A dry run unless ``dry_run is False``.

    The caller holds a write transaction and commits only a real run (a dry run's change is rolled back by the
    caller; nothing here commits). Refused: an unknown install, one of another source, one already inactive, and
    the source's last active install. The answer projects what the permissions reader will see afterwards.
    """
    from topos.permissions_v2.ai_chat_capture import install_dataset

    source, target = _text(source_id), _text(install_id)
    if source is None or target is None:
        raise MaintenanceError("install_id_required")
    real = dry_run is False
    if real and confirm is not True:
        raise MaintenanceError("install_deactivation_unconfirmed")
    if not _present(conn):
        raise MaintenanceError("install_unknown")
    rows = _rows(conn, source)
    chosen = [row for row in rows if row[0] == target]
    if not chosen:
        raise MaintenanceError("install_unknown")
    if not _active(chosen[0][3]):
        raise MaintenanceError("install_not_active")
    active_before = sum(1 for row in rows if _active(row[3]))
    if active_before < 2:
        raise MaintenanceError("install_last_active")
    clock = _clock(conn)
    stamp = now or datetime.now(timezone.utc).isoformat()
    moved = conn.execute(f"UPDATE {TABLE} SET is_active=0, status='superseded', failure_reason=?, updated_at=? "
                         "WHERE install_id=? AND source_id=? AND is_active IS NOT 0",
                         (REASON, stamp, target, source)).rowcount
    if moved != 1:
        raise MaintenanceError("install_unknown")
    remaining = [row for row in _rows(conn, source) if _active(row[3])]
    dataset = install_dataset(conn, owner_id=owner_id, source_id=source) if _text(owner_id) else None
    return {
        "source_id": source, "install_id": target, "dry_run": not real, "deactivated": real,
        "active_before": active_before, "active_after": len(remaining),
        # ``evidence._source_posture`` reads exactly one live install; more (or a stale status) stays unknown.
        "posture_resolvable_after": len(remaining) == 1 and remaining[0][2] in LIVE and remaining[0][3] == 1,
        # Whether an owner receipt over this source's pre-stamp rows can then certify a dataset
        # (``ai_chat_capture.install_dataset``: one live install, and every install of the source
        # for this owner, retired ones included, scoped to the same concrete dataset).
        "receipt_dataset_certifiable_after": dataset is not None,
        **clock,
        # For the route's in-process cleanup after a commit; never returned to the caller.
        "_scope_key": chosen[0][1],
    }


def forget_runtime_handle(*, scope_key: str, source_id: str) -> None:
    """Drop the retired row's in-process runtime handle, as ``install_service._supersede_install`` does.

    The handle is dropped, not uninstalled: the process registers one parser and mapper per source id whatever
    the scope, and the install that stays uses the same registration.
    """
    from . import install_service

    with install_service._LOCK:
        install_service._ACTIVE_HANDLES.pop((scope_key, source_id), None)
