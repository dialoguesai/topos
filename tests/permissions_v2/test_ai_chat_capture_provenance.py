"""OD-39: the owner's own AI-chat capture app's prompts are the owner's words; nobody else's are.

protects: ``EvidenceResolver._ai_chat_owner_proven`` used to accept only the
ChatGPT export lane, so every prompt the owner's own browser extension captured
was withheld as "not the owner's". OD-39 (29 Sep 2026) makes those the owner's
words, per owner, for any AI-chat capture source the owner attaches. The forgery
this must still stop: any writer that reaches app_ingest can send a "human" row
into the owner's conversation. So the proof is WHO WROTE the row, recorded from
the channel principal (the CP's signed stamp), never from the payload; rows
written before that was recorded pass only through the owner's own attestation
receipt.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict

import pytest

from tests.ingestion.test_ai_chat_writer_class import (  # noqa: F401 (the stamp key and helpers)
    GRANTEE, OWNER, SOURCE, _PUB_B64, _app_ingest, _chat_record, _keep_the_post_canonical_pipeline_offline,
    _relay, _row, _stamp)
from topos.permissions_v2 import ai_chat_capture
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceBinding, EvidenceResolver
from topos.permissions_v2.message_evidence import _source_checks
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.principal import OWNER_APP
from topos.storage.canonical.ai_chat import CanonicalTablesManager
from topos.storage.canonical.conversations_tables import ensure_conversation_messages_table, ensure_conversations_table
from topos.storage.db.migrations import apply_all_migrations

EXTENSION_APP = "chatgpt-shadow-extension"
OTHER_OWNER = "owner-uuid-9"
PROMPT = "I am planning a trip to the coast next spring."


def _binding(owner: str) -> EvidenceBinding:
    return EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1", resource_id="resource-1",
                           owner_id=owner)


class _Canonical(sqlite3.Connection):
    """The node's connection, which also knows its file (the resolver binds to the path)."""
    path = None


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "capture.db"
    conn = sqlite3.connect(str(path), check_same_thread=False, factory=_Canonical)
    conn.row_factory = sqlite3.Row
    # The chat tables are created lazily by their first writer; the migrations then add writer columns.
    CanonicalTablesManager(conn)
    ensure_conversations_table(conn)
    ensure_conversation_messages_table(conn)
    apply_all_migrations(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('user_id', ?)", (OWNER,))
    conn.commit()
    ensure_protection_clock(path, owner_id=OWNER)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: conn, raising=False)
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", _PUB_B64)
    monkeypatch.delenv(ai_chat_capture.APP_IDS_ENV, raising=False)
    _keep_the_post_canonical_pipeline_offline(monkeypatch)
    conn.path = path
    yield conn
    conn.close()


def _resolver(db, owner: str = OWNER) -> EvidenceResolver:
    db.commit()  # the resolver installs its clock through its own connection
    return EvidenceResolver(db.path, binding=_binding(owner))


def _identity(resolver, message_id: str, source_id: str = SOURCE):
    return resolver._identity("ai_chat_messages", message_id, source_id)


def _proven(db, message_id: str, *, owner: str = OWNER, source_id: str = SOURCE) -> bool:
    """The resolver's own answer on this node (bound to OWNER); for another owner, the rule it applies."""
    if owner != OWNER:
        # One node serves one owner (its protection clock binds engine_config's user_id), so the other
        # owner's view is the predicate the resolver calls with its binding's owner id.
        row = dict(db.execute("SELECT * FROM ai_chat_messages WHERE message_id=?", (message_id,)).fetchone())
        return ai_chat_capture.capture_proven(db, owner_id=owner, identity_source_id=source_id, row=row)
    resolver = _resolver(db, owner)
    identity = _identity(resolver, message_id, source_id)
    return resolver._ai_chat_owner_proven(db, identity, resolver._load(db, identity))


def _source_check_code(db, message_id: str, *, owner: str = OWNER) -> str | None:
    resolver = _resolver(db, owner)
    identity = _identity(resolver, message_id)
    try:
        _source_checks(resolver, db, identity, resolver._load(db, identity))
    except PolicyError as exc:
        return exc.code
    return None


async def _capture(message_id: str, content: str = PROMPT, *, app: str = EXTENSION_APP, role: str = "user",
                   requester: str = OWNER, stamped: bool = True, thread: str = "thread-1", **extra: Any):
    """app_ingest over the relay, stamped as the CP stamps the owner's capture app (rule C)."""
    message = _app_ingest(f"req-{message_id}", [_chat_record(message_id, content, role=role, thread_id=thread, **extra)],
                          requester=requester, app_id=app)
    if stamped:
        _stamp(message, cls=OWNER_APP, client_id=app, acting_user=OWNER)
    result = await _relay(message)
    assert result["status"] == "ok", result
    return result


def _pre_stamp(db, *message_ids: str) -> None:
    """A row written before the node recorded writer classes (the column exists, the value never did)."""
    db.executemany("UPDATE ai_chat_messages SET writer_class=NULL, writer_app_id=NULL WHERE message_id=?",
                   [(m,) for m in message_ids])
    db.commit()


def _attest(db, *, owner: str = OWNER, source_id: str = SOURCE, app: str = EXTENSION_APP) -> Dict[str, Any]:
    preview = ai_chat_capture.preview(db, owner_id=owner, source_id=source_id, app_id=app)
    receipt = ai_chat_capture.attest(db, owner_id=owner, source_id=source_id, app_id=app,
                                     preview_digest=preview["preview_digest"], confirm=True)
    db.commit()
    return receipt


# --- 1. a grantee's write ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_grantee_write_is_refused_whatever_its_payload_claims(db):
    # The grantee names the owner's writer class and the extension's app in the record itself.
    await _capture("m-grantee", requester=GRANTEE, app="grantee-app", stamped=False,
                   writer_class="owner_app", writer_app_id=EXTENSION_APP)
    row = _row(db, "m-grantee")
    assert (row["sender_type"], row["writer_class"], row["writer_app_id"]) == ("human", "cp_relay", None)
    assert not _proven(db, "m-grantee")
    assert _source_check_code(db, "m-grantee") == "native_owner_provenance_unavailable"
    # A grantee's row is never one the owner can attest: it carries a writer.
    assert ai_chat_capture.preview(db, owner_id=OWNER, source_id=SOURCE, app_id=EXTENSION_APP)["row_count"] == 0
    _attest(db)
    assert not _proven(db, "m-grantee")


@pytest.mark.asyncio
async def test_a_grantee_cannot_reach_a_stamp_by_naming_the_capture_app(db):
    # Unstamped: the CP never vouched. The app id in the envelope is the grantee's say-so.
    await _capture("m-unstamped", requester=GRANTEE, app=EXTENSION_APP, stamped=False)
    assert _row(db, "m-unstamped")["writer_class"] == "cp_relay"
    assert not _proven(db, "m-unstamped")


# --- 2. another app -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_owner_write_from_a_non_capture_app_is_refused(db):
    await _capture("m-other-app", app="owner-notes-app")
    row = _row(db, "m-other-app")
    assert (row["writer_class"], row["writer_app_id"]) == ("owner_app", "owner-notes-app")
    assert not _proven(db, "m-other-app")
    assert _source_check_code(db, "m-other-app") == "native_owner_provenance_unavailable"


@pytest.mark.asyncio
async def test_the_owner_socket_names_no_capture_app(db):
    from topos.ingestion.ingest_helpers import ingest_ui_payload
    from topos.principal import Principal, reset_principal, set_principal

    token = set_principal(Principal(cls=OWNER_APP, channel="uds", client_id=EXTENSION_APP))
    try:
        result = await ingest_ui_payload(dataset_id=f"{OWNER}:topos:default", schema_id="chatgpt.conversation.v1",
            payload=_chat_record("m-socket", PROMPT, role="user"), source_id=SOURCE, defer_enrichment=True)
    finally:
        reset_principal(token)
    assert result["status"] == "ok", result
    row = _row(db, "m-socket")
    # Only a verified relay stamp names an app; a socket client id is not a capture.
    assert (row["writer_class"], row["writer_app_id"]) == ("owner_app", None)
    assert not _proven(db, "m-socket")


@pytest.mark.asyncio
async def test_other_stamped_classes_are_refused(db):
    for cls, message_id in (("owner_automation", "m-routine"), ("third_party", "m-third")):
        message = _app_ingest(f"req-{message_id}", [_chat_record(message_id, PROMPT, role="user")],
                              requester=OWNER, app_id=EXTENSION_APP)
        _stamp(message, cls=cls, client_id=EXTENSION_APP, acting_user=OWNER)
        await _relay(message)
        stored = db.execute("SELECT writer_class, writer_app_id FROM ai_chat_messages WHERE message_id=?",
                            (message_id,)).fetchone()
        assert stored is None or stored[1] is None, (cls, tuple(stored))
        if stored is not None:
            assert not _proven(db, message_id), cls


# --- 3. the stamped capture ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_stamped_capture_prompt_is_the_owners_and_its_reply_is_not(db):
    await _capture("m-prompt")
    await _capture("m-reply", "Here are some coastal towns to consider.", role="assistant")
    prompt, reply = _row(db, "m-prompt"), _row(db, "m-reply")
    assert (prompt["sender_type"], prompt["writer_class"], prompt["writer_app_id"]) == ("human", "owner_app", EXTENSION_APP)
    assert reply["sender_type"] == "assistant"
    assert _proven(db, "m-prompt")
    assert _source_check_code(db, "m-prompt") is None
    assert not _proven(db, "m-reply")
    assert _source_check_code(db, "m-reply") == "native_owner_provenance_unavailable"


@pytest.mark.asyncio
async def test_a_stamped_capture_still_meets_every_other_source_check(db):
    await _capture("m-quoted", "Forwarded: meet me at noon.")
    db.execute("""UPDATE ai_chat_messages SET metadata_json='{"is_forwarded": true}' WHERE message_id='m-quoted'""")
    await _capture("m-copy-1", "A sentence twice.")
    await _capture("m-copy-2", "A sentence twice.", thread="thread-2")
    assert all(_proven(db, m) for m in ("m-quoted", "m-copy-1"))
    assert _source_check_code(db, "m-quoted") == "not_original_message"
    assert _source_check_code(db, "m-copy-1") == "independent_copy_lineage"


@pytest.mark.asyncio
async def test_the_parent_conversation_must_be_the_owners_own(db):
    await _capture("m-parent")
    assert _proven(db, "m-parent")
    conversation = _row(db, "m-parent")["conversation_id"]

    def predicate() -> bool:
        return ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=SOURCE, row=_row(db, "m-parent"))

    db.execute("UPDATE ai_chat_conversations SET owner_user_id=? WHERE conversation_id=?", (OTHER_OWNER, conversation))
    assert not predicate()
    with pytest.raises(PolicyError, match="evidence_owner_binding"):  # the resolver's load refuses it first
        _proven(db, "m-parent")
    db.execute("UPDATE ai_chat_conversations SET owner_user_id=?, source_id='another-source' WHERE conversation_id=?",
               (OWNER, conversation))
    assert not predicate()
    db.execute("UPDATE ai_chat_conversations SET source_id=? WHERE conversation_id=?", (SOURCE, conversation))
    assert predicate() and _proven(db, "m-parent")


@pytest.mark.asyncio
async def test_an_internal_replay_keeps_the_app_and_a_later_door_replaces_it(db):
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore

    await _capture("m-replay")
    row = _row(db, "m-replay")
    replay = {key: row[key] for key in ("message_id", "conversation_id", "sender_type", "event_at", "content", "source_id")}
    SQLiteCanonicalStore(db).upsert("ai_chat_messages", dict(replay))  # reprocess: no door, no writer
    assert (_row(db, "m-replay")["writer_class"], _row(db, "m-replay")["writer_app_id"]) == ("owner_app", EXTENSION_APP)
    assert _proven(db, "m-replay")
    SQLiteCanonicalStore(db).upsert("ai_chat_messages", {**replay, "writer_class": "owner_app",
                                                          "writer_app_id": "owner-notes-app"})
    assert _row(db, "m-replay")["writer_app_id"] == "owner-notes-app"
    assert not _proven(db, "m-replay")


@pytest.mark.asyncio
async def test_the_od39_app_list_follows_the_cp_setting(db, monkeypatch):
    await _capture("m-env")
    monkeypatch.setenv(ai_chat_capture.APP_IDS_ENV, "some-other-extension")
    assert not _proven(db, "m-env")
    monkeypatch.setenv(ai_chat_capture.APP_IDS_ENV, f"some-other-extension, {EXTENSION_APP}")
    assert _proven(db, "m-env")
    monkeypatch.setenv(ai_chat_capture.APP_IDS_ENV, "")
    assert not _proven(db, "m-env")


@pytest.mark.asyncio
async def test_an_owner_import_of_the_capture_source_is_the_owners(db):
    await _capture("m-import")
    db.execute("UPDATE ai_chat_messages SET writer_class='owner_import', writer_app_id=NULL WHERE message_id='m-import'")
    assert _proven(db, "m-import")
    for writer in ("local_legacy", "cp_relay", "third_party", "owner_automation", "something_new"):
        db.execute("UPDATE ai_chat_messages SET writer_class=? WHERE message_id='m-import'", (writer,))
        assert not _proven(db, "m-import"), writer


@pytest.mark.asyncio
async def test_the_export_lanes_source_never_takes_the_capture_rule(db):
    from topos.permissions_v2.ingest_protocol import CHATGPT_SOURCE_ID
    await _capture("m-lane")
    conversation = _row(db, "m-lane")["conversation_id"]
    db.execute("UPDATE ai_chat_messages SET source_id=? WHERE message_id='m-lane'", (CHATGPT_SOURCE_ID,))
    db.execute("UPDATE ai_chat_conversations SET source_id=? WHERE conversation_id=?", (CHATGPT_SOURCE_ID, conversation))
    assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=CHATGPT_SOURCE_ID,
                                              row=_row(db, "m-lane"))
    with pytest.raises(PolicyError, match="capture_attestation_invalid"):
        ai_chat_capture.preview(db, owner_id=OWNER, source_id=CHATGPT_SOURCE_ID, app_id=EXTENSION_APP)


@pytest.mark.asyncio
async def test_the_identity_must_name_the_rows_own_source(db):
    await _capture("m-identity")
    row = _row(db, "m-identity")
    assert ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=SOURCE, row=row)
    assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id="another-source", row=row)
    assert not ai_chat_capture.capture_proven(db, owner_id="", identity_source_id=SOURCE, row=row)


# --- 4. rows written before stamping --------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_pre_stamp_row_passes_only_after_the_owners_attestation(db):
    await _capture("m-old-1")
    await _capture("m-old-2", "I would like to learn to sail.", thread="thread-2")
    await _capture("m-old-reply", "Sailing lessons are widely available.", role="assistant")
    _pre_stamp(db, "m-old-1", "m-old-2", "m-old-reply")
    assert not _proven(db, "m-old-1")
    assert _source_check_code(db, "m-old-1") == "native_owner_provenance_unavailable"

    preview = ai_chat_capture.preview(db, owner_id=OWNER, source_id=SOURCE, app_id=EXTENSION_APP)
    assert (preview["row_count"], preview["conversation_count"]) == (2, 2)  # the reply is not a prompt
    assert "m-old-1" not in str(preview) and PROMPT not in str(preview)
    with pytest.raises(PolicyError, match="capture_attestation_unconfirmed"):
        ai_chat_capture.attest(db, owner_id=OWNER, source_id=SOURCE, app_id=EXTENSION_APP,
                               preview_digest=preview["preview_digest"], confirm="yes")
    with pytest.raises(PolicyError, match="capture_attestation_preview_stale"):
        ai_chat_capture.attest(db, owner_id=OWNER, source_id=SOURCE, app_id=EXTENSION_APP,
                               preview_digest="0" * 64, confirm=True)
    # A row that arrives between preview and confirmation changes the digest: the owner re-previews.
    await _capture("m-old-3", "Another prompt.", thread="thread-3")
    _pre_stamp(db, "m-old-3")
    with pytest.raises(PolicyError, match="capture_attestation_preview_stale"):
        ai_chat_capture.attest(db, owner_id=OWNER, source_id=SOURCE, app_id=EXTENSION_APP,
                               preview_digest=preview["preview_digest"], confirm=True)
    assert not ai_chat_capture.installed(db)

    receipt = _attest(db)
    assert receipt["row_count"] == 3
    assert all(_proven(db, m) for m in ("m-old-1", "m-old-2", "m-old-3"))
    assert _source_check_code(db, "m-old-1") is None
    assert not _proven(db, "m-old-reply")
    # Idempotent: nothing left to attest. The rows themselves were never re-labelled.
    assert ai_chat_capture.preview(db, owner_id=OWNER, source_id=SOURCE, app_id=EXTENSION_APP)["row_count"] == 0
    assert _row(db, "m-old-1")["writer_class"] is None

    # A later rewrite falls out of the receipt; the receipt is not editable.
    db.execute("UPDATE ai_chat_messages SET content='I am planning a trip inland.' WHERE message_id='m-old-2'")
    assert not _proven(db, "m-old-2")
    with pytest.raises(sqlite3.IntegrityError, match="capture_receipt_immutable"):
        db.execute(f"UPDATE {ai_chat_capture.RECEIPT_ROWS} SET content_revision='x'")
    with pytest.raises(sqlite3.IntegrityError, match="capture_receipt_immutable"):
        db.execute(f"UPDATE {ai_chat_capture.RECEIPTS} SET owner_id=?", (OTHER_OWNER,))

    # Revocation withdraws every row it covered, once.
    ai_chat_capture.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    assert not _proven(db, "m-old-1")
    with pytest.raises(PolicyError, match="capture_receipt_revoked"):
        ai_chat_capture.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    with pytest.raises(sqlite3.IntegrityError, match="capture_receipt_immutable"):
        db.execute(f"UPDATE {ai_chat_capture.RECEIPTS} SET revoked_at=NULL")
    with pytest.raises(sqlite3.IntegrityError, match="capture_receipt_immutable"):
        db.execute(f"INSERT INTO {ai_chat_capture.RECEIPT_ROWS} VALUES (?,?,?,?)",
                   (receipt["receipt_id"], "m-new", "c", "r"))
    assert [r["revoked_at"] is not None for r in ai_chat_capture.receipts(db, owner_id=OWNER)] == [True]


@pytest.mark.asyncio
async def test_a_receipt_names_rows_by_conversation_too(db):
    await _capture("m-moved")
    _pre_stamp(db, "m-moved")
    _attest(db)
    assert _proven(db, "m-moved")
    # The same id and words under another of the owner's conversations is not the attested row.
    db.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, updated_at) "
               "VALUES ('conv-elsewhere', ?, NULL, ?, '2026-09-01', '2026-09-01')", (OWNER, SOURCE))
    db.execute("UPDATE ai_chat_messages SET conversation_id='conv-elsewhere' WHERE message_id='m-moved'")
    assert not _proven(db, "m-moved")


# --- 5. one owner's sources and receipts never serve another's ------------------------------------

def _insert_capture_row(db, *, message_id, owner, source, writer=None, app=None, conversation=None, content=PROMPT):
    conversation = conversation or f"conv-{message_id}"
    db.execute("INSERT OR IGNORE INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, "
               "updated_at) VALUES (?,?,NULL,?,'2026-09-01','2026-09-01')", (conversation, owner, source))
    db.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, source_id, "
               "writer_class, writer_app_id) VALUES (?,?,'user','2026-09-01T10:00:00Z',?,?,?,?)",
               (message_id, conversation, content, source, writer, app))
    db.commit()


def _rule(db, message_id: str, *, owner: str = OWNER, source_id: str) -> bool:
    """What the resolver of a node bound to ``owner`` asks about this row."""
    row = dict(db.execute("SELECT * FROM ai_chat_messages WHERE message_id=?", (message_id,)).fetchone())
    return ai_chat_capture.capture_proven(db, owner_id=owner, identity_source_id=source_id, row=row)


def test_a_second_owners_capture_source_is_isolated_from_the_first(db):
    custom, app = "claude_ui_capture", "claude-capture-extension"
    _insert_capture_row(db, message_id="a-old", owner=OWNER, source=custom)
    _insert_capture_row(db, message_id="a-new", owner=OWNER, source=custom, writer="owner_app", app=app)
    _insert_capture_row(db, message_id="b-old", owner=OTHER_OWNER, source=custom)
    _insert_capture_row(db, message_id="b-new", owner=OTHER_OWNER, source=custom, writer="owner_app", app=app)

    # Nobody attached this source yet: nothing passes, stamped or not.
    for message_id, owner in (("a-old", OWNER), ("a-new", OWNER), ("b-old", OTHER_OWNER), ("b-new", OTHER_OWNER)):
        assert not _rule(db, message_id, owner=owner, source_id=custom)

    # Owner A attaches it. A's stamped and attested rows pass; B's rows pass for nobody.
    assert _attest(db, owner=OWNER, source_id=custom, app=app)["row_count"] == 1
    assert custom in ai_chat_capture.capture_sources(db, OWNER)
    assert custom not in ai_chat_capture.capture_sources(db, OTHER_OWNER)
    assert _rule(db, "a-old", source_id=custom) and _rule(db, "a-new", source_id=custom)
    for message_id in ("b-old", "b-new"):
        assert not _rule(db, message_id, owner=OTHER_OWNER, source_id=custom)
        assert not _rule(db, message_id, owner=OWNER, source_id=custom)
    # A's rows under B's binding: A's conversation is not B's.
    assert not _rule(db, "a-new", owner=OTHER_OWNER, source_id=custom)
    assert not _rule(db, "a-old", owner=OTHER_OWNER, source_id=custom)
    # B cannot revoke or list A's receipt.
    (receipt,) = ai_chat_capture.receipts(db, owner_id=OWNER)
    assert ai_chat_capture.receipts(db, owner_id=OTHER_OWNER) == []
    with pytest.raises(PolicyError, match="capture_receipt_unknown"):
        ai_chat_capture.revoke(db, owner_id=OTHER_OWNER, receipt_id=receipt["receipt_id"])

    # B attaches the same source for B's own rows; A's receipt still covers only A's.
    assert _attest(db, owner=OTHER_OWNER, source_id=custom, app=app)["row_count"] == 1
    assert _rule(db, "b-old", owner=OTHER_OWNER, source_id=custom)
    assert _rule(db, "b-new", owner=OTHER_OWNER, source_id=custom)
    assert not _rule(db, "a-old", owner=OTHER_OWNER, source_id=custom)
    assert not _rule(db, "b-old", owner=OWNER, source_id=custom)

    # A's revocation withdraws A's source for A's stamped rows too, and leaves B's standing.
    ai_chat_capture.revoke(db, owner_id=OWNER, receipt_id=receipt["receipt_id"])
    assert custom not in ai_chat_capture.capture_sources(db, OWNER)
    assert not _rule(db, "a-new", source_id=custom) and not _rule(db, "a-old", source_id=custom)
    assert _rule(db, "b-new", owner=OTHER_OWNER, source_id=custom)


# --- the owner socket route ------------------------------------------------------------------------

@pytest.fixture()
def owner_app(db, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from topos.api import permissions_ai_chat_capture
    from topos.config.settings import settings as runtime_settings

    runtime = SimpleNamespace(protocol=SimpleNamespace(canonical_database=db.path,
        ledger=SimpleNamespace(identity=SimpleNamespace(owner_id=OWNER))))
    monkeypatch.setattr("topos.permissions_v2.runtime.get_runtime", lambda: runtime)
    monkeypatch.setattr(runtime_settings, "topos_owner_key", "owner-key", raising=False)
    app = FastAPI()
    app.include_router(permissions_ai_chat_capture.router)
    return app


async def _call(app, method: str, path: str, *, socket: bool = True, **kwargs):
    import httpx
    from topos.uds import UDSChannelApp

    transport = httpx.ASGITransport(app=UDSChannelApp(app) if socket else app)
    headers = {} if socket else {"Authorization": "Bearer owner-key"}
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        return await client.request(method, f"/v1/permissions-beta/v2/ai-chat/capture-attestation{path}",
                                    headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_the_attestation_route_is_the_owners_socket_only(db, owner_app):
    await _capture("m-route")
    _pre_stamp(db, "m-route")
    body = {"source_id": SOURCE, "app_id": EXTENSION_APP}

    # The owner key over TCP is not the owner's socket.
    for path in ("/preview", "/attest", "/revoke"):
        response = await _call(owner_app, "POST", path, socket=False, json=body)
        assert response.status_code == 403, (path, response.text)
    assert (await _call(owner_app, "GET", "/receipts", socket=False)).status_code == 403

    preview = (await _call(owner_app, "POST", "/preview", json=body)).json()
    assert preview["row_count"] == 1 and "m-route" not in str(preview)
    stale = await _call(owner_app, "POST", "/attest", json={**body, "preview_digest": "0" * 64, "confirm": True})
    assert (stale.status_code, stale.json()["detail"]) == (409, "capture_attestation_preview_stale")
    unconfirmed = await _call(owner_app, "POST", "/attest", json={**body, "preview_digest": preview["preview_digest"]})
    assert unconfirmed.status_code == 400
    assert not _proven(db, "m-route")

    attested = await _call(owner_app, "POST", "/attest",
                           json={**body, "preview_digest": preview["preview_digest"], "confirm": True})
    assert attested.status_code == 200, attested.text
    assert _proven(db, "m-route")
    listed = (await _call(owner_app, "GET", "/receipts")).json()["receipts"]
    assert [r["receipt_id"] for r in listed] == [attested.json()["receipt_id"]]

    revoked = await _call(owner_app, "POST", "/revoke", json={"receipt_id": attested.json()["receipt_id"]})
    assert revoked.status_code == 200
    assert not _proven(db, "m-route")
    again = await _call(owner_app, "POST", "/revoke", json={"receipt_id": attested.json()["receipt_id"]})
    assert again.status_code == 409


# --- defence in depth: each guard holds on its own ------------------------------------------------

def test_the_route_refuses_an_owner_class_that_did_not_come_through_the_socket():
    from fastapi import HTTPException
    from topos.api.permissions_ai_chat_capture import _require_owner_socket
    from topos.principal import Principal

    for principal in (Principal(cls=OWNER_APP, channel="cp_relay", client_id=EXTENSION_APP, acting_user=OWNER),
                      Principal(cls=OWNER_APP, channel="local_http"), None):
        with pytest.raises(HTTPException) as refused:
            _require_owner_socket(principal)
        assert refused.value.status_code == 403
    _require_owner_socket(Principal(cls=OWNER_APP, channel="uds"))


@pytest.mark.asyncio
async def test_a_receipt_row_never_serves_the_export_lanes_source(db):
    from topos.permissions_v2.ingest_protocol import CHATGPT_SOURCE_ID
    await _capture("m-forged")
    conversation = _row(db, "m-forged")["conversation_id"]
    db.execute("UPDATE ai_chat_messages SET source_id=?, writer_class=NULL, writer_app_id=NULL WHERE message_id='m-forged'",
               (CHATGPT_SOURCE_ID,))
    db.execute("UPDATE ai_chat_conversations SET source_id=? WHERE conversation_id=?", (CHATGPT_SOURCE_ID, conversation))
    row = _row(db, "m-forged")
    # A receipt for the lane's source can only be written around the attestation door; it still proves nothing.
    ai_chat_capture.install(db)
    db.execute(f"INSERT INTO {ai_chat_capture.RECEIPTS} VALUES ('forged', ?, ?, ?, ?, 's', 'd', 1, 1, NULL, NULL)",
               (ai_chat_capture.VERSION, OWNER, CHATGPT_SOURCE_ID, EXTENSION_APP))
    db.execute(f"INSERT INTO {ai_chat_capture.RECEIPT_ROWS} VALUES ('forged', 'm-forged', ?, ?)",
               (conversation, ai_chat_capture.content_revision(row)))
    assert CHATGPT_SOURCE_ID in ai_chat_capture.capture_sources(db, OWNER)
    assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=CHATGPT_SOURCE_ID, row=row)
    db.execute("UPDATE ai_chat_messages SET writer_class='owner_app', writer_app_id=? WHERE message_id='m-forged'",
               (EXTENSION_APP,))
    assert not ai_chat_capture.capture_proven(db, owner_id=OWNER, identity_source_id=CHATGPT_SOURCE_ID,
                                              row=_row(db, "m-forged"))


def test_an_empty_owner_matches_no_conversation_even_an_ownerless_one(db):
    # The canonicalizer binds a conversation to "" when its dataset id has no owner prefix.
    _insert_capture_row(db, message_id="m-ownerless", owner="", source=SOURCE, writer="owner_app", app=EXTENSION_APP)
    row = dict(db.execute("SELECT * FROM ai_chat_messages WHERE message_id='m-ownerless'").fetchone())
    assert not ai_chat_capture.capture_proven(db, owner_id="", identity_source_id=SOURCE, row=row)
    assert not ai_chat_capture.capture_proven(db, owner_id=None, identity_source_id=SOURCE, row=row)


def test_a_conversation_that_changes_owner_leaves_the_first_owners_receipt_behind(db):
    _insert_capture_row(db, message_id="m-moving", owner=OWNER, source=SOURCE)
    _attest(db)
    assert _rule(db, "m-moving", owner=OWNER, source_id=SOURCE)
    db.execute("UPDATE ai_chat_conversations SET owner_user_id=? WHERE conversation_id='conv-m-moving'", (OTHER_OWNER,))
    assert not _rule(db, "m-moving", owner=OTHER_OWNER, source_id=SOURCE)
    assert ai_chat_capture.preview(db, owner_id=OTHER_OWNER, source_id=SOURCE, app_id=EXTENSION_APP)["row_count"] == 1


@pytest.mark.asyncio
async def test_the_export_lane_still_refuses_a_reply_with_a_live_link(db, monkeypatch):
    from topos.permissions_v2.ingest_protocol import CHATGPT_SOURCE_ID
    await _capture("m-lane-reply", "A synthetic reply.", role="assistant")
    conversation = _row(db, "m-lane-reply")["conversation_id"]
    db.execute("UPDATE ai_chat_messages SET source_id=? WHERE message_id='m-lane-reply'", (CHATGPT_SOURCE_ID,))
    db.execute("UPDATE ai_chat_conversations SET source_id=? WHERE conversation_id=?", (CHATGPT_SOURCE_ID, conversation))
    monkeypatch.setattr(EvidenceResolver, "_validate_native_origin", lambda *_a, **_k: True)
    resolver = _resolver(db)
    identity = _identity(resolver, "m-lane-reply", CHATGPT_SOURCE_ID)
    assert not resolver._ai_chat_owner_proven(db, identity, _row(db, "m-lane-reply"))
    db.execute("UPDATE ai_chat_messages SET sender_type='user' WHERE message_id='m-lane-reply'")
    assert resolver._ai_chat_owner_proven(db, identity, _row(db, "m-lane-reply"))   # the link alone decides the prompt


@pytest.mark.asyncio
async def test_a_conversation_message_twin_of_an_attested_prompt_is_not_proven(db):
    await _capture("m-twin")
    _pre_stamp(db, "m-twin")
    _attest(db)
    row = _row(db, "m-twin")
    assert _proven(db, "m-twin")
    resolver = _resolver(db)
    twin = resolver._identity("conversation_messages", "m-twin", SOURCE, f"{OWNER}:topos:default")
    assert not resolver._ai_chat_capture_proven(db, twin, row)
