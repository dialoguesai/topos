"""Continuous signal health: graded scores, stub exclusion, model readiness.

The old coverage formula (min(1, rows / object_type_count)) pinned every
dimension at 0 or 100%. These tests pin the replacement: a saturating
volume curve blended with freshness, brief recency, and model readiness,
with placeholder rows excluded and unmeasured dimensions never scored.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from topos.features.signal import data_health as dh
from topos.features.signal.data_health import (
    DataHealthComputer,
    is_stub_signal_row,
    volume_score,
)
from topos.features.signal.dimension_profiles import DimensionProfileUpdater
from topos.features.signal.model_recommendations import (
    estimate_params_b,
    signal_model_recommendation,
)
from topos.storage.adapters.factory import AdapterBundle, AdapterFactory
from topos.storage.adapters.fakes import (
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)
from topos.storage.db.migrations import ensure_migrations_applied


def _bundle() -> AdapterBundle:
    return AdapterBundle(
        canonical=InMemoryCanonicalStore(),
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=MagicMock(),
        query_session=MagicMock(),
        backend="memory",
    )


class TestVolumeScore:
    def test_zero_rows_zero_score(self):
        assert volume_score(0, 100) == 0.0

    def test_continuous_midrange_not_a_cliff(self):
        # 10 rows toward a 100-row target reads ~7%, not 100%.
        score = volume_score(10, 100)
        assert 0.05 < score < 0.10

    def test_half_at_target(self):
        assert volume_score(100, 100) == pytest.approx(0.5)

    def test_monotonic_and_bounded(self):
        scores = [volume_score(n, 50) for n in (1, 5, 25, 50, 200, 5000)]
        assert scores == sorted(scores)
        assert all(0.0 < s < 1.0 for s in scores[:-1])
        assert scores[-1] > 0.99


class TestStubExclusion:
    def test_stub_model_row_detected(self):
        assert is_stub_signal_row({"model": "availability_stub_v1"})
        assert is_stub_signal_row({"provider": "stub"})
        assert is_stub_signal_row({"_deferred": True})
        assert not is_stub_signal_row({"model": "availability_rules_v1", "provider": "rules"})

    def test_stub_rows_do_not_measure_a_dimension(self, monkeypatch):
        monkeypatch.setattr(dh, "check_provider_status", lambda: {"ollama": "down", "huggingface": "up"})
        bundle = _bundle()
        bundle.signal.put_score(
            {"dimension": "time", "source_id": "s", "label": "availability",
             "score": 0.0, "provider": "rules", "model": "availability_stub_v1"}
        )
        profiles = DataHealthComputer(bundle).compute()
        assert profiles["time"]["measured"] is False
        assert profiles["time"]["score"] == 0.0

    def test_real_rows_measure_a_dimension(self, monkeypatch):
        monkeypatch.setattr(dh, "check_provider_status", lambda: {"ollama": "down", "huggingface": "up"})
        bundle = _bundle()
        bundle.signal.put_score(
            {"dimension": "time", "source_id": "s", "label": "availability",
             "score": 0.7, "provider": "rules", "model": "availability_rules_v1"}
        )
        profiles = DataHealthComputer(bundle).compute()
        assert profiles["time"]["measured"] is True
        assert 0.0 < profiles["time"]["score"] < 1.0


class TestAvailabilityJobNoStub:
    @pytest.mark.asyncio
    async def test_no_calendar_rows_yields_no_output(self):
        from topos.enrichment.jobs.canonical.availability_scores_job import AvailabilityScoresJob

        out = await AvailabilityScoresJob().enrich(
            [{"message_id": "m1", "source_id": "s", "content": "hello"}]
        )
        assert out == []

    @pytest.mark.asyncio
    async def test_calendar_rows_yield_rules_score(self):
        from topos.enrichment.jobs.canonical.availability_scores_job import AvailabilityScoresJob

        out = await AvailabilityScoresJob().enrich(
            [{"event_id": "e1", "source_id": "cal", "activity_type": "calendar"}]
        )
        assert len(out) == 1
        assert out[0]["model"] == "availability_rules_v1"


class TestModelReadiness:
    def test_readiness_drops_when_ollama_down(self, monkeypatch):
        bundle = _bundle()
        bundle.signal.put_fact({"dimension": "memory", "source_id": "s", "record_id": "m1"})

        monkeypatch.setattr(dh, "check_provider_status", lambda: {"ollama": "up", "huggingface": "up"})
        up = DataHealthComputer(bundle).compute()["memory"]["model_readiness_score"]

        monkeypatch.setattr(dh, "check_provider_status", lambda: {"ollama": "down", "huggingface": "up"})
        down = DataHealthComputer(bundle).compute()["memory"]["model_readiness_score"]

        assert down < up
        assert up == 1.0

    def test_all_ten_dimensions_scored(self, monkeypatch):
        monkeypatch.setattr(dh, "check_provider_status", lambda: {"ollama": "down", "huggingface": "up"})
        profiles = DataHealthComputer(_bundle()).compute()
        assert len(profiles) == 10
        for profile in profiles.values():
            for key in ("score", "volume_score", "freshness_score", "brief_score",
                        "model_readiness_score", "measured", "signal_count"):
                assert key in profile


class TestModelRecommendations:
    def test_estimate_params(self):
        assert estimate_params_b("llama3.2:1b") == 1.0
        assert estimate_params_b("llama3.2:3b") == 3.0
        assert estimate_params_b("llama3:8b") == 8.0
        assert estimate_params_b("llama3.2:latest") == 3.0
        assert estimate_params_b("") == 3.0

    def test_small_device_default_model_recommended(self, monkeypatch):
        import topos.features.signal.model_recommendations as mr

        monkeypatch.setattr(mr, "device_ram_gb", lambda: 4.0)
        rec = signal_model_recommendation(None)
        assert rec["tier"] == "recommended"
        assert rec["minimum_model"] == "llama3.2:1b"
        assert rec["ollama_query_model"] == "llama3.2:3b"

    def test_midrange_device_heavy_model_is_minimum(self, monkeypatch):
        import topos.features.signal.model_recommendations as mr
        import topos.config.signal_extraction as se
        from topos.config.signal_extraction import DeviceSignalExtractionOverrides

        monkeypatch.setattr(mr, "device_ram_gb", lambda: 12.0)
        # resolve is imported inside the function; patch at source module.
        monkeypatch.setattr(
            se,
            "resolve_signal_extraction_config",
            lambda *_a, **_k: DeviceSignalExtractionOverrides(
                provider="ollama", query_model="llama3:8b"
            ),
        )
        rec = signal_model_recommendation(None)
        assert rec["tier"] == "minimum"
        assert rec["meets_minimum"] is True
        assert rec["meets_recommended"] is False
        assert "llama3.2" in rec["reason"]

    def test_midrange_device_huge_model_is_stress(self, monkeypatch):
        import topos.features.signal.model_recommendations as mr
        import topos.config.signal_extraction as se
        from topos.config.signal_extraction import DeviceSignalExtractionOverrides

        monkeypatch.setattr(mr, "device_ram_gb", lambda: 12.0)
        monkeypatch.setattr(
            se,
            "resolve_signal_extraction_config",
            lambda *_a, **_k: DeviceSignalExtractionOverrides(
                provider="ollama", query_model="llama3.1:70b"
            ),
        )
        rec = signal_model_recommendation(None)
        assert rec["tier"] == "stress"
        assert rec["meets_minimum"] is False

    def test_remote_provider_has_no_device_constraint(self, monkeypatch):
        import topos.config.signal_extraction as se
        from topos.config.signal_extraction import DeviceSignalExtractionOverrides

        monkeypatch.setattr(
            se,
            "resolve_signal_extraction_config",
            lambda *_a, **_k: DeviceSignalExtractionOverrides(
                provider="platform", query_model="gpt-4o-mini"
            ),
        )
        rec = signal_model_recommendation(None)
        assert rec["meets_minimum"] is True
        assert rec["meets_recommended"] is True
        assert rec["tier"] == "recommended"

    def test_unknown_ram_makes_no_claims(self, monkeypatch):
        import topos.features.signal.model_recommendations as mr

        monkeypatch.setattr(mr, "device_ram_gb", lambda: None)
        rec = signal_model_recommendation(None)
        assert rec["meets_minimum"] is True
        assert rec["meets_recommended"] is True


class TestBriefScore:
    def test_empty_shell_earns_nothing_real_revision_scores(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dh, "check_provider_status", lambda: {"ollama": "down", "huggingface": "up"})
        conn = sqlite3.connect(str(tmp_path / "briefs.db"))
        ensure_migrations_applied(conn)
        bundle = AdapterFactory.create("local_database", conn=conn)
        bundle.signal.put_fact({"dimension": "memory", "source_id": "s", "record_id": "m1"})

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO signal_dimension_briefs (
                brief_id, signal_dimension, head_revision_id, structured_json,
                markdown_body, revision_number, updated_at, updated_by
            ) VALUES ('b1', 'memory', 'r1', '{}', '', 1, ?, 'system')
            """,
            (now,),
        )
        conn.commit()
        shell = DataHealthComputer(bundle, conn).compute()["memory"]["brief_score"]
        assert shell == 0.0

        conn.execute(
            "UPDATE signal_dimension_briefs SET revision_number=3, updated_at=? WHERE brief_id='b1'",
            (now,),
        )
        conn.commit()
        real = DataHealthComputer(bundle, conn).compute()["memory"]["brief_score"]
        assert real > 0.9  # fresh brief, 7d half-life
