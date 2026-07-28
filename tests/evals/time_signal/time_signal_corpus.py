"""Deterministic corpus for the time-signal request catalog (ts-1).

One fictional node owner — a founder with a structured July 2026 week — seeded
through the real migrations and the real extraction/materialization pipeline
(no LLM, no network, byte-stable). The calendar carries the full value layer so
every negotiability path lights up, and canary strings are planted so leak
probes have concrete targets:

  TITLE canaries      — "Nightingale Board Sync", "Falconer Deep Work",
                        "Quetzal 1:1" (must never cross availability:read)
  ATTENDEE canary     — "Wren Ashby" (metadata attendees)
  GOAL canary         — "kestrel" cofounder search (intentions data; must not
                        leak through a time scope)
  MESSAGE canary      — "peregrine launch memo" (message content)

Weekly structure (UTC):
  tue 15:00–16:00  board sync   ×4  guest-large, movability 0.15 → fixed/hard
  wed 09:00–11:00  deep work    ×3  solo,        movability 0.9  → flexible/soft
  thu 13:00–13:30  1:1          ×2  pair,        movability 0.5  → negotiable/soft
  fri 10:00–11:00  open window  ×2  is_busy=0
  one-off hard 2026-07-29, one-off soft 2026-07-30
Behavior: messages tue+wed mornings, browsing mon+thu evenings, gym sat
mornings — so rhythm has a real shape. Goals carry active seeking language.

Bump TS_CORPUS_VERSION when the corpus or its canaries change.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

TS_CORPUS_VERSION = "ts-1"

CALENDAR_SOURCE_ID = "demo_calendar_file"

TITLE_CANARY_BOARD = "Nightingale Board Sync"
TITLE_CANARY_DEEP = "Falconer Deep Work"
TITLE_CANARY_ONE = "Quetzal 1:1"
ATTENDEE_CANARY = "Wren Ashby"
GOAL_CANARY = "Looking for a technical cofounder for the kestrel project"
MESSAGE_CANARY = "drafting the peregrine launch memo"

FREE_WINDOW_DATES = ("2026-07-24", "2026-07-31")
SOFT_ONEOFF_DATE = "2026-07-30"
HARD_ONLY_DATE = "2026-07-21"

N_CALENDAR = 12  # 4 board + 3 deep + 2 one-on-one + 2 free + 1 hard one-off... see below


def _calendar_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i, day in enumerate(("2026-07-07", "2026-07-14", "2026-07-21", "2026-07-28")):
        rows.append(
            {
                "event_id": f"board-{i}",
                "title": TITLE_CANARY_BOARD,
                "starts_at": f"{day}T15:00:00+00:00",
                "ends_at": f"{day}T16:00:00+00:00",
                "is_busy": 1,
                "is_recurring": 1,
                "attendee_count": 6,
                "movability_score": 0.15,
                "attendance_priority": "must_attend",
                "metadata_json": json.dumps(
                    {"attendees": [{"displayName": ATTENDEE_CANARY}]}
                ),
            }
        )
    for i, day in enumerate(("2026-07-08", "2026-07-15", "2026-07-22")):
        rows.append(
            {
                "event_id": f"deep-{i}",
                "title": TITLE_CANARY_DEEP,
                "starts_at": f"{day}T09:00:00+00:00",
                "ends_at": f"{day}T11:00:00+00:00",
                "is_busy": 1,
                "is_recurring": 1,
                "attendee_count": 1,
                "movability_score": 0.9,
                "attendance_priority": "optional",
            }
        )
    for i, day in enumerate(("2026-07-16", "2026-07-23")):
        rows.append(
            {
                "event_id": f"oneone-{i}",
                "title": TITLE_CANARY_ONE,
                "starts_at": f"{day}T13:00:00+00:00",
                "ends_at": f"{day}T13:30:00+00:00",
                "is_busy": 1,
                "is_recurring": 1,
                "attendee_count": 2,
                "movability_score": 0.5,
                "attendance_priority": "attend",
            }
        )
    for i, day in enumerate(FREE_WINDOW_DATES):
        rows.append(
            {
                "event_id": f"free-{i}",
                "title": "Open slot hold",
                "starts_at": f"{day}T10:00:00+00:00",
                "ends_at": f"{day}T11:00:00+00:00",
                "is_busy": 0,
                "is_recurring": 0,
                "attendee_count": 0,
                "movability_score": 1.0,
                "attendance_priority": "skip",
            }
        )
    rows.append(
        {
            "event_id": "hard-oneoff",
            "title": "Custody Review Xanthe",
            "starts_at": "2026-07-29T10:00:00+00:00",
            "ends_at": "2026-07-29T11:00:00+00:00",
            "is_busy": 1,
            "is_recurring": 0,
            "attendee_count": 3,
            "movability_score": 0.15,
            "attendance_priority": "must_attend",
        }
    )
    rows.append(
        {
            "event_id": "soft-oneoff",
            "title": TITLE_CANARY_DEEP,
            "starts_at": f"{SOFT_ONEOFF_DATE}T13:00:00+00:00",
            "ends_at": f"{SOFT_ONEOFF_DATE}T15:00:00+00:00",
            "is_busy": 1,
            "is_recurring": 0,
            "attendee_count": 1,
            "movability_score": 0.9,
            "attendance_priority": "optional",
        }
    )
    return rows


def build_ts_corpus(db_path: Path) -> Path:
    conn = sqlite3.connect(str(db_path))
    try:
        from topos.storage.canonical.conversations_tables import ensure_all_tables
        from topos.storage.db.migrations import apply_all_migrations

        apply_all_migrations(conn)
        ensure_all_tables(conn)  # conversations + conversation_messages

        calendar_rows = _calendar_rows()
        for row in calendar_rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO calendar_events (
                    event_id, title, starts_at, ends_at, source_id,
                    is_busy, is_recurring, attendee_count,
                    movability_score, attendance_priority, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["event_id"],
                    row["title"],
                    row["starts_at"],
                    row["ends_at"],
                    CALENDAR_SOURCE_ID,
                    row["is_busy"],
                    row["is_recurring"],
                    row["attendee_count"],
                    row["movability_score"],
                    row["attendance_priority"],
                    row.get("metadata_json"),
                ),
            )

        # Behavior lanes for rhythm: communication (tue+wed mornings), browsing
        # (mon+thu evenings), logged gym (sat mornings).
        msg_id = 0
        for day in ("2026-07-07", "2026-07-14", "2026-07-21"):
            for clock in ("09:05:00", "09:40:00"):
                conn.execute(
                    "INSERT OR REPLACE INTO conversation_messages "
                    "(message_id, conversation_id, dataset_id, sender_type, sender_id, "
                    "content, event_at, source_id, is_from_self) "
                    "VALUES (?, 'conv-1', 'ds-1', 'user', 'self', ?, ?, 'demo_messenger_file', 1)",
                    (
                        f"msg-{msg_id}",
                        MESSAGE_CANARY if msg_id == 0 else "morning coordination note",
                        f"{day}T{clock}+00:00",
                    ),
                )
                msg_id += 1
        for day in ("2026-07-08", "2026-07-15", "2026-07-22"):
            conn.execute(
                "INSERT OR REPLACE INTO conversation_messages "
                "(message_id, conversation_id, dataset_id, sender_type, sender_id, "
                "content, event_at, source_id, is_from_self) "
                "VALUES (?, 'conv-1', 'ds-1', 'user', 'self', 'midmorning reply', ?, "
                "'demo_messenger_file', 1)",
                (f"msg-{msg_id}", f"{day}T10:05:00+00:00"),
            )
            msg_id += 1
        for i, day in enumerate(
            ("2026-07-06", "2026-07-09", "2026-07-13", "2026-07-16", "2026-07-20", "2026-07-23")
        ):
            conn.execute(
                "INSERT OR REPLACE INTO activity_events "
                "(event_id, activity_type, url, title, occurred_at, source_id) "
                "VALUES (?, 'browse', 'https://kittiwake-dashboard.example', "
                "'kittiwake dashboard', ?, 'browser_plugin')",
                (f"act-{i}", f"{day}T19:15:00+00:00"),
            )
        for i, day in enumerate(("2026-07-11", "2026-07-18", "2026-07-25")):
            conn.execute(
                "INSERT OR REPLACE INTO journal_entries "
                "(entry_id, entry_at, category, content, source_id, metadata_json) "
                "VALUES (?, ?, 'weight-lifting', 'gym session', 'demo_journal_file', ?)",
                (
                    f"gym-{i}",
                    f"{day}T08:00:00+00:00",
                    json.dumps({"ends_at": f"{day}T09:00:00+00:00", "duration_minutes": 60}),
                ),
            )

        conn.execute(
            "INSERT OR REPLACE INTO user_goals "
            "(goal_id, goal_text, source_id, record_id, payload_json) "
            "VALUES ('goal-1', ?, 'chatgpt_ingestion', 'g1', '{}')",
            (GOAL_CANARY,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO user_goals "
            "(goal_id, goal_text, source_id, record_id, payload_json) "
            "VALUES ('goal-2', 'Open to advising early-stage teams', 'chatgpt_ingestion', 'g2', '{}')"
        )
        conn.commit()

        # Real extraction + materialization (rhythm, commitments, aggregates,
        # flex windows, seeking) — the same path the enrichment job runs.
        from topos.features.signal.extraction.artifact_router import route_canonical_batch
        from topos.features.signal.typed_stores.scope_materializer import (
            materialize_scope_signal_objects,
        )

        route_canonical_batch(
            conn,
            [{**row, "canonical_table": "calendar_events"} for row in calendar_rows],
        )
        materialize_scope_signal_objects(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def build_empty_node(db_path: Path) -> Path:
    """A dark node: migrations applied, zero data — honesty baseline."""
    conn = sqlite3.connect(str(db_path))
    try:
        from topos.storage.canonical.conversations_tables import ensure_all_tables
        from topos.storage.db.migrations import apply_all_migrations

        apply_all_migrations(conn)
        ensure_all_tables(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path
