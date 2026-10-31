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

def test_registry_contains_standard_and_fun():
    assert registered_mode_names() == ("fun", "standard")
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


# --- no fun mode unless the caller names it (owner, 31 Oct 2026; DR-64) --------------------------------------------

def _seed(conn, predicate, value, *, disclosure="scoped", subject="tru-owner-standard", source_refs=()):
    from topos.features.facts.store import FactStore

    FactStore(conn).assert_fact(subject_entity_id=subject, predicate=predicate, object_value=value, dimension="activity",
                                confidence=0.8, source_refs=list(source_refs), disclosure=disclosure)
    conn.commit()


def _default(conn, statement, **kwargs):
    result = verify_claim(conn, statement=statement, caller_app_id=APP, **kwargs)
    return result, result.pop("_audit", {})


def test_a_request_naming_no_mode_has_no_topic_gate(conn):
    _seed(conn, "works_on", "the quarterly budget spreadsheet")
    claim = "I work on the quarterly budget spreadsheet"
    assert _check(conn, claim)[1]["reason"] == "out_of_aperture"  # fun, named, still refuses it
    result, audit = _default(conn, claim)
    assert result["mode"] == audit["mode"] == "standard"
    assert audit["retrieval_touched"] is True
    assert audit["evidence_counts"]["self"] == 1
    assert result["lanes"]["self"]["stance"] == "supports"
    assert result["category"] is None


def test_the_default_has_no_sensitive_word_gate_either(conn):
    _seed(conn, "diagnosed_with", "seasonal pollen allergy")
    result, audit = _default(conn, "I was diagnosed with a seasonal pollen allergy")
    assert audit["evidence_counts"]["self"] == 1
    assert result["lanes"]["self"]["stance"] == "supports"
    assert result["category"] is None  # the reply never names a sensitive category


def test_the_default_never_reads_owner_only_or_calibration_facts(conn):
    _seed(conn, "works_on", "the private lighthouse ledger", disclosure="owner_only")
    _seed(conn, "calibration_drink", "lighthouse tea")
    result, audit = _default(conn, "I work on the lighthouse ledger and drink lighthouse tea")
    assert audit["evidence_counts"] == {"self": 0, "attributed": 0, "ambient": 0}
    assert result["abstained"] is True


def test_the_default_carries_no_evidence_text(conn):
    _seed(conn, "works_on", "the quarterly budget spreadsheet")
    result, _ = _default(conn, "I work on the quarterly budget spreadsheet")
    assert "spreadsheet" not in str(result)


class _Guard:
    """Blocks one entity, one record and one name, as a real guard answers for the Off-limits list."""

    def blocks_entity_id(self, entity_id):
        return entity_id == "tru-protected"

    def blocks_record_id(self, record_id):
        return record_id == "tru-protected-record"

    def text_mentions_blackholed(self, text):
        return "Quillfeather" in (text or "")


class _NoEntries(_Guard):
    def blocks_entity_id(self, entity_id):
        return False

    def blocks_record_id(self, record_id):
        return False

    def text_mentions_blackholed(self, text):
        return False


@pytest.mark.parametrize("mode", ["standard", "fun"])
@pytest.mark.parametrize(
    "seed",
    [
        {"subject": "tru-protected"},
        {"source_refs": [{"table": "messages", "record_id": "tru-protected-record"}]},
        {"value": "playing chess with Quillfeather"},
    ],
    ids=["by_subject", "by_source_record", "by_name_in_the_text"],
)
def test_off_limits_facts_are_never_evidence_in_any_mode(conn, mode, seed):
    seed = dict(seed)
    value = seed.pop("value", "playing chess with a neighbour")
    _seed(conn, "enjoys", value, **seed)
    statement = f"I enjoy {value}"
    unguarded = verify_claim(conn, statement=statement, mode=mode, caller_app_id=APP, guard=_NoEntries())
    guarded = verify_claim(conn, statement=statement, mode=mode, caller_app_id=APP, guard=_Guard())
    assert guarded["_audit"]["evidence_counts"]["self"] == unguarded["_audit"]["evidence_counts"]["self"] - 1


def test_prompts_and_seeding_do_not_follow_the_default(conn):
    from topos.query.truth_facts import seed_fun_fact
    from topos.query.truth_prompts import suggest_prompts

    assert suggest_prompts(conn, caller_app_id=APP)["mode"] == "fun"
    assert suggest_prompts(conn, mode="standard", caller_app_id=APP)["prompts"] == []
    refused = seed_fun_fact(conn, predicate="enjoys", value="pizza", mode="standard", caller_app_id=APP)
    assert refused["accepted"] is False
