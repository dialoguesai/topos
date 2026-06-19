"""
Gap: Signal feature migrations — inline tables → PRD §7.1 signal tables
Sprint: EN-P0-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.storage.db.migrations.wiki_mvp_phase0 import apply_wiki_mvp_phase0_up

pytestmark = pytest.mark.gap

SIGNAL_TABLES = [
    "signal_facts",
    "signal_scores",
    "signal_tags",
    "signal_summaries",
    "signal_dimension_profiles",
    "message_entities",
    "message_topics",
    "message_emotions",
    "message_sentiment",
    "user_goals",
    "relationship_edges",
    "data_health_dimension",
]


def test_signal_migrations_create_prd_tables() -> None:
    conn = sqlite3.connect(":memory:")
    apply_wiki_mvp_phase0_up(conn)
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for table in SIGNAL_TABLES:
        assert table in existing, table
