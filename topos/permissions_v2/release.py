"""Bounded source-message disclosure through the signed P2a policy boundary.

The locator is a scoped, owner-reviewed fact, but the output is its complete set
of terminal canonical messages. This does not add a fact, summary or NL form.
The callback is the trusted transport send itself, never a permit consumer.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from pydantic import StringConstraints

from topos.principal import THIRD_PARTY, current_principal
from topos.storage.db.write_gate import with_db_write

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Decision, MessageDisclosure, Only, PolicyV2, StrictModel, VIEW, evaluate_predicate
from .evidence import EvidenceResolver, EvidenceReviewStore, QualifiedEvidence, _key
from .forwarding import ReleaseBody, sign_node_result
from .node_protocol import NodePolicyProtocol
from .signing import AuthorityBinding, RequestContext, SignedEnvelope, verify_current_signature

VOCABULARY = "owner-review-vocabulary/v1"
MAX_DISCLOSURE_BYTES = 256_000


class SourceMessageIntent(StrictModel):
    query: Annotated[str, StringConstraints(strict=True, max_length=8_000)]

    def fact_id(self) -> str:
        from .contract import Identifier

        class Locator(StrictModel):
            value: Identifier
        if not self.query.startswith("fact:"):
            raise PolicyError("unsupported_query")
        return Locator.parse({"value": self.query[5:]}).value


def _attributes(classification) -> dict[str, list[str]]:
    # Qualification independently proves native owner authorship and owner-only
    # subjects. These are documented vocabulary values, never pack-name guesses.
    return {"domain": classification.domains, "actor_role": ["authored"],
            "subject": ["owner"], "sensitivity": [classification.sensitivity]}


def _rule_sources(rule, policy):
    selection = rule.evidence_use.sources
    return set(selection.values if isinstance(selection, Only) else policy.source_universe.source_ids)


def _tables(rule):
    return {table for form in rule.release.forms for table in form.tables}


def source_message_decision(policy: PolicyV2, evidence: QualifiedEvidence) -> Decision:
    """Evaluate whole correlated clauses against every qualified contributing row.

    This helper cannot grant access: only the service obtains trusted evidence.
    A denial on any selected terminal source withholds the whole unredacted
    result. For derived artifacts, exclusions apply conservatively to the entire
    contributing closure when a deny source overlaps; no partial recomputation.
    """
    if policy.versions.vocabulary != VOCABULARY:
        raise PolicyError("unsupported_vocabulary")
    snapshot = evidence.snapshot
    labels = {_key(item.evidence.identity): _attributes(item) for item in evidence.classifications}
    closure = snapshot.artifacts + snapshot.leaves
    if not snapshot.leaves or len(labels) != len(closure):
        raise PolicyError("classification_incomplete")
    selected_keys = {_key(item.identity) for item in closure}
    if set(labels) != selected_keys:
        raise PolicyError("classification_incomplete")
    all_sources = {item.identity.source_id for item in snapshot.leaves}
    all_tables = {item.identity.table for item in snapshot.leaves}
    allows, denies, unknown_deny, unknown_allow = [], [], False, False
    for rule in policy.rules:
        sources, tables = _rule_sources(rule, policy), _tables(rule)
        if "owner-engine-local" not in rule.evidence_use.processors.values:
            continue
        if rule.effect == "permit":
            if (rule.release.ceiling != "raw" or not sources or not tables
                or not all_sources <= sources or not all_tables <= tables):
                continue
            values = [evaluate_predicate(rule.evidence_use.predicate, labels[_key(item.identity)]) for item in closure]
            values += [evaluate_predicate(rule.release.predicate, labels[_key(item.identity)]) for item in snapshot.leaves]
            if all(value is True for value in values):
                allows.append(rule.rule_id)
            elif False not in values and None in values:
                unknown_allow = True
        else:
            selected = [item for item in snapshot.leaves if item.identity.source_id in sources and item.identity.table in tables]
            if not selected:
                continue
            values = [evaluate_predicate(rule.evidence_use.predicate, labels[_key(item.identity)]) for item in closure]
            values += [evaluate_predicate(rule.release.predicate, labels[_key(item.identity)]) for item in selected]
            if True in values:
                denies.append(rule.rule_id)
            elif None in values:
                unknown_deny = True
    verdict = "deny" if denies else "indeterminate" if unknown_deny else "permit" if allows else "indeterminate" if unknown_allow else "deny"
    return Decision(stage="output_release", verdict=verdict, policy_hash=digest(policy.model_dump()),
        candidate_revision=digest({"snapshot": snapshot.model_dump(), "review_revision": evidence.review_revision}),
        evaluator_version="hard-rules/p2a-v1", matched_allow_clause_ids=allows[:1] if verdict == "permit" else [],
        matched_deny_clause_ids=denies, reason_code="rule_permit" if verdict == "permit" else "rule_deny" if verdict == "deny" else "unknown_context",
        required_projection_id=VIEW if verdict == "permit" else None,
        missing_context_codes=["classification"] if verdict == "indeterminate" else [])


class SourceMessageRelease:
    """Single-process node adapter; constructed with trusted runtime services.

    send(result, output) must perform the bounded CP transport dispatch before
    returning. It must not enqueue a later sender. Revocations/protection writes
    serialized by the node write gate before this dispatch win. A failed or
    uncertain send consumes the request, and a retry needs a fresh CP issuance.
    No model, fallback query engine or source search is reachable here.
    """
    def __init__(self, *, protocol: NodePolicyProtocol, resolver: EvidenceResolver,
                 reviews: EvidenceReviewStore, clock: Callable[[], int]):
        if (protocol.ledger.identity.model_dump() != resolver.binding.model_dump()
            or protocol.canonical_database != resolver.path.resolve(strict=True)
            or reviews.binding != resolver.binding):
            raise PolicyError("release_service_binding")
        self.protocol, self.resolver, self.reviews, self.clock = protocol, resolver, reviews, clock

    def dispatch(self, *, envelope: dict, payload: dict, request_id: str, send: Callable) -> None:
        principal = current_principal()
        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"
            or not principal.acting_user or not principal.client_id):
            raise PolicyError("recipient_relay_required")
        intent = SourceMessageIntent.parse(payload)
        fact_id = intent.fact_id()
        signed = SignedEnvelope.parse(envelope)
        if signed.request_type != "permissions.v2.read":
            raise PolicyError("unsupported_query")
        ledger = self.protocol.ledger
        request = RequestContext.parse({**ledger.identity.model_dump(), "actor_id": principal.acting_user,
            "client_id": principal.client_id, "grant_id": signed.grant_id, "assignment_id": signed.assignment_id,
            "request_id": request_id, "request_type": "permissions.v2.read"})
        with with_db_write():
            # Protection changes advance effective authority before admission.
            with ledger._transaction() as db:
                self.protocol._sync_protection(db)
            lease = ledger.admit(envelope, request=request, payload=intent.model_dump(), now=self.clock())

            def release(qualified, rows):
                with ledger._transaction() as db:
                    self.protocol._sync_protection(db)
                    authority, policy = ledger._authority(db, signed.grant_id, self.clock())
                    # The snapshot binds its closure's protection history; signed
                    # authority binds the node-wide revision of this very read.
                    if (authority != AuthorityBinding.parse({field: getattr(signed, field) for field in AuthorityBinding.model_fields})
                        or self.resolver.current_floor is None or self.resolver.current_floor != authority.protection_revision):
                        raise PolicyError("authority_stale")
                decision = source_message_decision(policy, qualified)
                if decision.verdict != "permit":
                    ledger.checkpoint_decision(lease, decision.model_dump(), candidate_revision=decision.candidate_revision,
                                               output=None, now=self.clock())
                    raise PolicyError("permission_denied")
                records = []
                for ref in qualified.snapshot.leaves:
                    row = rows[_key(ref.identity)]
                    records.append({"record_id": ref.identity.record_id, "source_id": ref.identity.source_id,
                                    "canonical_table": ref.identity.table, "content": row.get("content")})
                output = MessageDisclosure.parse({"family": "canonical_record", "operation": "read", "view_id": VIEW, "records": records})
                if not records or len(canonical_bytes(output.model_dump())) > MAX_DISCLOSURE_BYTES:
                    raise PolicyError("disclosure_budget")
                # This durable one-shot checkpoint precedes transport so an
                # uncertain send cannot be replayed after restart. The outer
                # resolver callback still holds evidence/review/write gates.
                ledger.checkpoint_decision(lease, decision.model_dump(), candidate_revision=decision.candidate_revision,
                                           output=output.model_dump(), now=self.clock())
                checked_at = self.clock()
                verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=checked_at)
                result = sign_node_result(ReleaseBody(version="topos-node-disclosure/v1",
                    kid=self.protocol.node_signing_kid, envelope_hash=digest(signed.model_dump()),
                    request_id=request_id, request_hash=signed.request_hash, authority=authority,
                    output_hash=digest(output.model_dump()), checked_at=checked_at, expires_at=signed.expires_at),
                    self.protocol.node_signing_key)
                send(result.model_dump(), output.model_dump())

            self.resolver.with_qualified(fact_id, reviews=self.reviews, callback=release)
