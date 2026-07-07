"""Broader fact extraction (audit item 6: the starving FactStore).

The live store held 5 facts because extraction only covered profile_records
(9 rows) and journal training mentions. These tests pin the new extractors:
first-person message declarations, journal practices/works_on, profile
education/skills, and the location-aggregate lives_in rule — plus the safety
properties (owner-authored only, junk-gated, conflict-safe confidences).
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.facts.extract import (
    _clean_object,
    derive_location_facts,
    extract_facts_from_batch,
    extract_journal_facts,
    extract_message_facts,
    extract_profile_facts,
)
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    db = sqlite3.connect(str(tmp_path / "facts.db"))
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    db.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent-owner', 'person', 'Owner', 'owner', 1)"
    )
    db.commit()
    yield db
    db.close()


def _facts_by_predicate(conn, predicate: str) -> list:
    import json

    rows = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
    ).fetchall()
    out = []
    for (payload,) in rows:
        data = json.loads(payload)
        if data.get("predicate") == predicate:
            out.append(data)
    return out


class TestMessagePatterns:
    def _msg(self, content: str, **kw) -> dict:
        base = {
            "message_id": "m1",
            "content": content,
            "is_from_self": 1,
            "event_at": "2026-06-01T10:00:00+00:00",
        }
        base.update(kw)
        return base

    def test_lives_in(self):
        facts = extract_message_facts(self._msg("Yeah I live in Austin now, it's great"))
        assert [(f["predicate"], f["object_value"]) for f in facts] == [("lives_in", "Austin")]
        assert facts[0]["disclosure"] == "owner_only"

    def test_moved_to_carries_event_date(self):
        facts = extract_message_facts(self._msg("I just moved to San Francisco!"))
        assert facts[0]["object_value"] == "San Francisco"
        assert facts[0]["valid_from"] == "2026-06-01T10:00:00+00:00"

    def test_works_at_clips_trailing_clause(self):
        facts = extract_message_facts(
            self._msg("I work at Dialogues and it keeps me busy")
        )
        assert [(f["predicate"], f["object_value"]) for f in facts] == [
            ("works_at", "Dialogues")
        ]
        assert facts[0]["disclosure"] == "scoped"

    def test_training_for_lowercased(self):
        facts = extract_message_facts(self._msg("I'm training for a Half Marathon in October"))
        assert facts[0]["predicate"] == "training_for"
        assert facts[0]["object_value"] == "half marathon in october"[:40] or facts[0][
            "object_value"
        ].startswith("half marathon")

    def test_lowercase_object_head_rejected(self):
        # "the office" is not a proper noun — the strict head keeps precision.
        assert extract_message_facts(self._msg("I work at the office most days")) == []

    def test_non_self_messages_ignored(self):
        assert extract_message_facts(self._msg("I live in Paris", is_from_self=0)) == []

    def test_assistant_chat_ignored_user_kept(self):
        assistant = self._msg("I live in Berlin", sender_type="assistant")
        assistant.pop("is_from_self")
        assert extract_message_facts(assistant) == []
        user = self._msg("I live in Berlin", sender_type="user")
        user.pop("is_from_self")
        assert extract_message_facts(user)[0]["object_value"] == "Berlin"

    def test_junk_content_gated(self):
        junk = self._msg("streamtyped NSKeyedArchiver I live in Austin")
        assert extract_message_facts(junk) == []

    def test_message_confidence_cannot_silently_supersede_resume(self):
        # Chat-derived works_at (0.6) vs resume incumbent (0.9): the margin
        # rule must queue a conflict, not overwrite.
        from topos.features.facts.store import CONFLICT_CONFIDENCE_MARGIN

        for _, _, confidence, _, _, _ in [
            p for p in __import__(
                "topos.features.facts.extract", fromlist=["_MSG_FACT_PATTERNS"]
            )._MSG_FACT_PATTERNS
        ]:
            assert confidence + CONFLICT_CONFIDENCE_MARGIN < 0.9


class TestCleanObject:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Austin", "Austin"),
            ("Google and love it", "Google"),
            ("Berlin. It's nice", "Berlin"),
            ("Topos though it changes", "Topos"),
            ("A", None),  # too short
            ("x" * 50, None),  # too long
            ("check https://x.co", None),
        ],
    )
    def test_cases(self, raw, expected):
        assert _clean_object(raw) == expected


class TestJournalExtractors:
    def test_practice_category(self):
        facts = extract_journal_facts({"category": "Weight-lifting", "content": ""})
        assert [(f["predicate"], f["object_value"]) for f in facts] == [
            ("practices", "weight-lifting")
        ]

    def test_project_category_via_entity_match(self, conn):
        conn.execute(
            "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)"
            " VALUES ('ent-topos', 'org', 'Topos', 'topos')"
        )
        facts = extract_journal_facts({"category": "Topos", "content": ""}, conn)
        assert [(f["predicate"], f["object_value"]) for f in facts] == [
            ("works_on", "Topos")
        ]

    def test_unknown_category_yields_nothing(self, conn):
        assert extract_journal_facts({"category": "Chill", "content": ""}, conn) == []


class TestProfileExtensions:
    def test_education_and_skill(self):
        edu = extract_profile_facts(
            {"record_type": "education", "organization": "UT Austin", "title": "BSc"}
        )
        assert [(f["predicate"], f["object_value"]) for f in edu] == [
            ("studied_at", "UT Austin")
        ]
        skill = extract_profile_facts({"record_type": "skill", "title": "Python"})
        assert [(f["predicate"], f["object_value"]) for f in skill] == [
            ("skilled_in", "Python")
        ]


class TestLocationAggregate:
    def _seed(self, conn, city: str, days: int, start: int = 0):
        for i in range(days):
            conn.execute(
                "INSERT INTO location_events (event_id, city, event_at, source_id)"
                " VALUES (?, ?, ?, 's')",
                (f"loc_{city}_{i}", city, f"2026-05-{start + i + 1:02d}T12:00:00+00:00"),
            )

    def test_dominant_city_asserted(self, conn):
        self._seed(conn, "Austin", 5)
        self._seed(conn, "San Francisco", 2, start=10)
        conn.commit()
        assert derive_location_facts(conn) == 1
        facts = _facts_by_predicate(conn, "lives_in")
        assert facts[0]["object_value"] == "Austin"
        assert facts[0]["confidence"] == 0.55

    def test_tie_or_sparse_asserts_nothing(self, conn):
        self._seed(conn, "Austin", 2)
        self._seed(conn, "Paris", 2, start=10)
        conn.commit()
        assert derive_location_facts(conn) == 0


class TestBatchEndToEnd:
    def test_message_rows_flow_through_batch(self, conn):
        rows = [
            {
                "message_id": "m1",
                "sender_id": "me",
                "is_from_self": 1,
                "content": "I work at Dialogues these days",
                "event_at": "2026-06-01T00:00:00+00:00",
                "_table": "conversation_messages",
            },
            {
                "message_id": "m2",
                "sender_id": "them",
                "is_from_self": 0,
                "content": "I work at Initech",
                "event_at": "2026-06-01T00:00:00+00:00",
                "_table": "conversation_messages",
            },
        ]
        written = extract_facts_from_batch(conn, rows)
        conn.commit()
        assert written == 1
        facts = _facts_by_predicate(conn, "works_at")
        assert [f["object_value"] for f in facts] == ["Dialogues"]

    def test_table_inferred_from_message_shape(self, conn):
        rows = [
            {
                "message_id": "m3",
                "sender_type": "user",
                "content": "I'm training for a Triathlon",
            }
        ]
        written = extract_facts_from_batch(conn, rows)
        conn.commit()
        assert written == 1
        assert _facts_by_predicate(conn, "training_for")

    def test_repeated_practice_sessions_merge_not_duplicate(self, conn):
        rows = [
            {"record_id": f"j{i}", "category": "Run", "content": "", "entry_at": "2026-06-01",
             "_table": "journal_entries"}
            for i in range(3)
        ]
        extract_facts_from_batch(conn, rows)
        conn.commit()
        facts = _facts_by_predicate(conn, "practices")
        assert len(facts) == 1  # belief revision refreshes, never duplicates
        import json as _json

        refs_json = conn.execute(
            "SELECT source_refs_json FROM signal_objects"
            " WHERE object_type='fact' AND valid_to IS NULL"
            " AND payload_json LIKE '%practices%'"
        ).fetchone()[0]
        assert len(_json.loads(refs_json or "[]")) >= 2  # refs merged on refresh
