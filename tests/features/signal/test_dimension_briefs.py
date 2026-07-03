"""Tests for versioned signal dimension living briefs."""

from __future__ import annotations

import sqlite3

from topos.features.signal.brief_ontology import llm_merge_section_ids
from topos.features.signal.brief_schemas import empty_structured_for_dimension, render_markdown
from topos.features.signal.dimension_briefs import DimensionBriefStore
from topos.storage.db.migrations.wiki_mvp_phase6_dimension_briefs import apply_wiki_mvp_phase6_dimension_briefs_up


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_wiki_mvp_phase6_dimension_briefs_up(conn)
    return conn


def test_brief_user_edit_creates_revision_chain() -> None:
    conn = _conn()
    store = DimensionBriefStore(conn)
    head = store.get_brief("memory")
    assert head["revision_number"] == 1

    updated = store.save_user_edit("memory", "# Memory brief\n\n## Grant policy\nMy note.")
    assert updated["revision_number"] == 2
    assert "My note." in updated["markdown_body"]

    revisions = store.list_revisions("memory")
    assert len(revisions) == 2
    assert revisions[0]["change_kind"] == "user_edit"


def test_brief_system_merge_preserves_grant_policy() -> None:
    conn = _conn()
    store = DimensionBriefStore(conn)
    structured = empty_structured_for_dimension("profile")
    structured["sections"]["professional_identity"]["markdown"] = "glance"
    structured["sections"]["skills_and_credentials"]["markdown"] = "stable old"
    structured["sections"]["experience_arc"]["markdown"] = "recent old"
    structured["sections"]["grant_policy"]["markdown"] = "pinned owner truth"

    store.save_user_edit("profile", render_markdown("profile", structured))

    merge_keys = llm_merge_section_ids("profile")
    store.merge_system_update(
        "profile",
        {
            merge_keys[0]: "glance new",
            merge_keys[1]: "stable new",
            merge_keys[2]: "recent new",
        },
        source_id="chatgpt_ingestion",
    )
    head = store.get_brief("profile")
    assert "stable new" in head["markdown_body"]
    assert "pinned owner truth" in head["markdown_body"]
    assert head["revision_number"] == 3


def test_list_briefs_returns_all_signal_dimensions() -> None:
    conn = _conn()
    store = DimensionBriefStore(conn)
    items = store.list_briefs()
    dims = {row["signal_dimension"] for row in items}
    assert dims == {
        "profile",
        "time",
        "interests",
        "relationships",
        "work",
        "memory",
        "wellbeing",
        "resources",
        "places",
        "intentions",
    }


def test_list_briefs_reuses_existing_heads_without_duplicate_insert() -> None:
    conn = _conn()
    store = DimensionBriefStore(conn)
    first = store.get_brief("memory")
    items = store.list_briefs()
    memory_rows = [row for row in items if row["signal_dimension"] == "memory"]
    assert len(memory_rows) == 1
    assert memory_rows[0]["brief_id"] == first["brief_id"]
    assert memory_rows[0]["revision_number"] == first["revision_number"]
    count = conn.execute("SELECT COUNT(*) FROM signal_dimension_briefs").fetchone()[0]
    assert count == 10


def test_dimensions_for_brief_update_journal_lane() -> None:
    from topos.features.signal.dimension_registry import dimensions_for_brief_update

    dims = dimensions_for_brief_update(source_id="demo_journal_file")
    assert dims == ("wellbeing", "memory", "profile")


def test_dimensions_for_brief_update_financial_lane() -> None:
    from topos.features.signal.dimension_registry import dimensions_for_brief_update

    dims = dimensions_for_brief_update(source_id="demo_financial_file")
    assert dims == ("resources",)
