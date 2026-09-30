"""The capture app and the dataset a door wrote through, on every table that records its writer class.

protects: a journal row recorded only ``writer_class``. ``owner_app`` says an owner door wrote the
row; it cannot say WHICH app, so the owner's own capture app was indistinguishable from any other
stamped owner write, and the row carried no dataset, so a reader could not resolve the posture of
the source's dataset-scoped install (the RD5 gap, on the journal table). A permissions reader that
wants to treat a journal row as the owner's needs both, recorded by the door and never taken from
the payload (OD-50/OD-52). These tests pin:

  - the always-run migration step adds ``writer_app_id`` and ``writer_dataset_id`` beside
    ``writer_class`` on the six non-chat writer tables, and does not move the schema version;
  - a stamped owner capture write records its app and its dataset on the journal row and on the
    journal's location child;
  - a payload cannot name either: the door's values overwrite whatever a record carries;
  - a relay write that is not an owner capture names no app;
  - an internal replay (no door) keeps class, app and dataset; a later door replaces all three;
  - a refused non-owner rewrite leaves them untouched.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict

import pytest

from topos.canonicalization.mappers.base import CanonicalRecord
from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.principal import OWNER_APP
from topos.sources.registry import REGISTRY
from topos.storage.canonical.canonical_store import (REFUSED_OWNER_ROW_DUPLICATE, REFUSED_OWNER_ROW_REWRITE,
                                                     SQLiteCanonicalStore)
from topos.storage.db.migrations import read_user_version
from topos.storage.db.migrations.entity_mentions_authored_v1 import apply_entity_mentions_authored_v1_up

from test_ai_chat_writer_class import DATASET, GRANTEE, OWNER, _relay, _stamp, captured_jobs, conn  # noqa: F401
from test_canonical_writer_class import _TIME_LOG, _rows, _time_log, _write, time_log_source  # noqa: F401

CAPTURE_APP = "owner-journal-app"
IDENTITY = ("writer_class", "writer_app_id", "writer_dataset_id")


def _entry(**overrides: Any) -> Dict[str, Any]:
    entry = {"entry_id": "e1", "source_id": "time_log", "content": "Walked the long way home.",
             "entry_at": "2026-09-01T10:00:00"}
    entry.update(overrides)
    return entry


def _identity(row: Dict[str, Any]) -> tuple:
    return tuple(row[name] for name in IDENTITY)


def test_the_always_run_step_adds_the_columns_without_moving_the_version(tmp_path):
    db = sqlite3.connect(str(tmp_path / "legacy.db"))
    db.execute("CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, content TEXT)")
    db.execute("CREATE TABLE documents (doc_id TEXT PRIMARY KEY, source_id TEXT, content TEXT, writer_class TEXT)")
    db.execute("INSERT INTO journal_entries VALUES ('old', 'time_log', 'An older entry.')")
    before = read_user_version(db)

    for _ in range(2):  # always_run: a second pass changes nothing
        apply_entity_mentions_authored_v1_up(db)

    for table in ("journal_entries", "documents"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        assert set(IDENTITY) <= columns, table
    assert read_user_version(db) == before
    # No backfill: the door that wrote an existing row is recorded nowhere.
    assert db.execute("SELECT writer_class, writer_app_id, writer_dataset_id FROM journal_entries").fetchall() == [
        (None, None, None)]


@pytest.mark.asyncio
async def test_the_owners_capture_app_and_its_dataset_are_recorded_on_a_journal_row(
        conn, captured_jobs, time_log_source):
    message = _stamp(_write("req-capture", _TIME_LOG, _time_log(), requester=OWNER),
                     cls=OWNER_APP, client_id=CAPTURE_APP, acting_user=OWNER)
    assert (await _relay(message))["status"] == "ok"

    (entry,) = _rows(conn, "journal_entries")
    assert _identity(entry) == ("owner_app", CAPTURE_APP, DATASET)
    (place,) = _rows(conn, "location_events")  # the journal's location fan-out is the same write
    assert _identity(place) == ("owner_app", CAPTURE_APP, DATASET)


@pytest.mark.asyncio
async def test_a_payload_cannot_name_its_own_app_or_dataset(conn, captured_jobs, time_log_source):
    forged = _time_log(writer_app_id=CAPTURE_APP, writer_dataset_id="someone-elses:dataset",
                       writer_class="owner_app")
    assert (await _relay(_write("req-forged", _TIME_LOG, forged)))["status"] == "ok"

    (entry,) = _rows(conn, "journal_entries")
    # An unstamped relay write: the class is the relay's, no app is named, and the dataset is the
    # one the door wrote into, whatever the record said.
    assert _identity(entry) == ("cp_relay", None, DATASET)


@pytest.mark.asyncio
async def test_a_stamped_owner_write_without_a_capture_app_names_none(conn, captured_jobs, time_log_source):
    message = _stamp(_write("req-owner", _TIME_LOG, _time_log(), requester=OWNER), cls=OWNER_APP, acting_user=OWNER)
    assert (await _relay(message))["status"] == "ok"
    (entry,) = _rows(conn, "journal_entries")
    assert _identity(entry) == ("owner_app", None, DATASET)


def test_an_internal_replay_keeps_class_app_and_dataset(conn):
    store = SQLiteCanonicalStore(conn)
    store.upsert("journal_entries", _entry(writer_class="owner_app", writer_app_id=CAPTURE_APP,
                                           writer_dataset_id=DATASET))
    # Reprocess and upgrade replays carry no door.
    ref = store.upsert("journal_entries", _entry(content="Walked the long way home, twice.", writer_class=None,
                                                 writer_app_id="ignored", writer_dataset_id="ignored"))
    assert ref.refused is None and ref.writer_class == "owner_app"
    (entry,) = _rows(conn, "journal_entries")
    assert _identity(entry) == ("owner_app", CAPTURE_APP, DATASET)
    assert entry["content"].endswith("twice.")


def test_a_later_door_replaces_the_app_and_dataset_with_its_own(conn):
    store = SQLiteCanonicalStore(conn)
    store.upsert("journal_entries", _entry(writer_class="owner_app", writer_app_id=CAPTURE_APP,
                                           writer_dataset_id=DATASET))
    # The owner re-imports the same entry from a file: that door names no app.
    store.upsert("journal_entries", _entry(writer_class="owner_import", writer_dataset_id="owner:topos:import"))
    (entry,) = _rows(conn, "journal_entries")
    assert _identity(entry) == ("owner_import", None, "owner:topos:import")


def test_a_refused_rewrite_leaves_the_owners_identity_alone(conn):
    store = SQLiteCanonicalStore(conn)
    store.upsert("journal_entries", _entry(writer_class="owner_app", writer_app_id=CAPTURE_APP,
                                           writer_dataset_id=DATASET))
    ref = store.upsert("journal_entries", _entry(content="Rewritten by someone else.", writer_class="cp_relay",
                                                 writer_app_id="grantee-app", writer_dataset_id=f"{GRANTEE}:x:y"))
    assert ref.refused == REFUSED_OWNER_ROW_REWRITE
    (entry,) = _rows(conn, "journal_entries")
    assert _identity(entry) == ("owner_app", CAPTURE_APP, DATASET)
    assert entry["content"] == "Walked the long way home."


def test_a_class_without_an_app_clears_a_stale_one(conn):
    """The app follows the class: a door that records a class records its app, or none."""
    store = SQLiteCanonicalStore(conn)
    store.upsert("documents", {"doc_id": "d1", "source_id": "notion_pages", "title": "Notes", "content": "A page.",
                               "writer_class": "cp_relay", "writer_app_id": "sync-app", "writer_dataset_id": DATASET})
    store.upsert("documents", {"doc_id": "d1", "source_id": "notion_pages", "title": "Notes", "content": "A page.",
                               "writer_class": "owner_app", "writer_dataset_id": DATASET})
    (doc,) = _rows(conn, "documents")
    assert _identity(doc) == ("owner_app", None, DATASET)


class _CarryingMapper:
    """A mapper that hands the payload through, as a code mapper or an older declaration can."""

    def map_many(self, normalized):
        return [CanonicalRecord(record_id=normalized.record_id, payload=dict(normalized.payload))]


def _through_the_pipeline(conn, monkeypatch, payload, **door):
    monkeypatch.setattr("topos.canonicalization.declared_field_map.build_canonical_mapper",
                        lambda source_def, **_kwargs: _CarryingMapper())
    result = canonicalize_normalized_batch(conn, REGISTRY[_TIME_LOG], [payload], dataset_id=DATASET,
                                           sync_batch_id="batch-1", **door)
    assert result.errors == []
    return result


def test_a_mapped_payload_cannot_carry_its_own_app_or_dataset(conn, time_log_source, monkeypatch):
    forged = _entry(writer_app_id=CAPTURE_APP, writer_dataset_id="someone-elses:dataset")
    result = _through_the_pipeline(conn, monkeypatch, forged, writer_class="cp_relay", writer_app_id=None)
    (entry,) = _rows(conn, "journal_entries")
    assert _identity(entry) == ("cp_relay", None, DATASET)
    # Nor does the record handed to derivation: a reader of the in-flight record sees the door, not the claim.
    (handed,) = [r for r in result.canonical_records if r.get("_table") == "journal_entries"]
    assert handed.get("writer_app_id") is None and handed.get("writer_dataset_id") is None
    assert handed["writer_class"] == "cp_relay"


def test_a_write_with_no_door_records_no_dataset(conn, time_log_source, monkeypatch):
    """An internal path (reprocess, upgrade replay) names no class, so it names no app and no dataset:
    a row only says which dataset a DOOR wrote it into."""
    _through_the_pipeline(conn, monkeypatch, _entry(writer_app_id=CAPTURE_APP), writer_class=None,
                          writer_app_id=CAPTURE_APP)
    (entry,) = _rows(conn, "journal_entries")
    assert _identity(entry) == (None, None, None)


def test_a_resend_that_differs_only_in_who_sent_it_is_a_duplicate_not_a_rewrite(conn):
    store = SQLiteCanonicalStore(conn)
    store.upsert("journal_entries", _entry(writer_class="owner_app", writer_app_id=CAPTURE_APP,
                                           writer_dataset_id=DATASET))
    ref = store.upsert("journal_entries", _entry(writer_class="cp_relay", writer_app_id="grantee-app",
                                                 writer_dataset_id=f"{GRANTEE}:x:y"))
    # Same words under the same id: nothing to change, and nothing logged as an attempted rewrite.
    assert ref.refused == REFUSED_OWNER_ROW_DUPLICATE
