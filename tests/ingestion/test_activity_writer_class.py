"""Writer class on activity rows: a visit another app wrote is told from the owner's (OD-52 P1).

protects: activity_events recorded no writer at all. Any approved write permission,
whatever its scope, could write browser visits through app_ingest (the control plane
bound no source to a scope), and the rows it wrote were indistinguishable from the
owner's own capture: no class, no app, no dataset. The door now records all three,
from the channel principal and the door's dataset, never from the payload, and the
store keeps an owner door's row the way ``_upsert_recording_writer`` keeps the message
tables' (test_ai_chat_writer_class.py, test_canonical_writer_class.py):

  - an unstamped relay write records ``cp_relay``, the dataset and no app;
  - the owner's attested capture app (a stamped ``owner_app`` write) records its app;
  - a record cannot choose its writer;
  - a non-owner cannot rewrite a visit an owner door wrote; its replay changes and
    derives nothing, and the flat browser_visits row is left alone too;
  - an owner door takes over a visit another door wrote first;
  - between two non-owner doors the later one is recorded, with its app;
  - a legacy (NULL) row stays writable, as legacy documents and calendar rows do:
    activity is ambient by table, and refusing unstamped writes would freeze the plugin;
  - an internal write keeps the stored writer, and a reload carries it.

All of it sits behind the switch ``TOPOS_ACTIVITY_WRITER_CLASS``, on by default since
October 2026 (unset, as every test here leaves it unless it says otherwise): off (``0``,
``false``, ``no`` or ``off``), an activity write records no writer and is never refused,
as before.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

import pytest

from topos.principal import OWNER_APP, THIRD_PARTY
from topos.storage.canonical.activity_tables import ActivityEventsManager
from topos.storage.canonical.canonical_store import (
    ACTIVITY_WRITER_FLAG,
    REFUSED_OWNER_ROW_DUPLICATE,
    REFUSED_OWNER_ROW_REWRITE,
    WRITER_CLASS_TABLES,
    SQLiteCanonicalStore,
    activity_writer_recording_enabled,
)

# tests/ingestion is not a package: pytest (prepend import mode) puts this directory
# on sys.path, so sibling test modules import by their own name.
from test_ai_chat_writer_class import (  # noqa: F401  (fixtures)
    DATASET,
    GRANTEE,
    OWNER,
    _relay,
    _stamp,
    captured_jobs,
    conn,
)

SOURCE = "browser_visits"
PLUGIN = "browser-history-plugin"
URL = "https://example.test/articles/one"


@pytest.fixture(autouse=True)
def _the_default(monkeypatch):
    """The rule under test is the default: the switch unset. The tests of the off switch set it off."""
    monkeypatch.delenv(ACTIVITY_WRITER_FLAG, raising=False)


def _switch_off(monkeypatch, value: str = "false") -> None:
    monkeypatch.setenv(ACTIVITY_WRITER_FLAG, value)


def _visit(title: str = "A page", **extra: Any) -> Dict[str, Any]:
    return {"url": URL, "title": title, "visited_at": "2026-09-01T10:00:00Z", **extra}


def _write(msg_id: str, record: Dict[str, Any], *, requester: str = GRANTEE, app_id: str = "some-app") -> Dict[str, Any]:
    return {
        "id": msg_id,
        "type": "app_ingest",
        "payload": {
            "user_id": OWNER,
            "dataset_id": DATASET,
            "source_id": SOURCE,
            "records": [record],
            "resource_id": f"dataset:{OWNER}:{DATASET}",
            "app_id": app_id,
            "requesting_user_id": requester,
        },
    }


async def _owner_capture(msg_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """The owner's plugin once the control plane attests it: an owner_app stamp naming the app."""
    message = _write(msg_id, record, requester=OWNER, app_id=PLUGIN)
    return await _relay(_stamp(message, cls=OWNER_APP, client_id=PLUGIN, acting_user=OWNER))


def _rows(conn: sqlite3.Connection, table: str = "activity_events") -> list:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]


def _writer(row: Dict[str, Any]) -> tuple:
    return row["writer_class"], row["writer_app_id"], row["writer_dataset_id"]


def _handed_to_derivation(jobs) -> list:
    return [r for j in jobs for r in (j.get("payload") or {}).get("canonical_records") or []]


def test_activity_events_records_its_writer():
    assert WRITER_CLASS_TABLES["activity_events"] == "event_id"


# ---------------------------------------------------------------------------
# Through the door
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unstamped_visit_records_cp_relay_the_dataset_and_no_app(conn, captured_jobs):
    result = await _relay(_write("req-visit", _visit()))
    assert result["status"] == "ok", result
    (row,) = _rows(conn)
    assert _writer(row) == ("cp_relay", None, DATASET)
    records = _handed_to_derivation(captured_jobs)
    assert records and {r.get("writer_class") for r in records} == {"cp_relay"}


@pytest.mark.asyncio
async def test_the_owners_attested_capture_app_is_recorded_with_its_app(conn, captured_jobs):
    assert (await _owner_capture("req-own", _visit()))["status"] == "ok"
    (row,) = _rows(conn)
    assert _writer(row) == ("owner_app", PLUGIN, DATASET)
    assert {r.get("writer_class") for r in _handed_to_derivation(captured_jobs)} == {"owner_app"}


@pytest.mark.asyncio
async def test_a_stamped_third_party_is_recorded_as_one(conn, captured_jobs):
    message = _stamp(_write("req-3p", _visit()), cls=THIRD_PARTY, client_id="grantee-app", acting_user=GRANTEE)
    assert (await _relay(message))["status"] == "ok"
    (row,) = _rows(conn)
    assert _writer(row) == ("third_party", None, DATASET)


@pytest.mark.asyncio
async def test_the_payload_cannot_choose_its_writer(conn, captured_jobs):
    message = _write("req-spoof", _visit(writer_class="owner_app", writer_app_id=PLUGIN,
                                         writer_dataset_id="someone:topos:else"))
    message["payload"]["writer_class"] = "owner_app"
    message["writer_class"] = "owner_app"
    assert (await _relay(message))["status"] == "ok"
    (row,) = _rows(conn)
    assert _writer(row) == ("cp_relay", None, DATASET)


@pytest.mark.asyncio
async def test_a_grantee_cannot_rewrite_a_visit_an_owner_door_wrote(conn, captured_jobs):
    assert (await _owner_capture("req-own", _visit(title="The owner's page")))["status"] == "ok"
    before, flat_before = _rows(conn), _rows(conn, "browser_visits")
    assert len(flat_before) == 1
    captured_jobs.clear()

    result = await _relay(_write("req-rewrite", _visit(title="A forged title")))
    assert result["status"] == "error", result
    assert "owner_row_rewrite_refused" in json.dumps(result)
    assert _rows(conn) == before
    assert _rows(conn, "browser_visits") == flat_before
    assert _handed_to_derivation(captured_jobs) == []


@pytest.mark.asyncio
async def test_a_grantee_replaying_an_owner_visit_changes_and_derives_nothing(conn, captured_jobs):
    assert (await _owner_capture("req-own", _visit()))["status"] == "ok"
    before, flat_before = _rows(conn), _rows(conn, "browser_visits")
    captured_jobs.clear()

    result = await _relay(_write("req-dup", _visit()))
    assert result["status"] == "ok", result
    assert _rows(conn) == before and _rows(conn, "browser_visits") == flat_before
    assert _handed_to_derivation(captured_jobs) == []


@pytest.mark.asyncio
async def test_an_owner_door_takes_over_a_visit_another_door_wrote(conn, captured_jobs):
    assert (await _relay(_write("req-seed", _visit(title="Seeded title"))))["status"] == "ok"
    assert (await _owner_capture("req-own", _visit(title="The owner's page")))["status"] == "ok"
    (row,) = _rows(conn)
    assert (row["title"], *_writer(row)) == ("The owner's page", "owner_app", PLUGIN, DATASET)


@pytest.mark.asyncio
async def test_between_non_owner_doors_the_later_one_is_recorded(conn, captured_jobs):
    """As on the message tables: the class names the door whose write the row now holds."""
    assert (await _relay(_write("req-g1", _visit(title="partial"))))["status"] == "ok"
    later = _stamp(_write("req-g2", _visit(title="whole")), cls=THIRD_PARTY, client_id="grantee-app",
                   acting_user=GRANTEE)
    assert (await _relay(later))["status"] == "ok"
    (row,) = _rows(conn)
    assert (row["title"], *_writer(row)) == ("whole", "third_party", None, DATASET)


def test_an_internal_replay_derives_under_the_stored_class(conn):
    from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
    from topos.sources.registry import REGISTRY

    record = {**_visit(), "record_id": "replayed"}
    first = canonicalize_normalized_batch(conn, REGISTRY[SOURCE], [record], dataset_id=DATASET,
                                          sync_batch_id="b1", writer_class="cp_relay")
    assert [r["writer_class"] for r in first.canonical_records] == ["cp_relay"]
    replay = canonicalize_normalized_batch(conn, REGISTRY[SOURCE], [record], dataset_id=DATASET,
                                           sync_batch_id="b2", writer_class=None)
    assert [r["writer_class"] for r in replay.canonical_records] == ["cp_relay"]


@pytest.mark.asyncio
async def test_a_reload_carries_the_writer_class(conn, captured_jobs):
    from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
    from topos.sources.registry import REGISTRY

    assert (await _relay(_write("req-reload", _visit())))["status"] == "ok"
    (record,) = load_canonical_records_for_signal(conn, REGISTRY[SOURCE])
    assert record["writer_class"] == "cp_relay"


def test_a_refused_batch_write_is_undone_in_raw_retention_too(conn):
    """A file import retains raw rows keyed by the source record, while the refusal names
    the canonical id the mapper prefixed. Unmatched, the refused payload stayed in raw and a
    reprocess from raw (no writer, so not gated) replayed it over the owner's row."""
    from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
    from topos.ingestion.manager import _persist_raw_retention, _restore_refused_raw
    from topos.ingestion.parsers.base import NormalizedRecord
    from topos.sources.registry import REGISTRY

    source_def = REGISTRY[SOURCE]
    record_id = f"{URL}_2026-09-01T10:00:00Z"

    def _batch(title: str, writer_class: str, snapshots=None):
        record = NormalizedRecord(record_id=record_id, payload={**_visit(title=title), "record_id": record_id})
        _persist_raw_retention(conn, source_def, [record], sync_batch_id=f"b-{title}", records_in=1,
                               raw_snapshots=snapshots)
        return canonicalize_normalized_batch(conn, source_def, [record], dataset_id=DATASET,
                                             sync_batch_id=f"b-{title}", writer_class=writer_class)

    def _raw_titles() -> list:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%'").fetchall()
        titles = []
        for (table,) in rows:
            columns = {c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if {"source_system", "payload_json"} <= columns:
                titles += [json.loads(r[0]).get("title") for r in conn.execute(
                    f"SELECT payload_json FROM {table} WHERE source_system=?", (SOURCE,)).fetchall()]
        return titles

    assert not _batch("The owner's page", "owner_import").refused
    snapshots: list = []
    refused = _batch("A forged title", "cp_relay", snapshots)
    assert refused.refused.get(f"browser:{record_id}") == REFUSED_OWNER_ROW_REWRITE
    _restore_refused_raw(conn, snapshots, refused.refused)
    assert _raw_titles() == ["The owner's page"]
    row = conn.execute("SELECT title, writer_class FROM activity_events").fetchone()
    assert tuple(row) == ("The owner's page", "owner_import")


# ---------------------------------------------------------------------------
# The store's rule
# ---------------------------------------------------------------------------


def _event(title: str, writer_class=None, app=None, dataset=None, event_id: str = "browser:e1") -> Dict[str, Any]:
    return {"event_id": event_id, "activity_type": "visit", "url": URL, "title": title,
            "occurred_at": "2026-09-01T10:00:00Z", "source_id": SOURCE, "writer_class": writer_class,
            "writer_app_id": app, "writer_dataset_id": dataset}


def _stored(conn: sqlite3.Connection, event_id: str = "browser:e1") -> tuple:
    row = conn.execute(
        "SELECT title, writer_class, writer_app_id, writer_dataset_id FROM activity_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    return tuple(row)


def test_an_owner_written_visit_refuses_a_non_owner(conn):
    store = SQLiteCanonicalStore(conn)
    store.upsert("activity_events", _event("mine", "owner_app", PLUGIN, DATASET))
    refused = store.upsert("activity_events", _event("forged", "cp_relay", None, DATASET))
    assert refused.refused == REFUSED_OWNER_ROW_REWRITE and refused.writer_class == "owner_app"
    same = store.upsert("activity_events", _event("mine", "third_party", None, DATASET))
    assert same.refused == REFUSED_OWNER_ROW_DUPLICATE
    assert _stored(conn) == ("mine", "owner_app", PLUGIN, DATASET)


def test_a_legacy_visit_stays_writable_so_the_plugin_keeps_syncing(conn):
    store = SQLiteCanonicalStore(conn)
    store.upsert("activity_events", _event("v1"))
    assert _stored(conn) == ("v1", None, None, None)
    ref = store.upsert("activity_events", _event("v2", "cp_relay", None, DATASET))
    assert ref.refused is None and ref.writer_class == "cp_relay"
    assert _stored(conn) == ("v2", "cp_relay", None, DATASET)


def test_an_internal_write_keeps_the_stored_writer_app_and_dataset(conn):
    store = SQLiteCanonicalStore(conn)
    store.upsert("activity_events", _event("first", "owner_app", PLUGIN, DATASET))
    ref = store.upsert("activity_events", _event("second", None, "not-a-door", "not:a:dataset"))
    assert ref.refused is None and ref.writer_class == "owner_app"
    assert _stored(conn) == ("second", "owner_app", PLUGIN, DATASET)


def test_a_new_row_never_exists_without_its_writer(conn):
    """The class is written by the INSERT itself, not only by the store's follow-up UPDATE."""
    store = SQLiteCanonicalStore(conn)
    store._dispatch_table_upsert("activity_events", _event("direct", "cp_relay", None, DATASET), sync_batch_id="b1")
    assert _stored(conn) == ("direct", "cp_relay", None, DATASET)


def test_the_manager_takes_the_writer_from_the_door_not_the_record(conn):
    record = _event("from an app", "owner_app", PLUGIN, "someone:topos:else")
    out = ActivityEventsManager(conn).upsert_batch([record], source_id=SOURCE, sync_batch_id="b1",
                                                   writer_class="cp_relay", writer_dataset_id=DATASET)
    assert out["events_created"] == 1 and [ref.writer_class for ref in out["refs"]] == ["cp_relay"]
    assert _stored(conn) == ("from an app", "cp_relay", None, DATASET)


# ---------------------------------------------------------------------------
# The switch: on unless turned off. Off is the behaviour before migration 80
# ---------------------------------------------------------------------------


def test_the_switch_is_on_unless_the_owner_turns_it_off():
    assert activity_writer_recording_enabled()  # the autouse fixture left it unset
    assert activity_writer_recording_enabled({})
    for on in ("", "  ", "true", "1", "yes", "on", "TRUE", "enabled", "flase"):
        assert activity_writer_recording_enabled({ACTIVITY_WRITER_FLAG: on}), on
    for off in ("0", "false", "no", "off", " OFF ", "False", "NO"):
        assert not activity_writer_recording_enabled({ACTIVITY_WRITER_FLAG: off}), off


@pytest.mark.asyncio
async def test_off_a_visit_records_no_writer_and_nothing_is_refused(conn, captured_jobs, monkeypatch):
    _switch_off(monkeypatch)
    assert (await _owner_capture("req-own", _visit(title="The owner's page")))["status"] == "ok"
    result = await _relay(_write("req-other", _visit(title="Another title")))
    assert result["status"] == "ok", result
    (row,) = _rows(conn)
    assert (row["title"], *_writer(row)) == ("Another title", None, None, None)
    # Derivation still hears each door, as it did before the columns existed.
    assert [r.get("writer_class") for r in _handed_to_derivation(captured_jobs)] == ["owner_app", "cp_relay"]


@pytest.mark.asyncio
async def test_switching_off_never_leaves_a_writer_on_values_it_did_not_write(conn, captured_jobs, monkeypatch):
    assert (await _owner_capture("req-own", _visit(title="The owner's page")))["status"] == "ok"
    (row,) = _rows(conn)
    assert _writer(row) == ("owner_app", PLUGIN, DATASET)
    _switch_off(monkeypatch, "0")

    # An internal replay (no door) keeps what was recorded...
    SQLiteCanonicalStore(conn).upsert("activity_events", _event("The owner's page", event_id=row["event_id"]))
    assert _stored(conn, row["event_id"]) == ("The owner's page", "owner_app", PLUGIN, DATASET)
    # ...but a door's unrecorded write clears it: the class would name a door whose values it replaced.
    assert (await _relay(_write("req-other", _visit(title="Another title"))))["status"] == "ok"
    assert _stored(conn, row["event_id"]) == ("Another title", None, None, None)


def test_off_the_manager_stores_no_writer(conn, monkeypatch):
    _switch_off(monkeypatch, "off")
    out = ActivityEventsManager(conn).upsert_batch([_event("a visit")], source_id=SOURCE, sync_batch_id="b1",
                                                   writer_class="cp_relay", writer_dataset_id=DATASET)
    assert [ref.writer_class for ref in out["refs"]] == [None]
    assert _stored(conn) == ("a visit", None, None, None)

