"""Canonical-mapped sources receive baseline signal jobs without explicit definition."""

from __future__ import annotations

from dataclasses import replace

from topos.sources.canonical_signal_defaults import (
    CANONICAL_BASELINE_SIGNAL_JOBS,
    maps_to_canonical_table,
    resolved_signal_derivation_jobs,
)
from topos.sources.registry import DEMO_JOURNAL_FILE, topic_cluster_source_ids
from topos.sources.runtime_install import install_source_definition


def test_maps_to_canonical_table_when_group_or_mapper_present() -> None:
    assert maps_to_canonical_table(DEMO_JOURNAL_FILE) is True
    assert maps_to_canonical_table(
        replace(
            DEMO_JOURNAL_FILE,
            canonical_group_id=None,
            canonical_mapper_id=None,
            schema_id="unknown.schema.v1",
            parser_id="unknown.schema.v1",
        )
    ) is False


def test_resolved_jobs_include_baseline_for_canonical_sources() -> None:
    bare_journal = replace(
        DEMO_JOURNAL_FILE,
        source_id="time_log",
        source_type="ui_stream",
        schema_id="journal.time_log.v1",
        parser_id="journal.time_log.v1",
        canonical_mapper_id="journal_time_log",
        signal_derivation_jobs=[],
    )
    jobs = resolved_signal_derivation_jobs(bare_journal)
    for job in CANONICAL_BASELINE_SIGNAL_JOBS:
        assert job in jobs


def test_resolved_jobs_merge_lane_specific_extras() -> None:
    journal_with_emo = replace(
        DEMO_JOURNAL_FILE,
        signal_derivation_jobs=["emo_27", "goal_extraction"],
    )
    jobs = resolved_signal_derivation_jobs(journal_with_emo)
    assert "emo_27" in jobs
    assert "goal_extraction" in jobs
    assert "embeddings" in jobs
    assert "topic_clusters" in jobs


def test_runtime_time_log_install_gets_baseline_without_definition_jobs() -> None:
    handle = install_source_definition(
        {
            "source_id": "time_log",
            "display_name": "Time Log",
            "source_type": "ui_stream",
            "schema_id": "journal.time_log.v1",
            "parser_id": "journal.time_log.v1",
            "canonical_mapper_id": "journal_time_log",
            "canonical_group_id": "journal",
            "pipeline_include_data_table": True,
            "tables": [
                {
                    "table_id": "time_log_sessions",
                    "display_name": "Sessions",
                    "columns": [{"name": "record_id", "type": "text", "primary_key": True}],
                }
            ],
        }
    )
    try:
        from topos.sources.registry import REGISTRY

        installed = REGISTRY["time_log"]
        jobs = resolved_signal_derivation_jobs(installed)
        assert "embeddings" in jobs
        assert "topic_clusters" in jobs
        assert "dimension_summary" in jobs
        assert "relationship_edges" in jobs
        assert "time_log" in topic_cluster_source_ids()
    finally:
        handle.uninstall()
