"""The vector lane's best match reaches the step that writes the answer.

`semantic_hits` never leave the node in summary mode: the game layer rebuilds
`public_result` from `summaries` alone, and home chat's synthesis strips the key even if it
arrived (`FORBIDDEN_SYNTHESIS_KEYS` in topos-react-app `next/lib/mcp/homeChatOrchestration.ts`)
and reads the first 12 summaries (`SYNTHESIS_MAX_SUMMARIES`). So a record the vector lane found
reaches an answer only through the fused list, and only near its head.

It did not get there. The vector lane fuses at weight 1.0 with recency decay; stat insights fuse
at 2.0 and facts, entities and derived objects at 1.5, none decayed. With k=60 a stat insight at
rank 60 still outscores the vector lane's first hit. Measured on an owner snapshot 2026-10-21
(112 questions, counts only): the vector lane held the answering record for 60-75% of ChatGPT,
coding and GitHub questions, as its FIRST hit in 121 of 300 known-item turns, and the fused list
held it in about 1 in 20.

Every record and question here is invented. No database is opened: the adapters are the
in-memory fakes and the global connection is stubbed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

import topos.query.retrieval as retrieval
from topos.query.retrieval import _rrf_fuse_summary_lists

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)

#: What home chat's synthesis reads of one scope's `summaries`.
ANSWER_STEP_SUMMARIES = 12

SCOPE = "ai_conversations:read"
QUESTION = "What did I settle on for the greenhouse watering schedule?"
ANCHOR = "chat-greenhouse-drip"
ANCHOR_TEXT = "Settled the drip timer: two short cycles at dawn, skipped whenever the soil probe reads wet."


def _vector_hit(record_id: str, text: str, similarity: float, days_old: int) -> Dict[str, Any]:
    return {
        "record_id": record_id,
        "text_preview": text[:200],
        "search_text": text,
        "similarity": similarity,
        "source_id": "chatgpt",
        "record_type": "chatgpt_message",
        "event_at": (NOW - timedelta(days=days_old)).isoformat(),
    }


#: The answer, and four weaker neighbours. Five months old: decayed to the floor.
VECTOR_LANE = [_vector_hit(ANCHOR, ANCHOR_TEXT, 0.71, 150)] + [
    _vector_hit(f"chat-neighbour-{i}", f"Seed tray notes, batch {i}, labels and dates.", 0.45 - i * 0.01, 150 + i)
    for i in range(4)
]

#: A dense corpus's standing statistics: undecayed, at twice the vector lane's weight.
STAT_LANE = [
    {
        "record_id": f"stat-{i}",
        "topic": f"Conversation pattern {i}",
        "summary_text": f"You held {10 + i} conversations about planning in the last quarter.",
        "retrieval_source": "stat_insight",
        "relevance_score": 0.9,
    }
    for i in range(40)
]


def _ask(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """One owner turn through the real orchestrator; returns what the answer step is handed."""
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.pipeline import QueryPipelineOrchestrator
    from topos.storage.adapters.factory import AdapterFactory
    from topos.features.signal.derived_index import DERIVED_SOURCE_ID

    def semantic_hits(query_text: str, *, source_id: Any = None, **_: Any):
        if source_id == DERIVED_SOURCE_ID:
            return [], None
        return [dict(hit) for hit in VECTOR_LANE], None

    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: None)
    monkeypatch.setattr(retrieval, "_semantic_hits", semantic_hits)
    monkeypatch.setattr(retrieval, "_load_stat_insight_items", lambda *a, **k: [dict(i) for i in STAT_LANE])
    monkeypatch.setattr(retrieval, "_load_ranked_clusters", lambda *a, **k: [])
    orch = QueryPipelineOrchestrator(adapters=AdapterFactory.create("memory"))
    token = set_principal(Principal(OWNER_APP, "uds"))
    try:
        result = asyncio.run(orch.execute(
            query_text=QUESTION, scope_id=SCOPE, access_mode="summary",
            manifest=resolve_scope_manifest(SCOPE), now=NOW,
        ))
    finally:
        reset_principal(token)
    assert result["turn_outcome"] == "live_query"
    return result["public_result"]


def test_the_vector_lane_first_hit_is_in_what_the_answer_step_reads(monkeypatch):
    public = _ask(monkeypatch)
    assert "semantic_hits" not in public  # the fused list is the only way through
    read = public["summaries"][:ANSWER_STEP_SUMMARIES]
    found = [item for item in read if item.get("record_id") == ANCHOR]
    assert found, "the vector lane's first hit did not reach the answer step"
    assert found[0]["summary_text"] == ANCHOR_TEXT


def test_the_switch_turns_the_head_off(monkeypatch):
    monkeypatch.setenv("TOPOS_FUSION_VECTOR_HEAD", "0")
    public = _ask(monkeypatch)
    assert ANCHOR not in {item.get("record_id") for item in public["summaries"][:ANSWER_STEP_SUMMARIES]}


def _lanes() -> List[Any]:
    stats = [{"record_id": f"s{i}", "summary_text": "Standing statistic"} for i in range(40)]
    vector = [
        {"record_id": "v0", "summary_text": "Drip timer at dawn", "event_at": "2026-05-01T00:00:00Z"},
        {"record_id": "v1", "summary_text": "Seed tray labels", "event_at": "2026-05-01T00:00:00Z"},
        {"record_id": "v2", "summary_text": "Compost turning", "event_at": "2026-05-01T00:00:00Z"},
    ]
    return [("stat_insights", 2.0, stats), ("vector", 1.0, vector)]


def test_head_is_the_lane_own_order_and_only_as_deep_as_asked():
    out = _rrf_fuse_summary_lists(_lanes(), now=NOW, vector_head=2)
    assert [i["record_id"] for i in out[:2]] == ["v0", "v1"]
    assert "v2" not in {i["record_id"] for i in out}
    assert len(out) == 25
    assert _rrf_fuse_summary_lists(_lanes(), now=NOW, vector_head=0) == _rrf_fuse_summary_lists(_lanes(), now=NOW)


def test_specific_ask_keeps_its_witness_first_and_still_gets_the_head():
    lanes = _lanes() + [("canonical", 1.0, [{"record_id": "needle", "summary_text": "Indigo badger survey"}])]
    out = _rrf_fuse_summary_lists(lanes, now=NOW, vector_head=1, rare_token_groups=[{"indigo": 1}])
    assert [i["record_id"] for i in out[:2]] == ["needle", "v0"]


def test_sub_floor_context_hits_are_never_pinned():
    lanes = [lane for lane in _lanes() if lane[0] != "vector"] + [
        ("vector_context", 0.8, [{"record_id": "weak", "summary_text": "Loosely related"}]),
    ]
    out = _rrf_fuse_summary_lists(lanes, now=NOW, vector_head=2, context_sources=frozenset({"vector_context"}))
    assert out[0]["record_id"] != "weak"


def test_head_cannot_evict_a_lane_floor():
    lanes = _lanes() + [("goals", 1.0, [{"record_id": f"g{i}", "summary_text": "Goal"} for i in range(2)])]
    out = _rrf_fuse_summary_lists(lanes, now=NOW, vector_head=2, min_per_source={"goals": 2})
    ids = [i["record_id"] for i in out]
    assert ids[:2] == ["v0", "v1"]
    assert {"g0", "g1"} <= set(ids)
    assert len(ids) == 25


def test_activity_window_keeps_its_day_coverage_contract():
    window = ("2026-04-28T00:00:00+00:00", "2026-05-04T23:59:59+00:00")
    lanes = [("recent", 1.0, [
        {"record_id": f"r{i}", "summary_text": "Day entry", "event_at": f"2026-05-0{1 + i % 3}T0{i % 9}:00:00Z"}
        for i in range(30)
    ]), ("vector", 1.0, [{"record_id": "v0", "summary_text": "Drip timer", "event_at": "2026-04-29T00:00:00Z"}])]
    assert _rrf_fuse_summary_lists(lanes, now=NOW, vector_head=2, activity_time_range=window) == \
        _rrf_fuse_summary_lists(lanes, now=NOW, activity_time_range=window)
