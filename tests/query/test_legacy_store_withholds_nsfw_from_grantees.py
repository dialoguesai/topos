"""A grantee's legacy query never serves a row the owner's NSFW decision withholds.

`content_nsfw` (migration canonical_nsfw_v1, written at every write by
`disclosure/nsfw_tags.py`) withholds a row from every share. The legacy query
door reads canonical rows through `SQLiteCanonicalStore.list`, whose list specs
never selected the flag, so `exclude_nsfw_rows_for_grantee` and the summary
scrub downstream saw no flag and passed every row. The in-memory adapter applies
`apply_grantee_content_policy` and withheld the row, so the suites built on it
stayed green while every node served the flagged row's disclosed text to a
grantee under `health:read`, `messages:read` and `ai_conversations:read`.

These tests go through the real door: a `type: "query"` message, shaped exactly
as the control plane forwards `shared_query_scope` (`is_grantee_request`,
`disclosure_tier=default_disclosure`, the grant's filters), dispatched by
`handle_control_plane_request` with the relay principal, over the SQLite
adapters a node builds for that connection. Rows are invented.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, THIRD_PARTY, Principal

#: The word every row shares, so one ask reaches all of them.
NEEDLE = "quillfeather"

#: scope -> (table, flagged row's disclosed text, unflagged row's disclosed text, flagged row's raw text)
CASES = {
    "health:read": ("journal_entries", "quillfeather lanternfish evening", "quillfeather heronmoss morning",
                    "quillfeather rawjournal evening"),
    "messages:read": ("conversation_messages", "quillfeather lanternfish note", "quillfeather heronmoss note",
                      "quillfeather rawmessage note"),
    "ai_conversations:read": ("ai_chat_messages", "quillfeather lanternfish prompt", "quillfeather heronmoss prompt",
                              "quillfeather rawprompt"),
}


def _schema(conn: sqlite3.Connection) -> None:
    """The node's own DDL: migrations, the messenger tables, the AI-chat tables."""
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations

    apply_all_migrations(conn)
    ensure_all_tables(conn)
    CanonicalTablesManager(conn)


def _seed(conn: sqlite3.Connection) -> None:
    journal = CASES["health:read"]
    conn.execute(
        "INSERT INTO journal_entries (entry_id, content, content_disclosure, content_nsfw, source_id, entry_at) VALUES"
        " ('je-withheldrow', ?, ?, 1, 'demo_journal_file', '2026-07-01T19:00:00Z'),"
        " ('je-plainrow', 'quillfeather rawjournal morning', ?, 0, 'demo_journal_file', '2026-07-02T08:00:00Z')",
        (journal[3], journal[1], journal[2]),
    )
    messages = CASES["messages:read"]
    conn.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, content,"
        " content_disclosure, content_nsfw, event_at, source_id, is_from_self) VALUES"
        " ('cm-withheldrow', 'cv-a', 'ds-a', 'sender-a', ?, ?, 1, '2026-07-01T19:00:00Z', 'imessage', 1),"
        " ('cm-plainrow', 'cv-a', 'ds-a', 'sender-a', 'quillfeather rawmessage other', ?, 0,"
        " '2026-07-02T19:00:00Z', 'imessage', 1)",
        (messages[3], messages[1], messages[2]),
    )
    conn.execute(
        "INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, updated_at)"
        " VALUES ('ac-a', 'owner-a', 't', 'chatgpt_ui_conversation', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')"
    )
    chats = CASES["ai_conversations:read"]
    conn.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, content_disclosure,"
        " content_nsfw, source_id) VALUES"
        " ('am-withheldrow', 'ac-a', 'user', '2026-07-01T19:00:00Z', ?, ?, 1, 'chatgpt_ui_conversation'),"
        " ('am-plainrow', 'ac-a', 'user', '2026-07-02T19:00:00Z', 'quillfeather rawprompt other', ?, 0,"
        " 'chatgpt_ui_conversation')",
        (chats[3], chats[1], chats[2]),
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "legacy-store.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    _schema(c)
    _seed(c)
    # The handler and the pipeline both ask the process for its connection; the
    # node would answer with its own database, the test answers with this one.
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    yield c
    c.close()


#: The filters of a grant that caps its grantee at summary.
SUMMARY_GRANT = {"access_mode_ceiling": "summary"}


async def _ask(scope: str, *, grantee: bool = True, mode: str = "summary", grant_filters=SUMMARY_GRANT) -> dict:
    payload = {
        "scope_id": scope,
        "access_mode": mode,
        "intent": NEEDLE,
        "query": NEEDLE,
        "query_session_id": f"legacy-store-{uuid.uuid4().hex[:8]}",
    }
    if grantee:
        # What control_plane/mcp_query.py builds for `shared_query_scope`. A grant with no
        # filters forwards none (`if filter_manifest:`), so `grant_filters=None` omits the key.
        payload.update(
            disclosure_tier="default_disclosure",
            disclosure_ceiling="default",
            owner_user_id="owner-a",
            owner_id="owner-a",
            requester_id="owner-a",
        )
        if grant_filters is not None:
            payload["filter_manifest"] = grant_filters
        principal = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="outside-client", acting_user="owner-a")
    else:
        principal = Principal(cls=OWNER_APP, channel="uds")
    out = await handle_control_plane_request(
        {"id": str(uuid.uuid4()), "type": "query", "payload": payload}, principal=principal
    )
    assert out.get("status") == "ok", out
    return out["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", sorted(CASES))
async def test_a_grantee_never_receives_a_flagged_row(conn, scope):
    _table, flagged, plain, raw = CASES[scope]
    payload = await _ask(scope)
    assert payload.get("turn_outcome") == "live_query", payload
    assert payload.get("disclosure_tier") == "default_disclosure"
    wire = json.dumps(payload, default=str)
    # The positive control: the door is open and serves disclosed text.
    assert plain in wire, "the unflagged row must still reach the grantee"
    # The flagged row, in any form: its disclosed copy, its raw text, its id.
    assert flagged not in wire, f"{scope}: the NSFW-flagged row's disclosed text reached a grantee"
    assert raw not in wire
    assert "withheldrow" not in wire


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", sorted(CASES))
async def test_raw_mode_withholds_a_flagged_row_too(conn, scope):
    """A grant whose filters set no lower ceiling reaches rows directly.

    The node's manifest ceiling for these scopes is raw, and the control plane checks a
    grant's ceiling only when the grant sets one. Raw mode is not under the node-wide
    black-hole floor (it screens rows one by one), so on a node holding a black hole this
    was still a way in. The withhold sits in the store, so it holds here as well.
    """
    _table, flagged, plain, raw = CASES[scope]
    payload = await _ask(scope, mode="raw", grant_filters=None)
    assert (payload.get("public_result") or {}).get("rows"), payload
    wire = json.dumps(payload, default=str)
    assert plain in wire, "the unflagged row still reaches the grantee"
    assert flagged not in wire and raw not in wire and "withheldrow" not in wire


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", sorted(CASES))
async def test_the_owner_still_reads_the_flagged_row(conn, scope):
    """The withhold is a share rule: the owner's own tier keeps every row."""
    _table, _flagged, _plain, raw = CASES[scope]
    payload = await _ask(scope, grantee=False)
    assert payload.get("disclosure_tier") == "owner_raw"
    assert raw in json.dumps(payload, default=str)


@pytest.mark.parametrize("table", ["journal_entries", "conversation_messages", "ai_chat_messages"])
def test_the_store_count_does_not_see_a_withheld_row(conn, table):
    """`total` is part of the answer: a withheld row must not be counted, or the count is an oracle."""
    from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore

    store = SQLiteCanonicalStore(conn)
    owner = store.list(table, limit=10, disclosure_tier="owner_raw")
    grantee = store.list(table, limit=10, disclosure_tier="default_disclosure")
    assert owner.total == 2 and len(owner.items) == 2
    assert grantee.total == 1 and [row["record_id"].endswith("plainrow") for row in grantee.items] == [True]


def test_a_table_that_cannot_show_its_flag_lists_nothing_below_the_owner(conn):
    """No `content_nsfw` column: no row can be shown unflagged, so a grantee gets none. The owner is unaffected.

    The store's own constructor adds the column, so this is the state an ALTER that failed leaves behind.
    """
    from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore

    store = SQLiteCanonicalStore(conn)
    conn.execute("ALTER TABLE journal_entries DROP COLUMN content_nsfw")
    assert store.list("journal_entries", disclosure_tier="default_disclosure").items == []
    assert len(store.list("journal_entries", disclosure_tier="owner_raw").items) == 2


@pytest.mark.parametrize(
    "stored",
    [1, 0, None, "1", "0", "true", " TRUE ", "yes", "nsfw", "NSFW", "no", "false", "", 2, 1.0, 0.0, "1.0"],
)
def test_the_sql_flag_reads_exactly_as_is_record_nsfw(stored):
    """One decision, two spellings: the store's SQL and the Python helper every sibling read calls."""
    from topos.disclosure.content_policy import is_record_nsfw
    from topos.storage.adapters.sqlite.stores import _NSFW_FLAGGED_SQL

    c = sqlite3.connect(":memory:")
    # The production declaration: INTEGER affinity, as canonical_nsfw_v1 adds it.
    c.execute("CREATE TABLE t (content_nsfw INTEGER DEFAULT 0)")
    c.execute("INSERT INTO t (content_nsfw) VALUES (?)", (stored,))
    held = c.execute("SELECT content_nsfw FROM t").fetchone()[0]
    in_sql = bool(c.execute(f"SELECT {_NSFW_FLAGGED_SQL} FROM t").fetchone()[0])
    assert in_sql is is_record_nsfw({"content_nsfw": held}), (stored, held)


def test_the_store_names_the_same_tables_as_the_tagger():
    from topos.disclosure.nsfw_tags import TABLES
    from topos.storage.adapters.sqlite.stores import _NSFW_TAGGED_TABLES

    assert _NSFW_TAGGED_TABLES == frozenset(TABLES)
