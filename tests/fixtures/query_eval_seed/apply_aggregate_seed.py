"""SUITE-P corpus: 7,500+ rows whose aggregate answers are known by construction.

Every expected number in AGGREGATE_CASES (query_eval_cases.py) is a fact about
THIS seed — the corpus and the catalog move together or the battery reds.

Shapes drawn from the classified demand:
- the alias trap at scale: one human (synthetic "Casey Verano") under an
  E.164 contact card AND an unnamed 10-digit duplicate — 2,000 + 1,000 rows
  that must fold to ONE group of 3,000;
- Jordan F-02/F-04/F-05: March spend by category (1000/800/600), savings
  transfers summing 2000, income rows averaging 12000;
- Jordan B-01's mood counts (calm 25 / anxious 15 / hopeful 12 / energized 8);
- late-nights: exactly 400 messages at hour 23;
- OS-03's count skeleton: 30 calendar events in a fixed absolute week;
- load: exactly 40 activity events per day for 50 days.
"""

from __future__ import annotations

import sqlite3

DATASET = "u1:default"

# The one number most tests derive from — keep the arithmetic visible.
CASEY_E164_MSGS = 2000
CASEY_TENDIGIT_MSGS = 1000
ANA_MSGS = 1700
NOCONTACT_MSGS = 500
TOTAL_MSGS = CASEY_E164_MSGS + CASEY_TENDIGIT_MSGS + ANA_MSGS + NOCONTACT_MSGS  # 5200
LATE_NIGHT_MSGS = 400  # of the total, seeded at hour 23

ACTIVITY_PER_DAY = 40
ACTIVITY_DAYS = 50


def _day_for(i: int) -> str:
    # 90 deterministic days: 2026-06-01 .. 2026-08-29.
    month_lengths = [(6, 30), (7, 31), (8, 29)]
    d = i % 90
    for month, length in month_lengths:
        if d < length:
            return f"2026-{month:02d}-{d + 1:02d}"
        d -= length
    raise AssertionError("unreachable")


def apply_aggregate_seed(conn: sqlite3.Connection) -> None:
    from topos.storage.canonical.conversations_tables import ensure_all_tables

    ensure_all_tables(conn)

    conn.executemany(
        "INSERT OR REPLACE INTO contacts (contact_id, dataset_id, source_id, display_name)"
        " VALUES (?,?,?,?)",
        [
            ("agg-c1", DATASET, "imessage", "Casey Verano"),
            ("agg-c2", DATASET, "csv_import", None),
            ("agg-c3", DATASET, "imessage", "Ana Torres"),
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO contact_identifiers"
        " (dataset_id, source_id, identifier, identifier_type, contact_id) VALUES (?,?,?,?,?)",
        [
            (DATASET, "imessage", "+15125550100", "phone", "agg-c1"),
            (DATASET, "csv_import", "5125550100", "phone", "agg-c2"),
            (DATASET, "imessage", "+15125550101", "phone", "agg-c3"),
        ],
    )

    rows = []
    seq = 0

    def msg(sender: str, hour: int) -> None:
        nonlocal seq
        rows.append(
            (
                f"agg-m{seq:05d}",
                "agg-conv",
                DATASET,
                "contact",
                sender,
                "text",
                f"{_day_for(seq)}T{hour:02d}:00:00",
                "seed row",
                "imessage",
            )
        )
        seq += 1

    # The first LATE_NIGHT_MSGS of Casey's E.164 rows land at 23:00; everything
    # else at 10:00 — so hour "23" holds exactly LATE_NIGHT_MSGS rows.
    for i in range(CASEY_E164_MSGS):
        msg("+15125550100", 23 if i < LATE_NIGHT_MSGS else 10)
    for _ in range(CASEY_TENDIGIT_MSGS):
        msg("5125550100", 10)
    for _ in range(ANA_MSGS):
        msg("+15125550101", 10)
    for _ in range(NOCONTACT_MSGS):
        msg("+15125550199", 10)
    assert len(rows) == TOTAL_MSGS

    conn.executemany(
        "INSERT OR REPLACE INTO conversation_messages"
        " (message_id, conversation_id, dataset_id, sender_type, sender_id,"
        "  message_type, event_at, content, source_id) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )

    # --- finance (F-02 / F-04 / F-05).
    fin = []
    fid = 0

    def txn(day: str, amount: float, category: str) -> None:
        nonlocal fid
        fin.append((f"agg-f{fid:04d}", f"{day}T12:00:00", amount, category, "checking", "bank"))
        fid += 1

    for i in range(40):
        txn(f"2026-03-{(i % 28) + 1:02d}", 25.0, "groceries")  # 1000
    for i in range(10):
        txn(f"2026-03-{(i % 28) + 1:02d}", 80.0, "utilities")  # 800
    for i in range(50):
        txn(f"2026-03-{(i % 28) + 1:02d}", 12.0, "dining")  # 600
    for i in range(4):
        txn(f"2026-{(i + 1):02d}-15", 500.0, "savings")  # 2000
    for i in range(6):
        txn(f"2026-{(i + 1):02d}-01", 12000.0, "income")  # avg 12000

    conn.executemany(
        "INSERT OR REPLACE INTO financial_transactions"
        " (transaction_id, posted_at, amount, category, account_type, source_id)"
        " VALUES (?,?,?,?,?,?)",
        fin,
    )

    # --- journal moods (B-01 counts).
    jr = []
    jid = 0
    for mood, n in (("calm", 25), ("anxious", 15), ("hopeful", 12), ("energized", 8)):
        for _ in range(n):
            jr.append((f"agg-j{jid:03d}", f"2026-07-{(jid % 28) + 1:02d}T08:00:00", mood, "journal"))
            jid += 1
    conn.executemany(
        "INSERT OR REPLACE INTO journal_entries (entry_id, entry_at, mood_tag, source_id)"
        " VALUES (?,?,?,?)",
        jr,
    )

    # --- calendar (OS-03 count skeleton): 30 events, 2026-08-24..28.
    cal = [
        (
            f"agg-e{i:03d}",
            f"seed event {i}",
            f"2026-08-{24 + (i % 5):02d}T{9 + (i % 8):02d}:00:00",
            f"2026-08-{24 + (i % 5):02d}T{10 + (i % 8):02d}:00:00",
            "gcal",
        )
        for i in range(30)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO calendar_events (event_id, title, starts_at, ends_at, source_id)"
        " VALUES (?,?,?,?,?)",
        cal,
    )

    # --- activity load: exactly ACTIVITY_PER_DAY per day for ACTIVITY_DAYS days
    # starting 2026-07-01.
    act = []
    aid = 0
    for d in range(ACTIVITY_DAYS):
        month = 7 if d < 31 else 8
        dom = d + 1 if d < 31 else d - 30
        for h in range(ACTIVITY_PER_DAY):
            act.append(
                (
                    f"agg-a{aid:05d}",
                    f"2026-{month:02d}-{dom:02d}T{h % 24:02d}:00:00",
                    "browse",
                    "browser",
                )
            )
            aid += 1
    conn.executemany(
        "INSERT OR REPLACE INTO activity_events (event_id, occurred_at, activity_type, source_id)"
        " VALUES (?,?,?,?)",
        act,
    )

    conn.commit()
