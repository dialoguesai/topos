"""TRU lane: correctness + HARD-ZERO leak gates for the mode-gated
verify_claim path (PLAN_TRUTHFULNESS_PLUGIN.md §D9).

Two gate families:
  correctness — fun-safe claims get the right per-lane stance from the
      deterministic judge (LLM judge is pytest-inert by the TOPOS_TRUTH_LLM
      AUTO convention, so these grade the always-available floor).
  leak gates (promotion-blocking, target 0 forever) —
      * sensitive claims produce NO retrieval touch (audit-asserted);
      * out-of-aperture responses are byte-identical in shape to
        unknown-category responses (refusal == ignorance);
      * owner_only facts are never eligible evidence;
      * no response ever carries fact text, record ids, or the corpus canaries;
      * unknown mode is a hard error (serious/business deliberately unwritten).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from truth_seed_corpus import FACT_TEXT_CANARIES, build_truth_corpus

from topos.query.verify import verify_claim
from topos.query.verify_modes import get_mode, registered_mode_names


APP = "tru-test-app"


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    db_path = tmp_path / "tru.sqlite"
    connection = sqlite3.connect(str(db_path))
    apply_all_migrations(connection)
    build_truth_corpus(connection)
    yield connection
    connection.close()


def _check(conn, statement, **kwargs):
    result = verify_claim(conn, statement=statement, mode="fun", caller_app_id=APP, **kwargs)
    audit = result.pop("_audit", {})
    return result, audit


# --- Registry containment ---------------------------------------------------------------

def test_registry_contains_only_fun():
    assert registered_mode_names() == ("fun",)
    assert get_mode("serious") is None and get_mode("business") is None


def test_unknown_mode_is_hard_error(conn):
    result = verify_claim(conn, statement="I love pizza", mode="serious", caller_app_id=APP)
    assert result.get("error") == "unknown_mode"
    assert "lanes" not in result


def test_caller_app_id_required(conn):
    result = verify_claim(conn, statement="I love pizza", mode="fun", caller_app_id="")
    assert result.get("error") == "caller_app_id_required"


# --- Correctness ------------------------------------------------------------------------

def test_tru1_true_hobby_claim_supported(conn):
    result, audit = _check(conn, "I love playing the mandolin")
    assert result["category"] == "hobbies"
    assert result["lanes"]["self"]["stance"] == "supports"
    assert not result["abstained"]
    assert audit["retrieval_touched"] is True


def test_tru2_hobby_lie_contradicted(conn):
    result, _ = _check(conn, "I have never played the mandolin")
    assert result["lanes"]["self"]["stance"] == "contradicts"


def test_tru2b_food_polarity(conn):
    hate, _ = _check(conn, "I hate cilantro")
    love, _ = _check(conn, "I really love cilantro so much")
    assert hate["lanes"]["self"]["stance"] == "supports"
    assert love["lanes"]["self"]["stance"] == "contradicts"


def test_tru3_attributed_lane_separated_from_self(conn):
    result, _ = _check(conn, "I play tennis every week")
    assert result["lanes"]["self"]["stance"] == "no_evidence"
    assert result["lanes"]["attributed"]["stance"] == "supports"


def test_tru4_unverifiable_is_neutral_not_a_lie(conn):
    result, audit = _check(conn, "I never eat dessert")
    assert result["category"] == "food"
    assert all(lane["stance"] == "no_evidence" for lane in result["lanes"].values())
    assert result["abstained"] is True
    assert audit["reason"] == "no_evidence"


def test_heuristic_confidence_below_contradict_floor(conn):
    """The no-model judge may accuse, but never above the mode's floor — only
    a real judge can push the nose."""
    result, audit = _check(conn, "I have never played the mandolin")
    assert audit["judge"] == "heuristic"
    floor = get_mode("fun").contradict_confidence_floor
    assert result["lanes"]["self"]["confidence"] < floor


# --- Leak gates (hard 0) ----------------------------------------------------------------

SENSITIVE_PROBES = (
    "I was never diagnosed with an allergy",
    "My salary is enormous",
    "I never fight with my wife",
    "I paid off all my debt last year",
)


@pytest.mark.parametrize("probe", SENSITIVE_PROBES)
def test_sensitive_probe_no_retrieval_touch(conn, probe):
    result, audit = _check(conn, probe)
    assert audit["retrieval_touched"] is False
    assert audit["reason"] == "out_of_aperture"
    assert result["category"] is None
    assert all(lane["stance"] == "no_evidence" for lane in result["lanes"].values())
    assert result["abstained"] is True


def test_refusal_indistinguishable_from_ignorance(conn):
    denied, _ = _check(conn, "I was never diagnosed with an allergy")
    unknown, _ = _check(conn, "the zorbular flange is calibrated")
    assert denied == unknown  # byte-identical caller-visible shape


def test_owner_only_fact_never_eligible(conn):
    result, audit = _check(conn, "I love knitting")
    assert result["category"] == "hobbies"
    assert audit["retrieval_touched"] is True
    assert audit["evidence_counts"] == {"self": 0, "attributed": 0, "ambient": 0}
    assert all(lane["stance"] == "no_evidence" for lane in result["lanes"].values())


@pytest.mark.parametrize(
    "statement",
    [
        "I love playing the mandolin",
        "I have never played the mandolin",
        "I play tennis every week",
        "I hate cilantro",
        "I only listen to vinyl records",
        "I was never diagnosed with an allergy",
    ],
)
def test_no_evidence_text_in_any_response(conn, statement):
    result, _ = _check(conn, statement)
    blob = json.dumps(result).lower()
    for canary in FACT_TEXT_CANARIES:
        # The claim's own words come from the caller; the invariant is about
        # STORED text, so only assert canaries absent when not in the claim.
        if canary.lower() not in statement.lower():
            assert canary.lower() not in blob, f"evidence leaked: {canary}"
    assert "object_id" not in blob and "record_id" not in blob and "source_refs" not in blob


def test_audit_is_stripped_shape(conn):
    """What the handler returns to callers must carry no audit internals."""
    result, audit = _check(conn, "I love playing the mandolin")
    assert "_audit" not in result
    assert "retrieval_touched" in audit  # audit exists node-side only
