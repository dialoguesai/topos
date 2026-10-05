"""Deterministic Phase 3 answer schemas and signed vectors (invented data only).

Run from the node checkout: ``python -m tests.permissions_v2.answer_wire_fixtures``.
The control plane copies the resulting ``fixtures/permissions_v2/answers`` directory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.permissions_v2.test_knowledge_search import knowledge_policy
from topos.permissions_v2.answer_protocol import (ASK, FETCH, AnswerOnly, AnswerPending, AnswerWithSources,
    AskIntent, FetchIntent, NoAnswer, verify_node_answer)
from topos.permissions_v2.canonical import canonical_bytes, digest
from topos.permissions_v2.forwarding import ReleaseBody, sign_node_result
from topos.permissions_v2.knowledge_contract import JournalEntryResult, KnowledgeDeclaration, KnowledgePolicy
from topos.permissions_v2.signing import (AnswerRequestContext, AuthorityBinding, KnowledgeAnswerEnvelopeBody,
    SignedKnowledgeAnswerEnvelope, parse_authority, request_digest, sign_envelope)

ROOT = Path(__file__).resolve().parents[2] / "fixtures/permissions_v2/answers"
MODELS = (KnowledgeDeclaration, AskIntent, FetchIntent, AnswerPending, AnswerOnly, AnswerWithSources,
          NoAnswer, KnowledgeAnswerEnvelopeBody, SignedKnowledgeAnswerEnvelope, AnswerRequestContext)
NOW = 1_800_000_000


def _key(label: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode("ascii")).digest())


def _signed(policy, request_type, intent, request_id, cp_key):
    authority = parse_authority({**policy.binding.model_dump(), "grant_generation": 1,
        "assignment_generation": 1, "policy_version_id": policy.policy_version_id,
        "policy_hash": digest(policy.model_dump()), "capability_version": "permissions-beta/p2c-v3",
        "protection_revision": "b" * 64, "node_epoch": 3})
    body = KnowledgeAnswerEnvelopeBody.parse({**authority.model_dump(),
        "version": "topos-grantee-envelope/v2", "kid": "answer-cp-key", "request_id": request_id,
        "request_type": request_type, "request_hash": request_digest(request_type, intent),
        "issued_at": NOW, "expires_at": NOW + 100})
    return sign_envelope(body, cp_key)


def _result(envelope, output, node_key):
    authority = parse_authority({field: getattr(envelope, field) for field in AuthorityBinding.model_fields})
    body = ReleaseBody.parse({"version": "topos-node-disclosure/v1", "kid": "answer-node-key",
        "envelope_hash": digest(envelope.model_dump()), "request_id": envelope.request_id,
        "request_hash": envelope.request_hash, "authority": authority.model_dump(),
        "output_hash": digest(output.model_dump()), "checked_at": NOW + 1, "expires_at": NOW + 100})
    return sign_node_result(body, node_key)


def vector():
    cp_key, node_key = _key("topos-answer-golden/cp/v1"), _key("topos-answer-golden/node/v1")
    policies = {mode: KnowledgePolicy.parse(knowledge_policy(answers=mode))
                for mode in ("only", "with_sources")}
    question = AskIntent.parse({"question": "What is the invented plan?"})
    answer_id = "ans_" + "a" * 32
    fetch = FetchIntent.parse({"answer_id": answer_id})
    envelopes = {
        "ask": _signed(policies["only"], ASK, question.model_dump(), "answer-ask-one", cp_key),
        "fetch_only": _signed(policies["only"], FETCH, fetch.model_dump(), "answer-fetch-one", cp_key),
        "fetch_sources": _signed(policies["with_sources"], FETCH, fetch.model_dump(), "answer-fetch-sources", cp_key),
    }
    rid = "r." + "c" * 64
    record = JournalEntryResult.parse({"kind": "journal_entry", "record_id": rid,
        "content": "An invented trip was planned for Friday.", "source_ids": ["invented-journal"],
        "citations": [{"record_id": rid, "source_id": "invented-journal",
                       "content": "An invented trip was planned for Friday."}]})
    bodies = {
        "pending": AnswerPending(version="topos-answer/v1", state="pending", answer_id=answer_id),
        "only": AnswerOnly(version="topos-answer/v1", outcome="answered", answer="The invented trip is on Friday."),
        "with_sources": AnswerWithSources(version="topos-answer/v1", outcome="answered",
            answer="The invented trip is on Friday [1].", records=[record]),
        "no_answer": NoAnswer(version="topos-answer/v1", outcome="no_answer"),
    }
    links = {"pending": "ask", "only": "fetch_only", "with_sources": "fetch_sources", "no_answer": "fetch_only"}
    results = {name: _result(envelopes[links[name]], body, node_key) for name, body in bodies.items()}
    public = {"cp": cp_key.public_key().public_bytes_raw().hex(),
              "node": node_key.public_key().public_bytes_raw().hex()}
    for name, body in bodies.items():
        envelope = envelopes[links[name]]
        verify_node_answer(results[name], trusted_keys={"answer-node-key": bytes.fromhex(public["node"])},
            envelope=envelope, output=body, mode="with_sources" if name == "with_sources" else "only", now=NOW + 2)
    return {"version": "answer-golden-v1", "now": NOW + 2, "public_keys_hex": public,
        "policies": {mode: {"policy": policy.model_dump(), "canonical": canonical_bytes(policy.model_dump()).decode("ascii"),
                             "hash": digest(policy.model_dump())} for mode, policy in policies.items()},
        "intents": {"ask": question.model_dump(), "fetch": fetch.model_dump()},
        "envelopes": {name: item.model_dump() for name, item in envelopes.items()},
        "bodies": {name: body.model_dump() for name, body in bodies.items()},
        "results": {name: result.model_dump() for name, result in results.items()}, "links": links}


def write():
    ROOT.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        (ROOT / f"{model.__name__}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n", encoding="ascii")
    (ROOT / "answer-golden-v1.json").write_text(json.dumps(vector(), indent=2, sort_keys=True) + "\n", encoding="ascii")


if __name__ == "__main__":
    write()
