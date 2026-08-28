"""Tests for time-log journal → Time dimension artifact extraction."""

from __future__ import annotations

import json

from topos.features.signal.extraction.rule_extractors import extract_artifacts


def test_time_log_journal_emits_busy_interval_for_time_dimension() -> None:
    record = {
        "entry_id": "tl-1",
        "entry_at": "2026-05-01T08:00:00",
        "category": "Topos",
        "content": "Project: Topos",
        "metadata_json": json.dumps(
            {
                "ends_at": "2026-05-01T08:55:00",
                "duration_minutes": "55",
                "location": "Home",
            }
        ),
    }
    drafts = extract_artifacts("journal_entries", record)
    intervals = [d for d in drafts if d[0] == "Interval"]
    places = [d for d in drafts if d[0] == "EntityRef" and "places" in d[2]]
    assert len(intervals) == 1
    assert len(places) == 1
    artifact_type, payload, affinity, refs, confidence = intervals[0]
    assert artifact_type == "Interval"
    assert payload["start"] == "2026-05-01T08:00:00"
    assert payload["end"] == "2026-05-01T08:55:00"
    assert payload["availability_kind"] == "busy"
    assert "time" in affinity
    assert refs[0]["table"] == "journal_entries"
    assert confidence >= 0.9


def test_journal_without_end_time_skips_interval() -> None:
    record = {
        "entry_id": "j1",
        "entry_at": "2026-03-11T07:00:00",
        "content": "Rested well",
        "metadata_json": "{}",
    }
    assert extract_artifacts("journal_entries", record) == []


def test_time_log_journal_group_emits_relationship_edges() -> None:
    record = {
        "entry_id": "tl-42",
        "entry_at": "2026-05-01T10:00:00",
        "category": "Topos",
        "content": "Project: Topos",
        "people": "Mitch, Claire",
        "metadata_json": json.dumps(
            {
                "ends_at": "2026-05-01T11:00:00",
            }
        ),
    }
    drafts = extract_artifacts("journal_entries", record)
    intervals = [d for d in drafts if d[0] == "Interval"]
    edges = [d for d in drafts if d[0] == "Edge"]
    assert len(intervals) == 1
    assert len(edges) == 2
    assert all("relationships" in d[2] for d in edges)
    edge_keys = {d[1]["target_entity_key"] for d in edges}
    assert edge_keys == {"mitch", "claire"}


def test_time_log_journal_place_emits_places_artifact() -> None:
    record = {
        "entry_id": "tl-55",
        "entry_at": "2026-05-01T10:00:00",
        "category": "Topos",
        "content": "Project: Topos",
        "place_name": "Elmcourt Apt",
        "metadata_json": json.dumps({"ends_at": "2026-05-01T11:00:00"}),
    }
    drafts = extract_artifacts("journal_entries", record)
    places = [d for d in drafts if d[0] == "EntityRef" and "places" in d[2]]
    assert len(places) == 1
    assert places[0][1]["display_band"] == "Elmcourt Apt"


def test_time_log_journal_goal_emits_intentions_artifact() -> None:
    record = {
        "entry_id": "tl-1",
        "entry_at": "2026-05-01T08:00:00",
        "category": "Job Applications",
        "content": "Project: Job Applications\n\nGoal: Update resume",
        "metadata_json": json.dumps(
            {
                "ends_at": "2026-05-01T08:55:00",
                "goal": "Update resume",
                "completed": True,
            }
        ),
    }
    drafts = extract_artifacts("journal_entries", record)
    goals = [d for d in drafts if d[0] == "Goal"]
    assert len(goals) == 1
    assert goals[0][1]["goal_text"] == "Update resume"
    assert "intentions" in goals[0][2]


def test_time_log_journal_category_emits_profile_claim() -> None:
    record = {
        "entry_id": "tl-7",
        "entry_at": "2026-05-02T12:33:00",
        "category": "Topos",
        "content": "Project: Topos",
        "metadata_json": json.dumps({"ends_at": "2026-05-02T13:52:00"}),
    }
    drafts = extract_artifacts("journal_entries", record)
    claims = [d for d in drafts if d[0] == "Claim" and "profile" in d[2]]
    assert len(claims) == 1
    assert claims[0][1]["label"] == "Topos"
    assert claims[0][1]["claim_kind"] == "domain"
