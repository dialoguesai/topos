"""The AI-coding-tool sync lane: Claude Code, Codex and Cursor transcripts into ``ai_chat_messages``.

CONTRACTS.md §2.8. One generic runner, ``run_ai_coding_tool_sync(tool, ...)``, behind three
``run_<tool>_sync`` entries in the local-sync runner table (``local_sync_jobs.LOCAL_SYNC_RUNNERS``).
A run: hold the dataset (``exclusive_sync``), resolve the OWNER'S dataset from the bound identity
(never from the caller), refuse unless the source is enabled in the active profile and not paused,
refuse below the disk floor, read batches through the tool's reader (§2.7b), write the raw rows,
validate and parse with the registered parser, canonicalize through the canonical pipeline with
``writer_class="owner_app"`` and ``writer_dataset_id`` = the install dataset, mark the records the
reader flagged ``share_excluded`` by a post-write UPDATE, keep the project facet in its owner-only
table, hand ONLY the shareable rows to enrichment, ask the disclosure sweep, save the
``(dataset_id, schema_id)`` checkpoint, report progress, and return the reader's ledger.

The active profile is the database this node serves (``profiles.py``: the active Topos is the top
level of ``~/.topos``), so "enabled in the active profile" is answered from the served database:
an active install row for the identity's scope AND an enabled settings row, both of which only the
enable door (owner socket) writes. ``paused`` is a per-source flag in ``engine_config``; the sync
door and the scheduler refuse on it (it changes bytes, not display).

Nothing here edits ``storage/canonical/*``: every canonical write goes through
``canonical_pipeline.canonicalize_normalized_batch`` and ``run_post_canonical_pipeline``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .checkpoints.checkpoint_store import IngestionCheckpoint
from .checkpoints.sqlite_checkpoint_store import SqliteCheckpointStore
from .sources.base import RawRecord
from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.ingestion.ai_tool_sync")


@dataclass(frozen=True)
class ToolSpec:
    tool: str
    source_id: str
    schema_id: str
    display_name: str
    reader_module: str
    reader_name: str


TOOLS: Dict[str, ToolSpec] = {
    "claude_code": ToolSpec(
        "claude_code", "claude_code_sessions", "claude_code.transcript.v1", "Claude Code",
        "topos.ingestion.sources.claude_code_reader", "read_claude_code_batch",
    ),
    "codex": ToolSpec(
        "codex", "codex_sessions", "codex.transcript.v1", "Codex",
        "topos.ingestion.sources.codex_reader", "read_codex_batch",
    ),
    "cursor": ToolSpec(
        "cursor", "cursor_sessions", "cursor.transcript.v1", "Cursor",
        "topos.ingestion.sources.cursor_reader", "read_cursor_batch",
    ),
}
SOURCE_ID_TO_TOOL: Dict[str, str] = {spec.source_id: tool for tool, spec in TOOLS.items()}
AI_TOOL_SOURCE_IDS: Tuple[str, ...] = tuple(SOURCE_ID_TO_TOOL)
#: The one ai_chat-lane mapper for the three tools (§2.1); the prefix comes from ``_metadata.tool``.
MAPPER_ID = "ai_coding_tool"

#: Result codes. The first three write no receipt (a failure receipt INSERTs a settings row that
#: defaults to enabled=1, which would make a refused source look enabled).
SOURCE_NOT_ENABLED = "source_not_enabled"
SOURCE_PAUSED = "source_paused"
NOT_PAIRED = "not_paired"
DISK_FLOOR = "disk_floor"
READER_UNAVAILABLE = "reader_unavailable"
PARSER_UNAVAILABLE = "parser_unavailable"
OWNER_KEY_NOT_PERSISTED = "owner_key_not_persisted"
NO_RECEIPT_CODES = frozenset({SOURCE_NOT_ENABLED, SOURCE_PAUSED, NOT_PAIRED})

#: The plain sentences the doors and the CLI show. ``{free}``/``{floor}`` are ``format_bytes`` text.
DISK_FLOOR_SENTENCE = (
    "There isn't enough free disk space to bring in your coding sessions: {free} is free and Topos keeps "
    "{floor} free. Free up space, then try again."
)
NOT_PAIRED_SENTENCE = (
    "This computer isn't paired with a Topos yet, so there is nowhere to keep your coding sessions. Pair it first."
)
OWNER_KEY_NOT_PERSISTED_SENTENCE = (
    "Topos couldn't keep its owner key on disk, so its owner-only doors aren't protected yet. Fix the permissions on "
    "~/.topos (run `topos-node doctor`), restart Topos, then enable this source."
)

ENGINE_CONFIG_KEY = "ai_tool_sources"
BACKFILL_CHOICES = ("all", "30d")
MODE_SINCE_LAST = "since_last"
MODE_FULL_HISTORY = "full_history"
_SINCE_LAST_MODES = frozenset({"", "all", MODE_SINCE_LAST})
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 5000
DEFAULT_BATCH_SIZE = 2000
MAX_PAUSE_SECONDS = 30.0
#: A reader that keeps answering "more" without ever running dry is a bug, not a corpus.
MAX_BATCHES_PER_RUN = 100_000
#: Receipt fields a ReadBatch may carry beyond the §2.7b five (L6): Codex's imported sessions, Cursor's
#: composers read from the preferred JSONL and those with no rewind. Counts, summed across batches.
RECEIPT_FIELDS = ("imported_sessions", "composers_jsonl_preferred", "composers_no_rewind")


class AiToolSyncRefused(Exception):
    """A door refusal: ``code`` is the result code, ``status`` the HTTP status, ``message`` the plain sentence."""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --- identity -----------------------------------------------------------------


def resolve_owner_scope(conn: Any) -> Optional[Dict[str, str]]:
    """``{user_id, topos_id, dataset_id, identity_source}`` for the served database, or None when unpaired.

    The bound identity (permissions v2, ``switches.is_bound`` → the runtime's ledger identity) first,
    the stored ``engine_config.topos_id`` (written from ``connection_info``, §3) otherwise. The
    dataset is the canonical install dataset ``<user_id>:topos:<topos_id>`` the web app uses.
    """
    from ..core.state import get_engine_config_value, get_user_id

    user_id = str(get_user_id(conn) or "").strip()
    if not user_id:
        return None
    topos_id = ""
    identity_source = "stored"
    try:
        from ..permissions_v2 import switches

        if switches.is_bound():
            from ..permissions_v2.runtime import get_runtime

            identity = get_runtime().protocol.ledger.identity
            if str(identity.owner_id) == user_id and str(identity.resource_id or "").strip():
                topos_id = str(identity.resource_id).strip()
                identity_source = "bound"
    except Exception as exc:  # noqa: BLE001 — an unreadable binding falls back to the stored id
        logger.debug("ai tool sync: bound identity unreadable (%s)", type(exc).__name__)
    if not topos_id:
        topos_id = str(get_engine_config_value(conn, "topos_id") or "").strip()
    if not topos_id or topos_id == "*":
        return None
    return {
        "user_id": user_id,
        "topos_id": topos_id,
        "dataset_id": f"{user_id}:topos:{topos_id}",
        "identity_source": identity_source,
    }


def _install_scope(scope: Dict[str, str]) -> Dict[str, str]:
    return {"user_id": scope["user_id"], "device_id": "*", "topos_id": scope["topos_id"], "dataset_id": scope["dataset_id"]}


# --- per-source state ------------------------------------------------------------


def _load_states(conn: Any) -> Dict[str, Dict[str, Any]]:
    from ..core.state import get_engine_config_value

    raw = get_engine_config_value(conn, ENGINE_CONFIG_KEY) or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_source_state(conn: Any, source_id: str, **changes: Any) -> Dict[str, Any]:
    """Merge ``changes`` into the source's flag record (``paused``, ``skip``, ``backfill``, stamps)."""
    from ..core.state import set_engine_config_value

    tool = SOURCE_ID_TO_TOOL.get(source_id)
    if tool is None:
        raise ValueError(f"not an AI-coding-tool source: {source_id}")
    states = _load_states(conn)
    current = dict(states.get(source_id) or {})
    for key, value in changes.items():
        if key == "skip":
            kept: List[str] = []
            for item in value or []:
                text = str(item).strip()
                if text and text not in kept:
                    kept.append(text)
            current[key] = kept
        elif key == "paused":
            current[key] = bool(value)
            current["paused_at"] = _utcnow_iso() if value else None
        else:
            current[key] = value
    states[source_id] = current
    set_engine_config_value(conn, ENGINE_CONFIG_KEY, json.dumps(states, sort_keys=True))
    return current


def _install_active(conn: Any, source_id: str, scope: Optional[Dict[str, str]]) -> bool:
    if scope is None:
        return False
    from ..sources.install_service import INSTALL_TABLE, _scope_key

    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (INSTALL_TABLE,)
        ).fetchone()
        if not found:
            return False
        row = conn.execute(
            f"SELECT 1 FROM {INSTALL_TABLE} WHERE source_id = ? AND scope_key = ? AND is_active = 1 LIMIT 1",
            (source_id, _scope_key(_install_scope(scope))),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 — unreadable means not enabled
        logger.debug("ai tool sync: install lookup failed (%s)", type(exc).__name__)
        return False
    return row is not None


def _settings_row(conn: Any, dataset_id: Optional[str], source_id: str) -> Optional[Dict[str, Any]]:
    """The stored settings row, or None when there is none (``get_source_settings`` would invent enabled=True)."""
    if not dataset_id:
        return None
    from ..storage.source_settings import TABLE as SETTINGS_TABLE

    try:
        found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SETTINGS_TABLE,)).fetchone()
        if not found:
            return None
        row = conn.execute(
            f"SELECT enabled, last_sync_at, last_error FROM {SETTINGS_TABLE} WHERE dataset_id = ? AND source_id = ?",
            (dataset_id, source_id),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.debug("ai tool sync: settings lookup failed (%s)", type(exc).__name__)
        return None
    if row is None:
        return None
    return {"enabled": bool(row[0]), "last_sync_at": row[1], "last_error": row[2]}


def source_state(conn: Any, source_id: str, *, scope: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """What the doors, the CLI and the sync guard read: enabled, paused, skip list, backfill choice, receipts."""
    tool = SOURCE_ID_TO_TOOL.get(source_id)
    if tool is None:
        raise ValueError(f"not an AI-coding-tool source: {source_id}")
    scope = scope or resolve_owner_scope(conn)
    dataset_id = scope["dataset_id"] if scope else None
    installed = _install_active(conn, source_id, scope)
    settings = _settings_row(conn, dataset_id, source_id)
    flags = _load_states(conn).get(source_id) or {}
    return {
        "source_id": source_id,
        "tool": tool,
        "display_name": TOOLS[tool].display_name,
        "dataset_id": dataset_id,
        "installed": installed,
        "enabled": bool(installed and settings and settings["enabled"]),
        "paused": bool(flags.get("paused")),
        "skip": list(flags.get("skip") or []),
        "backfill": flags.get("backfill"),
        "enabled_at": flags.get("enabled_at"),
        "paused_at": flags.get("paused_at"),
        "last_sync_at": settings["last_sync_at"] if settings else None,
        "last_error": settings["last_error"] if settings else None,
        # The reader's counts from the last run (L4's ledger): session files in which a node secret was
        # scrubbed, drops by reason, tainted turns. Counts only; ``doctor`` reads ``secret_sightings``.
        "secret_sightings": int(flags.get("secret_sightings") or 0),
        "last_sync_ledger": dict(flags.get("last_sync_ledger") or {}),
        "last_sync_outcome": flags.get("last_sync_outcome"),
        "last_sync_finished_at": flags.get("last_sync_finished_at"),
    }


def _refused(code: str, message: str) -> Dict[str, Any]:
    return {"status": "error", "code": code, "error": message, "records_processed": 0, "records_skipped": 0}


def sync_refusal(
    conn: Any,
    source_id: str,
    sync_options: Optional[Dict[str, Any]] = None,
    *,
    scope: Optional[Dict[str, str]] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Why a sync of this source must not start now, or None. The caller's dataset plays no part."""
    tool = SOURCE_ID_TO_TOOL.get(source_id)
    if tool is None:
        return None
    scope = scope or resolve_owner_scope(conn)
    if scope is None:
        return _refused(NOT_PAIRED, NOT_PAIRED_SENTENCE)
    state = state or source_state(conn, source_id, scope=scope)
    display = TOOLS[tool].display_name
    if not state["enabled"]:
        return _refused(SOURCE_NOT_ENABLED, f"{display} sessions aren't enabled on this Topos. Enable the source first.")
    if state["paused"]:
        return _refused(SOURCE_PAUSED, f"{display} sessions are paused on this Topos. Resume the source to sync again.")
    return None


def disk_floor_refusal(conn: Any, *, needed_bytes: int = 0) -> Optional[str]:
    """The plain sentence when the volume holding the database sits below the owner's floor, else None."""
    from ..engine import disk_space

    try:
        from ..storage.db.paths import active_base

        free = disk_space.free_bytes(active_base())
    except Exception:  # noqa: BLE001
        free = None
    if free is None:
        free = disk_space.free_bytes()
    if free is None:
        return None
    floor = int(disk_space.min_free_bytes(conn))
    if free >= floor + max(0, int(needed_bytes or 0)):
        return None
    return DISK_FLOOR_SENTENCE.format(free=disk_space.format_bytes(free), floor=disk_space.format_bytes(floor))


# --- the doors' helpers -------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def backfill_sync_options(backfill: str) -> Dict[str, Any]:
    """The sync options a backfill job carries: everything, or the last 30 days."""
    choice = str(backfill or "all").strip().lower()
    if choice not in BACKFILL_CHOICES:
        raise ValueError("backfill must be 'all' or '30d'")
    if choice == "all":
        return {"mode": MODE_FULL_HISTORY, "trigger": "backfill"}
    start = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    return {"mode": "custom", "start_date": start, "trigger": "backfill"}


def _harden_topos_dir() -> Optional[Dict[str, Any]]:
    """L7's hardening pass (``upgrades/hardening.harden_topos_dir``): ~/.topos to 0700, the database, WAL, SHM,
    logs, agents and profiles to 0600. The node's copy of a transcript must never be easier to read than the
    tool's own (security finding 13). Idempotent; None when this build lacks the module."""
    try:
        from ..upgrades.hardening import harden_topos_dir
    except ImportError:
        logger.warning("ai tool sync: hardening module absent; ~/.topos modes not clamped")
        return None
    try:
        report = harden_topos_dir()
    except Exception as exc:  # noqa: BLE001 — the kind only
        logger.warning("ai tool sync: hardening failed: %s", type(exc).__name__)
        return None
    return report if isinstance(report, dict) else None


def _owner_key_persisted() -> Optional[bool]:
    """Whether ``~/.topos/.env`` holds the owner key (``owner_key.owner_key_persisted``, L7): a node whose
    ``~/.topos`` was unwritable at boot is in legacy mode with every direct-HTTP door open (DIGEST J6), and
    must not start shadowing transcripts. None when this build lacks the check."""
    try:
        from ..owner_key import owner_key_persisted
    except ImportError:
        logger.warning("ai tool sync: owner_key_persisted check absent in this build")
        return None
    try:
        return bool(owner_key_persisted())
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai tool sync: owner key check failed: %s", type(exc).__name__)
        return None


def enable_ai_tool_source(
    conn: Any,
    source_id: str,
    *,
    skip: Sequence[str] = (),
    backfill: str = "all",
) -> Dict[str, Any]:
    """The enable door's work (§2.8): install row, settings row, hourly schedule, backfill job.

    Before the first write: the disk floor, L7's hardening pass over ``~/.topos`` and the owner-key gate
    (409 ``owner_key_not_persisted`` on a legacy-mode node). Idempotent: a second enable keeps the active
    install, rewrites the flags and enqueues another backfill only when none is queued or running. Raises
    ``AiToolSyncRefused`` with the plain sentence on a refusal; nothing is written then.
    """
    tool = SOURCE_ID_TO_TOOL.get(source_id)
    if tool is None:
        raise ValueError(f"not an AI-coding-tool source: {source_id}")
    options = backfill_sync_options(backfill)
    scope = resolve_owner_scope(conn)
    if scope is None:
        raise AiToolSyncRefused(NOT_PAIRED, NOT_PAIRED_SENTENCE)
    floor = disk_floor_refusal(conn)
    if floor is not None:
        raise AiToolSyncRefused(DISK_FLOOR, floor)
    hardening = _harden_topos_dir()
    persisted = _owner_key_persisted()
    if persisted is None and hardening is not None and "owner_key_persisted" in hardening:
        persisted = bool(hardening.get("owner_key_persisted"))
    if persisted is False:
        raise AiToolSyncRefused(OWNER_KEY_NOT_PERSISTED, OWNER_KEY_NOT_PERSISTED_SENTENCE)

    from ..sources import install_service
    from ..sources.registry import BUNDLED_REGISTRY, REGISTRY
    from ..storage.source_settings import put_source_settings
    from .local_sync_jobs import enqueue_local_sync_blocking
    from .local_sync_schedule import put_schedule

    definition = BUNDLED_REGISTRY.get(source_id) or REGISTRY[source_id]
    record = install_service.install_source(
        source_definition_json=definition.to_dict(), scope=_install_scope(scope)
    )
    dataset_id = scope["dataset_id"]
    put_source_settings(conn, dataset_id, source_id, enabled=True)
    set_source_state(conn, source_id, paused=False, skip=list(skip), backfill=str(backfill or "all"),
                     enabled_at=_utcnow_iso())
    schedule = put_schedule(conn, dataset_id, source_id, {"enabled": True, "interval_hours": 1})
    enqueued = enqueue_local_sync_blocking(conn, source_id=source_id, dataset_id=dataset_id, sync_options=options)
    if enqueued.get("status") != "ok":
        raise AiToolSyncRefused(str(enqueued.get("code") or "backfill_not_started"),
                                str(enqueued.get("error") or "The backfill could not be started."), status=503)
    state = source_state(conn, source_id, scope=scope)
    return {
        "status": "ok",
        "source_id": source_id,
        "dataset_id": dataset_id,
        "enabled": state["enabled"],
        "paused": state["paused"],
        "skip": state["skip"],
        "backfill": state["backfill"],
        "install_id": record.install_id,
        "schedule": schedule,
        "backfill_job_id": enqueued.get("job_id"),
        "backfill_already_running": bool(enqueued.get("already_running")),
        "hardening": {k: hardening.get(k) for k in ("changed", "already_clamped", "skipped", "owner_key_persisted")
                      if k in hardening} if hardening else None,
    }


def describe_ai_tool_source(conn: Any, source_id: str) -> Dict[str, Any]:
    """The status block the CLI's ``status --json`` and the doors answer with. Counts and stamps only."""
    from .local_sync_schedule import describe_schedule

    state = source_state(conn, source_id)
    schedule = describe_schedule(conn, state["dataset_id"], source_id) if state["dataset_id"] else None
    return {**state, "schedule": schedule, "checkpoint": describe_ai_tool_checkpoint(conn, state["dataset_id"] or "", source_id)}


def describe_ai_tool_checkpoint(conn: Any, dataset_id: str, source_id: str) -> Dict[str, Any]:
    """Where the next since-last sync starts: the newest ``created_at`` any run of this dataset saved."""
    empty = {"has_checkpoint": False, "newest_created_at": None, "last_run_at": None, "updated_at": None}
    tool = SOURCE_ID_TO_TOOL.get(source_id)
    if conn is None or not dataset_id or tool is None:
        return empty
    try:
        found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingestion_checkpoints'").fetchone()
        if not found:
            return empty
        row = conn.execute(
            "SELECT last_record_id, metadata_json, updated_at FROM ingestion_checkpoints WHERE dataset_id = ? AND schema_id = ?",
            (dataset_id, TOOLS[tool].schema_id),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.debug("ai tool checkpoint describe failed: %s", type(exc).__name__)
        return empty
    if not row:
        return empty
    try:
        metadata = json.loads(row[1]) if row[1] else {}
    except (TypeError, ValueError):
        metadata = {}
    newest = str(row[0] or "") or None
    return {
        "has_checkpoint": newest is not None,
        "newest_created_at": newest,
        "last_run_at": metadata.get("last_run_at") if isinstance(metadata, dict) else None,
        "updated_at": row[2],
    }


class _MemoryCursorStore:
    """A cursor store that remembers nothing past the call: what a counts-only preview reads through."""

    def __init__(self) -> None:
        self._rows: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def get(self, source_id: str, path: Any):
        return self._rows.get((source_id, str(path)))

    def put(self, source_id: str, path: Any, *, size: int, mtime: float, offset: int, record_count: int) -> None:
        self._rows[(source_id, str(path))] = {"size": size, "mtime": mtime, "offset": offset, "record_count": record_count}


def preview_ai_tool_source(conn: Any, source_id: str, *, skip: Sequence[str] = ()) -> Dict[str, Any]:
    """Counts only (§2.8): sessions, turns, text bytes, project folders, oldest/newest. No raw write, no checkpoint."""
    tool = SOURCE_ID_TO_TOOL.get(source_id)
    if tool is None:
        raise ValueError(f"not an AI-coding-tool source: {source_id}")
    reader = _reader_for(tool)
    cursors = _MemoryCursorStore()
    skip_folders = tuple(sorted({str(s).strip() for s in skip if str(s).strip()}))
    sessions: set = set()
    folders: set = set()
    turns = 0
    bytes_text = 0
    oldest: Optional[str] = None
    newest: Optional[str] = None
    scanned_files = 0
    ledger: Dict[str, Any] = {}
    for _ in range(MAX_BATCHES_PER_RUN):
        batch = reader(root_path=None, cursors=cursors, batch_size=DEFAULT_BATCH_SIZE, since_unix=None,
                       session_path=None, skip_folders=skip_folders, secrets=node_secret_values(conn))
        rows = list(getattr(batch, "rows", None) or [])
        touched = int(getattr(batch, "files_touched", 0) or 0)
        scanned_files += int(getattr(batch, "scanned_files", 0) or 0)
        ledger = _merge_ledgers(ledger, _ledger_dict(getattr(batch, "ledger", None)))
        for row in rows:
            turns += 1
            sessions.add(str(row.get("thread_id") or ""))
            bytes_text += len(str(row.get("content") or "").encode("utf-8"))
            project_key = _lift_facets(dict(row))
            if project_key:
                folders.add(project_key)
            when = str(row.get("created_at") or "")
            if when:
                oldest = when if oldest is None or when < oldest else oldest
                newest = when if newest is None or when > newest else newest
        if touched == 0:
            break
    return {
        "source_id": source_id,
        "sessions": len(sessions),
        "turns": turns,
        "bytes_text": bytes_text,
        "folders": sorted(folders),
        "oldest": oldest,
        "newest": newest,
        "scanned_files": scanned_files,
        "skip": list(skip_folders),
        "ledger": ledger,
    }


# --- the runner ----------------------------------------------------------------


def _reader_for(tool: str) -> Callable[..., Any]:
    """``read_<tool>_batch`` from the tool's reader module (L4/L6); ImportError when the build lacks it."""
    spec = TOOLS[tool]
    module = importlib.import_module(spec.reader_module)
    return getattr(module, spec.reader_name)


def _cursor_store_for(conn: Any) -> Any:
    """L4's per-file cursor store (§2.6), bound to this connection."""
    from .checkpoints.file_cursors import FileCursorStore

    return FileCursorStore(conn)


def _parser_for(schema_id: str) -> Optional[Any]:
    """The BUNDLED parser class for this schema, or None: a runtime-built parser may never speak for a transcript."""
    from .parsers import BUNDLED_PARSER_CLASSES, PARSER_REGISTRY

    parser_cls = PARSER_REGISTRY.get(schema_id)
    if parser_cls is None or parser_cls not in BUNDLED_PARSER_CLASSES:
        return None
    return parser_cls


def _post_canonical(*, source_def: Any, canonical_records: List[Dict[str, Any]], sync_batch_id: str) -> Dict[str, Any]:
    """Enrichment and signal derivation for one batch, through the canonical pipeline's own entry."""
    from .canonical_pipeline import run_post_canonical_pipeline

    coroutine = run_post_canonical_pipeline(
        source_def=source_def, canonical_records=canonical_records, sync_batch_id=sync_batch_id,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # Called from a thread that owns a running loop: run the pipeline on its own thread and loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def node_secret_values(conn: Any = None) -> Tuple[str, ...]:
    """The node's own secret values for the reader's exact-value scrub (§2.4). Never logged, never returned to a caller.

    L4's ``parsers.ai_coding_tool.common.node_secrets`` (the env file's keys and every label the node holds)
    when the build has it; else the two keys the settings object carries.
    """
    try:
        from .parsers.ai_coding_tool.common import node_secrets

        found = node_secrets()
        return tuple(str(v) for v in (found or ()) if isinstance(v, str) and v.strip())
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — fall back to the settings' values
        logger.debug("ai tool sync: node_secrets failed: %s", type(exc).__name__)
    values: List[str] = []
    try:
        from ..config.settings import settings

        for value in (getattr(settings, "topos_key", None), getattr(settings, "topos_owner_key", None)):
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    except Exception:  # noqa: BLE001 — no settings, no exact values; the shape rules still apply
        pass
    return tuple(values)


def _ledger_dict(ledger: Any) -> Dict[str, Any]:
    if ledger is None:
        return {}
    if isinstance(ledger, dict):
        return dict(ledger)
    as_dict = getattr(ledger, "as_dict", None)
    if callable(as_dict):
        try:
            out = as_dict()
            return dict(out) if isinstance(out, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _merge_ledgers(total: Dict[str, Any], part: Dict[str, Any]) -> Dict[str, Any]:
    """Sum numbers, merge dicts, keep the last value for anything else."""
    out = dict(total)
    for key, value in part.items():
        if isinstance(value, dict):
            out[key] = _merge_ledgers(out.get(key) if isinstance(out.get(key), dict) else {}, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = (out.get(key) or 0) + value
        else:
            out[key] = value
    return out


def _iso_to_unix(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _bounded_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number))


def _bounded_float(value: Any, *, default: float, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number))


def _chunks(items: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _ensure_storage(conn: Any) -> None:
    """The column and the facets table, before the first write: the ai_chat tables may be newer than the migrations."""
    from ..storage.db.migrations.ai_coding_sessions_v1 import ensure_facets_table, ensure_share_excluded_column

    with with_db_write():
        ensure_share_excluded_column(conn)
        ensure_facets_table(conn)
        commit_connection(conn)


def _mark_share_excluded(conn: Any, message_ids: Sequence[str]) -> int:
    """The post-write UPDATE (§2.6), and the timeline projection those rows just received comes out again."""
    ids = [str(i) for i in message_ids if i]
    if not ids:
        return 0
    updated = 0
    with with_db_write():
        for chunk in _chunks(ids):
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"UPDATE ai_chat_messages SET share_excluded = 1 WHERE message_id IN ({placeholders})", list(chunk)
            )
            updated += int(cursor.rowcount or 0)
            try:
                conn.execute(
                    f"DELETE FROM timeline WHERE canonical_table = 'ai_chat_messages' AND record_id IN ({placeholders})",
                    list(chunk),
                )
            except Exception as exc:  # noqa: BLE001 — a node without the timeline table has nothing to remove
                logger.debug("timeline rows for share_excluded ids not removed: %s", type(exc).__name__)
        commit_connection(conn)
    return updated


def secret_sightings(ledger: Dict[str, Any]) -> int:
    """Session files in which a node secret was scrubbed: L4's ``sessions_with_secrets`` (its ``secret_sightings``
    is a per-session map); an integer ``secret_sightings`` is accepted too. A count, never a value."""
    for key in ("sessions_with_secrets", "secret_sightings"):
        value = ledger.get(key) if isinstance(ledger, dict) else None
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, dict):
            return len([k for k, n in value.items() if n])
    return 0


def _record_sync_ledger(conn: Any, source_id: str, ledger: Dict[str, Any], *, outcome: str) -> None:
    """After every run: the reader's counts beside the source's flags, for the status door and ``doctor``.

    ``secret_sightings`` is the number of session files in which the node's own secret was scrubbed; L3's
    doctor turns it into "rotate the key". Counts and stamps only; the ledger never holds text.
    """
    try:
        set_source_state(
            conn, source_id,
            last_sync_ledger=dict(ledger), secret_sightings=secret_sightings(ledger),
            last_sync_outcome=outcome, last_sync_finished_at=_utcnow_iso(),
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails a landed sync
        logger.warning("ai tool sync: ledger bookkeeping failed: %s", type(exc).__name__)


def _already_stored(conn: Any, message_ids: Sequence[str]) -> set:
    """Which of these message ids ``ai_chat_messages`` already holds, in any dataset."""
    found: set = set()
    ids = [i for i in message_ids if i]
    if not ids:
        return found
    try:
        for chunk in _chunks(ids):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT message_id FROM ai_chat_messages WHERE message_id IN ({placeholders})", list(chunk)
            ).fetchall()
            found.update(str(r[0]) for r in rows)
    except Exception as exc:  # noqa: BLE001 — no table yet means nothing stored
        logger.debug("ai tool sync: stored-id lookup failed: %s", type(exc).__name__)
    return found


def _write_facets(conn: Any, tool: str, facets: Dict[str, str]) -> int:
    if not facets:
        return 0
    from ..storage.db.migrations.ai_coding_sessions_v1 import FACETS_TABLE

    now = _utcnow_iso()
    with with_db_write():
        conn.executemany(
            f"INSERT OR REPLACE INTO {FACETS_TABLE} (message_id, tool, project_key, updated_at) VALUES (?, ?, ?, ?)",
            [(message_id, tool, project_key, now) for message_id, project_key in facets.items()],
        )
        commit_connection(conn)
    return len(facets)


def _lift_facets(row: Dict[str, Any]) -> Optional[str]:
    """Take the project facet OFF the record before anything writes it; never leave a path-shaped key behind.

    L6's readers put ``project_key`` (the repo folder's basename, or None) at the TOP level of the row,
    outside the §2.2 record; a ``_facets`` sibling block is accepted too. Either way it goes to the
    owner-only facets table and never to the raw row, the parser or ``metadata_json``.
    """
    project_key: Optional[str] = None
    top = row.pop("project_key", None)
    if isinstance(top, str) and top.strip():
        project_key = top.strip()
    facets = row.pop("_facets", None)
    if isinstance(facets, dict) and facets.get("project_key"):
        project_key = str(facets["project_key"]).strip() or project_key
    meta = row.get("_metadata")
    if isinstance(meta, dict):
        for key in ("project_key", "cwd", "repo_path", "project_path", "file_path"):
            meta.pop(key, None)
    return project_key


def run_ai_coding_tool_sync(
    tool: str,
    dataset_id: str,
    *,
    db_conn: Optional[Any] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sync_options: Optional[Dict[str, Any]] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    session_path: Optional[str] = None,
) -> Dict[str, Any]:
    """One sync of ``tool``'s transcripts into the owner's dataset. Same result shape as ``run_imessage_sync`` plus ``ledger``.

    ``dataset_id`` is accepted for the runner-table signature and IGNORED: the dataset comes from the
    bound identity (security finding 15). ``sync_options.mode``: ``since_last`` (default) resumes after
    the checkpoint's newest ``created_at``; ``full_history`` reads everything the file cursors have not
    seen; the window modes (``1m`` … ``custom``) as for iMessage. ``session_path`` (or
    ``sync_options.session_path``) narrows the read to one transcript, the hook's case.
    """
    spec = TOOLS.get(str(tool or "").strip())
    if spec is None:
        return {"status": "error", "error": f"unknown AI coding tool: {tool}", "records_processed": 0, "records_skipped": 0}
    options = dict(sync_options) if isinstance(sync_options, dict) else {}
    session_path = str(session_path or options.get("session_path") or "").strip() or None

    if db_conn is None:
        from ..core.state import get_db_connection

        db_conn = get_db_connection()
    if db_conn is None:
        return {"status": "error", "error": "Database connection not available", "records_processed": 0, "records_skipped": 0}

    scope = resolve_owner_scope(db_conn)
    if scope is None:
        return _refused(NOT_PAIRED, NOT_PAIRED_SENTENCE)
    requested = str(dataset_id or "").strip()
    dataset_id = scope["dataset_id"]
    if requested and requested != dataset_id:
        logger.info("[PIPELINE:SYNC] %s sync: the caller named another dataset; the bound identity decides", spec.source_id)

    from .local_sync import SYNC_IN_PROGRESS, exclusive_sync

    with exclusive_sync(spec.source_id, dataset_id) as acquired:
        if not acquired:
            return {
                "status": "error", "code": SYNC_IN_PROGRESS, "error": "Another sync of this dataset is running.",
                "records_processed": 0, "records_skipped": 0,
            }
        state = source_state(db_conn, spec.source_id, scope=scope)
        refusal = sync_refusal(db_conn, spec.source_id, options, scope=scope, state=state)
        if refusal is not None:
            return refusal
        floor = disk_floor_refusal(db_conn)
        if floor is not None:
            return _refused(DISK_FLOOR, floor)
        try:
            return _run_locked(
                spec, dataset_id=dataset_id, db_conn=db_conn, batch_size=batch_size, options=options,
                progress_cb=progress_cb, session_path=session_path, skip_folders=tuple(state["skip"]),
            )
        except Exception as exc:  # noqa: BLE001 — the executor turns this into a failure receipt
            logger.warning("%s sync failed (top-level catch): %s", spec.source_id, exc, exc_info=True)
            return {"status": "error", "error": str(exc), "records_processed": 0, "records_skipped": 0}


def _run_locked(
    spec: ToolSpec,
    *,
    dataset_id: str,
    db_conn: Any,
    batch_size: int,
    options: Dict[str, Any],
    progress_cb: Optional[Callable[[Dict[str, Any]], None]],
    session_path: Optional[str],
    skip_folders: Tuple[str, ...],
) -> Dict[str, Any]:
    from .canonical_pipeline import canonicalize_normalized_batch
    from .local_sync import _emit_sync_progress, _request_disclosure, _resolve_sync_start_unix
    from ..features.provenance.writer_class import WRITER_OWNER_APP
    from ..sources.registry import REGISTRY
    from ..storage.raw.raw_tables_manager import RawTablesManager

    source_id, schema_id = spec.source_id, spec.schema_id
    batch_size = _bounded_int(batch_size, default=DEFAULT_BATCH_SIZE, lo=MIN_BATCH_SIZE, hi=MAX_BATCH_SIZE)
    pause_seconds = _bounded_float(options.get("pause_seconds"), default=0.0, lo=0.0, hi=MAX_PAUSE_SECONDS)
    mode = str(options.get("mode") or MODE_SINCE_LAST).strip().lower()
    start_unix, start_error = _resolve_sync_start_unix(options)
    if start_error:
        return {"status": "error", "error": start_error, "records_processed": 0, "records_skipped": 0}

    store = SqliteCheckpointStore(db_conn)
    checkpoint = store.get_checkpoint(dataset_id, schema_id)
    last_record_id = str(checkpoint.last_record_id or "") if checkpoint else ""
    if mode in _SINCE_LAST_MODES:
        since_unix = _iso_to_unix(last_record_id)
    elif mode == MODE_FULL_HISTORY:
        since_unix = None
    else:
        since_unix = start_unix

    source_def = REGISTRY.get(source_id)
    if source_def is None:
        return {"status": "error", "error": f"unknown source_id: {source_id}", "records_processed": 0, "records_skipped": 0}
    parser_cls = _parser_for(schema_id)
    if parser_cls is None:
        return _refused(PARSER_UNAVAILABLE, f"This build can't read {spec.display_name} transcripts yet.")
    try:
        reader = _reader_for(spec.tool)
        cursors = _cursor_store_for(db_conn)
    except (ImportError, AttributeError):
        return _refused(READER_UNAVAILABLE, f"This build can't read {spec.display_name} transcripts yet.")
    parser = parser_cls(dataset_id=dataset_id, _schema_id=schema_id)
    raw_tables = RawTablesManager(db_conn)
    secrets = node_secret_values(db_conn)
    _ensure_storage(db_conn)

    totals = {"processed": 0, "skipped": 0, "created": 0, "excluded": 0, "facets": 0, "duplicates": 0,
              "scanned_files": 0, "files_touched": 0}
    receipt: Dict[str, Any] = {}
    ledger: Dict[str, Any] = {}
    reader_kwargs: Dict[str, Any] = {}
    state_db_path = str(options.get("state_db_path") or "").strip()
    if spec.tool == "cursor" and state_db_path:
        # Cursor's reader also takes the state database's path (env CURSOR_STATE_DB by default).
        reader_kwargs["state_db_path"] = state_db_path
    final_last_record_id = last_record_id
    batch_num = 0

    def _save(last: str) -> None:
        store.save_checkpoint(IngestionCheckpoint(
            dataset_id=dataset_id,
            schema_id=schema_id,
            last_record_id=last,
            metadata={"coverage_recorded": True, "mode": mode, "last_run_at": _utcnow_iso(),
                      "scanned_files": totals["scanned_files"], "files_touched": totals["files_touched"]},
        ))

    while batch_num < MAX_BATCHES_PER_RUN:
        batch_num += 1
        try:
            batch = reader(
                root_path=None, cursors=cursors, batch_size=batch_size, since_unix=since_unix,
                session_path=session_path, skip_folders=skip_folders, secrets=secrets, **reader_kwargs,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return {"status": "error", "error": str(exc), "records_processed": totals["processed"],
                    "records_skipped": totals["skipped"], "ledger": ledger}
        rows = list(getattr(batch, "rows", None) or [])
        touched = int(getattr(batch, "files_touched", 0) or 0)
        totals["scanned_files"] += int(getattr(batch, "scanned_files", 0) or 0)
        totals["files_touched"] += touched
        ledger = _merge_ledgers(ledger, _ledger_dict(getattr(batch, "ledger", None)))
        for field in RECEIPT_FIELDS:
            value = getattr(batch, field, None)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, int):
                receipt[field] = int(receipt.get(field) or 0) + value
            else:
                receipt[field] = value
        if not rows and not touched:
            break

        facets: Dict[str, str] = {}
        excluded: List[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            project_key = _lift_facets(row)
            message_id = str(row.get("id") or "")
            if project_key and message_id:
                facets[message_id] = project_key
            meta = row.get("_metadata") if isinstance(row.get("_metadata"), dict) else {}
            if meta.get("share_excluded") is True and message_id:
                excluded.append(message_id)

        # Raw rows for traceability, after the facet left the record (non-fatal, as for iMessage).
        try:
            for row in rows:
                raw_tables.write_raw_record(
                    source_id=source_id, source_record_id=str(row.get("id") or ""), payload=row, source_type="chat_messages",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[PIPELINE:RAW] %s raw write failed (non-fatal): %s", source_id, exc)

        normalized: List[Any] = []
        for row in rows:
            raw = RawRecord(record_id=str(row.get("id") or ""), payload=row)
            validation = parser.validate(raw)
            if not validation.is_valid:
                totals["skipped"] += 1
                ledger = _merge_ledgers(ledger, {"dropped": {"invalid_record": 1}, "dropped_total": 1})
                continue
            normalized.append(parser.parse(raw))

        if normalized:
            # First writer wins (§2.1): a fork or resume copy of a uuid already stored is a no-op, and its
            # text never rewrites the row (the owner door would otherwise be allowed to).
            already = _already_stored(db_conn, [str(rec.record_id) for rec in normalized])
            if already:
                normalized = [rec for rec in normalized if str(rec.record_id) not in already]
                totals["duplicates"] += len(already)
                ledger = _merge_ledgers(ledger, {"dropped": {"fork_duplicate": len(already)}, "dropped_total": len(already)})
                excluded = [i for i in excluded if i not in already]
        if normalized:
            sync_batch_id = f"{source_id}:{uuid.uuid4().hex[:12]}"
            result = canonicalize_normalized_batch(
                db_conn, source_def, normalized,
                dataset_id=dataset_id, sync_batch_id=sync_batch_id, parser_cls=parser_cls,
                writer_class=WRITER_OWNER_APP, writer_dataset_id=dataset_id,
            )
            if result.errors and not result.canonical_records and not result.messages_created:
                first = result.errors[0]
                message = first.get("error") if isinstance(first, dict) else str(first)
                return {"status": "error", "error": str(message or "canonical write failed"),
                        "records_processed": totals["processed"], "records_skipped": totals["skipped"], "ledger": ledger}
            written = {str(rec.get("message_id") or "") for rec in result.canonical_records if isinstance(rec, dict)}
            excluded_ids = [i for i in excluded if i in written] or [i for i in excluded]
            totals["excluded"] += _mark_share_excluded(db_conn, excluded_ids)
            totals["facets"] += _write_facets(db_conn, spec.tool, {k: v for k, v in facets.items() if k in written})
            excluded_set = set(excluded)
            shareable = [rec for rec in result.canonical_records
                         if isinstance(rec, dict) and str(rec.get("message_id") or "") not in excluded_set]
            if shareable:
                try:
                    _post_canonical(source_def=source_def, canonical_records=shareable, sync_batch_id=sync_batch_id)
                except Exception as exc:  # noqa: BLE001 — enrichment never fails a landed batch
                    logger.warning("[PIPELINE:ENRICHMENT] %s post-canonical failed (non-fatal): %s", source_id, exc, exc_info=True)
            _request_disclosure()
            totals["processed"] += len(normalized)
            totals["created"] += int(result.messages_created or 0)

        newest = str(getattr(batch, "newest_created_at", None) or "")
        if newest and newest > final_last_record_id:
            final_last_record_id = newest
        if rows:
            _save(final_last_record_id)
        _emit_sync_progress(
            progress_cb, batch_num=batch_num, records_processed=totals["processed"],
            records_skipped=totals["skipped"], last_record_id=final_last_record_id,
        )
        # The reader says when it is done: a batch can be short of batch_size (drops, a small file) and
        # still have files behind it. ``files_touched == 0`` is the end (L4's contract).
        if touched == 0:
            break
        if pause_seconds:
            time.sleep(pause_seconds)

    outcome = "imported" if totals["processed"] else "up_to_date"
    _record_sync_ledger(db_conn, source_id, ledger, outcome=outcome)
    return {
        "status": "ok",
        "outcome": outcome,
        "source_id": source_id,
        "dataset_id": dataset_id,
        "records_processed": totals["processed"],
        "records_skipped": totals["skipped"],
        "records_share_excluded": totals["excluded"],
        "records_duplicate": totals["duplicates"],
        "messages_created": totals["created"],
        "facets_written": totals["facets"],
        "scanned_files": totals["scanned_files"],
        "files_touched": totals["files_touched"],
        "last_record_id": final_last_record_id,
        "ledger": ledger,
        "reader_receipt": receipt,
    }


def run_claude_code_sync(dataset_id: str, *, db_conn: Optional[Any] = None, batch_size: int = DEFAULT_BATCH_SIZE,
                         sync_options: Optional[Dict[str, Any]] = None,
                         progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
                         session_path: Optional[str] = None) -> Dict[str, Any]:
    return run_ai_coding_tool_sync("claude_code", dataset_id, db_conn=db_conn, batch_size=batch_size,
                                   sync_options=sync_options, progress_cb=progress_cb, session_path=session_path)


def run_codex_sync(dataset_id: str, *, db_conn: Optional[Any] = None, batch_size: int = DEFAULT_BATCH_SIZE,
                   sync_options: Optional[Dict[str, Any]] = None,
                   progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
                   session_path: Optional[str] = None) -> Dict[str, Any]:
    return run_ai_coding_tool_sync("codex", dataset_id, db_conn=db_conn, batch_size=batch_size,
                                   sync_options=sync_options, progress_cb=progress_cb, session_path=session_path)


def run_cursor_sync(dataset_id: str, *, db_conn: Optional[Any] = None, batch_size: int = DEFAULT_BATCH_SIZE,
                    sync_options: Optional[Dict[str, Any]] = None,
                    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
                    session_path: Optional[str] = None, state_db_path: Optional[str] = None) -> Dict[str, Any]:
    """Cursor: ``session_path`` is the hook's ``transcript_path`` when transcripts are on, else the conversation id
    (composerId), which the reader uses to restrict the state-database read; ``state_db_path`` overrides the
    reader's ``CURSOR_STATE_DB`` default. Both pass through unchanged."""
    options = dict(sync_options) if isinstance(sync_options, dict) else {}
    if state_db_path:
        options["state_db_path"] = state_db_path
    return run_ai_coding_tool_sync("cursor", dataset_id, db_conn=db_conn, batch_size=batch_size,
                                   sync_options=options, progress_cb=progress_cb, session_path=session_path)


RUNNERS_BY_SOURCE: Dict[str, Callable[..., Dict[str, Any]]] = {
    "claude_code_sessions": run_claude_code_sync,
    "codex_sessions": run_codex_sync,
    "cursor_sessions": run_cursor_sync,
}


def ai_tool_post_sync(conn: Any, dataset_id: str, result: Dict[str, Any], sync_options: Any) -> Dict[str, Any]:
    """What a finished AI-tool sync reports to whoever polls its job: counts, the ledger, never a body."""
    options = sync_options if isinstance(sync_options, dict) else {}
    summary = {
        key: result.get(key)
        for key in (
            "outcome", "records_processed", "records_skipped", "records_share_excluded", "messages_created",
            "scanned_files", "files_touched", "last_record_id", "code", "error", "ledger", "reader_receipt",
        )
        if result.get(key) is not None
    }
    summary["trigger"] = str(options.get("trigger") or "manual")
    return {**result, "sync": summary}


__all__ = [
    "AI_TOOL_SOURCE_IDS",
    "AiToolSyncRefused",
    "BACKFILL_CHOICES",
    "DISK_FLOOR",
    "DISK_FLOOR_SENTENCE",
    "MAPPER_ID",
    "NOT_PAIRED",
    "NO_RECEIPT_CODES",
    "OWNER_KEY_NOT_PERSISTED",
    "RUNNERS_BY_SOURCE",
    "SOURCE_ID_TO_TOOL",
    "SOURCE_NOT_ENABLED",
    "SOURCE_PAUSED",
    "TOOLS",
    "ai_tool_post_sync",
    "backfill_sync_options",
    "describe_ai_tool_checkpoint",
    "describe_ai_tool_source",
    "disk_floor_refusal",
    "enable_ai_tool_source",
    "node_secret_values",
    "preview_ai_tool_source",
    "resolve_owner_scope",
    "run_ai_coding_tool_sync",
    "run_claude_code_sync",
    "run_codex_sync",
    "run_cursor_sync",
    "secret_sightings",
    "set_source_state",
    "source_state",
    "sync_refusal",
]
