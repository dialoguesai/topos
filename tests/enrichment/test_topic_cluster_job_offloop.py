"""TopicClusterJob.enrich must run its blocking body off the event loop.

The clustering path blocks for seconds (k-means, the label-pool wait, persist
under the write gate); run on the event-loop thread it starved every coroutine
including the control-plane keepalive, which dropped the websocket with a 1011
keepalive timeout during the 2026-08-06 inbox backlog.
"""

from __future__ import annotations

import threading

import pytest

from topos.enrichment.jobs.canonical import topic_clusters_job as mod


@pytest.mark.asyncio
async def test_enrich_body_runs_in_worker_thread(monkeypatch) -> None:
    seen: dict[str, int] = {}

    def _fake_conn():
        seen["thread"] = threading.get_ident()
        return None

    monkeypatch.setattr(mod, "get_db_connection", _fake_conn)
    job = mod.TopicClusterJob()
    out = await job.enrich([{"record_id": "r1", "source_id": "browser_visits"}])

    assert out == [{"_deferred": True, "error": "database_unavailable"}]
    assert seen["thread"] != threading.get_ident()
