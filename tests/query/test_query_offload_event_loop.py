"""Query retrieve must leave the event loop on a file-backed node.

Live 2026-09-08: ``retrieve()`` (MiniLM + write-gate) ran inside the uvicorn
async handler, ``/healthcheck`` stopped answering, and the macOS tray went red.
"""

from __future__ import annotations

import asyncio

import pytest

from topos.query import pipeline as query_pipeline


@pytest.mark.asyncio
async def test_offload_stays_on_loop_without_file_backed_db(monkeypatch):
    monkeypatch.setattr(query_pipeline, "_query_work_stays_on_loop", lambda _adapters=None: True)
    seen = []

    def work(value: int) -> int:
        seen.append(asyncio.get_running_loop())
        return value + 1

    assert await query_pipeline._offload_query_work(work, 3) == 4
    assert seen  # ran in this coroutine, not a worker


@pytest.mark.asyncio
async def test_offload_hops_to_thread_on_file_backed_node(monkeypatch):
    monkeypatch.setattr(query_pipeline, "_query_work_stays_on_loop", lambda _adapters=None: False)
    loop = asyncio.get_running_loop()
    ran_on = {"loop": False}

    def work() -> str:
        try:
            asyncio.get_running_loop()
            ran_on["loop"] = True
        except RuntimeError:
            ran_on["loop"] = False
        return "ok"

    assert await query_pipeline._offload_query_work(work) == "ok"
    assert ran_on["loop"] is False
    assert loop.is_running()


def test_retrieve_call_sites_still_use_offload_helper():
    """Sep 8 tray-red class: retrieve() ran on the uvicorn loop. Pin the hop."""
    from pathlib import Path

    src = Path(query_pipeline.__file__).read_text()
    assert "self._retrieve_on_calling_thread" in src
    assert src.count("_offload_query_work") >= 4
