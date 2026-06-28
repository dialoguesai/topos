"""Attribution-based row purge and tier-B raw/flat removal for source scrub."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Union

from ..config.settings import settings
from ..storage.db.postgres import execute_query, fetch_all, fetch_one

INSTALL_TABLE = "source_runtime_installs"
_VEC_BATCH_SIZE = 500
_VEC_TABLE = "signal_embeddings_vec"
_KNOWN_FLAT_TABLES_BY_SOURCE: Dict[str, List[str]] = {
    "browser_visits": ["browser_visits"],
    "browser_events": ["browser_events"],
    "starred_websites": ["starred_websites"],
}


@dataclass(frozen=True)
class TableAction:
    table: str
    action: str  # rows_deleted | table_dropped | vec_rows_deleted
    count: int


@dataclass
class AttributionScrubResult:
    tables: List[TableAction] = field(default_factory=list)
    rows_deleted: int = 0
    tables_dropped: List[str] = field(default_factory=list)

    def to_legacy_summary(self) -> Dict[str, Any]:
        return {
            "tables_dropped": sorted(set(self.tables_dropped)),
            "rows_deleted": int(self.rows_deleted),
            "table_actions": [
                {"table": item.table, "action": item.action, "count": int(item.count)}
                for item in self.tables
            ],
        }


def _safe_sql_identifier(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")))


def _extract_row_value(row: Any, index: int = 0, key: Optional[str] = None) -> Any:
    if isinstance(row, dict):
        if key is not None and key in row:
            return row[key]
        values = list(row.values())
        return values[index] if index < len(values) else None
    try:
        if key is not None:
            return row[key]
    except Exception:
        pass
    try:
        return row[index]
    except Exception:
        return None


def _list_user_tables(conn: Any) -> List[str]:
    if settings.topos_database_mode == "postgres":
        rows = fetch_all(
            conn,
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """,
        )
        return [str(_extract_row_value(row, 0, "table_name") or "").strip() for row in rows]

    rows = fetch_all(
        conn,
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """,
    )
    return [str(_extract_row_value(row, 0, "name") or "").strip() for row in rows]


def _list_table_columns(conn: Any, table_name: str) -> List[str]:
    if not _safe_sql_identifier(table_name):
        return []
    if settings.topos_database_mode == "postgres":
        rows = fetch_all(
            conn,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table_name,),
        )
        return [str(_extract_row_value(row, 0, "column_name") or "").strip() for row in rows]

    rows = fetch_all(conn, f'PRAGMA table_info("{table_name}")')
    columns: List[str] = []
    for row in rows:
        col_name = _extract_row_value(row, 1)
        if col_name is not None:
            columns.append(str(col_name))
    return columns


def _drop_table(conn: Any, table_name: str) -> None:
    if not _safe_sql_identifier(table_name):
        return
    if settings.topos_database_mode == "postgres":
        execute_query(conn, f'DROP TABLE IF EXISTS "{table_name}" CASCADE')
        return
    execute_query(conn, f'DROP TABLE IF EXISTS "{table_name}"')


def _count_table_rows_for_source(conn: Any, table_name: str, source_column: str, source_id: str) -> int:
    if not (_safe_sql_identifier(table_name) and _safe_sql_identifier(source_column)):
        return 0
    row = fetch_one(conn, f'SELECT COUNT(*) AS count FROM "{table_name}" WHERE "{source_column}" = %s', (source_id,))
    count_val = _extract_row_value(row, 0, "count") if row is not None else 0
    try:
        return int(count_val or 0)
    except Exception:
        return 0


def _count_all_rows(conn: Any, table_name: str) -> int:
    if not _safe_sql_identifier(table_name):
        return 0
    row = fetch_one(conn, f'SELECT COUNT(*) AS count FROM "{table_name}"')
    count_val = _extract_row_value(row, 0, "count") if row is not None else 0
    try:
        return int(count_val or 0)
    except Exception:
        return 0


def _table_exists(conn: Any, table_name: str) -> bool:
    return table_name in set(_list_user_tables(conn))


def _select_embedding_ids(conn: Any, table_name: str, source_column: str, source_id: str) -> List[str]:
    if table_name != "signal_embeddings" or not _safe_sql_identifier(source_column):
        return []
    columns = set(_list_table_columns(conn, table_name))
    if "embedding_id" not in columns or source_column not in columns:
        return []
    rows = fetch_all(
        conn,
        f'SELECT embedding_id FROM "{table_name}" WHERE "{source_column}" = %s',
        (source_id,),
    )
    ids: List[str] = []
    for row in rows:
        embedding_id = _extract_row_value(row, 0, "embedding_id")
        if embedding_id:
            ids.append(str(embedding_id))
    return ids


def _delete_vec_rows_batched(conn: Any, embedding_ids: Sequence[str]) -> int:
    if not embedding_ids or not isinstance(conn, sqlite3.Connection):
        return 0
    from ..storage.adapters.sqlite.vector_search import delete_vec_rows

    unique_ids = list(dict.fromkeys(str(item) for item in embedding_ids if str(item).strip()))
    if not unique_ids:
        return 0
    deleted = 0
    for start in range(0, len(unique_ids), _VEC_BATCH_SIZE):
        batch = unique_ids[start : start + _VEC_BATCH_SIZE]
        delete_vec_rows(conn, batch)
        deleted += len(batch)
    return deleted


def _flat_table_ids_from_source_def(source_def: Union[Dict[str, Any], Any]) -> List[str]:
    tables = source_def.get("tables") if isinstance(source_def, dict) else getattr(source_def, "tables", None)
    if not isinstance(tables, list):
        return []
    table_ids: List[str] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        table_id = str(table.get("table_id") or "").strip()
        if table_id and _safe_sql_identifier(table_id):
            table_ids.append(table_id)
    return table_ids


def _raw_table_candidates(source_id: str, source_def: Union[Dict[str, Any], Any]) -> Set[str]:
    source_type = (
        str(source_def.get("source_type") or "").strip()
        if isinstance(source_def, dict)
        else str(getattr(source_def, "source_type", "") or "").strip()
    )
    candidates: Set[str] = set()
    sid = str(source_id or "").strip()
    if not sid:
        return candidates

    # Heuristic names used when RawTablesManager is unavailable (postgres) or as fallback.
    normalized = re.sub(r"[^a-z0-9]+", "", sid.lower())
    candidates.add(f"raw_chat_messages_{normalized}")
    candidates.add(f"raw_{normalized}_events")
    candidates.add(f"raw_{normalized}_chat_messages")

    if source_type in {"ui_stream", "file"}:
        candidates.add(f"raw_chat_messages_{normalized}")
    if source_type in {"events", "browser"}:
        candidates.add(f"raw_{normalized}_events")

    return candidates


def _delete_attributed_rows(
    conn: Any,
    *,
    table_name: str,
    source_column: str,
    source_id: str,
) -> int:
    match_count = _count_table_rows_for_source(conn, table_name, source_column, source_id)
    if match_count <= 0:
        return 0
    execute_query(conn, f'DELETE FROM "{table_name}" WHERE "{source_column}" = %s', (source_id,))
    return match_count


def scrub_attributed_rows(conn: Any, source_id: str) -> AttributionScrubResult:
    """Delete all rows attributed to source_id / source_system across user tables."""
    sid = str(source_id or "").strip()
    result = AttributionScrubResult()
    if not sid:
        return result

    rows_deleted = 0
    tables_dropped: List[str] = []
    normalized_sid = re.sub(r"[^a-z0-9]+", "", sid.lower())
    table_names = [name for name in _list_user_tables(conn) if name and _safe_sql_identifier(name)]
    embedding_ids: List[str] = []

    for table_name in table_names:
        if table_name in {INSTALL_TABLE, "sqlite_sequence"}:
            continue
        columns = set(_list_table_columns(conn, table_name))
        deleted_from_table = False
        for source_column in ("source_id", "source_system"):
            if source_column not in columns:
                continue
            match_count = _count_table_rows_for_source(conn, table_name, source_column, sid)
            if match_count <= 0:
                continue
            if table_name == "signal_embeddings":
                embedding_ids.extend(_select_embedding_ids(conn, table_name, source_column, sid))
            execute_query(conn, f'DELETE FROM "{table_name}" WHERE "{source_column}" = %s', (sid,))
            rows_deleted += match_count
            deleted_from_table = True
            result.tables.append(
                TableAction(table=table_name, action="rows_deleted", count=match_count)
            )

        if deleted_from_table and table_name.startswith("raw_") and _count_all_rows(conn, table_name) == 0:
            _drop_table(conn, table_name)
            tables_dropped.append(table_name)
            result.tables.append(TableAction(table=table_name, action="table_dropped", count=0))
            continue

        normalized_table = re.sub(r"[^a-z0-9]+", "", table_name.lower())
        if table_name.startswith("raw_") and normalized_sid and normalized_sid in normalized_table:
            _drop_table(conn, table_name)
            tables_dropped.append(table_name)
            result.tables.append(TableAction(table=table_name, action="table_dropped", count=0))

    vec_deleted = _delete_vec_rows_batched(conn, embedding_ids)
    if vec_deleted > 0:
        result.tables.append(
            TableAction(table=_VEC_TABLE, action="vec_rows_deleted", count=vec_deleted)
        )

    result.rows_deleted = rows_deleted
    result.tables_dropped = sorted(set(tables_dropped))
    return result


def plan_attributed_rows(conn: Any, source_id: str) -> AttributionScrubResult:
    """Count attributable rows without deleting (dry run)."""
    sid = str(source_id or "").strip()
    result = AttributionScrubResult()
    if not sid:
        return result

    rows_deleted = 0
    table_names = [name for name in _list_user_tables(conn) if name and _safe_sql_identifier(name)]
    embedding_ids: List[str] = []

    for table_name in table_names:
        if table_name in {INSTALL_TABLE, "sqlite_sequence"}:
            continue
        columns = set(_list_table_columns(conn, table_name))
        for source_column in ("source_id", "source_system"):
            if source_column not in columns:
                continue
            match_count = _count_table_rows_for_source(conn, table_name, source_column, sid)
            if match_count <= 0:
                continue
            if table_name == "signal_embeddings":
                embedding_ids.extend(_select_embedding_ids(conn, table_name, source_column, sid))
            rows_deleted += match_count
            result.tables.append(
                TableAction(table=table_name, action="rows_deleted", count=match_count)
            )

    vec_count = len(dict.fromkeys(str(item) for item in embedding_ids if str(item).strip()))
    if vec_count > 0:
        result.tables.append(
            TableAction(table=_VEC_TABLE, action="vec_rows_deleted", count=vec_count)
        )

    result.rows_deleted = rows_deleted
    return result


def remove_raw_and_flat_tables(
    conn: Any,
    source_def: Union[Dict[str, Any], Any],
    source_id: str,
) -> List[TableAction]:
    """Tier B: remove raw retention and source flat tables without canonical attribution purge."""
    sid = str(source_id or "").strip()
    if not sid:
        return []

    actions: List[TableAction] = []
    raw_tables: Set[str] = set(_raw_table_candidates(sid, source_def))

    if isinstance(conn, sqlite3.Connection):
        from ..storage.raw.raw_tables_manager import RawTablesManager

        manager = RawTablesManager(conn)
        raw_tables.add(manager.get_raw_table_name(sid, "chat_messages"))
        raw_tables.add(manager.get_raw_table_name(sid, "events"))

    flat_tables = set(_flat_table_ids_from_source_def(source_def))
    flat_tables.update(_KNOWN_FLAT_TABLES_BY_SOURCE.get(sid, []))

    for table_name in sorted(raw_tables | flat_tables):
        if not table_name or not _safe_sql_identifier(table_name) or not _table_exists(conn, table_name):
            continue
        columns = set(_list_table_columns(conn, table_name))
        deleted = 0
        if "source_system" in columns:
            deleted += _delete_attributed_rows(conn, table_name=table_name, source_column="source_system", source_id=sid)
        if "source_id" in columns:
            deleted += _delete_attributed_rows(conn, table_name=table_name, source_column="source_id", source_id=sid)
        if deleted == 0:
            row_count = _count_all_rows(conn, table_name)
            if row_count > 0:
                execute_query(conn, f'DELETE FROM "{table_name}"')
                deleted = row_count
        if deleted > 0:
            actions.append(TableAction(table=table_name, action="rows_deleted", count=deleted))
        if table_name.startswith("raw_") and _count_all_rows(conn, table_name) == 0:
            _drop_table(conn, table_name)
            actions.append(TableAction(table=table_name, action="table_dropped", count=0))

    return actions


def plan_remove_raw_and_flat_tables(
    conn: Any,
    source_def: Union[Dict[str, Any], Any],
    source_id: str,
) -> List[TableAction]:
    """Count raw/flat rows that tier-B removal would delete (dry run)."""
    sid = str(source_id or "").strip()
    if not sid:
        return []

    actions: List[TableAction] = []
    raw_tables: Set[str] = set(_raw_table_candidates(sid, source_def))

    if isinstance(conn, sqlite3.Connection):
        from ..storage.raw.raw_tables_manager import RawTablesManager

        manager = RawTablesManager(conn)
        raw_tables.add(manager.get_raw_table_name(sid, "chat_messages"))
        raw_tables.add(manager.get_raw_table_name(sid, "events"))

    flat_tables = set(_flat_table_ids_from_source_def(source_def))
    flat_tables.update(_KNOWN_FLAT_TABLES_BY_SOURCE.get(sid, []))

    for table_name in sorted(raw_tables | flat_tables):
        if not table_name or not _safe_sql_identifier(table_name) or not _table_exists(conn, table_name):
            continue
        columns = set(_list_table_columns(conn, table_name))
        deleted = 0
        if "source_system" in columns:
            deleted += _count_table_rows_for_source(conn, table_name, "source_system", sid)
        if "source_id" in columns:
            deleted += _count_table_rows_for_source(conn, table_name, "source_id", sid)
        if deleted == 0:
            deleted = _count_all_rows(conn, table_name)
        if deleted > 0:
            actions.append(TableAction(table=table_name, action="rows_deleted", count=deleted))
    return actions
