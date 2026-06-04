from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time as time_module
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from topos.contacts.identity import normalize_contact_key as _normalize_contact_key
from topos.core.table_layers import layer_for_category, layer_kind_labels

logger = logging.getLogger("topos.core.handlers")

_GOOGLE_CONTACT_IMPORT_SESSIONS: Dict[str, Dict[str, Any]] = {}
UI_CONFIG_KEY = "ui_config"
ALLOWED_PINNED_WIDGETS = {
    "umaAllTime",
    "uma24h",
    "mcpRows",
    "mcp24h",
    "topConnector",
    "umaStatusMix",
    "topConnectors",
    "mcpSources",
}
MAX_PINNED_WIDGETS = 3


def _owner_user_id_from_dataset_id(dataset_id: Optional[str]) -> Optional[str]:
    raw = str(dataset_id or "").strip()
    if not raw or ":" not in raw:
        return None
    owner = raw.split(":", 1)[0].strip()
    return owner or None


async def _download_ingestion_payload(file_url: str) -> bytes:
    """Download ingestion payload from either HTTPS or gs:// URL."""
    if str(file_url).startswith("gs://"):
        from google.cloud import storage

        # gs://bucket/path -> bucket, blob path
        remainder = str(file_url)[len("gs://") :]
        if "/" not in remainder:
            raise ValueError(f"Invalid gs:// URL (missing object path): {file_url}")
        bucket_name, blob_path = remainder.split("/", 1)
        if not bucket_name or not blob_path:
            raise ValueError(f"Invalid gs:// URL: {file_url}")

        def _download_bytes() -> bytes:
            client = storage.Client()
            blob = client.bucket(bucket_name).blob(blob_path)
            return blob.download_as_bytes()

        return await asyncio.to_thread(_download_bytes)

    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(file_url)
        resp.raise_for_status()
        return resp.content


def _resource_owner_for_mcp_log(conn: Any) -> Optional[str]:
    if conn is None:
        return None
    uid = get_user_id(conn)
    return (uid or "").strip() or None


def _default_ui_config() -> Dict[str, Any]:
    return {"version": 1, "topbar": {"pinnedAnalytics": []}}


def _normalize_ui_config(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return _default_ui_config()
    out: Dict[str, Any] = {
        "version": int(value.get("version", 1)) if str(value.get("version", "")).isdigit() else 1,
        "topbar": {"pinnedAnalytics": []},
    }
    topbar = value.get("topbar")
    pinned = topbar.get("pinnedAnalytics") if isinstance(topbar, dict) else []
    if not isinstance(pinned, list):
        return out
    seen: set[str] = set()
    final: List[str] = []
    for item in pinned:
        wid = str(item or "").strip()
        if not wid or wid in seen or wid not in ALLOWED_PINNED_WIDGETS:
            continue
        seen.add(wid)
        final.append(wid)
        if len(final) >= MAX_PINNED_WIDGETS:
            break
    out["topbar"]["pinnedAnalytics"] = final
    return out


def _resolve_contact_import_targets(payload: Dict[str, Any]) -> tuple[str, List[str], Optional[str]]:
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not dataset_id:
        return "", [], "dataset_id required"
    requested = payload.get("apply_to_sources")
    if requested is None:
        requested = ["imessage", "signal"]
    if not isinstance(requested, list) or not requested:
        return "", [], "apply_to_sources must be a non-empty list"
    valid_local_sync = {
        sid for sid, definition in REGISTRY.items()
        if getattr(definition, "source_type", None) == "local_sync"
    }
    targets = [str(s or "").strip() for s in requested if str(s or "").strip()]
    if not targets:
        return "", [], "apply_to_sources has no valid source ids"
    invalid = [sid for sid in targets if sid not in valid_local_sync]
    if invalid:
        return "", [], f"invalid local_sync source ids: {', '.join(invalid)}"
    return dataset_id, sorted(set(targets)), None


def _normalize_messenger_source_filter(payload: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    source_id = payload.get("source_id")
    if isinstance(source_id, str) and source_id.strip():
        out.append(source_id.strip())
    source_ids = payload.get("source_ids")
    if isinstance(source_ids, str):
        out.extend([s.strip() for s in source_ids.split(",") if s.strip()])
    elif isinstance(source_ids, list):
        out.extend([str(s).strip() for s in source_ids if str(s).strip()])
    return sorted(set(out))


def _messenger_source_scope(source_filter: List[str]) -> str:
    if not source_filter:
        return "all"
    return ",".join(sorted(set(source_filter)))


def _uma_transform_progress_hook(req_id: str, stage_label: str):
    """Log coarse (10%) transform progress with a text progress bar."""
    state = {"bucket": -1}

    def _bar(pct: int, width: int = 20) -> str:
        pct = max(0, min(100, pct))
        filled = int((pct / 100.0) * width)
        return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"

    def _hook(done: int, total: int, current_filter: Optional[str] = None) -> None:
        total_safe = max(total, 1)
        pct = int((done * 100) / total_safe)
        bucket = pct // 10
        if bucket <= state["bucket"] and done < total:
            return
        state["bucket"] = bucket
        filter_text = f" filter={current_filter}" if current_filter else ""
        logger.info(
            "[PIPELINE:UMA][TRANSFORM] req=%s stage=%s%s %s %s%% (%s/%s)",
            req_id,
            stage_label,
            filter_text,
            _bar(pct),
            pct,
            done,
            total,
        )

    return _hook

from ..analytics.raw_queries import (
    avg_message_length,
    load_raw_messages,
    messages_by_sender,
    messages_per_day,
    total_messages,
)
from ..analytics.messenger_communities import (
    MESSENGER_COMMUNITIES_TABLE,
    MESSENGER_PARTICIPANT_IMPORTANCE_TABLE,
    MESSENGER_SOCIAL_EDGES_TABLE,
    compute_and_persist_messenger_analytics,
    ensure_messenger_analytics_tables,
)
from ..analytics.messenger_labels import (
    enrich_contact_rows_with_resolved_display_names,
    enrich_conversation_thread_previews,
    resolve_participant_labels,
)
from ..config.settings import settings
from ..storage.db.postgres import connect_postgres
from ..core.state import (
    get_db_connection,
    get_engine_config_value,
    get_mcp_request_counts,
    get_or_create_user_id,
    get_uma_request_counts,
    get_user_id,
    record_mcp_request,
    record_uma_request,
    set_engine_config_value,
    store_user_id,
)
from fastapi import HTTPException

from ..ingestion.ingest_helpers import ingest_file_payload, ingest_ui_payload
from ..services.container import get_services
from ..storage.raw.file_store import RawFileStore
from ..storage.signal_identity import get_signal_identity, put_signal_identity
from ..storage.source_settings import get_source_settings, put_source_settings, update_sync_result
from ..sources.registry import REGISTRY
from ..uma_contact_enrichment import apply_message_contact_pipeline, strip_contact_runtime_filters
from ..uma_resource_id import parse_dataset_id_from_uma_dataset_resource_id
from ..uma_filters import (
    UMAFilterError,
    apply_filter_manifest,
    apply_filter_manifest_async,
    build_sql_constraints,
    extract_field_transforms,
    extract_filter_manifest,
    get_limit_cap,
)
from ..engine.scoped_token import ScopedTokenValidationError, validate_scoped_invocation_token
from ..engine.registration import RUNTIME_PROFILE_OPERATIONS, resolve_runtime_profile


def _table_exists(conn, table_name: str) -> bool:
    """Check if a table exists in the database."""
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return cursor.fetchone() is not None
    except Exception:
        return False


def _safe_sql_identifier(name: str) -> bool:
    if not name:
        return False
    return all(ch.isalnum() or ch == "_" for ch in name)


def _is_sqlite_conn(conn: Any) -> bool:
    return "sqlite" in conn.__class__.__module__.lower()


def _resolve_uma_scope(payload: Dict[str, Any], resource_id: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve tenant scope for UMA reads: dataset_id, owner_user_id, tenant_id."""
    dataset_id = (payload.get("dataset_id") or "").strip() or None
    if not dataset_id and resource_id:
        dataset_id = parse_dataset_id_from_uma_dataset_resource_id(resource_id)
    owner_user_id = (payload.get("owner_user_id") or "").strip() or None
    if not owner_user_id and dataset_id:
        owner_user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
    if not owner_user_id and resource_id and len(resource_id.split(":")) >= 2:
        owner_user_id = resource_id.split(":")[1]
    tenant_id = (payload.get("tenant_id") or "").strip() or None
    return dataset_id, owner_user_id, tenant_id


def _build_uma_scope_clause(
    col_names: set[str],
    dataset_id: Optional[str],
    owner_user_id: Optional[str],
    tenant_id: Optional[str],
) -> tuple[str, tuple[Any, ...]]:
    """
    Build a mandatory scope predicate for UMA table reads.
    Prefer dataset_id (strongest), then owner_user_id, then tenant_id.
    """
    if dataset_id and "dataset_id" in col_names:
        return ' WHERE "dataset_id" = ?', (dataset_id,)
    if owner_user_id and "owner_user_id" in col_names:
        return ' WHERE "owner_user_id" = ?', (owner_user_id,)
    if tenant_id and "tenant_id" in col_names:
        return ' WHERE "tenant_id" = ?', (tenant_id,)
    return "", ()


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


COMPUTE_ENVELOPE_SCHEMA_VERSION = "2026-05-11"


def _compute_envelope(
    *,
    request_id: str,
    engine_instance_id: str,
    policy_hash: Optional[str],
    runtime_profile: Optional[Dict[str, Any]] = None,
    status: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "schema_version": COMPUTE_ENVELOPE_SCHEMA_VERSION,
        "request_id": request_id,
        "engine_instance_id": engine_instance_id,
        "policy_hash": policy_hash,
        "runtime_profile": runtime_profile or {"id": resolve_runtime_profile()},
        "result": result,
    }
    out: Dict[str, Any] = {"id": request_id, "status": status, "payload": payload}
    if error:
        out["error"] = error
    if error_code:
        out["error_code"] = error_code
    return out


def _operation_to_msg_type(operation: str) -> str:
    mapping = {
        "healthcheck": "healthcheck",
        "llm_generation": "llm_generation",
        "ollama_list_models": "ollama_list_models",
        "sanitization.run": "llm_generation",
        "filter_lab.list_job_groups": "list_filter_lab_job_groups",
        "filter_lab.run": "post_filter_lab_job_group",
        "filter_lab.create_job_group": "post_filter_lab_job_group",
    }
    return mapping.get(operation, operation.strip().lower().replace(".", "_"))


_POOLED_SCOPE_COLUMNS = ("dataset_id", "owner_user_id", "tenant_id")


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
    _ensure_pooled_scope_journal_table(conn)
    tables_applied: List[Dict[str, Any]] = []
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
    conn.commit()
    return {
        "migration_id": migration_id,
        "created_at": created_at,
        "tables_applied": tables_applied,
    }


def _pooled_scope_backfill_rollback(conn: Any, migration_id: str) -> Dict[str, Any]:
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
    restored: List[Dict[str, Any]] = []
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
    conn.commit()
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


def _query_messages_per_day_db(
    conn, dataset_id: Optional[str], table_name: str, source_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Query messages per day from database table."""
    try:
        # Check if table exists
        if not _table_exists(conn, table_name):
            logger.debug("Table %s does not exist, returning empty result", table_name)
            return []
        
        # For messages table, if dataset_id is provided but doesn't match any rows,
        # fall back to returning all messages (more lenient for local mode)
        if table_name == "messages" and dataset_id:
            # First check if any rows match this dataset_id
            cursor = conn.execute("SELECT COUNT(*) FROM messages WHERE dataset_id = ?", (dataset_id,))
            matching_count = cursor.fetchone()[0]
            if matching_count == 0:
                logger.debug("No messages found for dataset_id=%s, falling back to all messages", dataset_id)
                dataset_id = None  # Fall back to no filter
        
        if table_name == "ai_chat_messages":
            # Check if conversations table exists for join
            has_conversations_table = _table_exists(conn, "ai_chat_conversations")
            
            if has_conversations_table:
                # Join with conversations table to filter by owner_user_id
                query = """
                    SELECT DATE(m.event_at) as day, COUNT(*) as message_count 
                    FROM ai_chat_messages m
                    LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                """
                conditions = []
                params = []
                if dataset_id:
                    # Extract user_id from dataset_id (format: user_id:dataset_name)
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    conditions.append("c.owner_user_id = ?")
                    params.append(user_id)
                if source_filter:
                    conditions.append("m.source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " GROUP BY day ORDER BY day DESC"
            else:
                # No conversations table, query messages directly (no user filtering)
                query = "SELECT DATE(event_at) as day, COUNT(*) as message_count FROM ai_chat_messages"
                conditions = []
                params = []
                if source_filter:
                    conditions.append("source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " GROUP BY day ORDER BY day DESC"
        else:
            query = f"SELECT DATE(event_at) as day, COUNT(*) as message_count FROM {table_name}"
            conditions = []
            params = []
            if dataset_id:
                conditions.append("dataset_id = ?")
                params.append(dataset_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " GROUP BY day ORDER BY day DESC"
        cursor = conn.execute(query, params)
        return [{"day": row[0], "message_count": row[1]} for row in cursor.fetchall()]
    except Exception as e:
        logger.warning("Failed to query messages_per_day from %s: %s", table_name, e)
        return []


def _query_total_messages_db(
    conn, dataset_id: Optional[str], table_name: str, source_filter: Optional[str] = None
) -> Dict[str, Any]:
    """Query total messages from database table."""
    try:
        # Check if table exists
        if not _table_exists(conn, table_name):
            logger.debug("Table %s does not exist, returning 0", table_name)
            return {"total_messages": 0}
        
        # For messages table, if dataset_id is provided but doesn't match any rows,
        # fall back to returning all messages (more lenient for local mode)
        if table_name == "messages" and dataset_id:
            cursor = conn.execute("SELECT COUNT(*) FROM messages WHERE dataset_id = ?", (dataset_id,))
            matching_count = cursor.fetchone()[0]
            if matching_count == 0:
                logger.debug("No messages found for dataset_id=%s, falling back to all messages", dataset_id)
                dataset_id = None  # Fall back to no filter
        
        if table_name == "ai_chat_messages":
            # Check if conversations table exists for join
            has_conversations_table = _table_exists(conn, "ai_chat_conversations")
            
            if has_conversations_table:
                # Join with conversations table to filter by owner_user_id
                query = """
                    SELECT COUNT(*) as total_messages 
                    FROM ai_chat_messages m
                    LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                """
                conditions = []
                params = []
                if dataset_id:
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    conditions.append("c.owner_user_id = ?")
                    params.append(user_id)
                if source_filter:
                    conditions.append("m.source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
            else:
                # No conversations table, query messages directly
                query = "SELECT COUNT(*) as total_messages FROM ai_chat_messages"
                conditions = []
                params = []
                if source_filter:
                    conditions.append("source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
        else:
            query = f"SELECT COUNT(*) as total_messages FROM {table_name}"
            conditions = []
            params = []
            if dataset_id:
                conditions.append("dataset_id = ?")
                params.append(dataset_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
        cursor = conn.execute(query, params)
        row = cursor.fetchone()
        return {"total_messages": row[0] if row else 0}
    except Exception as e:
        logger.warning("Failed to query total_messages from %s: %s", table_name, e)
        return {"total_messages": 0}


def _query_avg_message_length_db(
    conn, dataset_id: Optional[str], table_name: str, source_filter: Optional[str] = None
) -> Dict[str, Any]:
    """Query average message length from database table."""
    try:
        # Check if table exists
        if not _table_exists(conn, table_name):
            logger.debug("Table %s does not exist, returning 0", table_name)
            return {"avg_length": 0.0, "min_length": 0, "max_length": 0}
        
        # For messages table, if dataset_id is provided but doesn't match any rows,
        # fall back to returning all messages (more lenient for local mode)
        if table_name == "messages" and dataset_id:
            cursor = conn.execute("SELECT COUNT(*) FROM messages WHERE dataset_id = ?", (dataset_id,))
            matching_count = cursor.fetchone()[0]
            if matching_count == 0:
                logger.debug("No messages found for dataset_id=%s, falling back to all messages", dataset_id)
                dataset_id = None  # Fall back to no filter
        
        if table_name == "ai_chat_messages":
            # Check if conversations table exists for join
            has_conversations_table = _table_exists(conn, "ai_chat_conversations")
            
            if has_conversations_table:
                # Join with conversations table to filter by owner_user_id
                query = """
                    SELECT AVG(LENGTH(m.content)) as avg_length, 
                           MIN(LENGTH(m.content)) as min_length, 
                           MAX(LENGTH(m.content)) as max_length 
                    FROM ai_chat_messages m
                    LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                """
                conditions = []
                params = []
                if dataset_id:
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    conditions.append("c.owner_user_id = ?")
                    params.append(user_id)
                if source_filter:
                    conditions.append("m.source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
            else:
                # No conversations table, query messages directly
                query = """
                    SELECT AVG(LENGTH(content)) as avg_length, 
                           MIN(LENGTH(content)) as min_length, 
                           MAX(LENGTH(content)) as max_length 
                    FROM ai_chat_messages
                """
                conditions = []
                params = []
                if source_filter:
                    conditions.append("source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
        else:
            query = f"SELECT AVG(LENGTH(content)) as avg_length, MIN(LENGTH(content)) as min_length, MAX(LENGTH(content)) as max_length FROM {table_name}"
            conditions = []
            params = []
            if dataset_id:
                conditions.append("dataset_id = ?")
                params.append(dataset_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
        cursor = conn.execute(query, params)
        row = cursor.fetchone()
        if row and row[0] is not None:
            return {
                "avg_length": float(row[0]),
                "min_length": int(row[1] or 0),
                "max_length": int(row[2] or 0),
            }
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}
    except Exception as e:
        logger.warning("Failed to query avg_message_length from %s: %s", table_name, e)
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}


def _query_messages_by_sender_db(
    conn, dataset_id: Optional[str], table_name: str, source_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Query messages by sender from database table."""
    try:
        # Check if table exists
        if not _table_exists(conn, table_name):
            logger.debug("Table %s does not exist, returning empty result", table_name)
            return []
        
        # For messages table, if dataset_id is provided but doesn't match any rows,
        # fall back to returning all messages (more lenient for local mode)
        if table_name == "messages" and dataset_id:
            cursor = conn.execute("SELECT COUNT(*) FROM messages WHERE dataset_id = ?", (dataset_id,))
            matching_count = cursor.fetchone()[0]
            if matching_count == 0:
                logger.debug("No messages found for dataset_id=%s, falling back to all messages", dataset_id)
                dataset_id = None  # Fall back to no filter
        
        if table_name == "ai_chat_messages":
            # Check if conversations table exists for join
            has_conversations_table = _table_exists(conn, "ai_chat_conversations")
            
            if has_conversations_table:
                # Join with conversations table to filter by owner_user_id
                query = """
                    SELECT m.sender_type, COUNT(*) as count 
                    FROM ai_chat_messages m
                    LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                """
                conditions = []
                params = []
                if dataset_id:
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    conditions.append("c.owner_user_id = ?")
                    params.append(user_id)
                if source_filter:
                    conditions.append("m.source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " GROUP BY m.sender_type ORDER BY count DESC"
            else:
                # No conversations table, query messages directly
                query = "SELECT sender_type, COUNT(*) as count FROM ai_chat_messages"
                conditions = []
                params = []
                if source_filter:
                    conditions.append("source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " GROUP BY sender_type ORDER BY count DESC"
        else:
            query = f"SELECT sender_type, COUNT(*) as count FROM {table_name}"
            conditions = []
            params = []
            if dataset_id:
                conditions.append("dataset_id = ?")
                params.append(dataset_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " GROUP BY sender_type ORDER BY count DESC"
        cursor = conn.execute(query, params)
        return [{"sender_type": row[0], "count": row[1]} for row in cursor.fetchall()]
    except Exception as e:
        logger.warning("Failed to query messages_by_sender from %s: %s", table_name, e)
        return []


def _build_messenger_contact_graph(
    conn,
    *,
    dataset_id: str,
    source_ids: Optional[List[str]] = None,
    max_messages: int = 25000,
    max_nodes: int = 40,
    include_broadcast_edges: bool = True,
) -> Dict[str, Any]:
    """Build lightweight people-interaction graph for messenger verification."""
    src_filter = source_ids or ["imessage", "signal"]
    placeholders = ",".join("?" for _ in src_filter)
    params: List[Any] = [dataset_id, *src_filter, int(max_messages)]
    rows = conn.execute(
        f"""
        SELECT message_id, conversation_id, sender_id, reply_to_message_id, source_id, event_at
        FROM conversation_messages
        WHERE dataset_id = ?
          AND source_id IN ({placeholders})
        ORDER BY event_at ASC
        LIMIT ?
        """,
        params,
    ).fetchall()

    # display name lookup by normalized identifier (prefer first non-empty name)
    name_rows = conn.execute(
        f"""
        SELECT ci.identifier, c.display_name
        FROM contact_identifiers ci
        JOIN contacts c
          ON c.contact_id = ci.contact_id
         AND c.dataset_id = ci.dataset_id
        WHERE ci.dataset_id = ?
          AND ci.source_id IN ({placeholders}, '*')
          AND c.display_name IS NOT NULL
          AND c.display_name != ''
        """,
        [dataset_id, *src_filter],
    ).fetchall()
    display_by_norm: Dict[str, str] = {}
    for identifier, display_name in name_rows:
        nk = _normalize_contact_key(identifier)
        if nk and nk not in display_by_norm and display_name:
            display_by_norm[nk] = str(display_name)

    msg_sender: Dict[str, str] = {}
    conversation_participants: Dict[str, set[str]] = {}
    conversation_rows: Dict[str, List[tuple[str, str, Optional[str]]]] = {}
    source_counts: Dict[str, int] = {}
    for message_id, conversation_id, sender_id, reply_to_message_id, source_id, _ts in rows:
        sid = _normalize_contact_key(sender_id)
        if not sid:
            continue
        mid = str(message_id or "").strip()
        cid = str(conversation_id or "").strip()
        if mid:
            msg_sender[mid] = sid
        if cid:
            conversation_participants.setdefault(cid, set()).add(sid)
            conversation_rows.setdefault(cid, []).append((mid, sid, str(reply_to_message_id or "").strip() or None))
        src = str(source_id or "")
        source_counts[src] = int(source_counts.get(src, 0)) + 1

    edge_weights: Dict[tuple[str, str, str], float] = {}

    def _add_edge(a: str, b: str, kind: str, weight: float) -> None:
        if not a or not b or a == b:
            return
        key = (a, b, kind)
        edge_weights[key] = float(edge_weights.get(key, 0.0)) + float(weight)

    for cid, participants in conversation_participants.items():
        plist = sorted(participants)
        n = len(plist)
        if n < 2:
            continue
        convo_msgs = conversation_rows.get(cid) or []
        if n == 2:
            # Two-party chat: one undirected relationship edge weighted by message volume.
            a, b = plist[0], plist[1]
            _add_edge(a, b, "pair_dialog", float(len(convo_msgs) or 1))
            _add_edge(b, a, "pair_dialog", float(len(convo_msgs) or 1))
            continue

        # Group chat: reply edges + optional broadcast-to-group heuristic edges.
        for mid, sender, reply_to in convo_msgs:
            if reply_to:
                target = msg_sender.get(reply_to)
                if target and target != sender:
                    _add_edge(sender, target, "reply", 1.0)
                    continue
            if include_broadcast_edges:
                others = [p for p in plist if p != sender]
                if others:
                    w = 1.0 / float(len(others))
                    for target in others:
                        _add_edge(sender, target, "broadcast", w)

    degree: Dict[str, float] = {}
    for (a, b, _kind), weight in edge_weights.items():
        degree[a] = float(degree.get(a, 0.0)) + weight
        degree[b] = float(degree.get(b, 0.0)) + weight

    ranked_nodes = sorted(degree.items(), key=lambda x: x[1], reverse=True)
    keep_nodes = {n for n, _ in ranked_nodes[: max(5, int(max_nodes))]}
    filtered_edges = []
    for (a, b, kind), weight in edge_weights.items():
        if a in keep_nodes and b in keep_nodes and weight > 0:
            filtered_edges.append({"from": a, "to": b, "kind": kind, "weight": round(weight, 3)})
    filtered_edges.sort(key=lambda e: e["weight"], reverse=True)
    filtered_edges = filtered_edges[:500]

    nodes = []
    for n, deg in ranked_nodes:
        if n in keep_nodes:
            nodes.append(
                {
                    "id": n,
                    "label": display_by_norm.get(n) or n,
                    "identifier": n,
                    "degree": round(float(deg), 3),
                    "is_self": n == "self",
                }
            )
    nodes.sort(key=lambda x: x["degree"], reverse=True)

    return {
        "dataset_id": dataset_id,
        "sources": src_filter,
        "messages_considered": len(rows),
        "conversations_considered": len(conversation_participants),
        "source_message_counts": source_counts,
        "nodes": nodes,
        "edges": filtered_edges,
    }


def _query_combined_messages_per_day(conn, dataset_id: Optional[str]) -> List[Dict[str, Any]]:
    """Query combined messages per day from all sources."""
    try:
        # Union messages from messages table and ai_chat_messages table
        query = """
            SELECT DATE(event_at) as day, COUNT(*) as message_count FROM (
                SELECT event_at FROM messages
                UNION ALL
                SELECT event_at FROM ai_chat_messages
            )
        """
        # Note: dataset_id filtering is complex for combined queries, so we'll get all messages
        # In a production system, you'd want to properly join with conversations table
        query += " GROUP BY day ORDER BY day DESC"
        cursor = conn.execute(query)
        return [{"day": row[0], "message_count": row[1]} for row in cursor.fetchall()]
    except Exception as e:
        logger.warning("Failed to query combined_messages_per_day: %s", e)
        return []


def _query_combined_total_messages(conn, dataset_id: Optional[str]) -> Dict[str, Any]:
    """Query combined total messages from all sources."""
    try:
        query = """
            SELECT COUNT(*) as total_messages FROM (
                SELECT message_id FROM messages
                UNION ALL
                SELECT message_id FROM ai_chat_messages
            )
        """
        cursor = conn.execute(query)
        row = cursor.fetchone()
        total = row[0] if row else 0
        
        # Also get breakdown by source
        demo_count = _query_total_messages_db(conn, dataset_id, "messages").get("total_messages", 0)
        chatgpt_count = _query_total_messages_db(conn, dataset_id, "ai_chat_messages", "chatgpt").get("total_messages", 0)
        if chatgpt_count == 0:
            chatgpt_count = _query_total_messages_db(conn, dataset_id, "chatgpt_messages").get("total_messages", 0)
        
        return {
            "total_messages": total,
            "demo_messages": demo_count,
            "chatgpt_messages": chatgpt_count,
            "jsonl_messages": 0,  # Would need to query raw files
        }
    except Exception as e:
        logger.warning("Failed to query combined_total_messages: %s", e)
        return {"total_messages": 0, "demo_messages": 0, "chatgpt_messages": 0, "jsonl_messages": 0}


def _query_combined_avg_message_length(conn, dataset_id: Optional[str]) -> Dict[str, Any]:
    """Query combined average message length from all sources."""
    try:
        query = """
            SELECT AVG(LENGTH(content)) as avg_length, MIN(LENGTH(content)) as min_length, MAX(LENGTH(content)) as max_length FROM (
                SELECT content FROM messages
                UNION ALL
                SELECT content FROM ai_chat_messages
            )
        """
        cursor = conn.execute(query)
        row = cursor.fetchone()
        if row and row[0] is not None:
            return {
                "avg_length": float(row[0]),
                "min_length": int(row[1] or 0),
                "max_length": int(row[2] or 0),
            }
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}
    except Exception as e:
        logger.warning("Failed to query combined_avg_message_length: %s", e)
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}


def _query_combined_messages_by_sender(conn, dataset_id: Optional[str]) -> List[Dict[str, Any]]:
    """Query combined messages by sender from all sources."""
    try:
        query = """
            SELECT sender_type, COUNT(*) as count FROM (
                SELECT sender_type FROM messages
                UNION ALL
                SELECT sender_type FROM ai_chat_messages
            )
            GROUP BY sender_type ORDER BY count DESC
        """
        cursor = conn.execute(query)
        return [{"sender_type": row[0], "count": row[1]} for row in cursor.fetchall()]
    except Exception as e:
        logger.warning("Failed to query combined_messages_by_sender: %s", e)
        return []


async def handle_control_plane_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    msg_type = str(message.get("type") or "").strip().lower()
    _payload = message.get("payload") or {}
    _mcp_source = _payload.get("mcp_source")
    _mcp_requester_id = _payload.get("mcp_requester_id")
    if msg_type == "migrate_browser_plugin_app_id":
        from .state import _migrate_legacy_browser_plugin_app_ids

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        updated = _migrate_legacy_browser_plugin_app_ids(conn)
        return {"id": req_id, "status": "ok", "payload": {"updated_rows": updated}}

    if msg_type == "get_request_counts":
        """Return UMA + MCP request counts from engine DB (for CP proxy or direct frontend)."""
        payload = message.get("payload") or {}
        owner_user_id = (payload.get("owner_user_id") or "").strip()
        since_days = min(max(int(payload.get("since_days") or 90), 1), 365)
        conn = get_db_connection()
        if not owner_user_id and conn:
            owner_user_id = get_user_id(conn) or ""
        uma = get_uma_request_counts(conn, owner_user_id, since_days) if conn else {"total_read_requests": 0, "total_write_requests": 0, "by_app": []}
        mcp = get_mcp_request_counts(conn, since_days) if conn else {"by_source": [], "by_tool": [], "total": 0}
        return {"id": req_id, "status": "ok", "payload": {"uma": uma, "mcp": mcp}}

    if msg_type == "get_ui_config":
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        raw = get_engine_config_value(conn, UI_CONFIG_KEY)
        if not raw:
            return {"id": req_id, "status": "ok", "payload": _default_ui_config()}
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        return {"id": req_id, "status": "ok", "payload": _normalize_ui_config(parsed)}

    if msg_type == "put_ui_config":
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        payload = message.get("payload") or {}
        normalized = _normalize_ui_config(payload.get("ui_config"))
        try:
            set_engine_config_value(conn, UI_CONFIG_KEY, json.dumps(normalized))
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}
        return {"id": req_id, "status": "ok", "payload": normalized}

    if msg_type == "get_data_explorer_table_prefs":
        from ..data_explorer_table_prefs import get_table_prefs as load_table_prefs

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        payload = message.get("payload") or {}
        user_id = str(payload.get("user_id") or "").strip()
        table_name = str(payload.get("table_name") or "").strip()
        if not user_id or not table_name:
            return {"id": req_id, "status": "error", "error": "user_id and table_name required"}
        try:
            prefs = load_table_prefs(conn, user_id=user_id, table_name=table_name)
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        return {"id": req_id, "status": "ok", "payload": {"prefs": prefs}}

    if msg_type == "put_data_explorer_table_prefs":
        from ..data_explorer_table_prefs import put_table_prefs as save_table_prefs

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        payload = message.get("payload") or {}
        user_id = str(payload.get("user_id") or "").strip()
        table_name = str(payload.get("table_name") or "").strip()
        prefs = payload.get("prefs") or {}
        if not user_id or not table_name:
            return {"id": req_id, "status": "error", "error": "user_id and table_name required"}
        try:
            saved = save_table_prefs(conn, user_id=user_id, table_name=table_name, prefs=prefs)
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        return {"id": req_id, "status": "ok", "payload": {"prefs": saved}}

    if msg_type == "delete_data_explorer_table_prefs":
        from ..data_explorer_table_prefs import delete_table_prefs as remove_table_prefs

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        payload = message.get("payload") or {}
        user_id = str(payload.get("user_id") or "").strip()
        table_name = str(payload.get("table_name") or "").strip()
        if not user_id or not table_name:
            return {"id": req_id, "status": "error", "error": "user_id and table_name required"}
        try:
            deleted = remove_table_prefs(conn, user_id=user_id, table_name=table_name)
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        return {"id": req_id, "status": "ok", "payload": {"deleted": deleted}}

    if msg_type == "get_user_identity":
        from ..storage.user_identity import get_user_identity as load_user_identity

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        payload = message.get("payload") or {}
        dataset_id = str(payload.get("dataset_id") or "").strip()
        if not dataset_id:
            return {"id": req_id, "status": "error", "error": "dataset_id required"}
        identity = load_user_identity(conn, dataset_id)
        if identity is None:
            body: Dict[str, Any] = {"status": "ok", "dataset_id": dataset_id, "display_name": None}
        else:
            body = {"status": "ok", "dataset_id": dataset_id, **identity}
        return {"id": req_id, "status": "ok", "payload": body}

    if msg_type == "put_user_identity":
        from ..storage.user_identity import put_user_identity as persist_user_identity

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        payload = message.get("payload") or {}
        dataset_id = str(payload.get("dataset_id") or "").strip()
        if not dataset_id:
            return {"id": req_id, "status": "error", "error": "dataset_id required"}
        raw_dn = payload.get("display_name")
        if isinstance(raw_dn, str):
            next_display_name = raw_dn.strip() or None
        else:
            next_display_name = None
        persist_user_identity(conn, dataset_id, display_name=next_display_name)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"status": "ok", "dataset_id": dataset_id, "display_name": next_display_name},
        }

    if msg_type == "post_source_install":
        from ..api.source_install import _install_source_core

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        try:
            result = await _install_source_core(payload)
            return {"id": req_id, "status": "ok", "payload": result}
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except RuntimeError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "get_source_install_status":
        from ..api.source_install import _list_install_status_core

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        try:
            result = await _list_install_status_core(payload)
            return {"id": req_id, "status": "ok", "payload": result}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "delete_source_install":
        from ..api.source_install import _uninstall_source_core

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        try:
            result = await _uninstall_source_core(payload)
            return {"id": req_id, "status": "ok", "payload": result}
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except RuntimeError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "post_source_test_ingestion":
        from ..api.source_install import _test_ingestion_core

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        try:
            result = await _test_ingestion_core(payload)
            return {"id": req_id, "status": "ok", "payload": result}
        except (ValueError, LookupError) as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type in {"post_source_test_enrichment", "post_source_test_enrichment_trigger"}:
        from ..api.source_install import _test_enrichment_core

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        try:
            result = await _test_enrichment_core(payload)
            return {"id": req_id, "status": "ok", "payload": result}
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "get_sanitization_ollama_config":
        from ..config.sanitization_ollama import effective_config_for_api

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        try:
            data = effective_config_for_api(settings, conn)
            return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "put_sanitization_ollama_config":
        from ..config.sanitization_ollama import (
            ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE,
            effective_config_for_api,
            normalize_put_device_overrides,
        )

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        payload = message.get("payload") or {}
        try:
            json_str = normalize_put_device_overrides(payload)
            set_engine_config_value(conn, ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE, json_str)
            data = effective_config_for_api(settings, conn)
            return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "delete_sanitization_ollama_config":
        from ..config.sanitization_ollama import (
            ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE,
            effective_config_for_api,
        )

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        try:
            set_engine_config_value(conn, ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE, "{}")
            data = effective_config_for_api(settings, conn)
            return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "get_filter_lab_bundles":
        from ..filter_lab import bundles as fl_bundles

        return {"id": req_id, "status": "ok", "payload": fl_bundles.list_bundle_metadata()}

    if msg_type == "get_filter_lab_bundle_detail":
        from ..filter_lab import bundles as fl_bundles

        bid = str((message.get("payload") or {}).get("bundle_id") or "").strip()
        if not bid:
            return {"id": req_id, "status": "error", "error": "bundle_id required"}
        data = fl_bundles.get_bundle_preview(bid)
        if not data:
            return {"id": req_id, "status": "error", "error": "Bundle not found"}
        return {"id": req_id, "status": "ok", "payload": data}

    if msg_type == "post_filter_lab_job_group":
        from ..filter_lab import service as fl_service

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        payload = message.get("payload") or {}
        try:
            gid = fl_service.create_job_group(
                filter_id=str(payload.get("filter_id") or "").strip(),
                bundle_id=str(payload.get("bundle_id") or "").strip(),
                models=list(payload.get("models") or []),
                options=payload.get("options") if isinstance(payload.get("options"), dict) else None,
            )
            data = fl_service.serialize_job_group(conn, gid)
            return {"id": req_id, "status": "ok", "payload": data}
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except RuntimeError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "get_filter_lab_job_group_detail":
        from ..filter_lab import service as fl_service

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        group_id = str((message.get("payload") or {}).get("group_id") or "").strip()
        if not group_id:
            return {"id": req_id, "status": "error", "error": "group_id required"}
        try:
            data = fl_service.serialize_job_group(conn, group_id)
            return {"id": req_id, "status": "ok", "payload": data}
        except KeyError:
            return {"id": req_id, "status": "error", "error": "Job group not found"}

    if msg_type == "list_filter_lab_job_groups":
        from ..filter_lab import service as fl_service
        from ..filter_lab import store as fl_store

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        pl = message.get("payload") or {}
        fid = str(pl.get("filter_id") or "").strip()
        limit = min(max(int(pl.get("limit") or 20), 1), 100)
        offset = max(int(pl.get("offset") or 0), 0)
        fl_store.prune_old_groups(conn, max_age_days=30)
        if fid:
            rows = fl_store.list_groups_for_filter(conn, fid, limit=limit, offset=offset)
        else:
            rows = fl_store.list_all_job_groups(conn, limit=limit, offset=offset)
        groups = []
        for row in rows:
            g = dict(row)
            g["baseline_models"] = json.loads(g.pop("baseline_models_json") or "[]")
            g["pulled_models"] = json.loads(g.pop("pulled_models_json") or "[]")
            opt_raw = g.pop("options_json", "{}")
            try:
                g["options"] = json.loads(opt_raw) if isinstance(opt_raw, str) else {}
            except json.JSONDecodeError:
                g["options"] = {}
            groups.append(g)
        fl_service.enrich_job_groups_list_with_run_summaries(conn, groups)
        return {"id": req_id, "status": "ok", "payload": {"groups": groups, "limit": limit, "offset": offset}}

    if msg_type == "patch_filter_lab_job_group":
        from ..filter_lab import service as fl_service
        from ..filter_lab import store as fl_store

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        pl = message.get("payload") or {}
        group_id = str(pl.get("group_id") or "").strip()
        body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
        if not group_id:
            return {"id": req_id, "status": "error", "error": "group_id required"}
        if not fl_store.get_group(conn, group_id):
            return {"id": req_id, "status": "error", "error": "Job group not found"}
        if "preferred_model_tag" in body:
            p = body.get("preferred_model_tag")
            fl_store.patch_group(conn, group_id, preferred_model_tag=p if p is None else str(p).strip() or None)
        if "group_notes" in body:
            fl_store.patch_group(conn, group_id, group_notes=body.get("group_notes"))
        if "notes" in body:
            fl_store.patch_group(conn, group_id, notes=body.get("notes"))
        try:
            data = fl_service.serialize_job_group(conn, group_id)
            return {"id": req_id, "status": "ok", "payload": data}
        except KeyError:
            return {"id": req_id, "status": "error", "error": "Job group not found"}

    if msg_type == "patch_filter_lab_job_run":
        from ..filter_lab import service as fl_service
        from ..filter_lab import store as fl_store

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        pl = message.get("payload") or {}
        group_id = str(pl.get("group_id") or "").strip()
        run_id = str(pl.get("run_id") or "").strip()
        body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
        if not group_id or not run_id:
            return {"id": req_id, "status": "error", "error": "group_id and run_id required"}
        runs = fl_store.list_runs(conn, group_id)
        if not any(dict(r)["id"] == run_id for r in runs):
            return {"id": req_id, "status": "error", "error": "Run not found"}
        rated = False
        if "user_quality_score_0_10" in body:
            v = body.get("user_quality_score_0_10")
            if v is not None and (not isinstance(v, int) or v < 0 or v > 10):
                return {"id": req_id, "status": "error", "error": "user_quality_score_0_10 must be 0–10 or null"}
            fl_store.patch_run(conn, run_id, user_quality_score_0_10=v)
            rated = True
        if "user_liked" in body:
            v = body.get("user_liked")
            if v is None:
                conn.execute("UPDATE filter_lab_run SET user_liked = NULL WHERE id = ?", (run_id,))
                conn.commit()
            else:
                fl_store.patch_run(conn, run_id, user_liked=bool(v))
            rated = True
        if "user_note" in body:
            note = body.get("user_note")
            fl_store.patch_run(conn, run_id, user_note=None if note is None else str(note)[:2000])
            rated = True
        if rated:
            conn.execute(
                "UPDATE filter_lab_run SET rated_at = ? WHERE id = ?",
                (fl_store.utc_now_iso(), run_id),
            )
            conn.commit()
        try:
            data = fl_service.serialize_job_group(conn, group_id)
            return {"id": req_id, "status": "ok", "payload": data}
        except KeyError:
            return {"id": req_id, "status": "error", "error": "Job group not found"}

    if msg_type == "post_filter_lab_apply_preferred":
        from ..filter_lab import service as fl_service

        group_id = str((message.get("payload") or {}).get("group_id") or "").strip()
        if not group_id:
            return {"id": req_id, "status": "error", "error": "group_id required"}
        try:
            data = fl_service.apply_preferred_model(group_id)
            return {"id": req_id, "status": "ok", "payload": data}
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except RuntimeError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "delete_filter_lab_job_group":
        from ..filter_lab import store as fl_store

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        group_id = str((message.get("payload") or {}).get("group_id") or "").strip()
        if not group_id:
            return {"id": req_id, "status": "error", "error": "group_id required"}
        if not fl_store.get_group(conn, group_id):
            return {"id": req_id, "status": "error", "error": "Job group not found"}
        fl_store.delete_group(conn, group_id)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "group_id": group_id, "deleted": True}}

    if msg_type == "delete_filter_lab_all_data":
        from ..filter_lab import store as fl_store

        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        fl_store.ensure_schema(conn)
        conn.execute("DELETE FROM filter_lab_model_event")
        conn.execute("DELETE FROM filter_lab_run")
        conn.execute("DELETE FROM filter_lab_job_group")
        conn.commit()
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "cleared": True}}

    if msg_type == "connection_info":
        """
        Handle connection_info message from control plane with user_id from auth.
        
        Linking Strategy:
        - If engine has NO user_id: Store auth user_id (engine started, waiting for user)
        - If engine HAS user_id that matches: Log confirmation (already linked)
        - If engine HAS user_id that differs: Warn but keep existing (engine started independently,
          may have data encrypted with that user_id - requires explicit pairing to change)
        """
        payload = message.get("payload") or {}
        auth_user_id = payload.get("user_id") or message.get("user_id")
        if auth_user_id:
            conn = get_db_connection()
            if conn:
                existing_user_id = get_user_id(conn)
                
                if not existing_user_id:
                    # Engine started but no user_id yet - link to authenticated user
                    store_user_id(conn, auth_user_id)
                    logger.info(
                        "[PIPELINE:CONNECTION] Engine had no user_id. Stored auth user_id=%s from control plane. "
                        "Engine is now linked to authenticated user.",
                        auth_user_id[:8] if auth_user_id else None,
                    )
                elif existing_user_id == auth_user_id:
                    # Already linked - perfect match
                    logger.debug(
                        "[PIPELINE:CONNECTION] Engine user_id=%s matches auth user_id from control plane (already linked)",
                        existing_user_id[:8] if existing_user_id else None,
                    )
                else:
                    # Mismatch: Engine started independently with its own user_id
                    # Don't replace automatically - engine may have data encrypted with existing user_id
                    logger.warning(
                        "[PIPELINE:CONNECTION] Engine has independent user_id=%s but control plane provided auth user_id=%s. "
                        "Keeping existing engine user_id to preserve data integrity. "
                        "If you want to link them, use device pairing (this will migrate/re-encrypt data).",
                        existing_user_id[:8] if existing_user_id else None,
                        auth_user_id[:8] if auth_user_id else None,
                    )
            # Note: We don't update settings.topos_user_id here because:
            # 1. The real source of truth is the database (engine_config table)
            # 2. settings.topos_user_id is read from environment variable at startup
            # 3. The database value takes precedence for all operations
        # Only send a response if CP sent an id (so it can match); unsolicited connection_info has no id
        connection_info_id = message.get("id")
        if connection_info_id is not None:
            return {"id": connection_info_id, "status": "ok"}
        return None
    if not req_id:
        # No id to match; don't send a response the Control Plane would drop
        return None
    if msg_type == "healthcheck":
        return {"id": req_id, "status": "ok", "payload": {"status": "ok"}}
    if msg_type == "compute_invoke":
        payload = message.get("payload") or {}
        invocation_token = str(payload.get("invocation_token") or "")
        resource_id = str(payload.get("resource_id") or "")
        operation = str(payload.get("operation") or "")
        request_id = str(message.get("id") or payload.get("request_id") or "")
        policy_hash = payload.get("policy_hash")
        engine_instance_id = str(payload.get("engine_instance_id") or settings.engine_name or "engine_local")
        runtime_profile_id = resolve_runtime_profile()
        allowed_operations = RUNTIME_PROFILE_OPERATIONS.get(runtime_profile_id, [])
        runtime_profile = {
            "id": runtime_profile_id,
            "allowed_operations": allowed_operations,
        }
        if not request_id:
            return None
        try:
            _ = validate_scoped_invocation_token(
                token=invocation_token,
                secret=str(settings.topos_key or ""),
                resource_id=resource_id,
                operation=operation,
                request_id=request_id,
            )
        except ScopedTokenValidationError as exc:
            return _compute_envelope(
                request_id=request_id,
                engine_instance_id=engine_instance_id,
                policy_hash=str(policy_hash) if policy_hash else None,
                runtime_profile=runtime_profile,
                status="error",
                error=exc.message,
                error_code=exc.code,
            )
        if allowed_operations and operation not in allowed_operations:
            return _compute_envelope(
                request_id=request_id,
                engine_instance_id=engine_instance_id,
                policy_hash=str(policy_hash) if policy_hash else None,
                runtime_profile=runtime_profile,
                status="error",
                error=f"operation '{operation}' is not allowed for runtime profile '{runtime_profile_id}'",
                error_code="PROFILE_CAPABILITY_DENIED",
            )
        target_msg_type = _operation_to_msg_type(operation)
        if target_msg_type == "compute_invoke":
            return _compute_envelope(
                request_id=request_id,
                engine_instance_id=engine_instance_id,
                policy_hash=str(policy_hash) if policy_hash else None,
                runtime_profile=runtime_profile,
                status="error",
                error="operation cannot target compute_invoke",
                error_code="OPERATION_NOT_ALLOWED",
            )
        forwarded_message = {
            "id": request_id,
            "type": target_msg_type,
            "payload": payload.get("input") if isinstance(payload.get("input"), dict) else {},
        }
        try:
            forwarded = await handle_control_plane_request(forwarded_message)
        except Exception as exc:  # noqa: BLE001
            return _compute_envelope(
                request_id=request_id,
                engine_instance_id=engine_instance_id,
                policy_hash=str(policy_hash) if policy_hash else None,
                runtime_profile=runtime_profile,
                status="error",
                error=str(exc),
                error_code="COMPUTE_EXECUTION_FAILED",
            )
        if not forwarded:
            return _compute_envelope(
                request_id=request_id,
                engine_instance_id=engine_instance_id,
                policy_hash=str(policy_hash) if policy_hash else None,
                runtime_profile=runtime_profile,
                status="error",
                error="compute handler returned no result",
                error_code="COMPUTE_EXECUTION_FAILED",
            )
        if str(forwarded.get("status") or "").lower() == "ok":
            return _compute_envelope(
                request_id=request_id,
                engine_instance_id=engine_instance_id,
                policy_hash=str(policy_hash) if policy_hash else None,
                runtime_profile=runtime_profile,
                status="ok",
                result=forwarded.get("payload") if isinstance(forwarded.get("payload"), dict) else {"value": forwarded.get("payload")},
            )
        return _compute_envelope(
            request_id=request_id,
            engine_instance_id=engine_instance_id,
            policy_hash=str(policy_hash) if policy_hash else None,
            runtime_profile=runtime_profile,
            status="error",
            error=str(forwarded.get("error") or "compute invocation failed"),
            error_code=str(forwarded.get("error_code") or "COMPUTE_EXECUTION_FAILED"),
        )
    if msg_type == "llm_generation":
        payload = message.get("payload") or {}
        logger.info(
            "llm_generation request: provider=%r model=%r prompt_chars=%d",
            payload.get("provider"),
            payload.get("model"),
            len(str(payload.get("prompt") or "")),
        )
        try:
            result = await get_services().llm.generate(payload)
            return {"id": req_id, "status": "ok", "payload": result}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "ollama_list_models":
        try:
            result = await get_services().llm.list_ollama_models()
            return {"id": req_id, "status": "ok", "payload": result}
        except HTTPException as exc:
            detail = exc.detail
            err_msg = detail if isinstance(detail, str) else str(detail)
            return {
                "id": req_id,
                "status": "error",
                "error": err_msg,
                "error_code": exc.status_code,
            }
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "get_device_info":
        payload = message.get("payload") or {}
        context = payload if isinstance(payload, dict) else {}
        try:
            result = await get_services().device.get_device_info(context=context)
            return {"id": req_id, "status": "ok", "payload": result.model_dump()}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "set_device_name":
        payload = message.get("payload") or {}
        try:
            result = await get_services().device.set_device_name(payload.get("device_name", ""))
            return {"id": req_id, "status": "ok", "payload": result.model_dump()}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "store_message":
        payload = message.get("payload") or {}
        from ..ingestion.log_preview import field_preview

        content_preview = field_preview(payload.get("content"))
        
        # Derive dataset_id from user_id if not provided
        dataset_id = payload.get("dataset_id")
        if not dataset_id:
            # Get user_id from database
            conn = get_db_connection()
            if conn:
                user_id = get_user_id(conn)
                if not user_id:
                    # Create user_id if it doesn't exist
                    user_id = get_or_create_user_id(conn)
                if user_id:
                    # Use default dataset name
                    base_dataset_id = getattr(settings, 'default_dataset_id', 'default')
                    dataset_id = f"{user_id}:{base_dataset_id}"
                    logger.debug(
                        "[PIPELINE:ENTRY] store_message: derived dataset_id=%s from user_id=%s",
                        dataset_id,
                        user_id,
                    )
                else:
                    logger.warning("[PIPELINE:ENTRY] store_message: user_id not found in database, cannot derive dataset_id")
            else:
                logger.warning("[PIPELINE:ENTRY] store_message: database connection not available, cannot derive dataset_id")
        else:
            # If dataset_id is provided but doesn't have user_id prefix, try to add it
            if ":" not in dataset_id:
                conn = get_db_connection()
                if conn:
                    user_id = get_user_id(conn)
                    if not user_id:
                        user_id = get_or_create_user_id(conn)
                    if user_id:
                        dataset_id = f"{user_id}:{dataset_id}"
                        logger.debug(
                            "[PIPELINE:ENTRY] store_message: added user_id prefix to dataset_id: %s",
                            dataset_id,
                        )
        
        logger.debug(
            "[PIPELINE:ENTRY] store_message received: dataset_id=%s, sender_type=%s, content_preview=%s",
            dataset_id,
            payload.get("sender_type"),
            content_preview,
        )
        
        if not dataset_id:
            error_msg = "dataset_id required (could not derive from user_id)"
            logger.error("[PIPELINE:ERROR] store_message: %s", error_msg)
            return {"id": req_id, "status": "error", "error": error_msg}
        
        try:
            # Route through chatgpt_ui_conversation source (UI stream, automatic enrichment)
            # Use ingest_ui_payload directly with source_id to enable direct database processing
            from ..ingestion.ingest_helpers import ingest_ui_payload
            
            result = await ingest_ui_payload(
                dataset_id=dataset_id,
                schema_id="chatgpt.conversation.v1",
                payload=payload,
                job_id=payload.get("message_id"),
                source_id="chatgpt_ui_conversation",  # Enable direct processing without JSONL
            )
            if result.get("status") != "ok":
                logger.debug("[PIPELINE:ERROR] store_message failed: %s", result.get("error"))
                return {"id": req_id, "status": "error", "error": result.get("error", "ingestion failed")}
            logger.debug(
                "[PIPELINE:COMPLETE] store_message processed: job_id=%s, records_processed=%s",
                result.get("job_id"),
                result.get("records_processed"),
            )
            return {"id": req_id, "status": "ok", "payload": result}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PIPELINE:ERROR] store_message exception: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "app_ingest":
        # Sprint 4 US-4.4: Ingest records using UMA write permission (Control Plane forwards here).
        payload = message.get("payload") or {}
        user_id = payload.get("user_id")
        dataset_id = payload.get("dataset_id")
        source_id = payload.get("source_id") or "chatgpt_ui_conversation"
        schema_id = payload.get("schema_id") or "chatgpt.conversation.v1"
        records = payload.get("records") or []
        if not user_id or not dataset_id:
            return {"id": req_id, "status": "error", "error": "user_id and dataset_id required"}
        if not records:
            return {"id": req_id, "status": "error", "error": "records required and must be non-empty"}
        from ..ingestion.ingest_helpers import ingest_ui_payload
        processed = 0
        errors = []
        records_total = len(records)
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                err = "record must be a dict"
                errors.append({"index": i, "error": err})
                logger.warning(
                    "[PIPELINE:APP_INGEST] Record failed: source_id=%s index=%s error=%s",
                    source_id,
                    i,
                    err,
                )
                continue
            try:
                result = await ingest_ui_payload(
                    dataset_id=dataset_id,
                    schema_id=schema_id,
                    payload=rec,
                    source_id=source_id,
                )
                if result.get("status") == "ok":
                    processed += 1
                else:
                    err = result.get("error", "ingest failed")
                    errors.append({"index": i, "error": err})
                    logger.warning(
                        "[PIPELINE:APP_INGEST] Record failed: source_id=%s index=%s error=%s",
                        source_id,
                        i,
                        err,
                    )
            except Exception as exc:
                errors.append({"index": i, "error": str(exc)})
                logger.warning(
                    "[PIPELINE:APP_INGEST] Record failed: source_id=%s index=%s error=%s",
                    source_id,
                    i,
                    exc,
                    exc_info=True,
                )
        if errors:
            logger.warning(
                "[PIPELINE:APP_INGEST] Ingest completed with failures: source_id=%s processed=%d total=%d error_count=%d first_error=%s",
                source_id,
                processed,
                records_total,
                len(errors),
                errors[0].get("error"),
            )
        db_conn = get_db_connection()
        if db_conn and user_id:
            resource_id = (payload.get("resource_id") or "").strip() or f"dataset:{user_id}:{dataset_id or 'default'}"
            record_uma_request(
                db_conn,
                owner_user_id=user_id,
                resource_id=resource_id,
                request_type="write",
                endpoint="app_ingest",
                requesting_user_id=(payload.get("requesting_user_id") or None),
                app_id=payload.get("app_id"),
                requesting_user_email=(payload.get("requesting_user_email") or "").strip() or None,
                access_channel=(payload.get("access_channel") or "internal").strip() or "internal",
            )
        response_payload = {
            "records_processed": processed,
            "records_total": records_total,
            "errors": errors,
        }
        if processed == 0:
            return {
                "id": req_id,
                "status": "error",
                "error": f"All {records_total} record(s) failed ingestion for source_id={source_id}",
                "payload": response_payload,
            }
        return {
            "id": req_id,
            "status": "ok",
            "payload": response_payload,
        }

    if msg_type == "start_ingestion":
        import sys
        import uuid

        print(f"\033[93m[CRITICAL TOPOS HANDLER] start_ingestion ENTERED: req_id={req_id}\033[0m", file=sys.stderr, flush=True)
        
        payload = message.get("payload") or {}
        dataset_id = payload.get("dataset_id")
        owner_user_id = _owner_user_id_from_dataset_id(dataset_id)
        schema_id = payload.get("schema_id") or "chatgpt.conversation.v1"
        file_format = payload.get("file_format") or "jsonl"
        file_url = payload.get("file_url")
        file_base64 = payload.get("file_base64")
        file_path = payload.get("file_path")
        job_id = payload.get("job_id")
        source_id = payload.get("source_id")  # Optional source_id from control plane
        source_definition = payload.get("source_definition")
        
        print(f"\033[93m[CRITICAL TOPOS HANDLER] start_ingestion params: job_id={job_id}, dataset_id={dataset_id}, schema_id={schema_id}\033[0m", file=sys.stderr, flush=True)
        
        if not job_id:
            print(f"\033[93m[CRITICAL TOPOS HANDLER] start_ingestion ERROR: Missing job_id\033[0m", file=sys.stderr, flush=True)
            return {"id": req_id, "status": "error", "error": "job_id required"}
        
        try:
            print(f"\033[93m[CRITICAL TOPOS HANDLER] start_ingestion: Starting background task\033[0m", file=sys.stderr, flush=True)
            
            # Determine control plane base URL for progress updates
            # Use module-level settings (already imported at top of file, line 15)
            progress_api_url = None
            progress_api_key = str(payload.get("progress_api_key") or settings.topos_key or "")
            
            control_plane_url = settings.topos_control_plane_url
            if control_plane_url:
                # Extract base URL from WebSocket URL (wss://host/ws/engine -> https://host)
                if control_plane_url.startswith("wss://"):
                    progress_api_url = control_plane_url.replace("wss://", "https://").split("/ws/")[0]
                elif control_plane_url.startswith("ws://"):
                    progress_api_url = control_plane_url.replace("ws://", "http://").split("/ws/")[0]
                else:
                    progress_api_url = control_plane_url
            
            print(f"\033[93m[CRITICAL TOPOS HANDLER] start_ingestion: progress_api_url={progress_api_url}\033[0m", file=sys.stderr, flush=True)
            
            # Background processing function
            async def _process_ingestion_in_background():
                try:
                    print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Ingestion background task started: job_id={job_id}\033[0m", file=sys.stderr, flush=True)
                    
                    if isinstance(file_base64, str) and file_base64:
                        payload_bytes = base64.b64decode(file_base64)
                        result = await ingest_file_payload(
                            dataset_id=dataset_id or "",
                            schema_id=schema_id,
                            file_bytes=payload_bytes,
                            file_format=file_format,
                            job_id=job_id,
                            source_id=source_id,
                            source_definition=source_definition,
                            progress_api_url=progress_api_url,
                            progress_api_key=progress_api_key,
                        )
                    elif file_url:
                        payload_bytes = await _download_ingestion_payload(str(file_url))
                        result = await ingest_file_payload(
                            dataset_id=dataset_id or "",
                            schema_id=schema_id,
                            file_bytes=payload_bytes,
                            file_format=file_format,
                            job_id=job_id,
                            source_id=source_id,
                            source_definition=source_definition,
                            progress_api_url=progress_api_url,
                            progress_api_key=progress_api_key,
                        )
                    else:
                        result = await ingest_file_payload(
                            dataset_id=dataset_id or "",
                            schema_id=schema_id,
                            file_path=file_path,
                            file_format=file_format,
                            job_id=job_id,
                            source_id=source_id,
                            source_definition=source_definition,
                            progress_api_url=progress_api_url,
                            progress_api_key=progress_api_key,
                        )
                    
                    print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Ingestion complete: job_id={job_id}, result={result}\033[0m", file=sys.stderr, flush=True)
                    
                    # Send final progress update
                    if progress_api_url and progress_api_key:
                        try:
                            import httpx
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                await client.post(
                                    f"{progress_api_url}/v1/ingestion/progress",
                                    json={
                                        "job_id": job_id,
                                        "user_id": owner_user_id,
                                        "dataset_id": dataset_id,
                                        "status": "completed",
                                        "progress_percent": 100.0,
                                        "records_processed": result.get("records_processed", 0),
                                        "records_total": result.get("records_total"),
                                    },
                                    headers={"Authorization": f"Bearer {progress_api_key}"},
                                )
                        except Exception as exc:
                            print(f"\033[91m[CRITICAL TOPOS BACKGROUND] Failed to send final progress: {exc}\033[0m", file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"\033[91m[CRITICAL TOPOS BACKGROUND] Ingestion error: {e}\033[0m", file=sys.stderr, flush=True)
                    import traceback
                    print(f"\033[91m[CRITICAL TOPOS BACKGROUND] Traceback:\n{traceback.format_exc()}\033[0m", file=sys.stderr, flush=True)
                    
                    # Send error progress update
                    if progress_api_url and progress_api_key:
                        try:
                            import httpx
                            error_message = f"Ingestion failed while parsing uploaded file: {str(e)}"
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                await client.post(
                                    f"{progress_api_url}/v1/ingestion/progress",
                                    json={
                                        "job_id": job_id,
                                        "user_id": owner_user_id,
                                        "dataset_id": dataset_id,
                                        "status": "failed",
                                        "current_step": "parsing",
                                        "progress_percent": 0.0,
                                        "records_processed": 0,
                                        "error_message": error_message,
                                        "errors": [
                                            {
                                                "error_type": "parse_error",
                                                "error": error_message,
                                                "record_index": 0,
                                            }
                                        ],
                                    },
                                    headers={"Authorization": f"Bearer {progress_api_key}"},
                                )
                        except Exception:
                            pass
            
            # Start background task (non-blocking)
            asyncio.create_task(_process_ingestion_in_background())
            
            print(f"\033[93m[CRITICAL TOPOS HANDLER] start_ingestion: Returning immediately\033[0m", file=sys.stderr, flush=True)
            return {"id": req_id, "status": "ok", "payload": {"job_id": job_id, "status": "processing"}}
        except Exception as exc:  # noqa: BLE001
            print(f"\033[91m[CRITICAL TOPOS HANDLER] start_ingestion exception: {exc}\033[0m", file=sys.stderr, flush=True)
            import traceback
            print(f"\033[91m[CRITICAL TOPOS HANDLER] Traceback:\n{traceback.format_exc()}\033[0m", file=sys.stderr, flush=True)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "get_messages":
        payload = message.get("payload") or {}
        dataset_id = payload.get("dataset_id")
        limit = int(payload.get("limit") or 100)
        offset = int(payload.get("offset") or 0)
        _raw_ms = (payload.get("message_stream") or "ai_chat").strip().lower()
        message_stream = _raw_ms if _raw_ms in ("conversation", "ai_chat") else "ai_chat"
        logger.debug(
            "[PIPELINE:QUERY] get_messages: dataset_id=%s, limit=%s, offset=%s, message_stream=%s",
            dataset_id,
            limit,
            offset,
            message_stream,
        )
        try:
            db_conn = get_db_connection()
            if not db_conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}

            # Human / messenger lane: canonical conversation_messages (iMessage, Signal, …).
            if message_stream == "conversation":
                ds = (str(dataset_id).strip() if dataset_id is not None else "")
                if not ds:
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": "dataset_id is required when message_stream is 'conversation' (e.g. your Topos dataset id for iMessage rows)",
                    }
                if not _table_exists(db_conn, "conversation_messages"):
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": "conversation_messages table not available",
                    }
                filters_dict = payload.get("filters")
                if not isinstance(filters_dict, dict):
                    filters_dict = None
                filter_manifest = extract_filter_manifest(filters_dict)
                field_transforms = extract_field_transforms(filters_dict)
                req_limit = min(max(0, int(payload.get("limit") or 100)), 1000)
                offset_clamped = max(0, int(payload.get("offset") or 0))
                eff_limit = get_limit_cap(req_limit, filter_manifest, "conversation_messages")
                filter_where_m, filter_params = build_sql_constraints(
                    filter_manifest, "m.", logical_table_id="conversation_messages"
                )
                extra_sql: List[str] = []
                extra_params: List[Any] = []
                ifs = payload.get("is_from_self")
                if isinstance(ifs, bool):
                    extra_sql.append("m.is_from_self = ?")
                    extra_params.append(1 if ifs else 0)
                st = payload.get("sender_type")
                if isinstance(st, str) and st.strip():
                    extra_sql.append("m.sender_type = ?")
                    extra_params.append(st.strip())
                sid = payload.get("source_id")
                if isinstance(sid, str) and sid.strip():
                    extra_sql.append("m.source_id = ?")
                    extra_params.append(sid.strip())
                extra_clause = ""
                if extra_sql:
                    extra_clause = " AND " + " AND ".join(extra_sql)
                query = (
                    """
                    SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                           m.event_at, m.content, m.metadata_json, m.source_id, m.dataset_id,
                           m.reply_to_message_id, m.message_type, m.event_type, m.is_from_self, m.owner_user_id
                    FROM conversation_messages m
                    WHERE m.dataset_id = ?
                    """
                    + filter_where_m
                    + extra_clause
                    + """
                    ORDER BY m.event_at DESC
                    LIMIT ? OFFSET ?
                    """
                )
                cursor = db_conn.execute(
                    query,
                    (ds,) + tuple(filter_params) + tuple(extra_params) + (eff_limit, offset_clamped),
                )
                messages: List[Dict[str, Any]] = []
                for row in cursor.fetchall():
                    messages.append(
                        {
                            "message_id": row[0],
                            "conversation_id": row[1],
                            "sender_type": row[2],
                            "sender_id": row[3],
                            "event_at": row[4],
                            "content": row[5],
                            "metadata_json": row[6],
                            "source_id": row[7],
                            "dataset_id": row[8],
                            "reply_to_message_id": row[9],
                            "message_type": row[10],
                            "event_type": row[11],
                            "is_from_self": row[12],
                            "owner_user_id": row[13],
                        }
                    )
                # Owner's engine: same contact pipeline as UMA conversation lane.
                # Optional allowed_scopes from MCP owner policy (control plane); default full owner lane.
                raw_owner_scopes = payload.get("allowed_scopes")
                if isinstance(raw_owner_scopes, list) and raw_owner_scopes:
                    owner_allowed_scopes = [
                        str(s).strip() for s in raw_owner_scopes if str(s).strip()
                    ]
                else:
                    owner_allowed_scopes = ["messages:read", "contacts:resolve"]
                try:
                    pre_len = len(messages)
                    after_contact, uma_contact_sidecar = apply_message_contact_pipeline(
                        messages,
                        conn=db_conn,
                        dataset_id=ds,
                        allowed_scopes=owner_allowed_scopes,
                        manifest=filter_manifest,
                        filters=filters_dict,
                    )
                    manifest_for_generic = strip_contact_runtime_filters(filter_manifest)
                    transform_diag: Dict[str, Any] = {}
                    messages = await apply_filter_manifest_async(
                        after_contact,
                        manifest_for_generic,
                        field_transforms=field_transforms,
                        table_id="conversation_messages",
                        diagnostics=transform_diag,
                        progress_hook=_uma_transform_progress_hook(req_id, "get_messages:conversation_messages"),
                    )
                    logger.debug(
                        "[PIPELINE:QUERY] get_messages conversation stream: raw=%s after_pipeline=%s",
                        pre_len,
                        len(messages),
                    )
                except UMAFilterError as exc:
                    return {"id": req_id, "status": "error", "error": str(exc)}
                record_mcp_request(
                    db_conn,
                    "get_messages",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                return {"id": req_id, "status": "ok", "payload": {"messages": messages, "message_stream": "conversation"}}

            # Query canonical messages from ai_chat_messages table (AI chat lane)
            if _table_exists(db_conn, "ai_chat_messages"):
                has_conversations_table = _table_exists(db_conn, "ai_chat_conversations")
                
                # Check if message_emotions table exists
                has_emotions_table = _table_exists(db_conn, "message_emotions")
                
                if has_conversations_table and dataset_id:
                    # Join with conversations to filter by owner_user_id
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    if has_emotions_table:
                        # Include emotion data via LEFT JOIN
                        # Get the emotion with highest confidence per message (in case multiple models exist)
                        query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id,
                                   e.emotion_label, e.confidence
                            FROM ai_chat_messages m
                            LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                            LEFT JOIN (
                                SELECT e1.message_id, e1.emotion_label, e1.confidence
                                FROM message_emotions e1
                                INNER JOIN (
                                    SELECT message_id, MAX(confidence) as max_confidence
                                    FROM message_emotions
                                    GROUP BY message_id
                                ) e2 ON e1.message_id = e2.message_id AND e1.confidence = e2.max_confidence
                            ) e ON m.message_id = e.message_id
                            WHERE c.owner_user_id = ?
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                    else:
                        query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id,
                                   NULL as emotion_label, NULL as confidence
                            FROM ai_chat_messages m
                            LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                            WHERE c.owner_user_id = ?
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                    cursor = db_conn.execute(query, (user_id, limit, offset))
                else:
                    # Query without user filtering
                    if has_emotions_table:
                        # Include emotion data via LEFT JOIN
                        # Get the emotion with highest confidence per message (in case multiple models exist)
                        query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id,
                                   e.emotion_label, e.confidence
                            FROM ai_chat_messages m
                            LEFT JOIN (
                                SELECT e1.message_id, e1.emotion_label, e1.confidence
                                FROM message_emotions e1
                                INNER JOIN (
                                    SELECT message_id, MAX(confidence) as max_confidence
                                    FROM message_emotions
                                    GROUP BY message_id
                                ) e2 ON e1.message_id = e2.message_id AND e1.confidence = e2.max_confidence
                            ) e ON m.message_id = e.message_id
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                    else:
                        query = """
                            SELECT message_id, conversation_id, sender_type, sender_id,
                                   event_at, content, content_rendered, metadata_json, sequence, source_id,
                                   NULL as emotion_label, NULL as confidence
                            FROM ai_chat_messages
                            ORDER BY event_at DESC
                            LIMIT ? OFFSET ?
                        """
                    cursor = db_conn.execute(query, (limit, offset))
                
                messages = []
                seen_message_ids = set()  # Track to avoid duplicates from JOIN
                for row in cursor.fetchall():
                    message_id = row[0]
                    # Skip duplicates (in case JOIN returns multiple emotion records)
                    if message_id in seen_message_ids:
                        continue
                    seen_message_ids.add(message_id)
                    
                    message = {
                        "message_id": message_id,
                        "conversation_id": row[1],
                        "sender_type": row[2],
                        "sender_id": row[3],
                        "event_at": row[4],
                        "content": row[5],
                        "content_rendered": row[6],
                        "metadata_json": row[7],
                        "sequence": row[8],
                        "source_id": row[9],
                    }
                    # Add emotion data if available
                    if len(row) > 10:
                        emotion_label = row[10] if row[10] is not None else None
                        confidence = row[11] if row[11] is not None else None
                        if emotion_label:
                            message["emotion"] = emotion_label
                            if confidence is not None:
                                message["emotion_confidence"] = float(confidence)
                    messages.append(message)
                
                logger.debug(
                    "[PIPELINE:QUERY] get_messages returned %d messages from canonical table",
                    len(messages),
                )
                record_mcp_request(
                    db_conn,
                    "get_messages",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                return {
                    "id": req_id,
                    "status": "ok",
                    "payload": {"messages": messages, "message_stream": "ai_chat"},
                }
            else:
                # Fallback to raw messages if canonical table doesn't exist
                messages = load_raw_messages(
                    dataset_id=dataset_id or "",
                    schema_id="chatgpt.conversation.v1",
                    limit=limit,
                    offset=offset,
                )
                logger.debug(
                    "[PIPELINE:QUERY] get_messages returned %d messages from raw files",
                    len(messages),
                )
                record_mcp_request(
                    db_conn,
                    "get_messages",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                return {
                    "id": req_id,
                    "status": "ok",
                    "payload": {"messages": messages, "message_stream": "ai_chat"},
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PIPELINE:QUERY] get_messages error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "get_oplog":
        conn = get_db_connection()
        record_mcp_request(
            conn,
            "get_oplog",
            source=_mcp_source,
            requester_id=_mcp_requester_id,
            resource_owner_user_id=_resource_owner_for_mcp_log(conn),
        )
        return {"id": req_id, "status": "ok", "payload": {"ops": []}}
    if msg_type == "replay_projection":
        return {"id": req_id, "status": "ok", "payload": {"status": "ok"}}
    if msg_type == "replay_projection_preview":
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"ops_replayed": 0, "total_ops": 0, "count": 0, "messages": []},
        }
    if msg_type == "get_analytics":
        payload = message.get("payload") or {}
        query = str(payload.get("query") or "").lower()
        dataset_id = payload.get("dataset_id")
        # Fallback to user_id:default if dataset_id is missing (for local mode compatibility)
        if not dataset_id:
            # Try to get user_id from database
            db_conn = get_db_connection()
            if db_conn:
                user_id = get_user_id(db_conn)
                if user_id:
                    dataset_id = f"{user_id}:{settings.topos_default_dataset_id}"
        logger.debug(
            "[PIPELINE:ANALYTICS] Query received: query=%s, dataset_id=%s",
            query,
            dataset_id,
        )
        try:
            # Get database connection (will create if needed)
            db_conn = get_db_connection()
            
            # Try to query database tables first, fallback to raw files
            if db_conn:
                # Demo app queries (messages table)
                if query == "messages_per_day":
                    result = _query_messages_per_day_db(db_conn, dataset_id, "messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "total_messages":
                    result = _query_total_messages_db(db_conn, dataset_id, "messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "avg_message_length":
                    result = _query_avg_message_length_db(db_conn, dataset_id, "messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "messages_by_sender":
                    result = _query_messages_by_sender_db(db_conn, dataset_id, "messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                
                # Canonical AI Chat Messages queries (ai_chat_messages table - all sources)
                if query == "canonical_messages_per_day":
                    result = _query_messages_per_day_db(db_conn, dataset_id, "ai_chat_messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "canonical_total_messages":
                    result = _query_total_messages_db(db_conn, dataset_id, "ai_chat_messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "canonical_avg_message_length":
                    result = _query_avg_message_length_db(db_conn, dataset_id, "ai_chat_messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "canonical_messages_by_sender":
                    result = _query_messages_by_sender_db(db_conn, dataset_id, "ai_chat_messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                
                # ChatGPT ingestion queries - try ai_chat_messages first, then chatgpt_messages (for backward compatibility)
                if query == "chatgpt_messages_per_day":
                    result = _query_messages_per_day_db(db_conn, dataset_id, "ai_chat_messages", source_filter="chatgpt")
                    if not result:
                        result = _query_messages_per_day_db(db_conn, dataset_id, "chatgpt_messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "chatgpt_total_messages":
                    result = _query_total_messages_db(db_conn, dataset_id, "ai_chat_messages", source_filter="chatgpt")
                    if result.get("total_messages", 0) == 0:
                        result = _query_total_messages_db(db_conn, dataset_id, "chatgpt_messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "chatgpt_avg_message_length":
                    result = _query_avg_message_length_db(db_conn, dataset_id, "ai_chat_messages", source_filter="chatgpt")
                    if result.get("avg_length", 0) == 0:
                        result = _query_avg_message_length_db(db_conn, dataset_id, "chatgpt_messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "chatgpt_messages_by_sender":
                    result = _query_messages_by_sender_db(db_conn, dataset_id, "ai_chat_messages", source_filter="chatgpt")
                    if not result:
                        result = _query_messages_by_sender_db(db_conn, dataset_id, "chatgpt_messages")
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                
                # Combined queries (messages + ai_chat_messages/chatgpt_messages)
                if query == "combined_messages_per_day":
                    result = _query_combined_messages_per_day(db_conn, dataset_id)
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "combined_total_messages":
                    result = _query_combined_total_messages(db_conn, dataset_id)
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "combined_avg_message_length":
                    result = _query_combined_avg_message_length(db_conn, dataset_id)
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
                if query == "combined_messages_by_sender":
                    result = _query_combined_messages_by_sender(db_conn, dataset_id)
                    record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                    return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            
            # JSONL queries (raw file store) - only handle explicit jsonl_* queries
            if query.startswith("jsonl_"):
                messages = load_raw_messages(
                    dataset_id=dataset_id or "",
                    schema_id="chatgpt.conversation.v1",
                    limit=None,
                    offset=0,
                )
                logger.debug(
                    "[PIPELINE:ANALYTICS] Loaded %d messages from raw store for query: %s",
                    len(messages),
                    query,
                )
                if query == "jsonl_messages_per_day":
                    result = messages_per_day(messages)
                elif query == "jsonl_total_messages":
                    result = total_messages(messages)
                elif query == "jsonl_avg_message_length":
                    result = avg_message_length(messages)
                elif query == "jsonl_messages_by_sender":
                    result = messages_by_sender(messages)
                else:
                    result = []
                logger.debug(
                    "[PIPELINE:ANALYTICS] Query result: query=%s, result_size=%s",
                    query,
                    len(result) if isinstance(result, list) else 1,
                )
                record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            
            # Unknown query - return empty result
            logger.warning("[PIPELINE:ANALYTICS] Unknown query: %s", query)
            record_mcp_request(
                    db_conn,
                    "get_analytics",
                    source=_mcp_source,
                    requester_id=_mcp_requester_id,
                    resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
                )
            return {"id": req_id, "status": "ok", "payload": {"query": query, "result": []}}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PIPELINE:ANALYTICS] Query error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "get_ingestion_datasets":
        logger.debug("[PIPELINE:QUERY] get_ingestion_datasets requested")
        try:
            file_store = RawFileStore()
            datasets = file_store.list_datasets()
            logger.debug(
                "[PIPELINE:QUERY] get_ingestion_datasets returned %d datasets",
                len(datasets),
            )
            return {"id": req_id, "status": "ok", "payload": {"datasets": datasets}}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PIPELINE:QUERY] get_ingestion_datasets error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "get_sources":
        try:
            from ..api.source_install import _list_sources_core

            payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
            result = await _list_sources_core(payload)
            sources = result.get("sources") if isinstance(result, dict) else []
            logger.debug("[PIPELINE:QUERY] get_sources returned %d scoped sources", len(sources) if isinstance(sources, list) else 0)
            return {"id": req_id, "status": "ok", "payload": result}
        except ValueError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PIPELINE:QUERY] get_sources error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "enrichment_process_source":
        import sys
        import uuid
        
        print(f"\033[93m[CRITICAL TOPOS HANDLER] enrichment_process_source ENTERED: req_id={req_id}\033[0m", file=sys.stderr, flush=True)
        
        payload = message.get("payload") or {}
        source_id = payload.get("source_id")
        dataset_id = payload.get("dataset_id")
        job_names = payload.get("job_names")
        force_reprocess = payload.get("force_reprocess", False)
        
        print(f"\033[93m[CRITICAL TOPOS HANDLER] Params: source_id={source_id}, dataset_id={dataset_id}\033[0m", file=sys.stderr, flush=True)
        
        logger.debug(
            "[PIPELINE:ENRICHMENT] enrichment_process_source received: source_id=%s, dataset_id=%s, job_names=%s, force_reprocess=%s",
            source_id,
            dataset_id,
            job_names,
            force_reprocess,
        )
        
        if not source_id:
            print(f"\033[93m[CRITICAL TOPOS HANDLER] ERROR: Missing source_id\033[0m", file=sys.stderr, flush=True)
            return {"id": req_id, "status": "error", "error": "source_id required"}
        
        try:
            print(f"\033[93m[CRITICAL TOPOS HANDLER] Importing modules...\033[0m", file=sys.stderr, flush=True)
            from ..api.enrichment import _process_enrichment_core
            # REGISTRY is already imported at module level (line 20), don't re-import here
            
            # Import progress module from engine (it's in engine/enrichment/progress.py)
            # We need to import it via sys.path or use a different approach
            # Since we're in topos but need engine module, we'll create a simple progress tracker
            import time
            # Create module-level progress store if it doesn't exist
            import sys as sys_module
            module = sys_module.modules[__name__]
            if not hasattr(module, '_progress_store'):
                module._progress_store = {}
            _progress_store = module._progress_store
            
            class SimpleProgress:
                """Simple progress tracker compatible with engine's EnrichmentProgress interface."""
                def __init__(self, job_id: str, total_messages: int):
                    self.job_id = job_id
                    self.total_messages = total_messages
                    self.messages_processed = 0
                    self.messages_skipped = 0
                    self.status = "processing"
                    self.start_time = time.time()
                    self.last_update_time = self.start_time
                    self.errors = []
                    self.records_created = {}
                    self.jobs_complete = 0
                    self.jobs_total = 0
                    self.jobs_progress_percent = 0.0
                    self.current_job_name = None
                    self.current_job_progress_percent = 0.0
                    _progress_store[job_id] = self
                
                def update(self, **kwargs):
                    """Update progress fields."""
                    for key, value in kwargs.items():
                        setattr(self, key, value)
                    self.last_update_time = time.time()
                
                def complete(self, result):
                    """Mark as complete."""
                    self.status = "completed"
                    self.update(**result)
                
                def fail(self, error):
                    """Mark as failed."""
                    self.status = "failed"
                    self.errors.append({"error": error})
                    self.last_update_time = time.time()
                
                def get_progress_percent(self):
                    """Get overall progress percentage."""
                    if not self.jobs_total or self.jobs_total == 0:
                        if not self.total_messages or self.total_messages == 0:
                            return 0.0
                        total_handled = self.messages_processed + self.messages_skipped
                        return min(100.0, (total_handled / self.total_messages) * 100.0)
                    jobs_base_progress = (self.jobs_complete / self.jobs_total) * 100.0
                    current_job_contribution = (self.current_job_progress_percent / 100.0) * (100.0 / self.jobs_total)
                    return min(100.0, jobs_base_progress + current_job_contribution)
                
                def to_dict(self):
                    """Convert to dict for API response."""
                    return {
                        "job_id": self.job_id,
                        "status": self.status,
                        "progress_percent": self.get_progress_percent(),
                        "messages_processed": self.messages_processed,
                        "messages_skipped": self.messages_skipped,
                        "messages_total": self.total_messages,
                        "records_created": self.records_created,
                        "errors": self.errors,
                        "jobs_complete": self.jobs_complete,
                        "jobs_total": self.jobs_total,
                        "jobs_progress_percent": (self.jobs_complete / self.jobs_total * 100) if self.jobs_total > 0 else 0.0,
                        "current_job_name": self.current_job_name,
                        "current_job_progress_percent": self.current_job_progress_percent,
                    }
            
            def create_progress(job_id: str, total_messages: int):
                """Create a progress tracker."""
                return SimpleProgress(job_id, total_messages)
            
            def get_progress(job_id: str):
                """Get progress tracker by job_id."""
                return _progress_store.get(job_id)
            
            # Make get_progress available at module level for enrichment_progress handler
            import sys as sys_module
            module = sys_module.modules[__name__]
            module._get_progress = get_progress
            
            print(f"\033[93m[CRITICAL TOPOS HANDLER] Getting source definition...\033[0m", file=sys.stderr, flush=True)
            source_def = REGISTRY.get(source_id)
            if not source_def:
                print(f"\033[93m[CRITICAL TOPOS HANDLER] ERROR: Source {source_id} not found\033[0m", file=sys.stderr, flush=True)
                return {"id": req_id, "status": "error", "error": f"Source {source_id} not found"}
            
            jobs_to_run = job_names or source_def.canonical_enrichment_jobs
            if not jobs_to_run:
                print(f"\033[93m[CRITICAL TOPOS HANDLER] No jobs configured, returning early\033[0m", file=sys.stderr, flush=True)
                return {
                    "id": req_id,
                    "status": "ok",
                    "payload": {
                        "status": "ok",
                        "message": "No enrichment jobs configured for this source",
                        "messages_processed": 0,
                        "records_created": {},
                    },
                }
            
            print(f"\033[93m[CRITICAL TOPOS HANDLER] Creating job_id...\033[0m", file=sys.stderr, flush=True)
            job_id = str(uuid.uuid4())
            progress = create_progress(job_id, 0)
            
            print(f"\033[93m[CRITICAL TOPOS HANDLER] Starting background task...\033[0m", file=sys.stderr, flush=True)
            # Background processing function
            async def _process_in_background():
                try:
                    print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Background task started\033[0m", file=sys.stderr, flush=True)
                    from ..core.state import get_db_connection
                    from ..enrichment.derived_tables import DerivedTablesManager
                    from ..enrichment.orchestrator import EnrichmentOrchestrator
                    from ..api.enrichment import _find_unprocessed_messages
                    
                    db_conn = get_db_connection()
                    if not db_conn:
                        raise RuntimeError("Database connection not available")
                    
                    # Find unprocessed messages
                    print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Finding unprocessed messages...\033[0m", file=sys.stderr, flush=True)
                    if force_reprocess:
                        # Load all messages for this source
                        cursor = db_conn.execute("""
                            SELECT message_id, conversation_id, sender_type, sender_id,
                                   event_at, content, content_rendered, metadata_json, sequence, source_id
                            FROM ai_chat_messages
                            WHERE source_id = ?
                            ORDER BY event_at ASC
                        """, (source_id,))
                        unprocessed_messages = []
                        for row in cursor.fetchall():
                            unprocessed_messages.append({
                                "message_id": row[0],
                                "conversation_id": row[1],
                                "sender_type": row[2],
                                "sender_id": row[3],
                                "event_at": row[4],
                                "content": row[5],
                                "content_rendered": row[6],
                                "metadata_json": row[7],
                                "sequence": row[8],
                                "source_id": row[9],
                            })
                    else:
                        unprocessed_messages = await _find_unprocessed_messages(source_id, dataset_id, jobs_to_run)
                    
                    if not unprocessed_messages:
                        print(f"\033[93m[CRITICAL TOPOS BACKGROUND] No unprocessed messages\033[0m", file=sys.stderr, flush=True)
                        progress.complete({
                            "messages_processed": 0,
                            "messages_skipped": 0,
                            "records_created": {},
                            "errors": [],
                        })
                        return
                    
                    # Update progress with total messages
                    print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Found {len(unprocessed_messages)} messages, updating progress\033[0m", file=sys.stderr, flush=True)
                    progress.total_messages = len(unprocessed_messages)
                    progress.update(
                        messages_processed=0,
                        messages_skipped=0,
                        jobs_total=len(jobs_to_run),
                        jobs_complete=0,
                    )
                    
                    # Run enrichment with progress callback
                    print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Initializing orchestrator...\033[0m", file=sys.stderr, flush=True)
                    tables_manager = DerivedTablesManager(conn=db_conn)
                    orchestrator = EnrichmentOrchestrator(tables_manager=tables_manager)
                    
                    # Define progress callback
                    def progress_callback(
                        processed_count: int, 
                        total_count: int, 
                        job_name: str, 
                        job_percent: float,
                        current_job_progress: float,
                    ):
                        """Update progress as jobs execute."""
                        estimated_messages_processed = int((job_percent / 100) * total_count)
                        jobs_complete = int((job_percent / 100) * len(jobs_to_run))
                        progress.update(
                            messages_processed=estimated_messages_processed,
                            messages_skipped=0,
                            current_job_name=job_name,
                            current_job_progress_percent=current_job_progress,
                            jobs_complete=jobs_complete,
                            jobs_total=len(jobs_to_run),
                        )
                    
                    print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Running enrichment...\033[0m", file=sys.stderr, flush=True)
                    enrichment_result = await orchestrator.run_canonical(
                        unprocessed_messages,
                        job_names=jobs_to_run,
                        progress_callback=progress_callback,
                    )
                    
                    print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Enrichment complete\033[0m", file=sys.stderr, flush=True)
                    result = {
                        "status": "ok",
                        "source_id": source_id,
                        "messages_processed": len(unprocessed_messages),
                        "jobs_run": enrichment_result.get("jobs_run", 0),
                        "records_created": enrichment_result.get("records_created", {}),
                        "errors": enrichment_result.get("errors", []),
                    }
                    progress.complete(result)
                except Exception as e:
                    print(f"\033[91m[CRITICAL TOPOS BACKGROUND] Error: {e}\033[0m", file=sys.stderr, flush=True)
                    import traceback
                    print(f"\033[91m[CRITICAL TOPOS BACKGROUND] Traceback:\n{traceback.format_exc()}\033[0m", file=sys.stderr, flush=True)
                    progress.fail(str(e))
            
            # Start background task (non-blocking)
            asyncio.create_task(_process_in_background())
            
            print(f"\033[93m[CRITICAL TOPOS HANDLER] Returning immediately with job_id={job_id}\033[0m", file=sys.stderr, flush=True)
            logger.debug(
                "[PIPELINE:ENRICHMENT] enrichment_process_source: source_id=%s, job_id=%s",
                source_id,
                job_id,
            )
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "job_id": job_id,
                    "status": "processing",
                    "source_id": source_id,
                    "messages_total": 0,  # Will be updated when background task finds messages
                    "message": "Processing started. Use /v1/enrichment/progress/{job_id} to track progress.",
                },
            }
        except Exception as exc:  # noqa: BLE001
            print(f"\033[91m[CRITICAL TOPOS HANDLER] Exception: {exc}\033[0m", file=sys.stderr, flush=True)
            import traceback
            print(f"\033[91m[CRITICAL TOPOS HANDLER] Traceback:\n{traceback.format_exc()}\033[0m", file=sys.stderr, flush=True)
            logger.error("[PIPELINE:ENRICHMENT] enrichment_process_source error: %s", exc, exc_info=True)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "enrichment_progress":
        payload = message.get("payload") or {}
        job_id = payload.get("job_id")
        
        if not job_id:
            return {"id": req_id, "status": "error", "error": "Missing job_id"}
        
        # Get progress from the store (created in enrichment_process_source handler)
        try:
            import sys as sys_module
            module = sys_module.modules[__name__]
            if hasattr(module, '_get_progress'):
                get_progress_func = getattr(module, '_get_progress')
                progress = get_progress_func(job_id)
            else:
                # Fallback: try to access _progress_store directly
                if hasattr(module, '_progress_store'):
                    progress = module._progress_store.get(job_id)
                else:
                    progress = None
            
            if not progress:
                return {"id": req_id, "status": "error", "error": f"Job {job_id} not found"}
            
            progress_dict = progress.to_dict() if hasattr(progress, 'to_dict') else {
                "job_id": job_id,
                "status": getattr(progress, 'status', 'unknown'),
                "progress_percent": getattr(progress, 'get_progress_percent', lambda: 0.0)(),
            }
            return {"id": req_id, "status": "ok", "payload": progress_dict}
        except Exception as e:
            print(f"\033[91m[CRITICAL TOPOS HANDLER] Error getting progress: {e}\033[0m", file=sys.stderr, flush=True)
            import traceback
            print(f"\033[91m[CRITICAL TOPOS HANDLER] Traceback:\n{traceback.format_exc()}\033[0m", file=sys.stderr, flush=True)
            return {"id": req_id, "status": "error", "error": str(e)}
    
    if msg_type == "enrichment_status_source":
        payload = message.get("payload") or {}
        source_id = payload.get("source_id")
        dataset_id = payload.get("dataset_id")
        
        if not source_id:
            return {"id": req_id, "status": "error", "error": "source_id required"}
        
        try:
            from ..api.enrichment import _get_enrichment_status_core
            
            # Call the core enrichment status logic
            result = await _get_enrichment_status_core(
                source_id=source_id,
                dataset_id=dataset_id,
            )
            logger.debug(
                "[PIPELINE:ENRICHMENT] enrichment_status_source: source_id=%s, total=%s, processed=%s",
                source_id,
                result.get("total_messages"),
                result.get("processed_messages"),
            )
            return {"id": req_id, "status": "ok", "payload": result}
        except Exception as exc:  # noqa: BLE001
            logger.error("[PIPELINE:ENRICHMENT] enrichment_status_source error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "source_enrichments_list":
        payload = message.get("payload") or {}
        source_id = payload.get("source_id")
        if not source_id:
            return {"id": req_id, "status": "error", "error": "source_id required"}
        try:
            from ..api.enrichment import list_source_enrichments

            result = await list_source_enrichments(source_id=source_id)
            return {"id": req_id, "status": "ok", "payload": result}
        except Exception as exc:  # noqa: BLE001
            logger.error("[PIPELINE:ENRICHMENT] source_enrichments_list error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "source_enrichment_backfill":
        payload = message.get("payload") or {}
        source_id = payload.get("source_id")
        enrichment_name = payload.get("enrichment_name")
        only_missing = payload.get("only_missing", True)
        limit = payload.get("limit")
        if not source_id:
            return {"id": req_id, "status": "error", "error": "source_id required"}
        if not enrichment_name:
            return {"id": req_id, "status": "error", "error": "enrichment_name required"}
        try:
            logger.info(
                "[PIPELINE:ENRICHMENT] source_enrichment_backfill received: source_id=%s enrichment=%s only_missing=%s limit=%s",
                source_id,
                enrichment_name,
                only_missing,
                limit,
            )
            from ..api.enrichment import backfill_source_enrichment

            result = await backfill_source_enrichment(
                source_id=source_id,
                enrichment_name=enrichment_name,
                only_missing=bool(only_missing),
                limit=limit,
            )
            logger.info(
                "[PIPELINE:ENRICHMENT] source_enrichment_backfill complete: source_id=%s enrichment=%s rows_scanned=%s rows_processed=%s rows_failed=%s",
                source_id,
                enrichment_name,
                result.get("rows_scanned"),
                result.get("rows_processed"),
                result.get("rows_failed"),
            )
            return {"id": req_id, "status": "ok", "payload": result}
        except Exception as exc:  # noqa: BLE001
            logger.error("[PIPELINE:ENRICHMENT] source_enrichment_backfill error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "source_enrichment_test":
        payload = message.get("payload") or {}
        source_id = payload.get("source_id")
        enrichment_name = payload.get("enrichment_name")
        data_packet = payload.get("data_packet") or {}
        if not source_id:
            return {"id": req_id, "status": "error", "error": "source_id required"}
        if not enrichment_name:
            return {"id": req_id, "status": "error", "error": "enrichment_name required"}
        try:
            logger.info(
                "[PIPELINE:ENRICHMENT] source_enrichment_test received: source_id=%s enrichment=%s",
                source_id,
                enrichment_name,
            )
            from ..api.enrichment import test_source_enrichment

            result = await test_source_enrichment(
                source_id=source_id,
                enrichment_name=enrichment_name,
                data_packet=data_packet,
            )
            return {"id": req_id, "status": "ok", "payload": result}
        except Exception as exc:  # noqa: BLE001
            logger.error("[PIPELINE:ENRICHMENT] source_enrichment_test error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}
    if msg_type == "list_database_tables":
        """List all tables in the database, grouped by architecture layer."""
        try:
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
                conn = get_db_connection()
                if not conn:
                    return {"id": req_id, "status": "error", "error": "Database connection not available"}
            
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
                "browser_url_classification",  # URL classification derived from browser_visits/events
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
            canonical_tables = {
                "ai_chat_messages", "ai_chat_conversations", "ai_chat_participants"
            }
            
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
                    if not (scope_field and scope_value and scope_strategy):
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

                if (pooled_mode or scope_requested) and scope_field and isinstance(row_count, int) and row_count <= 0:
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
                engine_context["primary_dataset_id"] = f"{uid}:{settings.topos_default_dataset_id}"

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
    
    if msg_type == "get_table_count":
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
                conn = get_db_connection()
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
    
    if msg_type == "get_table_rows":
        """Return rows from a table for the simple table viewer. Limit to avoid huge payloads."""
        try:
            payload = message.get("payload") or {}
            table_name = (payload.get("table_name") or "").strip()
            requested_limit = max(1, int(payload.get("limit") or 500))
            limit = min(requested_limit, 2000)
            cap_reason = "max_rows_limit" if limit < requested_limit else None
            offset = max(0, int(payload.get("offset") or 0))
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
                    if _is_sqlite_conn(conn):
                        if scope_field and scope_value:
                            sql = f'SELECT * FROM "{table_name}" WHERE "{scope_field}" = ? LIMIT ? OFFSET ?'
                            sql_params = (scope_value, limit + 1, offset)
                            query_plan = _sqlite_query_plan(conn, sql, sql_params)
                            cursor = conn.execute(
                                sql,
                                sql_params,
                            )
                        else:
                            sql = f'SELECT * FROM "{table_name}" LIMIT ? OFFSET ?'
                            sql_params = (limit + 1, offset)
                            query_plan = _sqlite_query_plan(conn, sql, sql_params)
                            cursor = conn.execute(sql, sql_params)
                        rows = [dict(r) for r in cursor.fetchall()]
                    else:
                        if scope_field and scope_value:
                            cursor = conn.execute(
                                f'SELECT * FROM "{table_name}" WHERE "{scope_field}" = %s LIMIT %s OFFSET %s',
                                (scope_value, limit + 1, offset),
                            )
                        else:
                            cursor = conn.execute(
                                f'SELECT * FROM "{table_name}" LIMIT %s OFFSET %s',
                                (limit + 1, offset),
                            )
                        col_names = [desc[0] for desc in (cursor.description or [])]
                        rows = [dict(zip(col_names, r)) for r in cursor.fetchall()]
            else:
                conn = get_db_connection()
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
                if scope_field and scope_value:
                    sql = f'SELECT * FROM "{table_name}" WHERE "{scope_field}" = ? LIMIT ? OFFSET ?'
                    sql_params = (scope_value, limit + 1, offset)
                    query_plan = _sqlite_query_plan(conn, sql, sql_params)
                    cursor = conn.execute(
                        sql,
                        sql_params,
                    )
                else:
                    sql = f'SELECT * FROM "{table_name}" LIMIT ? OFFSET ?'
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

    if msg_type == "pooled_scope_backfill_dry_run":
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
                conn = get_db_connection()
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

    if msg_type == "pooled_scope_backfill_apply":
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
                conn = get_db_connection()
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

    if msg_type == "pooled_scope_backfill_rollback":
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
                conn = get_db_connection()
                if not conn:
                    return {"id": req_id, "status": "error", "error": "Database connection not available"}
                result = _pooled_scope_backfill_rollback(conn, migration_id)
            return {"id": req_id, "status": "ok", "payload": result}
        except Exception as exc:  # noqa: BLE001
            logger.error("pooled_scope_backfill_rollback failed: %s", exc, exc_info=True)
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "delete_database_table":
        """Drop a user table or view from the engine SQLite database (validated name)."""
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
            }
        )
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
            if settings.topos_database_mode == "postgres":
                with connect_postgres() as conn:
                    if _is_sqlite_conn(conn):
                        meta = conn.execute(
                            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                            (table_name,),
                        ).fetchone()
                        if not meta:
                            return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}
                        obj_type = str(meta["type"])
                        if obj_type not in ("table", "view"):
                            return {"id": req_id, "status": "error", "error": f"Unsupported object type: {obj_type}"}
                        conn.execute(f'DROP {obj_type} IF EXISTS "{table_name}"')
                        conn.commit()
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
                        table_type = str(meta[1] or "").upper()
                        obj_type = "view" if table_type == "VIEW" else "table"
                        drop_type = "VIEW" if obj_type == "view" else "TABLE"
                        conn.execute(f'DROP {drop_type} IF EXISTS "{table_name}"')
                        conn.commit()
            else:
                conn = get_db_connection()
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
                conn.execute(f'DROP {obj_type} IF EXISTS "{table_name}"')
                conn.commit()
            return {
                "id": req_id,
                "status": "ok",
                "payload": {"table_name": table_name, "dropped_type": obj_type},
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to delete database table: %s", exc, exc_info=True)
            try:
                rb = get_db_connection()
                if rb is not None:
                    rb.rollback()
            except Exception:
                pass
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "get_table_schema":
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
                conn = get_db_connection()
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

    if msg_type == "uma_get_messages":
        """UMA: return messages for a resource (control plane proxies here for My Access). Stage 2b: apply filter_manifest from payload.filters."""
        payload = message.get("payload") or {}
        resource_id = (payload.get("resource_id") or "").strip()
        dataset_id = (payload.get("dataset_id") or "").strip() or None
        if not dataset_id and resource_id:
            dataset_id = parse_dataset_id_from_uma_dataset_resource_id(resource_id)
        limit = min(int(payload.get("limit") or 100), 1000)
        offset = max(0, int(payload.get("offset") or 0))
        filters = payload.get("filters") or {}
        filters_dict = filters if isinstance(filters, dict) else None
        filter_manifest = extract_filter_manifest(filters_dict)
        field_transforms = extract_field_transforms(filters_dict)
        _raw_ms = (payload.get("message_stream") or "conversation").strip().lower()
        message_stream = _raw_ms if _raw_ms in ("conversation", "ai_chat") else "conversation"
        limit = get_limit_cap(
            limit,
            filter_manifest,
            "conversation_messages" if message_stream == "conversation" else "ai_chat_messages",
        )
        requesting_user_email = (payload.get("requesting_user_email") or "").strip() or None
        requesting_app_id = (payload.get("requesting_app_id") or "").strip() or None
        requesting_app_name = (payload.get("requesting_app_name") or "").strip() or None
        app_display = requesting_app_name or requesting_app_id or "(unknown app)"
        logger.info(
            "[PIPELINE:UMA] uma_get_messages: message_stream=%s, resource_id=%s, dataset_id=%s, limit=%s, offset=%s, requesting_user_email=%s, requesting_app=%s",
            message_stream,
            resource_id[:50] if resource_id else None,
            dataset_id,
            limit,
            offset,
            requesting_user_email or "(unknown)",
            app_display,
        )
        try:
            db_conn = get_db_connection()
            if not db_conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}

            def _uma_messages_record_and_return(
                messages_out: list,
                debug_metadata: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
                logger.debug("[PIPELINE:UMA] uma_get_messages returned %d messages", len(messages_out))
                owner_uid = (dataset_id or "").split(":")[0] or (
                    resource_id.split(":")[1] if len(resource_id.split(":")) >= 2 else ""
                )
                req_uid = (payload.get("requesting_user_id") or "").strip() or None
                acc_ch = (payload.get("access_channel") or "").strip() or "http"
                if owner_uid:
                    record_uma_request(
                        db_conn,
                        owner_user_id=owner_uid,
                        resource_id=resource_id,
                        request_type="read",
                        endpoint="messages",
                        requesting_user_id=req_uid,
                        app_id=requesting_app_id,
                        requesting_user_email=requesting_user_email,
                        access_channel=acc_ch,
                    )
                payload_out: Dict[str, Any] = {"messages": messages_out}
                if isinstance(debug_metadata, dict) and debug_metadata:
                    payload_out["debug_metadata"] = debug_metadata
                return {"id": req_id, "status": "ok", "payload": payload_out}

            if message_stream == "conversation":
                if _table_exists(db_conn, "conversation_messages") and dataset_id:
                    filter_where_m, filter_params = build_sql_constraints(
                        filter_manifest, "m.", logical_table_id="conversation_messages"
                    )
                    query = (
                        """
                        SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                               m.event_at, m.content, m.metadata_json, m.source_id, m.dataset_id,
                               m.reply_to_message_id, m.message_type, m.event_type, m.is_from_self, m.owner_user_id
                        FROM conversation_messages m
                        WHERE m.dataset_id = ?
                        """
                        + filter_where_m
                        + """
                        ORDER BY m.event_at DESC
                        LIMIT ? OFFSET ?
                        """
                    )
                    cursor = db_conn.execute(query, (dataset_id,) + tuple(filter_params) + (limit, offset))
                    messages = []
                    for row in cursor.fetchall():
                        messages.append(
                            {
                                "message_id": row[0],
                                "conversation_id": row[1],
                                "sender_type": row[2],
                                "sender_id": row[3],
                                "event_at": row[4],
                                "content": row[5],
                                "metadata_json": row[6],
                                "source_id": row[7],
                                "dataset_id": row[8],
                                "reply_to_message_id": row[9],
                                "message_type": row[10],
                                "event_type": row[11],
                                "is_from_self": row[12],
                                "owner_user_id": row[13],
                            }
                        )
                    allowed_scopes: List[str] = []
                    raw_scopes = payload.get("allowed_scopes")
                    if isinstance(raw_scopes, list):
                        allowed_scopes = [str(s).strip() for s in raw_scopes if str(s).strip()]
                    try:
                        pre_contact_len = len(messages)
                        after_contact, uma_contact_sidecar = apply_message_contact_pipeline(
                            messages,
                            conn=db_conn,
                            dataset_id=dataset_id,
                            allowed_scopes=allowed_scopes,
                            manifest=filter_manifest,
                            filters=filters_dict,
                        )
                        if pre_contact_len > 0 and len(after_contact) == 0:
                            logger.warning(
                                "[PIPELINE:UMA] req=%s conversation_messages: contact pipeline dropped all %s "
                                "SQL row(s) (sharing_policy row_visibility, message_contact_participation, or "
                                "contact_grant_policy). Field transforms (e.g. nsfw_sanitization on content) do not "
                                "run when no rows remain — same query order as ToposUI but a small limit (e.g. MCP "
                                "default 5) can yield only excluded contacts; try limit=50.",
                                req_id,
                                pre_contact_len,
                            )
                        scope_set = {str(s).strip() for s in allowed_scopes if s}
                        contacts_resolve = "contacts:resolve" in scope_set
                        name_nf = (
                            filter_manifest.get_filter("contact_display_names") if filter_manifest else None
                        )
                        names_effective = contacts_resolve and (
                            name_nf is None or bool(name_nf.params.get("enabled"))
                        )
                        pre_name_rows = sum(1 for row in after_contact if row.get("sender_display_name"))
                        manifest_for_generic = strip_contact_runtime_filters(filter_manifest)
                        transform_diag: Dict[str, Any] = {}
                        logger.info(
                            "[PIPELINE:UMA][TRANSFORM] req=%s stage=conversation_messages start rows=%s",
                            req_id,
                            len(after_contact),
                        )
                        logger.info(
                            "[PIPELINE:UMA] contact names: contacts_resolve=%s names_effective=%s "
                            "sender_display_name_rows_after_pipeline=%s allowed_scopes=%s",
                            contacts_resolve,
                            names_effective,
                            pre_name_rows,
                            allowed_scopes,
                        )
                        messages = await apply_filter_manifest_async(
                            after_contact,
                            manifest_for_generic,
                            field_transforms=field_transforms,
                            table_id="conversation_messages",
                            diagnostics=transform_diag,
                            progress_hook=_uma_transform_progress_hook(req_id, "conversation_messages"),
                        )
                        logger.info(
                            "[PIPELINE:UMA][TRANSFORM] req=%s stage=conversation_messages done applied=%s skipped=%s reasons=%s",
                            req_id,
                            transform_diag.get("applied_count", 0),
                            transform_diag.get("skipped_count", 0),
                            transform_diag.get("skip_reasons", {}),
                        )
                        _skip_reasons = transform_diag.get("skip_reasons") or {}
                        if _skip_reasons.get("table_mismatch"):
                            logger.info(
                                "[PIPELINE:UMA] req=%s table_mismatch skips=%s are expected when field_transforms "
                                "include other tables (browser_visits, etc.) while processing conversation_messages.",
                                req_id,
                                _skip_reasons.get("table_mismatch"),
                            )
                        post_name_rows = sum(1 for row in messages if bool(row.get("sender_display_name")))
                        debug_metadata = {
                            "contact_names_enriched_count": post_name_rows,
                            "contact_rows_filtered_count": max(0, pre_contact_len - len(after_contact)),
                            "field_transforms": transform_diag,
                            "contact_name_enrichment": {
                                "contacts_resolve_in_scope": contacts_resolve,
                                "contact_display_names_effective": names_effective,
                                "sender_display_name_rows_after_contact_pipeline": pre_name_rows,
                                "sender_display_name_rows_final": post_name_rows,
                            },
                            "message_owner": (uma_contact_sidecar.get("message_owner") or {}),
                        }
                    except UMAFilterError as exc:
                        return {"id": req_id, "status": "error", "error": str(exc)}
                    return _uma_messages_record_and_return(messages, debug_metadata)
                return _uma_messages_record_and_return([], {"contact_names_enriched_count": 0, "field_transforms": {}})

            if _table_exists(db_conn, "ai_chat_messages"):
                has_conversations_table = _table_exists(db_conn, "ai_chat_conversations")
                has_emotions_table = _table_exists(db_conn, "message_emotions")
                filter_where_m, filter_params = build_sql_constraints(
                    filter_manifest, "m.", logical_table_id="ai_chat_messages"
                )
                filter_where_plain, filter_params_plain = build_sql_constraints(
                    filter_manifest, "", logical_table_id="ai_chat_messages"
                )
                if has_conversations_table and dataset_id:
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    if has_emotions_table:
                        query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id,
                                   e.emotion_label, e.confidence
                            FROM ai_chat_messages m
                            LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                            LEFT JOIN (
                                SELECT e1.message_id, e1.emotion_label, e1.confidence
                                FROM message_emotions e1
                                INNER JOIN (
                                    SELECT message_id, MAX(confidence) as max_confidence
                                    FROM message_emotions
                                    GROUP BY message_id
                                ) e2 ON e1.message_id = e2.message_id AND e1.confidence = e2.max_confidence
                            ) e ON m.message_id = e.message_id
                            WHERE c.owner_user_id = ?
                            """ + filter_where_m + """
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                    else:
                        query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id,
                                   NULL as emotion_label, NULL as confidence
                            FROM ai_chat_messages m
                            LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                            WHERE c.owner_user_id = ?
                            """ + filter_where_m + """
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                    cursor = db_conn.execute(query, (user_id,) + tuple(filter_params) + (limit, offset))
                else:
                    if has_emotions_table:
                        query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id,
                                   e.emotion_label, e.confidence
                            FROM ai_chat_messages m
                            LEFT JOIN (
                                SELECT e1.message_id, e1.emotion_label, e1.confidence
                                FROM message_emotions e1
                                INNER JOIN (
                                    SELECT message_id, MAX(confidence) as max_confidence
                                    FROM message_emotions
                                    GROUP BY message_id
                                ) e2 ON e1.message_id = e2.message_id AND e1.confidence = e2.max_confidence
                            ) e ON m.message_id = e.message_id
                            """ + ("WHERE 1=1" + filter_where_m if filter_where_m else "") + """
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                    else:
                        query = """
                            SELECT message_id, conversation_id, sender_type, sender_id,
                                   event_at, content, content_rendered, metadata_json, sequence, source_id,
                                   NULL as emotion_label, NULL as confidence
                            FROM ai_chat_messages
                            """ + ("WHERE 1=1" + filter_where_plain if filter_where_plain else "") + """
                            ORDER BY event_at DESC
                            LIMIT ? OFFSET ?
                        """
                    if has_emotions_table:
                        cursor = db_conn.execute(
                            query,
                            tuple(filter_params) + (limit, offset) if filter_where_m else (limit, offset),
                        )
                    else:
                        cursor = db_conn.execute(
                            query,
                            tuple(filter_params_plain) + (limit, offset) if filter_where_plain else (limit, offset),
                        )
                messages = []
                seen_message_ids = set()
                for row in cursor.fetchall():
                    message_id = row[0]
                    if message_id in seen_message_ids:
                        continue
                    seen_message_ids.add(message_id)
                    msg = {
                        "message_id": message_id,
                        "conversation_id": row[1],
                        "sender_type": row[2],
                        "sender_id": row[3],
                        "event_at": row[4],
                        "content": row[5],
                        "content_rendered": row[6],
                        "metadata_json": row[7],
                        "sequence": row[8],
                        "source_id": row[9],
                    }
                    if len(row) > 10 and row[10] is not None:
                        msg["emotion"] = row[10]
                        if row[11] is not None:
                            msg["emotion_confidence"] = float(row[11])
                    messages.append(msg)
                allowed_scopes: List[str] = []
                raw_scopes = payload.get("allowed_scopes")
                if isinstance(raw_scopes, list):
                    allowed_scopes = [str(s).strip() for s in raw_scopes if str(s).strip()]
                pre_contact_len = len(messages)
                messages, uma_contact_sidecar = apply_message_contact_pipeline(
                    messages,
                    conn=db_conn,
                    dataset_id=dataset_id,
                    allowed_scopes=allowed_scopes,
                    manifest=filter_manifest,
                    filters=filters_dict,
                )
                if pre_contact_len > 0 and len(messages) == 0:
                    logger.warning(
                        "[PIPELINE:UMA] req=%s ai_chat_messages: contact pipeline dropped all %s row(s); "
                        "no field transforms run. Try a larger limit if grant contact filters exclude recent rows.",
                        req_id,
                        pre_contact_len,
                    )
                transform_diag = {}
                logger.info(
                    "[PIPELINE:UMA][TRANSFORM] req=%s stage=ai_chat_messages start rows=%s",
                    req_id,
                    len(messages),
                )
                messages = await apply_filter_manifest_async(
                    messages,
                    filter_manifest,
                    field_transforms=field_transforms,
                    table_id="ai_chat_messages",
                    diagnostics=transform_diag,
                    progress_hook=_uma_transform_progress_hook(req_id, "ai_chat_messages"),
                )
                logger.info(
                    "[PIPELINE:UMA][TRANSFORM] req=%s stage=ai_chat_messages done applied=%s skipped=%s reasons=%s",
                    req_id,
                    transform_diag.get("applied_count", 0),
                    transform_diag.get("skipped_count", 0),
                    transform_diag.get("skip_reasons", {}),
                )
                debug_metadata = {
                    "contact_names_enriched_count": sum(
                        1 for row in messages if bool(row.get("sender_display_name"))
                    ),
                    "contact_rows_filtered_count": max(0, pre_contact_len - len(messages)),
                    "field_transforms": transform_diag,
                    "message_owner": (uma_contact_sidecar.get("message_owner") or {}),
                }
                return _uma_messages_record_and_return(messages, debug_metadata)
            messages = load_raw_messages(
                dataset_id=dataset_id or "",
                schema_id="chatgpt.conversation.v1",
                limit=limit,
                offset=offset,
                filter_manifest=filter_manifest.to_storage_dict() if filter_manifest else None,
            )
            allowed_scopes = []
            raw_scopes = payload.get("allowed_scopes")
            if isinstance(raw_scopes, list):
                allowed_scopes = [str(s).strip() for s in raw_scopes if str(s).strip()]
            pre_contact_len = len(messages)
            messages, uma_contact_sidecar = apply_message_contact_pipeline(
                messages,
                conn=db_conn,
                dataset_id=dataset_id,
                allowed_scopes=allowed_scopes,
                manifest=filter_manifest,
                filters=filters_dict,
            )
            if pre_contact_len > 0 and len(messages) == 0:
                logger.warning(
                    "[PIPELINE:UMA] req=%s ai_chat JSONL: contact pipeline dropped all %s row(s); "
                    "no field transforms run. Try a larger limit.",
                    req_id,
                    pre_contact_len,
                )
            transform_diag = {}
            logger.info(
                "[PIPELINE:UMA][TRANSFORM] req=%s stage=ai_chat_messages_jsonl start rows=%s",
                req_id,
                len(messages),
            )
            messages = await apply_filter_manifest_async(
                messages,
                filter_manifest,
                field_transforms=field_transforms,
                table_id="ai_chat_messages",
                diagnostics=transform_diag,
                progress_hook=_uma_transform_progress_hook(req_id, "ai_chat_messages_jsonl"),
            )
            logger.info(
                "[PIPELINE:UMA][TRANSFORM] req=%s stage=ai_chat_messages_jsonl done applied=%s skipped=%s reasons=%s",
                req_id,
                transform_diag.get("applied_count", 0),
                transform_diag.get("skipped_count", 0),
                transform_diag.get("skip_reasons", {}),
            )
            debug_metadata = {
                "contact_names_enriched_count": sum(1 for row in messages if bool(row.get("sender_display_name"))),
                "contact_rows_filtered_count": max(0, pre_contact_len - len(messages)),
                "field_transforms": transform_diag,
                "message_owner": (uma_contact_sidecar.get("message_owner") or {}),
            }
            return _uma_messages_record_and_return(messages, debug_metadata)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PIPELINE:UMA] uma_get_messages error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "uma_get_oplog":
        """UMA: return oplog for a resource (control plane proxies here). Record read for engine request counts."""
        payload = message.get("payload") or {}
        resource_id = (payload.get("resource_id") or "").strip()
        dataset_id = (payload.get("dataset_id") or "").strip() or None
        owner_uid = payload.get("owner_user_id") or ((resource_id.split(":")[1] if len(resource_id.split(":")) >= 2 else "") or (dataset_id.split(":")[0] if dataset_id else ""))
        requesting_app_id = (payload.get("requesting_app_id") or "").strip() or None
        db_conn = get_db_connection()
        if db_conn and owner_uid and resource_id:
            record_uma_request(
                db_conn,
                owner_user_id=owner_uid,
                resource_id=resource_id,
                request_type="read",
                endpoint="oplog",
                requesting_user_id=payload.get("requesting_user_id"),
                app_id=requesting_app_id,
                access_channel=(payload.get("access_channel") or "http").strip() or "http",
            )
        return {"id": req_id, "status": "ok", "payload": {"ops": []}}

    if msg_type == "uma_get_rows":
        """UMA generic row read for one concrete table name."""
        payload = message.get("payload") or {}
        resource_id = (payload.get("resource_id") or "").strip()
        dataset_id, owner_uid, tenant_id = _resolve_uma_scope(payload, resource_id)
        table_name = (payload.get("table_name") or payload.get("table_id") or "").strip()
        requested_limit = min(int(payload.get("limit") or 100), 1000)
        offset = max(0, int(payload.get("offset") or 0))
        filters = payload.get("filters") or {}
        filters_dict = filters if isinstance(filters, dict) else None
        filter_manifest = extract_filter_manifest(filters_dict)
        field_transforms = extract_field_transforms(filters_dict)
        limit = get_limit_cap(requested_limit, filter_manifest, table_name)
        cap_reason = "permission_max_rows" if limit < requested_limit else None
        allowed_tables = payload.get("allowed_tables") or []
        allowed_set = {str(t).strip() for t in allowed_tables if str(t).strip()}
        if not table_name:
            return {"id": req_id, "status": "error", "error": "table_name required"}
        if allowed_set and table_name not in allowed_set:
            return {"id": req_id, "status": "error", "error": f"table not allowed: {table_name}"}
        try:
            use_postgres = settings.topos_database_mode == "postgres"
            preferred_order_by = {
                "events": ["event_at", "timestamp", "created_at"],
                "activity": ["event_at", "timestamp", "created_at"],
                "journal": ["event_at", "created_at", "id"],
                "conversation_messages": ["event_at", "created_at", "message_id"],
                "ai_chat_messages": ["event_at", "created_at", "message_id"],
            }
            if use_postgres:
                with connect_postgres() as conn:
                    if _is_sqlite_conn(conn):
                        exists = conn.execute(
                            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                            (table_name,),
                        ).fetchone()
                    else:
                        exists = conn.execute(
                            """
                            SELECT 1
                            FROM information_schema.tables
                            WHERE table_schema='public' AND table_name=%s
                            LIMIT 1
                            """,
                            (table_name,),
                        ).fetchone()
                    if not exists:
                        return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}

                    if _is_sqlite_conn(conn):
                        try:
                            cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                            col_names = {str(c["name"]) for c in cols} if cols else set()
                        except Exception:
                            col_names = set()
                    else:
                        cols = conn.execute(
                            """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema='public' AND table_name=%s
                            """,
                            (table_name,),
                        ).fetchall()
                        col_names = {str(c[0]) for c in cols} if cols else set()

                    scope_where, scope_params = _build_uma_scope_clause(
                        col_names=col_names,
                        dataset_id=dataset_id,
                        owner_user_id=owner_uid,
                        tenant_id=tenant_id,
                    )
                    if not scope_where:
                        return {
                            "id": req_id,
                            "status": "error",
                            "error": f"table not tenant scoped for UMA reads: {table_name}",
                        }
                    order_col = None
                    for c in preferred_order_by.get(table_name, []):
                        if c in col_names:
                            order_col = c
                            break
                    if order_col:
                        order_clause = f'"{order_col}" DESC'
                    elif _is_sqlite_conn(conn):
                        order_clause = "rowid DESC"
                    else:
                        order_clause = "1 DESC"

                    # Pull one extra row to derive has_more without a separate COUNT.
                    if _is_sqlite_conn(conn):
                        cursor = conn.execute(
                            f'SELECT * FROM "{table_name}"{scope_where} ORDER BY {order_clause} LIMIT ? OFFSET ?',
                            scope_params + (limit + 1, offset),
                        )
                        all_rows = [dict(r) for r in cursor.fetchall()]
                    else:
                        pg_scope_where = scope_where.replace("?", "%s")
                        cursor = conn.execute(
                            f'SELECT * FROM "{table_name}"{pg_scope_where} ORDER BY {order_clause} LIMIT %s OFFSET %s',
                            scope_params + (limit + 1, offset),
                        )
                        col_order = [desc[0] for desc in (cursor.description or [])]
                        all_rows = [dict(zip(col_order, row)) for row in cursor.fetchall()]
            else:
                conn = get_db_connection()
                if not conn:
                    return {"id": req_id, "status": "error", "error": "Database connection not available"}
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                    (table_name,),
                ).fetchone()
                if not exists:
                    return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}
                try:
                    cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                    col_names = {str(c["name"]) for c in cols} if cols else set()
                except Exception:
                    col_names = set()
                scope_where, scope_params = _build_uma_scope_clause(
                    col_names=col_names,
                    dataset_id=dataset_id,
                    owner_user_id=owner_uid,
                    tenant_id=tenant_id,
                )
                if not scope_where:
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": f"table not tenant scoped for UMA reads: {table_name}",
                    }
                order_col = None
                for c in preferred_order_by.get(table_name, []):
                    if c in col_names:
                        order_col = c
                        break
                order_clause = f'"{order_col}" DESC' if order_col else "rowid DESC"
                cursor = conn.execute(
                    f'SELECT * FROM "{table_name}"{scope_where} ORDER BY {order_clause} LIMIT ? OFFSET ?',
                    scope_params + (limit + 1, offset),
                )
                all_rows = [dict(r) for r in cursor.fetchall()]
            has_more = len(all_rows) > limit
            rows = all_rows[:limit]
            try:
                transform_diag = {}
                logger.info(
                    "[PIPELINE:UMA][TRANSFORM] req=%s stage=rows:%s start rows=%s",
                    req_id,
                    table_name,
                    len(rows),
                )
                rows = await apply_filter_manifest_async(
                    rows,
                    filter_manifest,
                    field_transforms=field_transforms,
                    table_id=table_name,
                    diagnostics=transform_diag,
                    progress_hook=_uma_transform_progress_hook(req_id, f"rows:{table_name}"),
                )
                logger.info(
                    "[PIPELINE:UMA][TRANSFORM] req=%s stage=rows:%s done applied=%s skipped=%s reasons=%s",
                    req_id,
                    table_name,
                    transform_diag.get("applied_count", 0),
                    transform_diag.get("skipped_count", 0),
                    transform_diag.get("skip_reasons", {}),
                )
            except UMAFilterError as exc:
                return {"id": req_id, "status": "error", "error": str(exc)}
            if owner_uid:
                record_uma_request(
                    conn,
                    owner_user_id=owner_uid,
                    resource_id=resource_id,
                    request_type="read",
                    endpoint=f"rows:{table_name}",
                    requesting_user_id=payload.get("requesting_user_id"),
                    app_id=payload.get("requesting_app_id"),
                    access_channel=(payload.get("access_channel") or "http").strip() or "http",
                )
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "rows": rows,
                    "table_name": table_name,
                    "applied_limit": limit,
                    "has_more": bool(has_more),
                    "next_offset": (offset + limit) if has_more else None,
                    "cap_reason": cap_reason,
                    "debug_metadata": {"field_transforms": transform_diag},
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PIPELINE:UMA] uma_get_rows error: %s", exc)
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "list_jsonl_files":
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

    if msg_type == "delete_jsonl_file":
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

    if msg_type == "read_jsonl_file":
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

    if msg_type == "messenger_contact_graph":
        payload = message.get("payload") or {}
        dataset_id = (payload.get("dataset_id") or "").strip()
        if not dataset_id:
            return {"id": req_id, "status": "error", "error": "dataset_id required"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        try:
            source_ids = payload.get("source_ids")
            if not isinstance(source_ids, list):
                source_ids = ["imessage", "signal"]
            graph = _build_messenger_contact_graph(
                conn,
                dataset_id=dataset_id,
                source_ids=[str(s).strip() for s in source_ids if str(s).strip()],
                max_messages=int(payload.get("max_messages") or 25000),
                max_nodes=int(payload.get("max_nodes") or 40),
                include_broadcast_edges=bool(payload.get("include_broadcast_edges", True)),
            )
            return {"id": req_id, "status": "ok", "payload": {"status": "ok", **graph}}
        except Exception as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "messenger_analytics_recompute":
        payload = message.get("payload") or {}
        dataset_id = (payload.get("dataset_id") or "").strip()
        if not dataset_id:
            return {"id": req_id, "status": "error", "error": "dataset_id required"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        source_filter = _normalize_messenger_source_filter(payload)
        try:
            result = compute_and_persist_messenger_analytics(
                dataset_id=dataset_id,
                conn=conn,
                start_ts=(payload.get("start_ts") or None),
                end_ts=(payload.get("end_ts") or None),
                source_ids=source_filter or None,
                period_granularity=str(payload.get("period_granularity") or "month"),
                cumulative=bool(payload.get("cumulative", False)),
            )
            return {"id": req_id, "status": "ok", "payload": {"status": "ok", **result}}
        except Exception as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "messenger_analytics_sources":
        payload = message.get("payload") or {}
        dataset_id = (payload.get("dataset_id") or "").strip()
        if not dataset_id:
            return {"id": req_id, "status": "error", "error": "dataset_id required"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        rows = conn.execute(
            """
            SELECT DISTINCT source_id
            FROM conversation_messages
            WHERE dataset_id = ?
            ORDER BY source_id
            """,
            (dataset_id,),
        ).fetchall()
        sources = [str(r["source_id"]) for r in rows if r and r["source_id"]]
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id, "sources": sources}}

    if msg_type in {"messenger_analytics_periods", "messenger_analytics_graph", "messenger_analytics_importance", "messenger_analytics_communities"}:
        payload = message.get("payload") or {}
        dataset_id = (payload.get("dataset_id") or "").strip()
        if not dataset_id:
            return {"id": req_id, "status": "error", "error": "dataset_id required"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        ensure_messenger_analytics_tables(conn)
        source_filter = _normalize_messenger_source_filter(payload)
        source_scope = _messenger_source_scope(source_filter)
        period_key = (payload.get("period") or "").strip()

        try:
            if bool(payload.get("ensure_data", True)):
                where_period = "AND period_key = ?" if period_key else ""
                params = [dataset_id, source_scope] + ([period_key] if period_key else [])
                row = conn.execute(
                    f"""
                    SELECT 1
                    FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
                    WHERE dataset_id = ? AND source_scope = ? {where_period}
                    LIMIT 1
                    """,
                    tuple(params),
                ).fetchone()
                if not row:
                    compute_and_persist_messenger_analytics(
                        dataset_id=dataset_id,
                        conn=conn,
                        source_ids=source_filter or None,
                        period_granularity="month",
                    )

            if msg_type == "messenger_analytics_periods":
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT period_key
                    FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
                    WHERE dataset_id = ? AND source_scope = ?
                    ORDER BY period_key
                    """,
                    (dataset_id, source_scope),
                ).fetchall()
                periods = [str(r["period_key"]) for r in rows if r and r["period_key"]]
                return {
                    "id": req_id,
                    "status": "ok",
                    "payload": {"status": "ok", "dataset_id": dataset_id, "source_scope": source_scope, "periods": periods},
                }

            if not period_key:
                return {"id": req_id, "status": "error", "error": "period required"}

            if msg_type == "messenger_analytics_graph":
                edge_rows = conn.execute(
                    f"""
                    SELECT source_id, target_id, weight, edge_type, edge_type_counts_json
                    FROM {MESSENGER_SOCIAL_EDGES_TABLE}
                    WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
                    ORDER BY source_id, target_id
                    """,
                    (dataset_id, period_key, source_scope),
                ).fetchall()
                node_rows = conn.execute(
                    f"""
                    SELECT i.participant_id, i.centrality_degree, c.community_id
                    FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE} i
                    LEFT JOIN {MESSENGER_COMMUNITIES_TABLE} c
                      ON c.dataset_id = i.dataset_id
                     AND c.period_key = i.period_key
                     AND c.source_scope = i.source_scope
                     AND c.participant_id = i.participant_id
                    WHERE i.dataset_id = ? AND i.period_key = ? AND i.source_scope = ?
                    ORDER BY i.centrality_degree DESC, i.participant_id
                    """,
                    (dataset_id, period_key, source_scope),
                ).fetchall()
                labels_by_participant = resolve_participant_labels(
                    conn,
                    dataset_id=dataset_id,
                    participant_ids=[str(r["participant_id"]) for r in node_rows if r and r["participant_id"]],
                )
                nodes = [
                    {
                        "id": str(r["participant_id"]),
                        "label": labels_by_participant.get(str(r["participant_id"]), {}).get("label", str(r["participant_id"])),
                        "display_name": labels_by_participant.get(str(r["participant_id"]), {}).get("display_name"),
                        "identifier": labels_by_participant.get(str(r["participant_id"]), {}).get("identifier"),
                        "importance": float(r["centrality_degree"] or 0.0),
                        "community_id": r["community_id"],
                    }
                    for r in node_rows
                ]
                edges = []
                for row in edge_rows:
                    counts = {}
                    raw = row["edge_type_counts_json"]
                    if raw:
                        try:
                            counts = json.loads(raw)
                        except Exception:
                            counts = {}
                    edges.append(
                        {
                            "source": row["source_id"],
                            "target": row["target_id"],
                            "weight": float(row["weight"] or 0.0),
                            "edge_type": row["edge_type"],
                            "edge_type_counts": counts,
                        }
                    )
                return {
                    "id": req_id,
                    "status": "ok",
                    "payload": {
                        "status": "ok",
                        "dataset_id": dataset_id,
                        "period": period_key,
                        "source_scope": source_scope,
                        "nodes": nodes,
                        "edges": edges,
                    },
                }

            if msg_type == "messenger_analytics_importance":
                rows = conn.execute(
                    f"""
                    SELECT participant_id, centrality_degree, centrality_betweenness
                    FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
                    WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
                    ORDER BY centrality_degree DESC, centrality_betweenness DESC
                    """,
                    (dataset_id, period_key, source_scope),
                ).fetchall()
                labels_by_participant = resolve_participant_labels(
                    conn,
                    dataset_id=dataset_id,
                    participant_ids=[str(row["participant_id"]) for row in rows if row and row["participant_id"]],
                )
                importance = []
                for row in rows:
                    participant_id = str(row["participant_id"])
                    labels = labels_by_participant.get(participant_id, {})
                    importance.append(
                        {
                            "participant_id": participant_id,
                            "participant_label": labels.get("label", participant_id),
                            "participant_display_name": labels.get("display_name"),
                            "participant_identifier": labels.get("identifier"),
                            "centrality_degree": float(row["centrality_degree"] or 0.0),
                            "centrality_betweenness": float(row["centrality_betweenness"] or 0.0),
                        }
                    )
                return {
                    "id": req_id,
                    "status": "ok",
                    "payload": {
                        "status": "ok",
                        "dataset_id": dataset_id,
                        "period": period_key,
                        "source_scope": source_scope,
                        "importance": importance,
                    },
                }

            rows = conn.execute(
                f"""
                SELECT participant_id, community_id
                FROM {MESSENGER_COMMUNITIES_TABLE}
                WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
                ORDER BY community_id, participant_id
                """,
                (dataset_id, period_key, source_scope),
            ).fetchall()
            labels_by_participant = resolve_participant_labels(
                conn,
                dataset_id=dataset_id,
                participant_ids=[str(row["participant_id"]) for row in rows if row and row["participant_id"]],
            )
            grouped: Dict[int, List[str]] = {}
            for row in rows:
                cid = int(row["community_id"])
                grouped.setdefault(cid, []).append(str(row["participant_id"]))
            communities = [
                {
                    "community_id": cid,
                    "participants": participants,
                    "participants_labeled": [
                        {
                            "id": pid,
                            "label": labels_by_participant.get(str(pid), {}).get("label", pid),
                        }
                        for pid in participants
                    ],
                }
                for cid, participants in sorted(grouped.items(), key=lambda item: item[0])
            ]
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "status": "ok",
                    "dataset_id": dataset_id,
                    "period": period_key,
                    "source_scope": source_scope,
                    "communities": communities,
                },
            }
        except Exception as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}

    # Messenger ingestion: source settings and sync (forwarded from Control Plane)
    if msg_type == "get_source_settings":
        payload = message.get("payload") or {}
        source_id = (payload.get("source_id") or "").strip()
        dataset_id = (payload.get("dataset_id") or "").strip()
        if not source_id or not dataset_id:
            return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
        if not REGISTRY.get(source_id):
            return {"id": req_id, "status": "error", "error": "unknown source_id"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        settings_data = get_source_settings(conn, dataset_id, source_id)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id, "source_id": source_id, **settings_data}}

    if msg_type == "put_source_settings":
        payload = message.get("payload") or {}
        source_id = (payload.get("source_id") or "").strip()
        dataset_id = (payload.get("dataset_id") or "").strip()
        enabled = payload.get("enabled")
        if not source_id or not dataset_id:
            return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
        source = REGISTRY.get(source_id)
        if not source or getattr(source, "source_type", None) != "local_sync":
            return {"id": req_id, "status": "error", "error": "settings only apply to local_sync sources"}
        if enabled is None:
            return {"id": req_id, "status": "error", "error": "enabled required in body"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        put_source_settings(conn, dataset_id, source_id, enabled=enabled)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id, "source_id": source_id, "enabled": bool(enabled)}}

    if msg_type == "get_source_contacts":
        payload = message.get("payload") or {}
        source_id = (payload.get("source_id") or "").strip()
        dataset_id = (payload.get("dataset_id") or "").strip()
        if not source_id or not dataset_id:
            return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
        source = REGISTRY.get(source_id)
        if not source or getattr(source, "source_type", None) != "local_sync":
            return {"id": req_id, "status": "error", "error": "contacts only apply to local_sync sources"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        from ..storage.canonical import ConversationsTablesManager
        manager = ConversationsTablesManager(conn)
        contacts = manager.list_contacts(dataset_id=dataset_id, source_id=source_id, limit=int(payload.get("limit") or 200))
        enrich_contact_rows_with_resolved_display_names(conn, dataset_id=dataset_id, contacts=contacts)
        for c in contacts:
            identifier = c.get("identifier")
            if identifier:
                pid = str(identifier)
                c["sample_messages"] = manager.get_contact_message_samples(
                    dataset_id=dataset_id,
                    source_id=source_id,
                    identifier=identifier,
                    limit=5,
                )
                previews = manager.get_contact_conversation_thread_previews(
                    dataset_id=dataset_id,
                    source_id=source_id,
                    profile_identifier=pid,
                )
                enrich_conversation_thread_previews(
                    conn,
                    dataset_id=dataset_id,
                    profile_identifier=pid,
                    previews=previews,
                )
                c["conversation_thread_previews"] = previews
            else:
                c["conversation_thread_previews"] = []
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "status": "ok",
                "dataset_id": dataset_id,
                "source_id": source_id,
                "contacts": contacts,
            },
        }

    if msg_type == "put_source_contact":
        payload = message.get("payload") or {}
        source_id = (payload.get("source_id") or "").strip()
        dataset_id = (payload.get("dataset_id") or "").strip()
        contact_id = (payload.get("contact_id") or "").strip()
        display_name = payload.get("display_name")
        sharing_policy = payload.get("sharing_policy")
        if not source_id or not dataset_id or not contact_id:
            return {"id": req_id, "status": "error", "error": "source_id, dataset_id, contact_id required"}
        source = REGISTRY.get(source_id)
        if not source or getattr(source, "source_type", None) != "local_sync":
            return {"id": req_id, "status": "error", "error": "contacts only apply to local_sync sources"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        from ..storage.canonical import ConversationsTablesManager
        manager = ConversationsTablesManager(conn)
        manager.update_contact_display_name(
            dataset_id=dataset_id,
            source_id=source_id,
            contact_id=contact_id,
            display_name=display_name,
        )
        if isinstance(sharing_policy, dict) and sharing_policy:
            manager.update_contact_sharing_policy(
                dataset_id=dataset_id,
                contact_id=contact_id,
                sharing_policy=sharing_policy,
            )
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "status": "ok",
                "dataset_id": dataset_id,
                "source_id": source_id,
                "contact_id": contact_id,
                "display_name": display_name,
                "sharing_policy": sharing_policy if isinstance(sharing_policy, dict) else None,
            },
        }

    if msg_type == "auto_resolve_source_contacts":
        payload = message.get("payload") or {}
        source_id = (payload.get("source_id") or "").strip()
        dataset_id = (payload.get("dataset_id") or "").strip()
        if not source_id or not dataset_id:
            return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
        source = REGISTRY.get(source_id)
        if not source or getattr(source, "source_type", None) != "local_sync":
            return {"id": req_id, "status": "error", "error": "contacts only apply to local_sync sources"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        from ..storage.canonical import ConversationsTablesManager
        manager = ConversationsTablesManager(conn)
        updated = manager.auto_resolve_contact_names(dataset_id=dataset_id, source_id=source_id)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "status": "ok",
                "dataset_id": dataset_id,
                "source_id": source_id,
                "updated_contacts": updated,
            },
        }

    if msg_type in {"import_apple_contacts", "import_contacts_apple_global"}:
        payload = message.get("payload") or {}
        dataset_id, target_sources, err = _resolve_contact_import_targets(payload)
        if err:
            return {"id": req_id, "status": "error", "error": err}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        try:
            logger.info(
                "[CONTACT_IMPORT] engine request start: type=apple dataset_id=%s contact_namespaces=%s req_id=%s",
                dataset_id,
                target_sources,
                req_id[:8] if req_id else "?",
            )
            from ..ingestion.sources.contact_importers import import_apple_contacts_local
            from ..storage.canonical import ConversationsTablesManager

            contacts = await asyncio.to_thread(import_apple_contacts_local)
            manager = ConversationsTablesManager(conn)
            aggregate = manager.import_contacts_batch(
                dataset_id=dataset_id,
                contacts=contacts,
                target_sources=target_sources,
                import_source="apple_contacts",
                import_run_id=req_id,
            )
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "status": "ok",
                    "dataset_id": dataset_id,
                    "applied_sources": target_sources,
                    "import_source": "apple_contacts",
                    "contacts_discovered": len(contacts),
                    **aggregate,
                },
            }
        except Exception as exc:
            logger.exception(
                "[CONTACT_IMPORT] engine request failed: type=apple dataset_id=%s contact_namespaces=%s req_id=%s error=%s",
                dataset_id,
                target_sources,
                req_id[:8] if req_id else "?",
                str(exc),
            )
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type in {"import_google_contacts_token", "import_google_contacts_token_global"}:
        payload = message.get("payload") or {}
        dataset_id, target_sources, err = _resolve_contact_import_targets(payload)
        if err:
            return {"id": req_id, "status": "error", "error": err}
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            return {"id": req_id, "status": "error", "error": "access_token required"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        try:
            logger.info(
                "[CONTACT_IMPORT] engine request start: type=google_token dataset_id=%s contact_namespaces=%s req_id=%s",
                dataset_id,
                target_sources,
                req_id[:8] if req_id else "?",
            )
            from ..ingestion.sources.contact_importers import import_google_contacts
            from ..storage.canonical import ConversationsTablesManager

            contacts = await asyncio.to_thread(import_google_contacts, access_token)
            manager = ConversationsTablesManager(conn)
            aggregate = manager.import_contacts_batch(
                dataset_id=dataset_id,
                contacts=contacts,
                target_sources=target_sources,
                import_source="google_contacts_oauth",
                import_run_id=req_id,
            )
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "status": "ok",
                    "dataset_id": dataset_id,
                    "applied_sources": target_sources,
                    "import_source": "google_contacts_oauth",
                    "contacts_discovered": len(contacts),
                    **aggregate,
                },
            }
        except Exception as exc:
            logger.exception(
                "[CONTACT_IMPORT] engine request failed: type=google_token dataset_id=%s contact_namespaces=%s req_id=%s error=%s",
                dataset_id,
                target_sources,
                req_id[:8] if req_id else "?",
                str(exc),
            )
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type in {"start_google_contacts_import", "start_google_contacts_import_global"}:
        payload = message.get("payload") or {}
        dataset_id, target_sources, err = _resolve_contact_import_targets(payload)
        if err:
            return {"id": req_id, "status": "error", "error": err}
        google_client_id = (payload.get("google_client_id") or os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
        if not google_client_id:
            return {"id": req_id, "status": "error", "error": "google_client_id required (or set GOOGLE_OAUTH_CLIENT_ID)"}
        try:
            logger.info(
                "[CONTACT_IMPORT] engine request start: type=google_start dataset_id=%s contact_namespaces=%s req_id=%s",
                dataset_id,
                target_sources,
                req_id[:8] if req_id else "?",
            )
            from ..ingestion.sources.contact_importers import start_google_device_auth

            auth = await asyncio.to_thread(start_google_device_auth, google_client_id)
            if auth.get("error"):
                return {"id": req_id, "status": "error", "error": auth.get("error_description") or auth.get("error")}
            session_id = str(uuid.uuid4())
            _GOOGLE_CONTACT_IMPORT_SESSIONS[session_id] = {
                "dataset_id": dataset_id,
                "apply_to_sources": target_sources,
                "google_client_id": google_client_id,
                "device_code": auth.get("device_code"),
                "interval": int(auth.get("interval") or 5),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "status": "ok",
                    "session_id": session_id,
                    "applied_sources": target_sources,
                    "user_code": auth.get("user_code"),
                    "verification_url": auth.get("verification_url") or auth.get("verification_uri"),
                    "expires_in": auth.get("expires_in"),
                    "message": auth.get("message"),
                },
            }
        except Exception as exc:
            logger.exception(
                "[CONTACT_IMPORT] engine request failed: type=google_start dataset_id=%s contact_namespaces=%s req_id=%s error=%s",
                dataset_id,
                target_sources,
                req_id[:8] if req_id else "?",
                str(exc),
            )
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type in {"finish_google_contacts_import", "finish_google_contacts_import_global"}:
        payload = message.get("payload") or {}
        session_id = (payload.get("session_id") or "").strip()
        if not session_id:
            return {"id": req_id, "status": "error", "error": "session_id required"}
        session = _GOOGLE_CONTACT_IMPORT_SESSIONS.get(session_id)
        if not session:
            return {"id": req_id, "status": "error", "error": "google import session not found or expired"}
        if (
            (payload.get("dataset_id") or "").strip() != (session.get("dataset_id") or "")
        ):
            return {"id": req_id, "status": "error", "error": "session does not match dataset_id"}
        dataset_id = session.get("dataset_id")
        target_sources = session.get("apply_to_sources") or []
        google_client_id = session.get("google_client_id")
        device_code = session.get("device_code")
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        try:
            logger.info(
                "[CONTACT_IMPORT] engine request start: type=google_finish dataset_id=%s contact_namespaces=%s session_id=%s req_id=%s",
                dataset_id,
                target_sources,
                session_id[:8],
                req_id[:8] if req_id else "?",
            )
            from ..ingestion.sources.contact_importers import finish_google_device_auth, import_google_contacts
            from ..storage.canonical import ConversationsTablesManager

            token = await asyncio.to_thread(
                finish_google_device_auth,
                client_id=google_client_id,
                device_code=device_code,
                interval_seconds=int(session.get("interval") or 5),
                timeout_seconds=int(payload.get("timeout_seconds") or 120),
            )
            if token.get("error"):
                return {"id": req_id, "status": "error", "error": token.get("error_description") or token.get("error")}
            access_token = token.get("access_token")
            if not access_token:
                return {"id": req_id, "status": "error", "error": "Google token exchange did not return access_token"}
            contacts = await asyncio.to_thread(import_google_contacts, access_token)
            manager = ConversationsTablesManager(conn)
            aggregate = manager.import_contacts_batch(
                dataset_id=dataset_id,
                contacts=contacts,
                target_sources=target_sources,
                import_source="google_contacts",
                import_run_id=req_id,
            )
            _GOOGLE_CONTACT_IMPORT_SESSIONS.pop(session_id, None)
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "status": "ok",
                    "dataset_id": dataset_id,
                    "applied_sources": target_sources,
                    "import_source": "google_contacts",
                    "contacts_discovered": len(contacts),
                    **aggregate,
                },
            }
        except Exception as exc:
            logger.exception(
                "[CONTACT_IMPORT] engine request failed: type=google_finish dataset_id=%s contact_namespaces=%s session_id=%s req_id=%s error=%s",
                dataset_id,
                target_sources,
                session_id[:8],
                req_id[:8] if req_id else "?",
                str(exc),
            )
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "source_sync":
        import asyncio as _asyncio
        payload = message.get("payload") or {}
        source_id = (payload.get("source_id") or "").strip()
        dataset_id = (payload.get("dataset_id") or "").strip()
        sync_options = payload.get("sync_options")
        if source_id == "imessage" and not sync_options:
            sync_options = {"mode": "3m"}
        logger.info(
            "[PIPELINE:SYNC] Engine received source_sync request: source_id=%s dataset_id=%s sync_options=%s (req_id=%s)",
            source_id,
            dataset_id[:24] + "..." if dataset_id and len(dataset_id) > 24 else dataset_id,
            sync_options,
            req_id[:8] if req_id else "?",
        )
        if not source_id or not dataset_id:
            return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
        source = REGISTRY.get(source_id)
        if not source or getattr(source, "source_type", None) != "local_sync":
            return {"id": req_id, "status": "error", "error": "sync only applies to local_sync sources"}
        from ..ingestion.local_sync import run_imessage_sync, run_signal_sync
        conn = get_db_connection()
        try:
            if source_id == "imessage":
                result = await _asyncio.to_thread(run_imessage_sync, dataset_id, sync_options=sync_options)
            elif source_id == "signal":
                result = await _asyncio.to_thread(run_signal_sync, dataset_id, sync_options=sync_options)
            else:
                return {"id": req_id, "status": "error", "error": f"sync not implemented for source_id={source_id}"}
            status = result.get("status", "error")
            error = result.get("error")
            if status == "error" and error and ("84" in str(error) or "EOVERFLOW" in str(error).upper()):
                logger.warning(
                    "[PIPELINE:SYNC] source_sync returned errno 84 / EOVERFLOW: source_id=%s error=%s",
                    source_id,
                    error,
                    exc_info=False,
                )
            logger.info("[PIPELINE:SYNC] source_sync completed: source_id=%s status=%s", source_id, status)
            if conn and status == "ok":
                update_sync_result(conn, dataset_id, source_id, success=True, last_sync_at=datetime.now(timezone.utc).isoformat())
            elif conn and status == "error":
                update_sync_result(conn, dataset_id, source_id, success=False, last_error=error or "Sync failed")
            return {"id": req_id, "status": status, "payload": result, "error": error}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PIPELINE:SYNC] source_sync raised exception: source_id=%s exc=%s",
                source_id,
                exc,
                exc_info=True,
            )
            if conn:
                update_sync_result(conn, dataset_id, source_id, success=False, last_error=str(exc))
            return {"id": req_id, "status": "error", "error": str(exc)}

    if msg_type == "get_signal_settings":
        payload = message.get("payload") or {}
        dataset_id = (payload.get("dataset_id") or "").strip()
        if not dataset_id:
            return {"id": req_id, "status": "error", "error": "dataset_id required"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        identity = get_signal_identity(conn, dataset_id)
        if identity is None:
            return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id, "my_phone_number": None, "my_signal_id": None}}
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id, **identity}}

    if msg_type == "put_signal_settings":
        payload = message.get("payload") or {}
        dataset_id = (payload.get("dataset_id") or "").strip()
        my_phone_number = payload.get("my_phone_number")
        my_signal_id = payload.get("my_signal_id")
        if not dataset_id:
            return {"id": req_id, "status": "error", "error": "dataset_id required"}
        conn = get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database not available"}
        put_signal_identity(conn, dataset_id, my_phone_number=my_phone_number, my_signal_id=my_signal_id)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id}}

    if msg_type == "signal_upload":
        import asyncio as _asyncio
        payload = message.get("payload") or {}
        dataset_id = (payload.get("dataset_id") or "").strip()
        file_b64 = payload.get("file_base64")
        my_phone_number = payload.get("my_phone_number")
        owner_user_id = payload.get("owner_user_id")
        if not dataset_id:
            return {"id": req_id, "status": "error", "error": "dataset_id required"}
        if not file_b64:
            return {"id": req_id, "status": "error", "error": "file required (file_base64 in payload)"}
        try:
            file_bytes = base64.b64decode(file_b64)
        except Exception as e:
            return {"id": req_id, "status": "error", "error": f"Invalid file_base64: {e}"}
        from ..ingestion.local_sync import run_signal_upload
        try:
            result = await _asyncio.to_thread(run_signal_upload, dataset_id, file_bytes, my_phone_number=my_phone_number, owner_user_id=owner_user_id)
            return {"id": req_id, "status": result.get("status", "ok"), "payload": result, "error": result.get("error")}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "status": "error", "error": str(exc)}

    return {"id": req_id, "status": "error", "error": "unhandled message type"}
