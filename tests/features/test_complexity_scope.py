"""complexity:read scope: registry entry + summary-item shaping for retrieval."""

from __future__ import annotations

import sqlite3


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_scope_registry_declares_complexity_read() -> None:
    from topos.query.scope_registry_loader import get_scope_entry, list_scopes

    entry = get_scope_entry("complexity:read")
    assert entry is not None
    assert entry.get("default_mode_ceiling") == "summary"
    assert entry.get("raw_tables") == []
    assert entry.get("implementation_status") == "live"
    assert any(scope.get("scope_id") == "complexity:read" for scope in list_scopes())


def test_load_complexity_summary_items_from_snapshot() -> None:
    from topos.features.complexity.store import upsert_snapshot
    from topos.query.retrieval import _load_complexity_summary_items

    conn = _conn()
    upsert_snapshot(
        conn,
        snapshot_id="summary_latest",
        metric_set="summary",
        metrics={
            "computed_at": "2026-07-28T18:00:00Z",
            "focus_index": {"score": 34.0, "interpretation": "Moderately diffuse."},
            "readings": {
                "current_focus": {
                    "score": 34.0,
                    "interpretation": "Moderately diffuse.",
                    "baseline": {"status": "ok", "percentile": 0.75},
                },
                "structural_clarity": {"score": 58.0},
                "information_breadth": {"score": 48.0},
                "pipeline_confidence": {"score": 71.0},
            },
            "influence_threads": [
                {
                    "source_label": "Rowan Ellis",
                    "target_label": "Aurora launch planning",
                    "epistemic_status": "direct_evidence",
                }
            ],
        },
    )

    items = _load_complexity_summary_items(conn)
    assert len(items) == 2
    readings_item, influence_item = items
    assert readings_item["retrieval_source"] == "complexity_summary"
    assert "focus 34/100" in readings_item["summary_text"]
    assert "structural clarity 58/100" in readings_item["summary_text"]
    assert "p75" in readings_item["summary_text"]
    assert "Moderately diffuse." in readings_item["summary_text"]
    assert influence_item["retrieval_source"] == "complexity_influence"
    assert "Rowan Ellis → Aurora launch planning (direct evidence)" in influence_item["summary_text"]


def test_load_complexity_summary_items_empty_db() -> None:
    from topos.query.retrieval import _load_complexity_summary_items

    assert _load_complexity_summary_items(None) == []
    assert _load_complexity_summary_items(_conn()) == []
