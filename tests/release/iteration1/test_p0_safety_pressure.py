"""Iteration 1 — P0 safety pressure tests (scrub, disclosure, query deny paths)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.sources.scrub_service import normalize_scrub_payload
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore
from topos.storage.db.migrations import apply_canonical_disclosure_v1_up


pytestmark = [pytest.mark.release_pressure, pytest.mark.p0]


class TestScrubPayloadNormalizationPressure:
    """Footguns that caused live data loss in release testing."""

    def test_top_level_dry_run_true_with_empty_options(self) -> None:
        _, opts = normalize_scrub_payload({"source_id": "browser_visits", "dry_run": True})
        assert opts.dry_run is True

    def test_top_level_preset_scrub_honors_dry_run(self) -> None:
        _, opts = normalize_scrub_payload(
            {"source_id": "browser_visits", "preset": "scrub", "dry_run": True}
        )
        assert opts.dry_run is True
        assert opts.purge_attributed_rows is True

    def test_top_level_preset_remove_still_respects_dry_run(self) -> None:
        _, opts = normalize_scrub_payload(
            {"source_id": "grow_journal", "preset": "remove", "dry_run": True}
        )
        assert opts.dry_run is True
        assert opts.remove_raw_and_flat is True

    def test_conflicting_dry_run_raises(self) -> None:
        with pytest.raises(ValueError, match="conflicting dry_run"):
            normalize_scrub_payload(
                {
                    "source_id": "x",
                    "dry_run": True,
                    "options": {"dry_run": False},
                }
            )

    def test_dry_run_string_false_must_not_coerce_to_live_scrub(self) -> None:
        """JSON clients sometimes send string booleans; must not treat 'false' as truthy."""
        _, opts = normalize_scrub_payload({"source_id": "x", "dry_run": "false"})
        assert opts.dry_run is False

    def test_dry_run_string_true_is_dry_run(self) -> None:
        _, opts = normalize_scrub_payload({"source_id": "x", "dry_run": "true"})
        assert opts.dry_run is True


class TestDisclosureFallbackPressure:
    """Disclosure columns missing on legacy DBs (MCP work_context path)."""

    def test_default_disclosure_without_content_disclosure_column(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE journal_entries (
                entry_id TEXT PRIMARY KEY,
                content TEXT,
                source_id TEXT,
                entry_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO journal_entries VALUES ('e1', 'goal: ship v1', 'grow_journal', '2026-01-01')"
        )
        conn.commit()
        store = SQLiteCanonicalStore(conn)
        page = store.list("journal_entries", disclosure_tier="default_disclosure", limit=5)
        assert page.total == 1
        assert "ship" in str(page.items[0].get("content", ""))

    def test_disclosure_migration_idempotent_on_legacy_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, content TEXT, source_id TEXT)"
        )
        conn.commit()
        apply_canonical_disclosure_v1_up(conn)
        apply_canonical_disclosure_v1_up(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_chat_messages)").fetchall()}
        assert "content_disclosure" in cols


class TestQueryPipelineDenyPressure:
    """Mode ceiling and empty-query policies for grantee safety."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query_text",
        ["", "   ", "\n\t", "\u2003"],
        ids=["empty", "spaces", "newline_tab", "unicode_space"],
    )
    async def test_whitespace_only_queries_denied(self, query_text: str) -> None:
        adapters = AdapterFactory.create("memory")
        orch = QueryPipelineOrchestrator(adapters=adapters)
        manifest = resolve_scope_manifest("messages:read")
        out = await orch.execute(
            query_text=query_text,
            scope_id="messages:read",
            access_mode="raw",
            manifest=manifest,
            query_session_id="pressure-empty",
        )
        assert out.get("turn_outcome") == "denied"
        assert out.get("deny_reason") == "empty_query"

    @pytest.mark.asyncio
    async def test_work_context_inference_exceeds_summary_ceiling(self) -> None:
        adapters = AdapterFactory.create("memory")
        orch = QueryPipelineOrchestrator(adapters=adapters)
        manifest = resolve_scope_manifest("work_context:read")
        out = await orch.execute(
            query_text="what are my work goals",
            scope_id="work_context:read",
            access_mode="inference",
            manifest=manifest,
            query_session_id="pressure-pb1",
        )
        assert out.get("turn_outcome") == "denied"
        assert out.get("deny_reason") == "mode_ceiling_exceeded"

    @pytest.mark.asyncio
    async def test_work_context_raw_exceeds_summary_ceiling(self) -> None:
        adapters = AdapterFactory.create("memory")
        orch = QueryPipelineOrchestrator(adapters=adapters)
        manifest = resolve_scope_manifest("work_context:read")
        out = await orch.execute(
            query_text="show raw journal",
            scope_id="work_context:read",
            access_mode="raw",
            manifest=manifest,
            query_session_id="pressure-raw-ceiling",
        )
        assert out.get("turn_outcome") == "denied"
        assert out.get("deny_reason") == "mode_ceiling_exceeded"

    @pytest.mark.asyncio
    async def test_session_without_prior_data_does_not_crash(self) -> None:
        """Regression: fresh session_id must not raise UnboundLocalError on owner check."""
        adapters = AdapterFactory.create("memory")
        orch = QueryPipelineOrchestrator(adapters=adapters)
        manifest = resolve_scope_manifest("messages:read")
        out = await orch.execute(
            query_text="hello",
            scope_id="messages:read",
            access_mode="summary",
            manifest=manifest,
            query_session_id="brand-new-session-no-store-row",
        )
        assert out.get("turn_outcome") in ("denied", "live_query", "memory_hit", "expand_boundary", "requalify")
