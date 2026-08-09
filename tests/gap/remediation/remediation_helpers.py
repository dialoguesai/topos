"""Shared helpers for remediation gap tests."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from topos.query.manifest import ScopeResolutionManifest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.canonical.ai_chat.model import CanonicalAIChatMessage
from topos.storage.db.migrations import apply_all_migrations


def sqlite_conn() -> sqlite3.Connection:
    # Reprocess runs its canonical stage on a worker thread (asyncio.to_thread),
    # so an injected connection must allow cross-thread use, matching how
    # core.state opens every real connection.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    apply_all_migrations(conn)
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables

    CanonicalTablesManager(conn)
    ensure_all_tables(conn)
    return conn


def adapter_bundle(conn: sqlite3.Connection):
    return AdapterFactory.create("local_database", conn=conn)


def ingest_chatgpt_message(conn: sqlite3.Connection, *, message_id: str = "msg-r01") -> None:
    manager = CanonicalTablesManager(conn)
    msg = CanonicalAIChatMessage(
        message_id=message_id,
        conversation_id="conv-r01",
        sender_type="human",
        sender_id=None,
        ts="2026-06-01T12:00:00Z",
        content="investor meeting notes",
        content_rendered=None,
        metadata_json=None,
        seq=0,
        source_id="chatgpt_file_ingestion",
    )
    manager.write_messages_batch(
        [msg],
        sync_batch_id="batch-r01",
        mapping_source_id="chatgpt_file_ingestion",
    )


def ai_conversations_manifest(**overrides: Any) -> ScopeResolutionManifest:
    base = ScopeResolutionManifest(
        scope_id="ai_conversations:read",
        primary_dimensions=["Memory", "Work"],
        canonical_tables=["ai_chat_messages"],
        access_mode_ceiling="raw",
        default_source_id="chatgpt_file_ingestion",
        must_not_retrieve=[],
    )
    if overrides:
        fields = {f.name for f in ScopeResolutionManifest.__dataclass_fields__.values()}
        return ScopeResolutionManifest(**{**base.__dict__, **{k: v for k, v in overrides.items() if k in fields}})
    return base


def messages_manifest(**overrides: Any) -> ScopeResolutionManifest:
    base = ScopeResolutionManifest(
        scope_id="messages:read",
        primary_dimensions=["Relationships", "Memory"],
        canonical_tables=["conversation_messages"],
        access_mode_ceiling="raw",
        default_source_id="imessage",
        must_not_retrieve=[],
    )
    if overrides:
        fields = {f.name for f in ScopeResolutionManifest.__dataclass_fields__.values()}
        return ScopeResolutionManifest(**{**base.__dict__, **{k: v for k, v in overrides.items() if k in fields}})
    return base
