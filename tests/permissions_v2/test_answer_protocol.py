"""The answer wire must reject bodies or identities outside the signed share."""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from topos.permissions_v2.answer_protocol import (ASK, FETCH, AnswerPending, parse_answer_output,
    verify_node_answer)
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.forwarding import ReleaseBody, sign_node_result
from topos.permissions_v2.knowledge_contract import KnowledgeDeclaration
from topos.permissions_v2.signing import (AuthorityBinding, KnowledgeAnswerEnvelopeBody,
    parse_envelope, request_digest, sign_envelope, verify_current_signature)

WINDOW = {"kind": "rolling", "anchor": "server_request_as_of", "max_age_seconds": 86400,
          "event_time_semantics": "canonical_event_time_v1", "missing_or_ambiguous": "withhold", "future": "withhold"}
DECLARATION = {"view_id": "canonical.knowledge_search.v1", "max_permitted_records": 10, "max_k": 8,
    "window": WINDOW, "time_semantics": "underlying_evidence_time/v1", "tables": ["journal_entries"],
    "result_types": ["journal_entry"]}


def _signed(request_type=ASK, payload=None):
    payload = payload or {"question": "What was planned?"}
    key = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    body = KnowledgeAnswerEnvelopeBody.parse({
        "version": "topos-grantee-envelope/v2", "kid": "test-key", "request_id": "request-1",
        "request_type": request_type, "request_hash": request_digest(request_type, payload),
        "issued_at": 100, "expires_at": 200, "environment_id": "environment-1", "node_id": "node-1",
        "resource_id": "resource-1", "owner_id": "owner-1", "actor_id": "actor-1",
        "client_id": "client-1", "grant_id": "grant-1", "assignment_id": "assignment-1",
        "grant_generation": 1, "assignment_generation": 1, "policy_version_id": "policy-1",
        "policy_hash": "a" * 64, "capability_version": "permissions-beta/p2c-v3",
        "protection_revision": "b" * 64, "node_epoch": 1})
    return sign_envelope(body, key), key


def test_optional_mode_preserves_old_signed_bytes_and_null_is_not_absent():
    old = KnowledgeDeclaration.parse(DECLARATION)
    assert "answers" not in old.model_dump()
    assert KnowledgeDeclaration.parse({**DECLARATION, "answers": "only"}).model_dump()["answers"] == "only"
    assert digest(old.model_dump()) != digest(KnowledgeDeclaration.parse({**DECLARATION, "answers": "only"}).model_dump())
    with pytest.raises(PolicyError):
        KnowledgeDeclaration.parse({**DECLARATION, "answers": None})


def test_answer_request_type_is_signed_and_not_search():
    signed, key = _signed()
    assert parse_envelope(signed.model_dump()).request_type == ASK
    assert type(parse_envelope(signed.model_dump())).__name__ == "SignedKnowledgeAnswerEnvelope"
    public = key.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                           format=serialization.PublicFormat.Raw)
    with pytest.raises(PolicyError):
        verify_current_signature(parse_envelope({**signed.model_dump(), "request_type": "permissions.v2.search"}),
                                 trusted_keys={"test-key": public}, now=120)


def test_only_body_rejects_citations_extra_fields_and_control_characters():
    body = {"version": "topos-answer/v1", "outcome": "answered", "answer": "The trip is on Friday."}
    assert parse_answer_output(body, request_type=FETCH, mode="only").answer == body["answer"]
    for changed in ({**body, "answer": "The trip is on Friday [1]."},
                    {**body, "answer": "The trip is on\tFriday."},
                    {**body, "records": []}):
        with pytest.raises(PolicyError, match="answer_output_invalid"):
            parse_answer_output(changed, request_type=FETCH, mode="only")


def test_ask_never_returns_a_final_body_and_fetch_is_bound_to_mode():
    pending = {"version": "topos-answer/v1", "state": "pending", "answer_id": "ans_" + "a" * 32}
    assert isinstance(parse_answer_output(pending, request_type=ASK, mode="only"), AnswerPending)
    with pytest.raises(PolicyError):
        parse_answer_output({"version": "topos-answer/v1", "outcome": "no_answer"}, request_type=ASK, mode="only")
    with pytest.raises(PolicyError):
        parse_answer_output({**pending, "answer_id": "ans_wrong"}, request_type=FETCH, mode="only")


def test_node_result_uses_the_exact_answer_envelope_and_node_key():
    payload = {"answer_id": "ans_" + "c" * 32}
    signed, key = _signed(FETCH, payload)
    output = parse_answer_output({"version": "topos-answer/v1", "outcome": "no_answer"},
                                 request_type=FETCH, mode="only")
    authority = {field: getattr(signed, field) for field in AuthorityBinding.model_fields}
    result = sign_node_result(ReleaseBody.parse({"version": "topos-node-disclosure/v1", "kid": "node-key",
        "envelope_hash": digest(signed.model_dump()), "request_id": signed.request_id,
        "request_hash": signed.request_hash, "authority": authority, "output_hash": digest(output.model_dump()),
        "checked_at": 110, "expires_at": 190}), key)
    public = key.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                           format=serialization.PublicFormat.Raw)
    verify_node_answer(result, trusted_keys={"node-key": public}, envelope=signed, output=output,
                       mode="only", now=120)
    with pytest.raises(PolicyError):
        verify_node_answer(result, trusted_keys={"node-key": bytes([1]) * 32}, envelope=signed,
                           output=output, mode="only", now=120)
    with pytest.raises(PolicyError):
        verify_node_answer(result, trusted_keys={"node-key": public}, envelope=signed,
                           output={"version": "topos-answer/v1", "outcome": "answered", "answer": "Changed."},
                           mode="only", now=120)
