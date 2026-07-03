"""
Iteration 2 — Live engine retrieval vs SQLite ground truth.
Verifies grant ceiling and mode gating on the real ~/.topos/database.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterFactory

pytestmark = [pytest.mark.adversarial, pytest.mark.gap]

LIVE_DB = Path(os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / "database.db"))


def _sqlite_count(table: str) -> int:
    conn = sqlite3.connect(LIVE_DB)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


@pytest.fixture(scope="module")
def live_orchestrator() -> QueryPipelineOrchestrator:
    if not LIVE_DB.exists():
        pytest.skip(f"live db missing: {LIVE_DB}")
    adapters = AdapterFactory.create("local_database", db_path=LIVE_DB)
    return QueryPipelineOrchestrator(adapters=adapters)


@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
def test_ground_truth_has_scope_backing_tables() -> None:
    assert _sqlite_count("conversation_messages") > 100
    assert _sqlite_count("activity_events") > 10
    assert _sqlite_count("ai_chat_messages") > 1


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_messages_raw_without_ceiling_returns_live_query(live_orchestrator: QueryPipelineOrchestrator) -> None:
    manifest = resolve_scope_manifest("messages:read")
    out = await live_orchestrator.execute(
        query_text="recent messages sample",
        scope_id="messages:read",
        access_mode="raw",
        manifest=manifest,
        query_session_id="adv-iter2-raw-open",
    )
    assert out.get("turn_outcome") != "denied", out.get("deny_reason")
    blob = json.dumps(out.get("public_result") or {}, default=str).lower()
    assert "message" in blob or "row" in blob or "content" in blob or out.get("turn_outcome") == "live_query"


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_messages_raw_blocked_when_grant_ceiling_summary(live_orchestrator: QueryPipelineOrchestrator) -> None:
    manifest = resolve_scope_manifest(
        "messages:read",
        filter_manifest={"access_mode_ceiling": "summary"},
    )
    out = await live_orchestrator.execute(
        query_text="try raw bypass",
        scope_id="messages:read",
        access_mode="raw",
        manifest=manifest,
        query_session_id="adv-iter2-raw-blocked",
    )
    assert out.get("turn_outcome") == "denied" or out.get("deny_reason")
    reason = str(out.get("deny_reason") or "").lower()
    assert "ceiling" in reason or "mode" in reason


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_messages_summary_allowed_under_summary_ceiling(live_orchestrator: QueryPipelineOrchestrator) -> None:
    manifest = resolve_scope_manifest(
        "messages:read",
        filter_manifest={"access_mode_ceiling": "summary"},
    )
    out = await live_orchestrator.execute(
        query_text="message themes",
        scope_id="messages:read",
        access_mode="summary",
        manifest=manifest,
        query_session_id="adv-iter2-summary-ok",
    )
    assert out.get("turn_outcome") != "denied", out.get("deny_reason")


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_activity_raw_touches_canonical_table(live_orchestrator: QueryPipelineOrchestrator) -> None:
    if _sqlite_count("activity_events") == 0:
        pytest.skip("no activity_events rows")
    manifest = resolve_scope_manifest("activity:read")
    out = await live_orchestrator.execute(
        query_text="recent browsing activity",
        scope_id="activity:read",
        access_mode="raw",
        manifest=manifest,
        query_session_id="adv-iter2-activity-raw",
    )
    assert out.get("turn_outcome") != "denied", out.get("deny_reason")


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_ai_conversations_inference_blocked_by_summary_ceiling(live_orchestrator: QueryPipelineOrchestrator) -> None:
    manifest = resolve_scope_manifest(
        "ai_conversations:read",
        filter_manifest={"access_mode_ceiling": "summary"},
    )
    out = await live_orchestrator.execute(
        query_text="infer goals",
        scope_id="ai_conversations:read",
        access_mode="inference",
        manifest=manifest,
        query_session_id="adv-iter2-inf-blocked",
    )
    assert out.get("turn_outcome") == "denied" or out.get("deny_reason")


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_relationship_context_summary_no_raw_message_content(live_orchestrator: QueryPipelineOrchestrator) -> None:
    manifest = resolve_scope_manifest(
        "relationship_context:read",
        filter_manifest={"access_mode_ceiling": "summary"},
    )
    out = await live_orchestrator.execute(
        query_text="relationship warmth",
        scope_id="relationship_context:read",
        access_mode="summary",
        manifest=manifest,
        query_session_id="adv-iter2-rel-summary",
    )
    if out.get("turn_outcome") == "denied":
        pytest.skip(f"relationship summary unavailable: {out.get('deny_reason')}")
    blob = json.dumps(out.get("public_result") or {}, default=str)
    assert "conversation_messages" not in blob.lower()
