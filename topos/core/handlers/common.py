"""Shared imports, state, and helpers for control-plane message handlers."""
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
from typing import Any, Callable, Dict, List, Optional, TypeVar
from topos.contacts.identity import normalize_contact_key as _normalize_contact_key
from topos.core.table_layers import layer_for_category, layer_kind_labels
from ...analytics.raw_queries import (
    avg_message_length,
    load_raw_messages,
    messages_by_sender,
    messages_per_day,
    total_messages,
)
from ...analytics.messenger_communities import (
    MESSENGER_COMMUNITIES_TABLE,
    MESSENGER_PARTICIPANT_IMPORTANCE_TABLE,
    MESSENGER_SOCIAL_EDGES_TABLE,
    compute_and_persist_messenger_analytics,
    ensure_messenger_analytics_tables,
)
from ...analytics.messenger_labels import (
    enrich_contact_rows_with_resolved_display_names,
    enrich_conversation_thread_previews,
    resolve_participant_labels,
)
from ...config.settings import settings
from ...storage.db.postgres import connect_postgres
from ...core import state as engine_state
from ...core.state import (
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
from ...core.routine_access import routine_uma_attribution
from fastapi import HTTPException
from ...ingestion.ingest_helpers import ingest_file_payload, ingest_ui_payload, resolve_file_format
from ...services.container import get_services
from ...storage.raw.file_store import RawFileStore
from ...storage.signal_identity import get_signal_identity, put_signal_identity
from ...storage.source_settings import get_source_settings, put_source_settings, update_sync_result
from ...sources.registry import REGISTRY
from ...uma_contact_enrichment import apply_message_contact_pipeline, strip_contact_runtime_filters
from ...uma_resource_id import parse_dataset_id_from_uma_dataset_resource_id
from ...uma_filters import (
    UMAFilterError,
    apply_filter_manifest,
    apply_filter_manifest_async,
    build_sql_constraints,
    extract_field_transforms,
    extract_filter_manifest,
    get_limit_cap,
)
from ...engine.scoped_token import ScopedTokenValidationError, validate_scoped_invocation_token
from ...engine.registration import RUNTIME_PROFILE_OPERATIONS, resolve_runtime_profile


logger = logging.getLogger("topos.core.handlers")

_T = TypeVar("_T")


def _in_worker(fn: Callable[..., _T], args: Any, kwargs: Any) -> Callable[[], _T]:
    """Bind ``fn`` to the WORKER thread's own connection, resolved on entry.

    Resolved through the hub (``topos.core.handlers.get_db_connection``) rather
    than ``core.state`` directly, because tests monkeypatch the hub attribute —
    going around it would let a test reach the live database.
    """

    def _call() -> _T:
        import topos.core.handlers as hub

        return fn(hub.get_db_connection(), *args, **kwargs)

    return _call


async def run_db_read(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a sync SQLite read off the engine event loop (UI/CP RPC responsiveness).

    ``fn`` is called as ``fn(conn, *args, **kwargs)`` on the worker's OWN
    connection. The caller must NOT resolve a connection and pass it in.

    That is not a style preference. A ``sqlite3.Connection`` carries one
    transaction state AND one statement cache, and the cache is an unsynchronized
    C structure: on 2026-08-17 a handler resolved the loop thread's connection,
    handed it to this function, and the worker's ``execute`` raced the loop
    thread's own writes through that cache. Its LRU eviction path then deleted a
    key twice and raised ``KeyError(('<sql>',))`` — and kept raising it, for the
    life of the process, from every unrelated call site sharing the handle. The
    node stayed up and answered ``/healthcheck`` while its database was dead.

    ``get_db_connection`` was made thread-local precisely to prevent that; a
    caller-passed handle defeats it. Callers needing a request-scoped object
    built from the connection (a ``BlackholeGuard``, a store) must build it
    inside ``fn`` too — one constructed on the loop thread carries the loop
    thread's handle in with it.
    """
    return await asyncio.to_thread(_in_worker(fn, args, kwargs))


async def run_db_write(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a sync SQLite write off the loop, on the worker's OWN connection.

    ``fn`` is called as ``fn(conn, *args, **kwargs)``. Writes take the write
    gate, a blocking OS lock: holding it on the event-loop thread stalls every
    coroutine including the control-plane keepalive, which is how the node reads
    as offline mid-enrichment. Read-only-looking handlers need this too whenever
    their store opens its schema first — ``CREATE TABLE IF NOT EXISTS`` is a
    write.

    Same connection rule as :func:`run_db_read` — resolved INSIDE the worker,
    never threaded through by the caller.
    """
    return await asyncio.to_thread(_in_worker(fn, args, kwargs))


def _resource_owner_for_mcp_log(conn: Any) -> Optional[str]:
    if conn is None:
        return None
    uid = get_user_id(conn)
    return (uid or "").strip() or None

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
        logger.debug(
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

def _is_sqlite_conn(conn: Any) -> bool:
    return "sqlite" in conn.__class__.__module__.lower()

# Newest-first row reads for MCP/Data explorer (matches uma_get_rows and get_messages).
_TABLE_ROW_TIME_ORDER_COLUMNS = (
    "event_at",
    "timestamp",
    "visited_at",
    "start_time",
    "end_time",
    "created_at",
    "updated_at",
    "ts",
)

_TABLE_ROW_CANONICAL_TIME_ORDER: Dict[str, tuple[str, ...]] = {
    "events": ("event_at", "timestamp", "created_at"),
    "activity": ("event_at", "timestamp", "created_at"),
    "journal": ("event_at", "created_at", "id"),
    "conversation_messages": ("event_at", "created_at", "message_id"),
    "ai_chat_messages": ("event_at", "created_at", "message_id"),
    "browser_visits": ("visited_at", "created_at", "record_id"),
    "browser_events": ("visited_at", "created_at", "record_id"),
}

def _table_row_order_clause(
    col_names: set[str],
    *,
    table_name: str = "",
    is_sqlite: bool = True,
) -> str:
    """Resolve ORDER BY for tabular reads: prefer temporal columns, newest first."""
    for c in _TABLE_ROW_CANONICAL_TIME_ORDER.get(table_name, ()):
        if c in col_names:
            return f'"{c}" DESC'
    for c in _TABLE_ROW_TIME_ORDER_COLUMNS:
        if c in col_names:
            return f'"{c}" DESC'
    if is_sqlite:
        return "rowid DESC"
    return "1 DESC"
