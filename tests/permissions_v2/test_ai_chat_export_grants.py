"""The ChatGPT export import (``chatgpt_file_ingestion``) becomes a grant source only through the owner's word.

protects: on one owner's node every in-window row of the export source was withheld as
``source_posture_unknown`` for two independent reasons, and both had to be fixed without widening anything:

- the source had two active runtime installs, and ``evidence._source_posture`` refuses a source with more
  than one. Which install stays is the owner's call, so the node offers an owner-socket listing and a
  deactivation of the one the owner names (dry run first), and install refuses to create the state again;
- no export row carries a certified dataset (RD5 covered the capture lane only), and none names its writer,
  so nothing bound a row to the owner or to the install's dataset. The rows are never re-labelled (the #68
  rule): the owner attests the import once through the generalised capture receipts (table
  ``ai_chat_messages``), and a row counts only while a live receipt lists it at its current words. The export
  door already records ``owner_import`` and its dataset for new rows.

A receipt, a door stamp or a deactivation unlocks posture and authorship only; every later check (role,
Off-limits, copies, content, assessment) still runs, and an app's stamp, a grantee's write, a revoked receipt
or an edited row prove nothing.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

import pytest

from tests.ingestion.test_ai_chat_writer_class import OWNER
from tests.permissions_v2.test_ai_chat_capture_provenance import (  # noqa: F401 (fixture)
    OTHER_OWNER, _identity, _resolver, db)
from tests.permissions_v2.test_ai_chat_dataset_binding import DATASET, OTHER_DATASET, _install
from topos.permissions_v2 import ai_chat_capture
from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.evidence import _source_posture
from topos.permissions_v2.message_evidence import _source_checks
from topos.sources import install_maintenance

EXPORT = "chatgpt_file_ingestion"
TABLE = "ai_chat_messages"
IMPORT_APP = "owner_import"
PROMPT = "Help me outline a talk about tide pools for a school visit."
REPLY = "Here is a five-part outline you could use."


def _export_row(db, message_id: str, *, content: str = PROMPT, role: str = "user", writer=None, app=None,
                dataset=None, owner: str = OWNER, conversation: str = "chatgpt:thread-1", source: str = EXPORT):
    """A row as the export import wrote it; before writer classes, writer and dataset are NULL."""
    db.execute("INSERT OR IGNORE INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, "
               "created_at, updated_at) VALUES (?,?,NULL,?,'2026-09-01','2026-09-01')", (conversation, owner, source))
    db.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, source_id, "
               "writer_class, writer_app_id, writer_dataset_id) VALUES (?,?,?,'2026-09-01T10:00:00Z',?,?,?,?,?)",
               (message_id, conversation, role, content, source, writer, app, dataset))
    db.commit()


def _code(call) -> str | None:
    try:
        call()
    except PolicyError as exc:
        return exc.code
    return None


def _posture_code(db, message_id: str, source: str = EXPORT):
    resolver = _resolver(db)
    return _code(lambda: _source_posture(db, _identity(resolver, message_id, source)))


def _load_code(db, message_id: str, source: str = EXPORT):
    resolver = _resolver(db)
    return _code(lambda: resolver._load(db, _identity(resolver, message_id, source)))


def _check_code(db, message_id: str, source: str = EXPORT):
    """The message family's source checks after _load: provenance, authorship, posture, role, content."""
    resolver = _resolver(db)
    identity = _identity(resolver, message_id, source)
    return _code(lambda: _source_checks(resolver, db, identity, resolver._load(db, identity)))


def _preview(db, *, source: str = EXPORT, app: str = IMPORT_APP, owner: str = OWNER) -> Dict[str, Any]:
    return cr.preview(db, owner_id=owner, table=TABLE, source_id=source, app_id=app)


def _attest(db, *, source: str = EXPORT, app: str = IMPORT_APP, owner: str = OWNER) -> Dict[str, Any]:
    preview = _preview(db, source=source, app=app, owner=owner)
    receipt = cr.attest(db, owner_id=owner, table=TABLE, source_id=source, app_id=app,
                        preview_digest=preview["preview_digest"], confirm=True)
    db.commit()
    return receipt


def _two_installs(db, *, second_dataset=DATASET) -> None:
    """The owner's node: a 31 Aug install declaring `mixed` and a 9 Sep install declaring none, both active."""
    _install(db, source=EXPORT, posture="mixed")
    _install(db, source=EXPORT, dataset=second_dataset)


def _deactivate(db, install_id: str, *, dry_run: bool) -> Dict[str, Any]:
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        result = install_maintenance.deactivate(db, owner_id=OWNER, source_id=EXPORT, install_id=install_id,
                                                dry_run=dry_run, confirm=not dry_run)
    except BaseException:
        db.rollback()
        raise
    db.commit() if result["deactivated"] else db.rollback()
    return result


# --- 1. two active installs refuse; the owner's deactivation makes one --------------------------------------

def test_two_active_installs_refuse_and_one_the_owner_keeps_is_accepted(db):
    _two_installs(db)
    _export_row(db, "x-1")
    assert _posture_code(db, "x-1") == "source_posture_unknown"
    # The install record cannot name one live install, so the owner's receipt could certify nothing yet.
    assert _preview(db)["dataset_certified"] is False
    with pytest.raises(PolicyError, match="capture_attestation_invalid"):
        _attest(db)

    listed = install_maintenance.installs(db, source_id=EXPORT)
    assert listed["active_count"] == 2
    assert [(i["install_id"], i["active"], i["declared_posture"]) for i in listed["installs"]] == [
        ("install-0", True, "mixed"), ("install-1", True, None)]
    assert all(set(i) == {"install_id", "active", "status", "declared_posture", "scope", "created_at", "updated_at"}
               for i in listed["installs"])

    dry = _deactivate(db, "install-1", dry_run=True)
    assert (dry["dry_run"], dry["deactivated"], dry["active_before"], dry["active_after"]) == (True, False, 2, 1)
    assert dry["posture_resolvable_after"] is True and dry["receipt_dataset_certifiable_after"] is True
    assert install_maintenance.installs(db, source_id=EXPORT)["active_count"] == 2  # the dry run changed nothing
    assert _posture_code(db, "x-1") == "source_posture_unknown"

    real = _deactivate(db, "install-1", dry_run=False)
    assert (real["deactivated"], real["active_after"]) == (True, 1)
    kept = {i["install_id"]: i for i in install_maintenance.installs(db, source_id=EXPORT)["installs"]}
    assert kept["install-0"]["active"] and not kept["install-1"]["active"]
    assert kept["install-1"]["status"] == "superseded"  # retired, never deleted

    # One install now, but the row is still bound to nothing until the owner attests the import.
    assert _posture_code(db, "x-1") == "source_posture_unknown"
    receipt = _attest(db)
    assert receipt["dataset_certified"] is True and receipt["row_count"] == 1
    assert _posture_code(db, "x-1") is None
    assert _check_code(db, "x-1") is None


def test_the_deactivation_refuses_what_it_must_not_do(db):
    _two_installs(db)
    with pytest.raises(install_maintenance.MaintenanceError, match="install_unknown"):
        _deactivate(db, "install-9", dry_run=True)
    with pytest.raises(install_maintenance.MaintenanceError, match="install_deactivation_unconfirmed"):
        install_maintenance.deactivate(db, owner_id=OWNER, source_id=EXPORT, install_id="install-1", dry_run=False)
    with pytest.raises(install_maintenance.MaintenanceError, match="install_unknown"):
        # Another source's install id is not this source's.
        install_maintenance.deactivate(db, owner_id=OWNER, source_id="chatgpt_ui_conversation",
                                       install_id="install-1")
    _deactivate(db, "install-1", dry_run=False)
    with pytest.raises(install_maintenance.MaintenanceError, match="install_not_active"):
        _deactivate(db, "install-1", dry_run=True)
    with pytest.raises(install_maintenance.MaintenanceError, match="install_last_active"):
        _deactivate(db, "install-0", dry_run=True)  # never leaves the source with no install
    assert install_maintenance.installs(db, source_id=EXPORT)["active_count"] == 1


def test_the_dry_run_says_when_the_kept_install_still_cannot_certify_a_dataset(db):
    # Two installs on two datasets: retiring one leaves history naming both, so no receipt can bind a
    # pre-stamp row to either (ai_chat_capture.install_dataset). The dry run says so before anything moves.
    _two_installs(db, second_dataset=OTHER_DATASET)
    dry = _deactivate(db, "install-1", dry_run=True)
    assert dry["posture_resolvable_after"] is True
    assert dry["receipt_dataset_certifiable_after"] is False


def test_the_dry_run_names_the_ingest_clock_move_and_what_it_stales(db):
    _two_installs(db)
    assert _deactivate(db, "install-1", dry_run=True)["ingest_source_clock_advances"] is False
    # As the enrolled ingest store installs it (source clock v2 watches this table).
    db.execute("CREATE TABLE ingest_provenance_enrollments (enrollment_id TEXT PRIMARY KEY, state TEXT)")
    db.execute("INSERT INTO ingest_provenance_enrollments VALUES ('enr-1', 'active')")
    db.execute("CREATE TABLE clock_probe (n INTEGER)")
    db.execute("CREATE TRIGGER ingest_provenance_source_runtime_installs_update AFTER UPDATE ON "
               "source_runtime_installs BEGIN INSERT INTO clock_probe VALUES (1); END")
    db.commit()
    dry = _deactivate(db, "install-1", dry_run=True)
    assert (dry["ingest_source_clock_advances"], dry["native_enrollments_staled"]) == (True, 1)
    assert db.execute("SELECT COUNT(*) FROM clock_probe").fetchone()[0] == 0  # rolled back with the dry run


# --- 2. an export row: refused without a receipt, accepted with a live one -------------------------------

def test_an_export_row_without_a_receipt_is_refused(db):
    _install(db, source=EXPORT)
    _export_row(db, "x-none")
    assert _load_code(db, "x-none") == "source_posture_unknown"
    assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=EXPORT,
                                              row=_row(db, "x-none"))


def test_a_live_receipt_accepts_the_prompt_and_certifies_the_reply_without_making_it_the_owners(db):
    _install(db, source=EXPORT)
    _export_row(db, "x-prompt")
    _export_row(db, "x-reply", content=REPLY, role="assistant")
    receipt = _attest(db)
    assert receipt["row_count"] == 2 and receipt["table"] == TABLE
    assert _load_code(db, "x-prompt") is None and _check_code(db, "x-prompt") is None
    # The reply's posture resolves (a fact citing it can be judged), but it is never the owner's words.
    assert _load_code(db, "x-reply") is None
    assert _check_code(db, "x-reply") in {"native_owner_provenance_unavailable", "not_owner_authored"}


def test_a_revoked_receipt_refuses_again(db):
    _install(db, source=EXPORT)
    _export_row(db, "x-revoked")
    receipt = _attest(db)
    assert _check_code(db, "x-revoked") is None
    cr.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    db.commit()
    assert _load_code(db, "x-revoked") == "source_posture_unknown"
    assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=EXPORT,
                                              row=_row(db, "x-revoked"))


def test_an_edited_row_leaves_its_receipt_behind(db):
    _install(db, source=EXPORT)
    _export_row(db, "x-edited")
    _attest(db)
    assert _check_code(db, "x-edited") is None
    for column, value in (("content", PROMPT + " And a quiz."), ("sender_type", "human"),
                          ("conversation_id", "chatgpt:thread-2")):
        before = _row(db, "x-edited")[column]
        if column == "conversation_id":
            db.execute("INSERT OR IGNORE INTO ai_chat_conversations (conversation_id, owner_user_id, title, "
                       "source_id, created_at, updated_at) VALUES (?,?,NULL,?,'2026-09-01','2026-09-01')",
                       (value, OWNER, EXPORT))
        db.execute(f"UPDATE ai_chat_messages SET {column}=? WHERE message_id='x-edited'", (value,))
        db.commit()
        assert _load_code(db, "x-edited") == "source_posture_unknown", column
        assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=EXPORT,
                                                  row=_row(db, "x-edited")), column
        db.execute(f"UPDATE ai_chat_messages SET {column}=? WHERE message_id='x-edited'", (before,))
        db.commit()
        assert _check_code(db, "x-edited") is None, column
    # A new receipt covers only the row's words as they are now.
    assert _preview(db)["row_count"] == 0


def _row(db, message_id: str) -> Dict[str, Any]:
    return dict(db.execute("SELECT * FROM ai_chat_messages WHERE message_id=?", (message_id,)).fetchone())


# --- 3. new rows: the import door's own record, and nothing else ------------------------------------------

def test_the_owners_import_door_proves_a_new_row_and_no_other_writer_does(db):
    _install(db, source=EXPORT)
    _export_row(db, "x-door", writer="owner_import", dataset=DATASET)
    assert _check_code(db, "x-door") is None  # no receipt needed: the door recorded the owner's import
    # A receipt over the import (its app is the import door) must not lend itself to an app's stamp.
    _export_row(db, "x-other")
    _attest(db)
    for writer, app in (("cp_relay", None), ("third_party", None), ("owner_automation", None),
                        ("local_legacy", None), ("owner_app", "chatgpt-shadow-extension"), ("owner_app", IMPORT_APP)):
        db.execute("UPDATE ai_chat_messages SET writer_class=?, writer_app_id=? WHERE message_id='x-door'",
                   (writer, app))
        db.commit()
        assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=EXPORT,
                                                  row=_row(db, "x-door")), writer
    # The owner's import into another dataset than the install's is not bound to it.
    db.execute("UPDATE ai_chat_messages SET writer_class='owner_import', writer_app_id=NULL, writer_dataset_id=? "
               "WHERE message_id='x-door'", (OTHER_DATASET,))
    db.commit()
    assert _load_code(db, "x-door") == "source_posture_unknown"


def test_a_receipt_never_lists_a_stamped_row_and_never_certifies_one(db):
    _install(db, source=EXPORT)
    _export_row(db, "x-stamped", writer="cp_relay", dataset=DATASET)
    assert _preview(db)["row_count"] == 0
    _attest(db)
    assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=EXPORT,
                                              row=_row(db, "x-stamped"))


def test_the_parent_conversation_must_be_the_owners(db):
    _install(db, source=EXPORT)
    _export_row(db, "x-foreign", owner=OTHER_OWNER, conversation="chatgpt:thread-9")
    _attest(db)
    assert _load_code(db, "x-foreign") == "evidence_owner_binding"
    assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=EXPORT,
                                              row=_row(db, "x-foreign"))


def test_another_owners_receipt_proves_nothing_for_this_owner(db):
    _install(db, source=EXPORT, user="*")
    _export_row(db, "x-theirs")
    _attest(db, owner=OTHER_OWNER)
    assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=EXPORT,
                                              row=_row(db, "x-theirs"))
    assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=_row(db, "x-theirs")) is None


# --- 4. what the family admits --------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["chatgpt_ui_conversation", "chatgpt-owner-snapshot", "grow_journal", ""])
def test_the_import_family_admits_only_a_bundled_ai_chat_file_upload_source(db, source):
    # The capture lane has OD-39's receipts; the signed export lane keeps its own proof.
    with pytest.raises(PolicyError, match="capture_attestation_invalid"):
        _preview(db, source=source)


def test_the_import_receipt_names_the_import_door_and_no_app(db):
    _install(db, source=EXPORT)
    for app in ("chatgpt-shadow-extension", "some-app"):
        with pytest.raises(PolicyError, match="capture_attestation_invalid"):
            _preview(db, app=app)
    assert _preview(db)["app_id"] == IMPORT_APP


def test_the_journal_family_is_unchanged(db):
    # Its revision and its preview digest are byte-identical to the journal branch's (no hashing, same stream).
    row = {"entry_id": "e-1", "source_id": "grow_journal", "content": "Walked home."}
    assert cr.content_revision("journal_entries", row) == digest(
        {"table": "journal_entries", "record_id": "e-1", "source_id": "grow_journal", "content": "Walked home."})
    family = cr.family_of("journal_entries")
    rows = [("e-1", "a" * 64), ("e-2", "b" * 64)]
    assert cr._summary(OWNER, family, "grow_journal", "app", rows, DATASET)["preview_digest"] == digest(
        {"version": cr.VERSION, "owner_id": OWNER, "table": "journal_entries", "source_id": "grow_journal",
         "app_id": "app", "dataset_id": DATASET, "rows": [list(r) for r in rows]})


# --- 5. an export is large ------------------------------------------------------------------------------

def test_a_preview_over_more_rows_than_one_digest_can_hold(db):
    # 14,408 rows on one owner's node: [id, revision] pairs alone pass the 1 MiB canonical cap.
    family = cr.family_of(TABLE)
    rows = [(f"chatgpt:{index:036d}", "c" * 64) for index in range(15_000)]
    with pytest.raises(PolicyError, match="json_size"):
        digest({"rows": [list(r) for r in rows]})
    summary = cr._summary(OWNER, family, EXPORT, IMPORT_APP, rows, DATASET)
    assert summary["row_count"] == 15_000 and len(summary["preview_digest"]) == 64


def test_one_long_or_unencodable_row_does_not_refuse_the_whole_preview(db):
    _install(db, source=EXPORT)
    _export_row(db, "x-long", content="tide " * 400_000)  # > 1 MiB of words
    _export_row(db, "x-plain")
    receipt = _attest(db)
    assert receipt["row_count"] == 2
    assert _check_code(db, "x-plain") is None
    # The long row itself still fails closed: OD-39's revision (read first for any pre-stamp AI-chat row) is a
    # capped digest, and the message family refuses content over 100,000 characters anyway.
    assert _posture_code(db, "x-long") == "json_size"


# --- 6. the owner socket routes ------------------------------------------------------------------------

@pytest.fixture()
def owner_app(db, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from topos.api import permissions_capture_receipts, permissions_source_installs
    from topos.config.settings import settings as runtime_settings

    runtime = SimpleNamespace(protocol=SimpleNamespace(canonical_database=db.path,
        ledger=SimpleNamespace(identity=SimpleNamespace(owner_id=OWNER))))
    monkeypatch.setattr("topos.permissions_v2.runtime.get_runtime", lambda: runtime)
    monkeypatch.setattr(runtime_settings, "topos_owner_key", "owner-key", raising=False)
    app = FastAPI()
    app.include_router(permissions_source_installs.router)
    app.include_router(permissions_capture_receipts.router)
    return app


async def _call(app, method: str, path: str, *, socket: bool = True, **kwargs):
    import httpx
    from topos.uds import UDSChannelApp

    transport = httpx.ASGITransport(app=UDSChannelApp(app) if socket else app)
    headers = {} if socket else {"Authorization": "Bearer owner-key"}
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        return await client.request(method, f"/v1/permissions-beta/v2{path}", headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_the_install_routes_are_the_owners_socket_only_and_dry_run_first(db, owner_app):
    _two_installs(db)
    _export_row(db, "x-route")
    db.commit()

    assert (await _call(owner_app, "GET", "/source-installs", socket=False, params={"source_id": EXPORT})
            ).status_code == 403
    tcp = await _call(owner_app, "POST", "/source-installs/deactivate", socket=False,
                      json={"source_id": EXPORT, "install_id": "install-1", "dry_run": False, "confirm": True})
    assert tcp.status_code == 403

    listed = await _call(owner_app, "GET", "/source-installs", params={"source_id": EXPORT})
    assert listed.status_code == 200 and listed.json()["active_count"] == 2
    assert listed.headers["cache-control"] == "no-store"
    assert PROMPT not in listed.text

    dry = await _call(owner_app, "POST", "/source-installs/deactivate",
                      json={"source_id": EXPORT, "install_id": "install-1"})
    assert dry.status_code == 200 and dry.json()["dry_run"] is True and "_scope_key" not in dry.json()
    assert (await _call(owner_app, "GET", "/source-installs", params={"source_id": EXPORT})).json()["active_count"] == 2

    unconfirmed = await _call(owner_app, "POST", "/source-installs/deactivate",
                              json={"source_id": EXPORT, "install_id": "install-1", "dry_run": False})
    assert (unconfirmed.status_code, unconfirmed.json()["detail"]) == (400, "install_deactivation_unconfirmed")
    real = await _call(owner_app, "POST", "/source-installs/deactivate",
                       json={"source_id": EXPORT, "install_id": "install-1", "dry_run": False, "confirm": True})
    assert real.status_code == 200 and real.json()["deactivated"] is True
    last = await _call(owner_app, "POST", "/source-installs/deactivate",
                       json={"source_id": EXPORT, "install_id": "install-0"})
    assert (last.status_code, last.json()["detail"]) == (409, "install_last_active")

    # Then the owner's receipt over the import, through the generalised capture-attestation route.
    body = {"table": TABLE, "source_id": EXPORT, "app_id": IMPORT_APP}
    preview = (await _call(owner_app, "POST", "/capture-attestation/preview", json=body)).json()
    assert (preview["row_count"], preview["dataset_certified"]) == (1, True) and "x-route" not in str(preview)
    attested = await _call(owner_app, "POST", "/capture-attestation/attest",
                           json={**body, "preview_digest": preview["preview_digest"], "confirm": True})
    assert attested.status_code == 200, attested.text
    assert _check_code(db, "x-route") is None


# --- 7. install refuses to make a second active install again ------------------------------------------

@pytest.fixture()
def install_db(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from topos.sources import install_service

    conn = sqlite3.connect(tmp_path / "installs.db")

    @contextmanager
    def _fake_db_conn():
        yield conn

    monkeypatch.setattr(install_service, "_db_conn", _fake_db_conn)
    monkeypatch.setattr(install_service.settings, "topos_database_mode", "local")
    monkeypatch.setattr(install_service, "_ACTIVE_HANDLES", {})
    monkeypatch.setattr(install_service, "install_source_definition", lambda definition: object())
    install_service.ensure_install_schema()
    yield conn
    conn.close()


def _scope(dataset: str, *, user: str = OWNER, topos: str = "resource-1", device: str = "*") -> Dict[str, str]:
    return {"user_id": user, "device_id": device, "topos_id": topos, "dataset_id": dataset}


def _install_via_service(scope, *, version: str = "v1"):
    from topos.sources import install_service
    from topos.sources.registry import BUNDLED_REGISTRY

    definition = json.loads(json.dumps(BUNDLED_REGISTRY[EXPORT].to_dict()))
    return install_service.install_source(source_definition_json=definition, version_id=version, scope=scope)


def _active(conn) -> list:
    return [tuple(r) for r in conn.execute("SELECT install_id, scope_key FROM source_runtime_installs "
                                           "WHERE source_id=? AND is_active=1", (EXPORT,))]


def test_install_refuses_a_second_active_install_for_the_same_owner(install_db):
    from topos.sources import install_service

    first = _install_via_service(_scope(DATASET))
    # A reinstall in the same scope still replaces the active row.
    again = _install_via_service(_scope(DATASET), version="v2")
    assert [row[0] for row in _active(install_db)] == [again.install_id] != [first.install_id]
    # Another dataset, topos or device for the same owner would be a second live install: refused.
    for scope in (_scope(OTHER_DATASET), _scope(DATASET, topos="resource-2"), _scope(DATASET, device="mac-1")):
        with pytest.raises(install_service.SourceActiveInAnotherScope, match="source_active_in_another_scope"):
            _install_via_service(scope)
        assert [row[0] for row in _active(install_db)] == [again.install_id]
    # Another owner on a shared node is not this owner's scope.
    other = _install_via_service(_scope(f"{OTHER_OWNER}:topos:default", user=OTHER_OWNER))
    assert {row[0] for row in _active(install_db)} == {again.install_id, other.install_id}


def test_install_retires_the_legacy_scope_row_it_supersedes_instead_of_refusing(install_db):
    legacy = _install_via_service(_scope(f"{OWNER}:default:0123456789abcdef"))
    canonical = _install_via_service(_scope(f"{OWNER}:topos:resource-1"))
    assert [row[0] for row in _active(install_db)] == [canonical.install_id]
    status, reason = install_db.execute("SELECT status, failure_reason FROM source_runtime_installs "
                                        "WHERE install_id=?", (legacy.install_id,)).fetchone()
    assert status == "superseded" and "legacy dataset scope superseded" in reason
