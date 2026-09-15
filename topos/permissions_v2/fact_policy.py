"""Closed, pure P2b fact policy foundation; no ledger or transport registration.

The serving adapter must supply fresh resolver-owned rows, current authenticated
output review, exact verified authority binding, and signed envelope.issued_at.
This function checks their consistency and policy meaning, not their provenance.
Its decision is not a release permit without the final authority/transport gates.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from .canonical import PolicyError, digest
from .contract import Binding, Number, Only, StrictModel, evaluate_predicate
from .evidence import (MAX_DEPTH, MAX_NODES, EvidenceIdentity, Qualification,
    QualifiedEvidence, _json, _key, _row_revision)
from .fact_projection import ReviewedFactProjection, _SENSITIVITY, _current, prepare_fact_projection
from .fact_contract import (CAPABILITY, EVALUATOR, VOCABULARY, PURPOSE, PROJECTION_VERSION, VIEW,
    FactPolicyV2, FactDecision, FactEvidenceUse, FactOutputForm, RollingEventWindow)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")


class _EvaluationTime(StrictModel):
    request_as_of: Number
    now: Number


def canonical_utc_microseconds(value) -> int | None:
    """Only explicit UTC ISO text; no naive, offset, epoch or created_at fallback.

    The integer calculation avoids float rounding at inclusive window boundaries.
    None means unknown; callers may not turn an invalid value into an old event.
    """
    if type(value) is not str or _UTC.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
        delta = parsed - _EPOCH
    except ValueError:
        return None
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _reference(raw, binding):
    if type(raw) is not dict or "table" not in raw or "record_id" not in raw:
        raise PolicyError("fact_policy_lineage")
    table = raw["table"]
    fields = {"table", "record_id"}
    if table == "conversation_messages":
        fields |= {"source_id", "dataset_id"}
    elif table == "ai_chat_messages":
        fields.add("source_id")
    if set(raw) != fields:
        raise PolicyError("fact_policy_lineage")
    return EvidenceIdentity.parse({"binding": binding.model_dump(), "table": table,
        "record_id": raw["record_id"], "source_id": raw.get("source_id"),
        "dataset_kind": "row_dataset" if table == "conversation_messages" else "node_resource",
        "dataset_id": raw.get("dataset_id")})


def _bundle(evidence, projection, rows, binding):
    evidence = QualifiedEvidence.parse(evidence.model_dump())
    projection = ReviewedFactProjection.parse(projection.model_dump())
    qualification = Qualification(verdict="qualified", reason_code="trusted_adapter_input", evidence=evidence)
    _current(qualification)
    snapshot = evidence.snapshot
    if snapshot.binding.model_dump() != {field: getattr(binding, field) for field in type(snapshot.binding).model_fields}:
        raise PolicyError("fact_policy_binding")
    if (any(ref.identity.table != "signal_objects" for ref in snapshot.artifacts)
        or any(ref.identity.table not in {"conversation_messages", "ai_chat_messages"} for ref in snapshot.leaves)):
        raise PolicyError("fact_policy_lineage")
    refs = snapshot.artifacts + snapshot.leaves
    versions = {_key(ref.identity): ref for ref in refs}
    if type(rows) is not dict or set(rows) != set(versions) or len(versions) > MAX_NODES:
        raise PolicyError("fact_policy_lineage")
    edges = {}
    for key, ref in versions.items():
        if type(rows[key]) is not dict or _row_revision(rows[key]) != ref.revision:
            raise PolicyError("fact_policy_revision")
        if ref.identity.table == "signal_objects":
            children = [_key(_reference(raw, snapshot.binding)) for raw in _json(rows[key].get("source_refs_json"), list)]
            if not children or len(children) != len(set(children)) or not set(children) <= set(versions):
                raise PolicyError("fact_policy_lineage")
            edges[key] = sorted(children)
    if digest({"artifacts": [ref.model_dump() for ref in sorted(snapshot.artifacts, key=lambda ref: _key(ref.identity))],
        "leaves": [ref.model_dump() for ref in sorted(snapshot.leaves, key=lambda ref: _key(ref.identity))], "edges": edges}) != snapshot.lineage_revision:
        raise PolicyError("fact_policy_lineage")
    roots = [ref for ref in snapshot.artifacts if ref.identity.record_id == snapshot.fact_id]
    if len(roots) != 1:
        raise PolicyError("fact_policy_lineage")
    descendants, visiting, visited = {}, set(), set()
    def visit(key, depth):
        if depth > MAX_DEPTH or key in visiting:
            raise PolicyError("fact_policy_lineage")
        if key in descendants:
            return descendants[key]
        visiting.add(key)
        visited.add(key)
        value = set().union(*(visit(child, depth + 1) for child in edges[key])) if key in edges else {key}
        visiting.remove(key)
        descendants[key] = value
        return value
    visit(_key(roots[0].identity), 0)
    if visited != set(versions):
        raise PolicyError("fact_policy_lineage")
    candidate = prepare_fact_projection(qualification=qualification, fact_row=rows[_key(roots[0].identity)])
    if candidate != projection.candidate or candidate.projection_version != PROJECTION_VERSION:
        raise PolicyError("fact_policy_projection_stale")
    if _SENSITIVITY[projection.classification.sensitivity] < max(_SENSITIVITY[item.sensitivity] for item in evidence.classifications):
        raise PolicyError("output_sensitivity_attenuation_unsupported")
    return evidence, projection, versions, descendants


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
    """Pure decision over one current, whole correlated evidence/output tuple.

    Event windows use only signed request issuance time, never a recipient query
    parameter. The eventual serving adapter must pass envelope.issued_at here.
    Explicit deny or a potentially matching unknown deny dominates all permits.
    """
    policy = FactPolicyV2.parse(policy.model_dump())
    binding = Binding.parse(binding.model_dump())
    clock = _EvaluationTime.parse({"request_as_of": request_as_of, "now": now})
    if policy.binding != binding:
        raise PolicyError("fact_policy_binding")
    evidence, projection, versions, descendants = _bundle(evidence, projection, rows, binding)
    revision = digest({"snapshot": evidence.snapshot.model_dump(), "review_revision": evidence.review_revision,
        "output_review_revision": projection.output_review_revision, "projection": projection.candidate.output.model_dump(),
        "projection_version": PROJECTION_VERSION, "request_as_of": request_as_of, "event_time_semantics": "canonical_event_time_v1"})
    def result(verdict, reason, allows=(), denies=(), missing=()):
        return FactDecision(stage="output_release", verdict=verdict, policy_hash=digest(policy.model_dump()),
            candidate_revision=revision, evaluator_version=EVALUATOR,
            matched_allow_clause_ids=list(allows[:1]) if verdict == "permit" else [],
            matched_deny_clause_ids=list(denies), reason_code=reason,
            required_projection_id=VIEW if verdict == "permit" else None, missing_context_codes=list(missing))
    if (clock.now < clock.request_as_of or clock.now - clock.request_as_of > 120
        or not policy.validity.starts_at <= clock.request_as_of <= clock.now < policy.validity.expires_at):
        return result("deny", "stale_authority")
    anchor = clock.request_as_of * 1_000_000
    fact_times_unknown = False
    for ref in evidence.snapshot.artifacts:
        row = rows[_key(ref.identity)]
        if row.get("valid_to") is not None:
            return result("deny", "fact_not_current")
        start = canonical_utc_microseconds(row.get("valid_from"))
        fact_times_unknown |= start is None or "valid_to" not in row
        if start is not None and start > anchor:
            return result("deny", "fact_not_current")
    events = {_key(ref.identity): canonical_utc_microseconds(rows[_key(ref.identity)].get("event_at")) for ref in evidence.snapshot.leaves}
    labels = {_key(item.evidence.identity): _attributes(item) for item in evidence.classifications}
    output_labels = _attributes(projection.classification)
    sources_all = {ref.identity.source_id for ref in evidence.snapshot.leaves}
    tables_all = {ref.identity.table for ref in versions.values()}
    allows, denies, unknown_allow, unknown_deny, unsupported = [], [], False, False, False
    missing = set()
    if fact_times_unknown:
        missing.add("fact_validity")
    for rule in policy.rules:
        sources = set(rule.evidence_use.sources.values if isinstance(rule.evidence_use.sources, Only) else policy.source_universe.source_ids)
        tables = set(rule.evidence_use.tables)
        if not sources or not tables or not rule.release.forms or "owner-engine-local" not in rule.evidence_use.processors.values:
            continue
        lower = anchor - rule.evidence_use.event_window.max_age_seconds * 1_000_000
        def in_window(key):
            event = events[key]
            return None if event is None or event > anchor else lower <= event
        if rule.effect == "permit":
            if not sources_all <= sources or not tables_all <= tables:
                continue
            if rule.release.ceiling == "inference":
                unsupported = True
                continue
            values = [evaluate_predicate(rule.evidence_use.predicate, labels[key]) for key in versions]
            values += [evaluate_predicate(rule.release.predicate, output_labels)]
            times = [in_window(key) for key in events]
            values += times + ([None] if fact_times_unknown else [])
            match = _and(values)
            if match is True:
                allows.append(rule.rule_id)
            elif match is None:
                unknown_allow = True
            if None in times:
                missing.add("time")
        else:
            values = []
            selected_times = []
            for key, ref in versions.items():
                if ref.identity.table not in tables:
                    continue
                # A derived artifact inherits only its actual descendant sources;
                # unrelated leaves cannot make a different artifact match.
                times = [in_window(leaf) for leaf in descendants[key] if versions[leaf].identity.source_id in sources]
                if not times:
                    continue
                time_match = _or(times)
                selected_times.append(time_match)
                values.append(_and([time_match, evaluate_predicate(rule.evidence_use.predicate, labels[key])]))
            if not selected_times:
                continue
            values.append(_and([_or(selected_times), evaluate_predicate(rule.release.predicate, output_labels)]))
            match = _or(values)
            if match is True:
                denies.append(rule.rule_id)
            elif match is None:
                unknown_deny = True
                missing.add("time")
    if denies:
        return result("deny", "rule_deny", denies=denies)
    if unknown_deny or fact_times_unknown:
        return result("indeterminate", "unknown_context", missing=sorted(missing))
    if allows:
        return result("permit", "rule_permit", allows=allows)
    if unknown_allow:
        return result("indeterminate", "unknown_context", missing=sorted(missing))
    return result("deny", "unsupported_view" if unsupported else "rule_deny")
