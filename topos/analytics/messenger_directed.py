"""L1 — directed dyadic edges and lifetime dyad stats.

`messenger_social_edges` records that two people share a conversation. It cannot say who
spoke first, who answers, or who has gone quiet — its edges are undirected co-participation,
and its primary key has no room for a direction. This module adds the half that carries
direction, so the questions Rings 6 and 7 actually ask become answerable:

    "In my top relationships, who initiates — me or them?"          (catalog #18)
    "Who am I losing touch with, compared to how often we used to?" (catalog #17)
    "Which friendships have become one-sided in the last months?"   (catalog #19)

Two tables, because they answer at two grains:

  * `messenger_directed_edges` — per period, per connector, per ORDERED pair. Counts,
    session initiations and reply latency, each direction stored separately.
  * `messenger_dyad_stats` — lifetime, per UNORDERED pair. Streaks, gaps, balance and tie
    state are properties of a relationship rather than of a direction, so the dyad is keyed
    canonically (a_key < b_key) and the per-direction totals ride along as columns.

**No content columns, by construction.** Neither table has a column for content, snippet,
subject, body or hash. The only free text stored is handles, ids and enum strings. This is a
schema-level privacy invariant, not a convention, and `test_l1_schema.py` fails if it is ever
violated — a directed-edge table is exactly the shape that tempts someone to cache "the last
thing they said" next to it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MESSENGER_DIRECTED_EDGES_TABLE = "messenger_directed_edges"
MESSENGER_DYAD_STATS_TABLE = "messenger_dyad_stats"

#: The gap that ends a conversation session, so the next message opens one.
#:
#: 6 hours, chosen from the corpus rather than from taste. Measured over 7,392
#: within-conversation gaps the distribution has a clear elbow — p85 = 3.4 h, p90 = 16.7 h, a
#: 5x jump — and 6 h sits inside it, leaving 86.8% of gaps within a session.
#:
#: Deliberately FIXED rather than per-dyad self-calibrated: catalog #18 compares initiation
#: across relationships, and a per-dyad threshold makes those numbers incommensurable. The
#: value is written onto every row (`session_gap_seconds`) so a later recalibration can tell
#: which rows were produced under which threshold.
DEFAULT_SESSION_GAP_SECONDS = 6 * 3600

#: Peers that are not people. Kept rather than dropped (so "what is cluttering my inbox" stays
#: answerable) and filtered at read (so they never pollute a relationship ranking).
PEER_CLASS_HUMAN = "human"
PEER_CLASS_AUTOMATED = "automated"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_peer(key: str) -> str:
    """Shortcodes are carriers, 2FA and delivery notices — 29 of 179 DM peers on the first
    live corpus checked. A 5-digit 'sender' is not a relationship."""
    k = str(key or "").strip().lstrip("+")
    if k.isdigit() and len(k) <= 6:
        return PEER_CLASS_AUTOMATED
    return PEER_CLASS_HUMAN


def dyad_key(a: str, b: str) -> tuple:
    """A dyad is an unordered pair, ordered canonically so it has one row.

    Returned as (a_key, b_key) with a_key <= b_key. Streaks, gaps and tenure belong to the
    relationship; storing them twice (once per direction) would let the two copies disagree.
    """
    x, y = str(a or ""), str(b or "")
    return (x, y) if x <= y else (y, x)


def create_directed_tables(conn: Any) -> None:
    """DDL for both L1 tables.

    Called from `_create_messenger_analytics_tables`, which `ensure_messenger_analytics_tables`
    already wraps in the write gate. Deliberately NOT a registry migration: every messenger_*
    table is feature-owned and created this way, and routing this through the registry would
    bump `user_version` past what an installed engine understands and fence the node out of
    every write — which is what happened on 2026-08-25.
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_DIRECTED_EDGES_TABLE} (
            dataset_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            connector TEXT NOT NULL,
            from_key TEXT NOT NULL,
            to_key TEXT NOT NULL,
            msgs INTEGER NOT NULL DEFAULT 0,
            sessions_initiated INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0,
            median_reply_latency_s REAL,
            first_ts TEXT,
            last_ts TEXT,
            session_gap_seconds INTEGER NOT NULL,
            from_person_id TEXT,
            to_person_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, connector, from_key, to_key)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_DIRECTED_EDGES_TABLE}_period
        ON {MESSENGER_DIRECTED_EDGES_TABLE}(dataset_id, period_key, connector)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_DIRECTED_EDGES_TABLE}_from
        ON {MESSENGER_DIRECTED_EDGES_TABLE}(dataset_id, from_key)
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_DYAD_STATS_TABLE} (
            dataset_id TEXT NOT NULL,
            a_key TEXT NOT NULL,
            b_key TEXT NOT NULL,
            involves_self INTEGER NOT NULL DEFAULT 0,
            peer_class TEXT NOT NULL DEFAULT '{PEER_CLASS_HUMAN}',
            total_msgs INTEGER NOT NULL DEFAULT 0,
            a_to_b INTEGER NOT NULL DEFAULT 0,
            b_to_a INTEGER NOT NULL DEFAULT 0,
            balance REAL,
            first_ts TEXT,
            last_ts TEXT,
            active_periods INTEGER NOT NULL DEFAULT 0,
            reciprocal_periods INTEGER NOT NULL DEFAULT 0,
            longest_contact_streak_months INTEGER NOT NULL DEFAULT 0,
            longest_contact_streak_weeks INTEGER NOT NULL DEFAULT 0,
            longest_reciprocal_streak_months INTEGER NOT NULL DEFAULT 0,
            longest_reciprocal_streak_weeks INTEGER NOT NULL DEFAULT 0,
            max_gap_days REAL,
            median_gap_days REAL,
            recent_gap_days REAL,
            drift_ratio REAL,
            tie_state TEXT,
            a_person_id TEXT,
            b_person_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, a_key, b_key)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_DYAD_STATS_TABLE}_self
        ON {MESSENGER_DYAD_STATS_TABLE}(dataset_id, involves_self, peer_class)
        """
    )
