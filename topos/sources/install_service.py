from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..config.settings import settings
from ..core.state import get_db_connection
from ..enrichment.jobs import CANONICAL_JOBS, RAW_JOBS
from ..ingestion.parsers import PARSER_REGISTRY
from ..storage.db.postgres import connect_postgres, execute_query, fetch_all, fetch_one
from .runtime_install import RuntimeInstallHandle, install_source_definition

INSTALL_TABLE = "source_runtime_installs"
logger = logging.getLogger("topos.sources.install_service")
_SELECT_COLUMNS = (
    "install_id",
    "scope_key",
    "source_id",
    "version_id",
    "status",
    "is_active",
    "source_definition_json",
    "source_version_row_json",
    "failure_reason",
    "created_at",
    "updated_at",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_key(scope: Optional[Dict[str, Any]]) -> str:
    scope = scope or {}
    normalized = {
        "user_id": str(scope.get("user_id") or "*").strip() or "*",
        "device_id": str(scope.get("device_id") or "*").strip() or "*",
        "topos_id": str(scope.get("topos_id") or scope.get("app_id") or "*").strip() or "*",
        "dataset_id": str(scope.get("dataset_id") or "*").strip() or "*",
    }
    return json.dumps(normalized, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _scope_dict(scope_key: str) -> Dict[str, str]:
    try:
        parsed = json.loads(scope_key)
        if isinstance(parsed, dict):
            return {
                "user_id": str(parsed.get("user_id") or "*"),
                "device_id": str(parsed.get("device_id") or "*"),
                "topos_id": str(parsed.get("topos_id") or parsed.get("app_id") or "*"),
                "dataset_id": str(parsed.get("dataset_id") or "*"),
            }
    except Exception:
        pass
    return {"user_id": "*", "device_id": "*", "topos_id": "*", "dataset_id": "*"}


_SCOPE_FIELDS = ("user_id", "device_id", "topos_id", "dataset_id")
_CONCRETE_INSTALL_FIELDS = ("user_id", "topos_id", "dataset_id")


def _topos_id_from_scope(scope: Dict[str, Any]) -> str:
    return str(scope.get("topos_id") or scope.get("app_id") or "").strip()


def _validate_concrete_install_scope(scope: Optional[Dict[str, Any]]) -> None:
    scope = scope or {}
    missing: List[str] = []
    if not str(scope.get("user_id") or "").strip() or str(scope.get("user_id") or "").strip() == "*":
        missing.append("user_id")
    if not _topos_id_from_scope(scope) or _topos_id_from_scope(scope) == "*":
        missing.append("topos_id")
    if not str(scope.get("dataset_id") or "").strip() or str(scope.get("dataset_id") or "").strip() == "*":
        missing.append("dataset_id")
    if missing:
        raise ValueError(f"Concrete scope required: {', '.join(missing)}")


def _is_concrete_scope(scope: Optional[Dict[str, Any]]) -> bool:
    scope = scope or {}
    if not str(scope.get("user_id") or "").strip() or str(scope.get("user_id") or "").strip() == "*":
        return False
    if not _topos_id_from_scope(scope) or _topos_id_from_scope(scope) == "*":
        return False
    if not str(scope.get("dataset_id") or "").strip() or str(scope.get("dataset_id") or "").strip() == "*":
        return False
    return True


def _normalize_scope(scope: Optional[Dict[str, Any]]) -> Dict[str, str]:
    scope = scope or {}
    return {
        field: str(scope.get(field) or "*").strip() or "*"
        for field in _SCOPE_FIELDS
    }


def _scope_field_matches(query_value: str, stored_value: str) -> bool:
    query = str(query_value or "").strip()
    stored = str(stored_value or "").strip()
    if not query or query == "*":
        return False
    return query == stored


def _scope_matches(query: Dict[str, str], stored: Dict[str, str]) -> bool:
    """Return True when stored install scope exactly matches the query scope."""
    for field in _SCOPE_FIELDS:
        if not _scope_field_matches(query.get(field, ""), stored.get(field, "")):
            return False
    return True


def _migrate_scope_keys_to_topos_id(conn: Any) -> None:
    rows = fetch_all(
        conn,
        f"""
        SELECT install_id, scope_key
        FROM {INSTALL_TABLE}
        """,
    )
    for row in rows:
        install_id = str(_extract_row_value(row, 0, "install_id") or "").strip()
        raw_scope_key = str(_extract_row_value(row, 1, "scope_key") or "").strip()
        if not install_id or not raw_scope_key:
            continue
        try:
            parsed = json.loads(raw_scope_key)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue

        if "app_id" not in parsed:
            continue
        topos_value = str(parsed.get("topos_id") or parsed.get("app_id") or "*").strip() or "*"
        parsed["topos_id"] = topos_value
        parsed.pop("app_id", None)
        new_scope_key = json.dumps(parsed, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        if new_scope_key == raw_scope_key:
            continue

        execute_query(
            conn,
            f"""
            UPDATE {INSTALL_TABLE}
            SET scope_key = %s
            WHERE install_id = %s
            """,
            (new_scope_key, install_id),
        )


def _parse_source_definition_from_version_row(version_row: Dict[str, Any]) -> Dict[str, Any]:
    source_def = version_row.get("source_definition_json")
    if isinstance(source_def, str):
        source_def = json.loads(source_def)
    if not isinstance(source_def, dict):
        raise ValueError("source_version_row_json.source_definition_json must be a JSON object")
    return source_def


def _is_valid_path_token(token: str) -> bool:
    if not token:
        return False
    if token == "*":
        return True
    if token.endswith("[*]"):
        token = token[:-3]
    return token.replace("_", "").replace("-", "").isalnum()


def _validate_parser_extract_map(source_def: Dict[str, Any]) -> None:
    ingest_shape = source_def.get("file_ingest_shape")
    parser_extract_map = ingest_shape.get("parser_extract_map") if isinstance(ingest_shape, dict) else None
    if parser_extract_map is None:
        return
    if not isinstance(parser_extract_map, dict):
        raise ValueError("file_ingest_shape.parser_extract_map must be a JSON object")
    canonical_mapper_id = str(source_def.get("canonical_mapper_id") or "").strip()
    canonical_group_id = str(source_def.get("canonical_group_id") or "").strip()
    canonical_mapping_connected = bool(source_def.get("canonical_mapping_connected"))
    requires_canonical_contract = bool(canonical_mapping_connected or canonical_mapper_id or canonical_group_id)
    if requires_canonical_contract:
        required_keys = {"message_id", "sender_type", "content"}
        missing = sorted([key for key in required_keys if key not in parser_extract_map])
        if missing:
            raise ValueError(f"parser_extract_map missing required keys: {', '.join(missing)}")
        if "event_at" not in parser_extract_map and "created_at" not in parser_extract_map:
            raise ValueError("parser_extract_map must include either event_at or created_at")
    for key, value in parser_extract_map.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"parser_extract_map.{key} must be a non-empty string path")
        for token in [part.strip() for part in value.split(".") if part.strip()]:
            if not _is_valid_path_token(token):
                raise ValueError(f"parser_extract_map.{key} has invalid path token: {token}")


def _validate_mapper_contract(source_def: Dict[str, Any]) -> None:
    source_type = str(source_def.get("source_type") or "").strip()
    canonical_mapper_id = str(source_def.get("canonical_mapper_id") or "").strip()
    canonical_group_id = str(source_def.get("canonical_group_id") or "").strip()
    canonical_mapping_connected = bool(source_def.get("canonical_mapping_connected"))
    requires_canonical_contract = bool(canonical_mapping_connected or canonical_mapper_id or canonical_group_id)
    if source_type in {"file", "ui_stream"} and not requires_canonical_contract:
        return
    if requires_canonical_contract and not canonical_mapper_id:
        raise ValueError("canonical_mapper_id is required when canonical mapping is connected")
    if requires_canonical_contract and not canonical_group_id:
        raise ValueError("canonical_group_id is required when canonical mapping is connected")
    if canonical_group_id and canonical_group_id not in {
        "ai_messages",
        "conversations",
        "activity",
        "schedule",
        "journal",
        "profile",
        "financial",
        "places",
        "contacts",
    }:
        raise ValueError(
            "canonical_group_id must be one of: ai_messages, conversations, activity, "
            "schedule, journal, profile, financial, places, contacts"
        )


def _trusted_enrichment_function_catalog() -> Dict[str, Dict[str, str]]:
    canonical_job_names = {job.get_job_name(): job.get_job_name() for job in CANONICAL_JOBS}
    raw_job_names = {job.get_job_name(): job.get_job_name() for job in RAW_JOBS}
    return {
        **{job_name: {"stage": "canonical", "job_name": canonical_job_names[job_name]} for job_name in canonical_job_names},
        **{job_name: {"stage": "raw", "job_name": raw_job_names[job_name]} for job_name in raw_job_names},
    }


def _normalize_enrichment_bindings(source_def: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(source_def)
    bindings = out.get("source_enrichments")
    if not isinstance(bindings, list):
        return out
    catalog = _trusted_enrichment_function_catalog()
    canonical_jobs: List[str] = []
    raw_jobs: List[str] = []
    canonical_trigger_modes: List[str] = []
    for idx, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValueError(f"source_enrichments[{idx}] must be an object")
        function_id = str(binding.get("function_id") or "").strip()
        if not function_id:
            raise ValueError(f"source_enrichments[{idx}].function_id is required")
        trusted = catalog.get(function_id)
        if not trusted:
            raise ValueError(
                f"source_enrichments[{idx}].function_id={function_id!r} is not in trusted runtime catalog"
            )
        stage = str(binding.get("stage") or trusted["stage"]).strip()
        trigger_mode = str(binding.get("trigger_mode") or "automatic").strip()
        if trigger_mode not in {"automatic", "manual"}:
            raise ValueError(f"source_enrichments[{idx}].trigger_mode must be automatic or manual")
        if stage == "canonical":
            canonical_jobs.append(trusted["job_name"])
            canonical_trigger_modes.append(trigger_mode)
        elif stage == "raw":
            raw_jobs.append(trusted["job_name"])
        else:
            raise ValueError(f"source_enrichments[{idx}].stage must be canonical or raw")
    if canonical_jobs:
        out["canonical_enrichment_jobs"] = sorted(set(canonical_jobs))
        out["enrichment_trigger"] = "manual" if "manual" in canonical_trigger_modes else "automatic"
    if raw_jobs:
        out["raw_enrichment_jobs"] = sorted(set(raw_jobs))
    return out


def _validate_source_contract(source_def: Dict[str, Any]) -> None:
    source_id = str(source_def.get("source_id") or "").strip()
    schema_id = str(source_def.get("schema_id") or "").strip()
    parser_id = str(source_def.get("parser_id") or "").strip()
    source_type = str(source_def.get("source_type") or "").strip()
    if not source_id:
        raise ValueError("source_definition_json.source_id is required")
    if not schema_id or not parser_id:
        raise ValueError("source_definition_json.schema_id and parser_id are required")
    if source_type not in {"file", "ui_stream", "local_sync"}:
        raise ValueError("source_definition_json.source_type must be one of: file, ui_stream, local_sync")
    _validate_parser_extract_map(source_def)
    _validate_mapper_contract(source_def)


@dataclass(frozen=True)
class InstallRecord:
    install_id: str
    scope: Dict[str, str]
    source_id: str
    version_id: Optional[str]
    status: str
    is_active: bool
    source_definition_json: Dict[str, Any]
    source_version_row_json: Optional[Dict[str, Any]]
    failure_reason: Optional[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "install_id": self.install_id,
            "scope": self.scope,
            "source_id": self.source_id,
            "version_id": self.version_id,
            "status": self.status,
            "is_active": self.is_active,
            "source_definition_json": self.source_definition_json,
            "source_version_row_json": self.source_version_row_json,
            "failure_reason": self.failure_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_LOCK = threading.RLock()
_ACTIVE_HANDLES: Dict[Tuple[str, str], RuntimeInstallHandle] = {}


@contextmanager
def _db_conn() -> Iterator[Any]:
    """Yield a DB connection using configured database mode.

    - local mode: shared SQLite connection from `get_db_connection()`
    - postgres mode: transactional connection from `connect_postgres()`
    """
    if settings.topos_database_mode == "postgres":
        with connect_postgres() as conn:
            yield conn
        return

    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database not available")
    yield conn


def ensure_install_schema() -> None:
    with _db_conn() as conn:
        execute_query(
            conn,
            f"""
            CREATE TABLE IF NOT EXISTS {INSTALL_TABLE} (
                install_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                source_id TEXT NOT NULL,
                version_id TEXT,
                status TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                source_definition_json TEXT NOT NULL,
                source_version_row_json TEXT,
                failure_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        )
        execute_query(
            conn,
            f"""
            CREATE INDEX IF NOT EXISTS idx_{INSTALL_TABLE}_scope_source
            ON {INSTALL_TABLE}(scope_key, source_id, updated_at)
            """,
        )
        _migrate_scope_keys_to_topos_id(conn)
        if settings.topos_database_mode != "postgres":
            conn.commit()


def _row_to_record(row: Any) -> InstallRecord:
    if isinstance(row, dict):
        install_id = str(row.get("install_id"))
        scope_key = str(row.get("scope_key"))
        source_id = str(row.get("source_id"))
        version_id_value = row.get("version_id")
        status_value = row.get("status")
        is_active_value = row.get("is_active")
        source_def_raw = row.get("source_definition_json")
        source_row = row.get("source_version_row_json")
        failure_reason_value = row.get("failure_reason")
        created_at_value = row.get("created_at")
        updated_at_value = row.get("updated_at")
    else:
        (
            install_id,
            scope_key,
            source_id,
            version_id_value,
            status_value,
            is_active_value,
            source_def_raw,
            source_row,
            failure_reason_value,
            created_at_value,
            updated_at_value,
        ) = row

    source_def = json.loads(str(source_def_raw) or "{}")
    source_row_json = None
    if source_row:
        source_row_json = json.loads(str(source_row))
    return InstallRecord(
        install_id=str(install_id),
        scope=_scope_dict(str(scope_key)),
        source_id=str(source_id),
        version_id=str(version_id_value) if version_id_value else None,
        status=str(status_value),
        is_active=bool(is_active_value),
        source_definition_json=source_def if isinstance(source_def, dict) else {},
        source_version_row_json=source_row_json if isinstance(source_row_json, dict) else None,
        failure_reason=str(failure_reason_value) if failure_reason_value else None,
        created_at=str(created_at_value),
        updated_at=str(updated_at_value),
    )


def _get_active_record(conn: Any, scope_key: str, source_id: str) -> Optional[InstallRecord]:
    rows = fetch_all(
        conn,
        f"""
        SELECT {", ".join(_SELECT_COLUMNS)}
        FROM {INSTALL_TABLE}
        WHERE scope_key = %s AND source_id = %s AND is_active = 1
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (scope_key, source_id),
    )
    if not rows:
        return None
    return _row_to_record(rows[0])


def install_source(
    *,
    source_definition_json: Optional[Dict[str, Any]] = None,
    source_version_row_json: Optional[Dict[str, Any]] = None,
    version_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
) -> InstallRecord:
    ensure_install_schema()
    source_def = source_definition_json
    if source_def is None and source_version_row_json is not None:
        source_def = _parse_source_definition_from_version_row(source_version_row_json)
    if not isinstance(source_def, dict):
        raise ValueError("source_definition_json or source_version_row_json is required")
    source_def = _normalize_enrichment_bindings(source_def)
    _validate_source_contract(source_def)
    source_id = str(source_def.get("source_id") or "").strip()

    _validate_concrete_install_scope(scope)
    scope_key = _scope_key(scope)
    now = _utc_now_iso()
    install_id = str(uuid.uuid4())
    requested_version_id = (
        str(version_id).strip()
        if version_id
        else (str(source_version_row_json.get("version_id")).strip() if isinstance(source_version_row_json, dict) and source_version_row_json.get("version_id") else None)
    )

    with _db_conn() as conn:
        with _LOCK:
            active_before = _get_active_record(conn, scope_key, source_id)
            if (
                active_before
                and active_before.is_active
                and active_before.version_id == requested_version_id
                and active_before.source_definition_json == source_def
            ):
                # Runtime parser/mapper registrations are in-memory. After process restart we can have
                # an active DB install record but no runtime parser bound anymore.
                active_key = (scope_key, source_id)
                parser_ids = {
                    item
                    for item in [
                        str(source_def.get("schema_id") or "").strip(),
                        str(source_def.get("parser_id") or "").strip(),
                    ]
                    if item
                }
                runtime_missing = active_key not in _ACTIVE_HANDLES or any(
                    parser_id not in PARSER_REGISTRY for parser_id in parser_ids
                )
                if runtime_missing:
                    _ACTIVE_HANDLES[active_key] = install_source_definition(source_def)
                return active_before

            try:
                handle = install_source_definition(source_def)
            except Exception as exc:
                execute_query(
                    conn,
                    f"""
                    INSERT INTO {INSTALL_TABLE} (
                        install_id, scope_key, source_id, version_id, status, is_active,
                        source_definition_json, source_version_row_json, failure_reason,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, 'failed', 0, %s, %s, %s, %s, %s)
                    """,
                    (
                        install_id,
                        scope_key,
                        source_id,
                        requested_version_id,
                        json.dumps(source_def, separators=(",", ":"), ensure_ascii=True),
                        json.dumps(source_version_row_json, separators=(",", ":"), ensure_ascii=True)
                        if isinstance(source_version_row_json, dict)
                        else None,
                        str(exc),
                        now,
                        now,
                    ),
                )
                if settings.topos_database_mode != "postgres":
                    conn.commit()
                raise

            # Mark previous active record as installed (inactive).
            execute_query(
                conn,
                f"""
                UPDATE {INSTALL_TABLE}
                SET is_active = 0, status = 'installed', updated_at = %s
                WHERE scope_key = %s AND source_id = %s AND is_active = 1
                """,
                (now, scope_key, source_id),
            )
            execute_query(
                conn,
                f"""
                INSERT INTO {INSTALL_TABLE} (
                    install_id, scope_key, source_id, version_id, status, is_active,
                    source_definition_json, source_version_row_json, failure_reason,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'active', 1, %s, %s, NULL, %s, %s)
                """,
                (
                    install_id,
                    scope_key,
                    source_id,
                    requested_version_id,
                    json.dumps(source_def, separators=(",", ":"), ensure_ascii=True),
                    json.dumps(source_version_row_json, separators=(",", ":"), ensure_ascii=True)
                    if isinstance(source_version_row_json, dict)
                    else None,
                    now,
                    now,
                ),
            )
            if settings.topos_database_mode != "postgres":
                conn.commit()
            _ACTIVE_HANDLES[(scope_key, source_id)] = handle
            return InstallRecord(
                install_id=install_id,
                scope=_scope_dict(scope_key),
                source_id=source_id,
                version_id=requested_version_id,
                status="active",
                is_active=True,
                source_definition_json=source_def,
                source_version_row_json=source_version_row_json if isinstance(source_version_row_json, dict) else None,
                failure_reason=None,
                created_at=now,
                updated_at=now,
            )


def list_installs(*, scope: Optional[Dict[str, Any]] = None, source_id: Optional[str] = None) -> List[InstallRecord]:
    ensure_install_schema()
    clauses: List[str] = []
    params: List[Any] = []
    if source_id:
        clauses.append("source_id = %s")
        params.append(source_id)
    normalized_scope = _normalize_scope(scope) if scope is not None else None
    if scope is not None and _is_concrete_scope(scope):
        clauses.append("scope_key = %s")
        params.append(_scope_key(scope))
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _db_conn() as conn:
        rows = fetch_all(
            conn,
            f"""
            SELECT {", ".join(_SELECT_COLUMNS)}
            FROM {INSTALL_TABLE}
            {where_sql}
            ORDER BY updated_at DESC
            """,
            tuple(params),
        )
        records = [_row_to_record(row) for row in rows]
        if normalized_scope is None or _is_concrete_scope(scope):
            return records
        return [record for record in records if _scope_matches(normalized_scope, record.scope or {})]


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


def _purge_source_associated_data(conn: Any, source_id: str) -> Dict[str, Any]:
    sid = str(source_id or "").strip()
    if not sid:
        return {"tables_dropped": [], "rows_deleted": 0}

    rows_deleted = 0
    tables_dropped: List[str] = []
    normalized_sid = re.sub(r"[^a-z0-9]+", "", sid.lower())
    table_names = [name for name in _list_user_tables(conn) if name and _safe_sql_identifier(name)]

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
            execute_query(conn, f'DELETE FROM "{table_name}" WHERE "{source_column}" = %s', (sid,))
            rows_deleted += match_count
            deleted_from_table = True

        # Per-source raw tables are safe to drop when emptied by this purge.
        if deleted_from_table and table_name.startswith("raw_") and _count_all_rows(conn, table_name) == 0:
            _drop_table(conn, table_name)
            tables_dropped.append(table_name)
            continue

        # Some legacy source tables may not carry source_id/source_system columns.
        # Drop obvious per-source raw tables by normalized source id suffix.
        normalized_table = re.sub(r"[^a-z0-9]+", "", table_name.lower())
        if table_name.startswith("raw_") and normalized_sid and normalized_sid in normalized_table:
            _drop_table(conn, table_name)
            tables_dropped.append(table_name)

    return {"tables_dropped": sorted(set(tables_dropped)), "rows_deleted": rows_deleted}


def uninstall_source(
    *,
    source_id: str,
    scope: Optional[Dict[str, Any]] = None,
    delete_source_tables: bool = False,
) -> Dict[str, Any]:
    ensure_install_schema()
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError("source_id is required")

    scope_key = _scope_key(scope)
    now = _utc_now_iso()
    with _db_conn() as conn:
        with _LOCK:
            active_before = _get_active_record(conn, scope_key, sid)
            handle = _ACTIVE_HANDLES.pop((scope_key, sid), None)
            if handle is not None:
                handle.uninstall()
            execute_query(
                conn,
                f"""
                UPDATE {INSTALL_TABLE}
                SET is_active = 0, status = 'rolled_back', updated_at = %s
                WHERE scope_key = %s AND source_id = %s AND is_active = 1
                """,
                (now, scope_key, sid),
            )
            purge_summary: Dict[str, Any] = {"tables_dropped": [], "rows_deleted": 0}
            if delete_source_tables:
                purge_summary = _purge_source_associated_data(conn, sid)
            if settings.topos_database_mode != "postgres":
                conn.commit()
    return {
        "status": "ok",
        "source_id": sid,
        "scope": _scope_dict(scope_key),
        "uninstalled": active_before is not None,
        "delete_source_tables": bool(delete_source_tables),
        "rows_deleted": int(purge_summary.get("rows_deleted") or 0),
        "tables_dropped": purge_summary.get("tables_dropped") or [],
    }


_PATCHABLE_DEFINITION_KEYS = frozenset(
    {
        "default_scope_id",
        "ingestion_trigger",
        "enrichment_trigger",
        "raw_enrichment_jobs",
        "canonical_enrichment_jobs",
    }
)
_IMMUTABLE_DEFINITION_KEYS = frozenset(
    {
        "source_id",
        "display_name",
        "source_type",
        "schema_id",
        "parser_id",
        "canonical_mapper_id",
        "canonical_group_id",
    }
)


def patch_source_install(
    *,
    source_id: str,
    scope: Optional[Dict[str, Any]] = None,
    source_definition_json: Optional[Dict[str, Any]] = None,
) -> InstallRecord:
    """Update an active install's definition in place (user configuration changes)."""
    ensure_install_schema()
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError("source_id is required")
    partial = source_definition_json if isinstance(source_definition_json, dict) else {}
    if not partial:
        raise ValueError("source_definition_json is required")

    _validate_concrete_install_scope(scope)
    scope_key = _scope_key(scope)
    now = _utc_now_iso()

    with _db_conn() as conn:
        with _LOCK:
            active = _get_active_record(conn, scope_key, sid)
            if not active or not active.is_active:
                raise ValueError(f"No active install found for source_id={sid}")

            merged = dict(active.source_definition_json if isinstance(active.source_definition_json, dict) else {})
            for key, value in partial.items():
                if key in _IMMUTABLE_DEFINITION_KEYS and value != merged.get(key):
                    raise ValueError(f"Cannot change immutable field {key!r} via PATCH")
                if key in _PATCHABLE_DEFINITION_KEYS:
                    merged[key] = value

            merged = _normalize_enrichment_bindings(merged)
            _validate_source_contract(merged)
            if str(merged.get("source_id") or "").strip() != sid:
                raise ValueError("source_id in definition must match install source_id")

            try:
                handle = install_source_definition(merged)
            except Exception as exc:
                raise RuntimeError(str(exc)) from exc

            execute_query(
                conn,
                f"""
                UPDATE {INSTALL_TABLE}
                SET source_definition_json = %s, updated_at = %s
                WHERE install_id = %s
                """,
                (
                    json.dumps(merged, separators=(",", ":"), ensure_ascii=True),
                    now,
                    active.install_id,
                ),
            )
            if settings.topos_database_mode != "postgres":
                conn.commit()
            _ACTIVE_HANDLES[(scope_key, sid)] = handle
            return InstallRecord(
                install_id=active.install_id,
                scope=_scope_dict(scope_key),
                source_id=sid,
                version_id=active.version_id,
                status="active",
                is_active=True,
                source_definition_json=merged,
                source_version_row_json=active.source_version_row_json,
                failure_reason=None,
                created_at=active.created_at,
                updated_at=now,
            )


def get_active_source_definition(*, source_id: str, scope: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    try:
        with _db_conn() as conn:
            rec = _get_active_record(conn, _scope_key(scope), str(source_id or "").strip())
            if rec and rec.is_active:
                return rec.source_definition_json
    except Exception:
        return None
    return None


def rehydrate_active_installs_runtime(*, source_id: Optional[str] = None) -> Dict[str, int]:
    """Ensure active installs are present in runtime registries after restarts.

    This repopulates in-memory REGISTRY/PARSER_REGISTRY/MAPPER_REGISTRY from
    persisted active install rows without creating new install history rows.
    """
    ensure_install_schema()
    records = [rec for rec in list_installs(source_id=source_id) if rec.is_active]
    rehydrated = 0
    failed = 0
    with _LOCK:
        for rec in records:
            source_def = rec.source_definition_json if isinstance(rec.source_definition_json, dict) else {}
            if not source_def:
                continue
            try:
                scope_key = _scope_key(rec.scope)
                runtime_key = (scope_key, rec.source_id)
                parser_ids = {
                    item
                    for item in [
                        str(source_def.get("schema_id") or "").strip(),
                        str(source_def.get("parser_id") or "").strip(),
                    ]
                    if item
                }
                runtime_missing = runtime_key not in _ACTIVE_HANDLES or any(parser_id not in PARSER_REGISTRY for parser_id in parser_ids)
                if not runtime_missing:
                    continue
                _ACTIVE_HANDLES[runtime_key] = install_source_definition(source_def)
                rehydrated += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning(
                    "Failed to rehydrate active install runtime: source_id=%s scope=%s error=%s",
                    rec.source_id,
                    rec.scope,
                    exc,
                )
    return {"active_installs": len(records), "rehydrated": rehydrated, "failed": failed}

