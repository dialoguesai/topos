"""The owner's export receipt names the install the import came through, so two live installs need no retirement.

protects: on one owner's node the ChatGPT export source (``chatgpt_file_ingestion``) has two live installs, each
scoped to its own dataset: the 31 Aug one on this node's topos, declaring ``mixed``, and a 9 Sep one on another
node's topos, declaring nothing. The export's pre-stamp rows carry no writer and no dataset, so the install record
cannot certify one by elimination (``ai_chat_capture.install_dataset``), and ``evidence._source_posture`` refuses
a source with two live installs: all 336 in-window rows were withheld as ``source_posture_unknown``. Retiring an
install would move the ingest source clock, stale every native iMessage proof and force a refresh that deletes old
links. Instead the receipt NAMES the dataset (``capture_receipts.named_install``): only one of the source's own
live installs for this owner, on this node, declaring its posture; never an arbitrary id. It certifies exactly
the rows it lists at their current words, posture resolves from that install alone, nothing writes an install
row, and every other check (the parent conversation, the owner, role, revocation, edits) still runs.
"""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from tests.ingestion.test_ai_chat_writer_class import OWNER
from tests.permissions_v2.test_ai_chat_capture_provenance import (  # noqa: F401 (fixture)
    OTHER_OWNER, _identity, _resolver, db)
from tests.permissions_v2.test_ai_chat_dataset_binding import DATASET, OTHER_DATASET, RESOURCE
from tests.permissions_v2.test_ai_chat_export_grants import (  # noqa: F401 (fixture)
    EXPORT, IMPORT_APP, PROMPT, REPLY, TABLE, _call, _check_code, _deactivate, _export_row, _load_code, _posture_code,
    _row, owner_app)
from topos.permissions_v2 import ai_chat_capture
from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2.canonical import PolicyError, Rows, digest_stream
from topos.permissions_v2.evidence import _source_posture

THIS_NODE = RESOURCE         # the resolver binding's resource id: this node's own topos
OTHER_NODE = "resource-2"    # another node or device of the same owner


def _install(db, install_id: str, *, dataset: str, posture=None, topos: str = THIS_NODE, user: str = OWNER,
             device: str = "*", source: str = EXPORT, active: int = 1, status: str = "active",
             created: str = "2026-08-31T12:00:00+00:00") -> None:
    """One source_runtime_installs row, as the install service writes it."""
    db.execute("""CREATE TABLE IF NOT EXISTS source_runtime_installs (
        install_id TEXT PRIMARY KEY, scope_key TEXT, source_id TEXT, version_id TEXT, status TEXT, is_active INTEGER,
        source_definition_json TEXT, source_version_row_json TEXT, failure_reason TEXT, created_at TEXT, updated_at TEXT)""")
    scope = json.dumps({"user_id": user, "topos_id": topos, "device_id": device, "dataset_id": dataset})
    definition = json.dumps({"source_id": source, **({"posture": posture} if posture is not None else {})})
    db.execute("INSERT INTO source_runtime_installs (install_id, scope_key, source_id, version_id, status, is_active, "
               "source_definition_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (install_id, scope, source, "v1", status, active, definition, created, created))
    db.commit()


def _owners_node(db, *, topos: str = OTHER_NODE, posture=None, device: str = "*") -> None:
    """The measured node: the 31 Aug install (this node, ``mixed``) and the 9 Sep one (by default another node's,
    declaring nothing), each on its own dataset, both live."""
    _install(db, "install-31aug", dataset=DATASET, posture="mixed")
    _install(db, "install-9sep", dataset=OTHER_DATASET, posture=posture, topos=topos, device=device,
             created="2026-09-09T12:00:00+00:00")


def _export(db) -> None:
    """A prompt and its reply, imported before writer classes existed (no writer, no dataset)."""
    _export_row(db, "x-prompt")
    _export_row(db, "x-reply", content=REPLY, role="assistant")


def _preview(db, dataset=None, *, owner: str = OWNER, resource=THIS_NODE) -> Dict[str, Any]:
    return cr.preview(db, owner_id=owner, table=TABLE, source_id=EXPORT, app_id=IMPORT_APP, dataset_id=dataset,
                      resource_id=resource)


def _attest(db, dataset=DATASET, *, owner: str = OWNER, resource=THIS_NODE) -> Dict[str, Any]:
    preview = _preview(db, dataset, owner=owner, resource=resource)
    receipt = cr.attest(db, owner_id=owner, table=TABLE, source_id=EXPORT, app_id=IMPORT_APP,
                        preview_digest=preview["preview_digest"], confirm=True, dataset_id=dataset,
                        resource_id=resource)
    db.commit()
    return receipt


def _refused(call) -> str | None:
    try:
        call()
    except PolicyError as exc:
        return exc.code
    return None


def _posture(db, message_id: str):
    """The effective posture the permissions reader resolves for this row, or its refusal code."""
    resolver = _resolver(db)
    try:
        return _source_posture(db, _identity(resolver, message_id, EXPORT))[0]
    except PolicyError as exc:
        return exc.code


def _proven(db, message_id: str, owner: str = OWNER) -> bool:
    return ai_chat_capture.capture_proven(db, owner_id=owner, identity_source_id=EXPORT, row=_row(db, message_id))


def _receipt_count(db) -> int:
    return len(cr.receipts(db, owner_id=OWNER)) + len(cr.receipts(db, owner_id=OTHER_OWNER))


# --- 1. the preview: each install, its posture, and what naming it would cover ------------------------------

def test_the_preview_lists_each_install_with_its_posture_and_what_naming_it_would_cover(db):
    _owners_node(db)
    _export(db)
    unnamed = _preview(db)
    # No install binds the source by elimination: as before, nothing is certified and nothing listed.
    assert (unnamed["version"], unnamed["dataset_certified"], unnamed["row_count"]) == (cr.VERSION, False, 0)
    assert [(c["install_id"], c["dataset_id"], c["declared_posture"], c["nameable"], c["refusal"], c["row_count"])
            for c in unnamed["candidates"]] == [
        ("install-31aug", DATASET, "mixed", True, None, 2),
        ("install-9sep", OTHER_DATASET, None, False, "capture_attestation_dataset_not_this_node", 0)]
    named = _preview(db, DATASET)
    assert (named["version"], named["dataset_certified"], named["dataset_id"], named["row_count"]) == (
        cr.NAMED_VERSION, True, DATASET, 2)
    assert named["statement"] == cr.family_of(TABLE).named_statement
    for answer in (unnamed, named):   # counts, ids of installs, dates and postures; never a row or its words
        assert not {"x-prompt", "x-reply", PROMPT, REPLY} & set(json.dumps(answer).split('"'))


# --- 2. naming the 31 Aug install's dataset proves the rows and resolves its posture ---------------------------

def test_a_receipt_naming_the_mixed_installs_dataset_proves_the_prompt_and_resolves_mixed(db):
    _owners_node(db)
    _export(db)
    # A door-stamped row (other words: an identical text would be a copy, which withholds both).
    _export_row(db, "x-door", content="Which tide pools are safe at low tide?", writer="owner_import", dataset=DATASET)
    assert _posture(db, "x-prompt") == _posture(db, "x-reply") == "source_posture_unknown"
    assert not _proven(db, "x-prompt")

    receipt = _attest(db)
    assert (receipt["version"], receipt["dataset_id"], receipt["row_count"], receipt["dataset_certified"]) == (
        cr.NAMED_VERSION, DATASET, 2, True)
    assert _posture(db, "x-prompt") == _posture(db, "x-reply") == "mixed"
    assert _check_code(db, "x-prompt") is None                        # provenance, authorship, posture, role, content
    assert _proven(db, "x-prompt")
    # The reply's posture resolves (a fact citing it can be judged), but it is never the owner's words.
    assert _load_code(db, "x-reply") is None and not _proven(db, "x-reply")
    assert _check_code(db, "x-reply") in {"native_owner_provenance_unavailable", "not_owner_authored"}
    for message_id in ("x-prompt", "x-reply"):
        row = _row(db, message_id)
        assert cr.named_dataset(db, owner_id=OWNER, table=TABLE, row=row) == DATASET
        assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=row) == DATASET
    # The receipt claims only what it lists: the stamped row is neither listed nor named, and stays refused.
    assert cr.named_dataset(db, owner_id=OWNER, table=TABLE, row=_row(db, "x-door")) is None
    assert _posture(db, "x-door") == "source_posture_unknown"
    # The batch rule agrees with the row rule, row for row.
    rows = [_row(db, m) for m in ("x-prompt", "x-reply", "x-door")]
    assert cr.proven_rows(db, owner_id=OWNER, table=TABLE, source_id=EXPORT, rows=rows) == frozenset(
        r["message_id"] for r in rows if cr.proven(db, owner_id=OWNER, table=TABLE, identity_source_id=EXPORT, row=r))
    assert cr.proven_rows(db, owner_id=OWNER, table=TABLE, source_id=EXPORT, rows=rows) == {"x-prompt", "x-reply"}


# --- 3. an install that cannot carry the rows refuses ----------------------------------------------------------

@pytest.mark.parametrize("topos, posture, device, code", [
    (OTHER_NODE, None, "*", "capture_attestation_dataset_not_this_node"),     # the measured 9 Sep install
    (OTHER_NODE, "mixed", "*", "capture_attestation_dataset_not_this_node"),  # another node's, whatever it declares
    (THIS_NODE, None, "*", "capture_attestation_dataset_posture_unknown"),    # this node's, declaring nothing
    (THIS_NODE, "unknown", "*", "capture_attestation_dataset_posture_unknown"),
    (THIS_NODE, "mixed", "mac-1", "capture_attestation_dataset_unknown"),     # one device's install never binds
])
def test_naming_an_install_that_cannot_carry_the_rows_refuses(db, topos, posture, device, code):
    _owners_node(db, topos=topos, posture=posture, device=device)
    _export(db)
    assert _refused(lambda: _preview(db, OTHER_DATASET)) == code
    digest = _preview(db, DATASET)["preview_digest"]   # even a digest the owner holds cannot carry it over
    assert _refused(lambda: cr.attest(db, owner_id=OWNER, table=TABLE, source_id=EXPORT, app_id=IMPORT_APP,
                                      preview_digest=digest, confirm=True, dataset_id=OTHER_DATASET,
                                      resource_id=THIS_NODE)) == code
    assert _receipt_count(db) == 0
    assert _posture(db, "x-prompt") == "source_posture_unknown" and not _proven(db, "x-prompt")


def test_a_node_whose_own_topos_is_unknown_names_nothing(db):
    _owners_node(db)
    _export(db)
    assert _refused(lambda: _preview(db, DATASET, resource=None)) == "capture_attestation_dataset_not_this_node"
    assert [c["nameable"] for c in _preview(db, resource=None)["candidates"]] == [False, False]


# --- 4. a dataset that is not one of the source's own installs refuses ------------------------------------------

@pytest.mark.parametrize("case", ["invented", "retired", "another_source", "another_owner", "two_installs_on_it"])
def test_naming_a_dataset_that_is_not_one_of_the_sources_live_installs_refuses(db, case):
    _owners_node(db)
    _export(db)
    named = {"invented": f"{OWNER}:topos:invented", "retired": f"{OWNER}:topos:retired",
             "another_source": f"{OWNER}:topos:capture", "another_owner": f"{OTHER_OWNER}:topos:default",
             "two_installs_on_it": DATASET}[case]
    if case == "retired":
        _install(db, "install-old", dataset=named, posture="mixed", active=0, status="superseded")
    elif case == "another_source":
        _install(db, "install-capture", dataset=named, posture="mixed", source="chatgpt_ui_conversation")
    elif case == "another_owner":
        _install(db, "install-theirs", dataset=named, posture="mixed", user=OTHER_OWNER)
    elif case == "two_installs_on_it":
        _install(db, "install-31aug-again", dataset=DATASET, posture="mixed")
    assert _refused(lambda: _preview(db, named)) == "capture_attestation_dataset_unknown"
    assert _refused(lambda: _attest(db, named)) == "capture_attestation_dataset_unknown"
    assert _receipt_count(db) == 0


@pytest.mark.parametrize("named", ["*", "", " padded ", 7, ["x"]])
def test_a_malformed_name_is_refused_before_anything_is_read(db, named):
    _owners_node(db)
    assert _refused(lambda: _preview(db, named)) == "capture_attestation_invalid"


def test_the_journal_family_never_names_a_dataset(db):
    # Journals and browsing keep their one-install rule: their receipts take no dataset.
    for table, source, app in (("journal_entries", "grow_journal", "app"), ("activity_events", "browser_visits", "x")):
        assert _refused(lambda: cr.preview(db, owner_id=OWNER, table=table, source_id=source, app_id=app,
                                           dataset_id=DATASET, resource_id=THIS_NODE)) == "capture_attestation_invalid"
        assert "candidates" not in cr.preview(db, owner_id=OWNER, table=table, source_id=source, app_id=app)


# --- 5. an edited row, a revoked receipt, an install that stops carrying the rows -------------------------------

def test_an_edited_row_stops_being_proven_and_a_new_preview_covers_only_its_new_words(db):
    _owners_node(db)
    _export(db)
    _attest(db)
    for column, value in (("content", PROMPT + " And a quiz."), ("sender_type", "human"),
                          ("conversation_id", "chatgpt:thread-2")):
        before = _row(db, "x-prompt")[column]
        if column == "conversation_id":
            db.execute("INSERT OR IGNORE INTO ai_chat_conversations (conversation_id, owner_user_id, title, "
                       "source_id, created_at, updated_at) VALUES (?,?,NULL,?,'2026-09-01','2026-09-01')",
                       (value, OWNER, EXPORT))
        db.execute(f"UPDATE ai_chat_messages SET {column}=? WHERE message_id='x-prompt'", (value,))
        db.commit()
        assert _posture(db, "x-prompt") == "source_posture_unknown", column
        assert not _proven(db, "x-prompt"), column
        assert _preview(db, DATASET)["row_count"] == 1, column     # only the new words are eligible
        db.execute(f"UPDATE ai_chat_messages SET {column}=? WHERE message_id='x-prompt'", (before,))
        db.commit()
        assert _check_code(db, "x-prompt") is None, column
    assert _preview(db, DATASET)["row_count"] == 0


def test_revoking_the_named_receipt_removes_the_proof(db):
    _owners_node(db)
    _export(db)
    receipt = _attest(db)
    assert _check_code(db, "x-prompt") is None
    cr.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    db.commit()
    assert _posture(db, "x-prompt") == _posture(db, "x-reply") == "source_posture_unknown"
    assert not _proven(db, "x-prompt")
    assert cr.proven_rows(db, owner_id=OWNER, table=TABLE, source_id=EXPORT,
                          rows=[_row(db, "x-prompt"), _row(db, "x-reply")]) == frozenset()
    assert _preview(db, DATASET)["row_count"] == 2    # the owner may attest again, never edit the old receipt


@pytest.mark.parametrize("change", ["drops_its_posture", "drops_its_posture_alone", "is_retired",
                                    "becomes_a_wildcard", "moves_to_another_node"])
def test_the_rows_count_only_while_the_named_install_still_carries_them(db, change):
    _owners_node(db)
    _export(db)
    _attest(db)
    assert _check_code(db, "x-prompt") is None
    if change == "drops_its_posture_alone":
        # Even as the source's only live install, it must declare: a default never stands in for the named one.
        db.execute("UPDATE source_runtime_installs SET is_active=0, status='superseded' "
                   "WHERE install_id='install-9sep'")
    if change.startswith("drops_its_posture"):      # a same-scope reinstall whose definition declares nothing
        db.execute("UPDATE source_runtime_installs SET source_definition_json=? WHERE install_id='install-31aug'",
                   (json.dumps({"source_id": EXPORT}),))
    elif change == "is_retired":           # the remaining live install is another dataset's: never borrowed
        db.execute("UPDATE source_runtime_installs SET is_active=0, status='superseded' "
                   "WHERE install_id='install-31aug'")
    elif change == "becomes_a_wildcard":   # an install on every dataset is not the one the owner named
        db.execute("UPDATE source_runtime_installs SET scope_key=? WHERE install_id='install-31aug'",
                   (json.dumps({"user_id": OWNER, "topos_id": THIS_NODE, "device_id": "*", "dataset_id": "*"}),))
    elif change == "moves_to_another_node":
        db.execute("UPDATE source_runtime_installs SET scope_key=? WHERE install_id='install-31aug'",
                   (json.dumps({"user_id": OWNER, "topos_id": OTHER_NODE, "device_id": "*", "dataset_id": DATASET}),))
    db.commit()
    assert _posture(db, "x-prompt") == _posture(db, "x-reply") == "source_posture_unknown"
    if change != "moves_to_another_node":
        # Moving the install to another node is the evidence binding's to refuse, on every read.
        assert not _proven(db, "x-prompt")


# --- 6. nothing moves the ingest source clock --------------------------------------------------------------------

def _ingest_source_clock(db):
    """The node's own ingest source clock (source clock v2) over the source tables this node has."""
    from topos.permissions_v2.ingest_provenance import IngestProvenanceService
    schema = IngestProvenanceService._schema(None, db)
    db.execute(schema["ingest_provenance_state"])
    db.execute("INSERT INTO ingest_provenance_state VALUES (1, 'store-test', '{}', 'file-test', 0)")
    triggers = [sql for sql in schema.values() if sql.startswith("CREATE TRIGGER")]
    assert any(" ON source_runtime_installs " in sql for sql in triggers)
    for sql in triggers:
        db.execute(sql)
    db.commit()
    return lambda: db.execute("SELECT generation FROM ingest_provenance_state").fetchone()[0]


def test_the_receipt_writes_no_install_row_and_the_ingest_source_clock_does_not_move(db):
    _owners_node(db)
    _export(db)
    generation = _ingest_source_clock(db)
    installs = lambda: [tuple(r) for r in db.execute("SELECT * FROM source_runtime_installs ORDER BY install_id")]
    before = installs()
    _preview(db)
    receipt = _attest(db)
    assert _posture(db, "x-prompt") == _posture(db, "x-reply") == "mixed" and _proven(db, "x-prompt")
    cr.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    db.commit()
    _attest(db)
    assert _load_code(db, "x-prompt") is None
    assert installs() == before and generation() == 0
    # The clock is live: an install change (what a retirement does) moves it.
    db.execute("UPDATE source_runtime_installs SET status='superseded', is_active=0 WHERE install_id='install-9sep'")
    db.commit()
    assert generation() == 1


# --- 7. one row, one dataset; nobody else's word ---------------------------------------------------------------

def test_a_row_is_never_certified_to_two_datasets(db):
    _owners_node(db, topos=THIS_NODE, posture="personal")   # the other install could be named too
    _export(db)
    _attest(db, DATASET)
    # The same words cannot be listed again under the other dataset while the first receipt is live.
    other = _attest(db, OTHER_DATASET)
    assert other["row_count"] == 0
    assert _posture(db, "x-prompt") == "mixed"             # the first naming's install, never the other's
    # Were two live receipts ever to name two datasets for the same words, the row certifies nothing.
    row = _row(db, "x-prompt")
    db.execute("INSERT INTO capture_receipt_rows (receipt_id, canonical_table, record_id, content_revision) "
               "VALUES (?,?,?,?)", (other["receipt_id"], TABLE, "x-prompt", cr.content_revision(TABLE, row)))
    db.commit()
    assert cr.named_dataset(db, owner_id=OWNER, table=TABLE, row=row) is None
    assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=row) is None
    assert _posture(db, "x-prompt") == "source_posture_unknown" and not _proven(db, "x-prompt")


def test_a_capture_receipt_that_names_no_dataset_for_the_same_words_leaves_the_row_uncertified(db):
    # OD-39's receipts and the import's must agree on one dataset (ai_chat_capture.certified_dataset): here an
    # OD-39 receipt over the export source, taken while no install could certify one, names none.
    _owners_node(db)
    _export(db)
    _attest(db)
    preview = ai_chat_capture.preview(db, owner_id=OWNER, source_id=EXPORT, app_id="some-capture-app")
    ai_chat_capture.attest(db, owner_id=OWNER, source_id=EXPORT, app_id="some-capture-app",
                           preview_digest=preview["preview_digest"], confirm=True)
    db.commit()
    assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=_row(db, "x-prompt")) is None
    assert _posture(db, "x-prompt") == "source_posture_unknown"


def test_the_parent_conversation_and_the_owner_still_decide(db):
    _owners_node(db)
    _export(db)
    _export_row(db, "x-foreign", content="A prompt in another owner's conversation.", owner=OTHER_OWNER,
                conversation="chatgpt:thread-9")
    _install(db, "install-theirs", dataset=f"{OTHER_OWNER}:topos:default", posture="mixed", user=OTHER_OWNER)
    # Another owner's named receipt (it lists every pre-stamp row of the source) proves nothing for this owner.
    _attest(db, f"{OTHER_OWNER}:topos:default", owner=OTHER_OWNER)
    assert _posture(db, "x-prompt") == "source_posture_unknown" and not _proven(db, "x-prompt")
    # This owner's receipt lists the foreign row too, and the parent rule still refuses it.
    _attest(db)
    assert _check_code(db, "x-prompt") is None
    assert _load_code(db, "x-foreign") == "evidence_owner_binding" and not _proven(db, "x-foreign")


def test_an_override_on_the_named_dataset_applies_and_an_ambient_one_anywhere_vetoes(db):
    _owners_node(db)
    _export(db)
    _attest(db)
    db.execute("CREATE TABLE user_ingestion_sources (dataset_id TEXT, source_id TEXT, posture TEXT)")
    for dataset, posture, expected in ((DATASET, "personal", "personal"), (OTHER_DATASET, "personal", "mixed"),
                                       (OTHER_DATASET, "ambient", "ambient")):
        db.execute("DELETE FROM user_ingestion_sources")
        db.execute("INSERT INTO user_ingestion_sources VALUES (?,?,?)", (dataset, EXPORT, posture))
        db.commit()
        assert _posture(db, "x-prompt") == expected, (dataset, posture)
    # The owner's own words under an ambient source are observed, never authored.
    assert _check_code(db, "x-prompt") == "not_owner_authored"


# --- 8. the elimination path and its digest are unchanged ---------------------------------------------------------

def test_the_unnamed_receipt_is_byte_for_byte_what_it_was(db):
    family = cr.family_of(TABLE)
    rows = [("x-1", "a" * 64)]
    assert cr._summary(OWNER, family, EXPORT, IMPORT_APP, rows, DATASET)["preview_digest"] == digest_stream(
        {"version": cr.VERSION, "owner_id": OWNER, "table": TABLE, "source_id": EXPORT, "app_id": IMPORT_APP,
         "dataset_id": DATASET, "rows": Rows(rows)})
    # A named digest binds the naming: it is never the unnamed one, even over the same rows and dataset.
    assert cr._summary(OWNER, family, EXPORT, IMPORT_APP, rows, DATASET, named=True)["preview_digest"] != \
        cr._summary(OWNER, family, EXPORT, IMPORT_APP, rows, DATASET)["preview_digest"]
    _install(db, "install-only", dataset=DATASET)   # one install, declaring nothing: elimination as before
    _export(db)
    unnamed = _preview(db)
    assert (unnamed["dataset_certified"], unnamed["row_count"]) == (True, 2)
    receipt = cr.attest(db, owner_id=OWNER, table=TABLE, source_id=EXPORT, app_id=IMPORT_APP,
                        preview_digest=unnamed["preview_digest"], confirm=True)
    db.commit()
    assert receipt["version"] == cr.VERSION and "dataset_id" not in receipt
    assert _check_code(db, "x-prompt") is None
    assert cr.named_dataset(db, owner_id=OWNER, table=TABLE, row=_row(db, "x-prompt")) is None


# --- 9. the owner's socket ----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_owner_socket_routes_take_the_named_dataset(db, owner_app):
    _owners_node(db)
    _export(db)
    body = {"table": TABLE, "source_id": EXPORT, "app_id": IMPORT_APP}
    listed = (await _call(owner_app, "POST", "/capture-attestation/preview", json=body)).json()
    assert [c["nameable"] for c in listed["candidates"]] == [True, False]
    assert (await _call(owner_app, "POST", "/capture-attestation/preview", socket=False,
                        json={**body, "dataset_id": DATASET})).status_code == 403
    for dataset, code in ((OTHER_DATASET, "capture_attestation_dataset_not_this_node"),
                          (f"{OWNER}:topos:invented", "capture_attestation_dataset_unknown")):
        refused = await _call(owner_app, "POST", "/capture-attestation/preview", json={**body, "dataset_id": dataset})
        assert (refused.status_code, refused.json()["detail"]) == (400, code)
    preview = await _call(owner_app, "POST", "/capture-attestation/preview", json={**body, "dataset_id": DATASET})
    assert preview.status_code == 200 and preview.json()["dataset_certified"] is True
    assert "x-prompt" not in preview.text and PROMPT not in preview.text
    unnamed_digest = listed["preview_digest"]
    stale = await _call(owner_app, "POST", "/capture-attestation/attest",
                        json={**body, "dataset_id": DATASET, "preview_digest": unnamed_digest, "confirm": True})
    assert (stale.status_code, stale.json()["detail"]) == (409, "capture_attestation_preview_stale")
    attested = await _call(owner_app, "POST", "/capture-attestation/attest", json={
        **body, "dataset_id": DATASET, "preview_digest": preview.json()["preview_digest"], "confirm": True})
    assert attested.status_code == 200, attested.text
    assert attested.json()["version"] == cr.NAMED_VERSION and attested.json()["row_count"] == 2
    assert _check_code(db, "x-prompt") is None


# --- 10. the retirement dry run projects posture only for an install on this node ----------------------------------

@pytest.mark.parametrize("retired, kept_dataset, resolvable", [
    ("install-31aug", OTHER_DATASET, False),   # what stays is another node's install: the reader refuses it
    ("install-9sep", DATASET, True),
])
def test_the_dry_run_projects_posture_the_way_the_reader_resolves_it(db, retired, kept_dataset, resolvable):
    _owners_node(db)
    assert _deactivate(db, retired, dry_run=True)["posture_resolvable_after"] is resolvable
    _deactivate(db, retired, dry_run=False)
    # The reader agrees: a row the import door wrote into the kept install's dataset.
    _export_row(db, "x-stamped", writer="owner_import", dataset=kept_dataset)
    assert (_posture_code(db, "x-stamped") is None) is resolvable


def test_the_dry_run_never_projects_a_device_install_or_an_unknown_node(db):
    from topos.sources import install_maintenance
    _owners_node(db, topos=THIS_NODE, device="mac-1")
    assert _deactivate(db, "install-31aug", dry_run=True)["posture_resolvable_after"] is False
    db.execute("BEGIN IMMEDIATE")
    try:
        unknown = install_maintenance.deactivate(db, owner_id=OWNER, source_id=EXPORT, install_id="install-9sep")
    finally:
        db.rollback()
    assert unknown["posture_resolvable_after"] is False   # without this node's topos, nothing is projected
