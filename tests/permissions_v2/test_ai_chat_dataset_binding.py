"""RD5: an AI-chat row resolves its source posture through the dataset the node saw it come in through.

protects: an AI-chat evidence identity carries no dataset, so a source whose one
active install is scoped to a dataset (every install on a current node) never
resolved its posture: ``_source_posture`` compared the scope's dataset with None
and refused every row with ``source_posture_unknown``, before provenance was even
asked. The binding must come from the node's own record of the write (the
dataset the door wrote into, or an owner receipt certified from the install
record), never from the row's payload; a row whose dataset cannot be proven keeps
failing, and nothing it unlocks may reach past authorship or any later check.
"""
from __future__ import annotations

import json

import pytest

from tests.ingestion.test_ai_chat_writer_class import (
    DATASET, GRANTEE, OWNER, SOURCE, _app_ingest, _chat_record, _relay, _row, _stamp)
from tests.permissions_v2.test_ai_chat_capture_provenance import (  # noqa: F401 (fixture)
    EXTENSION_APP, OTHER_OWNER, PROMPT, _attest, _capture, _identity, _pre_stamp, _resolver, _source_check_code, db)
from topos.permissions_v2 import ai_chat_capture
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import _source_posture
from topos.principal import OWNER_APP

OTHER_DATASET = f"{OWNER}:topos:second"
RESOURCE = "resource-1"  # the resolver binding's resource id (test_ai_chat_capture_provenance._binding)


def _install(db, *, source: str = SOURCE, dataset=DATASET, user: str = OWNER, active: int = 1,
             status: str = "active", scope="scoped", posture: str | None = None) -> None:
    """One source_runtime_installs row, as the install service writes it (live schema)."""
    db.execute("""CREATE TABLE IF NOT EXISTS source_runtime_installs (
        install_id TEXT PRIMARY KEY, scope_key TEXT, source_id TEXT, version_id TEXT, status TEXT, is_active INTEGER,
        source_definition_json TEXT, source_version_row_json TEXT, failure_reason TEXT, created_at TEXT, updated_at TEXT)""")
    scope_key = (json.dumps({"user_id": user, "topos_id": RESOURCE, "device_id": "*", "dataset_id": dataset})
                 if scope == "scoped" else scope)
    definition = {"source_id": source, **({"posture": posture} if posture else {})}
    count = db.execute("SELECT COUNT(*) FROM source_runtime_installs").fetchone()[0]
    db.execute("INSERT INTO source_runtime_installs (install_id, scope_key, source_id, version_id, status, is_active, "
               "source_definition_json) VALUES (?,?,?,?,?,?,?)",
               (f"install-{count}", scope_key, source, "v1", status, active, json.dumps(definition)))
    db.commit()


def _posture_code(db, message_id: str, *, source: str = SOURCE):
    """None when the row's posture resolves, else the refusal code."""
    resolver = _resolver(db)
    try:
        _source_posture(db, _identity(resolver, message_id, source))
    except PolicyError as exc:
        return exc.code
    return None


def _load_code(db, message_id: str):
    resolver = _resolver(db)
    try:
        resolver._load(db, _identity(resolver, message_id))
    except PolicyError as exc:
        return exc.code
    return None


async def _capture_into(message_id: str, dataset: str) -> None:
    """The owner's stamped capture, sent by the CP into another dataset of the same owner."""
    message = _app_ingest(f"req-{message_id}-{dataset}", [_chat_record(message_id, PROMPT, role="user")],
                          requester=OWNER, app_id=EXTENSION_APP)
    message["payload"].update(dataset_id=dataset, resource_id=f"dataset:{OWNER}:{dataset}")
    _stamp(message, cls=OWNER_APP, client_id=EXTENSION_APP, acting_user=OWNER)
    result = await _relay(message)
    assert result["status"] == "ok", result


# --- 1. the door records the dataset it wrote into; the payload cannot ------------------------

@pytest.mark.asyncio
async def test_the_door_records_its_dataset_and_the_payload_cannot_name_one(db):
    await _capture("m-door", dataset_id=OTHER_DATASET, writer_dataset_id=OTHER_DATASET)
    row = _row(db, "m-door")
    assert (row["writer_class"], row["writer_app_id"], row["writer_dataset_id"]) == ("owner_app", EXTENSION_APP, DATASET)


# --- 2. a scoped install binds only its own dataset -------------------------------------------

@pytest.mark.asyncio
async def test_a_scoped_install_binds_its_own_dataset_and_the_capture_passes(db):
    _install(db)
    await _capture("m-bound")
    assert _posture_code(db, "m-bound") is None
    assert _load_code(db, "m-bound") is None
    assert _source_check_code(db, "m-bound") is None


@pytest.mark.asyncio
async def test_before_rd5_the_same_capture_was_posture_unknown(db):
    # The column is what binds it: a stamped row whose door recorded no dataset stays refused.
    _install(db)
    await _capture("m-unbound")
    db.execute("UPDATE ai_chat_messages SET writer_dataset_id=NULL WHERE message_id='m-unbound'")
    db.commit()
    assert _posture_code(db, "m-unbound") == "source_posture_unknown"
    assert _load_code(db, "m-unbound") == "source_posture_unknown"


# --- 3. a row from another dataset or install fails -------------------------------------------

@pytest.mark.asyncio
async def test_a_row_written_into_another_dataset_fails(db):
    _install(db)
    await _capture_into("m-elsewhere", OTHER_DATASET)
    assert _row(db, "m-elsewhere")["writer_dataset_id"] == OTHER_DATASET
    assert _posture_code(db, "m-elsewhere") == "source_posture_unknown"
    assert _load_code(db, "m-elsewhere") == "source_posture_unknown"


@pytest.mark.asyncio
async def test_another_sources_install_on_the_rows_dataset_does_not_bind_it(db):
    _install(db, source="chatgpt_file_ingestion", dataset=DATASET)
    _install(db, dataset=OTHER_DATASET)
    await _capture("m-borrowed")
    assert _posture_code(db, "m-borrowed") == "source_posture_unknown"


@pytest.mark.asyncio
async def test_a_reinstall_onto_another_dataset_unbinds_rows_written_through_the_first(db):
    _install(db, active=0, status="rolled_back")
    _install(db, dataset=OTHER_DATASET)
    await _capture("m-old-install")
    assert _posture_code(db, "m-old-install") == "source_posture_unknown"


@pytest.mark.asyncio
async def test_an_internal_replay_keeps_the_dataset_and_a_later_door_replaces_it(db):
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore

    _install(db)
    await _capture("m-moved")
    row = _row(db, "m-moved")
    replay = {key: row[key] for key in ("message_id", "conversation_id", "sender_type", "event_at", "content", "source_id")}
    # reprocess: no door, no class; a dataset the record names is not the door's either
    SQLiteCanonicalStore(db).upsert("ai_chat_messages", {**replay, "writer_dataset_id": OTHER_DATASET})
    db.commit()
    assert _row(db, "m-moved")["writer_dataset_id"] == DATASET
    assert _posture_code(db, "m-moved") is None
    await _capture_into("m-moved", OTHER_DATASET)
    assert _row(db, "m-moved")["writer_dataset_id"] == OTHER_DATASET
    assert _posture_code(db, "m-moved") == "source_posture_unknown"


# --- 4. pre-stamp rows: only the owner's receipt, certified from the install record -----------

@pytest.mark.asyncio
async def test_a_pre_stamp_row_binds_only_through_a_live_receipt_at_its_revision(db):
    _install(db)
    await _capture("m-old")
    _pre_stamp(db, "m-old")
    assert _posture_code(db, "m-old") == "source_posture_unknown"

    receipt = _attest(db)
    assert receipt["dataset_certified"] is True
    assert _posture_code(db, "m-old") is None
    assert _source_check_code(db, "m-old") is None

    db.execute("UPDATE ai_chat_messages SET content=? WHERE message_id='m-old'", (PROMPT + " Edited.",))
    db.commit()
    assert _posture_code(db, "m-old") == "source_posture_unknown"
    db.execute("UPDATE ai_chat_messages SET content=? WHERE message_id='m-old'", (PROMPT,))
    db.commit()
    assert _posture_code(db, "m-old") is None

    ai_chat_capture.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    db.commit()
    assert _posture_code(db, "m-old") == "source_posture_unknown"


@pytest.mark.parametrize("history", ["two_datasets", "wildcard", "unscoped", "no_active", "two_active", "bad_status"])
@pytest.mark.asyncio
async def test_an_install_record_that_cannot_name_one_dataset_certifies_none(db, history):
    if history == "two_datasets":
        _install(db, dataset=OTHER_DATASET, active=0, status="rolled_back")
        _install(db)
    elif history == "wildcard":
        _install(db, dataset="*")
    elif history == "unscoped":
        _install(db, scope=None, active=0, status="rolled_back")
        _install(db)
    elif history == "no_active":
        _install(db, active=0, status="rolled_back")
    elif history == "two_active":
        _install(db)
        _install(db)
    else:
        _install(db, status="failed")
    await _capture("m-unprovable")
    _pre_stamp(db, "m-unprovable")
    assert ai_chat_capture.install_dataset(db, owner_id=OWNER, source_id=SOURCE) is None
    assert _attest(db)["dataset_certified"] is False
    row = _row(db, "m-unprovable")
    assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=row) is None


@pytest.mark.asyncio
async def test_two_live_receipts_naming_different_datasets_certify_none(db):
    _install(db)
    await _capture("m-twice")
    _pre_stamp(db, "m-twice")
    first = _attest(db)
    # A receipt written around the door (the only way to get a second one for the same row) naming another dataset.
    db.execute(f"INSERT INTO {ai_chat_capture.RECEIPTS} VALUES ('forged', ?, ?, ?, ?, 's', 'd', 1, 1, NULL, ?)",
               (ai_chat_capture.VERSION, OWNER, SOURCE, EXTENSION_APP, OTHER_DATASET))
    db.execute(f"INSERT INTO {ai_chat_capture.RECEIPT_ROWS} SELECT 'forged', message_id, conversation_id, "
               f"content_revision FROM {ai_chat_capture.RECEIPT_ROWS} WHERE receipt_id=?", (first["receipt_id"],))
    db.commit()
    assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=_row(db, "m-twice")) is None
    assert _posture_code(db, "m-twice") == "source_posture_unknown"


def test_the_receipt_is_immutable_in_its_dataset(db):
    ai_chat_capture.install(db)
    db.execute(f"INSERT INTO {ai_chat_capture.RECEIPTS} VALUES ('r', ?, ?, ?, ?, 's', 'd', 0, 1, NULL, ?)",
               (ai_chat_capture.VERSION, OWNER, SOURCE, EXTENSION_APP, DATASET))
    with pytest.raises(Exception, match="capture_receipt_immutable"):
        db.execute(f"UPDATE {ai_chat_capture.RECEIPTS} SET dataset_id=? WHERE receipt_id='r'", (OTHER_DATASET,))


def test_a_v1_receipt_table_gains_the_dataset_and_its_rows_certify_none(db):
    receipts, rows = ai_chat_capture.RECEIPTS, ai_chat_capture.RECEIPT_ROWS
    db.execute(f"""CREATE TABLE {receipts} (receipt_id TEXT PRIMARY KEY, version TEXT NOT NULL, owner_id TEXT NOT NULL,
        source_id TEXT NOT NULL, app_id TEXT NOT NULL, statement TEXT NOT NULL, preview_digest TEXT NOT NULL,
        row_count INTEGER NOT NULL, attested_at INTEGER NOT NULL, revoked_at INTEGER)""")
    db.execute(f"""CREATE TRIGGER {receipts}_immutable BEFORE UPDATE OF receipt_id ON {receipts}
        BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END""")
    db.execute(f"INSERT INTO {receipts} VALUES ('old', 'v1', ?, ?, ?, 's', 'd', 0, 1, NULL)", (OWNER, SOURCE, EXTENSION_APP))
    ai_chat_capture.install(db)
    assert db.execute(f"SELECT dataset_id FROM {receipts} WHERE receipt_id='old'").fetchone()[0] is None
    with pytest.raises(Exception, match="capture_receipt_immutable"):
        db.execute(f"UPDATE {receipts} SET dataset_id=? WHERE receipt_id='old'", (DATASET,))
    assert ai_chat_capture.receipts(db, owner_id=OWNER)[0]["dataset_certified"] is False
    assert rows  # the row table is created alongside


# --- 5. a second owner stays isolated ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_second_owner_neither_certifies_nor_confuses_the_first(db):
    other_dataset = f"{OTHER_OWNER}:topos:default"
    _install(db, user=OTHER_OWNER, dataset=other_dataset, active=0, status="rolled_back")
    _install(db)
    await _capture("m-mine")
    _pre_stamp(db, "m-mine")
    # The other owner's install is not one this owner's capture could have gone through.
    assert ai_chat_capture.install_dataset(db, owner_id=OWNER, source_id=SOURCE) == DATASET
    assert ai_chat_capture.install_dataset(db, owner_id=OTHER_OWNER, source_id=SOURCE) is None
    # The other owner attesting covers none of this owner's rows, and certifies nothing for them.
    assert _attest(db, owner=OTHER_OWNER)["row_count"] == 0
    row = _row(db, "m-mine")
    assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=row) is None
    assert _posture_code(db, "m-mine") == "source_posture_unknown"
    _attest(db)
    assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=row) == DATASET
    assert ai_chat_capture.certified_dataset(db, owner_id=OTHER_OWNER, row=row) is None


@pytest.mark.asyncio
async def test_an_install_scoped_to_another_owner_never_binds_this_owners_row(db):
    _install(db, user=OTHER_OWNER)
    await _capture("m-foreign-install")
    assert _posture_code(db, "m-foreign-install") == "source_posture_unknown"


# --- 6. grantee and other-app writes still fail -----------------------------------------------

@pytest.mark.asyncio
async def test_a_grantee_write_gets_its_dataset_but_never_the_owners_words(db):
    _install(db)
    await _capture("m-grantee", requester=GRANTEE, app="grantee-app", stamped=False,
                   writer_class="owner_app", writer_app_id=EXTENSION_APP)
    row = _row(db, "m-grantee")
    assert (row["writer_class"], row["writer_dataset_id"]) == ("cp_relay", DATASET)
    assert _posture_code(db, "m-grantee") is None  # the CP authorised the write into this dataset
    assert _source_check_code(db, "m-grantee") == "native_owner_provenance_unavailable"


@pytest.mark.asyncio
async def test_another_owner_app_gets_its_dataset_but_never_the_owners_words(db):
    _install(db)
    await _capture("m-notes", app="owner-notes-app")
    assert _row(db, "m-notes")["writer_dataset_id"] == DATASET
    assert _posture_code(db, "m-notes") is None
    assert _source_check_code(db, "m-notes") == "native_owner_provenance_unavailable"


@pytest.mark.asyncio
async def test_an_attested_grantee_row_is_not_eligible_and_stays_unbound(db):
    # A stamped grantee row is never pre-stamp, so no owner receipt can cover it.
    _install(db)
    await _capture("m-grantee-2", requester=GRANTEE, app="grantee-app", stamped=False)
    db.execute("UPDATE ai_chat_messages SET writer_dataset_id=NULL WHERE message_id='m-grantee-2'")
    db.commit()
    assert _attest(db)["row_count"] == 0
    assert _posture_code(db, "m-grantee-2") == "source_posture_unknown"


# --- 7. posture resolves as it does for a conversation row -----------------------------------

@pytest.mark.asyncio
async def test_the_certified_datasets_own_ambient_override_still_vetoes(db):
    _install(db)
    await _capture("m-ambient")
    db.execute("CREATE TABLE IF NOT EXISTS user_ingestion_sources (dataset_id TEXT, source_id TEXT, enabled INTEGER, "
               "last_sync_at TEXT, last_error TEXT, updated_at TEXT, posture TEXT, exclude_spam INTEGER)")
    db.execute("INSERT INTO user_ingestion_sources (dataset_id, source_id, posture) VALUES (?,?, 'ambient')",
               (DATASET, SOURCE))
    db.commit()
    resolver = _resolver(db)
    assert _source_posture(db, _identity(resolver, "m-ambient"))[0] == "ambient"
    assert _source_check_code(db, "m-ambient") == "not_owner_authored"


@pytest.mark.asyncio
async def test_another_datasets_override_does_not_reach_a_certified_row(db):
    _install(db)
    await _capture("m-personal")
    db.execute("CREATE TABLE IF NOT EXISTS user_ingestion_sources (dataset_id TEXT, source_id TEXT, enabled INTEGER, "
               "last_sync_at TEXT, last_error TEXT, updated_at TEXT, posture TEXT, exclude_spam INTEGER)")
    db.execute("INSERT INTO user_ingestion_sources (dataset_id, source_id, posture) VALUES (?,?, 'ambient')",
               (OTHER_DATASET, SOURCE))
    db.commit()
    resolver = _resolver(db)
    assert _source_posture(db, _identity(resolver, "m-personal"))[0] != "ambient"
    # Uncertified, the same row keeps the datasetless rule: any ambient override of its source vetoes.
    db.execute("DELETE FROM source_runtime_installs")
    db.execute("UPDATE ai_chat_messages SET writer_dataset_id=NULL WHERE message_id='m-personal'")
    db.commit()
    assert _source_posture(db, _identity(resolver, "m-personal"))[0] == "ambient"


@pytest.mark.asyncio
async def test_the_certified_dataset_is_part_of_the_posture_revision(db):
    # Under a legacy node-local install (no scope column) both datasets resolve, so only the revision can tell
    # them apart: a review pinned to one dataset's posture must go stale when the row moves to another.
    db.execute("CREATE TABLE source_runtime_installs (source_id TEXT, is_active INTEGER, status TEXT, "
               "source_definition_json TEXT)")
    db.execute("INSERT INTO source_runtime_installs VALUES (?, 1, 'active', ?)", (SOURCE, json.dumps({"source_id": SOURCE})))
    db.commit()
    await _capture("m-rev")
    resolver = _resolver(db)
    first = _source_posture(db, _identity(resolver, "m-rev"))[1]
    await _capture_into("m-rev", OTHER_DATASET)
    second = _source_posture(db, _identity(resolver, "m-rev"))[1]
    db.execute("UPDATE ai_chat_messages SET writer_dataset_id=NULL WHERE message_id='m-rev'")
    db.commit()
    third = _source_posture(db, _identity(resolver, "m-rev"))[1]
    assert len({first, second, third}) == 3


@pytest.mark.asyncio
async def test_the_owner_confirms_the_dataset_a_preview_named_and_no_other(db):
    _install(db)
    await _capture("m-confirm")
    _pre_stamp(db, "m-confirm")
    preview = ai_chat_capture.preview(db, owner_id=OWNER, source_id=SOURCE, app_id=EXTENSION_APP)
    assert preview["dataset_certified"] is True
    _install(db, dataset=OTHER_DATASET, active=0, status="rolled_back")  # the install record changed underneath
    with pytest.raises(PolicyError, match="capture_attestation_preview_stale"):
        ai_chat_capture.attest(db, owner_id=OWNER, source_id=SOURCE, app_id=EXTENSION_APP,
                               preview_digest=preview["preview_digest"], confirm=True)


def test_a_new_row_with_no_door_never_takes_a_dataset_from_its_record(db):
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore

    db.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, source_id, created_at, updated_at) "
               "VALUES ('c-internal', ?, ?, '2026-09-01T10:00:00Z', '2026-09-01T10:00:00Z')",
               (OWNER, SOURCE))
    SQLiteCanonicalStore(db).upsert("ai_chat_messages", {
        "message_id": "m-internal", "conversation_id": "c-internal", "sender_type": "user", "content": PROMPT,
        "source_id": SOURCE, "event_at": "2026-09-01T10:00:00Z", "writer_dataset_id": DATASET})
    db.commit()
    assert _row(db, "m-internal")["writer_dataset_id"] is None


@pytest.mark.asyncio
async def test_a_door_row_whose_door_named_no_dataset_is_not_certified_by_a_leftover_receipt(db):
    _install(db)
    await _capture("m-leftover")
    _pre_stamp(db, "m-leftover")
    _attest(db)  # lists the row at this revision, certifying the install's dataset
    db.execute("UPDATE ai_chat_messages SET writer_class='owner_app', writer_app_id=?, writer_dataset_id=NULL "
               "WHERE message_id='m-leftover'", (EXTENSION_APP,))
    db.commit()
    # A recorded writer answers for itself: its door named no dataset, and a receipt is only for pre-stamp rows.
    assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=_row(db, "m-leftover")) is None
    assert _posture_code(db, "m-leftover") == "source_posture_unknown"


@pytest.mark.asyncio
async def test_no_owner_certifies_nothing(db):
    await _capture("m-no-owner")
    row = _row(db, "m-no-owner")
    assert ai_chat_capture.certified_dataset(db, owner_id=OWNER, row=row) == DATASET
    assert ai_chat_capture.certified_dataset(db, owner_id="", row=row) is None


@pytest.mark.parametrize("posture", ["personal", "mixed", "ambient"])
@pytest.mark.asyncio
async def test_the_certified_datasets_own_override_decides_as_for_a_conversation_row(db, posture):
    _install(db)
    await _capture("m-override")
    db.execute("CREATE TABLE IF NOT EXISTS user_ingestion_sources (dataset_id TEXT, source_id TEXT, enabled INTEGER, "
               "last_sync_at TEXT, last_error TEXT, updated_at TEXT, posture TEXT, exclude_spam INTEGER)")
    db.execute("INSERT INTO user_ingestion_sources (dataset_id, source_id, posture) VALUES (?,?,?)",
               (DATASET, SOURCE, posture))
    db.commit()
    assert _source_posture(db, _identity(_resolver(db), "m-override"))[0] == posture
