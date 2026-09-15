"""Pure candidate consistency tests; no new output authority is advertised."""
from copy import deepcopy
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import corpus, attest, decision, payload
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.contract import PolicyV2, capability_document
from topos.permissions_v2.evidence import Qualification
from topos.permissions_v2.fact_projection import (
    FactScalarDisclosure, FactProjectionReview, ReviewedFactProjection,
    prepare_fact_projection, bind_output_review,
)


def row(corpus):
    with sqlite3.connect(corpus[0].path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM signal_objects WHERE object_id=?", (corpus[2],)).fetchone())


def reviewed(corpus):
    attest(corpus)
    qualification, raw = decision(corpus), row(corpus)
    candidate = prepare_fact_projection(qualification=qualification, fact_row=raw)
    review = FactProjectionReview.parse({"version":"topos-fact-projection-review/v1", "review_id":"output-review-1",
        "owner_id":"owner-1", "reviewed_at":1100, "status":"approved", "candidate":candidate.model_dump(),
        "candidate_hash":digest(candidate.model_dump()), "output_hash":digest(candidate.output.model_dump()),
        "classification":{"domains":["reading"],"sensitivity":"personal","subject":"self","assertion":"explicit_atomic_preference"}})
    return qualification, raw, candidate, review


def test_exact_reviewed_preference_returns_only_nonexecuting_projection(corpus):
    qualification, raw, candidate, review = reviewed(corpus)
    prepared = bind_output_review(qualification=qualification,fact_row=raw,review=review,now=1101)
    assert prepared.candidate.output.model_dump() == {"family":"owner_stated_fact","operation":"read",
        "view_id":"owner_stated_fact.scalar.v1","subject":"self","predicate":"prefers","value":"history books"}
    assert prepared.execution_enabled is False and prepared.authorization_status == "not_evaluated"
    assert "source_refs" not in prepared.candidate.output.model_dump_json()
    assert "I enjoy reading" not in prepared.candidate.output.model_dump_json()
    assert candidate.snapshot == qualification.evidence.snapshot
    assert capability_document()["executable_forms"] == []


@pytest.mark.parametrize("value", [None,True,12,12.3,[],{},"", " padded", "a\nparagraph", "a\tlabel", "a  label",
    '"quoted"', "“quoted”", "'quoted'", "one sentence. Another sentence", "question?", "x"*257,
    "Cafe\u0301", "bidi\u202eattack", "hidden\u200btext", "separated\u2028paragraph", "——"])
def test_non_scalar_quoted_paragraph_control_or_noncanonical_text_rejected(corpus,value):
    _,_,candidate,_ = reviewed(corpus)
    with pytest.raises(PolicyError):
        FactScalarDisclosure.parse({**candidate.output.model_dump(),"value":value})


@pytest.mark.parametrize("value", ["café history", "日本の歴史", "sci-fi", "children’s history", "arts & crafts"])
def test_explicit_nfc_unicode_label_grammar_is_preserved(corpus,value):
    payload(corpus,object_value=value)
    qualification, raw, candidate, review = reviewed(corpus)
    assert bind_output_review(qualification=qualification,fact_row=raw,review=review,now=1101).candidate.output.value == value


@pytest.mark.parametrize("field,value", [("predicate","works_on"),("subject","other"),("raw","secret"),("view_id","canonical.message_disclosure.v1")])
def test_closed_output_schema_cannot_be_widened(corpus,field,value):
    _,_,candidate,_ = reviewed(corpus)
    with pytest.raises(PolicyError): FactScalarDisclosure.parse({**candidate.output.model_dump(),field:value})


@pytest.mark.parametrize("change", ["row", "snapshot", "leaf_binding", "missing_leaf", "duplicate_classification", "unknown_evidence", "non_authored", "mixed_speech", "owner_only", "withheld"])
def test_forged_inconsistent_or_withheld_evidence_cannot_be_rescued_by_output_review(corpus,change):
    qualification, raw, _, review = reviewed(corpus)
    data = qualification.model_dump()
    if change == "row": raw["payload_json"] += " "
    elif change == "snapshot": data["evidence"]["snapshot"]["candidate_revision"] = "a"*64
    elif change == "leaf_binding": data["evidence"]["snapshot"]["leaves"][0]["identity"]["binding"]["owner_id"] = "other"
    elif change == "missing_leaf": data["evidence"]["snapshot"]["leaves"] = []
    elif change == "duplicate_classification": data["evidence"]["classifications"].append(deepcopy(data["evidence"]["classifications"][0]))
    elif change == "unknown_evidence": data["evidence"]["classifications"][0]["sensitivity"] = "unknown"
    elif change == "non_authored": data["evidence"]["classifications"][0]["authorship"] = "other"
    elif change == "mixed_speech": data["evidence"]["classifications"][0]["speech"] = "mixed"
    elif change == "owner_only": payload(corpus,disclosure="owner_only"); raw=row(corpus)
    else: data.update(verdict="withheld",evidence=None,reason_code="owner_only")
    with pytest.raises(PolicyError): bind_output_review(qualification=Qualification.parse(data),fact_row=raw,review=review,now=1101)


@pytest.mark.parametrize("change", ["owner", "output", "snapshot", "review_revision", "missing", "revoked", "future", "sensitivity"])
def test_review_must_bind_exact_current_output_and_evidence(corpus,change):
    qualification, raw, _, review = reviewed(corpus)
    data = review.model_dump()
    if change == "missing":
        with pytest.raises(PolicyError,match="output_review_required"):
            bind_output_review(qualification=qualification,fact_row=raw,review=None,now=1101)
        return
    if change == "owner": data["owner_id"] = "other"
    elif change == "output": data["candidate"]["output"]["value"] = "changed label"
    elif change == "snapshot": data["candidate"]["snapshot"]["protection_revision"] = "f"*64
    elif change == "review_revision": data["candidate"]["evidence_review_revision"] = "e"*64
    elif change == "revoked": data["status"] = "revoked"
    elif change == "future": data["reviewed_at"] = 1200
    else: data["classification"]["sensitivity"] = "none"
    # Rehashing a stale edited review does not make it current authority.
    data["candidate_hash"] = digest(data["candidate"])
    data["output_hash"] = digest(data["candidate"]["output"])
    with pytest.raises(PolicyError):
        changed = FactProjectionReview.parse(data)
        bind_output_review(qualification=qualification,fact_row=raw,review=changed,now=1101)


def test_output_classification_cannot_turn_unknown_evidence_into_known(corpus):
    qualification, raw, _, review = reviewed(corpus)
    data = qualification.model_dump()
    data["evidence"]["classifications"][0]["domains"] = []
    with pytest.raises(PolicyError,match="projection_evidence_unknown"):
        bind_output_review(qualification=Qualification.parse(data),fact_row=raw,review=review,now=1101)


def test_source_value_is_not_summarized_redacted_or_normalized(corpus):
    payload(corpus,object_value="history books because of private health reasons.")
    attest(corpus)
    with pytest.raises(PolicyError): prepare_fact_projection(qualification=decision(corpus),fact_row=row(corpus))


def test_no_model_or_serialized_projection_can_claim_execution(corpus):
    qualification, raw, _, review = reviewed(corpus)
    result = bind_output_review(qualification=qualification,fact_row=raw,review=review,now=1101)
    for field,value in [("execution_enabled",True),("authorization_status","permit")]:
        with pytest.raises(PolicyError): ReviewedFactProjection.parse({**result.model_dump(),field:value})
    # A perfectly self-consistent caller-created review is still only a private
    # candidate, never a signed policy, authenticated store proof or release.
    with pytest.raises(PolicyError): PolicyV2.parse(result.model_dump())


@pytest.mark.parametrize("subjects", [["other"],["self","other"],["self","self"]])
def test_first_projection_requires_exact_self_classification(corpus,subjects):
    qualification,raw,_,review=reviewed(corpus)
    data=qualification.model_dump()
    data["evidence"]["classifications"][0]["subject_entity_ids"]=subjects
    with pytest.raises(PolicyError,match="projection_evidence_unknown"):
        bind_output_review(qualification=Qualification.parse(data),fact_row=raw,review=review,now=1101)


@pytest.mark.parametrize("now", [True, -1, 9007199254740992, 1101.0])
def test_review_clock_is_strict_bounded_integer(corpus,now):
    qualification,raw,_,review=reviewed(corpus)
    with pytest.raises(PolicyError,match="output_review_not_current"):
        bind_output_review(qualification=qualification,fact_row=raw,review=review,now=now)
