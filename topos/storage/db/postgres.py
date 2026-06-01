from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Any, Iterable, Sequence, Tuple

from ...config.settings import settings

logger = logging.getLogger("topos.storage.db.postgres")


class PostgresConfigurationError(RuntimeError):
    pass


def _build_postgres_dsn() -> str:
    if settings.topos_postgres_dsn:
        return settings.topos_postgres_dsn
    if settings.topos_database_service_url:
        return settings.topos_database_service_url

    required = {
        "TOPOS_POSTGRES_HOST": settings.topos_postgres_host,
        "TOPOS_POSTGRES_PORT": settings.topos_postgres_port,
        "TOPOS_POSTGRES_DB": settings.topos_postgres_db,
        "TOPOS_POSTGRES_USER": settings.topos_postgres_user,
        "TOPOS_POSTGRES_PASSWORD": settings.topos_postgres_password,
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise PostgresConfigurationError(
            "Missing Postgres settings: " + ", ".join(sorted(missing))
        )
    return (
        f"postgresql://{settings.topos_postgres_user}:{settings.topos_postgres_password}"
        f"@{settings.topos_postgres_host}:{int(settings.topos_postgres_port)}/{settings.topos_postgres_db}"
    )


def _is_sqlite_connection(conn: Any) -> bool:
    module_name = conn.__class__.__module__.lower()
    return "sqlite" in module_name


def _normalize_query_for_connection(conn: Any, query: str) -> str:
    if _is_sqlite_connection(conn):
        return query.replace("%s", "?")
    return query


def _table_exists(conn: Any, table_name: str) -> bool:
    if _is_sqlite_connection(conn):
        row = fetch_one(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        )
        return bool(row)
    row = fetch_one(
        conn,
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        LIMIT 1
        """,
        (table_name,),
    )
    return bool(row)


def _table_has_column(conn: Any, table_name: str, column_name: str) -> bool:
    if not _table_exists(conn, table_name):
        return False
    if _is_sqlite_connection(conn):
        rows = fetch_all(conn, f'PRAGMA table_info("{table_name}")')
        for row in rows:
            col = row["name"] if isinstance(row, dict) else row[1]
            if str(col) == column_name:
                return True
        return False
    row = fetch_one(
        conn,
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (table_name, column_name),
    )
    return bool(row)


def _drop_table_if_exists(conn: Any, table_name: str) -> None:
    execute_query(conn, f'DROP TABLE IF EXISTS "{table_name}" CASCADE')


def _ensure_postgres_tenant_schema_compat(conn: Any) -> None:
    """
    Detect legacy hosted schemas that predate tenant-aware columns.

    In dev/test we allow a destructive reset because the user has explicitly
    accepted data loss. For safety, reset is opt-in via
    TOPOS_POSTGRES_RESET_INCOMPATIBLE_SCHEMA.
    """
    if _is_sqlite_connection(conn):
        return

    requires_reset = any(
        _table_exists(conn, table) and not _table_has_column(conn, table, "tenant_id")
        for table in ("messages", "oplog", "engine_config")
    )
    if not requires_reset:
        return

    if not settings.topos_postgres_reset_incompatible_schema:
        raise PostgresConfigurationError(
            "Detected legacy Postgres schema without tenant_id on messages/oplog/engine_config. "
            "Set TOPOS_POSTGRES_RESET_INCOMPATIBLE_SCHEMA=true to reset these dev tables, or run a manual migration."
        )

    logger.warning(
        "Resetting legacy Postgres tables (messages, oplog, engine_config) to tenant-aware schema."
    )
    _drop_table_if_exists(conn, "messages")
    _drop_table_if_exists(conn, "oplog")
    _drop_table_if_exists(conn, "engine_config")


def execute_query(conn: Any, query: str, params: Sequence[Any] = ()) -> None:
    normalized = _normalize_query_for_connection(conn, query)
    with _cursor(conn) as cursor:
        cursor.execute(normalized, tuple(params))


def fetch_all(conn: Any, query: str, params: Sequence[Any] = ()) -> list[Tuple[Any, ...]]:
    normalized = _normalize_query_for_connection(conn, query)
    with _cursor(conn) as cursor:
        cursor.execute(normalized, tuple(params))
        rows = cursor.fetchall()
    return list(rows or [])


def fetch_one(conn: Any, query: str, params: Sequence[Any] = ()) -> Tuple[Any, ...] | None:
    normalized = _normalize_query_for_connection(conn, query)
    with _cursor(conn) as cursor:
        cursor.execute(normalized, tuple(params))
        row = cursor.fetchone()
    return row


@contextmanager
def _cursor(conn: Any):
    cursor = conn.cursor()
    try:
        yield cursor
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def ensure_postgres_schema(conn: Any) -> None:
    _ensure_postgres_tenant_schema_compat(conn)
    statements: Iterable[str] = (
        """
        CREATE TABLE IF NOT EXISTS messages (
            tenant_id TEXT NOT NULL,
            dataset_id TEXT NOT NULL,
            message_id TEXT PRIMARY KEY,
            sender_type TEXT NOT NULL,
            content TEXT NOT NULL,
            ts TEXT NOT NULL,
            user_id TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_messages_tenant_dataset_ts ON messages (tenant_id, dataset_id, ts DESC)",
        """
        CREATE TABLE IF NOT EXISTS oplog (
            tenant_id TEXT NOT NULL,
            dataset_id TEXT NOT NULL,
            op_id TEXT PRIMARY KEY,
            op_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            hlc_ts TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_oplog_tenant_dataset_ts ON oplog (tenant_id, dataset_id, hlc_ts DESC)",
        """
        CREATE TABLE IF NOT EXISTS engine_config (
            tenant_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (tenant_id, key)
        )
        """,
    )
    for statement in statements:
        execute_query(conn, statement)


@contextmanager
def connect_postgres():
    dsn = _build_postgres_dsn()
    if dsn.startswith("sqlite://"):
        import sqlite3

        sqlite_path = dsn.replace("sqlite://", "", 1)
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            ensure_postgres_schema(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    try:
        import psycopg
    except Exception as exc:  # pragma: no cover - depends on environment extras
        raise PostgresConfigurationError(
            "psycopg is required for postgres database_mode. Install optional dependency: psycopg[binary]."
        ) from exc

    conn = psycopg.connect(dsn)
    try:
        ensure_postgres_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
