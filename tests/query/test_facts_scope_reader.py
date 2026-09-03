"""facts:read / facts_sensitive:read have a reader — the flip's green case.

protects: a live facts scope answers facts end-to-end; the sensitive split
carries the special-class gate; a dict-valued fact can never crash the turn.

The S5 rule: a scope flips stub -> live only WITH its reader, proven by a
case that drives the real pipeline. This battery is that case, hermetic.
Live counterpart: qq-catalog F1/F2 on the owner snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.asyncio


def _seed_facts(conn: sqlite3.Connection) -> None:
    # The owner's packet-resolution dial: facts_direct serves content only at
    # facts/facts_all (a fresh DB defaults scores_only and the lane stays
    # silent — the case must model an owner who opted in, as the live node has).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO engine_config (key, value) VALUES ('packet_resolution', 'facts_all')"
    )
    conn.execute(
        "INSERT INTO entities (entity_id, canonical_name, normalized_name, entity_type, is_self)"
        " VALUES ('ent-self', 'Owner', 'owner', 'person', 1)"
    )
    rows = [
        # standard-class: a work project (legacy vocabulary, no sensitivity key)
        (
            "fact-1",
            "fact:ent-self:works_on:quartz",
            {"predicate": "works_on", "object_value": "Quartz Pipeline",
             "subject_entity_id": "ent-self"},
        ),
        # special-class: a derivation-written mind fact with a value_struct DICT
        # (the shape that crashed the live turn at the game layer's float()).
        (
            "fact-2",
            "fact:ent-self:mind.self_reported_state:s1",
            {"predicate": "mind.self_reported_state",
             "value_struct": {"dimension": "stress", "report": "steady week"},
             "subject_entity_id": "ent-self", "sensitivity": "special"},
        ),
    ]
    for oid, key, payload in rows:
        conn.execute(
            "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key,"
            " payload_json, confidence, valid_from, created_by, created_at, updated_at)"
            " VALUES (?, 'facts', 'fact', ?, ?, 0.9, '2026-08-01T00:00:00', 'test',"
            " '2026-08-01T00:00:00', '2026-08-01T00:00:00')",
            (oid, key, json.dumps(payload)),
        )
    conn.commit()


@pytest.fixture()
def orch(tmp_path: Path):
    db = tmp_path / "facts.db"
    conn = sqlite3.connect(str(db))
    apply_all_migrations(conn)
    _seed_facts(conn)
    conn.close()
    adapters = AdapterFactory.create("local_database", db_path=db)
    return QueryPipelineOrchestrator(adapters=adapters)


async def _run(orch, scope, mode, q):
    return await orch.execute(
        query_text=q,
        scope_id=scope,
        access_mode=mode,
        manifest=resolve_scope_manifest(scope),
        query_session_id=f"facts-reader-{scope.split(':')[0]}-{mode}",
    )


async def test_facts_read_inference_answers_facts(orch):
    out = await _run(orch, "facts:read", "inference", "What do I work on?")
    pr = out.get("public_result") or {}
    assert out.get("turn_outcome") == "live_query"
    assert pr.get("answer_type") == "facts", pr.get("answer_type")
    preds = {f.get("predicate") for f in pr.get("facts") or []}
    assert "works_on" in preds


async def test_sensitive_scope_serves_special_class_deterministically(orch):
    out = await _run(
        orch, "facts_sensitive:read", "inference", "What is my self reported state?"
    )
    pr = out.get("public_result") or {}
    assert out.get("turn_outcome") == "live_query"
    assert pr.get("answer_type") == "facts"
    preds = {f.get("predicate") for f in pr.get("facts") or []}
    assert "mind.self_reported_state" in preds


async def test_dict_valued_fact_never_crashes_the_turn(orch):
    """The live 2026-09-03 crash shape: an inference turn whose packet's top
    score is a dict-valued fact. The turn must complete, whatever it answers."""
    out = await _run(
        orch, "facts_sensitive:read", "inference", "How have things been lately?"
    )
    assert out.get("turn_outcome") in ("live_query", "denied")


async def test_facts_summary_mode_serves_fact_items(orch):
    out = await _run(orch, "facts:read", "summary", "What do you know about my work?")
    pr = out.get("public_result") or {}
    assert out.get("turn_outcome") == "live_query"
    items = pr.get("summaries") or []
    assert any(
        str(i.get("retrieval_source", "")).startswith("fact") for i in items
    ), f"no fact items among {len(items)} summaries"
