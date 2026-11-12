"""Turning iMessage and Signal Desktop on from the owner socket, by source id.

The coding-tool sources got ``preview`` / ``enable`` / ``pause`` / ``resume`` / ``sync`` doors with
the AI-tool on-ramp (``ai_tool_sync.py``, CONTRACTS §2.8). The two messenger sources never did: the
web app turns them on through the control plane, so a node with no account (local first, 5 Nov
2026) had no way to read Messages or Signal at all, and ``topos-node sources enable imessage``
answered 404. This module is the same five verbs for ``imessage`` and ``signal``; the doors in
``api/owner_sources.py`` call it and the sync itself is unchanged (``local_sync.run_imessage_sync``
and ``run_signal_sync`` through ``local_sync_jobs``).

What is different from a coding tool, and why:

* **Consent.** These are other people's words. ``preview``, ``enable``, ``resume`` and ``sync``
  are *dangerous* doors in the owner-socket census (``owner_door_routes``): they need the Topos app
  or a person on a terminal, never a flag a coding agent with a shell could pass. ``pause`` stays
  open: it only stops reading.
* **Readiness is checked before anything is written.** macOS gives Messages only to a process
  with Full Disk Access; Signal's database needs a SQLCipher binding and the key in the login
  Keychain. ``readiness`` says which of those is missing as a code and a sentence a person can
  act on, and ``enable`` refuses with it rather than queueing a sync that can only fail.
  ``readiness`` never asks the Keychain for anything: listing sources must not raise a prompt.
* **Signal is not scheduled.** ``local_sync_jobs`` marks it unschedulable because every read asks
  the Keychain for Signal's key. Enabling it runs one sync; later ones are the person's
  ``sources sync signal``.

Every answer is counts, flags, codes and stamps. No message text, sender or chat name leaves.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from ..runtime_platform import INSTALL_PYTHON
from ..sources.setup_catalog import BY_ID, MESSENGER_SOURCE_IDS

logger = logging.getLogger("topos.ingestion.messenger_setup")

ENGINE_CONFIG_KEY = "messenger_sources"
BACKFILL_CHOICES = ("all", "30d")

# Refusal codes (also the CLI's keys for the sentences it prints: the door's own text never
# reaches a terminal, ``cli/sources_cmd.py``).
MACOS_ONLY = "macos_only"
FULL_DISK_ACCESS_NEEDED = "full_disk_access_needed"
MESSAGES_NOT_FOUND = "messages_not_found"
SIGNAL_NOT_FOUND = "signal_not_found"
SIGNAL_READER_MISSING = "signal_reader_missing"
SIGNAL_KEY_UNAVAILABLE = "signal_key_unavailable"
SOURCE_NOT_ENABLED = "source_not_enabled"

SENTENCES = {
    MACOS_ONLY: "Messages can only be read on a Mac.",
    FULL_DISK_ACCESS_NEEDED: (
        "macOS is not letting Topos read Messages. Give Topos Full Disk Access in System Settings "
        "(Privacy & Security), restart Topos, then try again."
    ),
    MESSAGES_NOT_FOUND: "There is no Messages history on this Mac to read.",
    SIGNAL_NOT_FOUND: "Signal Desktop has no history on this computer to read.",
    SIGNAL_READER_MISSING: (
        "This install of Topos cannot open Signal's database. Reinstall it with the Signal reader: "
        f"uv tool install --force --python {INSTALL_PYTHON} 'topos-node[signal]'"
    ),
    SIGNAL_KEY_UNAVAILABLE: (
        "Topos could not get Signal's key from the Keychain, or the key did not open Signal's "
        "database. Allow the Keychain request when macOS shows it, then try again."
    ),
    SOURCE_NOT_ENABLED: "This source isn't enabled on this Topos. Enable it first.",
}


class MessengerRefused(Exception):
    """A door refusal: ``code`` the result code, ``status`` the HTTP status, ``message`` the plain sentence."""

    def __init__(self, code: str, message: Optional[str] = None, *, status: int = 409) -> None:
        self.code = code
        self.message = message or SENTENCES.get(code) or code
        self.status = status
        super().__init__(self.message)


def _messenger(source_id: str) -> str:
    sid = str(source_id or "").strip()
    if sid not in MESSENGER_SOURCE_IDS:
        raise ValueError(f"not a messenger source: {source_id}")
    return sid


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(unix_ts: Optional[float]) -> Optional[str]:
    if unix_ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(unix_ts), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# --- what the backfill choice means ------------------------------------------------


def backfill_sync_options(source_id: str, backfill: str) -> Dict[str, Any]:
    """The first sync's options: the last 30 days, or everything on this computer.

    iMessage's ``full_history`` is its unbounded mode; Signal has no modes and reads from the start
    when no window is named.
    """
    choice = str(backfill or "30d").strip().lower()
    if choice not in BACKFILL_CHOICES:
        raise ValueError(f"backfill must be one of: {', '.join(BACKFILL_CHOICES)}")
    if choice == "30d":
        return {"mode": "1m"}
    return {"mode": "full_history"} if _messenger(source_id) == "imessage" else {"mode": "all"}


def _window_start_unix(backfill: str) -> Optional[float]:
    if str(backfill or "30d").strip().lower() == "30d":
        return (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    return None


# --- readiness: can this process read the source at all? --------------------------------


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _imessage_readiness() -> Optional[str]:
    if not _is_macos():
        return MACOS_ONLY
    from .sources.imessage_reader import get_chat_db_path

    path = get_chat_db_path()
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        # A Mac whose owner never opened Messages has no chat.db. A Mac without Full Disk Access
        # cannot even list the folder, and says so with EPERM rather than ENOENT.
        try:
            os.listdir(str(path.parent))
        except FileNotFoundError:
            return MESSAGES_NOT_FOUND
        except OSError:
            return FULL_DISK_ACCESS_NEEDED
        return MESSAGES_NOT_FOUND
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES):
            return FULL_DISK_ACCESS_NEEDED
        logger.warning("imessage readiness: chat.db could not be opened (%s)", type(exc).__name__)
        return FULL_DISK_ACCESS_NEEDED
    try:
        os.read(fd, 16)
    except OSError:
        return FULL_DISK_ACCESS_NEEDED
    finally:
        os.close(fd)
    return None


def _signal_readiness() -> Optional[str]:
    from .sources.signal_reader import get_signal_paths, sqlcipher_available

    _config, db_path = get_signal_paths()
    try:
        if not db_path.exists():
            return SIGNAL_NOT_FOUND
    except OSError:
        return SIGNAL_NOT_FOUND
    if not sqlcipher_available():
        return SIGNAL_READER_MISSING
    return None


def readiness(source_id: str) -> Dict[str, Any]:
    """``{"ready": bool, "code": str | None, "message": str | None}``. Opens no database, reads no
    message and asks the Keychain for nothing."""
    sid = _messenger(source_id)
    code = _imessage_readiness() if sid == "imessage" else _signal_readiness()
    return {"ready": code is None, "code": code, "message": SENTENCES.get(code) if code else None}


def _require_ready(source_id: str) -> None:
    state = readiness(source_id)
    if not state["ready"]:
        raise MessengerRefused(str(state["code"]))


# --- per-source state ----------------------------------------------------------------


def _load_states(conn: Any) -> Dict[str, Dict[str, Any]]:
    from ..core.state import get_engine_config_value

    try:
        data = json.loads(get_engine_config_value(conn, ENGINE_CONFIG_KEY) or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _set_state(conn: Any, source_id: str, **changes: Any) -> Dict[str, Any]:
    from ..core.state import set_engine_config_value

    states = _load_states(conn)
    current = {**dict(states.get(source_id) or {}), **changes}
    states[source_id] = current
    set_engine_config_value(conn, ENGINE_CONFIG_KEY, json.dumps(states, sort_keys=True))
    return current


def messenger_state(conn: Any, source_id: str, *, scope: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """What the doors and ``sources list`` read: installed, enabled, the backfill choice, the receipts."""
    from .ai_tool_sync import _install_active, _settings_row, resolve_owner_scope

    sid = _messenger(source_id)
    scope = scope or resolve_owner_scope(conn)
    dataset_id = scope["dataset_id"] if scope else None
    installed = _install_active(conn, sid, scope)
    settings = _settings_row(conn, dataset_id, sid)
    flags = _load_states(conn).get(sid) or {}
    enabled = bool(installed and settings and settings["enabled"])
    return {
        "source_id": sid,
        "display_name": BY_ID[sid].name,
        "kind": BY_ID[sid].kind,
        "dataset_id": dataset_id,
        "installed": installed,
        "enabled": enabled,
        # Paused: turned on once, then switched off by the owner (the settings row is kept).
        "paused": bool(installed and settings and not settings["enabled"]),
        "backfill": flags.get("backfill"),
        "enabled_at": flags.get("enabled_at"),
        "last_sync_at": settings["last_sync_at"] if settings else None,
        "last_error": settings["last_error"] if settings else None,
    }


def describe_messenger_source(conn: Any, source_id: str) -> Dict[str, Any]:
    """The status block: the state, whether this process can read the source, and its schedule."""
    from .local_sync_jobs import runner_for
    from .local_sync_schedule import describe_schedule

    state = messenger_state(conn, source_id)
    runner = runner_for(state["source_id"])
    schedule = None
    if state["dataset_id"] and runner is not None and runner.schedulable:
        try:
            schedule = describe_schedule(conn, state["dataset_id"], state["source_id"])
        except Exception as exc:  # noqa: BLE001 -- a status read never fails on the schedule
            logger.debug("messenger describe: schedule unreadable (%s)", type(exc).__name__)
    return {**state, "schedule": schedule, "scheduled": bool(runner is not None and runner.schedulable),
            "readiness": readiness(source_id)}


# --- preview --------------------------------------------------------------------------


def _signal_counts(start_unix: Optional[float]) -> Dict[str, Any]:
    from .sources.signal_reader import count_signal_messages

    try:
        return count_signal_messages(start_unix=start_unix)
    except ImportError:
        raise MessengerRefused(SIGNAL_READER_MISSING) from None
    except FileNotFoundError:
        raise MessengerRefused(SIGNAL_NOT_FOUND) from None
    except ValueError as exc:
        # The reader's text names a path; the code and the sentence are what leave.
        logger.warning("signal preview: database not opened (%s)", type(exc).__name__)
        raise MessengerRefused(SIGNAL_KEY_UNAVAILABLE) from None


def _imessage_counts(conn: Any, dataset_id: Optional[str], start_unix: Optional[float]) -> Dict[str, Any]:
    from .local_sync import _resolve_exclude_spam
    from .sources.imessage_reader import inspect_imessage_backlog

    exclude_spam = _resolve_exclude_spam(None, db_conn=conn, dataset_id=dataset_id or "")
    try:
        backlog = inspect_imessage_backlog(0, exclude_spam=exclude_spam)
    except FileNotFoundError:
        raise MessengerRefused(MESSAGES_NOT_FOUND) from None
    except OSError:
        raise MessengerRefused(FULL_DISK_ACCESS_NEEDED) from None
    kept = len(backlog.dated)
    in_window = kept if start_unix is None else sum(
        1 for _rowid, when in backlog.dated if when is None or when >= start_unix
    )
    return {
        "messages": kept,
        "conversations": None,
        "in_window": in_window,
        "skipped_spam": backlog.spam,
        "oldest": backlog.first_at,
        "newest": backlog.last_at,
    }


def preview_messenger_source(conn: Any, source_id: str, *, backfill: str = "30d") -> Dict[str, Any]:
    """Counts only: how many messages, how many of them the chosen window would read, oldest and newest.

    Nothing is written. For Signal this opens the database, which is when macOS asks the person to
    let Topos use Signal's key.
    """
    from .ai_tool_sync import resolve_owner_scope

    sid = _messenger(source_id)
    backfill_sync_options(sid, backfill)  # validates the choice
    _require_ready(sid)
    start_unix = _window_start_unix(backfill)
    scope = resolve_owner_scope(conn)
    counts = (_imessage_counts(conn, scope["dataset_id"] if scope else None, start_unix)
              if sid == "imessage" else _signal_counts(start_unix))
    return {
        "source_id": sid,
        "backfill": str(backfill or "30d"),
        "messages": int(counts["messages"]),
        "in_window": int(counts["in_window"]),
        "conversations": counts.get("conversations"),
        "skipped_spam": int(counts.get("skipped_spam") or 0),
        "oldest": _iso(counts.get("oldest")),
        "newest": _iso(counts.get("newest")),
    }


# --- enable, pause, resume, sync ----------------------------------------------------------


def _owner_scope_or_refuse(conn: Any) -> Dict[str, str]:
    from .ai_tool_sync import NOT_PAIRED, NOT_PAIRED_SENTENCE, resolve_owner_scope

    scope = resolve_owner_scope(conn)
    if scope is None:
        raise MessengerRefused(NOT_PAIRED, NOT_PAIRED_SENTENCE)
    return scope


def _enqueue(conn: Any, source_id: str, dataset_id: str, options: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    from .local_sync_jobs import enqueue_local_sync_blocking

    enqueued = enqueue_local_sync_blocking(conn, source_id=source_id, dataset_id=dataset_id, sync_options=options)
    if enqueued.get("status") != "ok":
        raise MessengerRefused(str(enqueued.get("code") or "sync_not_started"),
                               str(enqueued.get("error") or "The sync could not be started."))
    return enqueued.get("job_id"), bool(enqueued.get("already_running"))


def _set_schedule(conn: Any, source_id: str, dataset_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
    from .local_sync_jobs import runner_for
    from .local_sync_schedule import put_schedule

    runner = runner_for(source_id)
    if runner is None or not runner.schedulable:
        return None
    changes: Dict[str, Any] = {"enabled": enabled}
    if enabled:
        changes["interval_hours"] = 1
    return put_schedule(conn, dataset_id, source_id, changes)


def enable_messenger_source(conn: Any, source_id: str, *, backfill: str = "30d") -> Dict[str, Any]:
    """Install row, settings row, the hourly schedule where the source has one, and the first sync.

    Before the first write, in this order: an identity (a local one counts), the disk floor, the
    hardening pass over ``~/.topos`` and the owner-key gate the coding tools use, whether this
    process can read the source at all, and for Signal that its key opens the database (the moment
    macOS asks). Idempotent: a second enable keeps the install and joins a sync already running.
    Raises ``MessengerRefused`` with the plain sentence; nothing is written then.
    """
    from .ai_tool_sync import (
        DISK_FLOOR,
        OWNER_KEY_NOT_PERSISTED,
        OWNER_KEY_NOT_PERSISTED_SENTENCE,
        _harden_topos_dir,
        _install_scope,
        _owner_key_persisted,
        disk_floor_refusal,
    )

    sid = _messenger(source_id)
    options = backfill_sync_options(sid, backfill)
    scope = _owner_scope_or_refuse(conn)
    floor = disk_floor_refusal(conn)
    if floor is not None:
        raise MessengerRefused(DISK_FLOOR, floor)
    hardening = _harden_topos_dir()
    persisted = _owner_key_persisted()
    if persisted is None and hardening is not None and "owner_key_persisted" in hardening:
        persisted = bool(hardening.get("owner_key_persisted"))
    if persisted is False:
        raise MessengerRefused(OWNER_KEY_NOT_PERSISTED, OWNER_KEY_NOT_PERSISTED_SENTENCE)
    _require_ready(sid)
    dataset_id = scope["dataset_id"]
    if sid == "signal":
        _signal_counts(None)
    else:
        from .local_sync import enrolled_dataset_refusal

        refusal = enrolled_dataset_refusal(conn, dataset_id, None)
        if refusal is not None:
            raise MessengerRefused(str(refusal.get("code") or "dataset_not_enrolled"), str(refusal.get("error")))

    from ..sources import install_service
    from ..sources.registry import BUNDLED_REGISTRY, REGISTRY
    from ..storage.source_settings import put_source_settings

    definition = BUNDLED_REGISTRY.get(sid) or REGISTRY[sid]
    record = install_service.install_source(
        source_definition_json=definition.to_dict(), scope=_install_scope(scope)
    )
    put_source_settings(conn, dataset_id, sid, enabled=True)
    _set_state(conn, sid, backfill=str(backfill or "30d"), enabled_at=_utcnow_iso())
    schedule = _set_schedule(conn, sid, dataset_id, True)
    job_id, already_running = _enqueue(conn, sid, dataset_id, options)
    state = messenger_state(conn, sid, scope=scope)
    return {
        "status": "ok",
        "source_id": sid,
        "dataset_id": dataset_id,
        "enabled": state["enabled"],
        "paused": state["paused"],
        "backfill": state["backfill"],
        "install_id": record.install_id,
        "schedule": schedule,
        "scheduled": schedule is not None,
        "backfill_job_id": job_id,
        "backfill_already_running": already_running,
    }


def set_messenger_enabled(conn: Any, source_id: str, enabled: bool) -> Dict[str, Any]:
    """``pause`` (off) and ``resume`` (on) for a source that was enabled once. What was read stays."""
    from ..storage.source_settings import put_source_settings

    sid = _messenger(source_id)
    scope = _owner_scope_or_refuse(conn)
    state = messenger_state(conn, sid, scope=scope)
    if not state["installed"]:
        raise MessengerRefused(SOURCE_NOT_ENABLED)
    if enabled:
        _require_ready(sid)
    put_source_settings(conn, scope["dataset_id"], sid, enabled=bool(enabled))
    try:
        _set_schedule(conn, sid, scope["dataset_id"], bool(enabled))
    except ValueError as exc:
        raise MessengerRefused("schedule_refused", str(exc)) from None
    state = messenger_state(conn, sid, scope=scope)
    return {"status": "ok", "source_id": sid, "enabled": state["enabled"], "paused": state["paused"]}


def sync_messenger_source(conn: Any, source_id: str) -> Dict[str, Any]:
    """Queue one since-last sync of an enabled source."""
    sid = _messenger(source_id)
    scope = _owner_scope_or_refuse(conn)
    state = messenger_state(conn, sid, scope=scope)
    if not state["enabled"]:
        raise MessengerRefused(SOURCE_NOT_ENABLED)
    _require_ready(sid)
    job_id, already_running = _enqueue(conn, sid, scope["dataset_id"], {"mode": "since_last"})
    return {"status": "ok", "source_id": sid, "job_id": job_id, "already_running": already_running}
