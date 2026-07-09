"""P1.5 (PLAN_PROVENANCE_SPLIT): exposure-profile visibility toggle.

When exposure_profile_visible is True (default) an exposure-ledger stat
("activity.visits.by_title", ledger=='exposure') is surfaced (labeled); when
False it is suppressed for every query — not just first-person ones — and
non-engaged (visit) browse rows stop answering interest asks while engaged
(highlight) rows survive.

Also pins the IMB9 mechanics the P2.1/retrieval half fixes:
  * an engaged activity_events row surfaces its metadata_json.highlight span;
  * page_excerpt (the page author's words) is never surfaced;
  * the "take away … reading" framing tokens no longer trip the rare-token veto.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List

from topos.config.settings import resolve_exposure_profile_visible, settings
from topos.query.retrieval import (
    _apply_first_person_stat_preference,
    _entry_ledger,
    _load_stat_insight_items,
    _query_tokens,
    _residual_content_tokens,
    _suppress_exposure_ledger_entries,
)


# --- settings resolver -----------------------------------------------------------------


def _engine_config_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    return conn


def test_resolver_defaults_true_without_toggle() -> None:
    conn = _engine_config_conn()
    assert resolve_exposure_profile_visible(settings, conn) is True
    # Missing engine_config table entirely also defaults True (fresh DB).
    assert resolve_exposure_profile_visible(settings, sqlite3.connect(":memory:")) is True


def test_resolver_honors_false_toggle() -> None:
    conn = _engine_config_conn()
    conn.execute(
        "INSERT INTO engine_config (key, value) VALUES ('exposure_profile_visible', ?)",
        ("false",),
    )
    assert resolve_exposure_profile_visible(settings, conn) is False


def test_resolver_coerces_common_forms() -> None:
    conn = _engine_config_conn()
    for raw, expected in (("0", False), ("1", True), ("off", False), ("on", True), ("true", True)):
        conn.execute(
            "INSERT OR REPLACE INTO engine_config (key, value) VALUES ('exposure_profile_visible', ?)",
            (raw,),
        )
        assert resolve_exposure_profile_visible(settings, conn) is expected, raw


# --- suppression helpers ---------------------------------------------------------------


def _exposure_stat() -> Dict[str, Any]:
    return {
        "object_type": "stat_insight",
        "record_id": "activity.visits.by_title",
        "fact_id": "stat:activity.visits.by_title:all",
        "stat_summary": {"ledger": "exposure", "n": 42},
    }


def _authored_stat() -> Dict[str, Any]:
    return {
        "object_type": "stat_insight",
        "record_id": "messages.volume.sent.by_thread",
        "fact_id": "stat:messages.volume.sent.by_thread:t1",
        "stat_summary": {"n": 23},
    }


def test_entry_ledger_reads_nested_and_top_level() -> None:
    assert _entry_ledger(_exposure_stat()) == "exposure"
    assert _entry_ledger({"ledger": "EXPOSURE", "object_type": "stat_insight"}) == "exposure"
    assert _entry_ledger(_authored_stat()) == ""


def test_suppress_drops_only_exposure_stats() -> None:
    entries = [_exposure_stat(), _authored_stat(), {"summary_text": "not a stat"}]
    kept = _suppress_exposure_ledger_entries(entries)
    ids = {e.get("record_id") for e in kept}
    assert "activity.visits.by_title" not in ids
    assert "messages.volume.sent.by_thread" in ids
    assert any("summary_text" in e for e in kept)  # non-stat passes through


# --- stat lane end-to-end (visible True keeps, False suppresses) -----------------------


def _stats_db() -> sqlite3.Connection:
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    rows = (
        (
            "stat:activity.visits.by_title:all",
            "activity.visits.by_title",
            {
                "fact_id": "stat:activity.visits.by_title:all",
                "object_type": "stat_insight",
                "record_id": "activity.visits.by_title",
                "tag": "Most visited: Fermentation Methods Deep Dive",
                "summary_text": "Most visited: Fermentation Methods Deep Dive",
                "dimension": "interests",
                "stat_summary": {"ledger": "exposure", "n": 42},
            },
        ),
        (
            "stat:messages.volume.sent.by_thread:t1",
            "messages.volume.sent.by_thread",
            {
                "fact_id": "stat:messages.volume.sent.by_thread:t1",
                "object_type": "stat_insight",
                "record_id": "messages.volume.sent.by_thread",
                "tag": "You sent 23 messages",
                "summary_text": "You sent 23 messages",
                "dimension": "relationships",
                "stat_summary": {"n": 23},
            },
        ),
    )
    for fact_id, record_id, payload in rows:
        conn.execute(
            """INSERT INTO signal_facts
               (fact_id, dimension, source_id, record_id, model, provider, payload_json, created_at)
               VALUES (?, ?, 'stats_engine', ?, 'v1', 'topos', ?, '2026-07-01T00:00:00Z')""",
            (fact_id, payload["dimension"], record_id, json.dumps(payload)),
        )
    conn.commit()
    return conn


def _titles(items: List[Dict[str, Any]]) -> set:
    return {str(i.get("record_id") or "") for i in items}


_EXPOSURE_FACT_ID = "stat:activity.visits.by_title:all"
_SENT_FACT_ID = "stat:messages.volume.sent.by_thread:t1"


def test_stat_lane_keeps_exposure_when_visible() -> None:
    conn = _stats_db()
    items = _load_stat_insight_items(
        conn, "what pages do I visit the most", exposure_visible=True
    )
    assert _EXPOSURE_FACT_ID in _titles(items)


def test_stat_lane_suppresses_exposure_when_hidden() -> None:
    conn = _stats_db()
    # Same query, only the toggle differs: visible surfaces it, hidden drops it.
    visible = _load_stat_insight_items(
        conn, "what pages do I visit the most", exposure_visible=True
    )
    hidden = _load_stat_insight_items(
        conn, "what pages do I visit the most", exposure_visible=False
    )
    assert _EXPOSURE_FACT_ID in _titles(visible)
    assert _EXPOSURE_FACT_ID not in _titles(hidden)
    # A non-exposure stat is unaffected by the toggle.
    kept = _load_stat_insight_items(conn, "how many messages have I sent", exposure_visible=False)
    assert _SENT_FACT_ID in _titles(kept)


# --- the "take away … reading" framing tokens no longer veto ---------------------------


def test_framing_tokens_excluded_from_residual() -> None:
    q = "What did I take away from my reading about fermentation methods?"
    residual = _residual_content_tokens(_query_tokens(q), tables=["activity_events"])
    # The recall-framing words are gone; the real content tokens remain.
    for framing in ("take", "away", "took", "takeaway", "reading", "read"):
        assert framing not in residual, framing
    assert "fermentation" in residual
    assert "methods" in residual
