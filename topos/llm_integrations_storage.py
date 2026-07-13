from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from topos.core.state import get_engine_config_value, set_engine_config_value

logger = logging.getLogger("topos.llm_integrations_storage")

CREDENTIALS_TABLE = "llm_provider_credentials"
PREFERENCES_TABLE = "llm_provider_preferences"
USAGE_EVENTS_TABLE = "llm_usage_events"

# Internal product tables — stored locally but hidden from /data explorer.
DATA_EXPLORER_HIDDEN_TABLES = frozenset(
    {
        CREDENTIALS_TABLE,
        PREFERENCES_TABLE,
        USAGE_EVENTS_TABLE,
    }
)

LEGACY_CREDENTIALS_CONFIG_KEY = "llm_integrations:v1:credentials"
LEGACY_PREFERENCES_CONFIG_KEY = "llm_integrations:v1:preferences"
LEGACY_USAGE_EVENTS_CONFIG_KEY = "llm_integrations:v1:usage_events"
MAX_USAGE_EVENTS = 5000

SUPPORTED_BYOK_PROVIDERS = ("openai", "anthropic", "grok")
ACTIVE_PROVIDERS = ("platform", "redpill", "ollama", "openai", "anthropic", "grok")

DEFAULT_MODELS: Dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-20241022",
    "grok": "grok-2-latest",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_row_factory(conn: sqlite3.Connection) -> None:
    """Shared engine conn can temporarily lose Row factory during scrub/adapters."""
    if getattr(conn, "row_factory", None) is not sqlite3.Row:
        conn.row_factory = sqlite3.Row


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    keys_fn = getattr(row, "keys", None)
    if callable(keys_fn):
        try:
            return {key: row[key] for key in keys_fn()}
        except Exception:
            pass
    # Fallback for plain tuples/lists when row_factory was cleared.
    if isinstance(row, (tuple, list)):
        return {str(i): value for i, value in enumerate(row)}
    try:
        return dict(row)
    except Exception:
        return {}


def ensure_llm_integrations_tables(conn: sqlite3.Connection) -> None:
    _ensure_row_factory(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CREDENTIALS_TABLE} (
            topos_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            key_hint TEXT,
            default_model TEXT,
            last_validated_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (topos_id, provider)
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PREFERENCES_TABLE} (
            topos_id TEXT PRIMARY KEY,
            active_provider TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {USAGE_EVENTS_TABLE} (
            event_id TEXT PRIMARY KEY,
            topos_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL,
            billing_source TEXT NOT NULL,
            source TEXT,
            idempotency_key TEXT,
            metadata_json TEXT,
            event_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_llm_usage_events_topos_period
        ON {USAGE_EVENTS_TABLE}(topos_id, event_at DESC)
        """
    )
    conn.commit()
    _migrate_legacy_engine_config_storage(conn)


def _delete_engine_config_key(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM engine_config WHERE key = ?", (key,))
    conn.commit()


def _migrate_legacy_engine_config_storage(conn: sqlite3.Connection) -> None:
    cred_raw = get_engine_config_value(conn, LEGACY_CREDENTIALS_CONFIG_KEY)
    if cred_raw:
        try:
            bucket = json.loads(cred_raw)
        except json.JSONDecodeError:
            bucket = {}
        if isinstance(bucket, dict):
            for provider, row in bucket.items():
                if not isinstance(row, dict):
                    continue
                prov = str(provider or "").strip().lower()
                if prov not in SUPPORTED_BYOK_PROVIDERS:
                    continue
                topos_id = str(row.get("topos_id") or "local").strip() or "local"
                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO {CREDENTIALS_TABLE}
                    (topos_id, provider, ciphertext, key_hint, default_model, last_validated_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        topos_id,
                        prov,
                        str(row.get("ciphertext") or ""),
                        row.get("key_hint"),
                        row.get("default_model"),
                        row.get("last_validated_at"),
                        str(row.get("updated_at") or _now_iso()),
                    ),
                )
        _delete_engine_config_key(conn, LEGACY_CREDENTIALS_CONFIG_KEY)

    prefs_raw = get_engine_config_value(conn, LEGACY_PREFERENCES_CONFIG_KEY)
    if prefs_raw:
        try:
            prefs = json.loads(prefs_raw)
        except json.JSONDecodeError:
            prefs = {}
        if isinstance(prefs, dict):
            topos_id = str(prefs.get("topos_id") or "local").strip() or "local"
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {PREFERENCES_TABLE}
                (topos_id, active_provider, updated_at)
                VALUES (?, ?, ?)
                """,
                (
                    topos_id,
                    prefs.get("active_provider"),
                    str(prefs.get("updated_at") or _now_iso()),
                ),
            )
        _delete_engine_config_key(conn, LEGACY_PREFERENCES_CONFIG_KEY)

    usage_raw = get_engine_config_value(conn, LEGACY_USAGE_EVENTS_CONFIG_KEY)
    if usage_raw:
        try:
            events = json.loads(usage_raw)
        except json.JSONDecodeError:
            events = []
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("event_id") or "").strip()
                if not event_id:
                    continue
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {USAGE_EVENTS_TABLE}
                    (event_id, topos_id, provider, model, prompt_tokens, completion_tokens, total_tokens,
                     cost_usd, billing_source, source, idempotency_key, metadata_json, event_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        str(event.get("topos_id") or "local"),
                        str(event.get("provider") or "unknown"),
                        str(event.get("model") or "unknown"),
                        int(event.get("prompt_tokens") or 0),
                        int(event.get("completion_tokens") or 0),
                        int(event.get("total_tokens") or 0),
                        event.get("cost_usd"),
                        str(event.get("billing_source") or "byok"),
                        event.get("source"),
                        event.get("idempotency_key"),
                        json.dumps(event.get("metadata") or {}),
                        str(event.get("event_at") or _now_iso()),
                        str(event.get("created_at") or _now_iso()),
                    ),
                )
        _delete_engine_config_key(conn, LEGACY_USAGE_EVENTS_CONFIG_KEY)
    conn.commit()


def list_credentials(conn: sqlite3.Connection, *, topos_id: str) -> List[Dict[str, Any]]:
    ensure_llm_integrations_tables(conn)
    tid = str(topos_id or "").strip()
    rows = conn.execute(
        f"SELECT * FROM {CREDENTIALS_TABLE} WHERE topos_id = ? ORDER BY provider ASC",
        (tid,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_credential(conn: sqlite3.Connection, *, topos_id: str, provider: str) -> Optional[Dict[str, Any]]:
    ensure_llm_integrations_tables(conn)
    prov = str(provider or "").strip().lower()
    if prov not in SUPPORTED_BYOK_PROVIDERS:
        return None
    row = conn.execute(
        f"SELECT * FROM {CREDENTIALS_TABLE} WHERE topos_id = ? AND provider = ? LIMIT 1",
        (str(topos_id or "").strip(), prov),
    ).fetchone()
    return _row_to_dict(row) or None


def upsert_credential(
    conn: sqlite3.Connection,
    *,
    topos_id: str,
    provider: str,
    ciphertext: str,
    key_hint: str,
    default_model: Optional[str] = None,
    last_validated_at: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_llm_integrations_tables(conn)
    tid = str(topos_id or "").strip()
    prov = str(provider or "").strip().lower()
    if not tid or prov not in SUPPORTED_BYOK_PROVIDERS:
        raise ValueError("Invalid topos_id or provider")
    row = {
        "topos_id": tid,
        "provider": prov,
        "ciphertext": ciphertext,
        "key_hint": key_hint or None,
        "default_model": (default_model or DEFAULT_MODELS.get(prov) or None),
        "last_validated_at": last_validated_at,
        "updated_at": _now_iso(),
    }
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {CREDENTIALS_TABLE}
        (topos_id, provider, ciphertext, key_hint, default_model, last_validated_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["topos_id"],
            row["provider"],
            row["ciphertext"],
            row["key_hint"],
            row["default_model"],
            row["last_validated_at"],
            row["updated_at"],
        ),
    )
    conn.commit()
    logger.info("llm_credential_saved provider=%s topos_id=%s", prov, tid[:12])
    return row


def delete_credential(conn: sqlite3.Connection, *, topos_id: str, provider: str) -> bool:
    ensure_llm_integrations_tables(conn)
    prov = str(provider or "").strip().lower()
    cur = conn.execute(
        f"DELETE FROM {CREDENTIALS_TABLE} WHERE topos_id = ? AND provider = ?",
        (str(topos_id or "").strip(), prov),
    )
    conn.commit()
    deleted = cur.rowcount > 0
    if deleted:
        logger.info("llm_credential_deleted provider=%s topos_id=%s", prov, str(topos_id or "")[:12])
    return deleted


def get_preferences(conn: sqlite3.Connection, *, topos_id: str) -> Optional[Dict[str, Any]]:
    ensure_llm_integrations_tables(conn)
    row = conn.execute(
        f"SELECT * FROM {PREFERENCES_TABLE} WHERE topos_id = ? LIMIT 1",
        (str(topos_id or "").strip(),),
    ).fetchone()
    return _row_to_dict(row) or None


def upsert_preferences(
    conn: sqlite3.Connection,
    *,
    topos_id: str,
    active_provider: Optional[str],
) -> Dict[str, Any]:
    ensure_llm_integrations_tables(conn)
    tid = str(topos_id or "").strip()
    if not tid:
        raise ValueError("topos_id required")
    active = str(active_provider or "").strip().lower() or None
    if active and active not in ACTIVE_PROVIDERS:
        raise ValueError(f"Invalid active_provider: {active}")
    row = {"topos_id": tid, "active_provider": active, "updated_at": _now_iso()}
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {PREFERENCES_TABLE}
        (topos_id, active_provider, updated_at)
        VALUES (?, ?, ?)
        """,
        (row["topos_id"], row["active_provider"], row["updated_at"]),
    )
    conn.commit()
    return row


def update_credential_default_model(
    conn: sqlite3.Connection,
    *,
    topos_id: str,
    provider: str,
    default_model: Optional[str],
) -> Optional[Dict[str, Any]]:
    ensure_llm_integrations_tables(conn)
    prov = str(provider or "").strip().lower()
    tid = str(topos_id or "").strip()
    conn.execute(
        f"""
        UPDATE {CREDENTIALS_TABLE}
        SET default_model = ?, updated_at = ?
        WHERE topos_id = ? AND provider = ?
        """,
        ((default_model or "").strip() or None, _now_iso(), tid, prov),
    )
    conn.commit()
    return get_credential(conn, topos_id=tid, provider=prov)


def insert_usage_event(conn: sqlite3.Connection, row: Dict[str, Any]) -> Dict[str, Any]:
    ensure_llm_integrations_tables(conn)
    idem = str(row.get("idempotency_key") or "").strip()
    if idem:
        existing = conn.execute(
            f"SELECT event_id FROM {USAGE_EVENTS_TABLE} WHERE idempotency_key = ? LIMIT 1",
            (idem,),
        ).fetchone()
        if existing:
            return {"deduped": True}
    event_id = str(row.get("event_id") or "").strip() or f"evt_{_now_iso()}"
    metadata = row.get("metadata")
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {USAGE_EVENTS_TABLE}
        (event_id, topos_id, provider, model, prompt_tokens, completion_tokens, total_tokens,
         cost_usd, billing_source, source, idempotency_key, metadata_json, event_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            str(row.get("topos_id") or ""),
            str(row.get("provider") or "unknown"),
            str(row.get("model") or "unknown"),
            int(row.get("prompt_tokens") or 0),
            int(row.get("completion_tokens") or 0),
            int(row.get("total_tokens") or 0),
            row.get("cost_usd"),
            str(row.get("billing_source") or "byok"),
            row.get("source"),
            row.get("idempotency_key"),
            json.dumps(metadata if isinstance(metadata, dict) else {}),
            str(row.get("event_at") or _now_iso()),
            str(row.get("created_at") or _now_iso()),
        ),
    )
    conn.commit()
    count_row = conn.execute(f"SELECT COUNT(*) AS count FROM {USAGE_EVENTS_TABLE}").fetchone()
    total = int(_row_to_dict(count_row).get("count") or 0)
    if total > MAX_USAGE_EVENTS:
        conn.execute(
            f"""
            DELETE FROM {USAGE_EVENTS_TABLE}
            WHERE event_id IN (
                SELECT event_id FROM {USAGE_EVENTS_TABLE}
                ORDER BY event_at ASC
                LIMIT ?
            )
            """,
            (total - MAX_USAGE_EVENTS,),
        )
        conn.commit()
    return row


def list_usage_events(
    conn: sqlite3.Connection,
    *,
    topos_id: str,
    period_start: str,
    period_end: str,
) -> List[Dict[str, Any]]:
    _ensure_row_factory(conn)
    ensure_llm_integrations_tables(conn)
    cur = conn.execute(
        f"""
        SELECT event_id, topos_id, provider, model, prompt_tokens, completion_tokens, total_tokens,
               cost_usd, billing_source, source, idempotency_key, metadata_json, event_at, created_at
        FROM {USAGE_EVENTS_TABLE}
        WHERE topos_id = ? AND event_at >= ? AND event_at < ?
        ORDER BY event_at DESC
        LIMIT 5000
        """,
        (str(topos_id or "").strip(), period_start, period_end),
    )
    columns = [desc[0] for desc in (cur.description or [])]
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        if isinstance(row, dict):
            item = dict(row)
        elif callable(getattr(row, "keys", None)):
            item = {key: row[key] for key in row.keys()}
        elif isinstance(row, (tuple, list)) and columns:
            item = {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
        else:
            item = _row_to_dict(row)
        metadata_raw = item.pop("metadata_json", None)
        if metadata_raw:
            try:
                item["metadata"] = json.loads(str(metadata_raw))
            except json.JSONDecodeError:
                item["metadata"] = {}
        out.append(item)
    return out


def resolve_local_usage_topos_id(conn: sqlite3.Connection) -> str:
    """Best-effort topos_id for engine-local usage writes (match CP-written home_chat rows)."""
    ensure_llm_integrations_tables(conn)
    row = conn.execute(
        f"""
        SELECT topos_id FROM {USAGE_EVENTS_TABLE}
        WHERE topos_id IS NOT NULL AND TRIM(topos_id) != ''
        ORDER BY event_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row:
        tid = str(_row_to_dict(row).get("topos_id") or "").strip()
        if tid:
            return tid
    pref = conn.execute(
        f"""
        SELECT topos_id FROM {PREFERENCES_TABLE}
        WHERE topos_id IS NOT NULL AND TRIM(topos_id) != ''
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    if pref:
        tid = str(_row_to_dict(pref).get("topos_id") or "").strip()
        if tid:
            return tid
    cred = conn.execute(
        f"""
        SELECT topos_id FROM {CREDENTIALS_TABLE}
        WHERE topos_id IS NOT NULL AND TRIM(topos_id) != ''
        LIMIT 1
        """
    ).fetchone()
    if cred:
        tid = str(_row_to_dict(cred).get("topos_id") or "").strip()
        if tid:
            return tid
    return "local"


def record_local_llm_usage_event(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    usage: Dict[str, Any],
    billing_source: str,
    source: str,
    idempotency_key: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    topos_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist an llm_usage_events row locally so billing UI can show ingestion usage."""
    import uuid

    _ensure_row_factory(conn)
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
        total_tokens = prompt_tokens + completion_tokens
    if total_tokens <= 0:
        return {"skipped": True}
    tid = str(topos_id or "").strip() or resolve_local_usage_topos_id(conn)
    return insert_usage_event(
        conn,
        {
            "event_id": f"llm_usage_{uuid.uuid4().hex}",
            "topos_id": tid,
            "provider": str(provider or "unknown").strip().lower() or "unknown",
            "model": str(model or "unknown").strip() or "unknown",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "billing_source": str(billing_source or "ollama").strip().lower() or "ollama",
            "source": source,
            "idempotency_key": idempotency_key,
            "metadata": metadata or {},
            "event_at": _now_iso(),
            "created_at": _now_iso(),
        },
    )


def maybe_migrate_legacy_llm_config(conn: sqlite3.Connection) -> None:
    """Move legacy engine_config JSON blobs into hidden tables (one-time)."""
    legacy_present = any(
        get_engine_config_value(conn, key)
        for key in (
            LEGACY_CREDENTIALS_CONFIG_KEY,
            LEGACY_PREFERENCES_CONFIG_KEY,
            LEGACY_USAGE_EVENTS_CONFIG_KEY,
        )
    )
    if legacy_present:
        ensure_llm_integrations_tables(conn)


def handle_storage_op(conn: sqlite3.Connection, *, op: str, args: Dict[str, Any]) -> Any:
    _ensure_row_factory(conn)
    operation = str(op or "").strip()
    if operation == "list_credentials":
        return list_credentials(conn, topos_id=str(args.get("topos_id") or ""))
    if operation == "get_credential":
        return get_credential(
            conn,
            topos_id=str(args.get("topos_id") or ""),
            provider=str(args.get("provider") or ""),
        )
    if operation == "upsert_credential":
        return upsert_credential(conn, **args)
    if operation == "delete_credential":
        return delete_credential(
            conn,
            topos_id=str(args.get("topos_id") or ""),
            provider=str(args.get("provider") or ""),
        )
    if operation == "get_preferences":
        return get_preferences(conn, topos_id=str(args.get("topos_id") or ""))
    if operation == "upsert_preferences":
        return upsert_preferences(
            conn,
            topos_id=str(args.get("topos_id") or ""),
            active_provider=args.get("active_provider"),
        )
    if operation == "update_credential_default_model":
        return update_credential_default_model(conn, **args)
    if operation == "insert_usage_event":
        return insert_usage_event(conn, dict(args.get("row") or {}))
    if operation == "list_usage_events":
        return list_usage_events(
            conn,
            topos_id=str(args.get("topos_id") or ""),
            period_start=str(args.get("period_start") or ""),
            period_end=str(args.get("period_end") or ""),
        )
    raise ValueError(f"Unsupported llm_integrations op: {operation}")
