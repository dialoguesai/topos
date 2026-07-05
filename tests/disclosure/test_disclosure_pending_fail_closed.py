"""Grantee reads must fail CLOSED when a record's ingest disclosure is pending.

Regression coverage for the fail-open where a record ingested before (or without)
the Platform Privacy Layer completing still holds raw PII in its canonical column.
A grantee (default_disclosure) read must never surface that raw value.
"""

from __future__ import annotations

import sqlite3

from topos.disclosure.tier import apply_disclosure_tier_to_rows
from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore
from topos.storage.db.migrations.canonical_disclosure_v1 import (
    apply_canonical_disclosure_v1_up,
)

RAW_EMAIL = "secret.person@example.com"
RAW_CONTENT = f"Reach me at {RAW_EMAIL} tomorrow"


def _migrated_conversation_messages() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE conversation_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            sender_id TEXT,
            source_id TEXT,
            content TEXT,
            created_at TEXT
        )
        """
    )
    # Adds content_disclosure / content_disclosure_hash / content_disclosure_model.
    apply_canonical_disclosure_v1_up(conn)
    return conn


def test_store_pending_disclosure_does_not_leak_raw_to_grantee() -> None:
    conn = _migrated_conversation_messages()
    # Record ingested but privacy layer has NOT run: content_disclosure is NULL.
    conn.execute(
        """
        INSERT INTO conversation_messages
            (message_id, conversation_id, sender_id, source_id, content, created_at)
        VALUES ('m1', 'c1', 'u1', 'src1', ?, '2026-01-01')
        """,
        (RAW_CONTENT,),
    )
    conn.commit()

    store = SQLiteCanonicalStore(conn)
    page = store.list("conversation_messages", disclosure_tier="default_disclosure", limit=10)

    assert page.total == 1
    row = page.items[0]
    blob = f"{row.get('content', '')} {row.get('content_preview', '')}"
    assert RAW_EMAIL not in blob, f"raw PII leaked to grantee on pending disclosure: {row}"


def test_store_completed_disclosure_serves_redacted_to_grantee() -> None:
    conn = _migrated_conversation_messages()
    conn.execute(
        """
        INSERT INTO conversation_messages
            (message_id, conversation_id, sender_id, source_id, content,
             content_disclosure, created_at)
        VALUES ('m1', 'c1', 'u1', 'src1', ?, ?, '2026-01-01')
        """,
        (RAW_CONTENT, "Reach me at [EMAIL] tomorrow"),
    )
    conn.commit()

    store = SQLiteCanonicalStore(conn)
    page = store.list("conversation_messages", disclosure_tier="default_disclosure", limit=10)

    row = page.items[0]
    assert RAW_EMAIL not in str(row.get("content", ""))
    assert "[EMAIL]" in str(row.get("content", ""))


def test_policy_pending_disclosure_does_not_leak_raw_to_grantee() -> None:
    # In-memory / direct-call path: raw content present, no disclosure column at all.
    rows = [{"record_id": "m1", "content": RAW_CONTENT}]
    out = apply_disclosure_tier_to_rows(
        rows, table="conversation_messages", tier="default_disclosure"
    )
    assert RAW_EMAIL not in str(out[0].get("content", "")), (
        f"raw PII leaked to grantee via policy layer on pending disclosure: {out[0]}"
    )


def test_policy_owner_still_sees_raw() -> None:
    rows = [{"record_id": "m1", "content": RAW_CONTENT}]
    out = apply_disclosure_tier_to_rows(rows, table="conversation_messages", tier="owner_raw")
    assert out[0]["content"] == RAW_CONTENT
