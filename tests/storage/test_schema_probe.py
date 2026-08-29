"""A schema probe must tell "not created yet" apart from "connection broken".

The distinction is load-bearing. Both probes below used to swallow every
exception and answer "absent", so a connection that could not execute anything
sent its caller into the DDL path — which takes the write gate, a blocking OS
lock, on whatever thread asked. On 2026-08-17 that thread was the event loop, and
the stall took the control-plane keepalive down with it. The DDL never had a
chance of working: the connection, not the schema, was broken.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.schema_probe import (
    UnusableConnection,
    describe_unusable,
    probe_bool,
)


class _PoisonedConnection:
    """Stands in for a connection whose sqlite3 statement cache is corrupted.

    The real failure raises ``KeyError`` whose key is the 1-tuple ``(sql,)`` of an
    unrelated statement — the one stranded at the head of the cache's LRU list
    when two threads raced its eviction path.
    """

    STRANDED_SQL = (
        "\n        SELECT id, owner_user_id FROM routines\n        WHERE id = ?\n        "
    )

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.executed.append(sql)
        raise KeyError((self.STRANDED_SQL,))

    def rollback(self) -> None:
        pass


def test_probe_reports_absent_for_a_locked_database():
    """OperationalError stays in the "run the DDL" bucket — it can genuinely succeed."""

    def _locked() -> bool:
        raise sqlite3.OperationalError("database is locked")

    assert probe_bool(_locked, what="thing") is False


def test_probe_raises_for_a_connection_that_cannot_execute():
    def _poisoned() -> bool:
        raise KeyError(("SELECT 1",))

    with pytest.raises(UnusableConnection):
        probe_bool(_poisoned, what="thing")


def test_probe_passes_a_real_answer_through():
    assert probe_bool(lambda: True, what="thing") is True
    assert probe_bool(lambda: False, what="thing") is False


def test_describe_names_the_real_cause_not_the_misleading_payload():
    try:
        probe_bool(lambda: (_ for _ in ()).throw(KeyError(("SELECT 1",))), what="thing")
    except UnusableConnection as exc:
        described = describe_unusable(exc)
    assert "statement cache" in described
    assert "cross-thread" in described


def test_canonical_manager_does_not_run_ddl_on_a_broken_connection(caplog):
    """The regression: probe fails -> DDL anyway -> write gate on the event loop."""
    conn = _PoisonedConnection()

    with caplog.at_level("ERROR"):
        # Must not raise: callers of this manager degrade, they do not crash.
        # It must also not attempt the DDL.
        manager = CanonicalTablesManager.__new__(CanonicalTablesManager)
        manager.conn = conn
        manager._ensure_tables()

    # Exactly one statement attempted: the probe. No CREATE TABLE followed it.
    assert len(conn.executed) == 1
    assert "sqlite_master" in conn.executed[0]
    assert not any("CREATE TABLE" in sql.upper() for sql in conn.executed)
    assert "Canonical DDL skipped" in caplog.text


def test_canonical_manager_still_creates_tables_on_a_healthy_empty_database(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    manager = CanonicalTablesManager.__new__(CanonicalTablesManager)
    manager.conn = conn
    manager._ensure_tables()
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"ai_chat_conversations", "ai_chat_messages"} <= names


def test_source_settings_reads_degrade_without_taking_the_gate():
    from topos.storage import source_settings

    conn = _PoisonedConnection()
    result = source_settings.get_source_settings(conn, "ds", "src")

    # Same defaults as before — the caller's contract is unchanged.
    assert result == {
        "enabled": True,
        "last_sync_at": None,
        "last_error": None,
        "posture": None,
        "exclude_spam": True,
    }
    # And only the PRAGMA probe ran: no CREATE TABLE, so no write gate.
    assert len(conn.executed) == 1
    assert conn.executed[0].startswith("PRAGMA table_info")
