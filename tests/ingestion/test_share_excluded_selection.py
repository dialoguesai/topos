"""Every row-selection site that feeds enrichment, signals, embeddings, facts or a grantee skips ``share_excluded = 1``.

The runner keeps flagged rows out of the batch it hands the post-canonical pipeline; these are
the OTHER ways a row reaches a derivation or a share: the reload used by reprocess/backfill and
the manual-process door, the brief loader, goal evidence, the grantee list in the SQLite store
and the aggregate verb. Each site is seeded with one flagged and one plain row (invented) and
must return only the plain one. A table that predates the column is unaffected.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal

SOURCE = "claude_code_sessions"
PLAIN = "claude_code:plain-1"
FLAGGED = "claude_code:flagged-1"


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    from tests.ingestion.ai_tool_fixtures import build_node

    db = build_node(tmp_path / "selection.db")
    db.execute(
        "INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, updated_at)"
        " VALUES ('claude_code:sess', 'owner-0000-1111', 'Lantern', ?, '2026-10-14T10:00:00Z', '2026-10-14T10:00:00Z')",
        (SOURCE,),
    )
    db.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, source_id,"
        " share_excluded, content_disclosure) VALUES"
        " (?, 'claude_code:sess', 'human', '2026-10-14T10:00:00Z', 'quillfeather plain words', ?, 0, 'quillfeather plain words'),"
        " (?, 'claude_code:sess', 'assistant', '2026-10-14T10:00:01Z', 'quillfeather flagged words', ?, 1, 'quillfeather flagged words')",
        (PLAIN, SOURCE, FLAGGED, SOURCE),
    )
    db.execute("INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) VALUES ('g-plain', ?, ?, 'finish the lantern', '{}')",
               (PLAIN, SOURCE))
    db.execute("INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) VALUES ('g-flagged', ?, ?, 'finish the amber', '{}')",
               (FLAGGED, SOURCE))
    db.commit()
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    yield db
    db.close()


def test_the_signal_reload_skips_flagged_rows(conn) -> None:
    from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
    from topos.sources.registry import REGISTRY

    ids = {r["message_id"] for r in load_canonical_records_for_signal(conn, REGISTRY[SOURCE])}
    assert ids == {PLAIN}


def test_the_manual_process_loader_skips_flagged_rows(conn) -> None:
    from topos.api.enrichment import _load_canonical_messages_for_enrichment
    from topos.sources.registry import REGISTRY

    ids = {r["message_id"] for r in _load_canonical_messages_for_enrichment(conn, REGISTRY[SOURCE])}
    assert ids == {PLAIN}


def test_the_legacy_ai_chat_fallback_loader_skips_flagged_rows(conn) -> None:
    from topos.api.enrichment import _legacy_ai_chat_messages_for_source

    ids = {r["message_id"] for r in _legacy_ai_chat_messages_for_source(conn, SOURCE, dataset_id=None)}
    assert ids == {PLAIN}
    with_owner = {r["message_id"] for r in _legacy_ai_chat_messages_for_source(conn, SOURCE, dataset_id="owner-0000-1111:topos:x")}
    assert with_owner == {PLAIN}


def test_the_brief_loader_skips_flagged_rows(conn) -> None:
    from topos.features.signal.brief_canonical_loader import load_canonical_messages_for_dimension

    rows = load_canonical_messages_for_dimension(conn, "memory", source_id=SOURCE)
    ids = {r["message_id"] for r in rows if r.get("canonical_table", "ai_chat_messages") == "ai_chat_messages"}
    assert PLAIN in ids and FLAGGED not in ids


def test_goal_evidence_never_cites_a_flagged_row(conn) -> None:
    from topos.features.derivation.goal_evidence import goal_sources

    cited = {g["record_id"] for g in goal_sources(conn)}
    assert cited == {PLAIN}


def test_the_store_lists_flagged_rows_to_the_owner_only(conn) -> None:
    from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore

    store = SQLiteCanonicalStore(conn)
    token = set_principal(Principal(cls=OWNER_APP, channel="uds"))
    try:
        owner = store.list("ai_chat_messages", limit=10, disclosure_tier="owner_raw")
    finally:
        reset_principal(token)
    token = set_principal(Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="outside"))
    try:
        grantee_raw = store.list("ai_chat_messages", limit=10, disclosure_tier="owner_raw")
        grantee = store.list("ai_chat_messages", limit=10, disclosure_tier="default_disclosure")
    finally:
        reset_principal(token)
    assert owner.total == 2 and {r["record_id"] for r in owner.items} == {PLAIN, FLAGGED}
    assert grantee.total == 1 and [r["record_id"] for r in grantee.items] == [PLAIN]
    assert grantee_raw.total == 1 and [r["record_id"] for r in grantee_raw.items] == [PLAIN]


def test_the_aggregate_verb_counts_flagged_rows_for_the_owner_only(conn) -> None:
    from topos.query.aggregate import run_aggregate, validate_aggregate_params

    class _Guard:
        def __init__(self, sees_everything: bool) -> None:
            self.sees_everything = sees_everything
            self.active = False

        def blocked_entity_ids(self):
            return set()

    spec = validate_aggregate_params({"scope_id": "ai_coding_sessions:read", "measure": "count"})
    owner = run_aggregate(conn, spec, guard=_Guard(True))
    other = run_aggregate(conn, spec, guard=_Guard(False))
    assert owner["rows"] == [{"value": 2}]
    assert other["rows"] == [{"value": 1}]


def test_a_table_without_the_column_is_read_as_before(tmp_path) -> None:
    """Older databases: no column means no filter, never an error."""
    from topos.storage.share_excluded import share_excluded_clause

    db = sqlite3.connect(str(tmp_path / "old.db"))
    db.execute("CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, content TEXT)")
    assert share_excluded_clause(db) == ""
    db.execute("ALTER TABLE ai_chat_messages ADD COLUMN share_excluded INTEGER NOT NULL DEFAULT 0")
    assert share_excluded_clause(db) == " AND share_excluded = 0"
    assert share_excluded_clause(db, alias="m") == " AND m.share_excluded = 0"
