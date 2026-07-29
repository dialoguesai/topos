"""Complexity snapshot enrichment job: registry wiring, warm-cache write,
and the staleness throttle."""

from __future__ import annotations

import pytest


def test_job_registered_in_signal_derivation() -> None:
    from topos.enrichment.jobs import SIGNAL_DERIVATION_JOBS, SIGNAL_JOB_REGISTRY

    names = [job.get_job_name() for job in SIGNAL_DERIVATION_JOBS]
    assert "complexity_snapshot" in names
    # Runs after attention triage at the tail of the derivation chain.
    assert names.index("complexity_snapshot") > names.index("attention_triage")
    assert "complexity_snapshot" in SIGNAL_JOB_REGISTRY


def test_catalog_declares_snapshot_table() -> None:
    from topos.enrichment.catalog import get_catalog_entry, output_tables_for_job

    assert output_tables_for_job("complexity_snapshot") == ("complexity_snapshots",)
    entry = get_catalog_entry("complexity_snapshot")
    assert entry is not None
    assert entry.cost_tier == "low"


@pytest.mark.asyncio
async def test_enrich_writes_snapshot_then_throttles(monkeypatch) -> None:
    import topos.enrichment.jobs.canonical.complexity_snapshot_job as job_module
    from topos.enrichment.jobs.canonical.complexity_snapshot_job import ComplexitySnapshotJob

    from tests.features.test_complexity import _conn, _seed_live_shaped

    conn = _conn()
    _seed_live_shaped(conn)
    monkeypatch.setattr(job_module, "get_db_connection", lambda: conn)

    job = ComplexitySnapshotJob()
    assert job.should_run([{"event_at": "2026-07-01T00:00:00Z"}])
    assert not job.should_run([])

    result = await job.enrich([{"event_at": "2026-07-01T00:00:00Z"}])
    assert result == []
    row = conn.execute(
        "SELECT updated_at FROM complexity_snapshots WHERE metric_set='summary'"
    ).fetchone()
    assert row is not None
    first_updated = row["updated_at"]

    # Fresh cache → the second batch must skip the recompute entirely.
    def _boom(*args, **kwargs):  # pragma: no cover - failure path
        raise AssertionError("recompute ran despite fresh snapshot")

    monkeypatch.setattr(
        "topos.features.complexity.engine.get_complexity_summary", _boom
    )
    result2 = await job.enrich([{"event_at": "2026-07-02T00:00:00Z"}])
    assert result2 == []
    row2 = conn.execute(
        "SELECT updated_at FROM complexity_snapshots WHERE metric_set='summary'"
    ).fetchone()
    assert row2["updated_at"] == first_updated
