"""Guards: long-running enrichment must not block the engine event loop.

The UI and control-plane keepalive share the engine asyncio loop with ingestion.
Sync LLM / CPU work on that thread causes keepalive ping timeouts and
"Node unreachable" in the UI while local enrichment keeps logging.

These tests pin the contract:
  1. Behavioral — FactExtractionJob.enrich keeps the loop schedulable.
  2. Static — async enrich methods that call known long-running sync entrypoints
     must offload via asyncio.to_thread (same pattern as run_engine_task).
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import pytest

from topos.enrichment.jobs.canonical.fact_extraction_job import FactExtractionJob

REPO_ROOT = Path(__file__).resolve().parents[2]
ENRICHMENT_JOBS_DIR = REPO_ROOT / "topos" / "enrichment" / "jobs"

# Sync callables that are known to block for long enough to kill CP keepalive
# when invoked on the event-loop thread from async enrich().
_MUST_OFFLOAD_SYNC_CALLS = frozenset(
    {
        "extract_facts_from_batch",
        "extract_owner_facts_llm",
        "_run_coro_blocking",
        "time.sleep",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "asyncio.run",
    }
)


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
        return parts[0] if len(parts) == 1 else None
    return None


def _async_enrich_methods(tree: ast.AST) -> list[ast.AsyncFunctionDef]:
    found: list[ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "enrich":
            found.append(node)
    return found


def _function_calls_to_thread(fn: ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name in {"to_thread", "asyncio.to_thread"}:
            return True
    return False


def _forbidden_sync_calls(fn: ast.AsyncFunctionDef) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name is None:
            continue
        # Bare sleep(...) is almost always time.sleep imported as sleep.
        if name == "sleep":
            hits.append("sleep")
            continue
        if name in _MUST_OFFLOAD_SYNC_CALLS or name.split(".")[-1] in {
            "extract_facts_from_batch",
            "extract_owner_facts_llm",
            "_run_coro_blocking",
        }:
            hits.append(name)
    return sorted(set(hits))


@pytest.mark.asyncio
async def test_fact_extraction_enrich_keeps_event_loop_responsive(monkeypatch):
    """fact_llm-style sync work must not starve concurrent UI/CP tasks."""

    work_seconds = 0.35

    def _slow_extract(_conn, _rows, **_):
        time.sleep(work_seconds)
        return 0

    monkeypatch.setattr(
        "topos.core.state.get_db_connection",
        lambda: object(),
    )
    monkeypatch.setattr(
        "topos.features.facts.extract.extract_facts_from_batch",
        _slow_extract,
    )

    job = FactExtractionJob()
    ticks = 0
    stop = asyncio.Event()

    async def ui_servicing_heartbeat() -> None:
        nonlocal ticks
        # Mimics CP ping / UI signal RPC scheduling on the shared loop.
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.04)

    heartbeat = asyncio.create_task(ui_servicing_heartbeat())
    try:
        await job.enrich([{"message_id": "m1", "content": "hello"}])
    finally:
        stop.set()
        await heartbeat

    # If enrich blocked the loop for ~work_seconds, we'd see ~1 tick.
    # With to_thread we expect roughly work_seconds / 0.04 ≈ 8+.
    assert ticks >= 5, (
        f"Event loop was blocked during FactExtractionJob.enrich "
        f"(only {ticks} UI heartbeats during {work_seconds:.2f}s sync work). "
        "Long-running fact extraction must run via asyncio.to_thread so the "
        "loop can keep servicing control-plane keepalive and UI RPCs."
    )


def test_async_enrich_jobs_offload_known_blocking_sync_calls():
    """Static guard: do not call long-running sync work directly from enrich()."""
    violations: list[str] = []

    for py in sorted(ENRICHMENT_JOBS_DIR.rglob("*.py")):
        if py.name.startswith("_") or py.name == "__init__.py":
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            violations.append(f"{py.relative_to(REPO_ROOT)}: parse error: {exc}")
            continue

        for fn in _async_enrich_methods(tree):
            forbidden = _forbidden_sync_calls(fn)
            if not forbidden:
                continue
            if _function_calls_to_thread(fn):
                continue
            rel = py.relative_to(REPO_ROOT)
            violations.append(
                f"{rel}::{fn.name} calls {forbidden} on the event-loop thread "
                "without asyncio.to_thread"
            )

    assert not violations, (
        "Long-running sync work must not run on the engine event loop "
        "(blocks CP keepalive / UI). Offload with asyncio.to_thread, matching "
        "run_engine_task / FactExtractionJob:\n  - "
        + "\n  - ".join(violations)
    )


def test_fact_extraction_job_source_uses_to_thread():
    """Pin the load-bearing offload site in source (complements the AST scan)."""
    src = (ENRICHMENT_JOBS_DIR / "canonical" / "fact_extraction_job.py").read_text(
        encoding="utf-8"
    )
    assert "asyncio.to_thread" in src
    assert "extract_facts_from_batch" in src


def test_fact_llm_stops_scheduling_after_shutdown(monkeypatch):
    """Ctrl+C must stop queuing more Ollama calls (cooperative cancel)."""
    from topos.features.facts.llm_extract import extract_owner_facts_llm
    from topos.runtime_shutdown import clear_shutdown, request_shutdown
    from topos.storage.db.migrations import apply_all_migrations
    import sqlite3

    clear_shutdown()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent-owner', 'person', 'Owner', 'owner', 1)"
    )
    conn.commit()

    calls: list[int] = []

    def _stub(prompt, row):
        calls.append(1)
        # After the first call starts completing, flip shutdown so remaining
        # semaphore waiters skip instead of burning more LLM work.
        if len(calls) == 1:
            request_shutdown("test_fact_llm_stops_scheduling_after_shutdown")
        time.sleep(0.05)
        return [{"predicate": "practices", "object": "yoga"}]

    rows = [
        {
            "message_id": f"m{i}",
            "conversation_id": "chatgpt:conv-1",
            "content": f"I have been practicing yoga for years number {i}",
            "sender_type": "human",
            "sender_id": None,
            "event_at": "2026-06-01T10:00:00+00:00",
            "_table": "ai_chat_messages",
            "source_id": "chatgpt",
        }
        for i in range(12)
    ]
    monkeypatch.setattr(
        "topos.features.facts.llm_extract.FACTS_LLM_CONCURRENCY",
        2,
    )
    try:
        extract_owner_facts_llm(conn, rows, extractor=_stub)
        # Without cooperative cancel this would be 12. Allow a small in-flight
        # window (concurrency=2) but reject draining the full batch.
        assert len(calls) <= 4, (
            f"fact_llm kept scheduling after shutdown ({len(calls)} calls); "
            "workers must poll is_shutdown_requested() between extractions"
        )
    finally:
        clear_shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_signal_entity_graph_handler_keeps_event_loop_responsive(monkeypatch):
    """UI graph reads must not block concurrent CP/UI tasks on the shared loop."""
    import time

    from topos.core.handlers.signal_features import handle_signal_entity_graph

    work_seconds = 0.25

    def _slow_entity_graph(*_args, **_kwargs):
        time.sleep(work_seconds)
        return {"nodes": [], "edges": []}

    monkeypatch.setattr(
        "topos.features.entities.reads.entity_graph",
        _slow_entity_graph,
    )
    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: object())

    ticks = 0
    stop = asyncio.Event()

    async def ui_servicing_heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.04)

    heartbeat = asyncio.create_task(ui_servicing_heartbeat())
    try:
        resp = await handle_signal_entity_graph(
            {"id": "req-graph", "type": "signal_entity_graph", "payload": {}}
        )
    finally:
        stop.set()
        await heartbeat

    assert resp is not None and resp.get("status") == "ok"
    assert ticks >= 4, (
        f"Event loop blocked during signal_entity_graph handler "
        f"(only {ticks} heartbeats during {work_seconds:.2f}s sync work)"
    )


@pytest.mark.asyncio
async def test_list_routine_runs_handler_keeps_event_loop_responsive(monkeypatch):
    """Run-history reads must not block concurrent CP/UI tasks on the shared loop."""
    import time

    from topos.core.handlers.routines import handle_list_routine_runs

    work_seconds = 0.25

    def _slow_list_runs(*_args, **_kwargs):
        time.sleep(work_seconds)
        return []

    monkeypatch.setattr("topos.routines.store.list_runs", _slow_list_runs)
    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: object())

    ticks = 0
    stop = asyncio.Event()

    async def ui_servicing_heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.04)

    heartbeat = asyncio.create_task(ui_servicing_heartbeat())
    try:
        resp = await handle_list_routine_runs(
            {
                "id": "req-runs",
                "type": "list_routine_runs",
                "payload": {
                    "user_id": "user-1",
                    "routine_id": "routine-1",
                },
            }
        )
    finally:
        stop.set()
        await heartbeat

    assert resp is not None and resp.get("status") == "ok"
    assert ticks >= 4, (
        f"Event loop blocked during list_routine_runs handler "
        f"(only {ticks} heartbeats during {work_seconds:.2f}s sync work)"
    )
