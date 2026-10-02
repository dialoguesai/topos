"""A journal entry the owner's own app pushes from now on is provable as the owner's (OD-52, lane G).

protects: the first journal rows to carry a writer class arrived as ``cp_relay`` with no app and no
dataset, and every entry the owner's journaling app pushes after them must stay provable without
another receipt per entry. Two things stood in the way on the node:

  - rows written between the node recording a journal row's class (16 Sep, first installed
    30 Sep) and recording its app and dataset (step 56's columns, 30 Sep) carry the class only.
    The door had a dataset to give; the table had nowhere to put it, and the columns arrive empty
    (no backfill). That is the first test: it reproduces those rows.
  - today's door records the dataset of the resource the control plane authorised
    (``<owner>:default:<device>`` for a local node), while ``capture_receipts.proven`` binds a
    journal row to the dataset of the source's install (``<owner>:topos:<topos id>`` when the web
    app installed it with a Topos selected). Two names for one store; where they differ, a stamped
    push from an attested app could never prove. The ``app_ingest`` door now records the install's
    dataset for a journal write (``capture_receipts.door_dataset``).

What must NOT change, and is pinned here: the dataset says where a row went, never who wrote it.
A grantee's push, an app the owner never attested, a push authorised for another owner's dataset,
a source with two installs or none, and every non-journal table keep failing or keep the dataset
they recorded before. A receipt over zero rows attests the app for the rows it writes next, and a
receipt can be revoked and made again under the app's real id.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

import pytest

from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2.canonical import PolicyError
from topos.principal import OWNER_APP
from topos.storage.db.migrations.entity_mentions_authored_v1 import apply_entity_mentions_authored_v1_up

# tests/ingestion is not a package: sibling test modules import by their own name.
from test_ai_chat_writer_class import GRANTEE, OWNER, _relay, _stamp, captured_jobs, conn  # noqa: F401
from test_canonical_writer_class import _TIME_LOG, _notion_page, _rows, _time_log, time_log_source  # noqa: F401
from test_canonical_writer_identity import _entry, _through_the_pipeline

DEVICE = "0f1e2d3c4b5a6978"
TOPOS = "topos_" + "7" * 32
#: The dataset of the resource the control plane authorised (control_plane uma/dataset_ids.py).
AUTHORISED = f"{OWNER}:default:{DEVICE}"
#: The dataset the owner's app installed the source under (the web app's install scope).
INSTALLED = f"{OWNER}:topos:{TOPOS}"
CAPTURE_APP = "journal-capture-app"
TABLE = "journal_entries"
IDENTITY = ("writer_class", "writer_app_id", "writer_dataset_id")


def _install(db: sqlite3.Connection, *, source: str = _TIME_LOG, dataset: Any = INSTALLED, user: str = OWNER,
             active: int = 1, status: str = "active") -> None:
    """One source_runtime_installs row, as the install service writes it (concrete scope)."""
    db.execute("""CREATE TABLE IF NOT EXISTS source_runtime_installs (
        install_id TEXT PRIMARY KEY, scope_key TEXT, source_id TEXT, version_id TEXT, status TEXT, is_active INTEGER,
        source_definition_json TEXT, source_version_row_json TEXT, failure_reason TEXT, created_at TEXT, updated_at TEXT)""")
    count = db.execute("SELECT COUNT(*) FROM source_runtime_installs").fetchone()[0]
    scope = json.dumps({"dataset_id": dataset, "device_id": "*", "topos_id": TOPOS, "user_id": user},
                       separators=(",", ":"), sort_keys=True)
    db.execute("INSERT INTO source_runtime_installs (install_id, scope_key, source_id, version_id, status, is_active, "
               "source_definition_json) VALUES (?,?,?,?,?,?,?)",
               (f"install-{count}", scope, source, "v1", status, active, json.dumps({"source_id": source})))
    db.commit()


def _push(msg_id: str, record: Dict[str, Any], *, source: str = _TIME_LOG, user: str = OWNER,
          dataset: str = AUTHORISED, requester: str = OWNER, app: str = CAPTURE_APP) -> Dict[str, Any]:
    """The relay message the control plane's app_ingest route sends (routes/ingestion.py)."""
    return {"id": msg_id, "type": "app_ingest",
            "payload": {"user_id": user, "dataset_id": dataset, "source_id": source, "records": [record],
                        "resource_id": f"dataset:{user}:{dataset}:{DEVICE}", "app_id": app,
                        "requesting_user_id": requester}}


def _owner_capture(msg_id: str, record: Dict[str, Any], *, app: str = CAPTURE_APP, **push: Any) -> Dict[str, Any]:
    """Rule C at the control plane: the owner's own grant, the app on the owner's capture list."""
    return _stamp(_push(msg_id, record, app=app, **push), cls=OWNER_APP, client_id=app, acting_user=OWNER)


def _journal_row(db: sqlite3.Connection) -> Dict[str, Any]:
    (row,) = _rows(db, TABLE)
    return row


def _identity(row: Dict[str, Any]) -> tuple:
    return tuple(row[name] for name in IDENTITY)


def _attest(db: sqlite3.Connection, app: str = CAPTURE_APP, *, source: str = _TIME_LOG) -> Dict[str, Any]:
    preview = cr.preview(db, owner_id=OWNER, table=TABLE, source_id=source, app_id=app)
    receipt = cr.attest(db, owner_id=OWNER, table=TABLE, source_id=source, app_id=app,
                        preview_digest=preview["preview_digest"], confirm=True)
    db.commit()
    return receipt


def _proven(db: sqlite3.Connection, row: Dict[str, Any], *, source: str = _TIME_LOG) -> bool:
    return cr.proven(db, owner_id=OWNER, table=TABLE, identity_source_id=source, row=row)


# --- why the first stamped pushes carry no dataset -------------------------------------------------

@pytest.mark.asyncio
async def test_a_push_before_the_identity_columns_existed_records_its_class_only(conn, captured_jobs,
                                                                                 time_log_source, monkeypatch):
    """The node between the class (790303b2) and the app and dataset columns (2b4a42f8, step 56).

    The always-run step then added ``writer_class`` alone, and every write re-runs it (the canonical
    store applies migrations when it opens), so the schema is rolled back by rolling the step back.
    """
    from topos.storage.db.migrations import entity_mentions_authored_v1 as step

    _install(conn)
    monkeypatch.setattr(step, "_WRITER_COLUMNS", ("writer_class",))
    for column in ("writer_app_id", "writer_dataset_id"):
        conn.execute(f"ALTER TABLE {TABLE} DROP COLUMN {column}")
    conn.commit()

    assert (await _relay(_push("req-early", _time_log())))["status"] == "ok"
    assert _journal_row(conn)["writer_class"] == "cp_relay"
    assert "writer_dataset_id" not in _journal_row(conn)  # the door had a dataset; the table had no column

    monkeypatch.undo()
    apply_entity_mentions_authored_v1_up(conn)  # the next install's always-run step: columns, no backfill
    row = _journal_row(conn)
    assert _identity(row) == ("cp_relay", None, None)
    # Such a row is neither the owner's by its door nor attestable: a receipt lists pre-stamp rows only.
    _attest(conn, "provisional-app")
    assert cr.preview(conn, owner_id=OWNER, table=TABLE, source_id=_TIME_LOG, app_id=CAPTURE_APP)["row_count"] == 0
    assert not _proven(conn, row)


# --- the door records the dataset the row is proven against --------------------------------------

@pytest.mark.asyncio
async def test_an_attested_apps_push_is_written_through_the_install_and_proves(conn, captured_jobs,
                                                                               time_log_source):
    _install(conn)
    assert (await _relay(_owner_capture("req-capture", _time_log())))["status"] == "ok"
    row = _journal_row(conn)
    assert _identity(row) == ("owner_app", CAPTURE_APP, INSTALLED)
    (place,) = _rows(conn, "location_events")  # the entry's location child is the same write
    assert _identity(place) == ("owner_app", CAPTURE_APP, INSTALLED)

    assert not _proven(conn, row)  # stamped by an app the owner has not vouched for on this node
    receipt = _attest(conn)  # nothing pre-stamp to list: the receipt attests the app
    assert receipt["row_count"] == 0 and receipt["dataset_certified"] is True
    assert _proven(conn, row)
    assert cr.certified_dataset(conn, owner_id=OWNER, table=TABLE, row=row) == INSTALLED

    cr.revoke(conn, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    conn.commit()
    assert not _proven(conn, row)


def test_the_authorised_name_alone_never_proved_a_stamped_row(conn):
    """What the door recorded before: the same row under the resource's name fails the install's binding."""
    _install(conn)
    _attest(conn)
    conn.execute(f"INSERT INTO {TABLE} (entry_id, entry_at, content, source_id, writer_class, writer_app_id, "
                 "writer_dataset_id) VALUES ('e-before', '2026-09-30T10:00:00', 'A synthetic entry.', ?, "
                 "'owner_app', ?, ?)", (_TIME_LOG, CAPTURE_APP, AUTHORISED))
    conn.commit()
    (row,) = [r for r in _rows(conn, TABLE) if r["entry_id"] == "e-before"]
    assert not _proven(conn, row)


@pytest.mark.asyncio
async def test_a_grantees_push_is_written_through_the_install_and_still_proves_nothing(conn, captured_jobs,
                                                                                         time_log_source):
    _install(conn)
    _attest(conn)
    message = _push("req-grantee", _time_log(), requester=GRANTEE)  # no stamp: rule C never stamps a grantee
    assert (await _relay(message))["status"] == "ok"
    row = _journal_row(conn)
    assert _identity(row) == ("cp_relay", None, INSTALLED)
    assert not _proven(conn, row)


@pytest.mark.asyncio
async def test_an_owner_stamp_from_an_app_the_owner_never_attested_proves_nothing(conn, captured_jobs,
                                                                                   time_log_source):
    _install(conn)
    _attest(conn)
    assert (await _relay(_owner_capture("req-other-app", _time_log(), app="another-app")))["status"] == "ok"
    row = _journal_row(conn)
    assert _identity(row) == ("owner_app", "another-app", INSTALLED)
    assert not _proven(conn, row)


@pytest.mark.asyncio
async def test_an_app_attested_for_another_source_proves_nothing_here(conn, captured_jobs, time_log_source):
    _install(conn)
    _install(conn, source="other_journal", dataset=INSTALLED)
    _attest(conn, source="other_journal")  # the app is the owner's for that source, not this one
    assert (await _relay(_owner_capture("req-cross-source", _time_log())))["status"] == "ok"
    assert not _proven(conn, _journal_row(conn))


# --- when the install cannot name the dataset, the door records the authorised one ---------------

@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["another_owners_dataset", "another_owner", "no_install", "two_datasets",
                                  "retired_install_elsewhere"])
async def test_without_one_install_of_this_owner_the_door_keeps_the_authorised_dataset(
        conn, captured_jobs, time_log_source, case):
    push: Dict[str, Any] = {}
    if case == "another_owners_dataset":
        _install(conn)
        push["dataset"] = f"{GRANTEE}:default:{DEVICE}"  # authorised for a dataset that is not this owner's
    elif case == "another_owner":
        _install(conn)
        push["user"] = GRANTEE  # the install binds the source to OWNER, not to the resource's owner
        push["dataset"] = f"{GRANTEE}:default:{DEVICE}"
    elif case == "two_datasets":
        _install(conn)
        _install(conn, dataset=f"{OWNER}:topos:topos_" + "8" * 32)
    elif case == "retired_install_elsewhere":  # install_dataset counts every install the owner ever had
        _install(conn, dataset=f"{OWNER}:topos:topos_" + "8" * 32, active=0, status="retired")
        _install(conn)
    assert (await _relay(_owner_capture("req-" + case, _time_log(), **push)))["status"] == "ok"
    row = _journal_row(conn)
    assert row["writer_dataset_id"] == push.get("dataset", AUTHORISED)

    if case in ("another_owners_dataset", "another_owner"):
        _attest(conn)  # the owner's install certifies a dataset; the row was not written into it
    else:
        with pytest.raises(PolicyError) as refused:  # no one certified dataset: no receipt binds anything
            _attest(conn)
        assert refused.value.code == "capture_attestation_invalid"
    assert not _proven(conn, row)


@pytest.mark.asyncio
async def test_a_non_journal_push_keeps_the_authorised_dataset(conn, captured_jobs):
    _install(conn, source="notion_pages")  # an install bound elsewhere changes nothing off the journal family
    page = _notion_page(doc_id="notion_pages:p1")
    assert (await _relay(_owner_capture("req-doc", page, source="notion_pages")))["status"] == "ok"
    (doc,) = _rows(conn, "documents")
    assert _identity(doc) == ("owner_app", CAPTURE_APP, AUTHORISED)


def test_a_named_dataset_follows_the_class_like_the_app(conn, time_log_source, monkeypatch):
    """An internal replay (no door) records no dataset, even when a caller hands it the install's name."""
    _through_the_pipeline(conn, monkeypatch, _entry(), writer_class=None, writer_dataset_id=INSTALLED)
    assert _identity(_journal_row(conn)) == (None, None, None)
    conn.execute(f"DELETE FROM {TABLE}")
    _through_the_pipeline(conn, monkeypatch, _entry(), writer_class="cp_relay", writer_dataset_id=INSTALLED)
    assert _identity(_journal_row(conn)) == ("cp_relay", None, INSTALLED)


@pytest.mark.asyncio
async def test_an_unreadable_install_record_keeps_the_authorised_dataset(conn, captured_jobs, time_log_source,
                                                                         monkeypatch):
    _install(conn)

    def _broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic")

    monkeypatch.setattr("topos.permissions_v2.capture_receipts.install_dataset", _broken)
    assert (await _relay(_owner_capture("req-broken", _time_log())))["status"] == "ok"
    assert _identity(_journal_row(conn)) == ("owner_app", CAPTURE_APP, AUTHORISED)


# --- the owner's step: re-attest the source under the app's real id ------------------------------

def _pre_stamp(db: sqlite3.Connection, entry_id: str, content: str) -> None:
    db.execute(f"INSERT INTO {TABLE} (entry_id, entry_at, content, source_id) VALUES (?,?,?,?)",
               (entry_id, "2026-09-01T10:00:00", content, _TIME_LOG))
    db.commit()


def _stamped(db: sqlite3.Connection, entry_id: str, app: str) -> Dict[str, Any]:
    db.execute(f"INSERT INTO {TABLE} (entry_id, entry_at, content, source_id, writer_class, writer_app_id, "
               "writer_dataset_id) VALUES (?,?,?,?,?,?,?)",
               (entry_id, "2026-10-01T10:00:00", f"Synthetic entry {entry_id}.", _TIME_LOG, "owner_app", app,
                INSTALLED))
    db.commit()
    (row,) = [r for r in _rows(db, TABLE) if r["entry_id"] == entry_id]
    return row


def test_revoking_a_provisional_receipt_and_attesting_the_real_app_keeps_every_old_row(conn):
    _install(conn)
    for n in range(3):
        _pre_stamp(conn, f"old-{n}", f"Synthetic older entry {n}.")
    provisional = _attest(conn, "provisional-app")
    assert provisional["row_count"] == 3
    old = [r for r in _rows(conn, TABLE) if r["entry_id"].startswith("old-")]
    new = _stamped(conn, "new-1", CAPTURE_APP)
    assert all(_proven(conn, r) for r in old) and not _proven(conn, new)

    cr.revoke(conn, owner_id=OWNER, receipt_id=provisional["receipt_id"])
    conn.commit()
    assert not any(_proven(conn, r) for r in old)  # until the new receipt, nothing of the source counts
    real = _attest(conn)
    assert real["row_count"] == 3  # the old rows are listed again, under the real app
    assert all(_proven(conn, r) for r in old) and _proven(conn, new)
    assert not _proven(conn, _stamped(conn, "new-2", "provisional-app"))  # the revoked app's id no longer counts


def test_a_zero_row_receipt_beside_a_live_one_attests_the_real_app_too(conn):
    """The other order: keep the provisional receipt and add one that lists nothing."""
    _install(conn)
    _pre_stamp(conn, "old-0", "Synthetic older entry.")
    _attest(conn, "provisional-app")
    extra = _attest(conn)
    assert extra["row_count"] == 0
    assert _proven(conn, _stamped(conn, "new-1", CAPTURE_APP))
    assert {r["app_id"] for r in cr.receipts(conn, owner_id=OWNER) if r["revoked_at"] is None} == {
        "provisional-app", CAPTURE_APP}
