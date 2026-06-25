"""Bundled schema/parser infers canonical mapper; connected flag is deprecated."""

from __future__ import annotations

import pytest

from topos.sources.bundled_canonical_triples import (
    apply_bundled_canonical_defaults,
    infer_bundled_canonical_triple,
    normalize_canonical_source_payload,
)
from topos.sources.registry import DEMO_JOURNAL_FILE


def test_infer_journal_time_log_triple() -> None:
    assert infer_bundled_canonical_triple(schema_id="journal.time_log.v1") == (
        "journal_time_log",
        "journal",
    )


def test_normalize_fills_mapper_from_group_and_schema() -> None:
    payload = normalize_canonical_source_payload(
        {
            "source_id": "time_log",
            "source_type": "ui_stream",
            "schema_id": "journal.time_log.v1",
            "parser_id": "journal.time_log.v1",
            "canonical_group_id": "journal",
            "canonical_mapping_connected": True,
        }
    )
    assert payload["canonical_mapper_id"] == "journal_time_log"
    assert payload["canonical_group_id"] == "journal"
    assert "canonical_mapping_connected" not in payload


def test_normalize_rejects_group_schema_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match bundled lane"):
        normalize_canonical_source_payload(
            {
                "schema_id": "journal.time_log.v1",
                "parser_id": "journal.time_log.v1",
                "canonical_group_id": "activity",
            }
        )


def test_bundled_registry_applies_inferred_mapper() -> None:
    assert DEMO_JOURNAL_FILE.canonical_group_id == "journal"
    assert DEMO_JOURNAL_FILE.canonical_mapper_id == "demo_journal"


def test_apply_bundled_defaults_minimal_time_log() -> None:
    defn = apply_bundled_canonical_defaults(
        {
            "source_id": "time_log",
            "display_name": "Time Log",
            "source_type": "ui_stream",
            "schema_id": "journal.time_log.v1",
            "parser_id": "journal.time_log.v1",
            "canonical_group_id": "journal",
        }
    )
    assert defn["canonical_mapper_id"] == "journal_time_log"
