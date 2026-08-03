"""Pipeline-aware row delete for Data Explorer."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .data_explorer_tables import is_canonical_schema_table
from .sources.scrub_attribution import (
    TableAction,
    _delete_vec_rows_batched,
    _extract_row_value,
    _list_table_columns,
    _list_user_tables,
    _safe_sql_identifier,
    _select_embedding_ids,
)
from .storage.adapters.sqlite.stores import _NATIVE_ID_COL
from .storage.db.postgres import execute_query, fetch_all, fetch_one

VALID_DELETE_SCOPES = frozenset({"row_only", "with_downstream", "with_upstream", "full_lineage"})

_PROTECTED_TABLES = frozenset(
    {
        "engine_config",
        "oplog",
        "projection_meta",
        "schema_meta",
        "sqlite_sequence",
        "ingestion_checkpoints",
        "ingestion_jobs",
        "ingestion_errors",
        "source_runtime_installs",
    }
)

_RECORD_LINK_COLUMNS = ("record_id", "message_id", "entry_id", "event_id", "transaction_id")
_PRIMARY_KEY_FALLBACKS = ("record_id", "id", "entry_id", "message_id", "event_id", "transaction_id")

_ENRICHMENT_SIGNAL_TABLES = frozenset(
    {
        "message_emotions",
        "message_embeddings",
        "message_entities",
        "message_topics",
        "message_sentiment",
        "user_goals",
        "signal_tags",
        "signal_scores",
        "signal_embeddings",
        "relationship_edges",
        "graph_nodes",
        "graph_edges",
        "data_health_dimension",
    }
)


@dataclass(frozen=True)
class LineageAnchor:
    anchor_table: str
    anchor_row_id: str
    canonical_table: Optional[str]
    canonical_id: Optional[str]
    source_id: Optional[str]
    source_record_id: Optional[str]


@dataclass
class RowDeleteResult:
    scope: str
    table_name: str
    row_ids: List[str]
    rows_deleted: int = 0
    table_actions: List[TableAction] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "table_name": self.table_name,
            "row_ids": list(self.row_ids),
            "rows_deleted": int(self.rows_deleted),
            "table_actions": [
                {"table": item.table, "action": item.action, "count": int(item.count)}
                for item in self.table_actions
            ],
        }


def _is_enrichment_or_signal_table(table_name: str) -> bool:
    if table_name in _ENRICHMENT_SIGNAL_TABLES:
        return True
    if table_name.startswith("signal_") or table_name.startswith("message_"):
        return True
    return False


def _is_upstream_table(table_name: str, columns: Set[str]) -> bool:
    if is_canonical_schema_table(table_name) or _is_enrichment_or_signal_table(table_name):
        return False
    if table_name.startswith("raw_"):
        return True
    if "source_system" in columns:
        return True
    if "record_id" in columns and ("source_id" in columns or "source_system" in columns):
        return True
    return False


def resolve_primary_key_column(conn: Any, table_name: str) -> Optional[str]:
    if not _safe_sql_identifier(table_name):
        return None
    known = _NATIVE_ID_COL.get(table_name)
    if known:
        return known
    columns = set(_list_table_columns(conn, table_name))
    for candidate in _PRIMARY_KEY_FALLBACKS:
        if candidate in columns:
            return candidate
    return None


def _row_to_dict(row: Any, columns: Sequence[str]) -> Dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {col: _extract_row_value(row, idx, col) for idx, col in enumerate(columns)}


def _fetch_row(conn: Any, table_name: str, pk_column: str, row_id: str) -> Optional[Dict[str, Any]]:
    columns = _list_table_columns(conn, table_name)
    if pk_column not in columns:
        return None
    row = fetch_one(
        conn,
        f'SELECT * FROM "{table_name}" WHERE "{pk_column}" = %s LIMIT 1',
        (row_id,),
    )
    if row is None:
        return None
    return _row_to_dict(row, columns)


def _lookup_mapping(
    conn: Any,
    *,
    source_id: str,
    source_record_id: str,
) -> Optional[Tuple[str, str]]:
    if not _table_exists(conn, "canonical_source_mappings"):
        return None
    row = fetch_one(
        conn,
        """
        SELECT canonical_table, canonical_id
        FROM canonical_source_mappings
        WHERE source_id = %s AND source_record_id = %s
        LIMIT 1
        """,
        (source_id, source_record_id),
    )
    if row is None:
        return None
    canonical_table = str(_extract_row_value(row, 0, "canonical_table") or "").strip()
    canonical_id = str(_extract_row_value(row, 1, "canonical_id") or "").strip()
    if not canonical_table or not canonical_id:
        return None
    return canonical_table, canonical_id


def _lookup_mapping_by_canonical(
    conn: Any,
    *,
    canonical_table: str,
    canonical_id: str,
) -> Optional[Tuple[str, str]]:
    if not _table_exists(conn, "canonical_source_mappings"):
        return None
    row = fetch_one(
        conn,
        """
        SELECT source_id, source_record_id
        FROM canonical_source_mappings
        WHERE canonical_table = %s AND canonical_id = %s
        LIMIT 1
        """,
        (canonical_table, canonical_id),
    )
    if row is None:
        return None
    source_id = str(_extract_row_value(row, 0, "source_id") or "").strip()
    source_record_id = str(_extract_row_value(row, 1, "source_record_id") or "").strip()
    if not source_id or not source_record_id:
        return None
    return source_id, source_record_id


def _table_exists(conn: Any, table_name: str) -> bool:
    return table_name in set(_list_user_tables(conn))


def _resolve_lineage(
    conn: Any,
    *,
    table_name: str,
    pk_column: str,
    row_id: str,
    row: Dict[str, Any],
) -> LineageAnchor:
    source_id = str(row.get("source_id") or row.get("source_system") or "").strip() or None
    source_record_id = str(row.get("source_record_id") or row.get("record_id") or "").strip() or None
    canonical_table: Optional[str] = None
    canonical_id: Optional[str] = None

    if is_canonical_schema_table(table_name):
        canonical_table = table_name
        canonical_id = row_id
        if not source_record_id:
            source_record_id = row_id
        if not source_id:
            mapped = _lookup_mapping_by_canonical(
                conn,
                canonical_table=table_name,
                canonical_id=row_id,
            )
            if mapped:
                source_id, mapped_source_record_id = mapped
                source_record_id = source_record_id or mapped_source_record_id
    elif source_id and source_record_id:
        mapped = _lookup_mapping(source_id=source_id, source_record_id=source_record_id, conn=conn)
        if mapped:
            canonical_table, canonical_id = mapped
        else:
            for candidate_table, id_col in _NATIVE_ID_COL.items():
                if not _table_exists(conn, candidate_table):
                    continue
                exists = fetch_one(
                    conn,
                    f'SELECT 1 FROM "{candidate_table}" WHERE "{id_col}" = %s AND source_id = %s LIMIT 1',
                    (source_record_id, source_id),
                )
                if exists is not None:
                    canonical_table = candidate_table
                    canonical_id = source_record_id
                    break
    else:
        link_id = str(
            row.get("record_id")
            or row.get("message_id")
            or row.get("entry_id")
            or row.get("event_id")
            or row_id
        ).strip()
        if link_id:
            canonical_id = link_id
            for candidate_table, id_col in _NATIVE_ID_COL.items():
                if not _table_exists(conn, candidate_table):
                    continue
                exists = fetch_one(
                    conn,
                    f'SELECT 1 FROM "{candidate_table}" WHERE "{id_col}" = %s LIMIT 1',
                    (link_id,),
                )
                if exists is not None:
                    canonical_table = candidate_table
                    break

    if canonical_table and canonical_id and (not source_id or not source_record_id):
        mapped = _lookup_mapping_by_canonical(
            conn,
            canonical_table=canonical_table,
            canonical_id=canonical_id,
        )
        if mapped:
            source_id = source_id or mapped[0]
            source_record_id = source_record_id or mapped[1]

    return LineageAnchor(
        anchor_table=table_name,
        anchor_row_id=row_id,
        canonical_table=canonical_table,
        canonical_id=canonical_id,
        source_id=source_id,
        source_record_id=source_record_id,
    )


def _count_rows(conn: Any, table_name: str, where_sql: str, params: Sequence[Any]) -> int:
    row = fetch_one(conn, f'SELECT COUNT(*) AS count FROM "{table_name}" WHERE {where_sql}', tuple(params))
    try:
        return int(_extract_row_value(row, 0, "count") or 0)
    except Exception:
        return 0


def _delete_where(
    conn: Any,
    *,
    table_name: str,
    where_sql: str,
    params: Sequence[Any],
    embedding_ids: List[str],
) -> int:
    if table_name == "signal_embeddings":
        columns = set(_list_table_columns(conn, table_name))
        source_column = next((col for col in ("record_id", "message_id", "source_id") if col in columns), None)
        if source_column and len(params) == 1:
            embedding_ids.extend(
                _select_embedding_ids(conn, table_name, source_column, str(params[0]))
            )
        elif "record_id" in columns and len(params) == 1:
            embedding_ids.extend(
                _select_embedding_ids(conn, table_name, "record_id", str(params[0]))
            )
    deleted = _count_rows(conn, table_name, where_sql, params)
    if deleted <= 0:
        return 0
    execute_query(conn, f'DELETE FROM "{table_name}" WHERE {where_sql}', tuple(params))
    return deleted


def _delete_anchor_row(
    conn: Any,
    *,
    table_name: str,
    pk_column: str,
    row_id: str,
    actions: List[TableAction],
    embedding_ids: List[str],
) -> int:
    deleted = _delete_where(
        conn,
        table_name=table_name,
        where_sql=f'"{pk_column}" = %s',
        params=(row_id,),
        embedding_ids=embedding_ids,
    )
    if deleted > 0:
        actions.append(TableAction(table=table_name, action="rows_deleted", count=deleted))
    return deleted


def _delete_downstream_for_canonical(
    conn: Any,
    *,
    canonical_id: str,
    skip_tables: Set[str],
    actions: List[TableAction],
    embedding_ids: List[str],
) -> int:
    deleted_total = 0
    for table_name in _list_user_tables(conn):
        if (
            not _safe_sql_identifier(table_name)
            or table_name in _PROTECTED_TABLES
            or table_name in skip_tables
            or not _is_enrichment_or_signal_table(table_name)
        ):
            continue
        columns = set(_list_table_columns(conn, table_name))
        for link_col in _RECORD_LINK_COLUMNS:
            if link_col not in columns:
                continue
            deleted = _delete_where(
                conn,
                table_name=table_name,
                where_sql=f'"{link_col}" = %s',
                params=(canonical_id,),
                embedding_ids=embedding_ids,
            )
            if deleted > 0:
                deleted_total += deleted
                actions.append(TableAction(table=table_name, action="rows_deleted", count=deleted))
            break
    return deleted_total


def _delete_canonical_row(
    conn: Any,
    *,
    canonical_table: str,
    canonical_id: str,
    actions: List[TableAction],
    embedding_ids: List[str],
) -> int:
    if not is_canonical_schema_table(canonical_table):
        return 0
    pk_column = resolve_primary_key_column(conn, canonical_table) or _NATIVE_ID_COL.get(canonical_table)
    if not pk_column:
        return 0
    return _delete_anchor_row(
        conn,
        table_name=canonical_table,
        pk_column=pk_column,
        row_id=canonical_id,
        actions=actions,
        embedding_ids=embedding_ids,
    )


def _delete_upstream_rows(
    conn: Any,
    *,
    source_id: str,
    source_record_id: str,
    skip_tables: Set[str],
    actions: List[TableAction],
) -> int:
    deleted_total = 0
    for table_name in _list_user_tables(conn):
        columns = set(_list_table_columns(conn, table_name))
        if (
            not _safe_sql_identifier(table_name)
            or table_name in _PROTECTED_TABLES
            or table_name in skip_tables
            or not _is_upstream_table(table_name, columns)
        ):
            continue
        predicates: List[str] = []
        params: List[Any] = []
        if "record_id" in columns:
            predicates.append('"record_id" = %s')
            params.append(source_record_id)
        elif "source_record_id" in columns:
            predicates.append('"source_record_id" = %s')
            params.append(source_record_id)
        else:
            continue
        source_predicates: List[str] = []
        if "source_id" in columns:
            source_predicates.append('"source_id" = %s')
            params.append(source_id)
        if "source_system" in columns:
            source_predicates.append('"source_system" = %s')
            params.append(source_id)
        if source_predicates:
            where_sql = f"({' OR '.join(predicates)}) AND ({' OR '.join(source_predicates)})"
        else:
            where_sql = " OR ".join(predicates)
        deleted = _count_rows(conn, table_name, where_sql, params)
        if deleted <= 0:
            continue
        execute_query(conn, f'DELETE FROM "{table_name}" WHERE {where_sql}', tuple(params))
        deleted_total += deleted
        actions.append(TableAction(table=table_name, action="rows_deleted", count=deleted))
    return deleted_total


def _delete_mapping_rows(
    conn: Any,
    *,
    source_id: Optional[str],
    source_record_id: Optional[str],
    canonical_table: Optional[str],
    canonical_id: Optional[str],
    actions: List[TableAction],
) -> int:
    if not _table_exists(conn, "canonical_source_mappings"):
        return 0
    deleted = 0
    if source_id and source_record_id:
        count = _count_rows(
            conn,
            "canonical_source_mappings",
            "source_id = %s AND source_record_id = %s",
            (source_id, source_record_id),
        )
        if count > 0:
            execute_query(
                conn,
                "DELETE FROM canonical_source_mappings WHERE source_id = %s AND source_record_id = %s",
                (source_id, source_record_id),
            )
            deleted += count
    if canonical_table and canonical_id:
        count = _count_rows(
            conn,
            "canonical_source_mappings",
            "canonical_table = %s AND canonical_id = %s",
            (canonical_table, canonical_id),
        )
        if count > 0:
            execute_query(
                conn,
                "DELETE FROM canonical_source_mappings WHERE canonical_table = %s AND canonical_id = %s",
                (canonical_table, canonical_id),
            )
            deleted += count
    if deleted > 0:
        actions.append(
            TableAction(table="canonical_source_mappings", action="rows_deleted", count=deleted)
        )
    return deleted


def delete_database_rows(
    conn: Any,
    *,
    table_name: str,
    row_ids: Sequence[str],
    scope: str,
) -> RowDeleteResult:
    normalized_scope = str(scope or "row_only").strip().lower()
    if normalized_scope not in VALID_DELETE_SCOPES:
        raise ValueError(f"Invalid scope: {scope!r}")

    table = str(table_name or "").strip()
    if not table or not _safe_sql_identifier(table):
        raise ValueError("table_name required")
    if table in _PROTECTED_TABLES:
        raise ValueError(f"Table is protected from deletion: {table}")
    if not _table_exists(conn, table):
        raise ValueError(f"Table or view not found: {table}")

    pk_column = resolve_primary_key_column(conn, table)
    if not pk_column:
        raise ValueError(f"Could not resolve primary key column for table: {table}")

    unique_row_ids = [str(item).strip() for item in row_ids if str(item).strip()]
    if not unique_row_ids:
        raise ValueError("row_ids required")

    result = RowDeleteResult(scope=normalized_scope, table_name=table, row_ids=unique_row_ids)
    actions: List[TableAction] = []
    embedding_ids: List[str] = []
    include_downstream = normalized_scope in {"with_downstream", "full_lineage"}
    include_upstream = normalized_scope in {"with_upstream", "full_lineage"}

    for row_id in unique_row_ids:
        row = _fetch_row(conn, table, pk_column, row_id)
        if row is None:
            raise ValueError(f"Row not found: {row_id}")
        lineage = _resolve_lineage(conn, table_name=table, pk_column=pk_column, row_id=row_id, row=row)
        skip_tables: Set[str] = {table}

        if include_downstream and lineage.canonical_id:
            if lineage.anchor_table != lineage.canonical_table:
                _delete_canonical_row(
                    conn,
                    canonical_table=str(lineage.canonical_table),
                    canonical_id=str(lineage.canonical_id),
                    actions=actions,
                    embedding_ids=embedding_ids,
                )
                if lineage.canonical_table:
                    skip_tables.add(str(lineage.canonical_table))
            _delete_downstream_for_canonical(
                conn,
                canonical_id=str(lineage.canonical_id),
                skip_tables=skip_tables,
                actions=actions,
                embedding_ids=embedding_ids,
            )

        if include_upstream:
            if lineage.source_id and lineage.source_record_id:
                _delete_upstream_rows(
                    conn,
                    source_id=str(lineage.source_id),
                    source_record_id=str(lineage.source_record_id),
                    skip_tables=skip_tables,
                    actions=actions,
                )
            if (
                lineage.canonical_table
                and lineage.canonical_id
                and lineage.anchor_table != lineage.canonical_table
            ):
                _delete_canonical_row(
                    conn,
                    canonical_table=str(lineage.canonical_table),
                    canonical_id=str(lineage.canonical_id),
                    actions=actions,
                    embedding_ids=embedding_ids,
                )
            _delete_mapping_rows(
                conn,
                source_id=lineage.source_id,
                source_record_id=lineage.source_record_id,
                canonical_table=lineage.canonical_table,
                canonical_id=lineage.canonical_id,
                actions=actions,
            )

        result.rows_deleted += _delete_anchor_row(
            conn,
            table_name=table,
            pk_column=pk_column,
            row_id=row_id,
            actions=actions,
            embedding_ids=embedding_ids,
        )

    vec_deleted = _delete_vec_rows_batched(conn, embedding_ids)
    if vec_deleted > 0:
        actions.append(TableAction(table="vector_index", action="vec_rows_deleted", count=vec_deleted))

    if isinstance(conn, sqlite3.Connection):
        conn.commit()

    result.table_actions = actions
    return result
