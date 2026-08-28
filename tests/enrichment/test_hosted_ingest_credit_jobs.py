"""Hosted ingest jobs pass wallet denials through as derivation-debt reasons."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from topos.enrichment.jobs.canonical.fact_extraction_job import FactExtractionJob
from topos.enrichment.jobs.canonical.goal_extraction_job import GoalExtractionJob
from topos.enrichment.jobs.canonical.topics_job import TopicsJob


def _deferred_credits(*_args, **_kwargs):
    return SimpleNamespace(status="deferred", error="insufficient_credits", output={})


@pytest.mark.asyncio
async def test_topics_job_passes_through_insufficient_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(*_args, **_kwargs):
        return _deferred_credits()

    monkeypatch.setattr(
        "topos.enrichment.jobs.canonical.topics_job.run_engine_task",
        fake_run,
    )
    monkeypatch.setattr(
        "topos.enrichment.jobs.canonical.topics_job.get_signal_extraction_model_request",
        lambda: ("platform", "gpt-4o-mini"),
    )
    monkeypatch.setattr(
        "topos.enrichment.jobs.canonical.topics_job.get_signal_extraction_provider",
        lambda: "platform",
    )
    out = await TopicsJob(engine=SimpleNamespace()).enrich(
        [{"message_id": "m1", "content": "hello", "source_id": "github_activity"}]
    )
    assert out == [{"_deferred": True, "error": "insufficient_credits"}]


@pytest.mark.asyncio
async def test_goal_extraction_passes_through_insufficient_credits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(*_args, **_kwargs):
        return _deferred_credits()

    monkeypatch.setattr(
        "topos.enrichment.jobs.canonical.goal_extraction_job.run_engine_task",
        fake_run,
    )
    monkeypatch.setattr(
        "topos.enrichment.jobs.canonical.goal_extraction_job.get_signal_extraction_model_request",
        lambda: ("platform", "gpt-4o-mini"),
    )
    out = await GoalExtractionJob(engine=SimpleNamespace()).enrich(
        [
            {
                "message_id": "a1",
                "_table": "ai_chat_messages",
                "conversation_id": "chatgpt:conv-1",
                "sender_type": "human",
                "content": "I want to learn the mandolin this year",
                "source_id": "chatgpt_file_ingestion",
                "event_at": "2026-08-27T10:00:00+00:00",
            }
        ]
    )
    assert out == [{"_deferred": True, "error": "insufficient_credits"}]


@pytest.mark.asyncio
async def test_facts_job_defers_when_hosted_wallet_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "topos.enrichment.jobs.canonical.fact_extraction_job.get_db_connection",
        lambda: object(),
    )
    monkeypatch.setattr(
        "topos.config.facts_llm.resolve_facts_llm_request",
        lambda settings, conn: ("platform", "gpt-4o-mini"),
    )
    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.hosted_llm_wallet_allows",
        lambda force=False: False,
    )

    def _must_not_extract(*_args, **_kwargs):
        raise AssertionError("facts LLM must not run when the wallet is empty")

    monkeypatch.setattr(
        "topos.features.facts.extract.extract_facts_from_batch",
        _must_not_extract,
    )
    out = await FactExtractionJob().enrich(
        [{"message_id": "m1", "content": "hello", "source_id": "github_activity"}]
    )
    assert out == [{"_deferred": True, "error": "insufficient_credits"}]
