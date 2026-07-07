"""Per-source enrichment attach/detach overrides (runtime toggles)."""

from __future__ import annotations

import sqlite3

import pytest

from topos.enrichment import source_overrides as so
from topos.sources.registry import REGISTRY

SOURCE_ID = "chatgpt_file_ingestion"


@pytest.fixture()
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT)")

    import topos.core.state as state_mod

    def fake_get(cfg_conn, key):
        row = cfg_conn.execute(
            "SELECT value FROM engine_config WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def fake_set(cfg_conn, key, value):
        cfg_conn.execute(
            "INSERT OR REPLACE INTO engine_config (key, value) VALUES (?, ?)", (key, value)
        )
        cfg_conn.commit()

    monkeypatch.setattr(state_mod, "get_engine_config_value", fake_get)
    monkeypatch.setattr(state_mod, "set_engine_config_value", fake_set)
    monkeypatch.setattr(state_mod, "get_db_connection", lambda: c)
    yield c
    c.close()


def test_set_and_get_override_roundtrip(conn):
    assert so.get_source_overrides(SOURCE_ID, conn=conn) == {}
    result = so.set_source_enrichment_override(SOURCE_ID, "goal_extraction", True, conn=conn)
    assert result["goal_extraction"]["enabled"] is True
    assert "signal" in result["goal_extraction"]["lanes"]
    fetched = so.get_source_overrides(SOURCE_ID, conn=conn)
    assert fetched["goal_extraction"]["enabled"] is True

    # Clearing reverts to definition defaults and prunes empty maps.
    so.set_source_enrichment_override(SOURCE_ID, "goal_extraction", None, conn=conn)
    assert so.get_source_overrides(SOURCE_ID, conn=conn) == {}


def test_set_override_rejects_bad_jobs(conn):
    with pytest.raises(ValueError, match="cannot be toggled"):
        so.set_source_enrichment_override(SOURCE_ID, "no_such_job", True, conn=conn)
    with pytest.raises(ValueError, match="cannot be toggled"):
        # Raw-lane stub jobs are definition-driven, not togglable.
        so.set_source_enrichment_override(SOURCE_ID, "attachments", True, conn=conn)
    with pytest.raises(ValueError, match="job_id required"):
        so.set_source_enrichment_override(SOURCE_ID, "", True, conn=conn)


def test_apply_overrides_add_and_remove(conn):
    so.set_source_enrichment_override(SOURCE_ID, "goal_extraction", True, conn=conn)
    so.set_source_enrichment_override(SOURCE_ID, "sentiment", False, conn=conn)

    jobs = so.apply_overrides_to_jobs(
        SOURCE_ID, "canonical", ["entities", "sentiment"], conn=conn
    )
    assert "sentiment" not in jobs
    assert "entities" in jobs
    # goal_extraction is signal-lane only: enabling it must not leak into canonical.
    assert "goal_extraction" not in jobs

    signal_jobs = so.apply_overrides_to_jobs(SOURCE_ID, "signal", ["embeddings"], conn=conn)
    assert "goal_extraction" in signal_jobs

    # Other sources are untouched.
    other = so.apply_overrides_to_jobs("another_source", "canonical", ["sentiment"], conn=conn)
    assert other == ["sentiment"]


def test_effective_canonical_jobs_uses_overrides(conn):
    source_def = REGISTRY.get(SOURCE_ID)
    assert source_def is not None
    baseline = list(source_def.canonical_enrichment_jobs)
    assert "sentiment" in baseline

    so.set_source_enrichment_override(SOURCE_ID, "sentiment", False, conn=conn)
    effective = so.effective_canonical_enrichment_jobs(source_def, conn=conn)
    assert "sentiment" not in effective
    assert set(effective) == set(baseline) - {"sentiment"}


def test_signal_lane_honors_overrides(conn):
    from topos.sources.canonical_signal_defaults import (
        CANONICAL_BASELINE_SIGNAL_JOBS,
        resolved_signal_derivation_jobs,
    )

    source_def = REGISTRY.get(SOURCE_ID)
    default_jobs = resolved_signal_derivation_jobs(source_def)
    assert "dimension_summary" in default_jobs  # baseline job

    so.set_source_enrichment_override(SOURCE_ID, "dimension_summary", False, conn=conn)
    jobs = resolved_signal_derivation_jobs(source_def)
    assert "dimension_summary" not in jobs
    # Other baseline jobs still present.
    assert set(CANONICAL_BASELINE_SIGNAL_JOBS) - {"dimension_summary"} <= set(jobs)

    # Explicit jobs bypass overrides (deliberate invocations).
    explicit = resolved_signal_derivation_jobs(source_def, explicit_jobs=["dimension_summary"])
    assert explicit == ["dimension_summary"]


def test_jobs_configured_for_source_reflects_toggles(conn):
    from topos.enrichment.catalog import jobs_configured_for_source

    source_def = REGISTRY.get(SOURCE_ID)
    so.set_source_enrichment_override(SOURCE_ID, "sentiment", False, conn=conn)
    so.set_source_enrichment_override(SOURCE_ID, "url_classification", True, conn=conn)

    lanes = jobs_configured_for_source(source_def)
    assert "sentiment" not in lanes["canonical"]
    assert "sentiment" not in lanes["signal"]
    assert "url_classification" in lanes["canonical"]


def test_toggle_core_end_to_end(conn):
    from topos.api.enrichment import _toggle_source_enrichment_core

    result = _toggle_source_enrichment_core(SOURCE_ID, "url_classification", True)
    assert result["status"] == "ok"
    assert result["enabled"] is True
    assert "canonical" in result["enabled_lanes"]
    assert result["override"] == {
        "enabled": True,
        "lanes": ["canonical", "signal"],
    }

    result = _toggle_source_enrichment_core(SOURCE_ID, "url_classification", None)
    assert result["override"] is None
    assert result["enabled"] is False  # not in the definition's defaults

    with pytest.raises(ValueError, match="not found"):
        _toggle_source_enrichment_core("missing_source", "sentiment", True)


def test_overrides_fail_open_without_db(monkeypatch):
    import topos.core.state as state_mod

    monkeypatch.setattr(state_mod, "get_db_connection", lambda: None)
    assert so.get_source_overrides(SOURCE_ID) == {}
    assert so.apply_overrides_to_jobs(SOURCE_ID, "canonical", ["entities"]) == ["entities"]
