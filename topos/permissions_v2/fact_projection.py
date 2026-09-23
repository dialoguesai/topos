"""Pure, unmounted owner-stated preference projection; never authorization.

The future serving adapter must obtain qualification afresh from the trusted
resolver and load an authenticated, current output review from an owner store.
Constructing these models or matching their hashes does not prove either fact.
No signed Policy v2 grammar, capability, ledger or transport imports this module.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .canonical import MAX_INTEGER, PolicyError, digest
from .contract import Hash, Identifier, Number, StrictModel
from .evidence import EvidenceSnapshot, Qualification, _json, _key, _row_revision
from .identity import LEGACY_CONTRACT, SELF

from .fact_contract import (FAMILY, OUTPUT_FAMILIES, PROJECTION_VERSION, VIEW, WORK_FAMILY,
    WORK_PROJECTION_VERSION, FactScalarDisclosure, WorkScalarDisclosure)

_SENSITIVITY = {"none": 0, "personal": 1, "special": 2}


class OutputClassification(StrictModel):
    domains: Annotated[list[Identifier], Field(min_length=1, max_length=16)]
    sensitivity: Literal["none", "personal", "special"]
    subject: Literal["self"]
    assertion: Literal["explicit_atomic_preference"]

    @field_validator("domains")
    @classmethod
    def unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate domain")
        return values


class FactProjectionCandidate(StrictModel):
    version: Literal["topos-fact-projection-candidate/v1"]
    projection_version: Literal["exact-owner-preference/v1"]
    snapshot: EvidenceSnapshot
    evidence_review_revision: Hash
    output: FactScalarDisclosure


class _ReviewBinding(StrictModel):
    """The exact-binding check, shared so a second family cannot drift from the first.

    Declares no fields, so it changes neither the field order nor the serialized
    shape nor the JSON schema of any class that inherits it.
    """

    @model_validator(mode="after")
    def exact_binding(self):
        if (self.owner_id != self.candidate.snapshot.binding.owner_id
            or self.candidate_hash != digest(self.candidate.model_dump())
            or self.output_hash != digest(self.candidate.output.model_dump())):
            raise ValueError("projection review binding")
        return self


class FactProjectionReview(_ReviewBinding):
    """Value loaded from a FUTURE authenticated owner store, not caller proof."""
    version: Literal["topos-fact-projection-review/v1"]
    review_id: Identifier
    owner_id: Identifier
    reviewed_at: Number
    status: Literal["approved", "revoked"]
    candidate: FactProjectionCandidate
    candidate_hash: Hash
    output_hash: Hash
    classification: OutputClassification


class ReviewedFactProjection(StrictModel):
    candidate: FactProjectionCandidate
    classification: OutputClassification
    output_review_revision: Hash
    authorization_status: Literal["not_evaluated"]
    execution_enabled: Literal[False]


class WorkOutputClassification(StrictModel):
    """What the owner attests about a work label, in their own review.

    A separate class rather than a widened `assertion` literal. Widening would
    let a preference review carry a work assertion and the reverse, and the
    review is the only place a human says what the label means -- so the two
    must not be interchangeable values of one field.
    """
    domains: Annotated[list[Identifier], Field(min_length=1, max_length=16)]
    sensitivity: Literal["none", "personal", "special"]
    subject: Literal["self"]
    assertion: Literal["explicit_atomic_work_engagement"]

    @field_validator("domains")
    @classmethod
    def unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate domain")
        return values


class WorkFactProjectionCandidate(StrictModel):
    version: Literal["topos-fact-projection-candidate/v1"]
    projection_version: Literal["exact-owner-work/v1"]
    snapshot: EvidenceSnapshot
    evidence_review_revision: Hash
    output: WorkScalarDisclosure


class WorkFactProjectionReview(_ReviewBinding):
    """Value loaded from a FUTURE authenticated owner store, not caller proof."""
    version: Literal["topos-fact-projection-review/v1"]
    review_id: Identifier
    owner_id: Identifier
    reviewed_at: Number
    status: Literal["approved", "revoked"]
    candidate: WorkFactProjectionCandidate
    candidate_hash: Hash
    output_hash: Hash
    classification: WorkOutputClassification


class WorkReviewedFactProjection(StrictModel):
    candidate: WorkFactProjectionCandidate
    classification: WorkOutputClassification
    output_review_revision: Hash
    authorization_status: Literal["not_evaluated"]
    execution_enabled: Literal[False]


# family name -> (candidate class, review class, reviewed class). Kept beside
# OUTPUT_FAMILIES rather than merged into it: the contract module must not
# import the projection layer, and these classes live here.
PROJECTION_BY_FAMILY = {
    FAMILY: (FactProjectionCandidate, FactProjectionReview, ReviewedFactProjection),
    WORK_FAMILY: (WorkFactProjectionCandidate, WorkFactProjectionReview, WorkReviewedFactProjection),
}


def _current(qualification: Qualification):
    if not isinstance(qualification, Qualification):
        raise PolicyError("qualified_evidence_required")
    current = Qualification.parse(qualification.model_dump())
    if current.verdict != "qualified" or current.evidence is None:
        raise PolicyError("qualified_evidence_required")
    evidence = current.evidence
    snapshot = evidence.snapshot
    references = snapshot.artifacts + snapshot.leaves
    identities = [_key(ref.identity) for ref in references]
    classified = {_key(item.evidence.identity): item for item in evidence.classifications}
    if (not snapshot.artifacts or not snapshot.leaves or len(set(identities)) != len(identities)
        or set(classified) != set(identities) or len(classified) != len(evidence.classifications)
        or any(ref.identity.binding != snapshot.binding for ref in references)):
        raise PolicyError("projection_evidence_binding")
    for ref in references:
        item = classified[_key(ref.identity)]
        if (item.evidence != ref or not item.domains or len(set(item.domains)) != len(item.domains)
            or item.sensitivity not in _SENSITIVITY or item.subject_entity_ids != ["self"]
            or item.authorship != "owner_authored" or item.speech != "direct_self_statement"
            or item.independent_copies != "none_known"):
            raise PolicyError("projection_evidence_unknown")
    return evidence


def prepare_fact_projection(*, qualification: Qualification, fact_row: dict,
                            permitted_subjects=frozenset({SELF}), family=FAMILY):
    """Create a PRIVATE review candidate from the exact existing fact scalar.

    This validates consistency of supplied values, not authenticity or current
    database state. Future owner preview must call it under resolver ownership.

    `permitted_subjects` is the resolver's permit set for the qualifying
    contract, derived in the same read transaction and never stored in the
    candidate. It defaults to the literal subject alone, so a caller that omits
    it can only reproduce the pre-binding behaviour. Under the legacy contract
    the literal rule is frozen and this argument is ignored entirely.

    `family` is the output family the POLICY selected, never the caller's
    preference and never inferred from the row. It defaults to the first family,
    so every pre-v4 caller is unchanged. The family fixes the view id, the
    projection version and the disclosure class, and that class pins the
    predicate -- so a `prefers` row asked for under the work family is refused
    as schema_invalid rather than silently relabelled, and the reverse likewise.
    """
    evidence = _current(qualification)
    snapshot = evidence.snapshot
    roots = [ref for ref in snapshot.artifacts if ref.identity.table == "signal_objects" and ref.identity.record_id == snapshot.fact_id]
    if (type(fact_row) is not dict or len(roots) != 1 or fact_row.get("object_id") != snapshot.fact_id
        or fact_row.get("object_type") != "fact" or roots[0].revision != snapshot.candidate_revision
        or _row_revision(fact_row, table="signal_objects") != snapshot.candidate_revision):
        raise PolicyError("projection_evidence_binding")
    payload = _json(fact_row.get("payload_json"), dict)
    subject = payload.get("subject_entity_id")
    # Frozen for every pre-binding capability; the attested contract widens the
    # accepted subject but never the emitted one, which stays the literal below.
    allowed = {SELF} if evidence.subject_contract == LEGACY_CONTRACT else set(permitted_subjects)
    if (payload.get("disclosure") != "scoped" or type(subject) is not str or subject not in allowed
        or payload.get("asserted_by") != "owner" or fact_row.get("actor_role") not in (None,"authored")
        or payload.get("actor_role", "authored") != "authored"):
        raise PolicyError("projection_source_restricted")
    if family not in OUTPUT_FAMILIES:
        raise PolicyError("unsupported_view")
    view, projection_version, disclosure = OUTPUT_FAMILIES[family]
    candidate_model = PROJECTION_BY_FAMILY[family][0]
    output = disclosure.parse({"family":family, "operation":"read", "view_id":view,
        "subject":"self", "predicate":payload.get("predicate"), "value":payload.get("object_value")})
    return candidate_model(version="topos-fact-projection-candidate/v1", projection_version=projection_version,
        snapshot=snapshot, evidence_review_revision=evidence.review_revision, output=output)


def bind_output_review(*, qualification: Qualification, fact_row: dict,
                       review=None, now: int,
                       permitted_subjects=frozenset({SELF}), family=FAMILY):
    """Return a non-executing candidate for later policy evaluation, never permit.

    The caller must load the current review from an authenticated owner store.
    A supplied or replayed JSON review cannot establish that trusted provenance.
    No evidence restriction can be overridden by output classification.

    The review class is selected by `family` and the isinstance test stays exact,
    so a preference review presented for a work release is refused as missing
    rather than accepted: the two review types are not substitutable, which is
    the whole reason the classification is a separate class per family.
    """
    current = prepare_fact_projection(qualification=qualification, fact_row=fact_row,
                                      permitted_subjects=permitted_subjects, family=family)
    if family not in PROJECTION_BY_FAMILY:
        raise PolicyError("unsupported_view")
    _candidate_model, review_model, reviewed_model = PROJECTION_BY_FAMILY[family]
    if review is None or type(review) is not review_model:
        raise PolicyError("output_review_required")
    review = review_model.parse(review.model_dump())
    if type(now) is not int or not 0 <= now <= MAX_INTEGER or now < review.reviewed_at or review.status != "approved":
        raise PolicyError("output_review_not_current")
    if review.candidate != current:
        raise PolicyError("output_review_stale")
    evidence = _current(qualification)
    # This exact scalar view proves no semantic declassification function. It
    # therefore cannot use output labels to lower the evidence sensitivity floor.
    floor = max(_SENSITIVITY[item.sensitivity] for item in evidence.classifications)
    if _SENSITIVITY[review.classification.sensitivity] < floor:
        raise PolicyError("output_sensitivity_attenuation_unsupported")
    return reviewed_model(candidate=current, classification=review.classification,
        output_review_revision=digest(review.model_dump()), authorization_status="not_evaluated", execution_enabled=False)
