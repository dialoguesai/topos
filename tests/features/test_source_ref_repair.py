"""The repair for derived-object provenance refs that miss the record table they name (upgrade step, 22 Oct 2026).

Read-only on one live node, refs to records that exist failed to resolve in their stated table: mirror rows stamped
``conversation_messages`` whatever their source, and July facts citing location rows and GitHub commits as journal
entries, the commits under an id no table holds. The repair moves such a ref to the one record table that holds its
id (a GitHub commit to its push event). A source label used as a table name ("iMessage") is read as its table and
never rewritten; everything else is left alone, counted. Synthetic node databases and invented rows only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.ingestion.ai_tool_fixtures import build_node
from topos.features.lifecycle.source_ref_repair import main, record_table, repair_source_refs, resolve_ref

CHAT = "7f3c0a10-0000-4000-8000-00000000c0de"          # an AI-chat message
TEXT = "imessage:101"                                  # a text message
ENTRY = "gj-17"                                        # a journal entry
PLACE = "gj-loc-3"                                     # a location row of the journal source
COMMIT = "octo-lab/lantern:9f8e7d6c5b4a"               # a commit, as owner/repo:sha
PAGE = "browser:https://example.org/notes_2026-07-01T10:00:00.000Z"

_RECORD_TABLES = {
    "conversation_messages": "CREATE TABLE IF NOT EXISTS conversation_messages (message_id TEXT PRIMARY KEY,"
                             " conversation_id TEXT NOT NULL, dataset_id TEXT NOT NULL, event_at TEXT NOT NULL,"
                             " source_id TEXT NOT NULL, content TEXT, sender_type TEXT)",
    "journal_entries": "CREATE TABLE IF NOT EXISTS journal_entries (entry_id TEXT PRIMARY KEY, entry_at TEXT,"
                       " content TEXT, source_id TEXT NOT NULL)",
    "activity_events": "CREATE TABLE IF NOT EXISTS activity_events (event_id TEXT PRIMARY KEY, activity_type TEXT,"
                       " occurred_at TEXT, content TEXT, source_id TEXT NOT NULL, source_record_id TEXT)",
    "location_events": "CREATE TABLE IF NOT EXISTS location_events (event_id TEXT PRIMARY KEY, place_name TEXT,"
                       " event_at TEXT, source_id TEXT NOT NULL)",
}


def _insert(conn, table, **values):
    """Insert the columns this node's table has (a migrated table carries more than the fixture names)."""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    values = {k: v for k, v in values.items() if k in columns}
    conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                 tuple(values.values()))


def _object(conn, object_id, refs, *, object_type="fact", valid_to=None, updated_at="2026-07-02T00:00:00Z"):
    refs_json = refs if isinstance(refs, str) else json.dumps(refs)
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence,"
        " source_refs_json, valid_from, valid_to, extractor_version, created_at, updated_at)"
        " VALUES (?, 'profile', ?, ?, ?, 0.8, ?, '2026-07-01T00:00:00Z', ?, 'v1', '2026-07-01T00:00:00Z', ?)",
        (object_id, object_type, f"key:{object_id}", json.dumps({"predicate": "likes", "object_value": "tea"}),
         refs_json, valid_to, updated_at),
    )


def _refs(conn, object_id):
    return json.loads(conn.execute("SELECT source_refs_json FROM signal_objects WHERE object_id=?",
                                   (object_id,)).fetchone()[0])


@pytest.fixture
def node(tmp_path: Path) -> sqlite3.Connection:
    conn = build_node(tmp_path / "node.db")
    for ddl in _RECORD_TABLES.values():
        conn.execute(ddl)
    _insert(conn, "ai_chat_messages", message_id=CHAT, conversation_id="chat-1", sender_type="human",
            event_at="2026-07-01T10:00:00Z", content="invented text", metadata_json="{}", sequence=0,
            source_id="chatgpt_file_ingestion", writer_class="owner_app")
    _insert(conn, "conversation_messages", message_id=TEXT, conversation_id="7", dataset_id="ds-1",
            event_at="2026-07-01T11:00:00Z", source_id="imessage", content="invented text", sender_type="user",
            writer_class="owner_app")
    _insert(conn, "journal_entries", entry_id=ENTRY, entry_at="2026-07-01T12:00:00Z", content="invented text",
            source_id="grow_journal", writer_class="owner_app")
    _insert(conn, "location_events", event_id=PLACE, place_name="Harbor", event_at="2026-07-01T12:00:00Z",
            source_id="grow_journal")
    _insert(conn, "activity_events", event_id=f"github:push:{COMMIT}", activity_type="push",
            occurred_at="2026-07-01T13:00:00Z", content="invented commit message", source_id="github_activity",
            source_record_id=f"push:{COMMIT}")
    _insert(conn, "activity_events", event_id=PAGE, activity_type="visit", occurred_at="2026-07-01T14:00:00Z",
            source_id="browser_visits", source_record_id=PAGE)
    # The shapes the measured node carried, each beside a ref that already resolves.
    _object(conn, "mirror", [{"table": "conversation_messages", "id": CHAT}], object_type="message_entities")
    _object(conn, "reading", [
        {"table": "iMessage", "record_id": TEXT, "source_id": "imessage", "kind": "owner_wrote"},
        {"table": "Grow Journal", "record_id": ENTRY, "source_id": "grow_journal", "kind": "owner_wrote"},
        {"table": "entities", "record_id": "ent_0001", "kind": "subject"},
    ], object_type="person_reading")
    _object(conn, "fact", [
        {"table": "journal_entries", "record_id": f"github:{COMMIT}", "source_id": "github_activity"},
        {"table": "journal_entries", "record_id": PLACE, "source_id": "grow_journal"},
        {"table": "journal_entries", "record_id": ENTRY, "source_id": "grow_journal"},
    ])
    conn.commit()
    yield conn
    conn.close()


def test_a_ref_moves_to_the_one_table_that_holds_its_id(node) -> None:
    report = repair_source_refs(node)

    assert report["status"] == "ok"
    assert _refs(node, "mirror") == [{"table": "ai_chat_messages", "id": CHAT}]
    assert report["relinked"] == {
        "conversation_messages->ai_chat_messages": 1,
        "journal_entries->activity_events": 1,
        "journal_entries->location_events": 1,
    }
    assert report["objects_rewritten"] == 2 and report["refs_relinked"] == 3


def test_a_source_label_is_read_as_its_table_and_never_rewritten(node) -> None:
    stored = node.execute("SELECT source_refs_json FROM signal_objects WHERE object_id='reading'").fetchone()[0]
    _object(node, "labels", [{"table": "Grow Journal", "record_id": "gj-404"},           # an alias, record gone
                             {"table": "Demo Inbox", "record_id": CHAT},                 # another label
                             {"table": "", "record_id": CHAT}])                          # no table at all
    node.commit()

    report = repair_source_refs(node)

    assert node.execute("SELECT source_refs_json FROM signal_objects WHERE object_id='reading'").fetchone()[0] == stored
    assert _refs(node, "labels") == [{"table": "Grow Journal", "record_id": "gj-404"},
                                     {"table": "Demo Inbox", "record_id": CHAT}, {"table": "", "record_id": CHAT}]
    assert report["left_alone"]["alias_resolves"] == 2 and report["left_alone"]["alias_unmatched"] == 1
    assert report["left_alone"]["label"] == 2


def test_resolve_ref_reads_an_alias_as_its_table_and_id_else_record_id(node) -> None:
    assert resolve_ref(node, {"table": "iMessage", "record_id": TEXT, "kind": "owner_wrote"}) \
        == ("conversation_messages", TEXT)
    assert resolve_ref(node, {"table": "Grow Journal", "id": ENTRY}) == ("journal_entries", ENTRY)
    assert resolve_ref(node, {"table": "ai_chat_messages", "id": CHAT}) == ("ai_chat_messages", CHAT)
    assert resolve_ref(node, {"table": "conversation_messages", "id": CHAT}) is None, "the table it names lacks it"
    assert resolve_ref(node, {"table": "Demo Inbox", "record_id": CHAT}) is None
    assert resolve_ref(node, {"table": "iMessage"}) is None


def test_a_github_commit_moves_to_its_push_event_and_takes_its_id(node) -> None:
    report = repair_source_refs(node)

    assert _refs(node, "fact") == [
        {"table": "activity_events", "record_id": f"github:push:{COMMIT}", "source_id": "github_activity"},
        {"table": "location_events", "record_id": PLACE, "source_id": "grow_journal"},
        {"table": "journal_entries", "record_id": ENTRY, "source_id": "grow_journal"},
    ]
    assert report["github_commits_relinked"] == 1


def test_a_second_run_changes_nothing(node) -> None:
    repair_source_refs(node)
    before = node.execute("SELECT object_id, source_refs_json FROM signal_objects ORDER BY object_id").fetchall()

    again = repair_source_refs(node)

    assert again["refs_relinked"] == 0 and again["objects_rewritten"] == 0
    assert again["left_alone"].get("unmatched", 0) == 0
    assert node.execute("SELECT object_id, source_refs_json FROM signal_objects ORDER BY object_id").fetchall() == before


def test_an_id_two_tables_hold_is_left_alone_and_counted(node) -> None:
    _insert(node, "journal_entries", entry_id="shared-1", entry_at="2026-07-03T00:00:00Z", content="invented",
            source_id="grow_journal", writer_class="owner_app")
    _insert(node, "location_events", event_id="shared-1", place_name="Pier", event_at="2026-07-03T00:00:00Z",
            source_id="grow_journal")
    _object(node, "ambiguous", [{"table": "conversation_messages", "id": "shared-1"}])
    node.commit()

    report = repair_source_refs(node)

    assert _refs(node, "ambiguous") == [{"table": "conversation_messages", "id": "shared-1"}]
    assert report["left_alone"]["ambiguous"] == 1


def test_a_commit_two_events_name_is_left_alone(node) -> None:
    other = "octo-lab/lantern:0a1b2c3d4e5f"
    for kind in ("push", "create"):
        _insert(node, "activity_events", event_id=f"github:{kind}:{other}", activity_type=kind,
                occurred_at="2026-07-04T00:00:00Z", source_id="github_activity", source_record_id=f"{kind}:{other}")
    _object(node, "two-events", [{"table": "journal_entries", "record_id": f"github:{other}"}])
    node.commit()

    report = repair_source_refs(node)

    assert _refs(node, "two-events") == [{"table": "journal_entries", "record_id": f"github:{other}"}]
    assert report["left_alone"]["ambiguous"] == 1


def test_a_ref_whose_record_is_gone_is_left_alone_and_counted(node) -> None:
    _object(node, "gone", [{"table": "conversation_messages", "id": "msg-404"},
                           {"table": "journal_entries", "record_id": "github:octo-lab/lantern:ffff0000"}])
    node.commit()

    report = repair_source_refs(node)

    assert _refs(node, "gone") == [{"table": "conversation_messages", "id": "msg-404"},
                                   {"table": "journal_entries", "record_id": "github:octo-lab/lantern:ffff0000"}]
    assert report["left_alone"]["unmatched"] == 2
    assert report["unmatched_by_stated_table"] == {"conversation_messages": 1, "journal_entries": 1}


def test_a_ref_naming_another_source_is_left_alone(node) -> None:
    _object(node, "other-source", [{"table": "conversation_messages", "record_id": CHAT, "source_id": "signal"}])
    node.commit()

    report = repair_source_refs(node)

    assert _refs(node, "other-source") == [{"table": "conversation_messages", "record_id": CHAT,
                                            "source_id": "signal"}]
    assert report["left_alone"]["source_mismatch"] == 1


def test_resolving_derived_and_idless_refs_are_left_alone(node) -> None:
    refs = [
        {"table": "ai_chat_messages", "record_id": CHAT},     # resolves where it says
        {"table": "topic_clusters", "id": "tc_0001"},         # a derived table: a second hop, not a repair
        {"table": "triage_verdicts", "day": "2026-07-01"},    # no record id
        {"table": "journal_entries", "record_id": ""},        # an empty one
    ]
    _object(node, "left", refs)
    node.commit()

    report = repair_source_refs(node)

    assert _refs(node, "left") == refs
    left = report["left_alone"]
    assert left["resolves"] >= 2 and left["not_a_record_table"] >= 2 and left["no_record_id"] >= 2


def test_unreadable_refs_are_counted_and_kept_byte_for_byte(node) -> None:
    _object(node, "broken", "[{not json")
    _object(node, "scalar", '"conversation_messages"')
    _object(node, "mixed", json.dumps(["loose text", {"table": "conversation_messages", "id": CHAT}]))
    node.commit()

    report = repair_source_refs(node)

    assert node.execute("SELECT source_refs_json FROM signal_objects WHERE object_id='broken'").fetchone()[0] \
        == "[{not json"
    assert node.execute("SELECT source_refs_json FROM signal_objects WHERE object_id='scalar'").fetchone()[0] \
        == '"conversation_messages"'
    assert _refs(node, "mixed") == ["loose text", {"table": "ai_chat_messages", "id": CHAT}]
    assert report["left_alone"]["unparseable"] == 2 and report["left_alone"]["not_an_object"] == 1


def test_no_row_is_deleted_closed_or_redated(node) -> None:
    _object(node, "closed", [{"table": "conversation_messages", "id": CHAT}], valid_to="2026-08-01T00:00:00Z")
    node.commit()
    columns = "object_id, valid_from, valid_to, created_at, updated_at, payload_json"
    before = node.execute(f"SELECT {columns} FROM signal_objects ORDER BY object_id").fetchall()

    repair_source_refs(node)

    assert node.execute(f"SELECT {columns} FROM signal_objects ORDER BY object_id").fetchall() == before
    # A closed object is lineage too (the permissions sibling floor reads closed rows): it is repaired the same way.
    assert _refs(node, "closed") == [{"table": "ai_chat_messages", "id": CHAT}]


def test_the_lineage_keys_follow_a_rewritten_id(node) -> None:
    """The permissions reads index refs by id (permissions_fact_lineage_keys_v1); its triggers re-key the row."""
    keys = lambda: {k for (k,) in node.execute(  # noqa: E731
        "SELECT ref_key FROM permissions_v2_fact_ref_keys WHERE object_id='fact'")}
    assert f"github:{COMMIT}" in keys()

    repair_source_refs(node)

    assert f"github:push:{COMMIT}" in keys() and f"github:{COMMIT}" not in keys()


def test_a_dry_run_counts_and_writes_nothing(node) -> None:
    before = node.execute("SELECT object_id, source_refs_json FROM signal_objects ORDER BY object_id").fetchall()

    report = repair_source_refs(node, dry_run=True)

    assert report["status"] == "dry_run" and report["refs_relinked"] == 3 and report["objects_rewritten"] == 2
    assert node.execute("SELECT object_id, source_refs_json FROM signal_objects ORDER BY object_id").fetchall() == before


def test_the_cli_dry_run_opens_the_database_read_only(node, tmp_path, capsys, monkeypatch) -> None:
    path = node.execute("PRAGMA database_list").fetchone()[2]
    node.close()
    opened = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append((str(target), kwargs.get("uri")))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    before = Path(path).read_bytes()

    assert main(["--db", path, "--dry-run"]) == 0

    assert json.loads(capsys.readouterr().out)["refs_relinked"] == 3
    assert opened == [(f"{Path(path).resolve().as_uri()}?mode=ro", True)]
    assert Path(path).read_bytes() == before


def test_a_failure_part_way_leaves_every_row_as_it_was(node, monkeypatch) -> None:
    import topos.storage.db.write_gate as write_gate

    before = node.execute("SELECT object_id, source_refs_json FROM signal_objects ORDER BY object_id").fetchall()

    def fail(conn):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(write_gate, "commit_connection", fail)
    with pytest.raises(sqlite3.OperationalError):
        repair_source_refs(node)

    assert node.execute("SELECT object_id, source_refs_json FROM signal_objects ORDER BY object_id").fetchall() == before


def test_a_node_without_signal_objects_is_skipped(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    try:
        assert repair_source_refs(conn)["status"] == "skipped"
    finally:
        conn.close()


def test_record_table_names_the_one_table_or_none(node) -> None:
    assert record_table(node, CHAT) == "ai_chat_messages"
    assert record_table(node, f" {TEXT} ") == "conversation_messages"
    assert record_table(node, "msg-404") is None
    assert record_table(node, "") is None


def test_the_upgrade_step_runs_the_repair(node) -> None:
    """The manifest declares the step (under "unreleased" until a cut stamps it into a release), and the runner's
    engine-endpoint dispatch reaches the repair."""
    import topos.upgrades as upgrades
    from topos.upgrades.runner import _exec_engine_endpoint

    manifest = json.loads((Path(upgrades.__file__).parent / "manifests.json").read_text())
    declaring = [s for r in manifest["releases"] for s in r.get("steps", []) if s.get("id") == "repair-derived-source-refs"]
    assert declaring, "no release entry declares repair-derived-source-refs"
    step = declaring[-1]
    assert step["kind"] == "engine_endpoint" and step["consent"] == "auto"

    detail = _exec_engine_endpoint(step, node)

    assert detail["refs_relinked"] == 3
    assert _refs(node, "mirror") == [{"table": "ai_chat_messages", "id": CHAT}]


# --- the writer that is still running cites the record's table ----------------------------------------------------


def test_the_scope_materializer_cites_the_table_that_holds_the_record(node) -> None:
    from topos.features.signal.signal_object_store import SignalObjectStore
    from topos.features.signal.typed_stores.scope_materializer import _materialize_table_rows

    node.execute("CREATE TABLE IF NOT EXISTS message_entities (entity_id TEXT PRIMARY KEY, record_id TEXT,"
                 " source_id TEXT, entity_text TEXT, payload_json TEXT NOT NULL DEFAULT '{}', message_id TEXT)")
    for entity_id, record_id, source_id in (("me-1", CHAT, "chatgpt_file_ingestion"), ("me-2", PAGE, "browser_visits"),
                                            ("me-3", TEXT, "imessage"), ("me-4", "msg-404", "imessage")):
        _insert(node, "message_entities", entity_id=entity_id, record_id=record_id, source_id=source_id,
                entity_text="harbor", payload_json="{}", message_id=record_id)
    node.commit()

    _materialize_table_rows(SignalObjectStore(node), node, table="message_entities", object_type="message_entities",
                            dimension="memory", id_col="entity_id", payload_cols=["entity_text", "record_id"],
                            ref_table="conversation_messages")

    cited = dict(node.execute(
        "SELECT object_key, json_extract(source_refs_json, '$[0].table') FROM signal_objects"
        " WHERE object_type='message_entities' AND signal_dimension='memory'").fetchall())
    # The declared table stays the answer when no table holds the record.
    assert cited == {"me-1": "ai_chat_messages", "me-2": "activity_events", "me-3": "conversation_messages",
                     "me-4": "conversation_messages"}

