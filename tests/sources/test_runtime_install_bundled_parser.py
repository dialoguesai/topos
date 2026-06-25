"""Bundled parser/mapper ids must survive uninstall of runtime-installed sources."""

from __future__ import annotations

from topos.ingestion.parsers import PARSER_REGISTRY
from topos.ingestion.parsers.demo_file_parsers import JournalTimeLogFileParser
from topos.sources.runtime_install import install_source_definition


def test_uninstall_bundled_parser_source_does_not_remove_shared_parser() -> None:
    time_log_payload = {
        "source_id": "time_log",
        "display_name": "Time Log",
        "source_type": "ui_stream",
        "schema_id": "journal.time_log.v1",
        "parser_id": "journal.time_log.v1",
        "canonical_group_id": "journal",
        "ingestion_trigger": "automatic",
        "enrichment_trigger": "manual",
        "default_scope_id": "health",
        "allowed_scope_ids": ["health:read"],
        "tables": [
            {
                "table_id": "time_log_sessions",
                "display_name": "Time Log Sessions",
                "columns": [{"name": "startDate", "type": "text"}],
            }
        ],
        "pipeline_include_data_table": True,
    }

    assert PARSER_REGISTRY.get("journal.time_log.v1") is not None

    handle = install_source_definition(time_log_payload)
    assert PARSER_REGISTRY.get("journal.time_log.v1") is not None

    from topos.sources.canonical_signal_defaults import resolved_signal_derivation_jobs
    from topos.sources.registry import REGISTRY, topic_cluster_source_ids

    installed = REGISTRY.get("time_log")
    assert installed is not None
    jobs = resolved_signal_derivation_jobs(installed)
    assert "topic_clusters" in jobs
    assert "embeddings" in jobs
    assert "time_log" in topic_cluster_source_ids()

    handle.uninstall()

    assert PARSER_REGISTRY.get("journal.time_log.v1") is not None, (
        "Uninstalling a bundled-parser source must not pop shared parser ids from PARSER_REGISTRY"
    )


def test_install_time_log_with_data_table_preserves_bundled_parser() -> None:
    """Runtime install must not replace JournalTimeLogFileParser with generic passthrough parser."""
    before = PARSER_REGISTRY.get("journal.time_log.v1")
    assert before is JournalTimeLogFileParser

    payload = {
        "source_id": "time_log",
        "display_name": "Time Log",
        "source_type": "ui_stream",
        "schema_id": "journal.time_log.v1",
        "parser_id": "journal.time_log.v1",
        "canonical_group_id": "journal",
        "ingestion_trigger": "automatic",
        "enrichment_trigger": "manual",
        "default_scope_id": "health",
        "allowed_scope_ids": ["health:read"],
        "pipeline_include_data_table": True,
        "tables": [
            {
                "table_id": "time_log_sessions",
                "display_name": "Sessions",
                "columns": [{"name": "record_id", "type": "text", "primary_key": True}],
            }
        ],
    }

    handle = install_source_definition(payload)
    try:
        assert PARSER_REGISTRY.get("journal.time_log.v1") is JournalTimeLogFileParser
    finally:
        handle.uninstall()


def test_uninstall_time_log_bundled_triple_preserves_parser() -> None:
    payload = {
        "source_id": "time_log",
        "display_name": "Time Log",
        "source_type": "ui_stream",
        "schema_id": "journal.time_log.v1",
        "parser_id": "journal.time_log.v1",
        "canonical_group_id": "journal",
        "ingestion_trigger": "automatic",
        "enrichment_trigger": "manual",
        "default_scope_id": "health",
        "allowed_scope_ids": ["health:read"],
    }

    handle = install_source_definition(payload)
    handle.uninstall()

    assert PARSER_REGISTRY.get("journal.time_log.v1") is not None
