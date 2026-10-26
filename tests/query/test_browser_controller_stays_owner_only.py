"""Who drove a browser tab is the owner's to see, never a grantee's.

topos#195's independent review (finding 2) showed `actor_kind` and a `[browser controller]` text prefix on retrieval
items at every disclosure tier, so a grantee could tell which visits an AI agent drove. Below `owner_raw` a browser
row must read exactly as it did before attribution existed, whoever controlled the tab.
"""
from __future__ import annotations

import json

import pytest

from topos.query.manifest import ScopeResolutionManifest

AGENT = {"kind": "agent", "method": "controller_declared", "provider": "openai", "name": "Codex",
         "conversation_source": "codex_sessions", "conversation_id": "codex:synthetic-parent"}
HUMAN = {"kind": "human", "method": "manual"}
ACTOR_FIELDS = {"actor_kind", "personal_interest_eligible", "browser_actor", "speaker_label"}


def _row(event_id, actor):
    return {"record_id": event_id, "event_id": event_id, "source_id": "browser_visits", "activity_type": "visit",
            "title": "Copper still methods", "url": "https://example.test/copper", "occurred_at": "2026-10-01T10:00:00Z",
            "metadata_json": json.dumps({"browser_actor": actor} if actor is not None else {})}


def _item(row, tier):
    from topos.query.retrieval import _canonical_row_to_item

    return _canonical_row_to_item(
        "activity_events", row,
        manifest=ScopeResolutionManifest(scope_id="owner:read", primary_dimensions=["memory"],
                                         canonical_tables=["activity_events"]),
        query_text="copper still", conn=None, first_person=False, belief_intent=False, exposure_visible=True,
        role_cache={}, display_cache={}, highlight_cache={}, disclosure_tier=tier,
    )


@pytest.mark.parametrize("tier", ["default_disclosure", "summary", "facts"])
def test_below_owner_raw_an_agent_visit_reads_like_any_other_visit(tier):
    agent, human, legacy = (_item(_row(f"browser:{n}", a), tier) for n, a in (("a", AGENT), ("h", HUMAN), ("l", None)))
    assert agent is not None and human is not None and legacy is not None
    for item in (agent, human, legacy):
        assert not ACTOR_FIELDS & set(item), item
    strip = lambda item: {k: v for k, v in item.items() if k not in {"record_id", "event_id", "id", "relevance_score"}}
    assert strip(agent) == strip(human) == strip(legacy)
    assert "Codex" not in json.dumps(agent) and "controller" not in json.dumps(agent)


def test_the_owner_sees_who_drove_the_tab():
    agent = _item(_row("browser:a", AGENT), "owner_raw")
    assert agent["actor_kind"] == "agent" and agent["personal_interest_eligible"] is False
    assert agent["browser_actor"]["conversation_id"] == AGENT["conversation_id"]
    assert "Codex" in (agent.get("summary_text") or agent.get("text") or "")
    legacy = _item(_row("browser:l", None), "owner_raw")
    assert legacy["actor_kind"] == "human" and legacy["personal_interest_eligible"] is True
    assert legacy["browser_actor"]["method"] == "default_human"
