"""ChatGPT snapshot canary: one owner prompt, from conversations.json to a p2a recipient, and nothing else.

A synthetic export holds owner prompts, assistant replies, a hidden
custom-instructions node, an edited-away branch and a group chat. Every owner
action goes through the shipped signed doors on the work canary's real runtime
(tests/permissions_v2/test_ingest_snapshot_work_canary.py, whose ``lane``
fixture, signers and owner handlers are reused unchanged): describe, enroll,
enqueue, run, status and revoke as CP-signed ``permissions_v2_ingest_snapshot``
commands naming ``chatgpt-owner-snapshot/v1``, after the owner attests their self
entity through ``permissions_v2_identity_command``; evidence review through
``permissions_v2_evidence_preview`` / ``_review_record``; a p2a-v1 grant through
the signed status/mutate pair; the recipient read through the source-message
transport (``release_transport.dispatch_source_message``) and the adapter it
builds.

Fixture state written directly, and why: the scoped locator fact over one
prompt is hand-seeded with ``FactStore.assert_fact`` because the lane derives no
facts (INGEST_SNAPSHOT_DESIGN.md, "ChatGPT lane"). The forged row in
``test_a_row_with_the_lanes_source_id_but_no_link_is_not_owner_authored`` is
written through the shared ``SQLiteCanonicalStore.upsert`` every legacy door
reaches, and says so. Everything else is written by the doors themselves.
"""
from __future__ import annotations

from copy import deepcopy
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from tests.ingestion.test_chatgpt_owner_snapshot import (
    ALTERNATE_TEXT, ASSISTANT_REPLY, CONVERSATION, GROUP, GROUP_OWNER_TEXT, GROUP_PARTICIPANT_TEXT, HIDDEN_TEXT,
    OWNER_PROMPT, SECOND_PROMPT, SECOND_REPLY, export, group_chat, owner_chat)
from tests.permissions_v2.test_contract_and_ledger import sample_policy
from tests.permissions_v2.test_ingest_snapshot_work_canary import (  # noqa: F401 (fixtures)
    CP_ISSUER, FRONTEND, OWNER_ID, OWNER_PRINCIPAL, SELF_ENTITY, _next, attest_selves, canonical, corpus, ingest, lane,
    owner_message, paired_runtime, projection_runtime, protocol_call)
from tests.permissions_v2.test_source_release_sibling_lane import source_adapter_read, source_socket_read
from topos.core.handlers import handle_control_plane_request
from topos.features.facts.store import FactStore
from topos.permissions_v2.canonical import digest
from topos.permissions_v2.evidence import ReviewedClassification
from topos.permissions_v2.evidence_reviews import OwnerEvidencePreview, RecordEvidenceReview
from topos.permissions_v2.ingest_protocol import CHATGPT_OWNER_ATTESTATION, CHATGPT_READER_CONTRACT, CHATGPT_SOURCE_ID
from topos.permissions_v2.protocol import MutationBody, StatusRequestBody, sign_mutation, sign_status_request
from topos.permissions_v2.release import VOCABULARY

SOURCE = CHATGPT_SOURCE_ID
DATASET = "dataset-chatgpt-canary"
SNAPSHOT_ID = "chatgpt-canary"
DOMAIN = "food"
CONVERSATION_ID = f"{SOURCE}:{CONVERSATION}"
PROMPT_ID = f"{CONVERSATION_ID}:user-1"
REPLY_ID = f"{CONVERSATION_ID}:assistant-1"
RESULT = {"status": "ok", "messages_processed": 4, "messages_created": 4, "conversations_created": 1,
          "historical_skipped": 0}
# The ids the reader would mint if it emitted a node it must drop or withhold.
NEVER_EMITTED = {HIDDEN_TEXT: f"{CONVERSATION_ID}:hidden-1", ALTERNATE_TEXT: f"{CONVERSATION_ID}:alternate-1",
                 GROUP_OWNER_TEXT: f"{SOURCE}:{GROUP}:user-1", GROUP_PARTICIPANT_TEXT: f"{SOURCE}:{GROUP}:user-2"}


# --- the lane through its signed doors --------------------------------------

async def run_chatgpt_lane(lane, data=None, *, with_status=True):
    """attest -> describe -> enroll -> enqueue -> run (-> status), every step a signed owner command."""
    await attest_selves(lane, [SELF_ENTITY])
    snapshot = lane.root / f"{SNAPSHOT_ID}.json"
    snapshot.write_bytes(data if data is not None else export(owner_chat(), group_chat()))
    snapshot.chmod(0o400)
    lane_request = {"source_id": SOURCE}
    described = await ingest(lane, {**lane_request, "operation": "describe", "snapshot_id": SNAPSHOT_ID,
                                    "reader_contract": CHATGPT_READER_CONTRACT})
    enrollment = await ingest(lane, {**lane_request, "operation": "enroll", "snapshot_id": SNAPSHOT_ID,
        "reader_contract": CHATGPT_READER_CONTRACT, "dataset_id": DATASET, "snapshot_sha256": described.snapshot_sha256,
        "owner_attestation": CHATGPT_OWNER_ATTESTATION})
    job = await ingest(lane, {**lane_request, "operation": "enqueue", "enrollment_id": enrollment.enrollment_id})
    result = await ingest(lane, {**lane_request, "operation": "run", "job_id": job.job_id})
    status = await ingest(lane, {**lane_request, "operation": "status", "job_id": job.job_id}) if with_status else None
    return SimpleNamespace(described=described, enrollment=enrollment, job=job, result=result, status=status)


def ai_rows(lane):
    with canonical(lane) as conn:
        return conn.execute("SELECT message_id,conversation_id,sender_type,actor_role,source_id,content,metadata_json "
                            "FROM ai_chat_messages ORDER BY conversation_id,sequence").fetchall()


def seed_locator(lane, record_id, value, *, source_id=SOURCE):
    """The hand-seeded scoped locator: a p2a read releases the messages its lineage names."""
    with canonical(lane) as conn:
        fact = FactStore(conn).assert_fact(subject_entity_id="self", predicate="prefers", object_value=value,
            disclosure="scoped", asserted_by="owner",
            source_refs=[{"table": "ai_chat_messages", "record_id": record_id, "source_id": source_id}])
    return fact["object_id"]


async def review(fact_id, *, review_id):
    """The owner labels every node owner-authored self speech: labels cannot make a row the owner's."""
    preview = OwnerEvidencePreview.parse(await owner_message("evidence", "preview", {"fact_id": fact_id}))
    assert preview.status == "complete", preview.reason_code
    classifications = [ReviewedClassification(evidence=version, domains=[DOMAIN], sensitivity="personal",
        subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
        independent_copies="none_known") for version in preview.snapshot.artifacts + preview.snapshot.leaves]
    recorded = await owner_message("evidence", "review_record", RecordEvidenceReview(review_id=review_id,
        expected_snapshot=preview.snapshot, expected_current_review_revision=None,
        classifications=classifications).model_dump())
    return preview.snapshot, recorded


def qualify(lane, fact_id):
    service = lane.runtime.evidence_reviews(require_existing=True)
    return service.resolver.qualify(fact_id, reviews=service.reviews)


def chatgpt_source_policy(lane, now):
    """A p2a-v1 raw message grant over this lane's source, the AI-chat table and the reviewed domain."""
    raw = sample_policy()
    raw["binding"].update(lane.runtime.protocol.ledger.identity.model_dump())
    raw["policy_version_id"] = "chatgpt-source-policy-1"
    raw["versions"]["vocabulary"] = VOCABULARY
    raw["validity"] = {"starts_at": now - 3600, "expires_at": now + 3600}
    raw["source_universe"]["source_ids"] = [SOURCE]
    atom = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": [DOMAIN]}
    rule = raw["rules"][0]
    rule["evidence_use"]["sources"]["values"] = [SOURCE]
    rule["evidence_use"]["predicate"] = deepcopy(atom)
    rule["release"]["predicate"] = deepcopy(atom)
    rule["release"]["forms"][0]["tables"] = ["ai_chat_messages"]
    return raw


async def signed_chatgpt_grant(lane):
    now = int(time.time())
    raw = chatgpt_source_policy(lane, now)
    identity = lane.runtime.protocol.ledger.identity
    status = await protocol_call(lane, "status", sign_status_request(StatusRequestBody.parse({
        "version": "topos-policy-status-request/v2", "kid": "cp-key", "issuer_id": CP_ISSUER,
        "audience_id": identity.node_id, "request_id": _next(lane, "status"), "binding": raw["binding"],
        "command_id": None, "command_hash": None, "issued_at": now, "expires_at": now + 100}), lane.cp_key))
    epoch = status.state.node_epoch
    authority = {**raw["binding"], "grant_generation": 1, "assignment_generation": 1,
                 "policy_version_id": raw["policy_version_id"], "policy_hash": digest(raw),
                 "capability_version": raw["versions"]["capability"],
                 "protection_revision": status.state.protection_revision, "node_epoch": epoch + 1}
    ack = await protocol_call(lane, "mutate", sign_mutation(MutationBody.parse({
        "version": "topos-policy-mutation/v2", "kid": "cp-key", "issuer_id": CP_ISSUER,
        "audience_id": identity.node_id, "command_id": _next(lane, "activate"), "operation": "activate",
        "expected_epoch": epoch, "authority": authority, "policy": raw,
        "owner_authorization": {"actor_id": identity.owner_id, "client_id": FRONTEND},
        "issued_at": now, "expires_at": now + 100}), lane.cp_key))
    assert (ack.outcome, ack.reason_code) == ("applied", "ok")
    assert ack.receipt.authority.capability_version == "permissions-beta/p2a-v1"
    return ack.receipt.authority


async def app_ingest(lane, monkeypatch, records, *, dataset=f"{OWNER_ID}:chatgpt"):
    """The relay door the owner's own ChatGPT extension uses, on the lane's canonical database."""
    import topos.core.handlers as handlers
    import topos.core.state as state
    conn = sqlite3.connect(str(lane.canonical), check_same_thread=False)
    monkeypatch.setattr(state, "get_db_connection", lambda: conn)
    monkeypatch.setattr(handlers, "get_db_connection", lambda: conn)
    monkeypatch.setenv("TOPOS_PIPELINE_WORKER", "off")
    try:
        response = await handle_control_plane_request({"id": _next(lane, "app-ingest"), "type": "app_ingest",
            "payload": {"user_id": OWNER_ID, "dataset_id": dataset, "source_id": "chatgpt_ui_conversation",
                        "records": records}}, principal=OWNER_PRINCIPAL)
    finally:
        conn.close()
    assert response["status"] == "ok", response
    return response


def released_text(outputs):
    return [(record["record_id"], record["canonical_table"], record["content"])
            for _result, output in outputs for record in output["records"]]


async def prove(lane):
    """The positive lane up to a released prompt. Returns what the controls perturb."""
    run = await run_chatgpt_lane(lane)
    # Counts are pinned by the positive test only, so a control fails at its own assertion.
    assert run.result.status == "ok"
    fact_id = seed_locator(lane, PROMPT_ID, "Contoso oolong")
    await review(fact_id, review_id="chatgpt-canary-review")
    assert qualify(lane, fact_id).verdict == "qualified"
    authority = await signed_chatgpt_grant(lane)
    outputs, error = source_adapter_read(lane, authority, fact_id, request_id="chatgpt-canary-read-1")
    assert error is None and released_text(outputs) == [(PROMPT_ID, "ai_chat_messages", OWNER_PROMPT)]
    return SimpleNamespace(run=run, fact_id=fact_id, authority=authority)


# --- positive ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_owner_prompt_in_the_export_reaches_a_p2a_recipient_as_its_exact_text(lane, monkeypatch):
    run = await run_chatgpt_lane(lane)
    assert run.described.reader_contract == CHATGPT_READER_CONTRACT
    assert (run.enrollment.source_id, run.enrollment.dataset_id, run.enrollment.state) == (SOURCE, DATASET, "active")
    assert run.result.model_dump() == RESULT
    assert run.status.status == "done" and run.status.result.model_dump() == RESULT

    marker = {"topos_owner_ingest": {"version": "owner-attested-snapshot/v1",
                                     "enrollment_id": run.enrollment.enrollment_id, "job_id": run.job.job_id}}
    assert [(row[0], row[1], row[2], row[3], row[4], row[5], json.loads(row[6])) for row in ai_rows(lane)] == [
        (PROMPT_ID, CONVERSATION_ID, "human", "authored", SOURCE, OWNER_PROMPT, marker),
        (REPLY_ID, CONVERSATION_ID, "assistant", "addressed", SOURCE, ASSISTANT_REPLY, marker),
        (f"{CONVERSATION_ID}:user-2", CONVERSATION_ID, "human", "authored", SOURCE, SECOND_PROMPT, marker),
        (f"{CONVERSATION_ID}:assistant-2", CONVERSATION_ID, "assistant", "addressed", SOURCE, SECOND_REPLY, marker)]
    with canonical(lane) as conn:
        assert conn.execute("SELECT conversation_id,owner_user_id,source_id,title FROM ai_chat_conversations").fetchall() == [
            (CONVERSATION_ID, OWNER_ID, SOURCE, "Contoso tea")]
        links = conn.execute("SELECT message_id,enrollment_id,job_id FROM ingest_provenance_records ORDER BY message_id").fetchall()
        assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
    assert links == sorted((row[0], run.enrollment.enrollment_id, run.job.job_id) for row in ai_rows(lane))

    fact_id = seed_locator(lane, PROMPT_ID, "Contoso oolong")
    snapshot, recorded = await review(fact_id, review_id="chatgpt-canary-review")
    assert [(leaf.identity.table, leaf.identity.record_id, leaf.identity.source_id, leaf.identity.dataset_kind)
            for leaf in snapshot.leaves] == [("ai_chat_messages", PROMPT_ID, SOURCE, "node_resource")]
    assert recorded["state"]["qualification"]["verdict"] == "qualified"

    authority = await signed_chatgpt_grant(lane)
    frames = await source_socket_read(lane, authority, fact_id, request_id="chatgpt-canary-socket", monkeypatch=monkeypatch)
    [frame] = frames
    assert frame["status"] == "ok", frame
    assert [(record["record_id"], record["content"]) for record in frame["payload"]["output"]["records"]] == [
        (PROMPT_ID, OWNER_PROMPT)]
    released = json.dumps(frames)
    for private in (ASSISTANT_REPLY, SECOND_PROMPT, HIDDEN_TEXT, ALTERNATE_TEXT, GROUP_OWNER_TEXT, GROUP_PARTICIPANT_TEXT):
        assert private not in released


# --- negative controls -------------------------------------------------------

@pytest.mark.asyncio
async def test_the_assistant_reply_is_never_owner_speech_even_when_the_owner_labels_it_so(lane):
    proved = await prove(lane)
    fact_id = seed_locator(lane, REPLY_ID, "Contoso oolong reply")
    snapshot, recorded = await review(fact_id, review_id="chatgpt-reply-review")
    assert [leaf.identity.record_id for leaf in snapshot.leaves] == [REPLY_ID]
    assert recorded["state"]["qualification"]["reason_code"] == "not_owner_authored"
    assert (qualify(lane, fact_id).verdict, qualify(lane, fact_id).reason_code) == ("withheld", "not_owner_authored")
    outputs, error = source_adapter_read(lane, proved.authority, fact_id, request_id="chatgpt-reply-read")
    assert (outputs, error) == ([], "not_owner_authored")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", list(NEVER_EMITTED), ids=["hidden_custom_instructions", "alternate_branch",
                                                           "group_chat_owner", "group_chat_participant"])
async def test_hidden_alternate_and_group_chat_prompts_leave_nothing_to_release(lane, text):
    proved = await prove(lane)
    with canonical(lane) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_chat_messages WHERE instr(content, ?) > 0", (text,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_chat_conversations WHERE conversation_id=?",
                            (f"{SOURCE}:{GROUP}",)).fetchone()[0] == 0
    # A locator naming the id the reader would have minted has nothing to stand on.
    fact_id = seed_locator(lane, NEVER_EMITTED[text], "never emitted")
    assert qualify(lane, fact_id).reason_code == "evidence_missing"
    outputs, error = source_adapter_read(lane, proved.authority, fact_id, request_id="chatgpt-never-emitted-read")
    assert (outputs, error) == ([], "evidence_missing")


@pytest.mark.asyncio
async def test_the_same_prompt_written_through_app_ingest_is_not_owner_authored(lane, monkeypatch):
    proved = await prove(lane)
    await app_ingest(lane, monkeypatch, [{"id": "extension-copy-1", "thread_id": CONVERSATION_ID, "role": "user",
                                          "content": OWNER_PROMPT, "created_at": 1757000200.5}])
    with canonical(lane) as conn:
        row = conn.execute("SELECT m.sender_type,m.source_id,c.owner_user_id FROM ai_chat_messages m JOIN "
                           "ai_chat_conversations c ON c.conversation_id=m.conversation_id "
                           "WHERE m.message_id='extension-copy-1'").fetchone()
    # Everything the old rule and a naive "human" rule looked at says "the owner typed this".
    assert row == ("human", "chatgpt_ui_conversation", OWNER_ID)
    fact_id = seed_locator(lane, "extension-copy-1", "Contoso oolong via extension", source_id="chatgpt_ui_conversation")
    _snapshot, recorded = await review(fact_id, review_id="chatgpt-extension-review")
    assert recorded["state"]["qualification"]["reason_code"] == "not_owner_authored"
    assert qualify(lane, fact_id).reason_code == "not_owner_authored"
    outputs, error = source_adapter_read(lane, proved.authority, fact_id, request_id="chatgpt-extension-read")
    assert (outputs, error) == ([], "not_owner_authored")


@pytest.mark.asyncio
async def test_a_row_with_the_lanes_source_id_but_no_link_is_not_owner_authored(lane):
    """Simulated legacy writer: the shared canonical upsert, the lane's source id, in the lane's own conversation."""
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
    proved = await prove(lane)
    with canonical(lane) as conn:
        SQLiteCanonicalStore(conn).upsert("ai_chat_messages", {
            "message_id": f"{CONVERSATION_ID}:forged-1", "conversation_id": CONVERSATION_ID, "sender_type": "human",
            "sender_id": "self", "event_at": "2025-09-04T15:40:00.000000+00:00", "source_id": SOURCE,
            "content": "I prefer Fabrikam espresso over tea."})
    fact_id = seed_locator(lane, f"{CONVERSATION_ID}:forged-1", "Fabrikam espresso")
    await review(fact_id, review_id="chatgpt-forged-review")
    assert qualify(lane, fact_id).reason_code == "not_owner_authored"
    outputs, error = source_adapter_read(lane, proved.authority, fact_id, request_id="chatgpt-forged-read")
    assert (outputs, error) == ([], "not_owner_authored")


@pytest.mark.asyncio
async def test_a_later_app_ingest_rewrite_of_the_lane_row_is_refused_by_the_store(lane, monkeypatch):
    proved = await prove(lane)
    with canonical(lane) as conn:
        before = conn.execute("SELECT content,sender_type,conversation_id,source_id,metadata_json FROM ai_chat_messages "
                              "WHERE message_id=?", (PROMPT_ID,)).fetchone()
    await app_ingest(lane, monkeypatch, [{"id": PROMPT_ID, "thread_id": CONVERSATION_ID, "role": "user",
                                          "content": "I prefer Fabrikam espresso over tea.", "created_at": 1757000300.5}])
    with canonical(lane) as conn:
        after = conn.execute("SELECT content,sender_type,conversation_id,source_id,metadata_json FROM ai_chat_messages "
                             "WHERE message_id=?", (PROMPT_ID,)).fetchone()
        batch = conn.execute("SELECT sync_batch_id FROM ai_chat_messages WHERE message_id=?", (PROMPT_ID,)).fetchone()[0]
    assert batch is not None  # the write did reach the row; only its body, role, source and marker were refused
    assert after == before
    assert qualify(lane, proved.fact_id).verdict == "qualified"
    outputs, error = source_adapter_read(lane, proved.authority, proved.fact_id, request_id="chatgpt-rewrite-read")
    assert error is None and released_text(outputs) == [(PROMPT_ID, "ai_chat_messages", OWNER_PROMPT)]


@pytest.mark.asyncio
async def test_revoking_the_enrollment_withholds_the_next_read(lane):
    proved = await prove(lane)
    revoked = await ingest(lane, {"operation": "revoke", "source_id": SOURCE,
                                  "enrollment_id": proved.run.enrollment.enrollment_id})
    assert (revoked.state, revoked.source_id) == ("revoked", SOURCE)
    result = qualify(lane, proved.fact_id)
    assert (result.verdict, result.reason_code) == ("withheld", "native_owner_provenance_unavailable")
    outputs, error = source_adapter_read(lane, proved.authority, proved.fact_id, request_id="chatgpt-revoked-read")
    assert (outputs, error) == ([], "native_owner_provenance_unavailable")


@pytest.mark.asyncio
async def test_the_imessage_doors_cannot_reach_a_chatgpt_enrollment_or_job(lane):
    run = await run_chatgpt_lane(lane, with_status=False)
    # Commands that omit the lane are iMessage commands, byte-identical to before.
    await ingest(lane, {"operation": "revoke", "enrollment_id": run.enrollment.enrollment_id},
                 expect_error="ingest_enrollment_unknown")
    await ingest(lane, {"operation": "enqueue", "enrollment_id": run.enrollment.enrollment_id},
                 expect_error="ingest_enrollment_unknown")
    await ingest(lane, {"operation": "status", "job_id": run.job.job_id}, expect_error="ingest_job_unknown")
    rerun = await ingest(lane, {"operation": "run", "job_id": run.job.job_id})
    assert rerun.model_dump() == {"status": "error", "reason_code": "snapshot_job_unavailable"}
    status = await ingest(lane, {"operation": "status", "source_id": SOURCE, "job_id": run.job.job_id})
    assert status.status == "done" and status.result.model_dump() == RESULT
    enrollment = await ingest(lane, {"operation": "revoke", "source_id": SOURCE, "enrollment_id": run.enrollment.enrollment_id})
    assert enrollment.state == "revoked"
