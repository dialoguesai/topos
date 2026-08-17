from __future__ import annotations

import logging
import asyncio
import os
import platform
import shutil
import subprocess
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MCP_REQUEST_LOG_TABLE = "mcp_request_log"
UMA_ACCESS_REQUESTS_TABLE = "uma_access_requests"
LEGACY_BROWSER_PLUGIN_APP_ID = "browser-plugin"
BROWSER_HISTORY_PLUGIN_APP_ID = "browser-history-plugin"

from .routine_access import (
    ROUTINE_ACCESS_CHANNEL,
    ROUTINE_ACCESS_CONTEXT,
    ROUTINE_APP_ID,
    is_routine_mcp_source,
)


def derive_uma_access_context(owner_user_id: str, requesting_user_id: Optional[str]) -> str:
    """Classify whether the caller is the resource owner or a grantee (or unknown)."""
    rid = (requesting_user_id or "").strip()
    if not rid:
        return "unknown"
    oid = (owner_user_id or "").strip()
    if oid and oid == rid:
        return "owner_self"
    return "grantee"


def _empty_access_attribution(window_days: int = 0) -> Dict[str, int]:
    return {
        "window_days": window_days,
        "owner_self_reads": 0,
        "owner_self_writes": 0,
        "grantee_reads": 0,
        "grantee_writes": 0,
        "unknown_reads": 0,
        "unknown_writes": 0,
        "routine_reads": 0,
        "routine_writes": 0,
    }


def _uma_actx_case_sql() -> str:
    """SQL CASE matching derive_uma_access_context for GROUP BY aggregates."""
    return """CASE
        WHEN LOWER(COALESCE(access_context, '')) IN ('owner_self', 'grantee', 'unknown', 'routine')
            THEN LOWER(access_context)
        WHEN requesting_user_id IS NOT NULL AND TRIM(requesting_user_id) = ?
            THEN 'owner_self'
        WHEN requesting_user_id IS NOT NULL AND TRIM(requesting_user_id) != ''
            THEN 'grantee'
        ELSE 'unknown'
    END"""


def _aggregate_uma_access_attribution(
    conn: sqlite3.Connection,
    owner_user_id: str,
    *,
    since_ts: Optional[str] = None,
    window_days: int = 0,
) -> Dict[str, Any]:
    attr = _empty_access_attribution(window_days)
    where = f"owner_user_id = ?"
    params: List[Any] = [owner_user_id, owner_user_id]
    if since_ts:
        where += " AND created_at >= ?"
        params.append(since_ts)
    actx_sql = _uma_actx_case_sql()
    cursor = conn.execute(
        f"""SELECT LOWER(request_type) AS rt, ({actx_sql}) AS actx, COUNT(*) AS cnt
            FROM {UMA_ACCESS_REQUESTS_TABLE}
            WHERE {where}
            GROUP BY rt, actx""",
        tuple(params),
    )
    for row in cursor.fetchall():
        rt = (row[0] or "").strip().lower() if len(row) > 0 else ""
        actx = (row[1] or "").strip().lower() if len(row) > 1 else "unknown"
        count = int(row[2] or 0) if len(row) > 2 else 0
        if rt == "read":
            if actx == "owner_self":
                attr["owner_self_reads"] += count
            elif actx == "grantee":
                attr["grantee_reads"] += count
            elif actx == "routine":
                attr["routine_reads"] += count
            else:
                attr["unknown_reads"] += count
        elif rt == "write":
            if actx == "owner_self":
                attr["owner_self_writes"] += count
            elif actx == "grantee":
                attr["grantee_writes"] += count
            elif actx == "routine":
                attr["routine_writes"] += count
            else:
                attr["unknown_writes"] += count
    return attr


def _ensure_uma_access_requests_indexes(conn: sqlite3.Connection) -> None:
    try:
        # DDL takes SQLite's write lock at execute time — gate it with the
        # commit like every other writer (write_gate lock-order inversion).
        with with_db_write():
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_uma_access_requests_owner_created "
                f"ON {UMA_ACCESS_REQUESTS_TABLE}(owner_user_id, created_at)"
            )
            commit_connection(conn)
    except Exception as exc:
        logger.debug("Failed to ensure uma_access_requests indexes: %s", exc)


def _ensure_mcp_request_log_indexes(conn: sqlite3.Connection) -> None:
    try:
        with with_db_write():
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_mcp_request_log_requested_at "
                f"ON {MCP_REQUEST_LOG_TABLE}(requested_at)"
            )
            commit_connection(conn)
    except Exception as exc:
        logger.debug("Failed to ensure mcp_request_log indexes: %s", exc)


def derive_mcp_access_context(resource_owner_user_id: Optional[str], requester_id: Optional[str]) -> str:
    """Classify MCP tool calls against this engine's linked owner user_id."""
    rid = (requester_id or "").strip()
    if not rid:
        return "unknown"
    oid = (resource_owner_user_id or "").strip()
    if oid and oid == rid:
        return "owner_self"
    return "other"


from ..analytics.duckdb_adapter import DuckDBAdapter
from ..config.settings import settings
from ..control_plane_client import ControlPlaneClient
from ..storage.db.paths import get_data_directory
from ..sync.client import SyncClient
from ..websocket_client import CloudWebSocketClient
from ..openai_client import OpenAIClient

logger = logging.getLogger("topos.core.state")

ws_client = CloudWebSocketClient(api_key=settings.gt_cloud_api_key)
openai_client = OpenAIClient()
control_plane_client: ControlPlaneClient | None = None

db_conn: sqlite3.Connection | None = None
_db_conn_path: str | None = None  # resolved path for current db_conn; invalidate if settings path changes
#: Thread that owns ``db_conn`` — every other thread gets its own connection to
#: the same file (see ``get_db_connection``). None until the database is opened.
_conn_owner_thread: int | None = None
#: Per-thread connections keyed implicitly by thread identity.
_thread_state = threading.local()
#: Serializes every read/validate/close/swap of the owner globals above.
#: Without it, one thread evicting ``db_conn`` on a settings-path change
#: closes the connection while another thread is mid-``execute`` on it — a
#: use-after-close on the C sqlite3 object that SIGBUSed the test process on
#: 2026-08-08. Lock-order rule: NEVER acquire the DB write gate while holding
#: this lock (gated writers call ``get_db_connection`` constantly, and
#: migrations take the gate — see ``_open_owner_db_connection``).
_owner_conn_lock = threading.RLock()

# Re-export write gate so callers can `from topos.core.state import with_db_write`.
from ..storage.db.write_gate import (  # noqa: E402
    batched_writes,
    commit_connection,
    db_write_lock,
    register_connection,
    sqlite_retry_busy,
    unregister_connection,
    with_db_write,
)

oplog_manager: Any = None
projection_manager: Any = None
encryption_manager: Any = None
analytics: DuckDBAdapter | None = None
sync_client: SyncClient | None = None
engine_presence_task: asyncio.Task | None = None
hosted_pool_lease_client: Any = None
hosted_pool_lease_task: asyncio.Task | None = None


def _resolve_database_path_from_settings() -> Optional[Path]:
    """Pick the SQLite file path from settings and default search order.

    When ``database_path`` is unset, the first existing file among canonical candidates wins
    (same order as before). Intentionally does not log: this runs on almost every API call.
    """
    from ..storage.db.paths import get_database_path
    from ..config.settings import settings

    if settings.topos_database_path:
        return Path(settings.topos_database_path)

    potential_paths: list[Path] = []
    potential_paths.append(get_database_path(settings.topos_database_path))
    if platform.system() == "Darwin":
        engine_path = Path.home() / "Library" / "Application Support" / "ToposEngine" / "database.db"
        potential_paths.append(engine_path)
    legacy_path = Path.home() / ".topos_engine" / "database.db"
    potential_paths.append(legacy_path)
    simple_path = Path.home() / ".topos" / "database.db"
    potential_paths.append(simple_path)

    for path in potential_paths:
        if path.exists() and path.is_file():
            return path
    return get_database_path(settings.topos_database_path)


def get_db_connection() -> Optional[sqlite3.Connection]:
    """The calling thread's database connection.

    A ``sqlite3.Connection`` holds exactly ONE transaction state, so handing the
    same object to concurrent threads corrupts it — that is what produced

        cannot start a transaction within a transaction
        cannot commit - no transaction is active
        Safety level may not be changed inside a transaction

    and cost an 88-record batch its facts on 2026-07-30.
    ``check_same_thread=False`` silenced Python's guard; it never made the
    connection safe. The concurrency is structural: the graph refresher runs on
    its own ``threading.Timer`` thread and enrichment jobs run in
    ``asyncio.to_thread`` workers, all against this one function.

    So each thread now gets its OWN connection to the same file. WAL already
    allows concurrent readers plus one writer, and ``write_gate`` still
    serializes writers. The signature is deliberately unchanged so all ~457 call
    sites keep working untouched.

    The owner thread (whoever opened the database first) keeps the module-global
    ``db_conn``: migrations run there exactly once, and code that injects a
    handle — notably tests using ``:memory:``, which CANNOT be reopened per
    thread because each connection would get its own empty database — keeps
    working as before.
    """
    owner = _get_owner_db_connection()
    if owner is None:
        return None

    with _owner_conn_lock:
        owner_path = _db_conn_path
        owner_thread = _conn_owner_thread

    # In-memory databases live only inside their connection; per-thread copies
    # would be empty. Share it, as before.
    if owner_path is None:
        return owner

    if threading.get_ident() == owner_thread:
        return owner

    return _get_thread_db_connection(owner_path)


#: What a ``SELECT 1`` liveness probe treats as "this handle is finished, open a
#: new one".
#:
#: ``KeyError`` is the odd one, and it is load-bearing. CPython's sqlite3 keeps a
#: per-connection prepared-statement cache whose key is the 1-tuple ``(sql,)``;
#: the C LRU behind it is unsynchronized, so concurrent use of one Connection can
#: leave its list and dict disagreeing. The eviction path then deletes a key that
#: is already gone and ``PyDict_DelItem`` raises ``KeyError(('<sql>',))`` — from
#: every later ``execute`` on that handle, naming a statement unrelated to the
#: caller. On 2026-08-17 that killed the node's database for nearly two hours
#: while ``/healthcheck`` kept answering ok, because nothing recognised the handle
#: as dead and swapped it.
#:
#: The cache cannot simply be turned off: ``cached_statements=0`` is clamped to a
#: minimum of 5 on CPython 3.10 (measured — 0, 1 and 4 all behave exactly like 5),
#: so recycling the poisoned handle is the available remedy. Preventing the race
#: is the job of ``run_db_read``/``run_db_write`` resolving per-worker handles.
_UNHEALTHY_CONNECTION_ERRORS = (sqlite3.ProgrammingError, sqlite3.OperationalError, KeyError)


def _get_thread_db_connection(resolved_path: str) -> Optional[sqlite3.Connection]:
    """A connection to ``resolved_path`` private to the calling thread.

    Migrations and backfills are NOT re-run here: the owner connection has
    already applied them by the time any other thread can reach this.
    """
    cached = getattr(_thread_state, "conn", None)
    cached_path = getattr(_thread_state, "path", None)
    if cached is not None and cached_path == resolved_path:
        try:
            cached.execute("SELECT 1")
            _ensure_row_factory(cached)
            return cached
        except _UNHEALTHY_CONNECTION_ERRORS:
            unregister_connection(cached)
            try:
                cached.close()
            except Exception:  # noqa: BLE001
                pass
            _thread_state.conn = None
            _thread_state.path = None
    elif cached is not None:
        # Path changed under us (tests re-point DATABASE_PATH).
        unregister_connection(cached)
        try:
            cached.close()
        except Exception:  # noqa: BLE001
            pass
        _thread_state.conn = None
        _thread_state.path = None

    try:
        conn = sqlite3.connect(resolved_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        logger.warning("Failed to open per-thread database connection: %s", exc)
        return None

    try:
        from ..storage.db.connection_tuning import tune_connection

        tune_connection(conn)
    except Exception as exc:  # noqa: BLE001 — tuning is an optimization, not a gate
        logger.debug("Per-thread connection tuning skipped: %s", exc)

    register_connection(conn)
    _thread_state.conn = conn
    _thread_state.path = resolved_path
    logger.debug(
        "Opened per-thread database connection thread=%s path=%s",
        threading.get_ident(),
        resolved_path,
    )
    return conn


def close_thread_db_connection() -> None:
    """Close the calling thread's connection. For worker teardown and tests."""
    conn = getattr(_thread_state, "conn", None)
    if conn is None:
        return
    unregister_connection(conn)
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    _thread_state.conn = None
    _thread_state.path = None


def _get_owner_db_connection() -> Optional[sqlite3.Connection]:
    """Establish/validate the owner connection (migrations run here, once).

    Every touch of the owner globals happens under ``_owner_conn_lock``. The
    expensive part — opening a NEW connection and bringing its schema current —
    happens OUTSIDE the lock on a local handle (see the lock-order rule at the
    lock's definition), then the winner is swapped in under the lock. Two
    threads racing an initial open both build a connection; the loser's is
    closed at the swap.
    """
    global db_conn, _db_conn_path, _conn_owner_thread

    from ..config.settings import settings

    with _owner_conn_lock:
        # Respect injected in-memory sqlite handles used by tests, even if _db_conn_path
        # still points to a stale file-backed connection from a prior run.
        if db_conn is not None:
            try:
                row = db_conn.execute("PRAGMA database_list").fetchone()
                if row is not None:
                    db_file = str(row[2] or "").strip()  # seq, name, file
                    if db_file in {"", ":memory:"}:
                        _db_conn_path = None
                        return db_conn
                    if _db_conn_path is None:
                        _db_conn_path = str(Path(db_file).resolve())
            except Exception:
                pass

        if db_conn is not None and settings.topos_database_path and _db_conn_path:
            try:
                db_conn.execute("SELECT 1")
            except _UNHEALTHY_CONNECTION_ERRORS:
                unregister_connection(db_conn)
                db_conn = None
                _db_conn_path = None
            else:
                try:
                    explicit_resolved = str(Path(settings.topos_database_path).resolve())
                except (OSError, RuntimeError):
                    explicit_resolved = None
                if explicit_resolved and explicit_resolved == _db_conn_path:
                    return db_conn

        db_path: Path | None = None
        try:
            db_path = _resolve_database_path_from_settings()
        except Exception as e:
            logger.warning("Failed to determine database path: %s", e)
            return None

        if not db_path:
            return None

        resolved = str(db_path.resolve())
        if db_conn is not None and _db_conn_path != resolved:
            # Deliberately NOT closed: the lock serializes this function, but a
            # caller elsewhere may hold this handle mid-execute (handlers keep
            # it across queries) — closing under them is the use-after-close
            # that SIGBUSed on 2026-08-08. Dropping the reference lets the last
            # user finish; the handle closes (rolling back any open
            # transaction) when its refcount reaches zero. The watchdog is told
            # here for the same reason it holds no strong reference: it must
            # not be what keeps this handle (or its stale record) around.
            unregister_connection(db_conn)
            db_conn = None
            _db_conn_path = None

        if db_conn is not None:
            try:
                db_conn.execute("SELECT 1")
                # Scrub/adapters may have cleared Row factory on the shared conn.
                if getattr(db_conn, "row_factory", None) is not sqlite3.Row:
                    db_conn.row_factory = sqlite3.Row
                return db_conn
            except _UNHEALTHY_CONNECTION_ERRORS:
                unregister_connection(db_conn)
                db_conn = None
                _db_conn_path = None

    new_conn = _open_owner_db_connection(db_path)
    if new_conn is None:
        return None

    with _owner_conn_lock:
        # Another thread may have opened (or a test injected) a connection
        # while we were migrating. Keep theirs, discard ours — the next call
        # re-validates path agreement.
        if db_conn is not None:
            try:
                db_conn.execute("SELECT 1")
            except Exception:
                unregister_connection(db_conn)
                try:
                    db_conn.close()
                except Exception:  # noqa: BLE001
                    pass
            else:
                unregister_connection(new_conn)
                try:
                    new_conn.close()
                except Exception:  # noqa: BLE001
                    pass
                return db_conn
        db_conn = new_conn
        _db_conn_path = resolved
        _conn_owner_thread = threading.get_ident()
        return db_conn


def _open_owner_db_connection(db_path: Path) -> Optional[sqlite3.Connection]:
    """Open ``db_path``, tune it, and bring its schema current — on a LOCAL handle.

    Deliberately called without ``_owner_conn_lock`` held: migrations acquire
    the DB write gate, and gated writers call ``get_db_connection`` — holding
    the owner lock across the gate would invert lock order and deadlock. The
    connection only becomes ``db_conn`` after this returns, so no other thread
    can observe a half-migrated database.
    """
    conn: sqlite3.Connection | None = None
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        logger.debug("Created database connection: %s", db_path)
        try:
            from ..storage.db.connection_tuning import tune_connection

            tuning = tune_connection(conn)
            logger.info(
                "DB tuning: journal_mode=%s sqlite_vec=%s",
                tuning.get("journal_mode"),
                tuning.get("sqlite_vec"),
            )
        except Exception as tuning_exc:  # noqa: BLE001
            logger.warning("Connection tuning skipped: %s", tuning_exc)
        # Registered before migrations run, not after: a migration that returns
        # without committing wedges every writer behind it exactly like any
        # other ungated write, and startup is when the database is least
        # observable.
        register_connection(conn)
        try:
            from ..storage.db.migrations import (
                DowngradeGuardError,
                MigrationError,
                ensure_migrations_applied,
            )

            ensure_migrations_applied(conn)
        except (MigrationError, DowngradeGuardError) as migration_exc:
            # Shape migrations must fail loud — serving a half-migrated DB corrupts data.
            logger.error("Schema migration failed; refusing to open database: %s", migration_exc)
            unregister_connection(conn)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as migration_exc:  # noqa: BLE001
            from ..storage.db.migrations import MigrationError as _MigrationError

            logger.error(
                "Unexpected schema migration failure; refusing to open database: %s",
                migration_exc,
            )
            unregister_connection(conn)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            raise _MigrationError(str(migration_exc)) from migration_exc
        try:
            from ..storage.raw.browser_flat_tables import backfill_browser_visits_from_raw_retention

            backfill_browser_visits_from_raw_retention(conn)
        except Exception as backfill_exc:  # noqa: BLE001
            logger.debug("browser_visits raw→flat backfill skipped: %s", backfill_exc)
        return conn
    except Exception as e:
        # Shape-migration failures must propagate (fail loud). Only soft-fail
        # unrelated connection errors so callers can treat conn=None as "no DB".
        from ..storage.db.migrations import DowngradeGuardError, MigrationError

        if isinstance(e, (MigrationError, DowngradeGuardError)):
            raise
        logger.warning("Failed to create database connection: %s", e)
        if conn is not None:
            unregister_connection(conn)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return None


def _ensure_row_factory(conn: sqlite3.Connection) -> None:
    """Shared engine conn can lose Row factory (e.g. scrub paths); restore it."""
    if getattr(conn, "row_factory", None) is not sqlite3.Row:
        conn.row_factory = sqlite3.Row


def get_or_create_user_id(conn: sqlite3.Connection) -> str:
    """Get existing user_id or create a new one.
    
    Ensures engine_config table exists before accessing it.
    """
    try:
        _ensure_row_factory(conn)
        _ensure_engine_config_table(conn)
        cursor = conn.execute("SELECT value FROM engine_config WHERE key = 'user_id'")
        row = cursor.fetchone()
        if row:
            return row["value"]
        user_id = str(uuid.uuid4())
        with with_db_write():
            conn.execute(
                "INSERT INTO engine_config (key, value) VALUES (?, ?)",
                ("user_id", user_id),
            )
            commit_connection(conn)
        return user_id
    except Exception as exc:
        logger.error("Failed to get or create user_id: %s", exc, exc_info=True)
        raise


def store_user_id(conn: sqlite3.Connection, user_id: str) -> None:
    """Store user_id in engine_config table.
    
    Ensures engine_config table exists before writing.
    """
    try:
        _ensure_engine_config_table(conn)
        with with_db_write():
            conn.execute(
                "INSERT OR REPLACE INTO engine_config (key, value) VALUES (?, ?)",
                ("user_id", user_id),
            )
            commit_connection(conn)
    except Exception as exc:
        logger.error("Failed to store user_id: %s", exc, exc_info=True)
        raise


def get_user_id(conn: sqlite3.Connection) -> str | None:
    """Get user_id from engine_config table.
    
    Ensures engine_config table exists before accessing it.
    Returns None if user_id doesn't exist or on error.
    """
    try:
        _ensure_row_factory(conn)
        _ensure_engine_config_table(conn)
        cursor = conn.execute("SELECT value FROM engine_config WHERE key = 'user_id'")
        row = cursor.fetchone()
        if row:
            return row["value"]
        return None
    except Exception as exc:
        logger.warning("Failed to get user_id: %s", exc)
        return None


def _ensure_engine_config_table(conn: sqlite3.Connection) -> None:
    """Ensure engine_config table exists, creating it if necessary."""
    try:
        # Check if table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='engine_config'"
        )
        if not cursor.fetchone():
            # Create table if it doesn't exist
            logger.debug("Creating engine_config table...")
            with with_db_write():
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS engine_config (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                commit_connection(conn)
            logger.debug("engine_config table created successfully")
    except Exception as exc:
        logger.warning("Failed to ensure engine_config table exists: %s", exc)


def get_engine_config_value(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """Get a value from engine_config table.
    
    If the table doesn't exist, it will be created automatically.
    Returns None if the key doesn't exist or on error.
    """
    try:
        _ensure_row_factory(conn)
        _ensure_engine_config_table(conn)
        cursor = conn.execute("SELECT value FROM engine_config WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            return row["value"]
        return None
    except Exception as exc:
        # Only log as warning for missing table (expected on fresh databases)
        # Log as error for other issues
        if "no such table" in str(exc).lower():
            logger.debug("engine_config table not found, will be created on next write: %s", key)
        else:
            logger.warning("Failed to get engine_config[%s]: %s", key, exc)
        return None


def set_engine_config_value(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a value in engine_config table.
    
    If the table doesn't exist, it will be created automatically.
    """
    try:
        _ensure_engine_config_table(conn)
        with with_db_write():
            conn.execute(
                "INSERT OR REPLACE INTO engine_config (key, value) VALUES (?, ?)",
                (key, value),
            )
            commit_connection(conn)
    except Exception as exc:
        logger.error("Failed to set engine_config[%s]: %s", key, exc, exc_info=True)
        raise


def _ensure_mcp_request_log_table(conn: sqlite3.Connection) -> None:
    """Ensure mcp_request_log table exists (engine-owned MCP request counting). Includes source and requester_id for per-source display."""
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (MCP_REQUEST_LOG_TABLE,),
        )
        if not cursor.fetchone():
            with with_db_write():
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {MCP_REQUEST_LOG_TABLE} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool_name TEXT NOT NULL,
                        requested_at TEXT NOT NULL DEFAULT (datetime('now')),
                        source TEXT,
                        requester_id TEXT,
                        access_context TEXT
                    )
                """)
                commit_connection(conn)
            logger.debug("mcp_request_log table created successfully")
            _ensure_mcp_request_log_indexes(conn)
            return
        # Add source/requester_id columns if missing (migration for existing DBs)
        cursor = conn.execute(f"PRAGMA table_info({MCP_REQUEST_LOG_TABLE})")
        columns = [row[1] for row in cursor.fetchall()]
        missing = [
            col for col in ("source", "requester_id", "access_context") if col not in columns
        ]
        if missing:
            with with_db_write():
                for col in missing:
                    conn.execute(f"ALTER TABLE {MCP_REQUEST_LOG_TABLE} ADD COLUMN {col} TEXT")
                commit_connection(conn)
        _ensure_mcp_request_log_indexes(conn)
    except Exception as exc:
        logger.warning("Failed to ensure mcp_request_log table exists: %s", exc)


def record_mcp_request(
    conn: Optional[sqlite3.Connection],
    tool_name: str,
    source: Optional[str] = None,
    requester_id: Optional[str] = None,
    resource_owner_user_id: Optional[str] = None,
    *,
    uma_resource_id: Optional[str] = None,
) -> None:
    """
    Record one MCP tool invocation in the engine's DB.
    source: e.g. "claude_desktop", "chatgpt", "routine_executor". requester_id: identity of requester.
    resource_owner_user_id: engine-linked owner; used to set access_context (owner_self vs other).
    When source is routine_executor, also writes a UMA access row tagged as Topos Routine.
    """
    if not conn or not tool_name:
        return
    try:
        _ensure_mcp_request_log_table(conn)
        requested_at = datetime.now(timezone.utc).isoformat()
        access_ctx = derive_mcp_access_context(resource_owner_user_id, requester_id)
        if is_routine_mcp_source(source):
            access_ctx = ROUTINE_ACCESS_CONTEXT
        with with_db_write():
            conn.execute(
                f"INSERT INTO {MCP_REQUEST_LOG_TABLE} (tool_name, requested_at, source, requester_id, access_context) VALUES (?, ?, ?, ?, ?)",
                (tool_name, requested_at, source or None, requester_id or None, access_ctx),
            )
            commit_connection(conn)
        if is_routine_mcp_source(source) and resource_owner_user_id:
            record_uma_request(
                conn,
                owner_user_id=resource_owner_user_id,
                resource_id=(uma_resource_id or "").strip() or f"routine:{tool_name}",
                request_type="read",
                endpoint=tool_name,
                requesting_user_id=(requester_id or "").strip() or resource_owner_user_id,
                app_id=ROUTINE_APP_ID,
                access_channel=ROUTINE_ACCESS_CHANNEL,
                access_context=ROUTINE_ACCESS_CONTEXT,
            )
    except Exception as exc:
        logger.debug("Failed to record MCP request (non-critical): %s", exc)


def _ensure_uma_access_requests_table(conn: sqlite3.Connection) -> None:
    """Ensure uma_access_requests table exists (engine-owned UMA request counting)."""
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (UMA_ACCESS_REQUESTS_TABLE,),
        )
        if not cursor.fetchone():
            with with_db_write():
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {UMA_ACCESS_REQUESTS_TABLE} (
                        id TEXT PRIMARY KEY,
                        owner_user_id TEXT NOT NULL,
                        requesting_user_id TEXT,
                        requesting_user_email TEXT,
                        app_id TEXT,
                        resource_id TEXT NOT NULL,
                        request_type TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        access_channel TEXT,
                        access_context TEXT,
                        created_at TEXT NOT NULL
                    )
                """)
                commit_connection(conn)
            logger.debug("uma_access_requests table created successfully")
            _ensure_uma_access_requests_indexes(conn)
            return
        cursor = conn.execute(f"PRAGMA table_info({UMA_ACCESS_REQUESTS_TABLE})")
        columns = [row[1] for row in cursor.fetchall()]
        missing = [
            (col, ddl)
            for col, ddl in (
                ("requesting_user_email", "TEXT"),
                ("access_channel", "TEXT"),
                ("access_context", "TEXT"),
            )
            if col not in columns
        ]
        if missing:
            with with_db_write():
                for col, ddl in missing:
                    conn.execute(f"ALTER TABLE {UMA_ACCESS_REQUESTS_TABLE} ADD COLUMN {col} {ddl}")
                commit_connection(conn)
    except Exception as exc:
        logger.warning("Failed to ensure uma_access_requests table exists: %s", exc)
    else:
        _ensure_uma_access_requests_indexes(conn)
        _migrate_legacy_browser_plugin_app_ids(conn)


def _migrate_legacy_browser_plugin_app_ids(conn: sqlite3.Connection) -> int:
    """Rewrite engine Activity counts from browser-plugin → browser-history-plugin (idempotent)."""
    try:
        with with_db_write():
            cursor = conn.execute(
                f"UPDATE {UMA_ACCESS_REQUESTS_TABLE} SET app_id = ? WHERE app_id = ?",
                (BROWSER_HISTORY_PLUGIN_APP_ID, LEGACY_BROWSER_PLUGIN_APP_ID),
            )
            # Commit even for 0 rows: the UPDATE opened an implicit transaction
            # either way, and this runs at startup on the owner connection.
            commit_connection(conn)
        if cursor.rowcount:
            logger.info(
                "Migrated %s uma_access_requests rows: %s → %s",
                cursor.rowcount,
                LEGACY_BROWSER_PLUGIN_APP_ID,
                BROWSER_HISTORY_PLUGIN_APP_ID,
            )
        return int(cursor.rowcount or 0)
    except Exception as exc:
        logger.warning("browser-plugin app_id migration on engine failed: %s", exc)
        return 0


def record_uma_request(
    conn: Optional[sqlite3.Connection],
    owner_user_id: str,
    resource_id: str,
    request_type: str,
    endpoint: str,
    requesting_user_id: Optional[str] = None,
    app_id: Optional[str] = None,
    *,
    requesting_user_email: Optional[str] = None,
    access_channel: Optional[str] = None,
    access_context: Optional[str] = None,
) -> None:
    """
    Record one UMA access request in the engine's DB (read or write from connected apps).
    Mirror of CP's Supabase recording; engine owns this data for request counts.
    """
    if not conn or not owner_user_id or not resource_id or not request_type or not endpoint:
        return
    try:
        _ensure_uma_access_requests_table(conn)
        row_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        ctx = access_context or derive_uma_access_context(owner_user_id, requesting_user_id)
        ch = (access_channel or "").strip() or None
        email = (requesting_user_email or "").strip() or None
        with with_db_write():
            conn.execute(
                f"""INSERT INTO {UMA_ACCESS_REQUESTS_TABLE}
                    (id, owner_user_id, requesting_user_id, requesting_user_email, app_id, resource_id, request_type, endpoint, access_channel, access_context, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row_id,
                    owner_user_id,
                    (requesting_user_id or "").strip() or None,
                    email,
                    app_id or None,
                    resource_id,
                    request_type,
                    endpoint,
                    ch,
                    ctx,
                    created_at,
                ),
            )
            commit_connection(conn)
    except Exception as exc:
        logger.debug("Failed to record UMA request (non-critical): %s", exc)


def get_uma_request_counts(
    conn: Optional[sqlite3.Connection],
    owner_user_id: str,
    since_days: Optional[int] = 90,
) -> Dict[str, Any]:
    """Return UMA request counts from engine DB. Extends CP shape with attribution + top requesters."""
    out: Dict[str, Any] = {
        "total_read_requests": 0,
        "total_write_requests": 0,
        "by_app": [],
        "by_app_requester": [],
        "by_requesting_user": [],
        "access_attribution": _empty_access_attribution(since_days or 0),
        "access_attribution_all_time": _empty_access_attribution(0),
        "access_attribution_24h": _empty_access_attribution(0),
    }
    if not conn or not owner_user_id:
        return out
    try:
        _ensure_uma_access_requests_table(conn)
        for req_type in ("read", "write"):
            cursor = conn.execute(
                f"SELECT COUNT(*) FROM {UMA_ACCESS_REQUESTS_TABLE} WHERE owner_user_id = ? AND request_type = ?",
                (owner_user_id, req_type),
            )
            row = cursor.fetchone()
            count = row[0] if row else 0
            if req_type == "read":
                out["total_read_requests"] = count
            else:
                out["total_write_requests"] = count
        out["access_attribution_all_time"] = _aggregate_uma_access_attribution(
            conn, owner_user_id, window_days=0
        )
        since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        out["access_attribution_24h"] = _aggregate_uma_access_attribution(
            conn, owner_user_id, since_ts=since_24h, window_days=0
        )
        if since_days and since_days > 0:
            since_ts = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
            cursor = conn.execute(
                f"SELECT app_id, request_type FROM {UMA_ACCESS_REQUESTS_TABLE} WHERE owner_user_id = ? AND created_at >= ?",
                (owner_user_id, since_ts),
            )
            by_app: Dict[str, Dict[str, int]] = {}
            for row in cursor.fetchall():
                app_id_val = (row[0] or "") if len(row) > 0 else ""
                rt = (row[1] or "") if len(row) > 1 else ""
                if app_id_val not in by_app:
                    by_app[app_id_val] = {"read_requests": 0, "write_requests": 0}
                if rt == "read":
                    by_app[app_id_val]["read_requests"] += 1
                elif rt == "write":
                    by_app[app_id_val]["write_requests"] += 1
            out["by_app"] = [
                {"app_id": aid, "app_name": None, "read_requests": d["read_requests"], "write_requests": d["write_requests"]}
                for aid, d in sorted(by_app.items())
            ]

            cursor = conn.execute(
                f"""SELECT app_id, requesting_user_id, requesting_user_email, request_type
                    FROM {UMA_ACCESS_REQUESTS_TABLE}
                    WHERE owner_user_id = ? AND created_at >= ?""",
                (owner_user_id, since_ts),
            )
            by_app_req: Dict[str, Dict[str, Any]] = {}
            for row in cursor.fetchall():
                app_id_val = (row[0] or "").strip() if len(row) > 0 else ""
                rid = (row[1] or "").strip() if len(row) > 1 else ""
                remail = (row[2] or "").strip() if len(row) > 2 else ""
                rt = (row[3] or "").strip().lower() if len(row) > 3 else ""
                key = f"{app_id_val}\0{rid}\0{remail}"
                if key not in by_app_req:
                    by_app_req[key] = {
                        "app_id": app_id_val or None,
                        "requesting_user_id": rid or None,
                        "requesting_user_email": remail or None,
                        "read_requests": 0,
                        "write_requests": 0,
                    }
                if rt == "read":
                    by_app_req[key]["read_requests"] += 1
                elif rt == "write":
                    by_app_req[key]["write_requests"] += 1
            app_req_ranked = []
            for d in by_app_req.values():
                tr = int(d["read_requests"]) + int(d["write_requests"])
                app_req_ranked.append({**d, "total_requests": tr})
            app_req_ranked.sort(key=lambda x: -x["total_requests"])
            out["by_app_requester"] = app_req_ranked[:50]

            cursor = conn.execute(
                f"""SELECT requesting_user_id, requesting_user_email, request_type, access_context
                    FROM {UMA_ACCESS_REQUESTS_TABLE}
                    WHERE owner_user_id = ? AND created_at >= ?""",
                (owner_user_id, since_ts),
            )
            by_req: Dict[str, Dict[str, Any]] = {}
            attr = out["access_attribution"]
            attr["window_days"] = int(since_days)
            for row in cursor.fetchall():
                rid = (row[0] or "").strip() if len(row) > 0 else ""
                remail = (row[1] or "").strip() if len(row) > 1 else ""
                rt = (row[2] or "").strip().lower() if len(row) > 2 else ""
                actx = (row[3] or "").strip().lower() if len(row) > 3 else ""
                if not actx:
                    actx = derive_uma_access_context(owner_user_id, rid or None)
                is_read = rt == "read"
                is_write = rt == "write"
                if actx == "owner_self":
                    if is_read:
                        attr["owner_self_reads"] += 1
                    elif is_write:
                        attr["owner_self_writes"] += 1
                elif actx == "grantee":
                    if is_read:
                        attr["grantee_reads"] += 1
                    elif is_write:
                        attr["grantee_writes"] += 1
                elif actx == "routine":
                    if is_read:
                        attr["routine_reads"] += 1
                    elif is_write:
                        attr["routine_writes"] += 1
                else:
                    if is_read:
                        attr["unknown_reads"] += 1
                    elif is_write:
                        attr["unknown_writes"] += 1
                key = rid if rid else (f"unknown:{remail}" if remail else "unknown")
                if key not in by_req:
                    by_req[key] = {
                        "requesting_user_id": rid or None,
                        "requesting_user_email": remail or None,
                        "read_requests": 0,
                        "write_requests": 0,
                    }
                if is_read:
                    by_req[key]["read_requests"] += 1
                elif is_write:
                    by_req[key]["write_requests"] += 1
            ranked = []
            for _k, d in by_req.items():
                tr = int(d["read_requests"]) + int(d["write_requests"])
                ranked.append({**d, "total_requests": tr})
            ranked.sort(key=lambda x: -x["total_requests"])
            out["by_requesting_user"] = ranked[:50]
    except Exception as exc:
        logger.debug("get_uma_request_counts failed: %s", exc)
    return out


def get_mcp_request_counts(
    conn: Optional[sqlite3.Connection],
    since_days: Optional[int] = 90,
) -> Dict[str, Any]:
    """Return MCP request counts from engine DB: by_source (identity + count), by_tool, by_access_context, total."""
    out: Dict[str, Any] = {
        "by_source": [],
        "by_tool": [],
        "by_access_context": [],
        "total": 0,
        "total_all_time": 0,
        "total_24h": 0,
        "by_source_all_time": [],
        "owner_self_all_time": 0,
        "other_all_time": 0,
        "owner_self_24h": 0,
        "other_24h": 0,
    }
    if not conn:
        return out
    try:
        _ensure_mcp_request_log_table(conn)
        since_ts = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat() if (since_days and since_days > 0) else None
        if since_ts:
            cursor = conn.execute(
                f"SELECT source, requester_id, access_context FROM {MCP_REQUEST_LOG_TABLE} WHERE requested_at >= ?",
                (since_ts,),
            )
        else:
            cursor = conn.execute(f"SELECT source, requester_id, access_context FROM {MCP_REQUEST_LOG_TABLE}")
        rows = cursor.fetchall()
        by_key: Dict[tuple, int] = {}
        by_ctx: Dict[str, int] = {}
        for row in rows:
            src = (row[0] or "") if len(row) > 0 else ""
            rid = (row[1] or "") if len(row) > 1 else ""
            actx = (row[2] or "").strip() if len(row) > 2 else ""
            if not actx:
                actx = "unknown"
            key = (src, rid)
            by_key[key] = by_key.get(key, 0) + 1
            by_ctx[actx] = by_ctx.get(actx, 0) + 1
        out["by_source"] = [
            {"source": src, "requester_id": rid or None, "count": c}
            for (src, rid), c in sorted(by_key.items(), key=lambda x: -x[1])
        ]
        out["by_access_context"] = [
            {"access_context": k, "count": v} for k, v in sorted(by_ctx.items(), key=lambda x: -x[1])
        ]
        if since_ts:
            cursor = conn.execute(
                f"SELECT tool_name FROM {MCP_REQUEST_LOG_TABLE} WHERE requested_at >= ?",
                (since_ts,),
            )
        else:
            cursor = conn.execute(f"SELECT tool_name FROM {MCP_REQUEST_LOG_TABLE}")
        tool_rows = cursor.fetchall()
        by_tool: Dict[str, int] = {}
        for row in tool_rows:
            t = (row[0] or "") if row else ""
            by_tool[t] = by_tool.get(t, 0) + 1
        out["by_tool"] = [{"tool_name": t, "count": c} for t, c in sorted(by_tool.items(), key=lambda x: -x[1])]
        out["total"] = sum(by_tool.values())

        cursor = conn.execute(f"SELECT COUNT(*) FROM {MCP_REQUEST_LOG_TABLE}")
        row = cursor.fetchone()
        out["total_all_time"] = int(row[0] or 0) if row else 0

        since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM {MCP_REQUEST_LOG_TABLE} WHERE requested_at >= ?",
            (since_24h,),
        )
        row = cursor.fetchone()
        out["total_24h"] = int(row[0] or 0) if row else 0

        cursor = conn.execute(
            f"""SELECT
                    COALESCE(NULLIF(TRIM(source), ''), NULLIF(TRIM(requester_id), ''), 'other') AS src,
                    COUNT(*) AS cnt
                FROM {MCP_REQUEST_LOG_TABLE}
                GROUP BY src
                ORDER BY cnt DESC
                LIMIT 6"""
        )
        out["by_source_all_time"] = [
            {"source": (r[0] or "other") if len(r) > 0 else "other", "requester_id": None, "count": int(r[1] or 0) if len(r) > 1 else 0}
            for r in cursor.fetchall()
        ]

        def _mcp_owner_other_counts(since: Optional[str]) -> tuple[int, int]:
            owner_self = 0
            other = 0
            bucket_sql = (
                "CASE WHEN LOWER(COALESCE(access_context, '')) = 'owner_self' "
                "THEN 'owner_self' ELSE 'other' END"
            )
            if since:
                cursor = conn.execute(
                    f"SELECT {bucket_sql} AS bucket, COUNT(*) FROM {MCP_REQUEST_LOG_TABLE} "
                    f"WHERE requested_at >= ? GROUP BY bucket",
                    (since,),
                )
            else:
                cursor = conn.execute(
                    f"SELECT {bucket_sql} AS bucket, COUNT(*) FROM {MCP_REQUEST_LOG_TABLE} GROUP BY bucket"
                )
            for mrow in cursor.fetchall():
                bucket = (mrow[0] or "").strip().lower() if len(mrow) > 0 else "other"
                count = int(mrow[1] or 0) if len(mrow) > 1 else 0
                if bucket == "owner_self":
                    owner_self = count
                else:
                    other += count
            return owner_self, other

        out["owner_self_all_time"], out["other_all_time"] = _mcp_owner_other_counts(None)
        out["owner_self_24h"], out["other_24h"] = _mcp_owner_other_counts(since_24h)
    except Exception as exc:
        logger.debug("get_mcp_request_counts failed: %s", exc)
    return out


def get_engine_mode() -> str:
    mode = (settings.engine_mode or "full").strip().lower()
    if mode not in {"full", "sync"}:
        return "full"
    if mode == "sync":
        return "sync"
    return "full" if settings.enable_llm else "sync"


def get_engine_class() -> str:
    return "sync_engine" if get_engine_mode() == "sync" else "full_engine"


def get_total_memory_bytes() -> Optional[int]:
    try:
        if hasattr(os, "sysconf"):
            page_size = os.sysconf("SC_PAGE_SIZE")
            pages = os.sysconf("SC_PHYS_PAGES")
            if isinstance(page_size, int) and isinstance(pages, int):
                return page_size * pages
    except Exception:
        return None
    return None


# Resolved at most once per process.
#
# get_device_info calls get_system_info twice per request (services/local.py,
# for engine_name and for the system dict), the control plane hits it on every
# websocket connect, and these are async handlers — an uncached subprocess.run
# blocks the event loop for its full timeout every time. Failures are cached
# too, deliberately: a Mac with ComputerName unset, or a slow configd, would
# otherwise stall EVERY request by up to the timeout, forever. One stall is a
# blip; a permanent per-request stall is an outage. The cost of caching a miss
# is a Topos named after the hostname instead, which is exactly the fallback
# that already exists.
_UNRESOLVED = object()
_computer_name_cache: Any = _UNRESOLVED


def _macos_computer_name() -> Optional[str]:
    """The name a Mac owner actually recognises — "Jonny's MacBook Pro".

    `platform.node()` returns the network hostname, which on macOS is a mangled
    form of the same thing ("jonnys-macbook-pro.local") or, on many networks,
    something meaningless from DHCP. A Topos named after it reads like a server.

    Read via scutil rather than a new dependency: pyobjc/SystemConfiguration
    would be a wheel added to every install for one string. Absolute path,
    bounded timeout, and every failure swallowed — a nicer label is never worth
    risking node startup. (Measured at 8ms, but a 1s bound proved tight enough to
    trip under load, and the first boot is the one call that has to answer.)
    """
    global _computer_name_cache
    if _computer_name_cache is not _UNRESOLVED:
        return _computer_name_cache
    if platform.system() != "Darwin":
        _computer_name_cache = None
        return None
    try:
        out = subprocess.run(
            ["/usr/sbin/scutil", "--get", "ComputerName"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        _computer_name_cache = (out.stdout or "").strip() or None
    except Exception:  # noqa: BLE001
        _computer_name_cache = None
    return _computer_name_cache


def get_system_info() -> Dict[str, Any]:
    disk_total = None
    disk_free = None
    try:
        # Get disk usage for the root filesystem (whole device)
        root_path = Path("/") if platform.system() != "Windows" else Path("C:\\")
        usage = shutil.disk_usage(root_path)
        disk_total = usage.total
        disk_free = usage.free
    except Exception as exc:
        logger.warning("Failed to get disk usage: %s", exc)
        disk_total = None
        disk_free = None
    return {
        "hostname": platform.node() or None,
        # Free-form dict (api_models.py types `system` as Dict[str, Any]) and the
        # control plane forwards it verbatim, so this reaches fleet status as
        # system_info.computer_name with no CP change at all.
        "computer_name": _macos_computer_name(),
        "os": platform.system() or None,
        "os_version": platform.version() or None,
        "platform": platform.platform() or None,
        "arch": platform.machine() or None,
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": get_total_memory_bytes(),
        "storage_total_bytes": disk_total,
        "storage_free_bytes": disk_free,
        "python_version": sys.version.split()[0],
        "gpu_name": None,
        "gpu_memory_bytes": None,
    }
