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

from .fact_contract import VIEW, PROJECTION_VERSION, FactScalarDisclosure

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


class FactProjectionReview(StrictModel):
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

    @model_validator(mode="after")
    def exact_binding(self):
        if (self.owner_id != self.candidate.snapshot.binding.owner_id
            or self.candidate_hash != digest(self.candidate.model_dump())
            or self.output_hash != digest(self.candidate.output.model_dump())):
            raise ValueError("projection review binding")
        return self


class ReviewedFactProjection(StrictModel):
    candidate: FactProjectionCandidate
    classification: OutputClassification
    output_review_revision: Hash
    authorization_status: Literal["not_evaluated"]
    execution_enabled: Literal[False]


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


def prepare_fact_projection(*, qualification: Qualification, fact_row: dict) -> FactProjectionCandidate:
    """Create a PRIVATE review candidate from the exact existing fact scalar.

    This validates consistency of supplied values, not authenticity or current
    database state. Future owner preview must call it under resolver ownership.
    """
    evidence = _current(qualification)
    snapshot = evidence.snapshot
    roots = [ref for ref in snapshot.artifacts if ref.identity.table == "signal_objects" and ref.identity.record_id == snapshot.fact_id]
    if (type(fact_row) is not dict or len(roots) != 1 or fact_row.get("object_id") != snapshot.fact_id
        or fact_row.get("object_type") != "fact" or roots[0].revision != snapshot.candidate_revision
        or _row_revision(fact_row, table="signal_objects") != snapshot.candidate_revision):
        raise PolicyError("projection_evidence_binding")
    payload = _json(fact_row.get("payload_json"), dict)
    if (payload.get("disclosure") != "scoped" or payload.get("subject_entity_id") != "self"
        or payload.get("asserted_by") != "owner" or fact_row.get("actor_role") not in (None,"authored")
        or payload.get("actor_role", "authored") != "authored"):
        raise PolicyError("projection_source_restricted")
    output = FactScalarDisclosure.parse({"family":"owner_stated_fact", "operation":"read", "view_id":VIEW,
        "subject":"self", "predicate":payload.get("predicate"), "value":payload.get("object_value")})
    return FactProjectionCandidate(version="topos-fact-projection-candidate/v1", projection_version=PROJECTION_VERSION,
        snapshot=snapshot, evidence_review_revision=evidence.review_revision, output=output)


def bind_output_review(*, qualification: Qualification, fact_row: dict,
                       review: FactProjectionReview | None, now: int) -> ReviewedFactProjection:
    """Return a non-executing candidate for later policy evaluation, never permit.

    The caller must load the current review from an authenticated owner store.
    A supplied or replayed JSON review cannot establish that trusted provenance.
    No evidence restriction can be overridden by output classification.
    """
    current = prepare_fact_projection(qualification=qualification, fact_row=fact_row)
    if review is None or not isinstance(review, FactProjectionReview):
        raise PolicyError("output_review_required")
    review = FactProjectionReview.parse(review.model_dump())
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
    return ReviewedFactProjection(candidate=current, classification=review.classification,
        output_review_revision=digest(review.model_dump()), authorization_status="not_evaluated", execution_enabled=False)
