"""One owner sentence, two facts: the raw message may not outrun its owner-only sibling.

"I work at Northwind <hex12> and I live in Contoso City." is one owner-authored
iMessage. The snapshot lane's real rules extractor writes a scoped ``works_at``
fact and an ``owner_only`` ``lives_in`` fact over that same message. A
p2a-v1 read located by the scoped fact returns the whole message, so it would
disclose the owner-only claim; it must withhold. A p2b-v4 read of the same
scoped fact releases only the employer scalar, which says nothing about where
the owner lives, so it must keep releasing.

Every step reuses the work canary's signed owner doors (identity, ingest,
evidence review, output review, signed status/mutate grant). The p2a recipient
read goes through ``release_transport.dispatch_source_message`` with a CP stamp
and a socket double, and once more through ``SourceMessageRelease`` as that
transport builds it, which is where the withholding reason is visible.
"""
from __future__ import annotations

from copy import deepcopy
import json
import time

import pytest

from tests.permissions_v2.test_ingest_snapshot_work_canary import (  # noqa: F401 (fixtures)
    CP_ISSUER, FRONTEND, SELF_ENTITY, _next, attest_selves, corpus, expected_scalar, facts, lane, paired_runtime,
    projection_runtime, protocol_call, recipient_read, review_evidence, review_output, run_lane, signed_grant,
    spy_lane_stats)
from tests.permissions_v2.test_contract_and_ledger import sample_policy
from tests.permissions_v2.test_release import dispatch as source_dispatch
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.protocol import MutationBody, StatusRequestBody, sign_mutation, sign_status_request
from tests.permissions_v2.retired_doors import SourceMessageRelease
from topos.permissions_v2.release import VOCABULARY
from topos.permissions_v2.signing import EnvelopeBody, request_digest, sign_envelope

HOME = "Contoso City"


def two_claims(lane):
    return [(1, f"I work at {lane.employer} and I live in {HOME}."), (0, "Synthetic reply from a correspondent.")]


async def two_facts(lane, monkeypatch):
    """The real lane over the two-claim sentence. Returns (works_at, lives_in)."""
    await attest_selves(lane, [SELF_ENTITY])
    stats = spy_lane_stats(monkeypatch)
    await run_lane(lane, two_claims(lane))
    assert stats == [{"rows_linked": 2, "facts_written": 2}]
    by_predicate = {fact.payload["predicate"]: fact for fact in facts(lane)}
    work, home = by_predicate["works_at"], by_predicate["lives_in"]
    assert (work.payload["object_value"], work.payload["disclosure"]) == (lane.employer, "scoped")
    assert (home.payload["object_value"], home.payload["disclosure"]) == (HOME, "owner_only")
    # One message backs both claims, through identical references.
    assert work.refs == home.refs and [ref["record_id"] for ref in work.refs] == ["imessage:1"]
    return work, home


def source_policy(lane, now):
    """A p2a-v1 raw message grant over this lane's source, table and reviewed domain."""
    raw = sample_policy()
    raw["binding"].update(lane.runtime.protocol.ledger.identity.model_dump())
    raw["policy_version_id"] = "sibling-source-policy-1"
    raw["versions"]["vocabulary"] = VOCABULARY
    raw["validity"] = {"starts_at": now - 3600, "expires_at": now + 3600}
    raw["source_universe"]["source_ids"] = ["imessage"]
    work = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["work"]}
    rule = raw["rules"][0]
    rule["evidence_use"]["sources"]["values"] = ["imessage"]
    rule["evidence_use"]["predicate"] = deepcopy(work)
    rule["release"]["predicate"] = deepcopy(work)
    return raw


async def signed_source_grant(lane):
    """The canary's CP status/mutate pair, activating a p2a-v1 grant instead of p2b-v4."""
    now = int(time.time())
    raw = source_policy(lane, now)
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


def source_envelope(lane, authority, fact_id, *, request_id, issued):
    payload = {"query": "fact:" + fact_id}
    envelope = sign_envelope(EnvelopeBody.parse({**authority.model_dump(), "version": "topos-grantee-envelope/v2",
        "kid": "cp-key", "request_id": request_id, "request_type": "permissions.v2.read",
        "request_hash": request_digest("permissions.v2.read", payload),
        "issued_at": issued, "expires_at": issued + 100}), lane.cp_key)
    return envelope, payload


def source_adapter_read(lane, authority, fact_id, *, request_id):
    """SourceMessageRelease exactly as release_transport builds it. Returns (outputs, error code)."""
    service = lane.runtime.evidence_reviews(require_existing=True)
    release = SourceMessageRelease(protocol=lane.runtime.protocol, resolver=service.resolver,
                                   reviews=service.reviews, clock=lambda: int(time.time()))
    envelope, payload = source_envelope(lane, authority, fact_id, request_id=request_id, issued=int(time.time()))
    try:
        return source_dispatch((release,), envelope, payload, request_id=request_id), None
    except PolicyError as exc:
        return [], exc.code


@pytest.mark.asyncio
async def test_the_raw_message_behind_a_scoped_fact_is_withheld_when_it_also_backs_an_owner_only_fact(
        lane, monkeypatch):
    work, home = await two_facts(lane, monkeypatch)
    # The owner keeps the lives_in claim to themselves: under implicit review that is a
    # deselection (an owner_only disclosure alone is the owner's own claim and no bar).
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user=lane.runtime.protocol.ledger.identity.owner_id))
    try:
        store = lane.runtime.evidence_reviews(require_existing=False).reviews
        assert store.opt_out(home.object_id, now=int(time.time())) is True
    finally:
        reset_principal(token)
    # The owner reviews the scoped works_at fact: its closure is that fact and
    # the one message, and it fully qualifies. The deselected sibling is not in it.
    snapshot, recorded = await review_evidence(work.object_id)
    assert [item.identity.record_id for item in snapshot.leaves] == ["imessage:1"]
    assert [item.identity.record_id for item in snapshot.artifacts] == [work.object_id]
    assert recorded["state"]["qualification"]["verdict"] == "qualified"
    authority = await signed_source_grant(lane)

    # Before the sibling floor this read released the whole sentence, "I live in Contoso City" included.
    outputs, error = source_adapter_read(lane, authority, work.object_id, request_id="sibling-adapter-1")
    assert (outputs, error) == ([], "owner_opted_out")


@pytest.mark.asyncio
async def test_the_work_scalar_still_releases_over_a_message_that_backs_an_owner_only_fact(lane, monkeypatch):
    """p2b-v4 releases one reviewed employer label, never the message text."""
    work, _home = await two_facts(lane, monkeypatch)
    await review_evidence(work.object_id)
    await review_output(work.object_id)
    authority = await signed_grant(lane)
    outputs, error = recipient_read(lane, authority, work.object_id, request_id="sibling-scalar-1")
    assert error is None
    [(result, output)] = outputs
    assert output == expected_scalar(lane)
    assert result["authority"]["capability_version"] == "permissions-beta/p2b-v4"
    for private in (HOME, "I live in", "I work at"):
        assert private not in json.dumps(output) and private not in json.dumps(result)
