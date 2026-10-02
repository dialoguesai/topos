"""The node's own schedule for iMessage syncs: since-last, at a time the owner picks.

The owner turns it on in the app (a daily time, optionally every N hours from
it); the node keeps the setting in ``local_sync_schedules`` and a background
loop enqueues a since-last sync when a slot comes due. Each run goes through
``enqueue_local_sync_blocking``, the same door as the "Sync now" button, so it
gets the same in-flight guard, the same enrolled-dataset guard and the same
receipt, and a run that finds no trustworthy checkpoint stops at its plan
(``needs_confirmation``) instead of importing anything.

Why the node and not a LaunchAgent. The node already runs while the app is
open, it is the only process with Full Disk Access to chat.db (granted to the
app that launches it), and the sync must run on its job lane anyway. A
LaunchAgent would add a second installed artefact, a second process that needs
Full Disk Access of its own, and would still have to wake the node. What the
node cannot do: run while it is stopped. A slot missed that way runs once at
the next start (only the most recent missed slot, never a backlog of them).

What the schedule never does:

- move the enrollment's source clock. Its table is not one of the tables the
  clock watches, and a sync's receipt only updates the enrolled source row's
  ``last_sync_at``/``last_error``, which the clock ignores;
- take the write gate on the event loop. The loop only sleeps; every read and
  write happens in a worker thread with that thread's own connection;
- hold the gate for long. A scheduled run uses small batches with a pause
  between them (``SCHEDULED_BATCH_SIZE``, ``SCHEDULED_PAUSE_SECONDS``), so a
  recipient search waiting on the gate gets in between batches;
- start while a sync of the dataset is queued or running. That slot is recorded
  as ``skipped``, and the run already going covers it.

After a settled iMessage run that imported rows, and on its own tick when one is due, the loop hands
over to the owner's standing attestation (``permissions_v2.imessage_standing``), if the owner made one:
it proves the owner's own new messages and keeps the existing proof current. That moves the protection
clock (a proof publication does), never the source clock.

``TOPOS_LOCAL_SYNC_SCHEDULER=off`` keeps the loop from starting at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.ingestion.local_sync_schedule")

TABLE = "local_sync_schedules"
#: Sources the node schedules. Signal needs a key the node may not hold.
SUPPORTED_SCHEDULE_SOURCES = ("imessage",)
DEFAULT_DAILY_TIME = "03:00"
#: Scheduled runs: small batches, a pause between them. A manual sync keeps the
#: large default batch, because someone is watching it.
SCHEDULED_BATCH_SIZE = 500
SCHEDULED_PAUSE_SECONDS = 2.0
#: How often the loop looks for a due slot, and how long it waits after start.
TICK_SECONDS = 60.0
STARTUP_DELAY_SECONDS = 90.0

STATUS_RUNNING = "running"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
#: The statuses a finished run settles to come from the sync's own outcome:
#: imported, up_to_date, needs_confirmation.

_REQUIRED_COLUMNS = frozenset({
    "dataset_id", "source_id", "enabled", "daily_time", "interval_hours", "timezone",
    "last_slot_at", "last_run_at", "last_status", "last_error", "last_job_id",
    "last_result_json", "updated_at",
})
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_OFF_VALUES = ("0", "false", "off", "no")


def scheduler_enabled() -> bool:
    return os.environ.get("TOPOS_LOCAL_SYNC_SCHEDULER", "on").strip().lower() not in _OFF_VALUES


def scheduled_sync_options() -> Dict[str, Any]:
    """The options every scheduled run is enqueued with."""
    return {
        "mode": "since_last",
        "trigger": "schedule",
        "batch_size": SCHEDULED_BATCH_SIZE,
        "pause_seconds": SCHEDULED_PAUSE_SECONDS,
    }


# --- storage ---------------------------------------------------------------


def _table_ready(conn: Any) -> bool:
    cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall() if r[1]}
    return _REQUIRED_COLUMNS <= cols


def ensure_schedule_table(conn: Any) -> None:
    """Create the table, skipping the gate once it is there (a PRAGMA probe first)."""
    if _table_ready(conn):
        return
    with with_db_write():
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                dataset_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                daily_time TEXT NOT NULL DEFAULT '{DEFAULT_DAILY_TIME}',
                interval_hours INTEGER,
                timezone TEXT,
                last_slot_at TEXT,
                last_run_at TEXT,
                last_status TEXT,
                last_error TEXT,
                last_job_id TEXT,
                last_result_json TEXT,
                updated_at TEXT,
                PRIMARY KEY (dataset_id, source_id)
            )
            """
        )
        commit_connection(conn)


_SELECT = (
    f"SELECT dataset_id, source_id, enabled, daily_time, interval_hours, timezone, last_slot_at, "
    f"last_run_at, last_status, last_error, last_job_id, last_result_json, updated_at FROM {TABLE}"
)


def _row(row: Any) -> Dict[str, Any]:
    keys = (
        "dataset_id", "source_id", "enabled", "daily_time", "interval_hours", "timezone",
        "last_slot_at", "last_run_at", "last_status", "last_error", "last_job_id",
        "last_result_json", "updated_at",
    )
    out = dict(zip(keys, tuple(row)))
    out["enabled"] = bool(out["enabled"])
    try:
        out["last_result"] = json.loads(out.pop("last_result_json") or "null")
    except (TypeError, ValueError):
        out["last_result"] = None
    return out


def get_schedule(conn: Any, dataset_id: str, source_id: str) -> Optional[Dict[str, Any]]:
    """The stored schedule, or None. Read-only: never creates the table."""
    if conn is None or not _table_ready(conn):
        return None
    row = conn.execute(f"{_SELECT} WHERE dataset_id = ? AND source_id = ?", (dataset_id, source_id)).fetchone()
    return _row(row) if row else None


# --- time --------------------------------------------------------------------


def _local_zone() -> tzinfo:
    """The node's own zone: the IANA zone /etc/localtime names, else a fixed offset."""
    try:
        from zoneinfo import ZoneInfo

        target = os.path.realpath("/etc/localtime")
        marker = "zoneinfo" + os.sep
        if marker in target:
            return ZoneInfo(target.split(marker, 1)[1])
    except Exception:  # noqa: BLE001 — any failure falls back below
        pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _zone(name: Optional[str]) -> tzinfo:
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — validated on write; a zone that vanished falls back
            logger.warning("schedule timezone %r not found; using the node's own", name)
    return _local_zone()


def validate_timezone(name: Optional[str]) -> Optional[str]:
    """An IANA zone name, or None for the node's own zone. Raises ValueError otherwise."""
    if name is None:
        return None
    text = str(name).strip()
    if not text:
        return None
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(text)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"unknown timezone: {text}") from exc
    return text


def validate_daily_time(value: Any) -> str:
    text = str(value or "").strip()
    if not _TIME_RE.match(text):
        raise ValueError("daily_time must be HH:MM, 24-hour (e.g. 03:00)")
    return text


def validate_interval_hours(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        hours = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("interval_hours must be a whole number of hours from 1 to 24") from exc
    if isinstance(value, bool) or not 1 <= hours <= 24:
        raise ValueError("interval_hours must be a whole number of hours from 1 to 24")
    return None if hours == 24 else hours


def slot_times(daily_time: str, interval_hours: Optional[int]) -> List[Tuple[int, int]]:
    """The wall-clock times a day's runs fall on: the daily time, then every N hours from it.

    The pattern restarts from the daily time each day, so "03:00 every 5 hours"
    is 03:00, 08:00, 13:00, 18:00 and 23:00, then 03:00 again.
    """
    match = _TIME_RE.match(daily_time or "") or _TIME_RE.match(DEFAULT_DAILY_TIME)
    base = int(match.group(1)) * 60 + int(match.group(2))
    if not interval_hours:
        return [divmod(base, 60)]
    step = int(interval_hours) * 60
    minutes = {(base + k * step) % 1440 for k in range(0, 1440 // step + 1) if k * step < 1440}
    return sorted(divmod(m, 60) for m in minutes)


def _slots_on(day: date, times: List[Tuple[int, int]], zone: tzinfo) -> List[datetime]:
    return [datetime(day.year, day.month, day.day, h, m, tzinfo=zone) for h, m in times]


def latest_slot(schedule: Dict[str, Any], now: datetime) -> Optional[datetime]:
    """The most recent slot at or before ``now`` (UTC), or None."""
    zone = _zone(schedule.get("timezone"))
    times = slot_times(str(schedule.get("daily_time") or DEFAULT_DAILY_TIME), schedule.get("interval_hours"))
    today = now.astimezone(zone).date()
    candidates = [
        slot.astimezone(timezone.utc)
        for day in (today - timedelta(days=1), today)
        for slot in _slots_on(day, times, zone)
    ]
    due = [slot for slot in candidates if slot <= now]
    return max(due) if due else None


def next_slot(schedule: Dict[str, Any], now: datetime) -> Optional[datetime]:
    """The first slot strictly after ``now`` (UTC)."""
    zone = _zone(schedule.get("timezone"))
    times = slot_times(str(schedule.get("daily_time") or DEFAULT_DAILY_TIME), schedule.get("interval_hours"))
    today = now.astimezone(zone).date()
    candidates = [
        slot.astimezone(timezone.utc)
        for day in (today, today + timedelta(days=1))
        for slot in _slots_on(day, times, zone)
    ]
    ahead = [slot for slot in candidates if slot > now]
    return min(ahead) if ahead else None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- writes ------------------------------------------------------------------


def put_schedule(
    conn: Any,
    dataset_id: str,
    source_id: str,
    changes: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Save the owner's schedule for one dataset; returns ``describe_schedule``.

    ``changes`` may carry ``enabled``, ``daily_time``, ``interval_hours`` and
    ``timezone``; a key left out keeps its stored value. Raises ValueError on a
    bad value, an unsupported source, or (for iMessage) a dataset the sync
    itself would refuse because another is enrolled.

    Turning the schedule on, or moving its time, marks the slot just passed as
    done, so the first run is the next slot rather than one right now.
    """
    if source_id not in SUPPORTED_SCHEDULE_SOURCES:
        raise ValueError(f"automatic sync is not available for {source_id}")
    if not isinstance(changes, dict):
        raise ValueError("sync_schedule must be an object")
    unknown = set(changes) - {"enabled", "daily_time", "interval_hours", "timezone"}
    if unknown:
        raise ValueError(f"unknown sync_schedule fields: {', '.join(sorted(unknown))}")
    now = now or _utcnow()
    current = get_schedule(conn, dataset_id, source_id) or {
        "enabled": False,
        "daily_time": DEFAULT_DAILY_TIME,
        "interval_hours": None,
        "timezone": None,
        "last_slot_at": None,
    }
    enabled = bool(changes["enabled"]) if "enabled" in changes else bool(current["enabled"])
    daily_time = validate_daily_time(changes["daily_time"]) if "daily_time" in changes else current["daily_time"]
    interval_hours = (
        validate_interval_hours(changes["interval_hours"]) if "interval_hours" in changes else current["interval_hours"]
    )
    tz_name = validate_timezone(changes["timezone"]) if "timezone" in changes else current["timezone"]

    if enabled and source_id == "imessage":
        from .local_sync import enrolled_dataset_refusal

        refusal = enrolled_dataset_refusal(conn, dataset_id, None)
        if refusal is not None:
            raise ValueError(refusal["error"])

    timing_moved = (daily_time, interval_hours, tz_name) != (
        current["daily_time"], current["interval_hours"], current["timezone"]
    )
    last_slot_at = current.get("last_slot_at")
    if enabled and (not current["enabled"] or timing_moved):
        passed = latest_slot(
            {"daily_time": daily_time, "interval_hours": interval_hours, "timezone": tz_name}, now
        )
        last_slot_at = passed.isoformat() if passed else None

    ensure_schedule_table(conn)
    with with_db_write():
        conn.execute(
            f"""
            INSERT INTO {TABLE} (dataset_id, source_id, enabled, daily_time, interval_hours, timezone,
                                 last_slot_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_id, source_id) DO UPDATE SET
                enabled = excluded.enabled,
                daily_time = excluded.daily_time,
                interval_hours = excluded.interval_hours,
                timezone = excluded.timezone,
                last_slot_at = excluded.last_slot_at,
                updated_at = excluded.updated_at
            """,
            (dataset_id, source_id, 1 if enabled else 0, daily_time, interval_hours, tz_name,
             last_slot_at, now.isoformat()),
        )
        commit_connection(conn)
    return describe_schedule(conn, dataset_id, source_id, now=now)


def _record_run(
    conn: Any,
    schedule: Dict[str, Any],
    *,
    status: str,
    now: datetime,
    slot: Optional[datetime] = None,
    job_id: Optional[str] = None,
    error: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    """Stamp one slot's outcome. ``slot`` None keeps the slot (a settling run)."""
    with with_db_write():
        conn.execute(
            f"""
            UPDATE {TABLE}
            SET last_slot_at = COALESCE(?, last_slot_at),
                last_run_at = CASE WHEN ? IS NULL THEN last_run_at ELSE ? END,
                last_status = ?,
                last_error = ?,
                last_job_id = COALESCE(?, last_job_id),
                last_result_json = ?,
                updated_at = ?
            WHERE dataset_id = ? AND source_id = ?
            """,
            (
                slot.isoformat() if slot else None,
                slot.isoformat() if slot else None,
                now.isoformat(),
                status,
                error,
                job_id,
                json.dumps(result) if result is not None else None,
                now.isoformat(),
                schedule["dataset_id"],
                schedule["source_id"],
            ),
        )
        commit_connection(conn)


# --- reading a run's outcome -------------------------------------------------


def _plan_summary(plan: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(plan, dict):
        return None
    keys = ("reason", "trusted", "start_rowid", "to_import", "messages", "spam_skipped",
            "already_stored", "first_at", "last_at", "chat_db_max_rowid")
    return {key: plan.get(key) for key in keys if key in plan}


def settle_job(job: Optional[Dict[str, Any]]) -> Optional[Tuple[str, Optional[str], Dict[str, Any]]]:
    """``(status, error, result)`` for a finished job, or None while it is still going."""
    if job is None:
        return STATUS_FAILED, "The scheduled sync's job record is gone.", {}
    status = str(job.get("status") or "")
    detail = job.get("detail") if isinstance(job.get("detail"), dict) else {}
    if status == "done":
        sync = detail.get("sync") if isinstance(detail.get("sync"), dict) else {}
        processed = int(sync.get("records_processed") or detail.get("records_processed") or 0)
        outcome = str(sync.get("outcome") or detail.get("outcome") or ("imported" if processed else "up_to_date"))
        result = {
            "outcome": outcome,
            "records_processed": processed,
            "records_skipped": int(sync.get("records_skipped") or detail.get("records_skipped") or 0),
            "records_held": int(sync.get("records_held") or detail.get("records_held") or 0),
        }
        plan = _plan_summary(sync.get("plan") or detail.get("plan"))
        if plan:
            result["plan"] = plan
        return outcome, None, result
    if status == "failed":
        error = str(detail.get("error") or "The scheduled sync failed.")
        return STATUS_FAILED, error, {}
    return None


def _settle_running(conn: Any, schedule: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """If this schedule's last run has finished, record how; returns the schedule as stored."""
    if schedule.get("last_status") != STATUS_RUNNING or not schedule.get("last_job_id"):
        return schedule
    from ..pipeline.job_store import get_job

    settled = settle_job(get_job(conn, str(schedule["last_job_id"])))
    if settled is None:
        return schedule
    status, error, result = settled
    _record_run(conn, schedule, status=status, now=now, error=error, result=result)
    if schedule.get("source_id") == "imessage":
        _prove_after_sync(str(schedule.get("dataset_id") or ""), status)
    return {**schedule, "last_status": status, "last_error": error, "last_result": result}


def _prove_after_sync(dataset_id: str, outcome: str) -> None:
    """After a settled iMessage sync: the owner's standing attestation proves what it imported.

    Owner decision 1 (1 Oct 2026, ``permissions_v2.imessage_standing``): with the owner's standing
    statement armed, the node enrolls every iMessage dataset that holds the owner's rows and refreshes
    each enrollment, so no proof ages out and no owner command is needed. Without the statement, or with
    the permissions beta off, this does nothing. It never raises into the tick, and logs codes only.
    """
    try:
        from ..permissions_v2.imessage_standing import after_scheduled_sync

        outcome_of_proof = after_scheduled_sync(dataset_id, outcome)
    except Exception as exc:  # noqa: BLE001 — the sync's own record stands
        logger.warning("[PIPELINE:SYNC] proof after sync failed: %s", type(exc).__name__)
        return
    if outcome_of_proof.get("ran"):
        logger.info("[PIPELINE:SYNC] proof after sync: outcome=%s refusal=%s",
                    outcome_of_proof.get("outcome"), outcome_of_proof.get("refusal"))


def _prove_when_due() -> Dict[str, Any]:
    """The scheduler's tick, beside the schedules: a standing-attestation run when one is due without a sync
    (just armed, a week since the last, or an hour after one that could not read). Never raises."""
    try:
        from ..permissions_v2.imessage_standing import run_if_due

        return run_if_due()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PIPELINE:SYNC] scheduled proof check failed: %s", type(exc).__name__)
        return {"ran": False, "reason": type(exc).__name__}


def describe_schedule(
    conn: Any, dataset_id: str, source_id: str, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """The schedule and its last run, for the settings screen. Read-only.

    A run still marked running is looked up (not written) so the screen shows
    how it ended even before the loop's next tick records it.
    """
    now = now or _utcnow()
    supported = source_id in SUPPORTED_SCHEDULE_SOURCES
    stored = get_schedule(conn, dataset_id, source_id) if supported else None
    schedule = stored or {
        "enabled": False,
        "daily_time": DEFAULT_DAILY_TIME,
        "interval_hours": None,
        "timezone": None,
        "last_slot_at": None,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_job_id": None,
        "last_result": None,
    }
    status = schedule.get("last_status")
    error = schedule.get("last_error")
    result = schedule.get("last_result")
    if status == STATUS_RUNNING and schedule.get("last_job_id"):
        try:
            from ..pipeline.job_store import get_job

            settled = settle_job(get_job(conn, str(schedule["last_job_id"])))
        except Exception:  # noqa: BLE001 — a status read never fails the settings screen
            settled = None
        if settled is not None:
            status, error, result = settled
    upcoming = next_slot(schedule, now) if schedule.get("enabled") else None
    return {
        "supported": supported,
        "enabled": bool(schedule.get("enabled")),
        "daily_time": schedule.get("daily_time") or DEFAULT_DAILY_TIME,
        "interval_hours": schedule.get("interval_hours"),
        "timezone": schedule.get("timezone"),
        "effective_timezone": getattr(_zone(schedule.get("timezone")), "key", None),
        "next_run_at": upcoming.isoformat() if upcoming else None,
        "last_run_at": schedule.get("last_run_at"),
        "last_status": status,
        "last_error": error,
        "last_job_id": schedule.get("last_job_id"),
        "last_result": result,
        "scheduler_running": scheduler_enabled(),
    }


#: Sync modes this node understands, so the app can tell what it may send.
SYNC_MODES = ("since_last", "full_history", "1m", "3m", "6m", "1y", "5y", "custom")


def describe_sync_settings(
    conn: Any, dataset_id: str, source_id: str, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """The sync fields the settings screen shows for a schedulable source; {} otherwise.

    Read-only, no chat.db: the checkpoint (where the next since-last sync
    starts) and the schedule with its last run.
    """
    if source_id not in SUPPORTED_SCHEDULE_SOURCES or conn is None or not dataset_id:
        return {}
    from .local_sync import describe_imessage_checkpoint

    try:
        return {
            "sync_modes": list(SYNC_MODES),
            "sync_checkpoint": describe_imessage_checkpoint(conn, dataset_id),
            "sync_schedule": describe_schedule(conn, dataset_id, source_id, now=now),
        }
    except Exception as exc:  # noqa: BLE001 — extras never fail the settings read they ride on
        logger.warning("sync settings describe failed source_id=%s: %s", source_id, exc)
        return {"sync_modes": list(SYNC_MODES)}


# --- the tick -----------------------------------------------------------------


def run_schedule_tick(
    conn: Any,
    *,
    now: Optional[datetime] = None,
    enqueue: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """One pass: settle finished runs, then enqueue a since-last sync for each due slot.

    Runs in a worker thread with that thread's own connection. Never creates the
    table: a node where nobody turned the schedule on reads one PRAGMA a minute.
    """
    if enqueue is None:
        from .local_sync_jobs import enqueue_local_sync_blocking as enqueue
    now = now or _utcnow()
    summary: Dict[str, Any] = {"enqueued": 0, "skipped": 0, "failed": 0, "settled": 0}
    if conn is None or not _table_ready(conn):
        return summary
    rows = [_row(r) for r in conn.execute(f"{_SELECT} WHERE enabled = 1").fetchall()]
    for schedule in rows:
        before = schedule.get("last_status")
        schedule = _settle_running(conn, schedule, now)
        if before == STATUS_RUNNING and schedule.get("last_status") != STATUS_RUNNING:
            summary["settled"] += 1
        slot = latest_slot(schedule, now)
        if slot is None:
            continue
        last = _parse_iso(schedule.get("last_slot_at"))
        if last is not None and last >= slot:
            continue
        try:
            outcome = enqueue(
                conn,
                source_id=schedule["source_id"],
                dataset_id=schedule["dataset_id"],
                sync_options=scheduled_sync_options(),
            )
        except Exception as exc:  # noqa: BLE001 — one bad schedule must not stop the others
            outcome = {"status": "error", "error": str(exc)}
        if outcome.get("status") != "ok":
            _record_run(conn, schedule, status=STATUS_FAILED, now=now, slot=slot,
                        error=str(outcome.get("error") or "Could not start the sync."), result={})
            summary["failed"] += 1
            logger.warning(
                "[PIPELINE:SYNC] scheduled sync not started: source_id=%s error=%s",
                schedule["source_id"], outcome.get("error"),
            )
        elif outcome.get("already_running"):
            active = str(outcome.get("job_id") or "") or None
            if schedule.get("last_status") == STATUS_RUNNING and active == schedule.get("last_job_id"):
                # Our own previous run is still going: this slot is covered by it,
                # and it stays the run whose outcome gets recorded.
                _record_run(conn, schedule, status=STATUS_RUNNING, now=now, slot=slot, job_id=active,
                            result=schedule.get("last_result") or {})
            else:
                _record_run(conn, schedule, status=STATUS_SKIPPED, now=now, slot=slot, job_id=active,
                            result={"reason": "A sync of this dataset was already running."})
            summary["skipped"] += 1
        else:
            _record_run(conn, schedule, status=STATUS_RUNNING, now=now, slot=slot,
                        job_id=str(outcome.get("job_id")), result={})
            summary["enqueued"] += 1
            logger.info(
                "[PIPELINE:SYNC] scheduled sync enqueued: source_id=%s job_id=%s",
                schedule["source_id"], outcome.get("job_id"),
            )
    return summary


async def run_scheduler_loop(
    conn_factory: Callable[[], Any],
    *,
    tick_seconds: float = TICK_SECONDS,
    startup_delay_seconds: float = STARTUP_DELAY_SECONDS,
) -> None:
    """Forever: wait, tick in a worker thread, start the job worker if a run was queued.

    The coroutine itself only sleeps and awaits; the connection is fetched inside
    the worker thread (thread-local), never on the loop.
    """
    from ..pipeline.job_runner import start_pipeline_worker

    def _tick() -> Dict[str, Any]:
        summary = run_schedule_tick(conn_factory())
        # Not inside run_schedule_tick: a node nobody scheduled has no schedule table, and the owner's
        # standing attestation still wants its first run (and its weekly one) there.
        summary["proof"] = _prove_when_due()
        return summary

    await asyncio.sleep(startup_delay_seconds)
    while True:
        try:
            summary = await asyncio.to_thread(_tick)
            if summary.get("enqueued"):
                start_pipeline_worker(conn_factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop outlives a bad tick
            logger.warning("local sync scheduler tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(tick_seconds)


def start_scheduler(spawn: Callable[..., Any], conn_factory: Callable[[], Any]) -> bool:
    """App startup: start the loop through ``spawn`` (the app's tracked-task helper)."""
    if not scheduler_enabled():
        logger.info("local sync scheduler off (TOPOS_LOCAL_SYNC_SCHEDULER)")
        return False
    spawn(run_scheduler_loop(conn_factory), name="local-sync-scheduler")
    return True


__all__ = [
    "SCHEDULED_BATCH_SIZE",
    "SCHEDULED_PAUSE_SECONDS",
    "SUPPORTED_SCHEDULE_SOURCES",
    "SYNC_MODES",
    "TABLE",
    "describe_schedule",
    "describe_sync_settings",
    "ensure_schedule_table",
    "get_schedule",
    "latest_slot",
    "next_slot",
    "put_schedule",
    "run_schedule_tick",
    "run_scheduler_loop",
    "scheduled_sync_options",
    "scheduler_enabled",
    "settle_job",
    "slot_times",
    "start_scheduler",
]
