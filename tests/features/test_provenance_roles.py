"""P1.2 (PLAN_PROVENANCE_SPLIT): record_role — the single source of truth.

The full row matrix: ai_chat (human/user/assistant/system), conversation
(is_from_self / sender_id / other-'human'), journal/profile by construction,
browser visit vs highlight, and the fail-toward-less-attributing default for
unknown/ambiguous rows. owner_authored must be exactly role == 'authored'.
"""

from __future__ import annotations

import json

import pytest

from topos.features.provenance.roles import (
    ROLE_ADDRESSED,
    ROLE_AMBIENT,
    ROLE_AUTHORED,
    ROLE_OBSERVED,
    ROLES,
    is_engaged,
    owner_authored,
    record_role,
)


def _ai_chat_row(sender_type: str) -> dict:
    # Exact canonical batch shape (mapper to_dict / api.enrichment loader):
    # is_from_self never present.
    return {
        "message_id": "a1",
        "conversation_id": "chatgpt:conv-1",
        "sender_type": sender_type,
        "sender_id": None,
        "content": "I live in Austin now",
        "event_at": "2026-06-01T10:00:00+00:00",
    }


def _messenger_row(**kw) -> dict:
    row = {
        "message_id": "m1",
        "conversation_id": "c1",
        "sender_type": "human",  # messenger_mapper default for EVERYONE
        "sender_id": "+15551234567",
        "content": "I live in Paris",
        "event_at": "2026-06-01T10:00:00+00:00",
        "is_from_self": 0,
    }
    row.update(kw)
    return row


class TestAiChatRoles:
    def test_human_is_authored(self):
        assert record_role(_ai_chat_row("human"), table="ai_chat_messages") == ROLE_AUTHORED

    def test_legacy_user_is_authored(self):
        assert record_role(_ai_chat_row("user"), table="ai_chat_messages") == ROLE_AUTHORED

    def test_assistant_is_addressed(self):
        assert record_role(_ai_chat_row("assistant"), table="ai_chat_messages") == ROLE_ADDRESSED

    def test_system_is_ambient(self):
        assert record_role(_ai_chat_row("system"), table="ai_chat_messages") == ROLE_AMBIENT

    def test_unknown_sender_in_ai_chat_is_ambient_never_authored(self):
        assert record_role(_ai_chat_row("tool"), table="ai_chat_messages") == ROLE_AMBIENT


class TestConversationRoles:
    def test_is_from_self_is_authored(self):
        assert (
            record_role(_messenger_row(is_from_self=1), table="conversation_messages")
            == ROLE_AUTHORED
        )

    def test_sender_id_self_is_authored(self):
        assert (
            record_role(
                _messenger_row(sender_id="self", is_from_self=0),
                table="conversation_messages",
            )
            == ROLE_AUTHORED
        )

    def test_other_human_is_observed(self):
        # participated-vs-observed heuristic explicitly deferred: v1 returns
        # observed for every non-self conversation row.
        assert record_role(_messenger_row(), table="conversation_messages") == ROLE_OBSERVED

    def test_sender_type_never_decides_conversation_rows(self):
        # messenger mapper writes 'human' for other people too; even a stray
        # 'user' must not flip a conversation row to authored.
        row = _messenger_row(sender_type="user")
        assert record_role(row, table="conversation_messages") == ROLE_OBSERVED


class TestAuthoredByConstruction:
    def test_journal_rows_are_authored(self):
        row = {"entry_id": "j1", "content": "long run", "mood_tag": "calm"}
        assert record_role(row, table="journal_entries") == ROLE_AUTHORED

    def test_profile_rows_are_authored(self):
        row = {"record_id": "p1", "record_type": "experience", "organization": "Lumon"}
        assert record_role(row, table="profile_records") == ROLE_AUTHORED

    def test_journal_row_inferred_without_table(self):
        row = {"entry_id": "j1", "entry_at": "2026-06-01", "content": "long run"}
        assert record_role(row, table="") == ROLE_AUTHORED


class TestBrowserRoles:
    def test_visit_is_ambient_not_engaged(self):
        row = {"event_id": "e1", "activity_type": "visit", "title": "Kombucha 101"}
        assert record_role(row, table="activity_events") == ROLE_AMBIENT
        assert not is_engaged(row)

    def test_highlight_is_ambient_but_engaged(self):
        # Engagement upgrades interest-eligibility, NEVER role: the highlighted
        # words remain the page author's.
        row = {
            "event_id": "e2",
            "activity_type": "browser_highlight",
            "title": "Fermentation Methods Deep Dive",
            "metadata_json": json.dumps({"highlight": "copper still method"}),
        }
        assert record_role(row, table="activity_events") == ROLE_AMBIENT
        assert is_engaged(row)

    def test_metadata_highlight_alone_is_engaged(self):
        row = {
            "event_id": "e3",
            "activity_type": "visit",
            "metadata_json": json.dumps({"highlight": "a span"}),
        }
        assert is_engaged(row)

    def test_star_event_is_engaged(self):
        row = {"event_id": "e4", "event_type": "star_page", "url": "https://x.test"}
        assert is_engaged(row)

    def test_visit_row_inferred_ambient_without_table(self):
        row = {"event_id": "e5", "url": "https://x.test", "title": "Page"}
        assert record_role(row, table="") == ROLE_AMBIENT


class TestFailLessAttributing:
    def test_ambiguous_human_without_owner_markers_is_observed(self):
        # No table + 'human' + no self markers: could be either family —
        # someone's speech, not provably the owner's.
        row = {"message_id": "x", "sender_type": "human", "content": "I live in Oslo"}
        assert record_role(row, table="") == ROLE_OBSERVED
        assert not owner_authored(row)

    def test_contentish_unknown_is_ambient(self):
        row = {"id": "z", "content": "some page text"}
        assert record_role(row, table="") == ROLE_AMBIENT

    def test_unknown_table_name_falls_through_to_row_shape(self):
        row = {"message_id": "x", "sender_type": "human", "content": "hi"}
        assert record_role(row, table="mystery_table") == ROLE_OBSERVED

    def test_never_authored_without_positive_evidence(self):
        for row in (
            {"content": "text"},
            {"sender_type": "human", "content": "text"},
            {"sender_id": "+1555", "conversation_id": "c1"},
            {"activity_type": "visit"},
        ):
            assert record_role(row, table="") != ROLE_AUTHORED


class TestNoTableFallbacks:
    def test_assistant_without_table_is_addressed(self):
        row = {"message_id": "x", "sender_type": "assistant", "content": "try linen"}
        assert record_role(row, table="") == ROLE_ADDRESSED

    def test_system_without_table_is_ambient(self):
        row = {"message_id": "x", "sender_type": "system", "content": "You are helpful"}
        assert record_role(row, table="") == ROLE_AMBIENT

    def test_legacy_user_without_table_is_authored(self):
        row = {"message_id": "x", "sender_type": "user", "content": "hi"}
        assert record_role(row, table="") == ROLE_AUTHORED

    def test_is_from_self_without_table_is_authored(self):
        row = {"message_id": "x", "is_from_self": 1, "content": "hi"}
        assert record_role(row, table="") == ROLE_AUTHORED

    def test_row_table_stamp_wins_over_inference(self):
        row = _ai_chat_row("human")
        row["_table"] = "ai_chat_messages"
        assert record_role(row, table="") == ROLE_AUTHORED

    def test_explicit_table_arg_wins_over_row_stamp(self):
        row = _messenger_row()
        row["_table"] = "ai_chat_messages"  # wrong stamp; caller knows better
        assert record_role(row, table="conversation_messages") == ROLE_OBSERVED


class TestPostureTiebreaker:
    def test_personal_posture_authors_unresolvable_rows(self):
        # journal-grade source (posture=personal): owner by construction.
        row = {"id": "n1", "content": "note text"}
        assert record_role(row, table="", posture="personal") == ROLE_AUTHORED

    def test_ambient_posture_keeps_rows_ambient(self):
        row = {"id": "n2", "content": "feed text"}
        assert record_role(row, table="", posture="ambient") == ROLE_AMBIENT

    def test_posture_never_overrides_table_rules(self):
        # A personal posture must not make an assistant reply authored.
        assert (
            record_role(_ai_chat_row("assistant"), table="ai_chat_messages", posture="personal")
            == ROLE_ADDRESSED
        )
        assert (
            record_role(_messenger_row(), table="conversation_messages", posture="personal")
            == ROLE_OBSERVED
        )


class TestOwnerAuthored:
    @pytest.mark.parametrize(
        "row,table,expected",
        [
            (_ai_chat_row("human"), "ai_chat_messages", True),
            (_ai_chat_row("user"), "ai_chat_messages", True),
            (_ai_chat_row("assistant"), "ai_chat_messages", False),
            (_ai_chat_row("system"), "ai_chat_messages", False),
            (_messenger_row(is_from_self=1), "conversation_messages", True),
            (_messenger_row(), "conversation_messages", False),
        ],
    )
    def test_owner_authored_is_exactly_role_authored(self, row, table, expected):
        assert owner_authored(row, table=table) is expected
        assert (record_role(row, table=table) == ROLE_AUTHORED) is expected

    def test_extract_shim_delegates_here(self):
        # facts/extract._is_owner_authored is a thin shim over owner_authored;
        # the two must never disagree.
        from topos.features.facts.extract import _is_owner_authored

        for row, table in (
            (_ai_chat_row("human"), "ai_chat_messages"),
            (_ai_chat_row("assistant"), "ai_chat_messages"),
            (_messenger_row(), "conversation_messages"),
            (_messenger_row(is_from_self=1), "conversation_messages"),
            ({"message_id": "x", "sender_type": "human"}, ""),
        ):
            assert _is_owner_authored(row, table) == owner_authored(row, table=table)


def test_role_vocabulary_is_the_approved_five():
    assert ROLES == ("authored", "addressed", "participated", "observed", "ambient")
