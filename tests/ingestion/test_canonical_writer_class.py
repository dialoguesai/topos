"""Writer class on every canonical table the role gate can read as the owner's.

protects: after chat rows recorded their writer (test_ai_chat_writer_class.py),
the same door still wrote journal, profile and documents rows with no writer
at all. Journal and profile rows are authored by construction; a document from
a personal-posture source resolves to authored too. Reproduced on c4306ff
through the relay dispatch: a grantee's unstamped app_ingest to a runtime
journal source (and, per a peer probe, to notion_pages/gdrive_files) minted an
owner fact from the grantee's sentence. These tests pin:

  - every in-flight record carries the door's class, so derivation caps it;
  - the rows record it, reloads carry it, and an internal replay derives under
    the stored class rather than as legacy;
  - a non-owner write cannot replace an owner-held row (owner-written, or a
    legacy row its own table rules make the owner's); legacy documents and
    calendar rows stay writable so external syncs keep working;
  - an owner door over a non-owner row replaces it outright;
  - the lives_in aggregate, which never passes the role gate, ignores rows a
    non-owner wrote;
  - brief input labels a non-owner journal row;
  - the pipeline worker does not inherit the request that started it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Dict

import pytest

from topos.features.provenance.roles import ROLE_AUTHORED, ROLE_OBSERVED, record_role
from topos.principal import OWNER_APP
from topos.storage.canonical.canonical_store import (
    REFUSED_OWNER_ROW_DUPLICATE,
    REFUSED_OWNER_ROW_REWRITE,
    WRITER_CLASS_TABLES,
    SQLiteCanonicalStore,
)
from topos.storage.db.migrations import apply_all_migrations, ensure_migrations_applied

# tests/ingestion is not a package: pytest (prepend import mode) puts this directory
# on sys.path, so sibling test modules import by their own name.
from test_ai_chat_writer_class import (  # noqa: F401  (fixtures)
    DATASET,
    GRANTEE,
    INJECTED,
    OWNER,
    _owner_facts,
    _relay,
    _run_facts,
    _stamp,
    captured_jobs,
    conn,
)

_TIME_LOG = "time_log"


@pytest.fixture()
def time_log_source():
    from topos.sources.runtime_install import install_source_definition

    from test_journal_time_log_ui_stream_ingest import TIME_LOG_SOURCE_DEF

    handle = install_source_definition(TIME_LOG_SOURCE_DEF)
    try:
        yield handle
    finally:
        handle.uninstall()


def _write(msg_id: str, source_id: str, record: Dict[str, Any], *, requester: str = GRANTEE) -> Dict[str, Any]:
    return {
        "id": msg_id,
        "type": "app_ingest",
        "payload": {
            "user_id": OWNER,
            "dataset_id": DATASET,
            "source_id": source_id,
            "records": [record],
            "resource_id": f"dataset:{OWNER}:{DATASET}",
            "app_id": "some-app",
            "requesting_user_id": requester,
        },
    }


def _time_log(**overrides: Any) -> Dict[str, Any]:
    entry = {
        "startDate": "2026-06-23", "startTime": "09:00 AM", "endDate": "2026-06-23",
        "endTime": "10:00 AM", "duration": 60, "project": "Topos", "goal": "Settle in",
        "accomplished": INJECTED, "completed": True, "location": "Lisbon", "group": "Solo",
    }
    entry.update(overrides)
    return entry


def _notion_page(**overrides: Any) -> Dict[str, Any]:
    # No url: with one, shape inference files the row as an activity event.
    page = {"doc_id": "notion:p1", "title": "Notes", "content": INJECTED, "mime_type": "text/notion",
            "created_at": "2026-09-01T10:00:00Z", "modified_at": "2026-09-01T10:00:00Z"}
    page.update(overrides)
    return page


def _rows(conn: sqlite3.Connection, table: str) -> list:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]


def _handed_to_derivation(jobs) -> list:
    return [r for j in jobs for r in (j.get("payload") or {}).get("canonical_records") or []]


# ---------------------------------------------------------------------------
# Doors: journal (authored by construction) and documents (personal posture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_grantees_journal_entry_is_not_the_owners_and_mints_no_owner_fact(
    conn, captured_jobs, time_log_source, monkeypatch
):
    result = await _relay(_write("req-journal", _TIME_LOG, _time_log()))
    assert result["status"] == "ok", result

    (entry,) = _rows(conn, "journal_entries")
    assert entry["writer_class"] == "cp_relay"
    assert record_role(entry, table="journal_entries") == ROLE_OBSERVED
    (place,) = _rows(conn, "location_events")  # the journal's location fan-out
    assert place["writer_class"] == "cp_relay"
    assert {r.get("writer_class") for r in _handed_to_derivation(captured_jobs)} == {"cp_relay"}

    await _run_facts(conn, captured_jobs, monkeypatch)
    assert _owner_facts(conn, "prefers") == []


@pytest.mark.asyncio
async def test_the_owners_journal_entry_stays_the_owners(conn, captured_jobs, time_log_source, monkeypatch):
    message = _stamp(_write("req-journal-own", _TIME_LOG, _time_log(), requester=OWNER),
                     cls=OWNER_APP, acting_user=OWNER)
    assert (await _relay(message))["status"] == "ok"
    (entry,) = _rows(conn, "journal_entries")
    assert entry["writer_class"] == "owner_app"
    assert record_role(entry, table="journal_entries") == ROLE_AUTHORED

    await _run_facts(conn, captured_jobs, monkeypatch)
    assert [f["object_value"] for f in _owner_facts(conn, "prefers")] == ["Lisbon"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["notion_pages", "gdrive_files"])
async def test_a_grantees_document_mints_no_owner_fact(conn, captured_jobs, monkeypatch, source_id):
    page = _notion_page(doc_id=f"{source_id}:p1")
    assert (await _relay(_write(f"req-{source_id}", source_id, page)))["status"] == "ok"
    (doc,) = _rows(conn, "documents")
    assert doc["writer_class"] == "cp_relay"
    await _run_facts(conn, captured_jobs, monkeypatch)
    assert _owner_facts(conn, "prefers") == []


@pytest.mark.asyncio
async def test_a_grantee_cannot_rewrite_a_document_the_owner_wrote(conn, captured_jobs):
    """Through the door: doc_id is stable (the time-log parser mints a new
    entry id per write, so a journal re-send lands beside the owner's row)."""
    own = _stamp(_write("req-own-doc", "notion_pages", _notion_page(content="The plan."), requester=OWNER),
                 cls=OWNER_APP, acting_user=OWNER)
    assert (await _relay(own))["status"] == "ok"
    (before,) = _rows(conn, "documents")
    assert before["writer_class"] == "owner_app"
    captured_jobs.clear()

    result = await _relay(_write("req-rewrite-doc", "notion_pages", _notion_page()))
    assert result["status"] == "error", result
    assert "owner_row_rewrite_refused" in str(result["payload"]["errors"])
    (after,) = _rows(conn, "documents")
    assert after == before
    assert _handed_to_derivation(captured_jobs) == []


@pytest.mark.asyncio
async def test_every_record_handed_to_derivation_carries_the_door(conn, captured_jobs):
    """Groups whose tables record no writer (activity, transcripts, contacts)
    still hand derivation records that say which door wrote them."""
    event = {"event_type": "highlight", "url": "https://example.com/a", "title": "A page",
             "content": INJECTED, "visited_at": "2026-09-01T10:00:00Z"}
    assert (await _relay(_write("req-activity", "browser_events", event)))["status"] == "ok"
    records = _handed_to_derivation(captured_jobs)
    assert records and {r.get("writer_class") for r in records} == {"cp_relay"}


@pytest.mark.asyncio
async def test_a_reprocess_cannot_replay_a_refused_document_write(conn, captured_jobs, monkeypatch):
    """A document's raw row is keyed on the job id, not doc_id, so the raw
    restore has to act on the refusal itself (test_conversation_writer_class.py
    covers the keyed case)."""
    own = _stamp(_write("req-own-doc-raw", "notion_pages", _notion_page(content="The plan."), requester=OWNER),
                 cls=OWNER_APP, acting_user=OWNER)
    assert (await _relay(own))["status"] == "ok"
    assert (await _relay(_write("req-grantee-doc-raw", "notion_pages", _notion_page())))["status"] == "error"

    from topos.ingestion.reprocess import reprocess_source
    from topos.storage.canonical.ai_chat import CanonicalTablesManager

    # reprocess counts a documents source's rows from ai_chat_messages (documents
    # has no disclosure-registry group); every node has that table, this one does not yet.
    CanonicalTablesManager(conn)
    monkeypatch.setattr("topos.ingestion.reprocess.get_db_connection", lambda: conn)
    await reprocess_source(source_id="notion_pages", dataset_id=DATASET, from_stage="raw", run_enrichment=False)
    (doc,) = _rows(conn, "documents")
    assert (doc["content"], doc["writer_class"]) == ("The plan.", "owner_app")


# ---------------------------------------------------------------------------
# The store's rule, table by table
# ---------------------------------------------------------------------------


def _store(conn: sqlite3.Connection) -> SQLiteCanonicalStore:
    return SQLiteCanonicalStore(conn)


def _journal(entry_id: str, content: str, writer_class) -> Dict[str, Any]:
    return {"entry_id": entry_id, "entry_at": "2026-09-01T10:00:00Z", "content": content,
            "source_id": _TIME_LOG, "writer_class": writer_class}


def test_every_writer_class_table_has_the_column_on_a_fresh_database(tmp_path):
    for migrate in (apply_all_migrations, lambda c: ensure_migrations_applied(c, skip_backup=True)):
        db = sqlite3.connect(str(tmp_path / f"fresh-{id(migrate)}.db"))
        try:
            migrate(db)
            from topos.storage.canonical.ai_chat import CanonicalTablesManager

            CanonicalTablesManager(db)  # ai_chat tables are created on first use, not by the registry
            for table in WRITER_CLASS_TABLES:
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
                assert "writer_class" in columns, table
        finally:
            db.close()


def test_owner_written_and_legacy_journal_rows_refuse_a_non_owner(conn):
    store = _store(conn)
    store.upsert("journal_entries", _journal("j-own", "mine", "owner_import"))
    store.upsert("journal_entries", _journal("j-legacy", "mine too", None))

    for entry_id, content in (("j-own", "mine"), ("j-legacy", "mine too")):
        refused = store.upsert("journal_entries", _journal(entry_id, INJECTED, "cp_relay"))
        assert refused.refused == REFUSED_OWNER_ROW_REWRITE
        same = store.upsert("journal_entries", _journal(entry_id, content, "third_party"))
        assert same.refused == REFUSED_OWNER_ROW_DUPLICATE
        row = conn.execute("SELECT content, writer_class FROM journal_entries WHERE entry_id=?", (entry_id,)).fetchone()
        assert row[0] == content


def test_a_legacy_document_stays_writable_so_syncs_keep_working(conn):
    store = _store(conn)
    store.upsert("documents", {"doc_id": "d-legacy", "title": "Plan", "content": "v1", "source_id": "notion_pages"})
    ref = store.upsert("documents", {"doc_id": "d-legacy", "title": "Plan", "content": "v2",
                                     "source_id": "notion_pages", "writer_class": "cp_relay"})
    assert ref.refused is None and ref.writer_class == "cp_relay"
    row = conn.execute("SELECT content, writer_class FROM documents WHERE doc_id='d-legacy'").fetchone()
    assert tuple(row) == ("v2", "cp_relay")


def test_an_owner_door_replaces_a_row_a_non_owner_seeded(conn):
    """profile_records' conflict update changes only description: an update
    would keep a grantee's title and organisation under the owner's class."""
    store = _store(conn)
    store.upsert("profile_records", {"record_id": "p1", "record_type": "job", "title": "Seeded title",
                                     "organization": "Seeded org", "description": "x",
                                     "source_id": "resume", "writer_class": "cp_relay"})
    store.upsert("profile_records", {"record_id": "p1", "record_type": "job", "title": "Engineer",
                                     "organization": "Acme", "description": "y",
                                     "source_id": "resume", "writer_class": "owner_import"})
    row = dict(conn.execute("SELECT title, organization, description, writer_class FROM profile_records").fetchone())
    assert row == {"title": "Engineer", "organization": "Acme", "description": "y", "writer_class": "owner_import"}


def test_an_internal_write_keeps_the_stored_class_and_reports_it(conn):
    store = _store(conn)
    store.upsert("journal_entries", _journal("j-grantee", INJECTED, "cp_relay"))
    ref = store.upsert("journal_entries", _journal("j-grantee", INJECTED + " (edited)", None))
    assert ref.refused is None and ref.writer_class == "cp_relay"
    assert conn.execute("SELECT writer_class FROM journal_entries").fetchone()[0] == "cp_relay"


def test_an_internal_replay_derives_under_the_stored_class(conn):
    from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
    from topos.sources.registry import REGISTRY

    page = _notion_page(doc_id="notion:replay")
    first = canonicalize_normalized_batch(conn, REGISTRY["notion_pages"], [page], dataset_id=DATASET,
                                          sync_batch_id="b1", writer_class="cp_relay")
    assert [r["writer_class"] for r in first.canonical_records] == ["cp_relay"]
    replay = canonicalize_normalized_batch(conn, REGISTRY["notion_pages"], [page], dataset_id=DATASET,
                                           sync_batch_id="b2", writer_class=None)
    assert [r["writer_class"] for r in replay.canonical_records] == ["cp_relay"]


@pytest.mark.asyncio
async def test_a_journal_reload_carries_the_writer_class(conn, captured_jobs, time_log_source):
    from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
    from topos.sources.registry import REGISTRY

    assert (await _relay(_write("req-reload-journal", _TIME_LOG, _time_log())))["status"] == "ok"
    (record,) = load_canonical_records_for_signal(conn, REGISTRY[_TIME_LOG])
    assert record["writer_class"] == "cp_relay"
    assert record_role({**record}, table="journal_entries") == ROLE_OBSERVED


# ---------------------------------------------------------------------------
# Readers that never pass the role gate, or label instead of gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("writer_class", "expected"), [("cp_relay", 0), ("third_party", 0), ("owner_import", 1), (None, 1)])
def test_lives_in_counts_only_rows_the_owner_wrote(conn, writer_class, expected):
    from topos.features.facts.extract import derive_location_facts

    store = _store(conn)
    for day in ("01", "02", "03"):
        store.upsert("location_events", {"event_id": f"loc-{day}", "place_name": "Somewhere", "city": "Lisbon",
                                         "event_at": f"2026-09-{day}T10:00:00Z", "source_id": "places",
                                         "writer_class": writer_class})
    assert derive_location_facts(conn) == expected


def test_brief_input_labels_a_journal_row_the_owner_did_not_write(conn):
    from topos.enrichment.jobs.canonical.brief_fallback import brief_input_text
    from topos.features.signal.brief_canonical_loader import load_canonical_messages_for_dimension

    store = _store(conn)
    store.upsert("journal_entries", _journal("j-brief", INJECTED, "cp_relay"))
    records = [r for r in load_canonical_messages_for_dimension(conn, "memory") if r["message_id"] == "j-brief"]
    assert records, "the memory dimension no longer loads journal_entries"
    assert records[0]["writer_class"] == "cp_relay"
    assert brief_input_text(records[0]).startswith("[not written by the owner] ")
    assert not brief_input_text({**records[0], "writer_class": "owner_app"}).startswith("[")
    assert not brief_input_text({**records[0], "writer_class": None}).startswith("[")


@pytest.mark.asyncio
async def test_the_pipeline_worker_does_not_inherit_the_request_that_started_it(monkeypatch):
    from topos.pipeline import job_runner
    from topos.principal import Principal, current_principal, reset_principal, set_principal
    from topos.storage.db import write_gate
    from topos.uds import _transport, current_transport

    seen: list = []

    async def _loop(*_args, **_kwargs):
        seen.append((current_principal(), current_transport(), write_gate._defer_commit.get()))

    monkeypatch.setattr(job_runner, "_worker_loop", _loop)
    monkeypatch.setattr(job_runner, "_worker_task", None)
    monkeypatch.setattr(job_runner, "_long_worker_task", None)
    monkeypatch.setenv("TOPOS_PIPELINE_WORKER", "on")

    principal_token = set_principal(Principal(OWNER_APP, "uds"))
    transport_token = _transport.set("uds")
    defer_token = write_gate._defer_commit.set(True)
    try:
        job_runner.start_pipeline_worker(lambda: None)
        tasks = [job_runner._worker_task, job_runner._long_worker_task]
    finally:
        write_gate._defer_commit.reset(defer_token)
        _transport.reset(transport_token)
        reset_principal(principal_token)
    await asyncio.gather(*tasks)
    assert seen == [(None, "tcp", False), (None, "tcp", False)]
