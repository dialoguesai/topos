"""Vector-search-driven graph filtering (PLAN_GRAPH_VECTOR_SEARCH Phase 1).

graph_search takes an injectable search_fn (the real endpoint wires
SignalService.search_vectors) so these tests run without the embedding stack:
records + scores in, ranked entities + evidence out.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.graph_search import graph_search
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _mention(conn, entity_id, record_id):
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, surface_text, confidence, created_at) "
        "VALUES (?, ?, ?, 'src', 'x', 0.9, '2026-06-01')",
        (f"m_{entity_id}_{record_id}", entity_id, record_id),
    )


def _fake_search(items):
    calls = []

    def search_fn(*, query, limit, event_after=None, event_before=None):
        calls.append({"query": query, "limit": limit, "event_after": event_after, "event_before": event_before})
        return {"items": items, "total": len(items)}

    search_fn.calls = calls  # type: ignore[attr-defined]
    return search_fn


def test_entities_ranked_by_summed_record_similarity(conn):
    r = EntityResolver(conn)
    a = r._create_entity("Alpha School", "org")
    b = r._create_entity("Beta Corp", "org")
    conn.commit()
    _mention(conn, a, "rec1")
    _mention(conn, b, "rec2")
    _mention(conn, b, "rec3")
    conn.commit()

    fn = _fake_search([
        {"record_id": "rec1", "similarity": 0.9, "text_preview": "edtech pilot at Alpha", "source_id": "chatgpt", "event_at": "2026-06-12"},
        {"record_id": "rec2", "similarity": 0.5, "text_preview": "beta note", "source_id": "browser", "event_at": None},
        {"record_id": "rec3", "similarity": 0.3, "text_preview": "beta again", "source_id": "browser", "event_at": None},
    ])
    out = graph_search(conn, query="edtech pilots", search_fn=fn)

    ids = [e["entity_id"] for e in out["entities"]]
    assert ids[0] == a  # 0.9 beats 0.5 + 0.3
    assert ids[1] == b
    assert out["records_considered"] == 3
    top = out["entities"][0]
    assert top["label"] == "Alpha School"
    assert top["evidence"][0]["snippet"] == "edtech pilot at Alpha"
    assert top["evidence"][0]["similarity"] == 0.9  # raw cosine for display
    # Scores are normalized so the top record = 1.0 (hybrid RRF scores live on
    # a tiny scale that fixed label bonuses would otherwise dwarf):
    # a = 0.9/0.9 = 1.0; b = (0.5 + 0.3)/0.9 ≈ 0.889.
    assert abs(top["score"] - 1.0) < 1e-6
    assert abs(out["entities"][1]["score"] - 0.8 / 0.9) < 1e-4


def test_event_window_passes_through_to_search_fn(conn):
    fn = _fake_search([])
    graph_search(conn, query="q", search_fn=fn, event_after="2026-06-01", event_before="2026-07-01")
    assert fn.calls[0]["event_after"] == "2026-06-01"
    assert fn.calls[0]["event_before"] == "2026-07-01"


def test_materialized_goal_node_matches_by_label(conn):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self, mention_count, metadata_json) "
        "VALUES ('goal_g1', 'goal', 'Summarize edtech pilot opportunities in Austin', "
        "'summarize edtech pilot opportunities in austin', 0, 0, '{\"mz\":1}')"
    )
    conn.commit()
    out = graph_search(conn, query="edtech pilots", search_fn=_fake_search([]))
    ids = [e["entity_id"] for e in out["entities"]]
    assert "goal_g1" in ids
    goal = next(e for e in out["entities"] if e["entity_id"] == "goal_g1")
    assert goal["score"] > 0
    assert goal["evidence"][0]["kind"] == "label"


def test_record_and_label_evidence_sum_for_same_entity(conn):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self, mention_count, metadata_json) "
        "VALUES ('tc_1', 'topic', 'edtech / pilots', 'edtech pilots', 0, 0, '{\"mz\":1}')"
    )
    _mention(conn, "tc_1", "rec1")
    conn.commit()
    fn = _fake_search([
        {"record_id": "rec1", "similarity": 0.6, "text_preview": "p", "source_id": "s", "event_at": None},
    ])
    out = graph_search(conn, query="edtech pilots", search_fn=fn)
    tc = next(e for e in out["entities"] if e["entity_id"] == "tc_1")
    assert tc["score"] > 0.6  # record evidence + label bonus
    kinds = {ev.get("kind", "record") for ev in tc["evidence"]}
    assert {"record", "label"} <= kinds


def test_label_matching_ignores_stopwords(conn):
    """'X and Y' labels must not match every query containing 'and'."""
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self, mention_count, metadata_json) "
        "VALUES ('tc_war', 'topic', 'Man and Woman, Peace and War', "
        "'man and woman peace and war', 0, 0, '{\"mz\":1}')"
    )
    conn.commit()
    out = graph_search(conn, query="provenance and privacy architecture", search_fn=_fake_search([]))
    assert all(e["entity_id"] != "tc_war" for e in out["entities"])


def test_no_matches_returns_empty_shape(conn):
    out = graph_search(conn, query="zzz nothing", search_fn=_fake_search([]))
    assert out["entities"] == []
    assert out["records_considered"] == 0
    assert out["query"] == "zzz nothing"
