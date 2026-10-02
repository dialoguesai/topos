"""The NSFW tag sweep and the write-time tag: who decides, what is written, what is left alone, and what it tells
the permissions refresh loop. Every row here is invented."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.disclosure import nsfw_tags
from topos.disclosure.nsfw_tags import (CLEARED_KEY, RULE_ID, STATE_KEY, TABLES, Retag, cleared_total, decide,
                                        tag_generation, tag_stored)
from topos.sanitization.explicit_wording import Verdict, evaluate
from topos.storage.db.migrations.canonical_nsfw_v1 import apply_canonical_nsfw_v1_up

CLASSIFIER = "michellejieli/NSFW_text_classifier"
RECHECKED = CLASSIFIER + "+cutoff-recheck>0.91"
ID = {"journal_entries": "entry_id", "conversation_messages": "message_id", "ai_chat_messages": "message_id"}
ORDINARY = "Dinner with my partner at the new place, then a long walk home."
EXPLICIT = "they were sexting all night"
PHRASE = "we hooked up last night"

# (table, id, content, content_nsfw, score, model) -> (flag, score, model) after the sweep, and the outcome.
ROWS = (
    ("journal_entries", "j-legacy-wrong", ORDINARY, 1, 0.97, CLASSIFIER, (0, 0.0, RULE_ID), "cleared"),
    ("journal_entries", "j-legacy-right", EXPLICIT, 1, 0.98, CLASSIFIER, (1, 0.98, CLASSIFIER), "kept_legacy_flag"),
    ("journal_entries", "j-legacy-clear", ORDINARY, 0, 0.12, CLASSIFIER, (0, 0.12, CLASSIFIER), "kept_legacy_clear"),
    ("journal_entries", "j-rechecked", ORDINARY, 0, 0.8, RECHECKED, (0, 0.8, RECHECKED), "kept_legacy_clear"),
    ("journal_entries", "j-missed", PHRASE, 0, 0.3, CLASSIFIER, (1, 0.5, RULE_ID), "flagged"),
    ("conversation_messages", "m-never", ORDINARY, 0, None, None, (0, 0.0, RULE_ID), "tagged"),
    ("conversation_messages", "m-never-explicit", EXPLICIT, 0, None, None, (1, 1.0, RULE_ID), "tagged"),
    ("conversation_messages", "m-empty", "", 0, None, None, (0, 0.0, RULE_ID), "tagged"),
    ("conversation_messages", "m-long", "An ordinary line about the weather. " * 30 + EXPLICIT, 0, 0.1, CLASSIFIER,
     (1, 1.0, RULE_ID), "flagged"),
    ("ai_chat_messages", "a-legacy-wrong", "Help me plan a gym routine and a dinner menu.", 1, 0.97, CLASSIFIER,
     (0, 0.0, RULE_ID), "cleared"),
    ("ai_chat_messages", "a-rule-old", PHRASE, 1, 0.9, "explicit-wording/v0", (1, 0.5, RULE_ID), "relabelled"),
    ("ai_chat_messages", "a-rule-current", ORDINARY, 0, 0.0, RULE_ID, (0, 0.0, RULE_ID), "unchanged"),
    ("ai_chat_messages", "a-null", None, 0, None, None, (0, 0.0, RULE_ID), "tagged"),
)
CLEARS = sum(1 for row in ROWS if row[-1] == "cleared")


def build(conn: sqlite3.Connection, tables=TABLES, rows=ROWS) -> None:
    for table in tables:
        conn.execute(f"CREATE TABLE {table} ({ID[table]} TEXT NOT NULL PRIMARY KEY, content TEXT, source_id TEXT)")
    apply_canonical_nsfw_v1_up(conn)
    conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                 "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    for table, rid, content, flag, score, model, *_rest in rows:
        if table in tables:
            conn.execute(f"INSERT INTO {table} ({ID[table]}, content, source_id, content_nsfw, content_nsfw_score, "
                         "content_nsfw_model) VALUES (?, ?, 'src-1', ?, ?, ?)", (rid, content, flag, score, model))
    conn.commit()


def tags(conn, table, rid):
    return tuple(conn.execute(f"SELECT content_nsfw, content_nsfw_score, content_nsfw_model FROM {table} "
                              f"WHERE {ID[table]}=?", (rid,)).fetchone())


def dump(conn):
    return {(table, row[0]): tuple(row) for table in TABLES
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {ID[table]}").fetchall()}


def state(conn):
    row = conn.execute("SELECT value FROM engine_config WHERE key=?", (STATE_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "canonical.db", check_same_thread=False)
    build(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def tagging_on(monkeypatch):
    import topos.config.settings as config

    monkeypatch.setattr(config.settings, "nsfw_classifier_enabled", True)


def sweep(conn, **kwargs):
    run = {key: kwargs.pop(key) for key in ("dry_run", "tables", "full") if key in kwargs}
    return Retag(lambda: conn, pause=0, **kwargs).run(**run)


# --- the decision ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("stored, verdict, expected", [
    ((0, None, None), Verdict(False), ((0, 0.0, RULE_ID), "tagged")),
    ((0, None, None), Verdict(True, "unambiguous"), ((1, 1.0, RULE_ID), "tagged")),
    ((1, 0.97, CLASSIFIER), Verdict(False), ((0, 0.0, RULE_ID), "cleared")),
    ((0, 0.4, CLASSIFIER), Verdict(True, "phrase"), ((1, 0.5, RULE_ID), "flagged")),
    ((1, 0.97, CLASSIFIER), Verdict(True, "unambiguous"), (None, "kept_legacy_flag")),
    ((0, 0.2, CLASSIFIER), Verdict(False), (None, "kept_legacy_clear")),
    ((0, 0.0, RULE_ID), Verdict(False), (None, "unchanged")),
    ((1, 1.0, RULE_ID), Verdict(True, "unambiguous"), (None, "unchanged")),
    ((1, 1.0, RULE_ID), Verdict(True, "phrase"), ((1, 0.5, RULE_ID), "relabelled")),
    ((1, 0.5, "explicit-wording/v0"), Verdict(True, "phrase"), ((1, 0.5, RULE_ID), "relabelled")),
    ((1, 1.0, RULE_ID), Verdict(False), ((0, 0.0, RULE_ID), "cleared")),
    ((0, 0.0, RULE_ID), Verdict(True, "unambiguous"), ((1, 1.0, RULE_ID), "flagged")),
    (("1", 0.97, CLASSIFIER), Verdict(True, "unambiguous"), (None, "kept_legacy_flag")),
])
def test_what_is_written_for_each_stored_tag(stored, verdict, expected):
    assert decide(*stored, verdict) == expected


# --- the sweep ---------------------------------------------------------------------------------------------------

def test_a_write_run_brings_every_row_to_the_rule_and_leaves_confirmed_legacy_tags_alone(conn):
    result = sweep(conn)
    for table, rid, *_stored, after, _outcome in ROWS:
        assert tags(conn, table, rid) == after, rid
        flag = after[0]
        assert flag == int(evaluate(conn.execute(f"SELECT content FROM {table} WHERE {ID[table]}=?",
                                                 (rid,)).fetchone()[0]).flagged), rid
    totals = result["totals"]
    for outcome in nsfw_tags.OUTCOMES:
        assert totals[outcome] == sum(1 for row in ROWS if row[-1] == outcome), outcome
    assert totals["rows"] == totals["evaluated"] == len(ROWS)
    assert result["finished"] and result["walked"] == "all" and result["generation"] == 1
    assert cleared_total(conn) == CLEARS and tag_generation(conn) == 1
    saved = state(conn)
    assert saved["rule"] == RULE_ID and all(saved["tables"][table]["complete"] for table in TABLES)


def test_a_second_run_writes_nothing_and_raises_nothing(conn):
    sweep(conn)
    before, changes = dump(conn), conn.total_changes
    result = sweep(conn)
    assert dump(conn) == before
    assert result["walked"] == "pending" and result["totals"]["evaluated"] == 0
    assert result["generation"] == 1 and not result["generation_raised"]
    # Only the state's own bookkeeping moved.
    assert conn.total_changes - changes <= 2 * len(TABLES) + 1


def test_a_dry_run_counts_what_a_write_run_would_do_and_writes_nothing(conn):
    before = dump(conn)
    dry = sweep(conn, dry_run=True)
    assert dump(conn) == before and state(conn) is None and cleared_total(conn) == 0
    wet = sweep(conn)
    assert {key: value for key, value in dry["totals"].items() if key != "not_written"} == \
        {key: value for key, value in wet["totals"].items() if key != "not_written"}


def test_an_interrupted_run_resumes_where_it_stopped_and_ends_where_one_run_would(tmp_path, conn):
    whole = sqlite3.connect(tmp_path / "whole.db")
    build(whole)
    Retag(lambda: whole, pause=0).run()
    calls = {"n": 0}

    def stop_after_two_batches():
        calls["n"] += 1
        return calls["n"] > 2

    first = Retag(lambda: conn, pause=0, batch_rows=2, stop=stop_after_two_batches).run()
    assert first["stopped"] and not first["finished"] and first["generation"] == 0
    saved = state(conn)
    cursor = saved["tables"]["journal_entries"]["cursor"]
    assert cursor == 4 and not saved["tables"]["journal_entries"]["complete"]
    walked = []
    real = nsfw_tags.Retag._evaluate

    def spy(self, conn_, table, rows, *args, **kwargs):
        walked.extend((table, row[0]) for row in rows)
        return real(self, conn_, table, rows, *args, **kwargs)

    nsfw_tags.Retag._evaluate = spy
    try:
        second = Retag(lambda: conn, pause=0, batch_rows=2).run()
        assert second["finished"] and second["generation"] == 1
        # The resumed run read the journal only past where the first one stopped, and every other row once.
        assert sorted(rowid for table, rowid in walked if table == "journal_entries") == [5]
        assert len(walked) == len(ROWS) - cursor
        walked.clear()
        third = Retag(lambda: conn, pause=0, batch_rows=2).run()
        assert third["walked"] == "pending" and walked == []
    finally:
        nsfw_tags.Retag._evaluate = real
    assert dump(conn) == dump(whole)
    assert cleared_total(conn) == cleared_total(whole) == CLEARS
    whole.close()


def test_a_forced_run_walks_every_row_from_the_top(conn):
    sweep(conn)
    result = sweep(conn, full=True)
    assert result["walked"] == "all" and result["totals"]["evaluated"] == len(ROWS)
    assert result["totals"]["cleared"] == result["totals"]["tagged"] == 0


def test_a_row_rewritten_between_read_and_write_is_left_to_its_writer(conn):
    real = nsfw_tags.Retag._evaluate

    def rewrite_first(self, conn_, table, rows, *args, **kwargs):
        if table == "journal_entries":
            conn_.execute("UPDATE journal_entries SET content=? WHERE entry_id='j-legacy-wrong'", (EXPLICIT,))
        return real(self, conn_, table, rows, *args, **kwargs)

    nsfw_tags.Retag._evaluate = rewrite_first
    try:
        result = sweep(conn)
    finally:
        nsfw_tags.Retag._evaluate = real
    assert tags(conn, "journal_entries", "j-legacy-wrong") == (1, 0.97, CLASSIFIER)
    assert result["tables"]["journal_entries"]["not_written"] == 1


def test_a_missing_table_is_complete_so_the_check_stays_cheap(tmp_path):
    connection = sqlite3.connect(tmp_path / "partial.db")
    build(connection, tables=("journal_entries",))
    first = Retag(lambda: connection, pause=0).run()
    assert set(first["tables"]) == {"journal_entries"} and first["finished"]
    assert all(state(connection)["tables"][table]["complete"] for table in TABLES)
    assert Retag(lambda: connection, pause=0).run()["walked"] == "pending"
    connection.close()


def test_the_pending_check_finds_a_row_a_path_wrote_without_a_tag(conn):
    sweep(conn)
    conn.execute("INSERT INTO conversation_messages (message_id, content, source_id) VALUES ('m-late', ?, 'src-1')",
                 (EXPLICIT,))
    conn.commit()
    result = sweep(conn)
    assert result["walked"] == "pending" and result["totals"]["tagged"] == 1
    assert tags(conn, "conversation_messages", "m-late") == (1, 1.0, RULE_ID)


def test_a_new_rule_version_walks_every_row_again(conn, monkeypatch):
    sweep(conn)
    monkeypatch.setattr(nsfw_tags, "RULE_ID", "explicit-wording/v9")
    result = sweep(conn)
    assert result["walked"] == "all" and result["totals"]["evaluated"] == len(ROWS)
    assert state(conn)["rule"] == "explicit-wording/v9"


def test_a_partial_run_by_table_leaves_the_others_for_later(conn):
    result = sweep(conn, tables=["journal_entries"], full=True)
    assert set(result["tables"]) == {"journal_entries"}
    assert tags(conn, "conversation_messages", "m-never") == (0, None, None)
    with pytest.raises(ValueError):
        sweep(conn, tables=["signal_objects"])
    with pytest.raises(ValueError):
        sweep(conn, tables=[])


# --- the cleared counter and the refresh loop's generation ----------------------------------------------------

def test_a_clear_at_a_write_is_counted_and_raises_the_generation_at_the_next_check(conn):
    sweep(conn)
    assert tag_generation(conn) == 1
    conn.execute("UPDATE journal_entries SET content=? WHERE entry_id='j-legacy-right'", (ORDINARY,))
    assert tag_stored(conn, "journal_entries", "j-legacy-right") == "cleared"
    conn.commit()
    assert cleared_total(conn) == CLEARS + 1 and tag_generation(conn) == 1
    result = sweep(conn)
    assert result["generation_raised"] and tag_generation(conn) == 2
    assert not sweep(conn)["generation_raised"]


def test_a_set_flag_raises_nothing(tmp_path):
    connection = sqlite3.connect(tmp_path / "set.db")
    build(connection, rows=[row for row in ROWS if row[-1] in ("flagged", "tagged", "kept_legacy_flag")])
    result = Retag(lambda: connection, pause=0).run()
    assert result["totals"]["flagged"] >= 1 and result["generation"] == 0 and tag_generation(connection) is None
    connection.close()


def test_a_rolled_back_clear_is_not_counted(conn):
    conn.execute("UPDATE journal_entries SET content=? WHERE entry_id='j-legacy-right'", (ORDINARY,))
    conn.commit()
    assert tag_stored(conn, "journal_entries", "j-legacy-right") == "cleared"
    conn.rollback()
    assert cleared_total(conn) == 0 and tags(conn, "journal_entries", "j-legacy-right") == (1, 0.98, CLASSIFIER)


def test_the_refresh_loops_proof_digest_moves_with_the_generation_and_only_then(conn):
    from topos.permissions_v2.refresh_loop import proof_digest

    untouched = proof_digest(conn, owner_id="owner-1")
    conn.execute("INSERT INTO engine_config (key, value) VALUES (?, ?)", (STATE_KEY, json.dumps(
        {"version": nsfw_tags.STATE_VERSION, "rule": RULE_ID, "tables": {}, "generation": 0})))
    assert proof_digest(conn, owner_id="owner-1") == untouched
    sweep(conn)
    first = proof_digest(conn, owner_id="owner-1")
    assert first != untouched
    sweep(conn)
    assert proof_digest(conn, owner_id="owner-1") == first


def test_an_unreadable_state_is_no_generation(conn):
    conn.execute("INSERT INTO engine_config (key, value) VALUES (?, ?)", (STATE_KEY, "{not json"))
    assert tag_generation(conn) is None
    conn.execute("INSERT INTO engine_config (key, value) VALUES (?, ?)", (CLEARED_KEY, "many"))
    assert cleared_total(conn) == 0


# --- the switch ----------------------------------------------------------------------------------------------------

def test_switched_off_nothing_is_written_and_a_dry_run_still_counts(conn, monkeypatch):
    import topos.config.settings as config

    monkeypatch.setattr(config.settings, "nsfw_classifier_enabled", False)
    before = dump(conn)
    result = sweep(conn)
    assert result["disabled"] and not result["finished"] and dump(conn) == before
    assert tag_stored(conn, "journal_entries", "j-legacy-wrong") is None
    nsfw_tags.tag_inserted(conn, "conversation_messages", "m-never", EXPLICIT, present=True)
    assert dump(conn) == before
    assert sweep(conn, dry_run=True)["totals"]["cleared"] == CLEARS


# --- the write paths ------------------------------------------------------------------------------------------------

def test_tag_stored_adds_the_columns_to_a_table_that_lacks_them(tmp_path):
    connection = sqlite3.connect(tmp_path / "bare.db")
    connection.execute("CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, content TEXT)")
    connection.execute("INSERT INTO journal_entries VALUES ('j-1', ?)", (EXPLICIT,))
    assert tag_stored(connection, "journal_entries", "j-1") == "tagged"
    assert tags(connection, "journal_entries", "j-1") == (1, 1.0, RULE_ID)
    assert tag_stored(connection, "journal_entries", "j-1") is None
    assert tag_stored(connection, "journal_entries", "j-missing") is None
    assert tag_stored(connection, "documents", "d-1") is None
    connection.close()


def _canonical_store(tmp_path):
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations

    connection = sqlite3.connect(tmp_path / "store.db", check_same_thread=False)
    apply_all_migrations(connection)
    ensure_all_tables(connection)
    # A node always has engine_config (core.state creates it at startup); the cleared count lives there.
    connection.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.commit()
    return connection, SQLiteCanonicalStore(connection)


@pytest.mark.parametrize("table, record", [
    ("conversation_messages", {"message_id": "imessage:1", "conversation_id": "c-1", "dataset_id": "d-1",
                               "source_id": "imessage", "sender_type": "human", "sender_id": "s-1",
                               "event_at": "2026-09-15T10:00:00+00:00", "content": EXPLICIT}),
    ("journal_entries", {"entry_id": "j-1", "source_id": "journal", "content": EXPLICIT,
                         "entry_at": "2026-09-15T10:00:00+00:00"}),
    ("ai_chat_messages", {"message_id": "a-1", "conversation_id": "c-1", "sender_type": "human",
                          "event_at": "2026-09-15T10:00:00+00:00", "source_id": "chatgpt", "content": EXPLICIT}),
])
def test_every_canonical_write_is_tagged_from_the_text_the_row_holds(tmp_path, table, record):
    connection, store = _canonical_store(tmp_path)
    if table == "ai_chat_messages":
        from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager

        CanonicalTablesManager(connection)
    store.upsert(table, record)
    rid = record[ID[table]]
    assert tags(connection, table, rid) == (1, 1.0, RULE_ID)
    # A rewrite of the body re-decides the tag, and the clear is counted for the refresh loop.
    store.upsert(table, {**record, "content": ORDINARY})
    stored = connection.execute(f"SELECT content FROM {table} WHERE {ID[table]}=?", (rid,)).fetchone()[0]
    assert tags(connection, table, rid) == ((0, 0.0, RULE_ID) if stored == ORDINARY else (1, 1.0, RULE_ID))
    if stored == ORDINARY:
        assert cleared_total(connection) == 1
    connection.close()


def test_the_messenger_sync_batch_tags_every_row(tmp_path):
    connection, _store = _canonical_store(tmp_path)
    from topos.storage.canonical.conversations_tables import ConversationsTablesManager

    records = [{"message_id": f"imessage:{n}", "thread_id": "thread-1", "dataset_id": "d-1", "source_id": "imessage",
                "sender_type": "human", "sender_id": "s-1", "ts": "2026-09-15T10:00:00+00:00",
                "content": text} for n, text in enumerate((ORDINARY, EXPLICIT, PHRASE, ""))]
    ConversationsTablesManager(connection).upsert_message_batch(records, "d-1", "imessage")
    found = dict(connection.execute("SELECT message_id, content_nsfw FROM conversation_messages "
                                    "WHERE content_nsfw_model=?", (RULE_ID,)).fetchall())
    assert found == {"imessage:0": 0, "imessage:1": 1, "imessage:2": 1, "imessage:3": 0}
    assert connection.execute("SELECT COUNT(*) FROM conversation_messages WHERE content_nsfw_model IS NULL "
                              "AND content IS NOT NULL AND trim(content) <> ''").fetchone()[0] == 0
    connection.close()


def test_a_fresh_conversations_table_has_the_tag_columns(tmp_path):
    from topos.storage.canonical.conversations_tables import ensure_all_tables

    connection = sqlite3.connect(tmp_path / "fresh.db")
    ensure_all_tables(connection)
    assert nsfw_tags.columns_present(connection, "conversation_messages")
    connection.close()


# --- the reviewed surface --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("table", ["conversation_messages", "ai_chat_messages", "journal_entries"])
def test_a_rule_tagged_rows_score_and_id_are_operational_and_its_flag_is_not(table):
    """Tagging a never-tagged row, or a new rule version, stales no review; a flag change does; a classifier-tagged
    row keeps its score and id in the surface exactly as before."""
    from topos.permissions_v2.evidence import _row_revision

    base = {"message_id": "m-1", "content": ORDINARY, "content_nsfw": 0}
    untagged = _row_revision(base, table=table)
    assert _row_revision({**base, "content_nsfw_score": 0.0, "content_nsfw_model": RULE_ID}, table=table) == untagged
    assert _row_revision({**base, "content_nsfw_score": 0.5, "content_nsfw_model": "explicit-wording/v2"},
                         table=table) == untagged
    assert _row_revision({**base, "content_nsfw": 1, "content_nsfw_score": 1.0, "content_nsfw_model": RULE_ID},
                         table=table) != untagged
    legacy = _row_revision({**base, "content_nsfw_score": 0.2, "content_nsfw_model": CLASSIFIER}, table=table)
    assert legacy != untagged
    assert legacy != _row_revision({**base, "content_nsfw_score": 0.3, "content_nsfw_model": CLASSIFIER}, table=table)


# --- the node's own start -------------------------------------------------------------------------------------------

def test_the_startup_task_sweeps_without_an_owner_command_and_stops_with_the_runtime(conn, monkeypatch):
    import asyncio

    import topos.runtime_shutdown as runtime

    before = dump(conn)
    monkeypatch.setattr(runtime, "is_shutdown_requested", lambda: True)   # the node is already going down
    asyncio.run(nsfw_tags.run_at_startup(lambda: conn, delay=0, interval=3600))
    # The stop is checked before every batch: a shutdown leaves the rows for the next start, and the task returns.
    assert dump(conn) == before and tag_generation(conn) is None
    monkeypatch.setattr(runtime, "is_shutdown_requested", lambda: False)
    ticks = []

    async def stop_after_first_wait(seconds):
        ticks.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_first_wait)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(nsfw_tags.run_at_startup(lambda: conn, delay=0, interval=1800))
    assert ticks == [1800] and tag_generation(conn) == 1
    assert tags(conn, "journal_entries", "j-legacy-wrong") == (0, 0.0, RULE_ID)


def test_a_failing_sweep_is_logged_by_class_and_never_raises(monkeypatch, caplog):
    import asyncio

    import topos.runtime_shutdown as runtime

    monkeypatch.setattr(runtime, "is_shutdown_requested", lambda: True)
    with caplog.at_level("WARNING", logger="topos.disclosure.nsfw_tags"):
        asyncio.run(nsfw_tags.run_at_startup(lambda: None, delay=0))
    assert "NSFW tag sweep failed (RuntimeError)" in caplog.text
