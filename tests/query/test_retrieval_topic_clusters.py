"""Query retrieval includes topic clusters in inference context."""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.manifest import ScopeResolutionManifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "retrieval_clusters.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: c)
    c.execute(
        """
        INSERT INTO topic_clusters (
            cluster_id, label, dimension, member_count, source_mix_json, label_terms_json, centroid_preview
        ) VALUES ('tc_demo', 'AI / code', 'memory', 5, '{"chatgpt_file_ingestion":5}', '["ai","code"]', 'fine-tuning')
        """
    )
    c.commit()
    yield c
    c.close()


def test_inference_retrieval_includes_topic_clusters(conn) -> None:
    adapters = AdapterFactory.create("local_database", conn=conn)
    manifest = ScopeResolutionManifest(
        scope_id="ai_conversations:read",
        primary_dimensions=["Memory"],
        canonical_tables=["ai_chat_messages"],
        access_mode_ceiling="inference",
        must_not_retrieve=[],
    )
    adapter = DefaultSignalRetrievalAdapter(adapters)
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="inference",
            skip_retrieval=False,
        )
    )
    clusters = bundle.context_packet.get("topic_clusters") or []
    assert len(clusters) == 1
    assert clusters[0]["label"] == "AI / code"
    assert "topic_clusters" in bundle.stores_touched
