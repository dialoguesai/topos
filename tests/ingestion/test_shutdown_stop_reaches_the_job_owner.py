"""A node stop during an import's graph fill must reach the job that owns it.

Third review, 2026-09-11: with the stop travelling as an ordinary exception,
IngestionManager.process_job's post-canonical ``except Exception`` swallowed it,
saved the checkpoint and returned normally, so the pipeline job was completed
and reported "completed" to the control plane with its signal derivation never
run. The stop is a BaseException (ShutdownInterrupt) so that no catch-all on the
way can turn it into a finished import; job_runner requeues the job.
"""

from __future__ import annotations

import json

import pytest

from topos.runtime_shutdown import ShutdownInterrupt


@pytest.mark.asyncio
async def test_ingestion_manager_lets_a_shutdown_stop_through(tmp_path, monkeypatch):
    from topos.ingestion import canonical_pipeline
    from topos.ingestion.manager import IngestionManager
    from topos.ingestion.triggers.file_trigger import FileTrigger
    from topos.storage.raw.file_store import RawFileStore

    async def stopped(**_kwargs):
        raise ShutdownInterrupt("graph rebuild subprocess pid=1 stopped by node shutdown")

    monkeypatch.setattr(canonical_pipeline, "run_post_canonical_pipeline", stopped)
    fs = RawFileStore(base_path=tmp_path)
    records = [
        {"id": "m1", "thread_id": "t1", "role": "user", "content": "hello", "created_at": 1},
        {"id": "m2", "thread_id": "t1", "role": "assistant", "content": "hi", "created_at": 2},
    ]
    job = FileTrigger(file_store=fs).create_job_from_bytes(
        job_id="job-1",
        dataset_id="user:chatgpt",
        schema_id="chatgpt.conversation.v1",
        payload="\n".join(json.dumps(r) for r in records).encode(),
        file_format="jsonl",
    )
    with pytest.raises(ShutdownInterrupt):
        await IngestionManager(file_store=fs).process_job(job, source_id="chatgpt_ui_conversation")
