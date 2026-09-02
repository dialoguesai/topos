"""S7 aggregate builder battery — deterministic SQL over curated scope registries.

protects: per-person numbers bind to people, not ghosts; every curated
(measure x group_by x bucket) combination emits valid SQL with exact,
pre-computed answers; black-holed people are absent from aggregates a
non-owner-UI caller sees.

Every expected number in this file is hand-computed from the seed rows —
if a builder drifts, the number moves and the assertion names the cell.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from topos.storage.db.migrations import apply_all_migrations
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.features.lifecycle.blackhole_guard import (
    BlackholeGuard,
    CallerClass,
    owner_ui_guard,
)

from topos.query.aggregate import (
    AGGREGATE_GROUP_CAP,
    AGGREGATE_REGISTRY,
    AggregateParamError,
    run_aggregate,
    validate_aggregate_params,
)

DATASET = "u1:default"


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(tmp_path / "agg.db"))
    apply_all_migrations(c)
    ensure_all_tables(c)
    _seed(c)
    c.commit()
    return c


def _seed(c: sqlite3.Connection) -> None:
    # --- contacts: ONE human under two contact rows (the alias trap).
    # c1 is the named card with the E.164 phone; c2 is an unnamed duplicate
    # import of the SAME phone in bare 10-digit form. c3 is a second person.
    c.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name) VALUES (?,?,?,?)",
        ("c1", DATASET, "imessage", "Casey Verano"),
    )
    c.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name) VALUES (?,?,?,?)",
        ("c2", DATASET, "csv_import", None),
    )
    c.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name) VALUES (?,?,?,?)",
        ("c3", DATASET, "imessage", "Ana Torres"),
    )
    ids = [
        (DATASET, "imessage", "+15125550100", "phone", "c1"),
        (DATASET, "csv_import", "5125550100", "phone", "c2"),
        (DATASET, "imessage", "+15125550101", "phone", "c3"),
    ]
    c.executemany(
        "INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id)"
        " VALUES (?,?,?,?,?)",
        ids,
    )

    # --- conversation_messages: 20 rows total.
    # Casey: 14 rows under "+15125550100" + 3 rows under "5125550100" = 17.
    # Ana: 3 rows. Days: 2026-08-01 x 10, 2026-08-02 x 6, 2026-08-03 x 4.
    rows = []
    seq = 0

    def msg(sender: str, day: str, hour: int, content: str = "hello world") -> None:
        nonlocal seq
        seq += 1
        rows.append(
            (
                f"m{seq:03d}",
                "conv1",
                DATASET,
                "contact",
                sender,
                "text",
                f"{day}T{hour:02d}:00:00",
                content,
                "imessage",
            )
        )

    for i in range(8):
        msg("+15125550100", "2026-08-01", 9 + (i % 3))
    for i in range(4):
        msg("+15125550100", "2026-08-02", 22)  # late-night block
    for i in range(2):
        msg("+15125550100", "2026-08-03", 10)
    for i in range(3):
        msg("5125550100", "2026-08-03", 11)  # the ghost half of the same human
    msg("+15125550101", "2026-08-01", 9)
    msg("+15125550101", "2026-08-01", 12)
    msg("+15125550102", "2026-08-02", 13)  # no contact row: falls back to raw sender

    assert len(rows) == 20
    c.executemany(
        "INSERT INTO conversation_messages"
        " (message_id, conversation_id, dataset_id, sender_type, sender_id,"
        "  message_type, event_at, content, source_id) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )

    # --- financial_transactions: amounts 10,20,30,40,100 = 200 total.
    fin = [
        ("f1", "2026-07-15T10:00:00", 10.0, "food"),
        ("f2", "2026-07-20T10:00:00", 20.0, "food"),
        ("f3", "2026-08-01T10:00:00", 30.0, "food"),
        ("f4", "2026-08-02T10:00:00", 40.0, "travel"),
        ("f5", "2026-08-03T10:00:00", 100.0, "travel"),
    ]
    c.executemany(
        "INSERT INTO financial_transactions"
        " (transaction_id, posted_at, amount, category, account_type, source_id)"
        " VALUES (?,?,?,?, 'checking', 'bank')",
        fin,
    )

    # --- journal_entries: 4 rows, two moods.
    jr = [
        ("j1", "2026-08-01T08:00:00", "calm", 30.0),
        ("j2", "2026-08-01T20:00:00", "calm", 20.0),
        ("j3", "2026-08-02T08:00:00", "stressed", 10.0),
        ("j4", "2026-08-03T08:00:00", "calm", 40.0),
    ]
    c.executemany(
        "INSERT INTO journal_entries (entry_id, entry_at, mood_tag, duration, source_id)"
        " VALUES (?,?,?,?, 'journal')",
        jr,
    )

    # --- calendar_events / activity_events / location_events: 2 each.
    c.executemany(
        "INSERT INTO calendar_events (event_id, title, starts_at, ends_at, event_type, source_id)"
        " VALUES (?,?,?,?,?, 'gcal')",
        [
            ("e1", "standup", "2026-08-01T09:00:00", "2026-08-01T09:30:00", "meeting"),
            ("e2", "focus", "2026-08-02T13:00:00", "2026-08-02T15:00:00", "block"),
        ],
    )
    c.executemany(
        "INSERT INTO activity_events (event_id, occurred_at, activity_type, source_id)"
        " VALUES (?,?,?, 'browser')",
        [("a1", "2026-08-01T10:00:00", "browse"), ("a2", "2026-08-02T11:00:00", "browse")],
    )
    c.executemany(
        "INSERT INTO location_events (event_id, event_at, event_type, place_name, city, source_id)"
        " VALUES (?,?,?,?,?, 'places')",
        [
            ("l1", "2026-08-01T10:00:00", "visit", "Cafe", "Austin"),
            ("l2", "2026-08-02T11:00:00", "visit", "Office", "Austin"),
        ],
    )


def _run(conn, payload, *, guard=None):
    spec = validate_aggregate_params(payload)
    return run_aggregate(
        conn, spec, guard=guard or owner_ui_guard(conn), dataset_id=DATASET
    )


def _values_by_group(result) -> dict:
    return {r.get("group"): r.get("value") for r in result["rows"]}


def _values_by_bucket(result) -> dict:
    return {r.get("bucket"): r.get("value") for r in result["rows"]}


# ---------------------------------------------------------------- scalars


def test_count_total_scalar(conn):
    r = _run(conn, {"scope_id": "messages:read", "measure": "count"})
    assert r["answer_type"] == "aggregate"
    assert r["rows"] == [{"value": 20}]


def test_scalar_zero_is_an_answer_not_an_absence(conn):
    r = _run(
        conn,
        {
            "scope_id": "messages:read",
            "measure": "count",
            "since": "2030-01-01T00:00:00",
        },
    )
    assert r["rows"] == [{"value": 0}]


def test_sum_avg_min_max_amounts(conn):
    for measure, want in (("sum", 200.0), ("avg", 40.0), ("min", 10.0), ("max", 100.0)):
        r = _run(
            conn,
            {"scope_id": "resources:read", "measure": measure, "field": "amount"},
        )
        assert r["rows"] == [{"value": want}], measure


# ---------------------------------------------------------------- group_by


def test_group_by_category_exact(conn):
    r = _run(
        conn,
        {"scope_id": "resources:read", "measure": "count", "group_by": "category"},
    )
    assert _values_by_group(r) == {"food": 3, "travel": 2}


def test_sum_by_category_exact(conn):
    r = _run(
        conn,
        {
            "scope_id": "resources:read",
            "measure": "sum",
            "field": "amount",
            "group_by": "category",
        },
    )
    assert _values_by_group(r) == {"food": 60.0, "travel": 140.0}


def test_group_by_mood(conn):
    r = _run(
        conn, {"scope_id": "health:read", "measure": "count", "group_by": "mood_tag"}
    )
    assert _values_by_group(r) == {"calm": 3, "stressed": 1}


# ---------------------------------------------------------------- buckets


def test_count_by_day_messages(conn):
    r = _run(conn, {"scope_id": "messages:read", "measure": "count", "bucket": "day"})
    assert _values_by_bucket(r) == {
        "2026-08-01": 10,
        "2026-08-02": 5,
        "2026-08-03": 5,
    }


def test_count_by_month_finance(conn):
    r = _run(conn, {"scope_id": "resources:read", "measure": "count", "bucket": "month"})
    assert _values_by_bucket(r) == {"2026-07": 2, "2026-08": 3}


def test_hour_of_day_bucket_finds_the_late_nights(conn):
    r = _run(
        conn, {"scope_id": "messages:read", "measure": "count", "bucket": "hour_of_day"}
    )
    by_hour = _values_by_bucket(r)
    assert by_hour["22"] == 4  # the late-night block
    assert sum(by_hour.values()) == 20


def test_time_range_filter(conn):
    r = _run(
        conn,
        {
            "scope_id": "messages:read",
            "measure": "count",
            "since": "2026-08-02T00:00:00",
            "until": "2026-08-02T23:59:59",
        },
    )
    assert r["rows"] == [{"value": 5}]


# ---------------------------------------------------------------- person


def test_person_group_alias_trap(conn):
    """One human under +1512… and 512… must be ONE group of 17, never 14+3.

    protects: per-person numbers bind to people, not ghosts.
    """
    r = _run(
        conn, {"scope_id": "messages:read", "measure": "count", "group_by": "person"}
    )
    by_label = {row["label"]: row["value"] for row in r["rows"]}
    assert by_label["Casey Verano"] == 17
    assert by_label["Ana Torres"] == 2
    # The contact-less sender falls back to its raw identifier, still counted.
    assert by_label["+15125550102"] == 1
    assert sum(by_label.values()) == 20


# ---------------------------------------------------------------- blackhole


def _blackhole_casey(conn) -> None:
    conn.execute(
        "INSERT INTO entities (entity_id, canonical_name, normalized_name, entity_type, contact_id)"
        " VALUES ('ent-casey', 'Casey Verano', 'casey verano', 'person', 'c1')"
    )
    conn.execute(
        "INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name)"
        " VALUES ('bh1', 'ent-casey', 'casey verano', 'Casey Verano')"
    )
    conn.commit()


def test_blackholed_person_absent_from_agent_aggregates(conn):
    """A black-holed human is absent — group gone AND rows uncounted — for a
    non-owner-UI caller; the exclusion covers BOTH contact rows of the human.

    protects: black-holed people are absent from aggregates, not renamed.
    """
    _blackhole_casey(conn)
    guard = BlackholeGuard(conn, caller_class=CallerClass.OWNER_AGENT)
    r = _run(
        conn,
        {"scope_id": "messages:read", "measure": "count", "group_by": "person"},
        guard=guard,
    )
    labels = {row["label"] for row in r["rows"]}
    assert "Casey Verano" not in labels
    assert not any("casey" in str(l).lower() for l in labels)
    assert sum(row["value"] for row in r["rows"]) == 3  # 2 Ana + 1 contact-less

    # Scalar totals move with the same exclusion — no count side-channel.
    total = _run(
        conn, {"scope_id": "messages:read", "measure": "count"}, guard=guard
    )
    assert total["rows"] == [{"value": 3}]


def test_owner_ui_still_sees_blackholed_person(conn):
    _blackhole_casey(conn)
    r = _run(
        conn,
        {"scope_id": "messages:read", "measure": "count", "group_by": "person"},
        guard=owner_ui_guard(conn),
    )
    by_label = {row["label"]: row["value"] for row in r["rows"]}
    assert by_label.get("Casey Verano") == 17


# ---------------------------------------------------------------- validation


def test_unknown_scope_is_param_error(conn):
    with pytest.raises(AggregateParamError) as exc:
        validate_aggregate_params({"scope_id": "nope:read", "measure": "count"})
    assert exc.value.reason == "aggregate_scope_unsupported"


def test_unknown_measure_group_bucket_field(conn):
    for bad in (
        {"scope_id": "messages:read", "measure": "median"},
        {"scope_id": "messages:read", "measure": "count", "group_by": "sender_ssn"},
        {"scope_id": "messages:read", "measure": "count", "bucket": "fortnight"},
        {"scope_id": "messages:read", "measure": "sum", "field": "content"},
        {"scope_id": "messages:read", "measure": "sum"},  # sum needs a curated field
        {"scope_id": "resources:read", "measure": "sum", "field": "amount", "since": "not-a-date"},
    ):
        with pytest.raises(AggregateParamError) as exc:
            validate_aggregate_params(bad)
        assert exc.value.reason == "aggregate_param_invalid", bad


# ---------------------------------------------------------------- sweep


def test_every_curated_combination_emits_valid_sql(conn):
    """Property sweep: every (measure x group_by x bucket) the registry curates
    executes on a seeded DB and returns numeric values.

    protects: no curated combination is dead on arrival.
    """
    guard = owner_ui_guard(conn)
    ran = 0
    for scope_id, spec in AGGREGATE_REGISTRY.items():
        measures = ["count"] + [
            (m, f) for f in spec.fields for m in ("sum", "avg", "min", "max")
        ]
        for m in measures:
            payload = {"scope_id": scope_id}
            if m == "count":
                payload["measure"] = "count"
            else:
                payload["measure"], payload["field"] = m
            for group_by in [None] + sorted(spec.group_bys):
                for bucket in [None] + sorted(spec.buckets):
                    p = dict(payload)
                    if group_by:
                        p["group_by"] = group_by
                    if bucket:
                        p["bucket"] = bucket
                    result = _run(conn, p, guard=guard)
                    assert isinstance(result["rows"], list), p
                    for row in result["rows"]:
                        assert isinstance(row["value"], (int, float)), p
                    ran += 1
    assert ran >= 100  # the surface is real, not a two-cell registry


def test_group_cap_truncates_with_receipt(conn):
    many = [
        (f"x{i}", f"2026-08-01T10:00:00", 1.0, f"cat{i:03d}") for i in range(AGGREGATE_GROUP_CAP + 10)
    ]
    conn.executemany(
        "INSERT INTO financial_transactions"
        " (transaction_id, posted_at, amount, category, account_type, source_id)"
        " VALUES (?,?,?,?, 'checking', 'bank')",
        many,
    )
    conn.commit()
    r = _run(
        conn,
        {"scope_id": "resources:read", "measure": "count", "group_by": "category"},
    )
    assert len(r["rows"]) == AGGREGATE_GROUP_CAP
    assert r["truncated"]["group_cap"] == AGGREGATE_GROUP_CAP
