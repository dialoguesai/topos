"""A node that has only ever taken in one kind of record can still release it (BL-108).

protects: the node's schema step makes neither message table. `conversation_messages` is made by the first
messages sync and `ai_chat_messages` by the first AI-chat write, so a person who installs a node and imports one
AI-chat export has a database with no `conversation_messages` table at all. The independent-copy rule counts a
line's exact text in BOTH message tables, and on that database its count ended every read with SQLite's "no such
table", which the resolver (correctly) reports as `evidence_storage_unavailable`: the node's own pass read
scanned 2, assessed 0, held back 2, the share's index could never be built, the share page's counts were
refused, and a recipient was told "has no answer to that" for ever. Every fixture in this suite makes both tables
by hand, which is why no test saw it.

The fix (`evidence._copy_count`) answers 0 for exactly one fact: the database's own catalog, asked inside the same
read transaction, holds nothing by that name. These tests pin both halves:

- a store that never made the sibling table assesses and releases its line: an imported AI-chat line with no
  messages table (the store is built the way the app's import builds it, through the owner's import door), a
  message with no AI-chat table, and a journal entry with neither;
- a table that IS there and cannot be read still holds everything back with the same code: a renamed column, a
  damaged page, a lock, a view, a name spelled in another case, an unreadable catalog, a caller with no read
  transaction, and a file replaced under the resolver;
- a database that has both tables runs the one statement it always ran, and a copy in the sibling is still found
  the moment the sibling exists.

Every person, word and id here is invented.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sqlite3
import traceback
from types import SimpleNamespace

import pytest

from tests.ingestion.test_ai_chat_writer_class import (  # noqa: F401 (a fixture and the import door's helpers)
    _PUB_B64, _keep_the_post_canonical_pipeline_offline, _row, _stamp, _start_ingestion, captured_jobs)
from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node
from tests.permissions_v2.test_ai_chat_dataset_binding import _install
from tests.permissions_v2.test_automatic_message_review import answer
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_knowledge_search import knowledge_policy
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.permissions_v2 import evidence as evidence_module
from topos.permissions_v2 import switches
from topos.permissions_v2.automatic_message_review import prepare
from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceResolver, EvidenceReviewStore
from topos.permissions_v2.message_review_contract import AutomaticReviewRequest
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.principal import OWNER_APP
from topos.storage.db.migrations import ensure_migrations_applied

OWNER = mc.OWNER_ID
DATASET = f"{OWNER}:topos:default"
EXPORT = "chatgpt_file_ingestion"
LINE = "I am sketching a talk about tide pools for the spring open day at work."
REPLY = "Here is a five-part outline you could use for the talk."
NOW = 1_788_256_800 + 3_600    # an hour after the line's own time, 2026-09-01T10:00:00Z
MESSAGES = "conversation_messages"
AI_CHAT = "ai_chat_messages"
COUNT = evidence_module._COPY_COUNT


def _copy_count(conn, table, content):
    """The fix itself, looked up at call time so that this file still collects on a tree without it."""
    return evidence_module._copy_count(conn, table, content)


# --- the stores ------------------------------------------------------------------------------------------

def _tables(conn) -> set:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _schema_step_only(path, monkeypatch):
    """A database as a node's start leaves it: the node's own schema step, the owner's id, the protection clock."""
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    ensure_migrations_applied(db)      # what `core.state._open_owner_db_connection` runs at every start
    db.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('user_id', ?)", (OWNER,))
    db.commit()
    # The kinds a node bound for sharing has on with an empty environment (switches.py): the copy rule then reads
    # the journal table as well, as it does on a real node that has turned sharing on.
    for switch in switches.SWITCHES:
        if switch.kind == "bool" and switch.bound is True and switch is not switches.ENABLED:
            monkeypatch.setenv(switch.name, "true")
    return db


def _export_file() -> bytes:
    lines = [{"id": "m-line", "thread_id": "thread-file", "role": "user", "content": LINE,
              "created_at": "2026-09-01T10:00:00Z"},
             {"id": "m-reply", "thread_id": "thread-file", "role": "assistant", "content": REPLY,
              "created_at": "2026-09-01T10:00:05Z"}]
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()


@pytest.fixture()
def ai_chat_only(tmp_path, monkeypatch, captured_jobs):  # noqa: F811
    """A fresh node whose only data is one imported AI-chat export, taken in through the owner's import door."""
    db = _schema_step_only(tmp_path / "database.db", monkeypatch)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: db, raising=False)
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", _PUB_B64)
    _keep_the_post_canonical_pipeline_offline(monkeypatch)
    message = {"id": "req-import", "type": "start_ingestion",
               "payload": {"dataset_id": DATASET, "job_id": "job-import", "source_id": EXPORT,
                           "schema_id": "chatgpt.conversation.v2", "file_format": "jsonl",
                           "file_base64": base64.b64encode(_export_file()).decode()}}
    _stamp(message, cls=OWNER_APP, acting_user=OWNER)       # the control plane's stamp for the owner's own app
    asyncio.run(_start_ingestion(message, captured_jobs, db, tmp_path, monkeypatch))
    _install(db, source=EXPORT, dataset=DATASET, user=OWNER)
    db.commit()
    ensure_protection_clock(tmp_path / "database.db", owner_id=OWNER)
    monkeypatch.setattr(mc, "NOW", NOW)
    yield SimpleNamespace(db=db, path=tmp_path / "database.db", root=tmp_path)
    db.close()


def _bound(store):
    resolver = EvidenceResolver(store.path, binding=mc.BINDING)
    with owner(actor=OWNER):
        reviews = EvidenceReviewStore(store.root / "reviews.db", resolver=resolver)
    return resolver, reviews


def _line(resolver, message_id="m-line"):
    return resolver._identity(AI_CHAT, message_id, EXPORT)


def _refusal(resolver, reviews, identity) -> str | None:
    """None when the node's own check of one line passes, else its code and the database error under it."""
    try:
        with owner(actor=OWNER):
            prepare(resolver, reviews, identity)
    except PolicyError as exc:
        cause = exc.__context__
        frames = [frame for frame in traceback.extract_tb(cause.__traceback__)] if cause is not None else []
        where = f" at {frames[-1].name}" if frames else ""
        return exc.code + (f" <- {type(cause).__name__}: {cause}{where}" if cause is not None else "")
    return None


def _own_pass(resolver, reviews) -> dict:
    """The node's own assessment pass over the last day, with a stand-in for the local model."""
    async def classify(prepared):
        return answer(prepared)
    worker = AutomaticReviewWorker(resolver, reviews, classifier=classify)
    with owner(actor=OWNER):
        asyncio.run(worker._process(AutomaticReviewRequest(after=NOW - 86_400, before=NOW)))
        status = worker.status()
    return {name: getattr(status, name) for name in ("scanned", "assessed", "withheld")}


def _share_policy() -> dict:
    """A knowledge share over the export source, as the control plane compiles one for the kinds a person picks."""
    raw = knowledge_policy()
    tables = [AI_CHAT, MESSAGES, "journal_entries"]
    for rule in raw["rules"]:
        rule["evidence_use"]["sources"] = {"kind": "only", "values": [EXPORT]}
        for form in rule["release"]["forms"]:
            form["tables"] = tables
    raw["source_universe"]["source_ids"] = [EXPORT]
    raw["search"]["tables"] = tables
    return raw


def _share(store, resolver, reviews) -> Node:
    return Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), store.root / "share",
                model=None, search_raw=_share_policy(), now=NOW)


def _released(node) -> list:
    output, refused = node.search_request("tide pools talk", k=10)
    assert refused is None, refused
    return [(record["kind"], record["content"]) for record in output["records"]]


def _make_the_messages_table(store) -> None:
    """What a node's first messages sync does before it writes a row."""
    from topos.storage.canonical.conversations_tables import ConversationsTablesManager
    ConversationsTablesManager(store.db).ensure_tables()
    store.db.commit()


def _a_message_with_the_same_words(store) -> None:
    store.db.execute("INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_type, "
                     "content, event_at, source_id, is_from_self) VALUES ('copy-1','thread-9',?,'human',?,"
                     "'2026-08-30T09:00:00Z','imessage',0)", (DATASET, LINE))
    store.db.commit()


def _damage_the_table(path, table: str) -> None:
    """Overwrite the table's root page in the file, as a bad sector would."""
    with sqlite3.connect(str(path)) as probe:
        page_size = probe.execute("PRAGMA page_size").fetchone()[0]
        (root,) = probe.execute("SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    probe.close()
    with open(path, "r+b") as raw:
        raw.seek((root - 1) * page_size)
        raw.write(b"\xa5" * page_size)
        raw.flush()
        os.fsync(raw.fileno())


# --- 1. a fresh node whose only data is an imported AI-chat export ---------------------------------------

def test_the_store_is_what_the_fault_needs(ai_chat_only):
    row = _row(ai_chat_only.db, "m-line")
    assert (row["writer_class"], row["writer_dataset_id"], row["source_id"]) == ("owner_import", DATASET, EXPORT)
    tables = _tables(ai_chat_only.db)
    assert AI_CHAT in tables and "journal_entries" in tables
    assert MESSAGES not in tables and "conversations" not in tables   # never made: no messages source ever synced


def test_the_nodes_own_check_of_an_imported_line_passes(ai_chat_only):
    resolver, reviews = _bound(ai_chat_only)
    assert _refusal(resolver, reviews, _line(resolver)) is None
    # The assistant's reply is still not the owner's words: only the missing table changed.
    assert _refusal(resolver, reviews, _line(resolver, "m-reply")) == "native_owner_provenance_unavailable"


def test_the_nodes_own_pass_assesses_the_line_and_a_share_releases_it(ai_chat_only):
    resolver, reviews = _bound(ai_chat_only)
    assert _own_pass(resolver, reviews) == {"scanned": 2, "assessed": 1, "withheld": 1}
    node = _share(ai_chat_only, resolver, reviews)
    with owner(actor=OWNER):
        assert node.index.rebuild("grant-search", now=NOW) == {"state": "ready", "member_count": 1}
    assert _released(node) == [("message", LINE)]
    # The node's deep sweep reads the same count for every member: it keeps the index, and the share keeps serving.
    with owner(actor=OWNER):
        assert node.index.sweep(now=NOW) == 0
    assert _released(node) == [("message", LINE)]
    assert MESSAGES not in _tables(ai_chat_only.db)        # nothing made the table on the way


def test_the_share_pages_counts_are_answered(ai_chat_only):
    from topos.permissions_v2.registry import parse_policy
    from topos.permissions_v2.share_counts import count, forget
    resolver, reviews = _bound(ai_chat_only)
    _own_pass(resolver, reviews)
    forget()
    with owner(actor=OWNER):
        counted = count(resolver, reviews, parse_policy(_share_policy()), now=NOW)
    forget()
    assert counted["kinds"]["ai_chats"]["can_share"] == 1
    assert sum(counted["kinds"]["ai_chats"]["held_back"].values()) == 0


def test_a_copy_in_the_messages_table_is_found_the_moment_the_table_exists(ai_chat_only):
    resolver, reviews = _bound(ai_chat_only)
    assert _refusal(resolver, reviews, _line(resolver)) is None
    _make_the_messages_table(ai_chat_only)
    assert _refusal(resolver, reviews, _line(resolver)) is None            # there, and empty
    _a_message_with_the_same_words(ai_chat_only)
    assert _refusal(resolver, reviews, _line(resolver)) == "independent_copy_lineage"


# --- 2. a messages table that is there and cannot be read still holds the line back ----------------------

def _held_back(store, resolver, reviews) -> None:
    """Refused with the storage code, never assessed, never a member, never released."""
    refusal = _refusal(resolver, reviews, _line(resolver))
    assert refusal is not None and refusal.split(" ")[0] == "evidence_storage_unavailable", refusal
    counts = _own_pass(resolver, reviews)
    assert counts["assessed"] == 0 and counts["withheld"] == counts["scanned"], counts
    node = _share(store, resolver, reviews)
    with owner(actor=OWNER):
        try:
            built = node.index.rebuild("grant-search", now=NOW)
        except PolicyError as exc:
            assert exc.code == "evidence_storage_unavailable"
        else:
            assert built["member_count"] == 0, built
    output, refused = node.search_request("tide pools talk", k=10)
    assert refused is not None or output["records"] == []


def test_a_messages_table_with_a_renamed_column_holds_the_line_back(ai_chat_only):
    _make_the_messages_table(ai_chat_only)
    ai_chat_only.db.execute("ALTER TABLE conversation_messages RENAME COLUMN content TO content_was")
    ai_chat_only.db.commit()
    resolver, reviews = _bound(ai_chat_only)
    refusal = _refusal(resolver, reviews, _line(resolver))
    assert refusal is not None and refusal.startswith(
        "evidence_storage_unavailable <- OperationalError: no such column: content"), refusal
    _held_back(ai_chat_only, resolver, reviews)


def test_a_messages_table_with_a_damaged_page_holds_the_line_back(ai_chat_only):
    _make_the_messages_table(ai_chat_only)
    _a_message_with_the_same_words(ai_chat_only)       # so a count that read nothing would also miss a copy
    resolver, reviews = _bound(ai_chat_only)
    _damage_the_table(ai_chat_only.path, MESSAGES)
    refusal = _refusal(resolver, reviews, _line(resolver))
    assert refusal is not None and refusal.startswith("evidence_storage_unavailable <- DatabaseError"), refusal
    _held_back(ai_chat_only, resolver, reviews)


def test_a_view_by_the_tables_name_that_cannot_be_read_holds_the_line_back(ai_chat_only):
    ai_chat_only.db.execute("CREATE TABLE elsewhere (content TEXT)")
    ai_chat_only.db.execute("CREATE VIEW conversation_messages AS SELECT content FROM elsewhere")
    ai_chat_only.db.execute("DROP TABLE elsewhere")
    ai_chat_only.db.commit()
    resolver, reviews = _bound(ai_chat_only)
    _held_back(ai_chat_only, resolver, reviews)


def test_a_file_replaced_under_the_resolver_is_refused_before_any_count(ai_chat_only):
    """An older copy with no messages table, put where the database was, is never read as "no copies"."""
    before_any_messages = ai_chat_only.root / "older-copy.db"
    shutil.copyfile(ai_chat_only.path, before_any_messages)
    _make_the_messages_table(ai_chat_only)
    _a_message_with_the_same_words(ai_chat_only)
    resolver, reviews = _bound(ai_chat_only)
    assert _refusal(resolver, reviews, _line(resolver)) == "independent_copy_lineage"
    ai_chat_only.db.close()
    os.replace(before_any_messages, ai_chat_only.path)
    assert _refusal(resolver, reviews, _line(resolver)) == "evidence_database_binding"


# --- 3. the count itself --------------------------------------------------------------------------------

@pytest.fixture()
def reader(tmp_path):
    """A read-only connection inside a read transaction, as `EvidenceResolver._read` holds one."""
    path = tmp_path / "count.db"
    with sqlite3.connect(str(path)) as setup:
        setup.execute("CREATE TABLE ai_chat_messages (message_id TEXT, content TEXT)")
        setup.execute("INSERT INTO ai_chat_messages VALUES ('a', 'the same words')")
    setup.close()

    def open_reader(*, transaction=True):
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        if transaction:
            conn.execute("BEGIN")
            conn.execute("SELECT count(*) FROM ai_chat_messages").fetchone()   # the snapshot is taken
        opened.append(conn)
        return conn
    opened = []
    yield SimpleNamespace(path=path, open=open_reader)
    for conn in opened:
        conn.close()


def _write(path, *statements) -> None:
    with sqlite3.connect(str(path)) as conn:
        for statement in statements:
            conn.execute(statement)
    conn.close()


def _statements(conn, call) -> list:
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        call()
    finally:
        conn.set_trace_callback(None)
    return seen


def test_a_table_the_database_never_made_holds_no_copy(reader):
    conn = reader.open()
    assert _copy_count(conn, AI_CHAT, "the same words") == 1
    assert _copy_count(conn, MESSAGES, "the same words") == 0


def test_a_table_the_database_has_runs_the_one_statement_it_always_ran(reader):
    _write(reader.path, "CREATE TABLE conversation_messages (message_id TEXT, content TEXT)",
           "INSERT INTO conversation_messages VALUES ('m', 'the same words')")
    conn = reader.open()
    for table in (MESSAGES, AI_CHAT):
        ran = _statements(conn, lambda: _copy_count(conn, table, "the same words"))
        assert ran == [COUNT.format(table=table).replace("?1", "'the same words'")]   # the catalog is never asked


@pytest.mark.parametrize("statements, error", [
    (("CREATE TABLE conversation_messages (message_id TEXT, body TEXT)",), "no such column: content"),
    (("CREATE TABLE Conversation_Messages (message_id TEXT, body TEXT)",), "no such column: content"),
    (("CREATE TABLE gone (content TEXT)", "CREATE VIEW conversation_messages AS SELECT content FROM gone",
      "DROP TABLE gone"), "no such table: main.gone"),
])
def test_a_table_that_is_there_and_cannot_be_read_raises_what_the_count_raised(reader, statements, error):
    _write(reader.path, *statements)
    conn = reader.open()
    with pytest.raises(sqlite3.OperationalError, match=error):
        _copy_count(conn, MESSAGES, "the same words")


def test_a_damaged_table_raises(reader):
    _write(reader.path, "CREATE TABLE conversation_messages (message_id TEXT, content TEXT)",
           "INSERT INTO conversation_messages VALUES ('m', 'the same words')")
    _damage_the_table(reader.path, MESSAGES)
    conn = reader.open()
    with pytest.raises(sqlite3.DatabaseError) as raised:
        _copy_count(conn, MESSAGES, "the same words")
    assert not isinstance(raised.value, sqlite3.OperationalError)     # "database disk image is malformed"


def test_without_a_read_transaction_nothing_is_taken_on_the_catalogs_word(reader):
    conn = reader.open(transaction=False)
    assert not conn.in_transaction
    with pytest.raises(sqlite3.OperationalError, match="no such table: conversation_messages"):
        _copy_count(conn, MESSAGES, "the same words")


class _Scripted:
    """A connection that answers the count and the catalog as told: a fault no file on disk makes on demand."""
    in_transaction = True

    def __init__(self, *, count, catalog):
        self.count, self.catalog, self.asked = count, catalog, []

    def execute(self, sql, _args=()):
        answer_ = self.catalog if "sqlite_master" in sql else self.count
        self.asked.append("catalog" if "sqlite_master" in sql else "count")
        if isinstance(answer_, Exception):
            raise answer_
        return SimpleNamespace(fetchone=lambda: answer_)


def test_a_locked_table_raises_what_the_count_raised():
    locked = sqlite3.OperationalError("database is locked")
    conn = _Scripted(count=locked, catalog=(1,))                # the catalog lists the table
    with pytest.raises(sqlite3.OperationalError) as raised:
        _copy_count(conn, MESSAGES, "the same words")
    assert raised.value is locked and conn.asked == ["count", "catalog"]


@pytest.mark.parametrize("unreadable", [sqlite3.OperationalError("database is locked"),
                                        sqlite3.DatabaseError("database disk image is malformed")])
def test_a_catalog_that_cannot_be_read_is_a_fault(unreadable):
    conn = _Scripted(count=sqlite3.OperationalError("no such table: conversation_messages"), catalog=unreadable)
    with pytest.raises(sqlite3.Error) as raised:
        _copy_count(conn, MESSAGES, "the same words")
    assert raised.value is unreadable


def test_only_the_missing_name_is_asked_about():
    conn = _Scripted(count=sqlite3.OperationalError("no such table: conversation_messages"), catalog=None)
    seen = []
    execute = conn.execute
    conn.execute = lambda sql, args=(): (seen.append((sql, args)), execute(sql, args))[1]
    assert _copy_count(conn, MESSAGES, "the same words") == 0
    assert seen[-1] == ("SELECT 1 FROM sqlite_master WHERE name=?1 COLLATE NOCASE LIMIT 1", (MESSAGES,))


# --- 4. the mirror cases: a message with no AI-chat table, a journal entry with neither -------------------

def test_a_message_releases_on_a_node_that_never_made_the_ai_chat_table(legacy, tmp_path, monkeypatch):  # noqa: F811
    from tests.permissions_v2.test_knowledge_search import node_for
    legacy[1].execute("DROP TABLE ai_chat_messages")     # the fixture's stand-in: a real messages-only node has none
    legacy[1].commit()
    assert AI_CHAT not in _tables(legacy[1])
    node, _identity = node_for(legacy, tmp_path, monkeypatch)
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0]) == {"state": "ready", "member_count": 1}
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is None
    assert [record["content"] for record in output["records"]] == ["I am working on Synthetic message at work."]


def test_a_message_is_held_back_when_the_ai_chat_table_is_there_and_cannot_be_read(legacy):  # noqa: F811
    from tests.permissions_v2.test_automatic_message_review import setup
    legacy[1].execute("DROP TABLE ai_chat_messages")
    legacy[1].execute("CREATE TABLE ai_chat_messages (message_id TEXT, body TEXT)")
    legacy[1].commit()
    with pytest.raises(PolicyError) as refused:
        setup(legacy)
    assert refused.value.code == "evidence_storage_unavailable"
    assert str(refused.value.__context__) == "no such column: content"


@pytest.fixture()
def journal_only(tmp_path, monkeypatch):
    """A fresh node whose only data is a journal: the schema step made its table, and neither message table."""
    from tests.permissions_v2 import test_journal_family as journal
    path = tmp_path / "canonical.db"
    db = _schema_step_only(path, monkeypatch)
    monkeypatch.setenv(journal.JOURNAL_FLAG, "true")
    _install(db, source=journal.SOURCE, dataset=journal.DATASET, user=journal.OWNER)
    db.commit()
    db.close()
    ensure_protection_clock(path, owner_id=journal.OWNER)
    return path


def test_a_journal_entry_releases_on_a_node_that_never_made_either_message_table(journal_only, tmp_path, monkeypatch):
    from tests.permissions_v2 import test_journal_family as journal
    with sqlite3.connect(str(journal_only)) as conn:
        assert not {MESSAGES, AI_CHAT} & _tables(conn)
    conn.close()
    journal._entry(journal_only, "e1")
    assert journal._check(journal_only, "e1") is None
    search, state = journal._journal_node(journal_only, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 1}
    output, refused = search.search_request("draft walked home", k=10)
    assert refused is None
    assert [(record["kind"], record["content"]) for record in output["records"]] == [("journal_entry", journal.WORDS)]
