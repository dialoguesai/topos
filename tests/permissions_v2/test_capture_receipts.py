"""OD-50/OD-52: a journal row is the owner's own only when its door, or the owner, says so.

protects: ``record_role`` calls every journal row authored because of the table it sits in. Any
writer that reaches an ingest door of a journal-lane source can put a row there, and rows written
before the node recorded writer classes (all of them, on a node installed before September 2026)
name no door at all. A grant that released "the owner's journal" on the table's word would release
whatever anyone wrote into it. ``capture_receipts.proven`` is the rule the evidence layer will ask
instead: a row counts only when the door recorded an owner writer through an app the owner attested
(or the owner's file import), into the dataset the owner's one install of the source is scoped to,
or when the owner attested the pre-stamp row itself at its current words.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

import pytest

from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2.canonical import PolicyError
from topos.principal import OWNER_APP
from topos.storage.db.migrations import apply_all_migrations

OWNER = "owner-uuid-1"
OTHER_OWNER = "owner-uuid-9"
RESOURCE = "resource-1"
SOURCE = "time_log"
DATASET = f"{OWNER}:topos:default"
APP = "owner-journal-app"
TABLE = "journal_entries"
WORDS = "Finished the draft and walked home the long way."


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "receipts.db"))
    apply_all_migrations(conn)
    conn.commit()
    yield conn
    conn.close()


def _install(db, *, source: str = SOURCE, dataset: Any = DATASET, user: str = OWNER, active: int = 1,
             status: str = "active") -> None:
    """One source_runtime_installs row, as the install service writes it."""
    db.execute("""CREATE TABLE IF NOT EXISTS source_runtime_installs (
        install_id TEXT PRIMARY KEY, scope_key TEXT, source_id TEXT, version_id TEXT, status TEXT, is_active INTEGER,
        source_definition_json TEXT, source_version_row_json TEXT, failure_reason TEXT, created_at TEXT, updated_at TEXT)""")
    count = db.execute("SELECT COUNT(*) FROM source_runtime_installs").fetchone()[0]
    scope = json.dumps({"user_id": user, "topos_id": RESOURCE, "device_id": "*", "dataset_id": dataset})
    db.execute("INSERT INTO source_runtime_installs (install_id, scope_key, source_id, version_id, status, is_active, "
               "source_definition_json) VALUES (?,?,?,?,?,?,?)",
               (f"install-{count}", scope, source, "v1", status, active, json.dumps({"source_id": source})))
    db.commit()


def _entry(db, entry_id: str, content: str = WORDS, *, source: str = SOURCE, writer_class=None, app=None,
           dataset=None) -> None:
    db.execute("INSERT OR REPLACE INTO journal_entries (entry_id, entry_at, content, source_id, writer_class, "
               "writer_app_id, writer_dataset_id) VALUES (?,?,?,?,?,?,?)",
               (entry_id, "2026-09-01T10:00:00", content, source, writer_class, app, dataset))
    db.commit()


def _row(db, entry_id: str) -> Dict[str, Any]:
    cursor = db.execute("SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,))
    return dict(zip([c[0] for c in cursor.description], cursor.fetchone()))


def _proven(db, entry_id: str, *, owner: str = OWNER, source: str = SOURCE) -> bool:
    return cr.proven(db, owner_id=owner, table=TABLE, identity_source_id=source, row=_row(db, entry_id))


def _attest(db, *, owner: str = OWNER, source: str = SOURCE, app: str = APP) -> Dict[str, Any]:
    preview = cr.preview(db, owner_id=owner, table=TABLE, source_id=source, app_id=app)
    receipt = cr.attest(db, owner_id=owner, table=TABLE, source_id=source, app_id=app,
                        preview_digest=preview["preview_digest"], confirm=True)
    db.commit()
    return receipt


# --- pre-stamp rows: only the owner's receipt ---------------------------------------------------

def test_a_pre_stamp_row_is_nobodys_until_the_owner_attests_it(db):
    _install(db)
    _entry(db, "e-old")
    assert not _proven(db, "e-old")  # the table's name proves nothing

    preview = cr.preview(db, owner_id=OWNER, table=TABLE, source_id=SOURCE, app_id=APP)
    assert preview["row_count"] == 1 and preview["dataset_certified"] is True
    assert "e-old" not in json.dumps(preview) and WORDS not in json.dumps(preview)
    assert not _proven(db, "e-old")  # a preview records nothing

    with pytest.raises(PolicyError) as unconfirmed:
        cr.attest(db, owner_id=OWNER, table=TABLE, source_id=SOURCE, app_id=APP,
                  preview_digest=preview["preview_digest"], confirm="yes")
    assert unconfirmed.value.code == "capture_attestation_unconfirmed"
    with pytest.raises(PolicyError) as stale:
        cr.attest(db, owner_id=OWNER, table=TABLE, source_id=SOURCE, app_id=APP, preview_digest="0" * 64,
                  confirm=True)
    assert stale.value.code == "capture_attestation_preview_stale"
    assert not cr.installed(db) and not _proven(db, "e-old")

    receipt = _attest(db)
    assert receipt["row_count"] == 1 and _proven(db, "e-old")
    assert cr.certified_dataset(db, owner_id=OWNER, table=TABLE, row=_row(db, "e-old")) == DATASET


def test_a_row_that_arrives_after_the_preview_is_not_swept_in(db):
    _install(db)
    _entry(db, "e-old")
    preview = cr.preview(db, owner_id=OWNER, table=TABLE, source_id=SOURCE, app_id=APP)
    _entry(db, "e-late", "Written by someone else between the preview and the confirmation.")
    with pytest.raises(PolicyError) as stale:
        cr.attest(db, owner_id=OWNER, table=TABLE, source_id=SOURCE, app_id=APP,
                  preview_digest=preview["preview_digest"], confirm=True)
    assert stale.value.code == "capture_attestation_preview_stale"


def test_an_attested_row_that_is_rewritten_falls_out_of_its_receipt(db):
    _install(db)
    _entry(db, "e-old")
    _attest(db)
    db.execute("UPDATE journal_entries SET content='Different words now.' WHERE entry_id='e-old'")
    assert not _proven(db, "e-old")
    assert cr.certified_dataset(db, owner_id=OWNER, table=TABLE, row=_row(db, "e-old")) is None


def test_revoking_a_receipt_withdraws_its_rows_and_its_app(db):
    _install(db)
    _entry(db, "e-old")
    receipt = _attest(db)
    _entry(db, "e-new", writer_class="owner_app", app=APP, dataset=DATASET)
    assert _proven(db, "e-old") and _proven(db, "e-new")

    cr.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    assert not _proven(db, "e-old") and not _proven(db, "e-new")
    with pytest.raises(PolicyError) as again:
        cr.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    assert again.value.code == "capture_receipt_revoked"
    with pytest.raises(PolicyError) as foreign:
        cr.revoke(db, owner_id=OTHER_OWNER, receipt_id=receipt["receipt_id"])
    assert foreign.value.code == "capture_receipt_unknown"
    (listed,) = cr.receipts(db, owner_id=OWNER)
    assert listed["revoked_at"] is not None and listed["table"] == TABLE


# --- stamped rows: the door's record ---------------------------------------------------------------

def test_a_stamped_row_counts_only_through_an_app_the_owner_attested(db):
    _install(db)
    _entry(db, "e-app", writer_class="owner_app", app=APP, dataset=DATASET)
    assert not _proven(db, "e-app")  # an owner stamp names an app; the owner has not vouched for it

    receipt = _attest(db)  # nothing pre-stamp to list: the receipt attests the app
    assert receipt["row_count"] == 0 and _proven(db, "e-app")
    _entry(db, "e-other-app", writer_class="owner_app", app="some-other-app", dataset=DATASET)
    _entry(db, "e-socket", writer_class="owner_app", app=None, dataset=DATASET)
    assert not _proven(db, "e-other-app") and not _proven(db, "e-socket")


def test_the_owners_file_import_counts_and_every_other_class_does_not(db):
    _install(db)
    _attest(db)
    _entry(db, "e-import", writer_class="owner_import", dataset=DATASET)
    assert _proven(db, "e-import")
    for writer in ("cp_relay", "third_party", "local_legacy", "owner_automation", "something_new"):
        _entry(db, f"e-{writer}", writer_class=writer, app=APP, dataset=DATASET)
        assert not _proven(db, f"e-{writer}"), writer


def test_a_stamped_row_must_have_been_written_into_the_installs_dataset(db):
    _install(db)
    _attest(db)
    for entry_id, dataset in (("e-elsewhere", f"{OTHER_OWNER}:topos:default"), ("e-nowhere", None)):
        _entry(db, entry_id, writer_class="owner_app", app=APP, dataset=dataset)
        assert not _proven(db, entry_id)
        _entry(db, entry_id + "-import", writer_class="owner_import", dataset=dataset)
        assert not _proven(db, entry_id + "-import")


def test_the_identity_must_name_the_rows_own_source(db):
    _install(db)
    _entry(db, "e-old")
    _attest(db)
    assert _proven(db, "e-old") and not _proven(db, "e-old", source="another_source")


# --- the install is the owner binding --------------------------------------------------------------

@pytest.mark.parametrize("installs", [
    [],                                                                  # never installed
    [dict(dataset=DATASET), dict(dataset=f"{OWNER}:topos:second")],      # two live installs
    [dict(dataset="*")],                                                 # a wildcard dataset
    [dict(dataset=DATASET, user=OTHER_OWNER)],                           # someone else's install
    [dict(dataset=DATASET, status="failed")],                            # not live
])
def test_without_one_live_install_scoped_to_this_owner_nothing_is_proven(db, installs):
    for install in installs:
        _install(db, **install)
    if not installs:
        db.execute("CREATE TABLE source_runtime_installs (install_id TEXT, scope_key TEXT, source_id TEXT, "
                   "status TEXT, is_active INTEGER)")
    _entry(db, "e-old")
    _entry(db, "e-import", writer_class="owner_import", dataset=DATASET)
    preview = cr.preview(db, owner_id=OWNER, table=TABLE, source_id=SOURCE, app_id=APP)
    assert preview["row_count"] == 0 and preview["dataset_certified"] is False
    with pytest.raises(PolicyError) as refused:
        cr.attest(db, owner_id=OWNER, table=TABLE, source_id=SOURCE, app_id=APP,
                  preview_digest=preview["preview_digest"], confirm=True)
    assert refused.value.code == "capture_attestation_invalid"
    assert not _proven(db, "e-old") and not _proven(db, "e-import")


def test_a_receipt_stops_counting_when_the_install_no_longer_binds_the_source(db):
    _install(db)
    _entry(db, "e-old")
    _attest(db)
    assert _proven(db, "e-old")
    _install(db, dataset=f"{OWNER}:topos:second")  # a second live install: the binding is ambiguous
    assert not _proven(db, "e-old")


def test_another_owners_receipt_never_counts(db):
    _install(db)
    _install(db, user=OTHER_OWNER, dataset=f"{OTHER_OWNER}:topos:default")
    _entry(db, "e-old")
    _attest(db, owner=OTHER_OWNER)
    assert not _proven(db, "e-old")
    assert cr.capture_apps(db, owner_id=OWNER, table=TABLE, source_id=SOURCE) == frozenset()
    assert cr.receipts(db, owner_id=OWNER) == []
    # ... and each owner's view is its own.
    assert _proven(db, "e-old", owner=OTHER_OWNER)


# --- closed tables, immutable receipts -------------------------------------------------------------

@pytest.mark.parametrize("table", ["browser_visits", "conversation_messages", "journal_entries; DROP TABLE x", None, 7])
def test_only_a_registered_table_can_be_attested(db, table):
    _install(db)
    with pytest.raises(PolicyError) as refused:
        cr.preview(db, owner_id=OWNER, table=table, source_id=SOURCE, app_id=APP)
    assert refused.value.code == "capture_attestation_invalid"
    assert cr.proven(db, owner_id=OWNER, table=table, identity_source_id=SOURCE, row={"source_id": SOURCE}) is False


@pytest.mark.parametrize("field, value", [("owner_id", ""), ("source_id", " padded "), ("app_id", None),
                                          ("app_id", "x" * 200)])
def test_a_malformed_request_is_refused(db, field, value):
    _install(db)
    request = {"owner_id": OWNER, "table": TABLE, "source_id": SOURCE, "app_id": APP, field: value}
    with pytest.raises(PolicyError):
        cr.preview(db, **request)


def test_a_receipt_cannot_be_edited_or_grown(db):
    _install(db)
    _entry(db, "e-old")
    receipt = _attest(db)
    for statement in ("UPDATE capture_receipts SET app_id='another-app'",
                      "UPDATE capture_receipts SET dataset_id='another:dataset'",
                      "UPDATE capture_receipt_rows SET content_revision='0'"):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(statement)
    cr.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    with pytest.raises(sqlite3.DatabaseError):  # a revoked receipt takes no more rows
        db.execute("INSERT INTO capture_receipt_rows VALUES (?,?,?,?)", (receipt["receipt_id"], TABLE, "e-x", "0"))
    with pytest.raises(sqlite3.DatabaseError):  # and is never un-revoked
        db.execute("UPDATE capture_receipts SET revoked_at=NULL")


def test_a_second_attestation_lists_only_what_the_first_did_not(db):
    _install(db)
    _entry(db, "e-1")
    first = _attest(db)
    _entry(db, "e-2", "A second entry, written later.")
    second = _attest(db)
    assert (first["row_count"], second["row_count"]) == (1, 1)
    assert _proven(db, "e-1") and _proven(db, "e-2")
    cr.revoke(db, owner_id=OWNER, receipt_id=first["receipt_id"])
    assert not _proven(db, "e-1") and _proven(db, "e-2")


def test_a_door_written_row_names_its_own_dataset(db):
    _install(db)
    _entry(db, "e-app", writer_class="owner_app", app=APP, dataset=DATASET)
    assert cr.certified_dataset(db, owner_id=OWNER, table=TABLE, row=_row(db, "e-app")) == DATASET
    _entry(db, "e-unknown")  # pre-stamp and unattested: nothing names a dataset
    assert cr.certified_dataset(db, owner_id=OWNER, table=TABLE, row=_row(db, "e-unknown")) is None


# --- the owner socket route ------------------------------------------------------------------------

@pytest.fixture()
def owner_app(db, monkeypatch, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    from fastapi import FastAPI
    from topos.api import permissions_capture_receipts
    from topos.config.settings import settings as runtime_settings

    runtime = SimpleNamespace(protocol=SimpleNamespace(canonical_database=Path(tmp_path / "receipts.db"),
        ledger=SimpleNamespace(identity=SimpleNamespace(owner_id=OWNER))))
    monkeypatch.setattr("topos.permissions_v2.runtime.get_runtime", lambda: runtime)
    monkeypatch.setattr(runtime_settings, "topos_owner_key", "owner-key", raising=False)
    app = FastAPI()
    app.include_router(permissions_capture_receipts.router)
    return app


async def _call(app, method: str, path: str, *, socket: bool = True, **kwargs):
    import httpx
    from topos.uds import UDSChannelApp

    transport = httpx.ASGITransport(app=UDSChannelApp(app) if socket else app)
    headers = {} if socket else {"Authorization": "Bearer owner-key"}
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        return await client.request(method, f"/v1/permissions-beta/v2/capture-attestation{path}",
                                    headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_the_route_is_the_owners_socket_only(db, owner_app):
    _install(db)
    _entry(db, "e-route")
    body = {"table": TABLE, "source_id": SOURCE, "app_id": APP}

    for path in ("/preview", "/attest", "/revoke"):  # the owner key over TCP is not the owner's socket
        response = await _call(owner_app, "POST", path, socket=False, json=body)
        assert response.status_code == 403, (path, response.text)
    assert (await _call(owner_app, "GET", "/receipts", socket=False)).status_code == 403

    preview = (await _call(owner_app, "POST", "/preview", json=body)).json()
    assert preview["row_count"] == 1 and "e-route" not in str(preview)
    stale = await _call(owner_app, "POST", "/attest", json={**body, "preview_digest": "0" * 64, "confirm": True})
    assert (stale.status_code, stale.json()["detail"]) == (409, "capture_attestation_preview_stale")
    bad_table = await _call(owner_app, "POST", "/preview", json={**body, "table": "conversation_messages"})
    assert (bad_table.status_code, bad_table.json()["detail"]) == (400, "capture_attestation_invalid")
    assert not _proven(db, "e-route")

    attested = await _call(owner_app, "POST", "/attest",
                           json={**body, "preview_digest": preview["preview_digest"], "confirm": True})
    assert attested.status_code == 200, attested.text
    assert _proven(db, "e-route")
    listed = (await _call(owner_app, "GET", "/receipts")).json()["receipts"]
    assert [r["receipt_id"] for r in listed] == [attested.json()["receipt_id"]]

    revoked = await _call(owner_app, "POST", "/revoke", json={"receipt_id": attested.json()["receipt_id"]})
    assert revoked.status_code == 200 and not _proven(db, "e-route")
    again = await _call(owner_app, "POST", "/revoke", json={"receipt_id": attested.json()["receipt_id"]})
    assert again.status_code == 409


def test_the_node_serves_the_route():
    """A route no app mounts is a rule nobody can use (the OD-39 route is mounted the same way)."""
    from pathlib import Path
    import topos

    source = (Path(topos.__file__).parent / "app.py").read_text()
    assert "app.include_router(permissions_capture_receipts_routes.router)" in source
