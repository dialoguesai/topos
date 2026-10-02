"""The PII disclosure sweep: every canonical row with text gets the privacy layer's redacted copy, whatever wrote it.

Which rows it redacts, what it writes and what it leaves alone, how it resumes, how the layer's version keys it, and
that the paths which never ran the pipeline's privacy stage (the messenger sync on a fresh node, a healed body; the
snapshot lanes' tests sit beside those lanes) end with a disclosure a grantee read can serve. Every row here is invented; the filter is a
stand-in that masks the invented address and name it is given."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

import pytest

from topos.disclosure import disclosure_sweep
from topos.disclosure.disclosure_sweep import Sweep, request_run, run_at_startup, targets
from topos.disclosure.field_registry import DISCLOSURE_PENDING_PLACEHOLDER
from topos.disclosure.privacy_layer import disclosure_hash
from topos.storage.db.migrations.canonical_disclosure_v1 import apply_canonical_disclosure_v1_up

ADDRESS = "alice@example.com"
NAME = "Alice"
MODEL = "openai/privacy-filter"


def scrub(text: str) -> str:
    return text.replace(ADDRESS, "[EMAIL]").replace(NAME, "[NAME]")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Filter:
    """Stands in for ``PrivacyLayerClient``: records every call, masks the invented address and name, and can fail a
    record (an error beside its raw text, as ``redact_privacy_batch`` answers), fail the whole call, or run something
    while the call is in flight."""

    def __init__(self, *, fail=(), status="ok", during=None):
        self.calls = []
        self.fail = set(fail)
        self.status = status
        self.during = during

    @property
    def ids(self):
        return [key for call in self.calls for key in call]

    async def redact_batch(self, items):
        self.calls.append([item["id"] for item in items])
        if self.during is not None:
            self.during(items)
        if self.status != "ok":
            return {"status": self.status, "error": "filter down", "items": []}
        out = []
        for item in items:
            if item["id"] in self.fail:
                out.append({"id": item["id"], "text": item["text"], "error": "model failed"})
            else:
                out.append({"id": item["id"], "text": scrub(item["text"])})
        return {"items": out, "model": MODEL, "status": "ok", "privacy_layer_version": "1"}


# (table, id, field values) in insertion order; the stored disclosure state is set per row below.
ROWS = (
    ("journal_entries", "j-old", {"content": f"Lunch with {NAME}, wrote to {ADDRESS}"}),
    ("journal_entries", "j-current", {"content": "A quiet day at home."}),
    ("conversation_messages", "m-never", {"content": f"Call {NAME} about the trip"}),
    ("conversation_messages", "m-blank", {"content": "   "}),
    ("conversation_messages", "m-none", {"content": None}),
    ("conversation_messages", "m-stale", {"content": f"New plan, write to {ADDRESS}"}),
    ("conversation_messages", "m-new", {"content": f"{NAME} says hi"}),
    ("ai_chat_messages", "a-both", {"content": f"Draft a note to {ADDRESS}",
                                     "content_rendered": f"<p>Draft a note to {ADDRESS}</p>"}),
    ("ai_chat_messages", "a-half", {"content": "Plan a gym routine", "content_rendered": f"<p>{NAME}'s plan</p>"}),
    ("location_events", "l-place", {"place_name": f"{NAME}'s flat"}),
)
ID = {"journal_entries": "entry_id", "conversation_messages": "message_id", "ai_chat_messages": "message_id",
      "location_events": "event_id"}
FIELDS = {"journal_entries": ("content",), "conversation_messages": ("content",),
          "ai_chat_messages": ("content", "content_rendered"), "location_events": ("place_name",)}
#: Every field the sweep must redact on the first full walk, as the filter sees it (table:id:field).
NEEDED = ["journal_entries:j-old:content", "conversation_messages:m-new:content",
          "conversation_messages:m-stale:content", "conversation_messages:m-never:content",
          "ai_chat_messages:a-half:content_rendered", "ai_chat_messages:a-both:content",
          "ai_chat_messages:a-both:content_rendered", "location_events:l-place:place_name"]


def build(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, content TEXT, entry_at TEXT)")
    conn.execute("CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, content TEXT, is_from_self INTEGER)")
    conn.execute("CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, content TEXT, content_rendered TEXT)")
    conn.execute("CREATE TABLE location_events (event_id TEXT PRIMARY KEY, place_name TEXT)")
    apply_canonical_disclosure_v1_up(conn)
    for table, rid, values in ROWS:
        columns = [ID[table], *values]
        conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                     (rid, *values.values()))
    # Current: disclosed under version 1 by the pipeline (the plain hash of the text it redacted).
    conn.execute("UPDATE journal_entries SET content_disclosure=content, content_disclosure_hash=?, "
                 "content_disclosure_model=? WHERE entry_id='j-current'", (sha("A quiet day at home."), MODEL))
    conn.execute("UPDATE ai_chat_messages SET content_disclosure=content, content_disclosure_hash=?, "
                 "content_disclosure_model=? WHERE message_id='a-half'", (sha("Plan a gym routine"), MODEL))
    # Stale: a disclosure of text the row no longer holds (an upsert replaced the text and kept the columns).
    conn.execute("UPDATE conversation_messages SET content_disclosure='Old plan', content_disclosure_hash=?, "
                 "content_disclosure_model=? WHERE message_id='m-stale'", (sha("Old plan"), MODEL))
    conn.commit()


def disclosure(conn, table, rid, field="content"):
    return conn.execute(f"SELECT {field}_disclosure, {field}_disclosure_hash FROM {table} WHERE {ID[table]}=?",
                        (rid,)).fetchone()


def dump(conn):
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in ID}


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "canonical.db", check_same_thread=False)
    build(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def layer_on(monkeypatch):
    import topos.config.settings as config

    monkeypatch.setattr(config.settings, "platform_privacy_via_engine", True)
    disclosure_sweep._REQUESTED.clear()


def sweep(conn, client, mode="verify", **kwargs):
    run = {key: kwargs.pop(key) for key in ("dry_run", "tables") if key in kwargs}
    kwargs.setdefault("pause", 0)
    kwargs.setdefault("poll", 0)
    kwargs.setdefault("stage_active", lambda: False)
    return asyncio.run(Sweep(lambda: conn, client=client, **kwargs).run(mode=mode, **run))


# --- the hash that keys the layer ----------------------------------------------------------------------------------

def test_version_one_is_the_plain_hash_every_existing_disclosure_holds_and_a_later_version_is_not():
    assert disclosure_hash("hello", version="1") == sha("hello")
    assert disclosure_hash("hello", version="2") not in (sha("hello"), disclosure_hash("hello", version="3"))
    assert disclosure_hash("hello") == sha("hello")   # the shipped version is 1


# --- what a walk does ----------------------------------------------------------------------------------------------

def test_a_full_walk_redacts_every_field_without_a_current_disclosure_and_nothing_else(conn):
    client = Filter()
    result = sweep(conn, client)
    assert result["finished"] and not result["stopped"] and not result["unavailable"]
    assert sorted(client.ids) == sorted(NEEDED)
    for table, rid, values in ROWS:
        for field in FIELDS[table]:
            text = values.get(field)
            stored = disclosure(conn, table, rid, field)
            if not (isinstance(text, str) and text.strip()):
                assert stored == (None, None), rid          # nothing to disclose: left alone
            elif (table, rid, field) in {("journal_entries", "j-current", "content"),
                                         ("ai_chat_messages", "a-half", "content")}:
                assert stored == (text, sha(text)), rid      # already current: not sent, not rewritten
            else:
                assert stored == (scrub(text), sha(text)), rid
    assert conn.execute("SELECT content_disclosure_model FROM conversation_messages WHERE message_id='m-new'"
                        ).fetchone()[0] == MODEL
    totals = result["totals"]
    assert (totals["missing"], totals["stale"], totals["redacted"], totals["failed"]) == (7, 1, 8, 0)
    # A second walk finds nothing to do and calls nothing.
    again = Filter()
    assert sweep(conn, again)["totals"]["redacted"] == 0 and again.calls == []


def test_journal_then_messages_then_ai_chat_each_newest_first(conn):
    client = Filter()
    sweep(conn, client, chunk_rows=1)
    assert client.ids == NEEDED


def test_a_grantee_read_serves_the_disclosure_once_the_sweep_has_run(conn):
    from topos.disclosure.tier import apply_disclosure_tier_to_rows

    def read():
        row = conn.execute("SELECT message_id, content, content_disclosure FROM conversation_messages "
                           "WHERE message_id='m-never'").fetchone()
        return apply_disclosure_tier_to_rows([dict(zip(("message_id", "content", "content_disclosure"), row))],
                                             table="conversation_messages", tier="default_disclosure")[0]["content"]

    assert read() == DISCLOSURE_PENDING_PLACEHOLDER
    sweep(conn, Filter())
    assert read() == f"Call [NAME] about the trip"


def test_a_pending_walk_reads_only_fields_with_no_disclosure(conn):
    client = Filter()
    result = sweep(conn, client, mode="pending")
    assert "conversation_messages:m-stale:content" not in client.ids
    assert sorted(client.ids) == sorted(set(NEEDED) - {"conversation_messages:m-stale:content"})
    assert result["totals"]["stale"] == 0 and disclosure(conn, "conversation_messages", "m-stale")[0] == "Old plan"


def test_a_dry_run_counts_and_writes_nothing_and_calls_no_filter(conn):
    before = dump(conn)
    result = sweep(conn, None, dry_run=True)
    assert dump(conn) == before
    totals = result["totals"]
    assert (totals["missing"], totals["stale"], totals["current"], totals["calls"]) == (7, 1, 2, 0)
    assert result["tables"]["ai_chat_messages"]["missing"] == 3


# --- resuming, failing, racing --------------------------------------------------------------------------------------

def test_an_interrupted_walk_resumes_from_the_rows_themselves_and_redoes_nothing(conn):
    first = Filter()
    result = sweep(conn, first, chunk_rows=1, stop=lambda: len(first.calls) >= 3)
    assert result["stopped"] and not result["finished"] and len(first.calls) == 3
    second = Filter()
    assert sweep(conn, second)["finished"]
    assert sorted(first.ids + second.ids) == sorted(NEEDED)          # every field exactly once across both runs
    assert not set(first.ids) & set(second.ids)


def test_a_record_the_filter_failed_on_stays_empty_and_the_next_walk_retries_it(conn):
    result = sweep(conn, Filter(fail={"conversation_messages:m-never:content"}))
    assert result["totals"]["failed"] == 1 and result["finished"]
    assert disclosure(conn, "conversation_messages", "m-never") == (None, None)   # never its raw text
    retry = Filter()
    sweep(conn, retry)
    assert retry.ids == ["conversation_messages:m-never:content"]
    assert disclosure(conn, "conversation_messages", "m-never")[0] == "Call [NAME] about the trip"


def test_an_unavailable_filter_stops_the_walk_and_writes_nothing(conn):
    before = dump(conn)
    client = Filter(status="unavailable")
    result = sweep(conn, client)
    assert result["unavailable"] and not result["finished"] and len(client.calls) == 1
    assert dump(conn) == before


def test_a_row_rewritten_while_its_text_was_with_the_filter_keeps_what_its_writer_left(conn):
    def rewrite(items):
        if "conversation_messages:m-never:content" in [item["id"] for item in items]:
            conn.execute("UPDATE conversation_messages SET content='Rewritten meanwhile' WHERE message_id='m-never'")
            conn.commit()

    result = sweep(conn, Filter(during=rewrite), chunk_rows=1)
    assert result["totals"]["not_written"] == 1
    assert disclosure(conn, "conversation_messages", "m-never") == (None, None)
    sweep(conn, Filter())
    assert disclosure(conn, "conversation_messages", "m-never") == ("Rewritten meanwhile", sha("Rewritten meanwhile"))


def test_a_long_row_is_a_call_of_its_own_and_a_rows_fields_travel_together(conn):
    sweeper = Sweep(lambda: conn, client=None, chunk_rows=2, chunk_chars=100, stage_active=lambda: False)
    pending = [(9, "a", "content", "x" * 30, "h"), (9, "a", "content_rendered", "x" * 30, "h"),
               (8, "b", "content", "y" * 500, "h"), (7, "c", "content", "z" * 10, "h"),
               (6, "d", "content", "w" * 10, "h"), (5, "e", "content", "v" * 10, "h")]
    assert [[item[1] for item in chunk] for chunk in sweeper._chunks(pending)] == [
        ["a", "a"], ["b"], ["c", "d"], ["e"]]


# --- the version key ------------------------------------------------------------------------------------------------

def test_raising_the_layer_version_redoes_every_row_once(conn, monkeypatch):
    from topos.sanitization import privacy_filter

    sweep(conn, Filter())
    monkeypatch.setattr(privacy_filter, "PRIVACY_LAYER_VERSION", "2")
    client = Filter()
    sweep(conn, client, chunk_rows=1)
    every = [f"{table}:{rid}:{field}" for table, rid, values in ROWS for field in FIELDS[table]
             if isinstance(values.get(field), str) and values[field].strip()]
    assert sorted(client.ids) == sorted(every)
    text = f"Call {NAME} about the trip"
    assert disclosure(conn, "conversation_messages", "m-never") == (scrub(text), disclosure_hash(text, version="2"))
    again = Filter()
    sweep(conn, again)
    assert again.calls == []


@pytest.mark.asyncio
async def test_the_pipelines_own_stage_reads_a_disclosure_under_an_older_version_as_out_of_date(conn, monkeypatch):
    from topos.disclosure.privacy_layer import run_privacy_disclosure_layer
    from topos.sanitization import privacy_filter

    text = "A quiet day at home."
    batch = [{"entry_id": "j-current", "content": text, "content_disclosure": text,
              "content_disclosure_hash": sha(text), "_table": "journal_entries"}]
    client = Filter()
    assert (await run_privacy_disclosure_layer(conn, [dict(r) for r in batch], client=client))["records_updated"] == 0
    monkeypatch.setattr(privacy_filter, "PRIVACY_LAYER_VERSION", "2")
    assert (await run_privacy_disclosure_layer(conn, [dict(r) for r in batch], client=client))["records_updated"] == 1
    assert disclosure(conn, "journal_entries", "j-current")[1] == disclosure_hash(text, version="2")


# --- switches and neighbours ------------------------------------------------------------------------------------------

def test_with_the_privacy_layer_switched_off_nothing_is_called_or_written(conn, monkeypatch):
    import topos.config.settings as config

    monkeypatch.setattr(config.settings, "platform_privacy_via_engine", False)
    before = dump(conn)
    client = Filter()
    result = sweep(conn, client)
    assert result["disabled"] and client.calls == [] and dump(conn) == before


def test_the_sweep_waits_while_the_pipelines_privacy_stage_is_calling_the_model(conn):
    answers = iter([True, True, True])
    looked = []

    def active():
        looked.append(1)
        return next(answers, False)

    client = Filter()
    sweep(conn, client, stage_active=active, chunk_rows=1)
    assert len(looked) >= 4 and sorted(client.ids) == sorted(NEEDED)


@pytest.mark.asyncio
async def test_the_pipelines_stage_counts_as_active_only_while_it_calls_the_model(conn):
    from topos.disclosure.privacy_layer import privacy_stage_active, run_privacy_disclosure_layer

    seen = []

    class Watching(Filter):
        async def redact_batch(self, items):
            seen.append(privacy_stage_active())
            return await super().redact_batch(items)

    class Raising(Filter):
        async def redact_batch(self, items):
            raise RuntimeError("engine gone")

    batch = [{"message_id": "m-never", "content": f"Call {NAME} about the trip", "_table": "conversation_messages"}]
    await run_privacy_disclosure_layer(conn, batch, client=Watching())
    assert seen == [True] and not privacy_stage_active()
    with pytest.raises(RuntimeError):
        await run_privacy_disclosure_layer(conn, [dict(batch[0])], client=Raising())
    assert not privacy_stage_active()


def test_a_table_without_the_columns_is_not_walked(tmp_path):
    connection = sqlite3.connect(tmp_path / "old.db", check_same_thread=False)
    connection.execute("CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, content TEXT)")
    connection.execute("INSERT INTO conversation_messages VALUES ('m-1', ?)", (f"Ask {NAME}",))
    connection.commit()
    assert targets(connection) == []
    client = Filter()
    assert sweep(connection, client)["finished"] and client.calls == []
    connection.close()


def test_a_stop_ends_a_walk_before_its_next_read(conn):
    client = Filter()
    result = sweep(conn, client, stop=lambda: True)
    assert result["stopped"] and not result["finished"] and result["totals"]["rows"] == 0 and client.calls == []


# --- the paths that never ran the pipeline's stage ------------------------------------------------------------------

def _fresh_node(tmp_path):
    """A node's database as a fresh install leaves it: the startup migrations ran, and the message tables do not
    exist yet (the first sync creates them; the canonical store's own migration check gives them the disclosure
    columns as it is constructed)."""
    from topos.storage.db.migrations import apply_all_migrations

    connection = sqlite3.connect(tmp_path / "fresh.db", check_same_thread=False)
    apply_all_migrations(connection)
    present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"conversation_messages", "ai_chat_messages"} & present
    return connection


def test_a_fresh_nodes_messenger_sync_rows_end_with_a_disclosure(tmp_path):
    from topos.disclosure.tier import apply_disclosure_tier_to_rows
    from topos.storage.canonical.conversations_tables import ConversationsTablesManager

    connection = _fresh_node(tmp_path)
    records = [{"message_id": f"imessage:{n}", "thread_id": "thread-1", "sender_type": "human", "sender_id": "s-1",
                "from_self": n == 0, "ts": "2026-09-15T10:00:00+00:00", "content": text}
               for n, text in enumerate((f"Meet {NAME} at noon", f"Mail {ADDRESS}", ""))]
    # The node's own messenger sync writes through this manager and never runs the pipeline's privacy stage.
    ConversationsTablesManager(connection).upsert_message_batch(records, "d-1", "imessage")
    assert connection.execute("SELECT COUNT(*) FROM conversation_messages WHERE content_disclosure_hash IS NULL "
                              "AND trim(content) != ''").fetchone()[0] == 2
    client = Filter()
    result = sweep(connection, client, mode="pending")
    assert sorted(client.ids) == ["conversation_messages:imessage:0:content", "conversation_messages:imessage:1:content"]
    rows = [dict(zip(("message_id", "content", "content_disclosure"), row)) for row in connection.execute(
        "SELECT message_id, content, content_disclosure FROM conversation_messages ORDER BY message_id")]
    served = apply_disclosure_tier_to_rows(rows, table="conversation_messages", tier="default_disclosure")
    assert [row["content"] for row in served][:2] == ["Meet [NAME] at noon", "Mail [EMAIL]"]
    assert result["finished"]
    connection.close()


def test_a_healed_message_body_gets_a_disclosure_of_its_new_text(tmp_path):
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
    from topos.storage.canonical.conversations_tables import ensure_all_tables

    connection = sqlite3.connect(tmp_path / "heal.db", check_same_thread=False)
    ensure_all_tables(connection)
    record = {"message_id": "imessage:7", "conversation_id": "chat-1", "dataset_id": "d-1", "source_id": "imessage",
              "sender_type": "human", "sender_id": "s-1", "event_at": "2026-09-15T10:00:00+00:00",
              "content": "garbled"}
    store = SQLiteCanonicalStore(connection)
    store.upsert("conversation_messages", record)
    connection.commit()
    sweep(connection, Filter())
    store.upsert("conversation_messages", {**record, "content": f"Dinner with {NAME}"})   # a re-sync heals the body
    connection.commit()
    assert disclosure(connection, "conversation_messages", "imessage:7") == (None, None)
    sweep(connection, Filter(), mode="pending")
    assert disclosure(connection, "conversation_messages", "imessage:7")[0] == "Dinner with [NAME]"
    connection.close()


# --- the node's own loop ----------------------------------------------------------------------------------------------

def test_the_node_walks_everything_after_startup_then_answers_a_writers_request_with_a_pending_walk(conn, monkeypatch):
    import topos.runtime_shutdown as runtime

    modes = []
    real = disclosure_sweep.run_sweep

    async def recording(connect, *, mode, **kwargs):
        modes.append(mode)
        return await real(connect, mode=mode, **kwargs)

    waits = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        if seconds != 5:          # the pause between calls, not the wait between walks
            return await real_sleep(0)
        waits.append(seconds)
        if len(modes) == 1:
            # A messenger sync batch commits a new row and asks.
            conn.execute("INSERT INTO conversation_messages (message_id, content) VALUES ('m-late', ?)",
                         (f"Lunch with {NAME}",))
            conn.commit()
            request_run()
        elif len(modes) >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(runtime, "is_shutdown_requested", lambda: False)
    monkeypatch.setattr(disclosure_sweep, "run_sweep", recording)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = Filter()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_at_startup(lambda: conn, delay=0, recheck=600, poll=5, client=client))
    assert modes == ["verify", "pending"]
    assert client.ids[-1] == "conversation_messages:m-late:content"
    assert disclosure(conn, "conversation_messages", "m-late")[0] == "Lunch with [NAME]"


def test_a_shutdown_leaves_the_rows_for_the_next_start_and_a_failure_never_raises(conn, monkeypatch, caplog):
    import topos.runtime_shutdown as runtime

    before = dump(conn)
    monkeypatch.setattr(runtime, "is_shutdown_requested", lambda: True)
    asyncio.run(run_at_startup(lambda: conn, delay=0, client=Filter()))
    assert dump(conn) == before
    flags = iter([False, True, True])
    monkeypatch.setattr(runtime, "is_shutdown_requested", lambda: next(flags, True))
    with caplog.at_level("WARNING", logger="topos.disclosure.disclosure_sweep"):
        asyncio.run(run_at_startup(lambda: None, delay=0, client=Filter()))
    assert "PII disclosure sweep failed (RuntimeError)" in caplog.text


def test_counts_only_leave_the_module(conn, caplog):
    with caplog.at_level("DEBUG", logger="topos.disclosure.disclosure_sweep"):
        result = sweep(conn, Filter())
    shown = caplog.text + repr(result)
    for _table, rid, values in ROWS:
        assert rid not in shown
        for text in values.values():
            assert not (isinstance(text, str) and text.strip() and text in shown)
