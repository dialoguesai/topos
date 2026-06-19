from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from .checkpoints.checkpoint_store import CheckpointStore, IngestionCheckpoint
from .parser import parse_file
from .parsers import PARSER_REGISTRY
from .progress import IngestionProgress
from .sources.base import RawRecord
from .state_machine import IngestionJob
from ..canonicalization.mappers import MAPPER_REGISTRY
from ..config.settings import settings
from ..enrichment.derived_tables import DerivedTablesManager
from ..enrichment.jobs import CANONICAL_JOBS
from ..enrichment.orchestrator import EnrichmentOrchestrator
from ..enrichment.progress_bar import ProgressBar
from ..engine.usage_observation import emit_usage_observation
from ..sources.registry import REGISTRY
from ..storage.db.postgres import connect_postgres
from ..storage.raw.file_store import RawFileStore
from ..utils.base_object import BaseObject

logger = logging.getLogger("topos.ingestion.manager")


def _owner_user_id_from_dataset_id(dataset_id: Optional[str]) -> Optional[str]:
    raw = str(dataset_id or "").strip()
    if not raw or ":" not in raw:
        return None
    owner = raw.split(":", 1)[0].strip()
    return owner or None


def _control_plane_base_url(raw_url: Optional[str]) -> str:
    value = str(raw_url or "").strip()
    if value.startswith("wss://"):
        return value.replace("wss://", "https://").split("/ws/")[0]
    if value.startswith("ws://"):
        return value.replace("ws://", "http://").split("/ws/")[0]
    return value.rstrip("/")


def _filter_unenriched_messages(
    canonical_messages: List[Dict[str, Any]],
    job_names: List[str],
    tables_manager: DerivedTablesManager,
    *,
    source_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filter out messages that have already been enriched.
    
    Args:
        canonical_messages: List of canonical message dictionaries
        job_names: List of enrichment job names to check
        tables_manager: DerivedTablesManager instance for database access
        
    Returns:
        List of messages that haven't been enriched yet
    """
    if not canonical_messages or not job_names:
        return canonical_messages
    
    if not tables_manager.conn:
        # No database connection, can't check - return all messages
        logger.debug("[PIPELINE:ENRICHMENT] No database connection, processing all messages")
        return canonical_messages
    
    # Create mapping from job name to table name
    job_to_table = {job.get_job_name(): job.get_derived_table() for job in CANONICAL_JOBS}
    
    # Get set of message IDs that are already enriched for any of the jobs.
    # Scope checks by source_id and (when available) dataset owner so one source/user
    # does not suppress enrichment for another when message_id collides.
    enriched_message_ids: set[str] = set()
    candidate_ids = sorted(
        {str(msg.get("message_id") or "").strip() for msg in canonical_messages if str(msg.get("message_id") or "").strip()}
    )
    if not candidate_ids:
        return canonical_messages
    owner_user_id = ""
    if dataset_id:
        owner_user_id = dataset_id.split(":", 1)[0].strip() if ":" in dataset_id else str(dataset_id).strip()
    
    for job_name in job_names:
        table_name = job_to_table.get(job_name)
        if not table_name:
            logger.warning("[PIPELINE:ENRICHMENT] Unknown job name: %s, skipping check", job_name)
            continue
        
        try:
            # Check if table exists
            cursor = tables_manager.conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name=?
            """, (table_name,))
            if not cursor.fetchone():
                # Table doesn't exist yet, no messages are enriched
                continue
            
            placeholders = ",".join("?" for _ in candidate_ids)

            # Prefer scoped join against canonical tables when present.
            params: list[Any] = []
            if source_id:
                if owner_user_id:
                    cursor = tables_manager.conn.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table' AND name='ai_chat_conversations'
                        """
                    )
                    has_conversations = cursor.fetchone() is not None
                    if has_conversations:
                        params = [source_id, owner_user_id, *candidate_ids]
                        cursor = tables_manager.conn.execute(
                            f"""
                            SELECT DISTINCT d.message_id
                            FROM {table_name} d
                            INNER JOIN ai_chat_messages m ON m.message_id = d.message_id
                            INNER JOIN ai_chat_conversations c ON c.conversation_id = m.conversation_id
                            WHERE m.source_id = ? AND c.owner_user_id = ? AND d.message_id IN ({placeholders})
                            """,
                            tuple(params),
                        )
                    else:
                        params = [source_id, *candidate_ids]
                        cursor = tables_manager.conn.execute(
                            f"""
                            SELECT DISTINCT d.message_id
                            FROM {table_name} d
                            INNER JOIN ai_chat_messages m ON m.message_id = d.message_id
                            WHERE m.source_id = ? AND d.message_id IN ({placeholders})
                            """,
                            tuple(params),
                        )
                else:
                    params = [source_id, *candidate_ids]
                    cursor = tables_manager.conn.execute(
                        f"""
                        SELECT DISTINCT d.message_id
                        FROM {table_name} d
                        INNER JOIN ai_chat_messages m ON m.message_id = d.message_id
                        WHERE m.source_id = ? AND d.message_id IN ({placeholders})
                        """,
                        tuple(params),
                    )
            else:
                cursor = tables_manager.conn.execute(
                    f"SELECT DISTINCT message_id FROM {table_name} WHERE message_id IN ({placeholders})",
                    tuple(candidate_ids),
                )
            enriched_message_ids.update(str(row[0]) for row in cursor.fetchall() if row and row[0])
        except Exception as e:
            logger.warning(
                "[PIPELINE:ENRICHMENT] Failed to check enriched messages in %s: %s",
                table_name,
                e,
            )
            # On error, assume no messages are enriched (safer to process than skip)
            continue
    
    # Filter to only messages that haven't been enriched
    unenriched = [
        msg for msg in canonical_messages
        if msg.get("message_id") not in enriched_message_ids
    ]
    
    if len(unenriched) < len(canonical_messages):
        logger.debug(
            "[PIPELINE:ENRICHMENT] Filtered %d already-enriched messages, %d new messages to process",
            len(canonical_messages) - len(unenriched),
            len(unenriched),
        )
    
    return unenriched


async def _read_file_bytes(file_path: Path) -> AsyncIterator[bytes]:
    def read_all() -> bytes:
        return file_path.read_bytes()

    file_data = await asyncio.to_thread(read_all)
    chunk_size = 8192
    for i in range(0, len(file_data), chunk_size):
        yield file_data[i : i + chunk_size]


_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_valid_sql_identifier(value: str) -> bool:
    return bool(_SQL_IDENTIFIER_RE.match(value or ""))


def _sql_type_for_source_column(column_type: str) -> str:
    ctype = str(column_type or "").strip().lower()
    if ctype in {"identifier", "text"}:
        return "TEXT"
    if ctype in {"real", "float", "number"}:
        return "REAL"
    if ctype in {"integer", "int"}:
        return "INTEGER"
    if ctype in {"json"}:
        return "TEXT"
    return "TEXT"


def _coerce_table_value(value: Any, *, declared_type: str) -> Any:
    ctype = str(declared_type or "").strip().lower()
    if value is None:
        return None
    if ctype == "json":
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True)
    return value


def _tokenize_path(path: str) -> List[str]:
    return [part.strip() for part in str(path).split(".") if part.strip()]


def _walk_path_step(nodes: List[Any], token: str) -> List[Any]:
    out: List[Any] = []
    if token == "*":
        for node in nodes:
            if isinstance(node, dict):
                out.extend(node.values())
            elif isinstance(node, list):
                out.extend(node)
        return out

    list_mode = token.endswith("[*]")
    key = token[:-3] if list_mode else token
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if key not in node:
            continue
        value = node.get(key)
        if list_mode:
            if isinstance(value, list):
                out.extend(value)
            elif value is not None:
                out.append(value)
        else:
            out.append(value)
    return out


def _extract_path_value(payload: Dict[str, Any], path: str) -> Any:
    if not path:
        return payload
    nodes: List[Any] = [payload]
    for token in _tokenize_path(path):
        nodes = _walk_path_step(nodes, token)
        if not nodes:
            return None
    if len(nodes) == 1:
        return nodes[0]
    return nodes


def _expand_file_records(raw_payload: Dict[str, Any], source_def: Optional[Any]) -> List[Dict[str, Any]]:
    if not isinstance(raw_payload, dict):
        return []
    ingest_shape = getattr(source_def, "file_ingest_shape", None) if source_def else None
    if not isinstance(ingest_shape, dict):
        return [raw_payload]
    record_path = str(ingest_shape.get("raw_record_path") or "").strip()
    if not record_path:
        return [raw_payload]
    extracted = _extract_path_value(raw_payload, record_path)
    if isinstance(extracted, list):
        return [item for item in extracted if isinstance(item, dict)]
    if isinstance(extracted, dict):
        return [extracted]
    return []


def _persist_source_data_tables(
    *,
    db_conn: Any,
    source_def: Optional[Any],
    dataset_id: str,
    normalized_records: List[Any],
) -> None:
    # Hosted mode should persist source tables in Postgres so rows survive engine restarts.
    if settings.topos_database_mode == "postgres":
        with connect_postgres() as hosted_conn:
            _persist_source_data_tables_on_connection(
                db_conn=hosted_conn,
                source_def=source_def,
                dataset_id=dataset_id,
                normalized_records=normalized_records,
            )
        return

    _persist_source_data_tables_on_connection(
        db_conn=db_conn,
        source_def=source_def,
        dataset_id=dataset_id,
        normalized_records=normalized_records,
    )


def _persist_source_data_tables_on_connection(
    *,
    db_conn: Any,
    source_def: Optional[Any],
    dataset_id: str,
    normalized_records: List[Any],
) -> None:
    if not db_conn or not source_def:
        return
    if not bool(getattr(source_def, "pipeline_include_data_table", False)):
        return
    tables = getattr(source_def, "tables", None)
    if not isinstance(tables, list) or not tables:
        return
    if not normalized_records:
        return

    owner_user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    dataset_parts = [part for part in str(dataset_id or "").split(":") if part]
    if dataset_parts:
        owner_user_id = dataset_parts[0]
    if len(dataset_parts) >= 3:
        tenant_id = dataset_parts[2]

    pooled_scope_columns: List[Dict[str, Any]] = [
        {"name": "dataset_id", "type": "text"},
        {"name": "owner_user_id", "type": "text"},
        {"name": "tenant_id", "type": "text"},
    ]

    for table in tables:
        if not isinstance(table, dict):
            continue
        table_id = str(table.get("table_id") or "").strip()
        columns = table.get("columns")
        if not table_id or not _is_valid_sql_identifier(table_id):
            logger.warning("[PIPELINE:DATA_TABLE] Skipping invalid table_id=%r", table_id)
            continue
        if not isinstance(columns, list) or not columns:
            continue

        valid_columns: List[Dict[str, Any]] = []
        for column in columns:
            if not isinstance(column, dict):
                continue
            col_name = str(column.get("name") or "").strip()
            if not col_name or not _is_valid_sql_identifier(col_name):
                continue
            valid_columns.append(column)
        existing_names = {str(col.get("name") or "").strip() for col in valid_columns}
        for pooled_col in pooled_scope_columns:
            pooled_name = str(pooled_col["name"])
            if pooled_name in existing_names:
                continue
            valid_columns.append(dict(pooled_col))
            existing_names.add(pooled_name)

        if not valid_columns:
            continue

        defs: List[str] = []
        pk_cols: List[str] = []
        for column in valid_columns:
            col_name = str(column.get("name")).strip()
            col_type = _sql_type_for_source_column(str(column.get("type") or "text"))
            defs.append(f'"{col_name}" {col_type}')
            if bool(column.get("primary_key")):
                pk_cols.append(col_name)
        if pk_cols:
            pk_sql = ", ".join([f'"{name}"' for name in pk_cols])
            defs.append(f"PRIMARY KEY ({pk_sql})")

        db_conn.execute(f'CREATE TABLE IF NOT EXISTS "{table_id}" ({", ".join(defs)})')

        is_sqlite = "sqlite" in db_conn.__class__.__module__.lower()
        try:
            if is_sqlite:
                existing_col_rows = db_conn.execute(f'PRAGMA table_info("{table_id}")').fetchall()
                persisted_columns = {
                    str(row["name"]) if isinstance(row, dict) else str(row[1])
                    for row in existing_col_rows
                }
            else:
                existing_col_rows = db_conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=%s
                    """,
                    (table_id,),
                ).fetchall()
                persisted_columns = {str(row[0]) for row in existing_col_rows}
        except Exception:
            persisted_columns = set()

        for pooled_col in ("dataset_id", "owner_user_id", "tenant_id"):
            if pooled_col in persisted_columns:
                continue
            db_conn.execute(f'ALTER TABLE "{table_id}" ADD COLUMN "{pooled_col}" TEXT')
            persisted_columns.add(pooled_col)

        column_names = [str(column.get("name")).strip() for column in valid_columns]
        quoted_columns = ", ".join([f'"{name}"' for name in column_names])
        placeholder_token = "?" if is_sqlite else "%s"
        placeholders = ", ".join([placeholder_token] * len(column_names))
        if is_sqlite:
            sql = f'INSERT OR REPLACE INTO "{table_id}" ({quoted_columns}) VALUES ({placeholders})'
        else:
            conflict_cols = [name for name in pk_cols if name in column_names]
            non_pk_cols = [name for name in column_names if name not in conflict_cols]
            if conflict_cols:
                conflict_sql = ", ".join([f'"{name}"' for name in conflict_cols])
                if non_pk_cols:
                    update_sql = ", ".join(
                        [f'"{name}" = EXCLUDED."{name}"' for name in non_pk_cols]
                    )
                    sql = (
                        f'INSERT INTO "{table_id}" ({quoted_columns}) VALUES ({placeholders}) '
                        f'ON CONFLICT ({conflict_sql}) DO UPDATE SET {update_sql}'
                    )
                else:
                    sql = (
                        f'INSERT INTO "{table_id}" ({quoted_columns}) VALUES ({placeholders}) '
                        f'ON CONFLICT ({conflict_sql}) DO NOTHING'
                    )
            else:
                sql = f'INSERT INTO "{table_id}" ({quoted_columns}) VALUES ({placeholders})'

        for normalized in normalized_records:
            payload = normalized.payload if hasattr(normalized, "payload") else {}
            if not isinstance(payload, dict):
                continue
            row_values: List[Any] = []
            for column in valid_columns:
                col_name = str(column.get("name")).strip()
                raw_value = payload.get(col_name)
                if raw_value is None and col_name == "dataset_id":
                    raw_value = dataset_id
                if raw_value is None and col_name == "owner_user_id":
                    raw_value = owner_user_id
                if raw_value is None and col_name == "tenant_id":
                    raw_value = tenant_id
                if raw_value is None and col_name == "record_id":
                    raw_value = payload.get("id") or payload.get("message_id")
                row_values.append(_coerce_table_value(raw_value, declared_type=str(column.get("type") or "")))
            db_conn.execute(sql, tuple(row_values))

    db_conn.commit()


async def _try_install_runtime_source_definition_from_control_plane(
    *,
    source_id: Optional[str],
    schema_id: str,
    user_id: Optional[str],
    dataset_id: str,
    progress_api_url: Optional[str],
    progress_api_key: Optional[str],
) -> Optional[Any]:
    """Best-effort source install when runtime registry is stale."""
    cp_base = _control_plane_base_url(progress_api_url or settings.topos_control_plane_url)
    if not cp_base:
        return None
    token = str(progress_api_key or settings.topos_key or "").strip()
    if not token:
        return None
    params = {
        "user_id": str(user_id or "").strip(),
        "dataset_id": str(dataset_id or "").strip(),
    }
    if not params["user_id"] or not params["dataset_id"]:
        return None
    try:
        import httpx
        from ..sources.runtime_install import install_source_definition

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{cp_base}/sources",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            payload = resp.json() if resp.content else {}
        rows = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        wanted_source_id = str(source_id or "").strip()
        wanted_schema = str(schema_id or "").strip()
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_source_id = str(row.get("source_id") or "").strip()
            row_schema_id = str(row.get("schema_id") or "").strip()
            if wanted_source_id and row_source_id != wanted_source_id:
                continue
            if wanted_schema and row_schema_id != wanted_schema:
                continue
            install_source_definition(row)
            installed = REGISTRY.get(row_source_id)
            if installed:
                logger.info(
                    "[PIPELINE:MANAGER] Installed runtime source definition from control-plane: source_id=%s schema_id=%s",
                    row_source_id,
                    row_schema_id,
                )
                return installed
    except Exception as exc:
        logger.warning(
            "[PIPELINE:MANAGER] Failed to install runtime source definition from control-plane (source_id=%s schema_id=%s): %s",
            source_id,
            schema_id,
            exc,
        )
    return None


@dataclass
class IngestionManager(BaseObject):
    file_store: RawFileStore
    checkpoint_store: Optional[CheckpointStore] = None
    
    def __post_init__(self):
        """Initialize BaseObject after dataclass initialization."""
        # Generate name if not set (dataclass doesn't call __init__)
        if not hasattr(self, "_name"):
            from ..utils.base_object import _next_instance_number
            n = _next_instance_number(self.__class__)
            object.__setattr__(self, "_name", f"{self.__class__.__name__}#{n}")
        # Call parent __init__ to ensure BaseObject is properly initialized
        BaseObject.__init__(self, name=getattr(self, "_name", None))

    async def process_job(
        self, 
        job: IngestionJob, 
        source_id: Optional[str] = None,
        progress_api_url: Optional[str] = None,
        progress_api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        file_path = self.file_store.get_file_path(job.dataset_id, job.schema_id)
        if not file_path.exists():
            raise FileNotFoundError(f"Raw file not found: {file_path}")

        parser_cls = PARSER_REGISTRY.get(job.schema_id)
        if not parser_cls:
            raise ValueError(f"No parser registered for schema: {job.schema_id}")

        logger.debug(
            "[PIPELINE:MANAGER] %s: Starting job processing: job_id=%s, dataset_id=%s, schema_id=%s, source_id=%s, file_path=%s",
            self,
            job.job_id,
            job.dataset_id,
            job.schema_id,
            source_id,
            file_path,
        )
        # Instantiate parser with schema_id (for v2 support)
        parser = parser_cls(dataset_id=job.dataset_id, _schema_id=job.schema_id)
        
        # Try to count total records for progress tracking (optional, may be None)
        records_total = None
        try:
            # Count lines in file (approximation for JSONL)
            if file_format == "jsonl":
                with open(file_path, 'rb') as f:
                    records_total = sum(1 for _ in f)
        except Exception:
            pass  # If counting fails, records_total remains None
        
        progress = IngestionProgress(job_id=job.job_id, records_total=records_total)
        progress_context = {
            "user_id": _owner_user_id_from_dataset_id(job.dataset_id),
            "dataset_id": job.dataset_id,
        }
        
        # Send initial progress update
        if progress_api_url and progress_api_key:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(
                        f"{progress_api_url}/v1/ingestion/progress",
                        json={
                            "job_id": job.job_id,
                            **progress_context,
                            "status": "processing",
                            "progress_percent": 0.0,
                            "records_processed": 0,
                            "records_total": records_total,
                            "current_step": "starting",
                        },
                        headers={"Authorization": f"Bearer {progress_api_key}"},
                    )
            except Exception as exc:
                logger.warning("Failed to send initial ingestion progress: %s", exc)

        # Find source definition: use source_id if provided, otherwise find by schema_id
        source_def = None
        if source_id:
            source_def = REGISTRY.get(source_id)
            if source_def:
                logger.info(
                    "[PIPELINE:MANAGER] %s: Using source from source_id=%s: %s (enrichment_trigger=%s)",
                    self,
                    source_id,
                    source_def.display_name,
                    getattr(source_def, "enrichment_trigger", "not_set"),
                )
            else:
                logger.warning(
                    "[PIPELINE:MANAGER] %s: source_id=%s not found in registry, falling back to schema_id lookup",
                    self,
                    source_id,
                )
        
        if not source_def:
            # Fallback: find by schema_id (prefer file type for file ingestion)
            for source in REGISTRY.values():
                if source.schema_id == job.schema_id:
                    # Prefer file type sources for file ingestion
                    if source.source_type == "file":
                        source_def = source
                        logger.debug(
                            "[PIPELINE:MANAGER] %s: Found file source by schema_id: source_id=%s",
                            self,
                            source.source_id,
                        )
                        break
                    elif not source_def:
                        # Keep first match as fallback
                        source_def = source
            if source_def:
                logger.info(
                    "[PIPELINE:MANAGER] %s: Found source by schema_id: source_id=%s, source_type=%s, enrichment_trigger=%s",
                    self,
                    source_def.source_id,
                    source_def.source_type,
                    getattr(source_def, "enrichment_trigger", "not_set"),
                )
            else:
                source_def = await _try_install_runtime_source_definition_from_control_plane(
                    source_id=source_id,
                    schema_id=job.schema_id,
                    user_id=_owner_user_id_from_dataset_id(job.dataset_id),
                    dataset_id=job.dataset_id,
                    progress_api_url=progress_api_url,
                    progress_api_key=progress_api_key,
                )

        # Get canonical mapper if available
        canonical_mapper = None
        if source_def and source_def.canonical_mapper_id:
            mapper_cls = MAPPER_REGISTRY.get(source_def.canonical_mapper_id)
            if mapper_cls:
                canonical_mapper = mapper_cls()

        # Initialize enrichment orchestrator with a real connection, even outside app startup.
        from ..core.state import get_db_connection

        db_conn = get_db_connection()
        tables_manager = DerivedTablesManager(conn=db_conn) if db_conn else None
        enrichment_orchestrator = EnrichmentOrchestrator(tables_manager=tables_manager) if tables_manager else None

        records_processed = 0
        errors: list[dict] = []
        last_record_id: Optional[str] = None
        normalized_records: List[Any] = []

        # Use TUI progress bar for better terminal display (single-line updates)
        # If records_total is None, we'll update it as we go
        pbar = None
        if records_total:
            pbar = ProgressBar(total=records_total, desc=f"{self}: Parsing")
        else:
            # Create progress bar with placeholder total, will update dynamically
            pbar = ProgressBar(total=1000, desc=f"{self}: Parsing")  # Placeholder, will adjust

        try:
            async for raw_payload in parse_file(_read_file_bytes(file_path), job.metadata.get("file_format", "jsonl")):
                expanded_payloads = _expand_file_records(raw_payload, source_def)
                if not expanded_payloads:
                    expanded_payloads = [raw_payload] if isinstance(raw_payload, dict) else []
                for record_payload in expanded_payloads:
                    record_id = (
                        str(record_payload.get("id"))
                        or str(record_payload.get("message_id"))
                        or f"{records_processed + 1}"
                    )
                    raw_content = record_payload.get("content")
                    if isinstance(raw_content, str):
                        content_preview = raw_content[:100]
                    else:
                        content_preview = str(raw_content)[:100]
                    logger.debug(
                        "[PIPELINE:MANAGER] %s: Processing raw record: record_id=%s, content_preview=%s",
                        self,
                        record_id,
                        content_preview,
                    )
                    raw_record = RawRecord(record_id=record_id, payload=record_payload)
                    validation = parser.validate(raw_record)
                    if not validation.is_valid:
                        logger.debug(
                            "[PIPELINE:MANAGER] %s: Validation failed: record_id=%s, errors=%s",
                            self,
                            record_id,
                            validation.errors,
                        )
                        errors.append({"record_id": record_id, "errors": validation.errors})
                        if pbar:
                            pbar.update(1)  # Still count invalid records
                        continue
                    normalized = parser.parse(raw_record)
                    logger.debug(
                        "[PIPELINE:NORMALIZED] Record normalized: record_id=%s, fields=%s",
                        normalized.record_id,
                        sorted(list(normalized.payload.keys()))[:12],
                    )
                    normalized_records.append(normalized)
                    records_processed += 1
                    last_record_id = record_id
                    progress.update(records_processed, current_step="parsing")

                    # Update progress bar
                    if pbar:
                        # If we didn't know total initially, update it now
                        if not records_total and records_processed > pbar.total:
                            # Estimate: assume we're at least 10% done, so total is at least 10x current
                            pbar.total = max(pbar.total, records_processed * 10)
                        pbar.update(1)

                    # Report progress to control plane if configured
                    if progress.should_report() and progress_api_url and progress_api_key:
                        try:
                            import httpx
                            progress_dict = progress.to_dict()
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                await client.post(
                                    f"{progress_api_url}/v1/ingestion/progress",
                                    json={
                                        "job_id": job.job_id,
                                        **progress_context,
                                        **progress_dict,
                                    },
                                    headers={"Authorization": f"Bearer {progress_api_key}"},
                                )
                        except Exception as exc:
                            logger.warning("Failed to send ingestion progress update: %s", exc)

                    if progress.should_report():
                        logger.debug("Ingestion progress: %s", progress.to_dict())
        except Exception:
            # Re-raise exception but ensure progress bar state is preserved
            raise
        finally:
            # Progress bar will be closed at the end of the function
            pass
        
        # Update progress bar: parsing complete, move to canonicalization
        if pbar:
            # Update total if we now know it
            if not records_total and records_processed > 0:
                pbar.total = records_processed
                pbar.n = records_processed  # Set current to match
            pbar.set_description(f"{self}: Canonicalizing")
            pbar._display()  # Force display update

        # Update progress: parsing complete
        if progress_api_url and progress_api_key:
            try:
                import httpx
                progress_dict = progress.to_dict()
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(
                        f"{progress_api_url}/v1/ingestion/progress",
                        json={
                            "job_id": job.job_id,
                            **progress_context,
                            "current_step": "canonicalizing",
                            **progress_dict,
                        },
                        headers={"Authorization": f"Bearer {progress_api_key}"},
                    )
            except Exception as exc:
                logger.warning("Failed to send parsing complete progress: %s", exc)

        # Persist parser output into source-defined logical tables when configured.
        if source_def and db_conn and normalized_records:
            try:
                _persist_source_data_tables(
                    db_conn=db_conn,
                    source_def=source_def,
                    dataset_id=job.dataset_id,
                    normalized_records=normalized_records,
                )
            except Exception as exc:
                logger.error(
                    "[PIPELINE:DATA_TABLE] %s: Failed to persist source table rows: %s",
                    self,
                    exc,
                    exc_info=True,
                )
                errors.append({"step": "source_data_table", "errors": [str(exc)]})
        
        # Canonicalize normalized records: conversations group -> conversation_messages; else engine ai_chat_*
        canonical_messages: List[Dict[str, Any]] = []
        sync_batch_id = str(getattr(job, "job_id", "unknown"))
        if source_def and normalized_records:
            # Build staging records once (same shape for both paths)
            staging_records = []
            for normalized in normalized_records:
                staging_record = {
                    "message_id": normalized.payload.get("message_id"),
                    "dataset_id": job.dataset_id,
                    "thread_id": normalized.payload.get("thread_id") or normalized.payload.get("conversation_id") or job.dataset_id,
                    "ts": normalized.payload.get("ts") or normalized.payload.get("created_at") or str(datetime.now(timezone.utc).timestamp()),
                    "sender_type": normalized.payload.get("sender_type"),
                    "content": normalized.payload.get("content"),
                    "source_id": source_def.source_id,
                }
                if "_metadata" in normalized.payload:
                    staging_record["_metadata"] = normalized.payload["_metadata"]
                staging_records.append(staging_record)

            canonical_group_id = getattr(source_def, "canonical_group_id", None)
            if canonical_group_id == "conversations":
                # Conversations canonical: write only to conversation_messages / conversations (never ai_chat_*)
                from ..core.state import get_db_connection
                from ..storage.canonical import ConversationsTablesManager
                db_conn = get_db_connection()
                if db_conn:
                    conv_manager = ConversationsTablesManager(db_conn)
                    canonical_result = conv_manager.upsert_message_batch(
                        staging_records, job.dataset_id, source_def.source_id, sync_batch_id=sync_batch_id
                    )
                    logger.debug(
                        "[PIPELINE:CANONICAL] %s: Conversations canonical: messages_created=%s, conversations_created=%s",
                        self,
                        canonical_result.get("messages_created", 0),
                        canonical_result.get("conversations_created", 0),
                    )
                for staging_record in staging_records:
                    import json as _json
                    metadata_json = None
                    if "_metadata" in staging_record:
                        metadata_json = _json.dumps(staging_record["_metadata"])
                    canonical_messages.append({
                        "message_id": staging_record.get("message_id"),
                        "conversation_id": staging_record.get("thread_id") or staging_record.get("conversation_id") or job.dataset_id,
                        "sender_type": staging_record.get("sender_type"),
                        "sender_id": None,
                        "ts": staging_record.get("ts"),
                        "content": staging_record.get("content"),
                        "content_rendered": None,
                        "metadata_json": metadata_json,
                        "seq": 0,
                        "source_id": source_def.source_id,
                    })
            elif source_def.canonical_mapper_id:
                # Engine path: ai_chat_messages / ai_chat_conversations
                try:
                    from ..storage.canonical.ai_chat import CanonicalTablesManager, Canonicalizer
                    from ..core.state import get_db_connection

                    db_conn = get_db_connection()
                    canonical_tables_manager = CanonicalTablesManager(db_conn) if db_conn else None
                    if canonical_tables_manager:
                        canonicalizer = Canonicalizer(canonical_tables_manager)
                        mapper_source = source_def.canonical_mapper_id
                        logger.debug(
                            "[PIPELINE:CANONICAL] %s: Canonicalizing %d records with mapper=%s (source_id=%s)",
                            self,
                            len(staging_records),
                            mapper_source,
                            source_def.source_id,
                        )
                        canonical_result = canonicalizer.canonicalize_staging_batch(
                            staging_records,
                            source=mapper_source,
                            batch_size=1000,
                            sync_batch_id=sync_batch_id,
                            mapping_source_id=source_def.source_id,
                        )
                        # Enrichment should consume canonicalized rows (not pre-mapper staging rows).
                        mapped_messages = canonical_result.get("canonical_messages")
                        if isinstance(mapped_messages, list):
                            canonical_messages.extend(
                                [msg for msg in mapped_messages if isinstance(msg, dict)]
                            )
                        logger.debug(
                            "[PIPELINE:CANONICAL] %s: Canonicalization complete: messages_created=%s, conversations_created=%s, canonical_messages_count=%s",
                            self,
                            canonical_result.get("messages_created", 0),
                            canonical_result.get("conversations_created", 0),
                            len(canonical_messages),
                        )
                    else:
                        logger.warning("[PIPELINE:CANONICAL] %s: No database connection, skipping canonicalization", self)
                except ImportError as e:
                    logger.warning("[PIPELINE:CANONICAL] %s: Canonicalization modules not available: %s. Using fallback mapper.", self, e)
                    if canonical_mapper:
                        for normalized in normalized_records:
                            try:
                                canonical = canonical_mapper.map(normalized)
                                if source_def:
                                    canonical.payload["source_id"] = source_def.source_id
                                canonical_messages.append(canonical.payload)
                            except Exception as exc:
                                logger.error("[PIPELINE:CANONICAL] %s: Failed to canonicalize record %s: %s", self, normalized.record_id, exc)
                                errors.append({"record_id": normalized.record_id, "errors": [str(exc)]})
                except Exception as exc:
                    logger.error("[PIPELINE:CANONICAL] %s: Failed to canonicalize records: %s", self, exc, exc_info=True)
                    errors.append({"step": "canonicalization", "errors": [str(exc)]})

            if source_def and normalized_records:
                try:
                    from ..core.state import get_db_connection
                    from ..pipeline.audit import SQLiteIngestAuditStore, StageAuditRow
                    from ..pipeline.stages import PipelineStage

                    db_conn = get_db_connection()
                    if db_conn:
                        audit = SQLiteIngestAuditStore(db_conn)
                        audit.append_stage(
                            StageAuditRow(
                                sync_batch_id=sync_batch_id,
                                source_id=source_def.source_id,
                                stage=PipelineStage.CANONICAL_MAP,
                                status="completed",
                                records_in=len(normalized_records),
                                records_out=len(canonical_messages),
                            )
                        )
                except Exception as exc:
                    logger.debug("[PIPELINE:AUDIT] %s: canonical audit skipped: %s", self, exc)

        if canonical_messages and source_def:
            try:
                import asyncio

                from ..enrichment.orchestrator import SignalDerivationOrchestrator
                from ..pipeline.audit import SQLiteIngestAuditStore, StageAuditRow
                from ..pipeline.stages import PipelineStage

                orchestrator = SignalDerivationOrchestrator()
                derive_result = await orchestrator.run_signal_derivation(
                    canonical_messages,
                    source_id=source_def.source_id,
                    sync_batch_id=sync_batch_id,
                )
                try:
                    from ..core.state import get_db_connection

                    db_conn = get_db_connection()
                    if db_conn:
                        status = "completed" if derive_result.get("jobs_run") else "deferred"
                        if derive_result.get("deferred_jobs"):
                            status = "deferred"
                        SQLiteIngestAuditStore(db_conn).append_stage(
                            StageAuditRow(
                                sync_batch_id=sync_batch_id,
                                source_id=source_def.source_id,
                                stage=PipelineStage.SIGNAL_DERIVE,
                                status=status,
                                records_out=sum(derive_result.get("records_created", {}).values()),
                            )
                        )
                except Exception:
                    pass
            except Exception as exc:
                logger.debug("[PIPELINE:SIGNAL_DERIVE] %s: wave A skipped: %s", self, exc)

        # Run enrichment on canonical messages (only if automatic trigger)
        if canonical_messages and source_def and source_def.canonical_enrichment_jobs:
            # Get enrichment trigger - explicitly check attribute, default to "automatic" if not set
            enrichment_trigger = getattr(source_def, "enrichment_trigger", "automatic")
            
            logger.info(
                "[PIPELINE:ENRICHMENT] %s: Enrichment trigger check: source_id=%s, enrichment_trigger=%s, canonical_messages=%d, jobs=%s",
                self,
                source_def.source_id if source_def else "unknown",
                enrichment_trigger,
                len(canonical_messages),
                source_def.canonical_enrichment_jobs,
            )
            
            # Explicitly check for "manual" trigger - skip enrichment if manual
            if enrichment_trigger == "manual":
                logger.info(
                    "[PIPELINE:ENRICHMENT] %s: ✅ SKIPPING enrichment (manual trigger): %d canonical messages will be enriched later via POST /v1/enrichment/process",
                    self,
                    len(canonical_messages),
                )
                # Do NOT run enrichment - return early from this block
            elif enrichment_trigger == "automatic":
                # Only run enrichment if explicitly set to "automatic"
                logger.info(
                    "[PIPELINE:ENRICHMENT] %s: Running enrichment (automatic trigger)",
                    self,
                )
                # Automatic trigger - run enrichment now
                # Filter out messages that are already enriched
                unenriched_messages = _filter_unenriched_messages(
                    canonical_messages,
                    source_def.canonical_enrichment_jobs,
                    tables_manager,
                    source_id=source_def.source_id,
                    dataset_id=job.dataset_id,
                )
                
                if not unenriched_messages:
                    logger.debug(
                        "[PIPELINE:ENRICHMENT] %s: All %d messages already enriched, skipping",
                        self,
                        len(canonical_messages),
                    )
                else:
                    if not enrichment_orchestrator:
                        logger.error(
                            "[PIPELINE:ENRICHMENT] %s: Cannot run enrichment - enrichment_orchestrator not initialized",
                            self,
                        )
                        errors.append({"step": "enrichment", "errors": ["Enrichment orchestrator not initialized"]})
                    else:
                        logger.info(
                            "[PIPELINE:ENRICHMENT] %s → %s: Starting enrichment (automatic): %d new messages (out of %d total), jobs=%s",
                            self,
                            enrichment_orchestrator,
                            len(unenriched_messages),
                            len(canonical_messages),
                            source_def.canonical_enrichment_jobs,
                        )
                        try:
                            enrichment_result = await enrichment_orchestrator.run_canonical(
                                unenriched_messages,
                                job_names=source_def.canonical_enrichment_jobs,
                            )
                            logger.info(
                                "[PIPELINE:ENRICHMENT] %s → %s: Enrichment complete: jobs_run=%s, records_created=%s, errors=%s",
                                self,
                                enrichment_orchestrator,
                                enrichment_result.get("jobs_run"),
                                enrichment_result.get("records_created"),
                                len(enrichment_result.get("errors", [])),
                            )
                            if enrichment_result.get("errors"):
                                errors.extend(enrichment_result["errors"])
                        except Exception as exc:
                            logger.error(
                                "[PIPELINE:ENRICHMENT] %s → %s: Enrichment failed: %s",
                                self,
                                enrichment_orchestrator,
                                exc,
                                exc_info=True,
                            )
                            errors.append({"step": "enrichment", "errors": [str(exc)]})

        if self.checkpoint_store and last_record_id:
            checkpoint = IngestionCheckpoint(
                dataset_id=job.dataset_id,
                schema_id=job.schema_id,
                last_record_id=last_record_id,
                metadata={"file_path": str(file_path)},
            )
            self.checkpoint_store.save_checkpoint(checkpoint)

        logger.debug(
            "[PIPELINE:MANAGER] %s: Job complete: job_id=%s, records_processed=%s, errors_count=%s, last_record_id=%s",
            self,
            job.job_id,
            records_processed,
            len(errors),
            last_record_id,
        )
        
        # Update progress: set records_total if we now know it
        if not progress.records_total and records_processed > 0:
            progress.records_total = records_processed
        
        # Finalize progress bar
        if pbar:
            # Ensure progress bar shows 100%
            if records_processed > 0:
                pbar.total = records_processed
                pbar.n = records_processed
                pbar.set_description(f"{self}: Complete")
                pbar._display()
            pbar.close()
        
        # Send final progress update
        if progress_api_url and progress_api_key:
            try:
                import httpx
                progress_dict = progress.to_dict()
                progress_dict["progress_percent"] = 100.0  # Ensure 100% on completion
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(
                        f"{progress_api_url}/v1/ingestion/progress",
                        json={
                            "job_id": job.job_id,
                            **progress_context,
                            "status": "completed",
                            "current_step": "completed",
                            **progress_dict,
                        },
                        headers={"Authorization": f"Bearer {progress_api_key}"},
                    )
            except Exception as exc:
                logger.warning("Failed to send final ingestion progress: %s", exc)

        file_size_bytes = 0
        try:
            file_size_bytes = int(file_path.stat().st_size)
        except Exception:
            file_size_bytes = 0
        quantity_mb = int((max(0, file_size_bytes) + (1024 * 1024) - 1) // (1024 * 1024))
        await emit_usage_observation(
            action="ingestion.file_processed",
            quantity=quantity_mb,
            producer="ingestion.manager",
            canonical_action_identity={
                "job_id": job.job_id,
                "dataset_id": job.dataset_id,
                "schema_id": job.schema_id,
                "source_id": source_id or "",
                "records_processed": records_processed,
            },
            topos_id=job.dataset_id,
            trust_class="cp_observed_self_hosted",
            metadata={"file_size_bytes": file_size_bytes, "quantity_mb": quantity_mb},
        )
        
        # Include progress information in return (for progress bar)
        progress_dict = progress.to_dict()
        
        return {
            "job_id": job.job_id,
            "records_processed": records_processed,
            "errors_count": len(errors),
            "errors": errors[:100],
            # Include progress for progress bar
            "progress_percent": progress_dict.get("progress_percent", 0.0),
            "records_total": progress_dict.get("records_total"),
            "estimated_seconds_remaining": progress_dict.get("estimated_seconds_remaining"),
            "current_step": progress_dict.get("current_step"),
        }
