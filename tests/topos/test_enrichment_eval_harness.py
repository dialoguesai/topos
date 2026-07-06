"""WS-E: enrichment eval harness — density, coverage plumbing, precision/latency."""

from __future__ import annotations

import sqlite3

import pytest

from topos.evals.enrichment_eval import (
    RetrievalEvalCase,
    format_report_summary,
    retrieval_precision_eval,
    run_enrichment_eval,
    signal_density,
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE ai_chat_messages (
            message_id TEXT PRIMARY KEY,
            content TEXT,
            event_at TEXT,
            source_id TEXT
        );
        CREATE TABLE signal_embeddings (embedding_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT);
        CREATE TABLE signal_facts (fact_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT);
        """
    )
    # 20 filler messages (recent) + 2 fundraising messages (old, would never
    # appear in a chronological top-N read).
    for i in range(20):
        c.execute(
            "INSERT INTO ai_chat_messages VALUES (?, ?, ?, 'src')",
            (f"noise{i}", f"lunch plans {i}", f"2026-07-{i % 28 + 1:02d}T12:00:00Z"),
        )
    c.execute(
        "INSERT INTO ai_chat_messages VALUES ('f1', 'our fundraise strategy for the seed round', '2025-01-01T00:00:00Z', 'src')"
    )
    c.execute(
        "INSERT INTO ai_chat_messages VALUES ('f2', 'investor deck feedback re fundraise', '2025-01-02T00:00:00Z', 'src')"
    )
    for i, rid in enumerate(["f1", "f2", "noise1"]):
        c.execute("INSERT INTO signal_embeddings VALUES (?, ?, 'src')", (f"e{i}", rid))
    c.execute("INSERT INTO signal_facts VALUES ('fact1', 'f1', 'src')")
    c.commit()
    yield c
    c.close()


def test_signal_density(conn):
    density = signal_density(conn)
    assert density["canonical_records"] == 22
    assert density["enrichment_outputs"]["signal_embeddings"] == 3
    assert density["enrichment_outputs"]["signal_facts"] == 1
    assert density["density_per_1k_records"] == round(4 / 22 * 1000, 1)


def test_retrieval_precision_narrowing_beats_chronological(conn):
    # Fake semantic search: enrichment index points straight at the fundraising records.
    def fake_ids(query: str):
        return {"f1", "f2"} if "fundrais" in query.lower() else set()

    cases = [
        RetrievalEvalCase(
            query="fundraising plans",
            expected_record_ids=["f1", "f2"],
            expected_keywords=["fundraise"],
        )
    ]
    result = retrieval_precision_eval(
        conn, cases, table="ai_chat_messages", limit=10, semantic_ids_fn=fake_ids
    )
    case = result["cases"][0]
    # Chronological top-10 misses the old fundraising rows entirely.
    assert case["baseline"]["hits"] == 0
    # Narrowed read finds both.
    assert case["narrowed"]["hits"] == 2
    assert case["narrowed"]["rows"] == 2
    summary = result["summary"]
    assert summary["narrowed_avg_hit_rate"] > summary["baseline_avg_hit_rate"]


def test_full_report_and_summary(conn, monkeypatch):
    import topos.evals.enrichment_eval as eval_mod

    # Coverage needs the platform core + registry; stub it to keep this test hermetic.
    monkeypatch.setattr(
        eval_mod,
        "coverage_report",
        lambda source_ids=None: {
            "chatgpt_file_ingestion": {
                "status": "ok",
                "jobs": [{"job_id": "emo_27", "coverage_percent": 50.0}],
            }
        },
    )
    report = run_enrichment_eval(
        conn,
        retrieval_cases=[
            RetrievalEvalCase(query="fundraising", expected_keywords=["fundraise"])
        ],
    )
    assert "signal_density" in report and "coverage" in report and "retrieval" in report
    text = format_report_summary(report)
    assert "signal density" in text
    assert "coverage chatgpt_file_ingestion" in text
