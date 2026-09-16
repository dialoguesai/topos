"""Pure P2b input consistency and per-clause structural context.

These checks do NOT authenticate provenance, an active owner review, current
ledger authority, Off-limits state or permission to inspect data with a model.
Those remain obligations of the resolver/review/ledger/transport services.
The private, frozen clause contexts contain only consistency results for this
call. They are neither serializable authority nor a reusable eligibility permit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Literal

from .canonical import PolicyError, digest
from .contract import Binding, Number, Only, StrictModel
from .evidence import (MAX_DEPTH, MAX_NODES, EvidenceIdentity, Qualification,
    QualifiedEvidence, _json, _key, _row_revision)
from .fact_projection import ReviewedFactProjection, _SENSITIVITY, _current, prepare_fact_projection
from .identity import SELF, SUBJECT_CONTRACT_BY_CAPABILITY
from .fact_contract import (FACT_VALIDITY_STATED_DAY, PROJECTION_VERSION, AttestedSubjectFactPolicy,
    FactPolicyV2, StatedDayFactPolicy, fact_validity_semantics)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# A calendar day with an unrecorded timezone basis ends last at UTC-12, so it has
# elapsed everywhere 36 hours after its own 00:00 UTC: 12:00:00 UTC the next day.
STATED_DAY_ELAPSED_MICROSECONDS = 36 * 3600 * 1_000_000


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


def stated_day_elapsed_microseconds(value) -> int | None:
    """Instant from which a stated YYYY-MM-DD day has ended at every Earth offset.

    Only that exact lexical form is a stated day. Month, year, naive, offset,
    calendar-invalid or padded text stays unknown; nothing is rounded to a day.
    """
    if type(value) is not str or _DAY.fullmatch(value) is None:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (day - _EPOCH).days * 86_400_000_000 + STATED_DAY_ELAPSED_MICROSECONDS


def fact_current_from_microseconds(value, *, semantics) -> int | None:
    """Instant from which `valid_from` counts as current under the selected contract.

    Exact UTC instants are accepted by every contract. A stated day is accepted
    only by `stated_day_v1`, and then conservatively. None means unknown.
    """
    start = canonical_utc_microseconds(value)
    if start is None and semantics == FACT_VALIDITY_STATED_DAY:
        start = stated_day_elapsed_microseconds(value)
    return start


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


def _check_bundle(evidence, projection, rows, binding, permitted_subjects=frozenset({SELF})):
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
        if type(rows[key]) is not dict or _row_revision(rows[key], table=ref.identity.table) != ref.revision:
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
    # Re-derived here so a reviewed projection cannot be paired with different
    # rows. `permitted_subjects` must be the set the resolver qualified under,
    # or this re-derivation legitimately refuses an attested subject.
    candidate = prepare_fact_projection(qualification=qualification, fact_row=rows[_key(roots[0].identity)],
                                        permitted_subjects=permitted_subjects)
    if candidate != projection.candidate or candidate.projection_version != PROJECTION_VERSION:
        raise PolicyError("fact_policy_projection_stale")
    if _SENSITIVITY[projection.classification.sensitivity] < max(_SENSITIVITY[item.sensitivity] for item in evidence.classifications):
        raise PolicyError("output_sensitivity_attenuation_unsupported")
    return evidence, projection, versions, descendants


TriState = bool | None


@dataclass(frozen=True)
class PermitStructure:
    rule_index: int
    evidence_keys: tuple[str, ...]
    leaf_times: tuple[TriState, ...]


@dataclass(frozen=True)
class DenyStructure:
    rule_index: int
    evidence_times: tuple[tuple[str, TriState], ...]
    output_time: TriState


@dataclass(frozen=True)
class FactEligibility:
    policy_hash: str
    candidate_revision: str
    terminal_reason: Literal["stale_authority", "fact_not_current"] | None
    fact_times_unknown: bool
    unsupported_view: bool
    clauses: tuple[PermitStructure | DenyStructure, ...]


def _or(values):
    return True if True in values else None if None in values else False


def prepare_fact_eligibility(*, policy: FactPolicyV2, evidence: QualifiedEvidence,
    projection: ReviewedFactProjection, rows: dict, binding: Binding,
    request_as_of: int, now: int, permitted_subjects=frozenset({SELF})):
    """Return parsed input copies and frozen structure, without membership.

    False/unknown temporal masks are retained, not promoted or flattened. In
    particular, unknown fact validity cannot short-circuit a known semantic
    deny, whose existing decision reason takes precedence. Each deny artifact
    carries only its own descendants selected by that exact rule's sources.
    A future model adapter must independently establish processing authority
    and stop on mandatory unknowns before passing any candidate contents on.
    Fact validity follows the contract the policy itself selected: exact UTC
    instants for v1, and additionally conservatively elapsed stated days for a
    v2 policy. The contributor event window is unchanged by that selection.
    """
    if type(policy) not in (FactPolicyV2, StatedDayFactPolicy, AttestedSubjectFactPolicy):
        raise PolicyError("fact_policy_binding")
    policy = type(policy).parse(policy.model_dump())
    semantics = fact_validity_semantics(policy)
    binding = Binding.parse(binding.model_dump())
    clock = _EvaluationTime.parse({"request_as_of": request_as_of, "now": now})
    if policy.binding != binding:
        raise PolicyError("fact_policy_binding")
    # The policy's own capability fixes which owner-identity rule applies. If the
    # evidence was qualified under a different one, the two were paired after the
    # fact and neither the permit set nor the review can be trusted for this grant.
    if SUBJECT_CONTRACT_BY_CAPABILITY.get(policy.versions.capability) != evidence.subject_contract:
        raise PolicyError("subject_contract_mismatch")
    evidence, projection, versions, descendants = _check_bundle(evidence, projection, rows, binding,
                                                                permitted_subjects)
    revision = digest({"snapshot": evidence.snapshot.model_dump(), "review_revision": evidence.review_revision,
        "output_review_revision": projection.output_review_revision, "projection": projection.candidate.output.model_dump(),
        "projection_version": PROJECTION_VERSION, "request_as_of": request_as_of, "event_time_semantics": "canonical_event_time_v1"})
    policy_hash = digest(policy.model_dump())

    def prepared(terminal=None, unknown=False, unsupported=False, clauses=()):
        # Only the structural context is recursively immutable; parsed Pydantic
        # inputs are fresh copies and stay local to the synchronous evaluator.
        return policy, evidence, projection, FactEligibility(policy_hash, revision,
            terminal, unknown, unsupported, tuple(clauses))

    if (clock.now < clock.request_as_of or clock.now - clock.request_as_of > 120
        or not policy.validity.starts_at <= clock.request_as_of <= clock.now < policy.validity.expires_at):
        return prepared("stale_authority")
    anchor = clock.request_as_of * 1_000_000
    fact_times_unknown = False
    for ref in evidence.snapshot.artifacts:
        row = rows[_key(ref.identity)]
        if row.get("valid_to") is not None:
            return prepared("fact_not_current")
        start = fact_current_from_microseconds(row.get("valid_from"), semantics=semantics)
        fact_times_unknown |= start is None or "valid_to" not in row
        if start is not None and start > anchor:
            return prepared("fact_not_current")
    events = {_key(ref.identity): canonical_utc_microseconds(rows[_key(ref.identity)].get("event_at")) for ref in evidence.snapshot.leaves}
    sources_all = {ref.identity.source_id for ref in evidence.snapshot.leaves}
    tables_all = {ref.identity.table for ref in versions.values()}
    clauses, unsupported = [], False
    for index, rule in enumerate(policy.rules):
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
            clauses.append(PermitStructure(index, tuple(versions), tuple(in_window(key) for key in events)))
        else:
            evidence_times = []
            for key, ref in versions.items():
                if ref.identity.table not in tables:
                    continue
                times = [in_window(leaf) for leaf in descendants[key] if versions[leaf].identity.source_id in sources]
                if times:
                    evidence_times.append((key, _or(times)))
            if evidence_times:
                clauses.append(DenyStructure(index, tuple(evidence_times), _or([value for _, value in evidence_times])))
    return prepared(unknown=fact_times_unknown, unsupported=unsupported, clauses=clauses)
