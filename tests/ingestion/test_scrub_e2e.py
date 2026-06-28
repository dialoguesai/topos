"""Integration tests for source scrub (PRD §14)."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from unittest.mock import patch

import pytest

from topos.sources.scrub_service import SCRUB_SOURCE_OPTIONS, scrub_source_async
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture(autouse=True)
def _stub_recompute(monkeypatch) -> None:
    async def _fake_recompute(*_args, **_kwargs):
        from topos.features.signal.topic_clustering import recompute_topic_clusters, write_top_topics_signal_facts
        from topos.storage.adapters.factory import AdapterFactory

        conn = _args[0]
        tc_result = recompute_topic_clusters(conn, min_records=2, k=2)
        if tc_result.get("status") == "completed":
            bundle = AdapterFactory.create("local_database", conn=conn)
            write_top_topics_signal_facts(bundle, conn)
        return (
            {
                "topic_clusters": tc_result,
                "dimension_briefs": [],
                "dimension_profiles": {"status": "skipped", "reason": "test"},
            },
            False,
        )

    monkeypatch.setattr("topos.sources.scrub_service._run_recompute_phase", _fake_recompute)


@pytest.fixture
def conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "scrub-e2e.db"))
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    monkeypatch.setattr("topos.sources.scrub_service.get_db_connection", lambda: db)
    return db


def test_scrub_removes_embeddings_for_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
        VALUES ('j1', 'grow_journal', 'alpha', '2026-01-01')
        """
    )
    conn.execute(
        """
        INSERT INTO signal_embeddings (
            embedding_id, record_id, source_id, signal_dimension, model, provider,
            dims, text_preview, provenance_json
        ) VALUES ('emb-1', 'j1', 'grow_journal', 'memory', 'test', 'test', 384, 'alpha', '{}')
        """
    )
    conn.commit()

    with patch("topos.sources.scrub_service.install_service.uninstall_source") as uninstall:
        uninstall.return_value = {"uninstalled": True}
        result = asyncio.run(
            scrub_source_async(source_id="grow_journal", options=SCRUB_SOURCE_OPTIONS)
        )

    assert result["scrub_status"] in {"completed", "partial"}
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_id='grow_journal'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_embeddings WHERE source_id='grow_journal'").fetchone()[0] == 0
    assert result["report"]["residue"]["summary"]


def test_scrub_recompute_excludes_scrubbed_cluster_members(conn: sqlite3.Connection) -> None:
    import json

    for idx, (rid, src, vec) in enumerate(
        [
            ("a1", "source_a", [1.0, 0.0]),
            ("a2", "source_a", [0.95, 0.05]),
            ("b1", "source_b", [0.0, 1.0]),
            ("b2", "source_b", [0.05, 0.95]),
        ]
    ):
        conn.execute(
            """
            INSERT INTO signal_embeddings (
                embedding_id, record_id, source_id, signal_dimension, model, provider,
                dims, text_preview, provenance_json, vector_blob
            ) VALUES (?, ?, ?, 'memory', 'test', 'test', ?, 'text', '{}', ?)
            """,
            (f"emb-{idx}", rid, src, len(vec), json.dumps(vec).encode("utf-8")),
        )
    conn.commit()

    with patch("topos.sources.scrub_service.install_service.uninstall_source") as uninstall:
        uninstall.return_value = {"uninstalled": True}
        asyncio.run(
            scrub_source_async(
                source_id="source_a",
                options=replace(SCRUB_SOURCE_OPTIONS, refresh_dimension_briefs=False),
            )
        )

    members = conn.execute(
        "SELECT source_id, record_id FROM topic_cluster_members"
    ).fetchall()
    assert all(str(row[0]) != "source_a" for row in members)
    assert conn.execute("SELECT COUNT(*) FROM signal_embeddings WHERE source_id='source_a'").fetchone()[0] == 0
