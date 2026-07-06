"""Recency decay in RRF fusion + the 'recent' contributor.

The 2026-07-06 audit found no time model anywhere in ranking: a message from
last year and last night fused identically. These tests pin the decay
contract: time-stamped event items decay by 2^(-age/half_life) toward a
floor, current-state contributors (facts/stats/briefs) never decay, and the
last two weeks are always representable via the 'recent' contributor.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.query.retrieval import (
    _load_recent_summary_items,
    _recency_decay_factor,
    _rrf_fuse_summary_lists,
)

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def _item(record_id: str, *, event_at: str | None = None, source: str = "x") -> dict:
    out = {"record_id": record_id, "topic": record_id, "retrieval_source": source}
    if event_at:
        out["event_at"] = event_at
    return out


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _iso_real(days_ago: float) -> str:
    """Relative to wall-clock now — for code paths that call datetime.now()."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class TestDecayFactor:
    def test_fresh_item_no_decay(self):
        factor = _recency_decay_factor(
            _item("a", event_at=_iso(0)), now=NOW, half_life_days=45, floor=0.2
        )
        assert factor == pytest.approx(1.0, abs=1e-6)

    def test_half_life(self):
        factor = _recency_decay_factor(
            _item("a", event_at=_iso(45)), now=NOW, half_life_days=45, floor=0.2
        )
        assert factor == pytest.approx(0.5, abs=1e-6)

    def test_floor_holds_for_ancient_items(self):
        factor = _recency_decay_factor(
            _item("a", event_at=_iso(3650)), now=NOW, half_life_days=45, floor=0.2
        )
        assert factor == 0.2

    def test_missing_timestamp_means_no_decay(self):
        assert (
            _recency_decay_factor(_item("a"), now=NOW, half_life_days=45, floor=0.2)
            == 1.0
        )


class TestFusionDecay:
    def test_recent_outranks_old_at_same_rank(self):
        old = _item("old", event_at=_iso(180))
        fresh = _item("fresh", event_at=_iso(1))
        fused = _rrf_fuse_summary_lists(
            [("vector", 1.0, [old]), ("canonical", 1.0, [fresh])], now=NOW
        )
        assert [i["record_id"] for i in fused] == ["fresh", "old"]
        assert fused[1]["recency_factor"] < 1.0

    def test_no_decay_sources_are_exempt(self):
        # An old stat insight must not lose to an equally-ranked old vector hit.
        old_stat = _item("stat", event_at=_iso(180))
        old_vec = _item("vec", event_at=_iso(180))
        fused = _rrf_fuse_summary_lists(
            [("stat_insights", 1.0, [old_stat]), ("vector", 1.0, [old_vec])], now=NOW
        )
        assert fused[0]["record_id"] == "stat"
        assert "recency_factor" not in fused[0]

    def test_flag_off_restores_time_blind_fusion(self, monkeypatch):
        monkeypatch.setenv("TOPOS_FUSION_RECENCY", "off")
        old = _item("old", event_at=_iso(180))
        fresh = _item("fresh", event_at=_iso(1))
        fused = _rrf_fuse_summary_lists(
            [("vector", 1.0, [old]), ("canonical", 1.0, [fresh])], now=NOW
        )
        scores = {i["record_id"]: i["relevance_score"] for i in fused}
        assert scores["old"] == scores["fresh"]

    def test_decay_does_not_bury_exact_matches(self):
        # A rank-0 ancient item at the floor still beats deep-ranked fresh
        # noise: floor(0.2)/(60+1) > 1.0/(60+301).
        ancient_top = _item("ancient", event_at=_iso(3000))
        fillers = [_item(f"f{i}", event_at=_iso(1)) for i in range(300)]
        fresh_deep = _item("fresh_deep", event_at=_iso(1))
        fused = _rrf_fuse_summary_lists(
            [("vector", 1.0, [ancient_top]), ("canonical", 1.0, fillers + [fresh_deep])],
            now=NOW,
            cap=400,
        )
        ranks = {i["record_id"]: r for r, i in enumerate(fused)}
        assert ranks["ancient"] < ranks["fresh_deep"]


class TestRecentContributor:
    @pytest.fixture()
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE signal_embeddings (
                embedding_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT,
                signal_dimension TEXT, text_preview TEXT, event_at TEXT,
                chunk_index INTEGER DEFAULT 0
            )
            """
        )
        rows = [
            ("e1", "r1", "signal", "memory", "fresh message", _iso_real(1), 0),
            ("e2", "r2", "signal", "memory", "old message", _iso_real(60), 0),
            ("e3", "r3", "chatgpt", "memory", "fresh chat", _iso_real(2), 0),
            ("e4", "r4", "signal", "memory", "fresh chunk tail", _iso_real(1), 1),
            ("e5", "r5", "signal", "memory", "", _iso_real(1), 0),
        ]
        conn.executemany(
            "INSERT INTO signal_embeddings VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        yield conn
        conn.close()

    def test_returns_recent_window_only(self, conn):
        items = _load_recent_summary_items(conn, days=14, limit=10)
        ids = [i["record_id"] for i in items]
        assert "r2" not in ids  # outside window
        assert "r4" not in ids  # non-zero chunk
        assert "r5" not in ids  # empty preview
        assert set(ids) == {"r1", "r3"}
        assert all(i["retrieval_source"] == "recent" for i in items)

    def test_source_filter(self, conn):
        items = _load_recent_summary_items(conn, source_ids=["chatgpt"], days=14)
        assert [i["record_id"] for i in items] == ["r3"]

    def test_none_conn_is_safe(self):
        assert _load_recent_summary_items(None) == []
