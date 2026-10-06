"""The correlated source-decision helper used by the knowledge-search release.

Historical capability names remain in the pure decision grammar for signed
custody records. The locator read adapter and its transport have been removed.
"""
from __future__ import annotations

from .canonical import PolicyError, digest
from .contract import (CAPABILITY, CAPABILITY_ATTESTED, CAPABILITY_OPAQUE, EVALUATOR_ATTESTED, EVALUATOR_OPAQUE,
    Decision, MessageDisclosure, Only, PolicyV2, VIEW, VIEW_OPAQUE, evaluate_predicate)
from .evidence import QualifiedEvidence, _key
from .identity import SUBJECT_CONTRACT_BY_CAPABILITY
from .registry import AttestedSubjectSourceDecision, OpaqueMessageDisclosure, OpaqueSubjectSourceDecision
from .search_contract import (CAPABILITY_SEARCH, EVALUATOR_SEARCH, SearchMemberDecision,
    CAPABILITY_MESSAGE_SEARCH, EVALUATOR_MESSAGE_SEARCH, DirectSearchMemberDecision,
    DIRECT_SEARCH_CAPABILITIES)
from .knowledge_contract import CAPABILITY_KNOWLEDGE, EVALUATOR_KNOWLEDGE, KnowledgeMemberDecision, VIEW_KNOWLEDGE

VOCABULARY = "owner-review-vocabulary/v1"
MAX_DISCLOSURE_BYTES = 256_000
# capability -> (decision class, evaluator version). Closed: a policy of any other
# capability, fact capabilities included, has no raw message decision at all.
SOURCE_DECISIONS = {CAPABILITY: (Decision, "hard-rules/p2a-v1"),
                    CAPABILITY_KNOWLEDGE: (KnowledgeMemberDecision, EVALUATOR_KNOWLEDGE),
                    CAPABILITY_ATTESTED: (AttestedSubjectSourceDecision, EVALUATOR_ATTESTED),
                    CAPABILITY_OPAQUE: (OpaqueSubjectSourceDecision, EVALUATOR_OPAQUE),
                    # p2c-v1 search re-decides each returned record's fact with this very function.
                    CAPABILITY_SEARCH: (SearchMemberDecision, EVALUATOR_SEARCH),
                    CAPABILITY_MESSAGE_SEARCH: (DirectSearchMemberDecision, EVALUATOR_MESSAGE_SEARCH)}
# Historical view names stay available to validate stored decision records;
# these are not a read dispatch table.
SOURCE_VIEWS = {CAPABILITY: (VIEW, MessageDisclosure), CAPABILITY_ATTESTED: (VIEW, MessageDisclosure),
                CAPABILITY_OPAQUE: (VIEW_OPAQUE, OpaqueMessageDisclosure)}


def source_view(capability: str) -> tuple:
    if capability == CAPABILITY_KNOWLEDGE:
        from .knowledge_contract import KnowledgeSearchResult
        return VIEW_KNOWLEDGE, KnowledgeSearchResult
    return SOURCE_VIEWS.get(capability, (VIEW, MessageDisclosure))


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
    Evidence qualified under a subject rule other than the one the policy's
    capability selects is refused, never evaluated.
    """
    if policy.versions.vocabulary != VOCABULARY:
        raise PolicyError("unsupported_vocabulary")
    capability = policy.versions.capability
    if capability not in SOURCE_DECISIONS:
        raise PolicyError("unsupported_capability")
    if capability in DIRECT_SEARCH_CAPABILITIES:
        from .message_evidence import QualifiedMessage
        if not isinstance(evidence, QualifiedMessage):
            raise PolicyError("evidence_family_mismatch")
    elif not isinstance(evidence, QualifiedEvidence):
        raise PolicyError("evidence_family_mismatch")
    if evidence.subject_contract != SUBJECT_CONTRACT_BY_CAPABILITY[capability]:
        raise PolicyError("subject_contract_mismatch")
    decision_class, evaluator_version = SOURCE_DECISIONS[capability]
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
            def permit_labels(item):
                attrs = labels[_key(item.identity)]
                return ([{**attrs, "domain": [domain]} for domain in attrs["domain"]]
                        if capability in DIRECT_SEARCH_CAPABILITIES else [attrs])
            values = [evaluate_predicate(rule.evidence_use.predicate, attrs) for item in closure for attrs in permit_labels(item)]
            values += [evaluate_predicate(rule.release.predicate, attrs) for item in snapshot.leaves for attrs in permit_labels(item)]
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
    return decision_class(stage="output_release", verdict=verdict, policy_hash=digest(policy.model_dump()),
        candidate_revision=digest({"snapshot": snapshot.model_dump(), "review_revision": evidence.review_revision}),
        evaluator_version=evaluator_version, matched_allow_clause_ids=allows[:1] if verdict == "permit" else [],
        matched_deny_clause_ids=denies, reason_code="rule_permit" if verdict == "permit" else "rule_deny" if verdict == "deny" else "unknown_context",
        required_projection_id=source_view(capability)[0] if verdict == "permit" else None,
        missing_context_codes=["classification"] if verdict == "indeterminate" else [])

