"""Public, deliberately synthetic fixtures. No copied corpus or environment IO."""
from __future__ import annotations

import json

from ..canonical import digest
from ..contract import CAPABILITY
from .evaluators import ModelResponse, PROMPT_REVISION
from .models import Candidate, EvaluatorConfig, ExperimentPolicy, FORM, RequestContext, TrustedSnapshot


def fixture():
    authority = {"environment_id": "permissions-beta-offline", "node_id": "synthetic-node", "resource_id": "synthetic-resource",
        "owner_id": "synthetic-owner", "actor_id": "synthetic-book-reader", "client_id": "synthetic-client",
        "grant_id": "synthetic-grant", "assignment_id": "synthetic-assignment", "grant_generation": 1, "assignment_generation": 1,
        "policy_version_id": "synthetic-policy", "policy_hash": "1"*64, "capability_version": CAPABILITY,
        "protection_revision": "2"*64, "node_epoch": 1}
    atom = lambda value: {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": [value]}
    policy = {"version": "topos-offline-experiment/v1", "experiment_id": "synthetic-books-v1", "authority": authority,
        "validity": {"starts_at": 1000, "expires_at": 5000}, "source_universe": ["synthetic-books", "synthetic-journal"],
        "processor": "owner-engine-local", "rules": [
            {"clause_id": "reading", "effect": "permit", "sources": ["synthetic-books", "synthetic-journal"], "evidence_predicate": atom("reading"), "output_predicate": atom("reading"), "forms": [FORM]},
            {"clause_id": "private-health", "effect": "deny", "sources": ["synthetic-books", "synthetic-journal"], "evidence_predicate": atom("health"), "output_predicate": atom("health"), "forms": [FORM]}],
        "prose": {"original": "Share my reading information. Exclude my private health information and private reasons for reading.",
            "inclusions": [{"clause_id": "reading", "text": "My book reading information."}],
            "exclusions": [{"clause_id": "private-health", "text": "Private health information and private reasons for reading."}],
            "examples": [{"example_id": "book-example", "clause_id": "reading", "verdict": "permit", "role": "illustration", "text": "I am reading a science-fiction novel."},
                         {"example_id": "health-example", "clause_id": "private-health", "verdict": "deny", "role": "illustration", "text": "A private diagnosis that explains why I chose a book."}]}}
    policy["owner_approved_revision"] = digest(policy)
    policy = ExperimentPolicy.parse(policy)
    shared = {"record_revision": "3"*64, "attributes": {"domain": ["reading"]}, "text": "Synthetic owner is reading a science-fiction novel.", "owner_only": False}
    candidate = Candidate.parse({"candidate_id": "synthetic-fact", "qualification_revision": "4"*64, "lineage_revision": "5"*64,
        "lineage_state": "qualified", "protection_revision": authority["protection_revision"], "form": FORM,
        "evidence": [{**shared, "unit_id": "synthetic-leaf", "source_id": "synthetic-books", "table": "conversation_messages"}],
        "output": {**shared, "unit_id": "synthetic-projection"}})
    snapshot = TrustedSnapshot(current_authority=policy.authority, candidate=candidate)
    context = RequestContext(authority=policy.authority, request_id="synthetic-request", request_hash="6"*64, as_of=1100, processor="owner-engine-local")
    return policy, snapshot, context


def config(arm):
    return EvaluatorConfig.parse({"arm": arm, "evaluator_version": "rules-experiment/v1" if arm == "rules_v2" else "semantic-experiment/v1",
        "session_id": "synthetic-session", "model_id": "deterministic-test-fixture" if arm == "semantic_v1" else None,
        "model_revision": "7"*64 if arm == "semantic_v1" else None, "prompt_revision": PROMPT_REVISION if arm == "semantic_v1" else None,
        "timeout_ms": 100, "max_prompt_bytes": 32768, "max_response_bytes": 4096, "max_output_tokens": 512, "temperature": 0})


class DeterministicTestTransport:
    """Always permits the reading clause. Not a classifier or accuracy fixture."""
    async def complete(self, request):
        return ModelResponse(arm=request.arm, model_id=request.model_id, model_revision=request.model_revision,
            prompt_revision=request.prompt_revision, body=json.dumps({"verdict": "permit", "matched_allow_clause_ids": ["reading"],
                "matched_deny_clause_ids": [], "required_projection_id": FORM, "missing_context_codes": []}))
