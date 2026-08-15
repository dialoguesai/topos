"""Database explorer and JSONL file message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    List,
    Optional,
    _is_sqlite_conn,
    _resource_owner_for_mcp_log,
    _table_row_order_clause,
    base64,
    connect_postgres,
    datetime,
    get_user_id,
    hashlib,
    json,
    layer_for_category,
    layer_kind_labels,
    logger,
    record_mcp_request,
    settings,
    time_module,
    timezone,
    uuid,
)
from .registry import handles
from ...storage.db.write_gate import batched_writes, commit_connection, with_db_write


def _safe_sql_identifier(name: str) -> bool:
    if not name:
        return False
    return all(ch.isalnum() or ch == "_" for ch in name)

def _device_id_for_topos_key(topos_key: Optional[str]) -> Optional[str]:
    import hashlib

    key = (topos_key or "").strip()
    if not key:
        return None
    return hashlib.sha256(key.encode()).hexdigest()[:16].lower()

def _primary_dataset_id_for_engine_context(user_id: Optional[str]) -> Optional[str]:
    """Match control-plane default dataset id shape when TOPOS_KEY is configured."""
    uid = (user_id or "").strip()
    if not uid:
        return None
    default_name = (getattr(settings, "topos_default_dataset_id", None) or "default").strip() or "default"
    device_id = _device_id_for_topos_key(getattr(settings, "topos_key", None))
    if device_id:
        return f"{uid}:{default_name}:{device_id}"
    return f"{uid}:{default_name}"

def _pooled_table_scope_for_columns(
    col_names: set[str],
    dataset_id: Optional[str],
    owner_user_id: Optional[str],
    tenant_id: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolve the strongest scope field/value available for pooled generic table reads.
    Preference order: dataset_id, owner_user_id, tenant_id.
    """
    if dataset_id and "dataset_id" in col_names:
        return "dataset_id", dataset_id, "dataset_id"
    if owner_user_id and "owner_user_id" in col_names:
        return "owner_user_id", owner_user_id, "owner_user_id"
    if tenant_id and "tenant_id" in col_names:
        return "tenant_id", tenant_id, "tenant_id"
    return None, None, None

def _pooled_read_enforcement_enabled() -> bool:
    return str(getattr(settings, "engine_pool_mode", "off") or "").strip().lower() == "pooled"

POOLED_DECLARED_ENDPOINT_POLICY: Dict[str, Dict[str, Any]] = {
    "list_database_tables": {
        "enforcement_state": "enforced",
        "supports_cap_metadata": False,
    },
    "get_table_count": {
        "enforcement_state": "enforced",
        "supports_cap_metadata": False,
    },
    "get_table_rows": {
        "enforcement_state": "enforced",
        "supports_cap_metadata": True,
    },
    "delete_database_table": {
        "enforcement_state": "blocked_until_hardened",
        "supports_cap_metadata": False,
    },
}

def _pooled_endpoint_policy_for_message(msg_type: str) -> Dict[str, Any]:
    policy = POOLED_DECLARED_ENDPOINT_POLICY.get(msg_type, {})
    return {
        "endpoint": msg_type,
        "enforcement_state": policy.get("enforcement_state", "unknown"),
        "supports_cap_metadata": bool(policy.get("supports_cap_metadata", False)),
    }

_POOLED_SCOPE_COLUMNS = ("dataset_id", "owner_user_id", "tenant_id")

def _resolve_table_row_order_clause(
    col_names: set[str],
    *,
    table_name: str = "",
    is_sqlite: bool = True,
    order_by: Optional[str] = None,
    order_dir: Optional[str] = None,
) -> str:
    """Apply explicit client sort when valid; otherwise use default table ordering."""
    column = (order_by or "").strip()
    direction = (order_dir or "asc").strip().lower()
    if direction not in ("asc", "desc"):
        direction = "asc"
    if column and column in col_names and _safe_sql_identifier(column):
        return f'"{column}" {direction.upper()}'
    return _table_row_order_clause(col_names, table_name=table_name, is_sqlite=is_sqlite)

def _pooled_scope_tables(conn: Any, requested_tables: Optional[List[str]] = None) -> List[str]:
    if requested_tables:
        return [t for t in requested_tables if _safe_sql_identifier(t)]
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
        AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    tables: List[str] = []
    for row in rows:
        name = row["name"] if isinstance(row, dict) else row[0]
        if _safe_sql_identifier(name):
            tables.append(name)
    return tables

def _pooled_scope_columns_for_table(conn: Any, table_name: str) -> List[str]:
    cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    col_names = {str(c["name"]) if isinstance(c, dict) else str(c[1]) for c in cols}
    return [c for c in _POOLED_SCOPE_COLUMNS if c in col_names]

def _pooled_scope_missing_predicate(scope_columns: List[str]) -> str:
    return " OR ".join([f'("{col}" IS NULL OR TRIM(CAST("{col}" AS TEXT)) = \'\')' for col in scope_columns])

def _pooled_scope_backfill_dry_run(
    conn: Any,
    requested_tables: Optional[List[str]] = None,
) -> Dict[str, Any]:
    table_summaries: List[Dict[str, Any]] = []
    checksums: List[str] = []
    for table_name in _pooled_scope_tables(conn, requested_tables):
        scope_columns = _pooled_scope_columns_for_table(conn, table_name)
        if not scope_columns:
            table_summaries.append(
                {
                    "table_name": table_name,
                    "scope_columns": [],
                    "missing_scope_rows": 0,
                    "status": "unscoped_table",
                }
            )
            continue
        missing_pred = _pooled_scope_missing_predicate(scope_columns)
        row = conn.execute(
            f'SELECT COUNT(*) AS missing_count, COALESCE(SUM(rowid), 0) AS rowid_sum FROM "{table_name}" WHERE {missing_pred}'
        ).fetchone()
        missing_count = int(row["missing_count"] if isinstance(row, dict) else row[0])
        rowid_sum = int(row["rowid_sum"] if isinstance(row, dict) else row[1])
        checksum_seed = f"{table_name}|{','.join(scope_columns)}|{missing_count}|{rowid_sum}"
        checksum = hashlib.sha256(checksum_seed.encode("utf-8")).hexdigest()
        checksums.append(f"{table_name}:{checksum}")
        table_summaries.append(
            {
                "table_name": table_name,
                "scope_columns": scope_columns,
                "missing_scope_rows": missing_count,
                "rowid_sum": rowid_sum,
                "checksum": checksum,
                "status": "needs_backfill" if missing_count > 0 else "already_scoped",
            }
        )
    overall_checksum = hashlib.sha256("|".join(checksums).encode("utf-8")).hexdigest() if checksums else ""
    return {
        "tables": table_summaries,
        "overall_checksum": overall_checksum,
        "table_count": len(table_summaries),
    }

def _ensure_pooled_scope_journal_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pooled_scope_backfill_journal (
            migration_id TEXT NOT NULL,
            table_name TEXT NOT NULL,
            backup_table TEXT NOT NULL,
            scope_columns_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

def _pooled_scope_backfill_apply(
    conn: Any,
    *,
    dataset_id: Optional[str],
    owner_user_id: Optional[str],
    tenant_id: Optional[str],
    requested_tables: Optional[List[str]] = None,
) -> Dict[str, Any]:
    migration_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    tables_applied: List[Dict[str, Any]] = []
    # Backup/UPDATE/journal writes take SQLite's write lock at execute time —
    # hold the gate for the whole migration batch (single commit at exit).
    # Callers guarantee a sqlite conn (_is_sqlite_conn checked in handlers).
    with batched_writes(conn):
        _ensure_pooled_scope_journal_table(conn)
        for table_name in _pooled_scope_tables(conn, requested_tables):
            scope_columns = _pooled_scope_columns_for_table(conn, table_name)
            if not scope_columns:
                continue
            missing_pred = _pooled_scope_missing_predicate(scope_columns)
            backup_table = f'pooled_scope_backup_{table_name}_{int(time_module.time() * 1000)}_{uuid.uuid4().hex[:8]}'
            scoped_column_select = ", ".join([f'"{c}"' for c in scope_columns])
            conn.execute(
                f'CREATE TABLE "{backup_table}" AS SELECT rowid AS _rowid, {scoped_column_select} FROM "{table_name}" WHERE {missing_pred}'
            )
            missing_row = conn.execute(f'SELECT COUNT(*) AS count FROM "{backup_table}"').fetchone()
            missing_count = int(missing_row["count"] if isinstance(missing_row, dict) else missing_row[0])
            if missing_count <= 0:
                conn.execute(f'DROP TABLE IF EXISTS "{backup_table}"')
                continue
            set_clauses: List[str] = []
            params: List[Any] = []
            replacements = {
                "dataset_id": dataset_id,
                "owner_user_id": owner_user_id,
                "tenant_id": tenant_id,
            }
            for scope_col in scope_columns:
                replacement = replacements.get(scope_col)
                if replacement is None:
                    continue
                set_clauses.append(
                    f'''"{scope_col}" = CASE WHEN "{scope_col}" IS NULL OR TRIM(CAST("{scope_col}" AS TEXT)) = '' THEN ? ELSE "{scope_col}" END'''
                )
                params.append(replacement)
            if not set_clauses:
                conn.execute(f'DROP TABLE IF EXISTS "{backup_table}"')
                continue
            params.extend([migration_id, table_name, backup_table, json.dumps(scope_columns), created_at])
            conn.execute(
                f'UPDATE "{table_name}" SET {", ".join(set_clauses)} WHERE rowid IN (SELECT _rowid FROM "{backup_table}")',
                tuple(params[: len(set_clauses)]),
            )
            conn.execute(
                """
                INSERT INTO pooled_scope_backfill_journal
                (migration_id, table_name, backup_table, scope_columns_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                tuple(params[len(set_clauses) :]),
            )
            tables_applied.append(
                {
                    "table_name": table_name,
                    "scope_columns": scope_columns,
                    "rows_backfilled": missing_count,
                    "backup_table": backup_table,
                }
            )
    return {
        "migration_id": migration_id,
        "created_at": created_at,
        "tables_applied": tables_applied,
    }

def _pooled_scope_backfill_rollback(conn: Any, migration_id: str) -> Dict[str, Any]:
    restored: List[Dict[str, Any]] = []
    # Restore/DROP/journal writes take SQLite's write lock at execute time —
    # hold the gate for the whole rollback batch (single commit at exit).
    # Callers guarantee a sqlite conn (_is_sqlite_conn checked in handlers).
    with batched_writes(conn):
        _ensure_pooled_scope_journal_table(conn)
        rows = conn.execute(
            """
            SELECT table_name, backup_table, scope_columns_json
            FROM pooled_scope_backfill_journal
            WHERE migration_id = ?
            ORDER BY table_name
            """,
            (migration_id,),
        ).fetchall()
        for row in rows:
            table_name = str(row["table_name"] if isinstance(row, dict) else row[0])
            backup_table = str(row["backup_table"] if isinstance(row, dict) else row[1])
            columns_json = row["scope_columns_json"] if isinstance(row, dict) else row[2]
            try:
                scope_columns = json.loads(columns_json) if isinstance(columns_json, str) else []
            except Exception:
                scope_columns = []
            if not scope_columns:
                continue
            set_clauses = []
            for scope_col in scope_columns:
                set_clauses.append(
                    f'''"{scope_col}" = (SELECT "{scope_col}" FROM "{backup_table}" b WHERE b._rowid = "{table_name}".rowid)'''
                )
            conn.execute(
                f'UPDATE "{table_name}" SET {", ".join(set_clauses)} WHERE rowid IN (SELECT _rowid FROM "{backup_table}")'
            )
            restored_count_row = conn.execute(f'SELECT COUNT(*) AS count FROM "{backup_table}"').fetchone()
            restored_count = int(
                restored_count_row["count"] if isinstance(restored_count_row, dict) else restored_count_row[0]
            )
            conn.execute(f'DROP TABLE IF EXISTS "{backup_table}"')
            restored.append(
                {
                    "table_name": table_name,
                    "backup_table": backup_table,
                    "rows_restored": restored_count,
                }
            )
        conn.execute("DELETE FROM pooled_scope_backfill_journal WHERE migration_id = ?", (migration_id,))
    return {
        "migration_id": migration_id,
        "tables_restored": restored,
    }

def _sqlite_query_plan(conn: Any, sql: str, params: tuple[Any, ...]) -> List[str]:
    try:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    except Exception:
        return []
    plan: List[str] = []
    for row in rows:
        if isinstance(row, dict):
            plan.append(str(row.get("detail") or row))
        else:
            plan.append(str(row[-1] if len(row) >= 4 else row))
    return plan

@handles("get_database_explorer_summary", owner_only=True)
async def handle_get_database_explorer_summary(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...api.database_explorer import _get_database_explorer_summary_core

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        result = await _get_database_explorer_summary_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:QUERY] get_database_explorer_summary error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("list_database_tables")
async def handle_list_database_tables(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    msg_type = str(message.get("type") or "").strip().lower()
    _payload = message.get("payload") or {}
    _mcp_source = _payload.get("mcp_source")
    _mcp_requester_id = _payload.get("mcp_requester_id")
    """List all tables in the database, grouped by architecture layer."""
    try:
        from ...llm_integrations_storage import DATA_EXPLORER_HIDDEN_TABLES, maybe_migrate_legacy_llm_config

        payload = message.get("payload") or {}
        pooled_mode = _pooled_read_enforcement_enabled()
        pooled_dataset_id = (payload.get("dataset_id") or "").strip() or None
        pooled_owner_user_id = (payload.get("owner_user_id") or "").strip() or None
        pooled_tenant_id = (payload.get("tenant_id") or "").strip() or None
        scope_requested = bool(pooled_dataset_id or pooled_owner_user_id or pooled_tenant_id)
        if pooled_mode and not scope_requested:
            return {
                "id": req_id,
                "status": "error",
                "error": "Pooled table visibility requires tenant context",
                "error_metadata": {
                    "policy_reason": "missing_tenant_context",
                    "mode": "pooled",
                },
            }
        use_postgres = settings.topos_database_mode == "postgres"
        conn = None
        if not use_postgres:
            conn = hub.get_db_connection()
            if not conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}
            maybe_migrate_legacy_llm_config(conn)
        
        # Define table categories based on architecture
        # Raw Retention Tables (original payloads before parsing)
        # Pattern: raw_chat_messages_{source}, raw_{source}_events, etc.
        # These store the original source payloads verbatim
        raw_retention_patterns = [
            "raw_chat_messages_",  # e.g., raw_chat_messages_chatgpt
            "raw_calendar_events",  # e.g., raw_calendar_events
        ]
        
        # Raw Enrichment Tables (per-connector, parser-assist enrichment)
        # These are enrichment results that operate on raw/source-normalized data
        # Examples: raw_attachments, raw_tool_calls, raw_language, raw_time_normalization
        # Note: These may have source suffixes (e.g., raw_attachments_chatgpt) or be shared
        raw_enrichment_tables = {
            "raw_attachments", "raw_tool_calls", "raw_language", "raw_time_normalization",
            "raw_attendees", "raw_locations",  # Calendar-specific raw enrichment
        }
        raw_enrichment_prefixes = [
            "raw_attachments_", "raw_tool_calls_", "raw_language_", "raw_time_normalization_",
            "raw_attendees_", "raw_locations_",
        ]
        
        # Source Tables (source-normalized)
        source_tables = {
            "messages", "ingestion_checkpoints", "ingestion_jobs", "ingestion_errors"
        }
        
        # Canonical Tables (shared canonical layer)
        from ...data_explorer_tables import CANONICAL_SCHEMA_TABLES

        canonical_tables = set(CANONICAL_SCHEMA_TABLES)
        
        # Enrichment System Tables
        enrichment_system_tables = {
            "enrichment_models", "enrichment_processing_state"
        }
        
        # Canonical Enrichment Tables (shared enrichment on canonical data)
        # These operate on canonical tables and are shared across all sources
        canonical_enrichment_tables = {
            "message_emotions", "message_text_classifications", "message_token_classifications",
            "message_llm_extractions", "message_topics", "message_sentiment", "message_embeddings",
            "message_entities",  # If implemented
        }
        # Also check for message_* prefix for any other canonical enrichment tables
        canonical_enrichment_prefix = "message_"
        
        # System Tables
        system_tables = {
            "oplog", "projection_meta", "engine_config", "schema_meta"
        }
        
        if use_postgres:
            with connect_postgres() as pg_conn:
                if _is_sqlite_conn(pg_conn):
                    table_rows = pg_conn.execute(
                        """
                            SELECT name, type
                            FROM sqlite_master
                            WHERE type IN ('table', 'view')
                            AND name NOT LIKE 'sqlite_%'
                            ORDER BY name
                            """
                    ).fetchall()
                else:
                    table_rows = pg_conn.execute(
                        """
                            SELECT table_name AS name,
                                   CASE WHEN table_type = 'VIEW' THEN 'view' ELSE 'table' END AS type
                            FROM information_schema.tables
                            WHERE table_schema = 'public'
                            ORDER BY table_name
                            """
                    ).fetchall()
        else:
            table_rows = conn.execute(
                """
                    SELECT name, type
                    FROM sqlite_master
                    WHERE type IN ('table', 'view')
                    AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
            ).fetchall()
        
        # Browser flat tables (one row per event, one column per field; DuckDB-friendly)
        browser_flat_tables = {"browser_visits", "browser_events"}
        
        # Initialize grouped tables
        grouped_tables = {
            "raw_retention": [],      # Raw retention (original payloads)
            "raw_enrichment": [],     # Raw enrichment (per-source, parser-assist)
            "browser_flat": [],       # Browser plugin flat (DuckDB-friendly)
            "source": [],
            "canonical": [],
            "enrichment_system": [],
            "canonical_enrichment": [],  # Canonical enrichment (shared)
            "system": [],
            "other": []
        }
        pooled_filtered_tables = 0
        pooled_hidden_unscoped_tables = 0
        
        for row in table_rows:
            table_name = row["name"] if isinstance(row, dict) else row[0]
            table_type = row["type"] if isinstance(row, dict) else row[1]
            
            if not _safe_sql_identifier(table_name):
                continue
            if table_name in DATA_EXPLORER_HIDDEN_TABLES:
                continue

            # Get table schema info
            try:
                if use_postgres:
                    with connect_postgres() as pg_conn:
                        if _is_sqlite_conn(pg_conn):
                            schema_cursor = pg_conn.execute(f'PRAGMA table_info("{table_name}")')
                            columns = [col["name"] for col in schema_cursor.fetchall()]
                        else:
                            schema_cursor = pg_conn.execute(
                                """
                                    SELECT column_name
                                    FROM information_schema.columns
                                    WHERE table_schema = 'public' AND table_name = %s
                                    ORDER BY ordinal_position
                                    """,
                                (table_name,),
                            )
                            columns = [col[0] for col in schema_cursor.fetchall()]
                else:
                    schema_cursor = conn.execute(f"PRAGMA table_info({table_name})")
                    columns = [col["name"] for col in schema_cursor.fetchall()]
            except Exception:
                columns = []

            scope_field: Optional[str] = None
            scope_value: Optional[str] = None
            scope_strategy: Optional[str] = None
            if pooled_mode or scope_requested:
                scope_field, scope_value, scope_strategy = _pooled_table_scope_for_columns(
                    set(columns),
                    pooled_dataset_id,
                    pooled_owner_user_id,
                    pooled_tenant_id,
                )
                # Pooled tenants must not see tables we cannot scope. Local/off-pool
                # engines still list source_id-only canonical tables (harness lanes).
                if pooled_mode and not (scope_field and scope_value and scope_strategy):
                    pooled_hidden_unscoped_tables += 1
                    continue

            try:
                if use_postgres:
                    with connect_postgres() as pg_conn:
                        if scope_field and scope_value:
                            if _is_sqlite_conn(pg_conn):
                                count_cursor = pg_conn.execute(
                                    f'SELECT COUNT(*) AS count FROM "{table_name}" WHERE "{scope_field}" = ?',
                                    (scope_value,),
                                )
                            else:
                                count_cursor = pg_conn.execute(
                                    f'SELECT COUNT(*) AS count FROM "{table_name}" WHERE "{scope_field}" = %s',
                                    (scope_value,),
                                )
                        else:
                            count_cursor = pg_conn.execute(f'SELECT COUNT(*) AS count FROM "{table_name}"')
                        count_row = count_cursor.fetchone()
                        if isinstance(count_row, dict):
                            row_count = count_row.get("count", 0)
                        else:
                            row_count = count_row[0] if count_row else 0
                else:
                    if scope_field and scope_value:
                        count_cursor = conn.execute(
                            f'SELECT COUNT(*) as count FROM "{table_name}" WHERE "{scope_field}" = ?',
                            (scope_value,),
                        )
                    else:
                        count_cursor = conn.execute(f'SELECT COUNT(*) as count FROM "{table_name}"')
                    row_count = count_cursor.fetchone()["count"]
            except Exception:
                row_count = None

            if pooled_mode and scope_field and isinstance(row_count, int) and row_count <= 0:
                pooled_filtered_tables += 1
                continue
            
            table_info = {
                "name": table_name,
                "type": table_type,
                "row_count": row_count,
                "columns": columns,
            }
            if (pooled_mode or scope_requested) and scope_strategy:
                table_info["policy_scope_field"] = scope_strategy
            
            # Categorize table based on architecture layers
            # Order matters: check more specific patterns first
            category_key = "other"
            # 1. Check for canonical enrichment tables (message_* prefix)
            if table_name.startswith(canonical_enrichment_prefix) or table_name in canonical_enrichment_tables:
                category_key = "canonical_enrichment"
            # 2. Check for raw enrichment tables (parser-assist enrichment)
            elif table_name in raw_enrichment_tables or any(table_name.startswith(prefix) for prefix in raw_enrichment_prefixes):
                category_key = "raw_enrichment"
            # 3. Check for raw retention tables (original payloads)
            elif any(table_name.startswith(pattern) for pattern in raw_retention_patterns):
                category_key = "raw_retention"
            # 4. Check for other raw_* tables (fallback - likely raw retention)
            elif table_name.startswith("raw_"):
                category_key = "raw_retention"
            # 4b. Browser flat tables (one row per event, flat columns for DuckDB)
            elif table_name in browser_flat_tables:
                category_key = "browser_flat"
            # 5. Source-normalized tables
            elif table_name in source_tables:
                category_key = "source"
            # 6. Canonical tables
            elif table_name in canonical_tables:
                category_key = "canonical"
            # 7. Enrichment system tables
            elif table_name in enrichment_system_tables:
                category_key = "enrichment_system"
            # 8. System tables
            elif table_name in system_tables:
                category_key = "system"
            layer_kind, layer_label = layer_for_category(category_key)
            table_info["layer_kind"] = layer_kind
            table_info["layer_label"] = layer_label
            grouped_tables[category_key].append(table_info)
        
        # Count only true MCP-originated calls. Frontend/internal callers do not
        # set mcp_source and should not inflate MCP usage metrics.
        if _mcp_source:
            if conn is not None:
                record_mcp_request(
                    conn,
                    "list_database_tables",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(conn),
                )

        engine_context: Dict[str, Any] = {
            "note": (
                "Owner path: use primary_dataset_id with get_messages when message_stream is "
                "'conversation'. Grantees: use list_shared_resources / shared_* with resource_id "
                "(dataset id is embedded there); do not guess dataset_id for someone else's node."
            ),
        }
        uid = (
            ((get_user_id(conn) if conn is not None else None) or "").strip()
            or (getattr(settings, "user_id", None) or "").strip()
            or None
        )
        if uid:
            engine_context["user_id"] = uid
            engine_context["primary_dataset_id"] = _primary_dataset_id_for_engine_context(uid)

        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "tables": grouped_tables,
                "categories": {
                    "raw_retention": "Raw Retention Tables",
                    "raw_enrichment": "Raw Enrichment Tables (Per-Source)",
                    "browser_flat": "Browser (flat, DuckDB-friendly)",
                    "source": "Source-Normalized Tables",
                    "canonical": "Canonical Tables",
                    "enrichment_system": "Enrichment System Tables",
                    "canonical_enrichment": "Canonical Enrichment Tables (Shared)",
                    "system": "System Tables",
                    "other": "Other Tables",
                },
                "layer_kinds": layer_kind_labels(),
                "engine_context": engine_context,
                "policy": (
                    {
                        "mode": "pooled",
                        "scope_applied": True,
                        "hidden_unscoped_tables": pooled_hidden_unscoped_tables,
                        "hidden_empty_tables": pooled_filtered_tables,
                        **_pooled_endpoint_policy_for_message(msg_type),
                    }
                    if pooled_mode
                    else {
                        "mode": "off",
                        "scope_applied": False,
                        **_pooled_endpoint_policy_for_message(msg_type),
                    }
                ),
            }
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list database tables: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_table_count")
async def handle_get_table_count(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    msg_type = str(message.get("type") or "").strip().lower()
    """Get row count for a specific table. Used for entry counts in frontend."""
    try:
        payload = message.get("payload") or {}
        table_name = (payload.get("table_name") or "").strip()
        query_plan: List[str] = []
        started_at = time_module.perf_counter()
        pooled_mode = _pooled_read_enforcement_enabled()
        pooled_dataset_id = (payload.get("dataset_id") or "").strip() or None
        pooled_owner_user_id = (payload.get("owner_user_id") or "").strip() or None
        pooled_tenant_id = (payload.get("tenant_id") or "").strip() or None
        if pooled_mode and not (pooled_dataset_id or pooled_owner_user_id or pooled_tenant_id):
            return {
                "id": req_id,
                "status": "error",
                "error": "Pooled table count requires tenant context",
                "error_metadata": {
                    "policy_reason": "missing_tenant_context",
                    "mode": "pooled",
                },
            }
        if not table_name:
            return {"id": req_id, "status": "error", "error": "table_name required"}
        if not _safe_sql_identifier(table_name):
            return {"id": req_id, "status": "error", "error": "Invalid table_name"}
        if settings.topos_database_mode == "postgres":
            with connect_postgres() as conn:
                if _is_sqlite_conn(conn):
                    check = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                        (table_name,),
                    ).fetchone()
                else:
                    check = conn.execute(
                        """
                            SELECT 1 FROM information_schema.tables
                            WHERE table_schema='public' AND table_name=%s
                            LIMIT 1
                            """,
                        (table_name,),
                    ).fetchone()
                if not check:
                    return {"id": req_id, "status": "ok", "payload": {"table_name": table_name, "count": 0}}
                col_rows = (
                    conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                    if _is_sqlite_conn(conn)
                    else conn.execute(
                        """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema='public' AND table_name=%s
                            """,
                        (table_name,),
                    ).fetchall()
                )
                col_names = (
                    {str(c["name"]) for c in col_rows}
                    if _is_sqlite_conn(conn)
                    else {str(c[0]) for c in col_rows}
                )
                scope_field, scope_value, scope_strategy = _pooled_table_scope_for_columns(
                    col_names,
                    pooled_dataset_id,
                    pooled_owner_user_id,
                    pooled_tenant_id,
                )
                if pooled_mode and not (scope_field and scope_value and scope_strategy):
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": f"table not tenant scoped for pooled reads: {table_name}",
                        "error_metadata": {
                            "policy_reason": "table_not_scoped",
                            "mode": "pooled",
                            "table_name": table_name,
                        },
                    }
                if scope_field and scope_value:
                    if _is_sqlite_conn(conn):
                        sql = f'SELECT COUNT(*) AS count FROM "{table_name}" WHERE "{scope_field}" = ?'
                        query_plan = _sqlite_query_plan(conn, sql, (scope_value,))
                        row = conn.execute(
                            sql,
                            (scope_value,),
                        ).fetchone()
                    else:
                        row = conn.execute(
                            f'SELECT COUNT(*) AS count FROM "{table_name}" WHERE "{scope_field}" = %s',
                            (scope_value,),
                        ).fetchone()
                else:
                    row = conn.execute(f'SELECT COUNT(*) AS count FROM "{table_name}"').fetchone()
                if isinstance(row, dict):
                    count = row.get("count", 0)
                else:
                    count = row[0] if row else 0
        else:
            conn = hub.get_db_connection()
            if not conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}
            check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (table_name,),
            ).fetchone()
            if not check:
                return {"id": req_id, "status": "ok", "payload": {"table_name": table_name, "count": 0}}
            col_rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            col_names = {str(c["name"]) for c in col_rows} if col_rows else set()
            scope_field, scope_value, scope_strategy = _pooled_table_scope_for_columns(
                col_names,
                pooled_dataset_id,
                pooled_owner_user_id,
                pooled_tenant_id,
            )
            if pooled_mode and not (scope_field and scope_value and scope_strategy):
                return {
                    "id": req_id,
                    "status": "error",
                    "error": f"table not tenant scoped for pooled reads: {table_name}",
                    "error_metadata": {
                        "policy_reason": "table_not_scoped",
                        "mode": "pooled",
                        "table_name": table_name,
                    },
                }
            if scope_field and scope_value:
                sql = f'SELECT COUNT(*) as count FROM "{table_name}" WHERE "{scope_field}" = ?'
                query_plan = _sqlite_query_plan(conn, sql, (scope_value,))
                row = conn.execute(
                    sql,
                    (scope_value,),
                ).fetchone()
            else:
                sql = f'SELECT COUNT(*) as count FROM "{table_name}"'
                query_plan = _sqlite_query_plan(conn, sql, ())
                row = conn.execute(sql).fetchone()
            count = row["count"] if row else 0
        query_duration_ms = round((time_module.perf_counter() - started_at) * 1000, 3)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "table_name": table_name,
                "count": count,
                "policy": {
                    "mode": "pooled" if pooled_mode else "off",
                    "scope_applied": bool(pooled_mode),
                    "query_duration_ms": query_duration_ms,
                    "query_plan": query_plan,
                    **_pooled_endpoint_policy_for_message(msg_type),
                },
                "query_duration_ms": query_duration_ms,
                "query_plan": query_plan,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to get table count: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_table_rows")
async def handle_get_table_rows(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    msg_type = str(message.get("type") or "").strip().lower()
    """Return rows from a table for the simple table viewer. Limit to avoid huge payloads."""
    try:
        from ...llm_integrations_storage import DATA_EXPLORER_HIDDEN_TABLES

        payload = message.get("payload") or {}
        table_name = (payload.get("table_name") or "").strip()
        requested_limit = max(1, int(payload.get("limit") or 500))
        limit = min(requested_limit, 2000)
        cap_reason = "max_rows_limit" if limit < requested_limit else None
        offset = max(0, int(payload.get("offset") or 0))
        order_by = (payload.get("order_by") or payload.get("sort_column") or "").strip() or None
        order_dir = (payload.get("order_dir") or payload.get("sort_direction") or "").strip() or None
        scope_strategy: Optional[str] = None
        query_plan: List[str] = []
        started_at = time_module.perf_counter()
        pooled_mode = _pooled_read_enforcement_enabled()
        pooled_dataset_id = (payload.get("dataset_id") or "").strip() or None
        pooled_owner_user_id = (payload.get("owner_user_id") or "").strip() or None
        pooled_tenant_id = (payload.get("tenant_id") or "").strip() or None
        if pooled_mode and not (pooled_dataset_id or pooled_owner_user_id or pooled_tenant_id):
            return {
                "id": req_id,
                "status": "error",
                "error": "Pooled table reads require tenant context",
                "error_metadata": {
                    "policy_reason": "missing_tenant_context",
                    "mode": "pooled",
                },
            }
        if not table_name:
            return {"id": req_id, "status": "error", "error": "table_name required"}
        if not _safe_sql_identifier(table_name):
            return {"id": req_id, "status": "error", "error": "Invalid table_name"}
        if table_name in DATA_EXPLORER_HIDDEN_TABLES:
            return {
                "id": req_id,
                "status": "error",
                "error": "This table is managed in Settings and is not available in Data explorer",
            }
        if settings.topos_database_mode == "postgres":
            with connect_postgres() as conn:
                if _is_sqlite_conn(conn):
                    check = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                        (table_name,),
                    ).fetchone()
                else:
                    check = conn.execute(
                        """
                            SELECT 1 FROM information_schema.tables
                            WHERE table_schema='public' AND table_name=%s
                            LIMIT 1
                            """,
                        (table_name,),
                    ).fetchone()
                if not check:
                    return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}
                col_rows = (
                    conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                    if _is_sqlite_conn(conn)
                    else conn.execute(
                        """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema='public' AND table_name=%s
                            """,
                        (table_name,),
                    ).fetchall()
                )
                col_names = (
                    {str(c["name"]) for c in col_rows}
                    if _is_sqlite_conn(conn)
                    else {str(c[0]) for c in col_rows}
                )
                scope_field, scope_value, scope_strategy = _pooled_table_scope_for_columns(
                    col_names,
                    pooled_dataset_id,
                    pooled_owner_user_id,
                    pooled_tenant_id,
                )
                if pooled_mode and not (scope_field and scope_value and scope_strategy):
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": f"table not tenant scoped for pooled reads: {table_name}",
                        "error_metadata": {
                            "policy_reason": "table_not_scoped",
                            "mode": "pooled",
                            "table_name": table_name,
                        },
                    }
                order_clause = _resolve_table_row_order_clause(
                    col_names,
                    table_name=table_name,
                    is_sqlite=_is_sqlite_conn(conn),
                    order_by=order_by,
                    order_dir=order_dir,
                )
                if _is_sqlite_conn(conn):
                    if scope_field and scope_value:
                        sql = (
                            f'SELECT * FROM "{table_name}" WHERE "{scope_field}" = ? '
                            f"ORDER BY {order_clause} LIMIT ? OFFSET ?"
                        )
                        sql_params = (scope_value, limit + 1, offset)
                        query_plan = _sqlite_query_plan(conn, sql, sql_params)
                        cursor = conn.execute(
                            sql,
                            sql_params,
                        )
                    else:
                        sql = f'SELECT * FROM "{table_name}" ORDER BY {order_clause} LIMIT ? OFFSET ?'
                        sql_params = (limit + 1, offset)
                        query_plan = _sqlite_query_plan(conn, sql, sql_params)
                        cursor = conn.execute(sql, sql_params)
                    rows = [dict(r) for r in cursor.fetchall()]
                else:
                    if scope_field and scope_value:
                        cursor = conn.execute(
                            f'SELECT * FROM "{table_name}" WHERE "{scope_field}" = %s '
                            f"ORDER BY {order_clause} LIMIT %s OFFSET %s",
                            (scope_value, limit + 1, offset),
                        )
                    else:
                        cursor = conn.execute(
                            f'SELECT * FROM "{table_name}" ORDER BY {order_clause} LIMIT %s OFFSET %s',
                            (limit + 1, offset),
                        )
                    result_col_names = [desc[0] for desc in (cursor.description or [])]
                    rows = [dict(zip(result_col_names, r)) for r in cursor.fetchall()]
        else:
            conn = hub.get_db_connection()
            if not conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}
            check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (table_name,),
            ).fetchone()
            if not check:
                return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}
            col_rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            col_names = {str(c["name"]) for c in col_rows} if col_rows else set()
            scope_field, scope_value, scope_strategy = _pooled_table_scope_for_columns(
                col_names,
                pooled_dataset_id,
                pooled_owner_user_id,
                pooled_tenant_id,
            )
            if pooled_mode and not (scope_field and scope_value and scope_strategy):
                return {
                    "id": req_id,
                    "status": "error",
                    "error": f"table not tenant scoped for pooled reads: {table_name}",
                    "error_metadata": {
                        "policy_reason": "table_not_scoped",
                        "mode": "pooled",
                        "table_name": table_name,
                    },
                }
            order_clause = _resolve_table_row_order_clause(
                col_names,
                table_name=table_name,
                is_sqlite=True,
                order_by=order_by,
                order_dir=order_dir,
            )
            if scope_field and scope_value:
                sql = (
                    f'SELECT * FROM "{table_name}" WHERE "{scope_field}" = ? '
                    f"ORDER BY {order_clause} LIMIT ? OFFSET ?"
                )
                sql_params = (scope_value, limit + 1, offset)
                query_plan = _sqlite_query_plan(conn, sql, sql_params)
                cursor = conn.execute(
                    sql,
                    sql_params,
                )
            else:
                sql = f'SELECT * FROM "{table_name}" ORDER BY {order_clause} LIMIT ? OFFSET ?'
                sql_params = (limit + 1, offset)
                query_plan = _sqlite_query_plan(conn, sql, sql_params)
                cursor = conn.execute(sql, sql_params)
            rows = [dict(r) for r in cursor.fetchall()]
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        query_duration_ms = round((time_module.perf_counter() - started_at) * 1000, 3)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "rows": rows,
                "table_name": table_name,
                "policy": {
                    "mode": "pooled" if pooled_mode else "off",
                    "scope_applied": bool(pooled_mode),
                    "state": "scoped" if pooled_mode else "none",
                    "scope_field": (scope_strategy if pooled_mode else None),
                    "requested_limit": requested_limit,
                    "applied_limit": limit,
                    "cap_reason": cap_reason,
                    "query_duration_ms": query_duration_ms,
                    "query_plan": query_plan,
                    **_pooled_endpoint_policy_for_message(msg_type),
                },
                "requested_limit": requested_limit,
                "applied_limit": limit,
                "cap_reason": cap_reason,
                "has_more": has_more,
                "query_duration_ms": query_duration_ms,
                "query_plan": query_plan,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to get table rows: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("pooled_scope_backfill_dry_run")
async def handle_pooled_scope_backfill_dry_run(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    """Return scoped migration dry-run details and checksum for pooled scope backfill."""
    try:
        payload = message.get("payload") or {}
        requested_tables = payload.get("tables")
        if requested_tables is not None and not isinstance(requested_tables, list):
            return {"id": req_id, "status": "error", "error": "tables must be an array of table names"}
        if settings.topos_database_mode == "postgres":
            with connect_postgres() as conn:
                if not _is_sqlite_conn(conn):
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": "pooled_scope_backfill_dry_run currently supports sqlite-backed engines only",
                    }
                plan = _pooled_scope_backfill_dry_run(
                    conn,
                    requested_tables=[str(t) for t in requested_tables] if isinstance(requested_tables, list) else None,
                )
        else:
            conn = hub.get_db_connection()
            if not conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}
            plan = _pooled_scope_backfill_dry_run(
                conn,
                requested_tables=[str(t) for t in requested_tables] if isinstance(requested_tables, list) else None,
            )
        return {"id": req_id, "status": "ok", "payload": plan}
    except Exception as exc:  # noqa: BLE001
        logger.error("pooled_scope_backfill_dry_run failed: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("pooled_scope_backfill_apply")
async def handle_pooled_scope_backfill_apply(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    """Backfill missing tenant scope columns and persist rollback journal."""
    try:
        payload = message.get("payload") or {}
        requested_tables = payload.get("tables")
        if requested_tables is not None and not isinstance(requested_tables, list):
            return {"id": req_id, "status": "error", "error": "tables must be an array of table names"}
        if settings.topos_database_mode == "postgres":
            with connect_postgres() as conn:
                if not _is_sqlite_conn(conn):
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": "pooled_scope_backfill_apply currently supports sqlite-backed engines only",
                    }
                result = _pooled_scope_backfill_apply(
                    conn,
                    dataset_id=(payload.get("dataset_id") or "").strip() or None,
                    owner_user_id=(payload.get("owner_user_id") or "").strip() or None,
                    tenant_id=(payload.get("tenant_id") or "").strip() or None,
                    requested_tables=[str(t) for t in requested_tables] if isinstance(requested_tables, list) else None,
                )
        else:
            conn = hub.get_db_connection()
            if not conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}
            result = _pooled_scope_backfill_apply(
                conn,
                dataset_id=(payload.get("dataset_id") or "").strip() or None,
                owner_user_id=(payload.get("owner_user_id") or "").strip() or None,
                tenant_id=(payload.get("tenant_id") or "").strip() or None,
                requested_tables=[str(t) for t in requested_tables] if isinstance(requested_tables, list) else None,
            )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("pooled_scope_backfill_apply failed: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("pooled_scope_backfill_rollback")
async def handle_pooled_scope_backfill_rollback(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    """Rollback a pooled scope backfill migration id."""
    try:
        payload = message.get("payload") or {}
        migration_id = str(payload.get("migration_id") or "").strip()
        if not migration_id:
            return {"id": req_id, "status": "error", "error": "migration_id required"}
        if settings.topos_database_mode == "postgres":
            with connect_postgres() as conn:
                if not _is_sqlite_conn(conn):
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": "pooled_scope_backfill_rollback currently supports sqlite-backed engines only",
                    }
                result = _pooled_scope_backfill_rollback(conn, migration_id)
        else:
            conn = hub.get_db_connection()
            if not conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}
            result = _pooled_scope_backfill_rollback(conn, migration_id)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("pooled_scope_backfill_rollback failed: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("delete_database_table", owner_only=True)
async def handle_delete_database_table(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    msg_type = str(message.get("type") or "").strip().lower()
    """Clear rows or drop tables/views. Canonical schema tables may only be cleared."""
    from ...data_explorer_tables import is_canonical_schema_table
    from ...llm_integrations_storage import DATA_EXPLORER_HIDDEN_TABLES

    _NON_DROPPABLE_TABLES = frozenset(
        {
            "engine_config",
            "oplog",
            "projection_meta",
            "schema_meta",
            "sqlite_sequence",
            "ingestion_checkpoints",
            "ingestion_jobs",
            "ingestion_errors",
            *DATA_EXPLORER_HIDDEN_TABLES,
        }
    )

    def _resolve_table_action(table_name: str, requested_action: Any) -> tuple[str | None, str | None]:
        canonical = is_canonical_schema_table(table_name)
        action = str(requested_action or "").strip().lower()
        if not action:
            return ("clear" if canonical else "drop", None)
        if action not in ("clear", "drop"):
            return (None, f"Invalid action: {requested_action!r} (expected 'clear' or 'drop')")
        if canonical and action == "drop":
            return (None, f"Canonical schema table cannot be dropped: {table_name}")
        return (action, None)

    def _clear_table_rows(conn: Any, table_name: str) -> int:
        count_row = conn.execute(f'SELECT COUNT(*) AS count FROM "{table_name}"').fetchone()
        if count_row is None:
            rows_before = 0
        elif isinstance(count_row, dict):
            rows_before = int(count_row.get("count") or 0)
        else:
            rows_before = int(count_row[0] or 0)
        if _is_sqlite_conn(conn):
            # DELETE takes SQLite's write lock at execute time — gate it with
            # the commit (write_gate lock-order inversion).
            with with_db_write():
                conn.execute(f'DELETE FROM "{table_name}"')
                commit_connection(conn)
        else:
            conn.execute(f'DELETE FROM "{table_name}"')
            conn.commit()
        return rows_before

    def _drop_table_or_view(conn: Any, *, table_name: str, obj_type: str, is_sqlite: bool) -> None:
        if is_sqlite:
            with with_db_write():
                conn.execute(f'DROP {obj_type} IF EXISTS "{table_name}"')
                commit_connection(conn)
        else:
            drop_type = "VIEW" if obj_type == "view" else "TABLE"
            conn.execute(f'DROP {drop_type} IF EXISTS "{table_name}"')
            conn.commit()

    try:
        payload = message.get("payload") or {}
        pooled_mode = _pooled_read_enforcement_enabled()
        if pooled_mode:
            return {
                "id": req_id,
                "status": "error",
                "error": "delete_database_table is blocked in pooled mode until write-path hardening is complete",
                "error_metadata": {
                    "policy_reason": "endpoint_not_hardened",
                    "mode": "pooled",
                    **_pooled_endpoint_policy_for_message(msg_type),
                },
            }
        table_name = (payload.get("table_name") or "").strip()
        if not table_name:
            return {"id": req_id, "status": "error", "error": "table_name required"}
        if table_name.startswith("sqlite_"):
            return {"id": req_id, "status": "error", "error": "Cannot drop SQLite internal objects"}
        if table_name.startswith("hosted_job__"):
            return {
                "id": req_id,
                "status": "error",
                "error": (
                    "hosted_job__* names are not database tables. Remove the upload via "
                    "DELETE /v1/database/jsonl_files?job_id=… on the control plane."
                ),
            }
        if table_name in _NON_DROPPABLE_TABLES:
            return {"id": req_id, "status": "error", "error": f"Table is protected from deletion: {table_name}"}
        table_action, action_error = _resolve_table_action(table_name, payload.get("action"))
        if action_error:
            return {"id": req_id, "status": "error", "error": action_error}
        if settings.topos_database_mode == "postgres":
            with connect_postgres() as conn:
                is_sqlite = _is_sqlite_conn(conn)
                if is_sqlite:
                    meta = conn.execute(
                        "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                        (table_name,),
                    ).fetchone()
                else:
                    meta = conn.execute(
                        """
                            SELECT table_name, table_type
                            FROM information_schema.tables
                            WHERE table_schema='public' AND table_name=%s
                            LIMIT 1
                            """,
                        (table_name,),
                    ).fetchone()
                if not meta:
                    return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}
                if is_sqlite:
                    obj_type = str(meta["type"])
                else:
                    table_type = str(meta[1] or "").upper()
                    obj_type = "view" if table_type == "VIEW" else "table"
                if obj_type not in ("table", "view"):
                    return {"id": req_id, "status": "error", "error": f"Unsupported object type: {obj_type}"}
                if table_action == "clear":
                    if obj_type != "table":
                        return {
                            "id": req_id,
                            "status": "error",
                            "error": f"Cannot clear rows from a view: {table_name}",
                        }
                    rows_deleted = _clear_table_rows(conn, table_name)
                    action = "cleared"
                else:
                    _drop_table_or_view(conn, table_name=table_name, obj_type=obj_type, is_sqlite=is_sqlite)
                    rows_deleted = 0
                    action = "dropped"
        else:
            conn = hub.get_db_connection()
            if not conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}
            meta = conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (table_name,),
            ).fetchone()
            if not meta:
                return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}
            obj_type = str(meta["type"])
            if obj_type not in ("table", "view"):
                return {"id": req_id, "status": "error", "error": f"Unsupported object type: {obj_type}"}
            if table_action == "clear":
                if obj_type != "table":
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": f"Cannot clear rows from a view: {table_name}",
                    }
                rows_deleted = _clear_table_rows(conn, table_name)
                action = "cleared"
            else:
                _drop_table_or_view(conn, table_name=table_name, obj_type=obj_type, is_sqlite=True)
                rows_deleted = 0
                action = "dropped"
        payload_out: Dict[str, Any] = {
            "table_name": table_name,
            "action": action,
            "rows_deleted": rows_deleted,
        }
        if action == "dropped":
            payload_out["dropped_type"] = obj_type
        return {
            "id": req_id,
            "status": "ok",
            "payload": payload_out,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to delete database table: %s", exc, exc_info=True)
        try:
            rb = hub.get_db_connection()
            if rb is not None:
                rb.rollback()
        except Exception:
            pass
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("delete_database_rows", owner_only=True)
async def handle_delete_database_rows(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    msg_type = str(message.get("type") or "").strip().lower()
    """Delete selected rows with optional upstream/downstream pipeline scope."""
    from ...data_explorer_row_delete import delete_database_rows
    from ...llm_integrations_storage import DATA_EXPLORER_HIDDEN_TABLES

    try:
        pooled_mode = _pooled_read_enforcement_enabled()
        if pooled_mode:
            return {
                "id": req_id,
                "status": "error",
                "error": "delete_database_rows is blocked in pooled mode until write-path hardening is complete",
                "error_metadata": {
                    "policy_reason": "endpoint_not_hardened",
                    "mode": "pooled",
                    **_pooled_endpoint_policy_for_message(msg_type),
                },
            }
        payload = message.get("payload") or {}
        table_name = str(payload.get("table_name") or "").strip()
        row_ids = payload.get("row_ids") or []
        scope = str(payload.get("scope") or "row_only").strip().lower()
        if table_name in DATA_EXPLORER_HIDDEN_TABLES:
            return {
                "id": req_id,
                "status": "error",
                "error": f"Table is hidden from Data Explorer deletes: {table_name}",
            }
        if not isinstance(row_ids, list):
            return {"id": req_id, "status": "error", "error": "row_ids must be an array"}
        conn = hub.get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database connection not available"}
        result = delete_database_rows(
            conn,
            table_name=table_name,
            row_ids=[str(item) for item in row_ids],
            scope=scope,
        )
        return {"id": req_id, "status": "ok", "payload": result.to_payload()}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to delete database rows: %s", exc, exc_info=True)
        try:
            rb = hub.get_db_connection()
            if rb is not None:
                rb.rollback()
        except Exception:
            pass
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_table_schema")
async def handle_get_table_schema(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    _payload = message.get("payload") or {}
    _mcp_source = _payload.get("mcp_source")
    _mcp_requester_id = _payload.get("mcp_requester_id")
    """Return column info for a table (PRAGMA table_info). Used by MCP get_table_schema tool."""
    try:
        payload = message.get("payload") or {}
        table_name = (payload.get("table_name") or "").strip()
        if not table_name:
            return {"id": req_id, "status": "error", "error": "table_name required"}
        if not _safe_sql_identifier(table_name):
            return {"id": req_id, "status": "error", "error": "Invalid table_name"}
        conn = None
        if settings.topos_database_mode == "postgres":
            with connect_postgres() as pg_conn:
                if _is_sqlite_conn(pg_conn):
                    check = pg_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                        (table_name,),
                    ).fetchone()
                else:
                    check = pg_conn.execute(
                        """
                            SELECT 1 FROM information_schema.tables
                            WHERE table_schema='public' AND table_name=%s
                            LIMIT 1
                            """,
                        (table_name,),
                    ).fetchone()
                if not check:
                    return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}
                if _is_sqlite_conn(pg_conn):
                    schema_cursor = pg_conn.execute(f"PRAGMA table_info([{table_name}])")
                    columns = [dict(col) for col in schema_cursor.fetchall()]
                else:
                    schema_cursor = pg_conn.execute(
                        """
                            SELECT
                                column_name AS name,
                                data_type AS type,
                                is_nullable = 'NO' AS notnull
                            FROM information_schema.columns
                            WHERE table_schema='public' AND table_name=%s
                            ORDER BY ordinal_position
                            """,
                        (table_name,),
                    )
                    columns = [
                        {
                            "name": row[0],
                            "type": row[1],
                            "notnull": bool(row[2]),
                        }
                        for row in schema_cursor.fetchall()
                    ]
        else:
            conn = hub.get_db_connection()
            if not conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}
            check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (table_name,),
            ).fetchone()
            if not check:
                return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}
            schema_cursor = conn.execute(f"PRAGMA table_info([{table_name}])")
            columns = [dict(col) for col in schema_cursor.fetchall()]
        if _mcp_source:
            if conn is not None:
                record_mcp_request(
                    conn,
                    "get_table_schema",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(conn),
                )
        return {"id": req_id, "status": "ok", "payload": {"table_name": table_name, "columns": columns}}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to get table schema: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("list_jsonl_files")
async def handle_list_jsonl_files(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    """List all JSONL files in ingestion directories."""
    try:
        from pathlib import Path

        jsonl_files = []
        
        # Check multiple potential locations
        potential_paths = [
            Path.home() / ".topos_engine" / "ingestion",
            Path.home() / ".topos" / "ingestion",
        ]
        
        for base_path in potential_paths:
            if base_path.exists() and base_path.is_dir():
                # Find all JSONL files recursively
                for jsonl_file in base_path.rglob("*.jsonl"):
                    # Skip backup files
                    if jsonl_file.name.endswith(".backup"):
                        continue
                    
                    try:
                        # Get file stats
                        stat = jsonl_file.stat()
                        file_size = stat.st_size
                        
                        # Count lines (messages) in file
                        line_count = 0
                        try:
                            with open(jsonl_file, 'r', encoding='utf-8') as f:
                                line_count = sum(1 for line in f if line.strip())
                        except Exception:
                            pass
                        
                        # Get relative path from base
                        relative_path = str(jsonl_file.relative_to(base_path))
                        
                        jsonl_files.append({
                            "path": str(jsonl_file),
                            "relative_path": relative_path,
                            "base_path": str(base_path),
                            "file_name": jsonl_file.name,
                            "size_bytes": file_size,
                            "line_count": line_count,
                            "modified_at": stat.st_mtime,
                        })
                    except Exception as e:
                        logger.warning("Failed to read file info for %s: %s", jsonl_file, e)
        
        # Sort by modified time (newest first)
        jsonl_files.sort(key=lambda x: x.get("modified_at", 0), reverse=True)
        
        return {"id": req_id, "status": "ok", "payload": {"files": jsonl_files, "base_paths": [str(p) for p in potential_paths]}}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list JSONL files: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("delete_jsonl_file")
async def handle_delete_jsonl_file(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    """Delete a JSONL file under allowed ingestion roots only (absolute path from list_jsonl_files)."""
    try:
        from pathlib import Path

        payload = message.get("payload") or {}
        raw_path = (payload.get("file_path") or "").strip()
        if not raw_path:
            return {"id": req_id, "status": "error", "error": "file_path required"}
        potential_paths = [
            Path.home() / ".topos_engine" / "ingestion",
            Path.home() / ".topos" / "ingestion",
        ]
        try:
            target = Path(raw_path).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            return {"id": req_id, "status": "error", "error": f"Invalid file path: {exc}"}

        under_allowed = False
        for base_path in potential_paths:
            try:
                broot = base_path.expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if not broot.is_dir():
                continue
            try:
                target.relative_to(broot)
                under_allowed = True
                break
            except ValueError:
                continue

        if not under_allowed:
            return {"id": req_id, "status": "error", "error": "File path is outside allowed ingestion directories"}
        if not target.name.lower().endswith(".jsonl"):
            return {"id": req_id, "status": "error", "error": "Only .jsonl files may be deleted"}
        if not target.is_file():
            return {"id": req_id, "status": "error", "error": f"File not found: {target}"}
        target.unlink()
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"file_path": str(target), "deleted": True},
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to delete JSONL file: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("read_jsonl_file")
async def handle_read_jsonl_file(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    """Read a JSONL file under allowed ingestion roots (same scope as delete_jsonl_file)."""
    try:
        from pathlib import Path

        _max_jsonl_download_bytes = 50 * 1024 * 1024
        payload = message.get("payload") or {}
        raw_path = (payload.get("file_path") or "").strip()
        if not raw_path:
            return {"id": req_id, "status": "error", "error": "file_path required"}
        potential_paths = [
            Path.home() / ".topos_engine" / "ingestion",
            Path.home() / ".topos" / "ingestion",
        ]
        try:
            target = Path(raw_path).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            return {"id": req_id, "status": "error", "error": f"Invalid file path: {exc}"}

        under_allowed = False
        for base_path in potential_paths:
            try:
                broot = base_path.expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if not broot.is_dir():
                continue
            try:
                target.relative_to(broot)
                under_allowed = True
                break
            except ValueError:
                continue

        if not under_allowed:
            return {"id": req_id, "status": "error", "error": "File path is outside allowed ingestion directories"}
        if not target.name.lower().endswith(".jsonl"):
            return {"id": req_id, "status": "error", "error": "Only .jsonl files may be read"}
        if not target.is_file():
            return {"id": req_id, "status": "error", "error": f"File not found: {target}"}
        size = target.stat().st_size
        if size > _max_jsonl_download_bytes:
            return {
                "id": req_id,
                "status": "error",
                "error": f"File too large to download via API ({size} bytes; max {_max_jsonl_download_bytes})",
            }
        raw = target.read_bytes()
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "file_path": str(target),
                "file_name": target.name,
                "size_bytes": size,
                "content_base64": base64.b64encode(raw).decode("ascii"),
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to read JSONL file: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}
