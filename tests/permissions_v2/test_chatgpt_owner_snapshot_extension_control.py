"""The extension's own AI-chat row is not owner speech, even where nothing else would stop its release.

The canary's app_ingest negative writes the SAME text as a lane prompt under a
grant over the lane's source only, so two other checks also withhold it (the
independent-copy veto and the policy's source universe): with the lane proof
removed it still withholds, and only its reason code notices. Here the prompt is
new text, in the owner's own conversation, written through the relay app_ingest
handler, under a p2a-v1 grant whose source universe covers both the lane and the
extension. The lane prompt releases under that same grant, so the only thing
standing between the extension row and a recipient is the lane proof.
"""
from __future__ import annotations

from copy import deepcopy
import json
import time

import pytest

from tests.ingestion.test_chatgpt_owner_snapshot import OWNER_PROMPT
from tests.permissions_v2.test_chatgpt_owner_snapshot_canary import (
    PROMPT_ID, SOURCE, app_ingest, chatgpt_source_policy, qualify, released_text, review, run_chatgpt_lane, seed_locator)
from tests.permissions_v2.test_ingest_snapshot_work_canary import (  # noqa: F401 (fixtures)
    CP_ISSUER, FRONTEND, OWNER_ID, _next, canonical, corpus, lane, paired_runtime, projection_runtime, protocol_call)
from tests.permissions_v2.test_source_release_sibling_lane import source_adapter_read, source_socket_read
from topos.permissions_v2.canonical import digest
from topos.permissions_v2.protocol import MutationBody, StatusRequestBody, sign_mutation, sign_status_request

EXTENSION = "chatgpt_ui_conversation"
EXTENSION_ID = "extension-new-prompt-1"
EXTENSION_TEXT = "I prefer Fabrikam espresso over Contoso oolong on Mondays."


async def grant_over_lane_and_extension(lane):
    """The canary's p2a-v1 grant, widened to the extension's source."""
    now = int(time.time())
    raw = chatgpt_source_policy(lane, now)
    raw["policy_version_id"] = "chatgpt-two-source-policy-1"
    sources = sorted([SOURCE, EXTENSION])
    raw["source_universe"]["source_ids"] = deepcopy(sources)
    raw["rules"][0]["evidence_use"]["sources"]["values"] = deepcopy(sources)
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


@pytest.mark.asyncio
async def test_a_new_prompt_through_app_ingest_is_withheld_only_by_the_lane_proof(lane, monkeypatch):
    run = await run_chatgpt_lane(lane)
    assert run.result.status == "ok"
    await app_ingest(lane, monkeypatch, [{"id": EXTENSION_ID, "thread_id": "extension-thread-1", "role": "user",
                                          "content": EXTENSION_TEXT, "created_at": 1757000400.5}])
    with canonical(lane) as conn:
        row = conn.execute("SELECT m.sender_type,m.source_id,c.owner_user_id,c.source_id FROM ai_chat_messages m JOIN "
                           "ai_chat_conversations c ON c.conversation_id=m.conversation_id WHERE m.message_id=?",
                           (EXTENSION_ID,)).fetchone()
        copies = sum(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE content=?", (EXTENSION_TEXT,)).fetchone()[0]
                     for table in ("ai_chat_messages", "conversation_messages"))
    # The owner's conversation, a "human" row, and no copy anywhere for the copy veto to find.
    assert tuple(row) == ("human", EXTENSION, OWNER_ID, EXTENSION) and copies == 1

    lane_fact = seed_locator(lane, PROMPT_ID, "Contoso oolong")
    await review(lane_fact, review_id="two-source-lane-review")
    extension_fact = seed_locator(lane, EXTENSION_ID, "Fabrikam espresso", source_id=EXTENSION)
    _snapshot, recorded = await review(extension_fact, review_id="two-source-extension-review")
    authority = await grant_over_lane_and_extension(lane)

    # The same grant releases the lane prompt: it is not what withholds the extension row.
    outputs, error = source_adapter_read(lane, authority, lane_fact, request_id="two-source-lane-read")
    assert error is None and released_text(outputs) == [(PROMPT_ID, "ai_chat_messages", OWNER_PROMPT)]

    # Read first: without the lane proof this is where the extension's text would leave the node.
    outputs, error = source_adapter_read(lane, authority, extension_fact, request_id="two-source-extension-read")
    assert (outputs, error) == ([], "not_owner_authored")
    assert recorded["state"]["qualification"]["reason_code"] == "not_owner_authored"
    assert (qualify(lane, extension_fact).verdict, qualify(lane, extension_fact).reason_code) == (
        "withheld", "not_owner_authored")
    frames = await source_socket_read(lane, authority, extension_fact, request_id="two-source-extension-socket",
                                      monkeypatch=monkeypatch)
    assert [frame["status"] for frame in frames] == ["error"] and EXTENSION_TEXT not in json.dumps(frames)
