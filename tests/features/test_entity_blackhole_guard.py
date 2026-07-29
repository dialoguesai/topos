"""M1 foundation tests: the requester-aware black-hole predicate.

Maps to eval classes C6 (forgery / privilege escalation), C9 (indistinguishability),
C5 (derived-artifact containment, at the primitive level) and I6 (rebuild window).
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.blackhole_guard import (
    BlackholeGuard,
    CallerClass,
    guard_for,
)
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "guard.db"))
    apply_all_migrations(c)
    c.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json)
        VALUES ('ent-bh', 'person', 'Dana Reyes', 'dana reyes', '["Dana"]')
        """
    )
    c.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json)
        VALUES ('ent-ok', 'person', 'Sam Okoye', 'sam okoye', '[]')
        """
    )
    c.commit()
    store = BlackholeStore(c)
    store.blackhole_entity(entity_ref="ent-bh")
    store.mark_rebuild_complete("ent-bh")
    yield c
    c.close()


# ------------------------------------------------------ caller resolution


@pytest.mark.parametrize(
    "mcp_source,expected",
    [
        ("topos_home_chat", CallerClass.OWNER_UI),
        ("routine_executor", CallerClass.ROUTINE),
        ("claude_desktop", CallerClass.OWNER_AGENT),
        ("cursor", CallerClass.OWNER_AGENT),
        ("chatgpt", CallerClass.OWNER_AGENT),
        ("plugin_attach", CallerClass.GRANTEE),
        ("rpt", CallerClass.GRANTEE),
        ("unknown_mcp_client", CallerClass.OWNER_AGENT),
        (None, CallerClass.UNKNOWN),
        ("", CallerClass.UNKNOWN),
    ],
)
def test_caller_class_resolution(mcp_source, expected):
    assert CallerClass.resolve(mcp_source=mcp_source) == expected


def test_grantee_flag_wins_over_any_claimed_source():
    """C6.1 — a grantee cannot become the owner by naming a first-party source."""
    assert (
        CallerClass.resolve(mcp_source="topos_home_chat", is_grantee_request=True)
        == CallerClass.GRANTEE
    )
    assert (
        CallerClass.resolve(mcp_source="topos_home_chat", requester_is_owner=False)
        == CallerClass.GRANTEE
    )


def test_unknown_caller_is_filtered(conn):
    """Fail closed: anything not positively recognised as owner UI gets filtered."""
    guard = BlackholeGuard(conn, caller_class=CallerClass.UNKNOWN)
    assert guard.sees_everything is False
    assert guard.blocks_entity_id("ent-bh") is True


def test_garbage_caller_class_falls_back_to_unknown(conn):
    guard = BlackholeGuard(conn, caller_class="owner_ui_but_not_really")
    assert guard.caller_class == CallerClass.UNKNOWN
    assert guard.blocks_entity_id("ent-bh") is True


# --------------------------------------------------------- owner fidelity


def test_owner_ui_sees_everything(conn):
    """I3 — over-blocking is a failure too; this keeps the leak tests honest."""
    guard = guard_for(conn, mcp_source="topos_home_chat")

    assert guard.sees_everything is True
    assert guard.active is False
    assert guard.blocks_entity_id("ent-bh") is False
    assert guard.blocks_name("Dana Reyes") is False
    assert guard.text_mentions_blackholed("dinner with Dana Reyes") is False


@pytest.mark.parametrize(
    "mcp_source", ["claude_desktop", "cursor", "chatgpt", "plugin_attach", "rpt"]
)
def test_every_non_owner_caller_is_blocked(conn, mcp_source):
    """I1 — the owner's own third-party agents are blocked, same as grantees."""
    guard = guard_for(conn, mcp_source=mcp_source)

    assert guard.sees_everything is False
    assert guard.blocks_entity_id("ent-bh") is True
    assert guard.blocks_entity_id("ent-ok") is False


def test_spoofed_home_chat_header_does_not_grant_access(conn):
    """C6.2 — the trust signal must be server-derived, not client-asserted.

    Modelled at this layer: a caller the gateway resolved as a grantee stays a
    grantee no matter what client string rode along with the request.
    """
    guard = guard_for(conn, mcp_source="topos_home_chat", is_grantee_request=True)

    assert guard.caller_class == CallerClass.GRANTEE
    assert guard.blocks_entity_id("ent-bh") is True


# ------------------------------------------------------ D2: routine access


def test_routine_blocked_by_default(conn):
    guard = guard_for(conn, mcp_source="routine_executor")
    assert guard.sees_everything is False
    assert guard.blocks_entity_id("ent-bh") is True


def test_local_only_routine_may_see_protected_entities(conn):
    """D2 — allowed, but only when local-only end-to-end."""
    guard = guard_for(conn, mcp_source="routine_executor", routine_local_only=True)
    assert guard.sees_everything is True
    assert guard.blocks_entity_id("ent-bh") is False


def test_local_only_flag_does_not_help_a_grantee(conn):
    """The D2 carve-out is scoped to routines and must not leak sideways."""
    guard = guard_for(conn, mcp_source="rpt", routine_local_only=True)
    assert guard.sees_everything is False
    assert guard.blocks_entity_id("ent-bh") is True


# ---------------------------------------------------------- row filtering


def test_filter_rows_by_id(conn):
    guard = guard_for(conn, mcp_source="cursor")
    rows = [{"entity_id": "ent-bh", "n": 1}, {"entity_id": "ent-ok", "n": 2}]

    assert guard.filter_rows(rows) == [{"entity_id": "ent-ok", "n": 2}]


def test_filter_rows_catches_the_object_end_of_a_relation(conn):
    """C5.1 — a fact whose *object* is protected is keyed under another subject."""
    guard = guard_for(conn, mcp_source="cursor")
    rows = [
        {"subject_entity_id": "ent-ok", "object_entity_id": "ent-bh", "n": 1},
        {"subject_entity_id": "ent-ok", "object_entity_id": None, "n": 2},
    ]

    kept = guard.filter_rows(rows, id_keys=("subject_entity_id", "object_entity_id"))
    assert [r["n"] for r in kept] == [2]


def test_filter_rows_by_name_for_string_valued_artifacts(conn):
    """C5 — top_topics/stat group_key carry the name as a bare string, no id."""
    guard = guard_for(conn, mcp_source="cursor")
    rows = [{"group_key": "Dana Reyes"}, {"group_key": "Sam Okoye"}]

    kept = guard.filter_rows(rows, id_keys=(), name_keys=("group_key",))
    assert kept == [{"group_key": "Sam Okoye"}]


def test_owner_filtering_is_identity(conn):
    guard = guard_for(conn, mcp_source="topos_home_chat")
    rows = [{"entity_id": "ent-bh"}, {"entity_id": "ent-ok"}]
    assert guard.filter_rows(rows) == rows
    assert guard.filter_entity_ids(["ent-bh", "ent-ok"]) == ["ent-bh", "ent-ok"]


# ------------------------------------------------------- free-text egress


def test_text_scan_catches_canonical_name_and_alias(conn):
    """C4 — the alias path, for mentions the resolver never bound."""
    guard = guard_for(conn, mcp_source="rpt")

    assert guard.text_mentions_blackholed("had lunch with Dana Reyes today") is True
    assert guard.text_mentions_blackholed("had lunch with Dana today") is True
    assert guard.text_mentions_blackholed("had lunch with Sam Okoye today") is False


def test_withhold_returns_none_rather_than_a_redacted_stub(conn):
    """D3 — full exclusion. A hole where a name was is itself information."""
    guard = guard_for(conn, mcp_source="rpt")

    assert guard.withhold_if_mentions("coffee with Dana Reyes") is None
    assert guard.withhold_if_mentions("coffee with Sam Okoye") == "coffee with Sam Okoye"


def test_text_scan_is_a_noop_for_owner(conn):
    guard = guard_for(conn, mcp_source="topos_home_chat")
    assert guard.withhold_if_mentions("coffee with Dana Reyes") == "coffee with Dana Reyes"


# --------------------------------------------- I6: the pending-rebuild window


def test_pending_rebuild_withholds_all_prose_artifacts(conn):
    """Fail closed, never stale: a pre-flag brief may name the entity in prose
    the scan cannot catch, so the whole class is withheld until rebuilt."""
    store = BlackholeStore(conn)
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)
        VALUES ('ent-new', 'person', 'Kit Alvarez', 'kit alvarez')
        """
    )
    conn.commit()
    store.blackhole_entity(entity_ref="ent-new")  # rebuild now pending

    guard = guard_for(conn, mcp_source="cursor")
    briefs = [{"markdown_body": "a brief mentioning nobody in particular"}]

    assert guard.withhold_pending_rebuild() is True
    assert guard.filter_name_string_artifacts(briefs, text_keys=("markdown_body",)) == []


def test_after_rebuild_only_mentioning_artifacts_are_withheld(conn):
    guard = guard_for(conn, mcp_source="cursor")
    briefs = [
        {"markdown_body": "quarterly review with Dana Reyes"},
        {"markdown_body": "quarterly review with Sam Okoye"},
    ]

    assert guard.withhold_pending_rebuild() is False
    kept = guard.filter_name_string_artifacts(briefs, text_keys=("markdown_body",))
    assert kept == [{"markdown_body": "quarterly review with Sam Okoye"}]


def test_owner_keeps_prose_artifacts_during_rebuild_window(conn):
    """The window is a non-owner withholding; the owner is never degraded."""
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)
        VALUES ('ent-new2', 'person', 'Kit Alvarez', 'kit alvarez')
        """
    )
    conn.commit()
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-new2")

    guard = guard_for(conn, mcp_source="topos_home_chat")
    briefs = [{"markdown_body": "anything at all"}]

    assert guard.withhold_pending_rebuild() is False
    assert guard.filter_name_string_artifacts(briefs, text_keys=("markdown_body",)) == briefs


# ------------------------------------------------------------- no signal


def test_guard_never_raises_for_a_blocked_entity(conn):
    """D5 — hide by absence. An exception is a confirmation of existence."""
    guard = guard_for(conn, mcp_source="rpt")

    # Every accessor returns a value; none of them distinguishes "protected"
    # from "never existed" by raising or by carrying a reason.
    assert guard.blocks_entity_id("ent-bh") is True
    assert guard.blocks_entity_id("entity-that-never-existed") is False
    assert guard.filter_rows([{"entity_id": "ent-bh"}]) == []
    assert guard.filter_rows([{"entity_id": "entity-that-never-existed"}]) == [
        {"entity_id": "entity-that-never-existed"}
    ]
