"""truth_prompts: "ask me" seeds obey the fun aperture and hide stances.

Prompts are published INTO the video frame (the other caller sees them), so
the leak bar is the same hard zero as verify_claim: no owner_only or
sensitive topic may ever surface, and no prompt may reveal which way the
owner feels (the predicate must not appear)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from truth_seed_corpus import build_truth_corpus

from topos.query.truth_prompts import suggest_prompts

APP = "tru-test-app"


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    connection = sqlite3.connect(str(tmp_path / "prompts.sqlite"))
    apply_all_migrations(connection)
    build_truth_corpus(connection)
    yield connection
    connection.close()


def test_prompts_come_from_fun_eligible_topics_only(conn):
    result = suggest_prompts(conn, caller_app_id=APP)
    questions = " ".join(p["question"].lower() for p in result["prompts"])
    assert result["prompts"], "expected prompts from the seeded fun facts"
    # Trap facts must never surface: owner_only fun fact + sensitive fact.
    assert "knitting" not in questions
    assert "allergy" not in questions and "pollen" not in questions


def test_prompts_hide_stance(conn):
    result = suggest_prompts(conn, caller_app_id=APP)
    blob = json.dumps(result["prompts"]).lower()
    for stance_word in ("enjoy", "dislike", "love", "hate", "collect", "play "):
        assert stance_word not in blob, f"stance leaked via {stance_word!r}"
    # Topics themselves are the deliberate disclosure.
    assert any("mandolin" in p["question"] for p in result["prompts"])


def test_prompts_dedupe_and_limit(conn):
    result = suggest_prompts(conn, caller_app_id=APP, limit=2)
    assert len(result["prompts"]) == 2
    topics = [p["question"] for p in result["prompts"]]
    assert len(set(topics)) == 2


def test_prompts_no_evidence_shape(conn):
    result = suggest_prompts(conn, caller_app_id=APP)
    result.pop("_audit")
    blob = json.dumps(result).lower()
    assert "record_id" not in blob and "object_id" not in blob
    assert "asserted_by" not in blob and "predicate" not in blob


def test_prompts_door_policy(conn):
    assert suggest_prompts(conn, mode="serious", caller_app_id=APP)["error"] == "unknown_mode"
    assert suggest_prompts(conn, caller_app_id="")["error"] == "caller_app_id_required"


def test_prompts_empty_store_is_empty_not_error(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    connection = sqlite3.connect(str(tmp_path / "empty.sqlite"))
    apply_all_migrations(connection)
    result = suggest_prompts(connection, caller_app_id=APP)
    assert result["prompts"] == []
    connection.close()
