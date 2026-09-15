"""Closed, pure P2b fact policy foundation; no ledger or transport registration.

The serving adapter must supply fresh resolver-owned rows, current authenticated
output review, exact verified authority binding, and signed envelope.issued_at.
This function checks their consistency and policy meaning, not their provenance.
Its decision is not a release permit without the final authority/transport gates.
"""
from __future__ import annotations

from .contract import Binding, evaluate_predicate
from .evidence import QualifiedEvidence, _key
from .fact_projection import ReviewedFactProjection
from .fact_contract import (CAPABILITY, EVALUATOR, EVALUATOR_STATED_DAY, VOCABULARY, PURPOSE, PROJECTION_VERSION, VIEW,
    FactPolicyV2, FactDecision, FactEvidenceUse, FactOutputForm, RollingEventWindow,
    StatedDayFactDecision, StatedDayFactPolicy)
from .fact_eligibility import PermitStructure, prepare_fact_eligibility, canonical_utc_microseconds

def _attributes(classification):
    return {"domain": classification.domains, "actor_role": ["authored"],
        "subject": ["owner"], "sensitivity": [classification.sensitivity]}


def _and(values):
    return False if False in values else None if None in values else True


def _or(values):
    return True if True in values else None if None in values else False


def fact_projection_decision(*, policy: FactPolicyV2, evidence: QualifiedEvidence,
    projection: ReviewedFactProjection, rows: dict, binding: Binding,
    request_as_of: int, now: int) -> FactDecision:
    """Pure hard-rule membership over one correlated structural context.

    The serving adapter supplies fresh resolver/review-owned inputs and signed
    request issuance. Preparation is consistency checking, not authentication
    or a model-use permit. Result shape, precedence and ordering remain P2b v1.
    A stated-day (v2) policy only changes which `valid_from` values count as
    current and stamps its own evaluator version on the decision.
    """
    policy, evidence, projection, structure = prepare_fact_eligibility(policy=policy,
        evidence=evidence, projection=projection, rows=rows, binding=binding,
        request_as_of=request_as_of, now=now)
    stated_day = isinstance(policy, StatedDayFactPolicy)
    model, evaluator = (StatedDayFactDecision, EVALUATOR_STATED_DAY) if stated_day else (FactDecision, EVALUATOR)
    def result(verdict, reason, allows=(), denies=(), missing=()):
        return model(stage="output_release", verdict=verdict, policy_hash=structure.policy_hash,
            candidate_revision=structure.candidate_revision, evaluator_version=evaluator,
            matched_allow_clause_ids=list(allows[:1]) if verdict == "permit" else [],
            matched_deny_clause_ids=list(denies), reason_code=reason,
            required_projection_id=VIEW if verdict == "permit" else None, missing_context_codes=list(missing))
    if structure.terminal_reason:
        return result("deny", structure.terminal_reason)
    labels = {_key(item.evidence.identity): _attributes(item) for item in evidence.classifications}
    output_labels = _attributes(projection.classification)
    allows, denies, unknown_allow, unknown_deny = [], [], False, False
    missing = {"fact_validity"} if structure.fact_times_unknown else set()
    for clause in structure.clauses:
        rule = policy.rules[clause.rule_index]
        if isinstance(clause, PermitStructure):
            values = [evaluate_predicate(rule.evidence_use.predicate, labels[key]) for key in clause.evidence_keys]
            values += [evaluate_predicate(rule.release.predicate, output_labels)]
            values += list(clause.leaf_times) + ([None] if structure.fact_times_unknown else [])
            match = _and(values)
            if match is True:
                allows.append(rule.rule_id)
            elif match is None:
                unknown_allow = True
            if None in clause.leaf_times:
                missing.add("time")
        else:
            values = [_and([time_match, evaluate_predicate(rule.evidence_use.predicate, labels[key])])
                for key, time_match in clause.evidence_times]
            values.append(_and([clause.output_time, evaluate_predicate(rule.release.predicate, output_labels)]))
            match = _or(values)
            if match is True:
                denies.append(rule.rule_id)
            elif match is None:
                unknown_deny = True
                missing.add("time")
    if denies:
        return result("deny", "rule_deny", denies=denies)
    if unknown_deny or structure.fact_times_unknown:
        return result("indeterminate", "unknown_context", missing=sorted(missing))
    if allows:
        return result("permit", "rule_permit", allows=allows)
    if unknown_allow:
        return result("indeterminate", "unknown_context", missing=sorted(missing))
    return result("deny", "unsupported_view" if structure.unsupported_view else "rule_deny")
