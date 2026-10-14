"""The enrichment status scan runs off the engine loop (lane 172, 14 Oct 2026).

On the owner's node the watchdog's stalled frames were the sources page's chain: `handle_get_sources_overview` ->
`_get_sources_overview_core` -> `_get_enrichment_status_core` -> `_find_unprocessed_messages`, plain blocking reads
of every canonical message, awaited on the loop while a recipient's ask waited. Both loop callers now go through
`enrichment_status_off_loop`.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from topos.api import enrichment, sources_overview


def test_the_status_core_runs_on_a_worker_thread(monkeypatch):
    seen = {}

    async def core(source_id, dataset_id=None):
        seen["thread"] = threading.get_ident()
        return {"status": "ok", "source_id": source_id, "dataset_id": dataset_id}

    monkeypatch.setattr(enrichment, "_get_enrichment_status_core", core)

    async def ask():
        seen["loop"] = threading.get_ident()
        return await enrichment.enrichment_status_off_loop("journal", "u1:d1")

    assert asyncio.run(ask()) == {"status": "ok", "source_id": "journal", "dataset_id": "u1:d1"}
    assert seen["thread"] != seen["loop"]


def test_the_sources_overview_reads_status_through_the_off_loop_helper(monkeypatch):
    calls = []

    async def listed(payload):
        return {"sources": [{"source_id": "journal", "enrichment_trigger": "manual"}]}

    async def off_loop(source_id, dataset_id=None):
        calls.append((source_id, dataset_id))
        return {"status": "ok"}

    async def on_loop(*_args, **_kwargs):  # the old path: must not be awaited by the overview any more
        raise AssertionError("status read on the loop")

    monkeypatch.setattr(sources_overview, "_list_sources_core", listed)
    monkeypatch.setattr(sources_overview, "enrichment_status_off_loop", off_loop)
    monkeypatch.setattr(enrichment, "_get_enrichment_status_core", on_loop)
    monkeypatch.setattr(sources_overview, "get_db_connection", lambda: None)
    monkeypatch.setattr(sources_overview, "_scope_from_payload", lambda payload: {"dataset_id": "u1:d1"})
    result = asyncio.run(sources_overview._get_sources_overview_core({"include": "enrichment_status"}))
    assert calls == [("journal", "u1:d1")]
    assert result["metadata_by_source_id"]["journal"]["enrichment_status"] == {"status": "ok"}


def test_the_status_handler_reads_through_the_off_loop_helper(monkeypatch):
    from topos.core.handlers import enrichment as handlers

    calls = []

    async def off_loop(source_id, dataset_id=None):
        calls.append((source_id, dataset_id))
        return {"status": "ok"}

    monkeypatch.setattr(enrichment, "enrichment_status_off_loop", off_loop)
    handler = next(getattr(handlers, name) for name in dir(handlers)
                   if name.startswith("handle_") and "status" in name and "enrichment" in name)
    reply = asyncio.run(handler({"id": "r1", "payload": {"source_id": "journal", "dataset_id": "u1:d1"}}))
    assert reply == {"id": "r1", "status": "ok", "payload": {"status": "ok"}} and calls == [("journal", "u1:d1")]
