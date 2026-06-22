"""Tests for per-dimension brief record selection."""

from __future__ import annotations

from topos.enrichment.jobs.canonical.dimension_summary_job import _records_for_brief_dimension


def test_records_for_brief_dimension_filters_by_source_group() -> None:
    batch = [
        {"source_id": "demo_financial_file", "transaction_id": "f1", "description": "Payroll deposit"},
        {"source_id": "chatgpt_file_ingestion", "message_id": "m1", "content": "signal dimensions sprint"},
    ]
    resources_recs = _records_for_brief_dimension("resources", batch, fallback_source_id=None, conn=None)
    assert len(resources_recs) == 1
    assert resources_recs[0]["source_id"] == "demo_financial_file"

    intentions_recs = _records_for_brief_dimension("intentions", batch, fallback_source_id=None, conn=None)
    assert len(intentions_recs) == 1
    assert intentions_recs[0]["source_id"] == "chatgpt_file_ingestion"


def test_profile_dimension_uses_profile_group_only() -> None:
    batch = [
        {"source_id": "demo_resume_file", "record_id": "p1", "title": "Staff Engineer"},
        {"source_id": "chatgpt_file_ingestion", "message_id": "m1", "content": "git cherry-pick"},
    ]
    profile_recs = _records_for_brief_dimension("profile", batch, fallback_source_id=None, conn=None)
    assert len(profile_recs) == 1
    assert profile_recs[0]["source_id"] == "demo_resume_file"
