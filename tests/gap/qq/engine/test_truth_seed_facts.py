"""truth_seed_fact: the truth feature's only write — fills the fun aperture,
can never widen it. Basics facts flow through to value-hidden prompts."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.query.truth_facts import seed_fun_fact
from topos.query.truth_prompts import suggest_prompts
from topos.query.verify import verify_claim

APP = "tru-test-app"


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    connection = sqlite3.connect(str(tmp_path / "seed.sqlite"))
    apply_all_migrations(connection)
    yield connection
    connection.close()


def _seed(conn, predicate, value):
    result = seed_fun_fact(conn, predicate=predicate, value=value, caller_app_id=APP)
    result.pop("_audit", None)
    return result


def test_basics_accepted(conn):
    assert _seed(conn, "goes_by", "Jonny")["accepted"] is True
    assert _seed(conn, "years_old", "34")["accepted"] is True
    assert _seed(conn, "hails_from", "Texas")["accepted"] is True
    assert _seed(conn, "favorite_food", "breakfast tacos")["accepted"] is True
    assert _seed(conn, "favorite_color", "orange")["accepted"] is True


def test_hobby_fact_accepted(conn):
    result = _seed(conn, "enjoys", "playing the mandolin")
    assert result["accepted"] is True and result["category"] == "hobbies"


def test_sensitive_fact_refused(conn):
    result = _seed(conn, "diagnosed_with", "pollen allergy")
    assert result["accepted"] is False and result["reason"] == "out_of_aperture"


def test_sensitive_value_refused_even_under_basics_predicate(conn):
    result = _seed(conn, "favorite_food", "whatever my therapist prescribed")
    assert result["accepted"] is False


def test_uncategorized_fact_refused(conn):
    result = _seed(conn, "believes_in", "the zorbular flange")
    assert result["accepted"] is False and result["reason"] == "out_of_aperture"


def test_door_policy(conn):
    assert seed_fun_fact(conn, predicate="enjoys", value="chess",
                         caller_app_id="")["reason"] == "caller_app_id_required"
    assert seed_fun_fact(conn, predicate="enjoys", value="chess", mode="serious",
                         caller_app_id=APP)["reason"] == "unknown_mode"


def test_basics_prompts_hide_values(conn):
    _seed(conn, "goes_by", "Jonny")
    _seed(conn, "favorite_color", "orange")
    _seed(conn, "hails_from", "Texas")
    result = suggest_prompts(conn, caller_app_id=APP)
    questions = [p["question"] for p in result["prompts"]]
    assert "Ask me my name" in questions
    assert "Ask me my favorite color" in questions
    assert "Ask me where I'm from" in questions
    blob = json.dumps(result["prompts"]).lower()
    for value in ("jonny", "orange", "texas"):
        assert value not in blob, f"basics value {value!r} leaked into a prompt"


def test_seeded_fact_powers_verify(conn):
    _seed(conn, "favorite_food", "breakfast tacos")
    result = verify_claim(conn, statement="my favorite food is breakfast tacos",
                          mode="fun", caller_app_id=APP)
    assert result["lanes"]["self"]["stance"] == "supports"


def test_basics_lie_contradicted_via_predicate_hint(conn):
    """A lie shares no tokens with the stored truth — predicate-hinted
    retrieval must surface the fact and the value mismatch must contradict
    (the 'My name is Timothy' field bug, 2026-07-27)."""
    _seed(conn, "goes_by", "Jonny")
    lie = verify_claim(conn, statement="My name is Timothy",
                       mode="fun", caller_app_id=APP)
    assert lie["lanes"]["self"]["stance"] == "contradicts"
    truth = verify_claim(conn, statement="My name is Jonny",
                         mode="fun", caller_app_id=APP)
    assert truth["lanes"]["self"]["stance"] == "supports"


def test_origin_lie_contradicted_via_predicate_hint(conn):
    _seed(conn, "hails_from", "Texas")
    lie = verify_claim(conn, statement="I'm from Norway originally",
                       mode="fun", caller_app_id=APP)
    assert lie["lanes"]["self"]["stance"] == "contradicts"
