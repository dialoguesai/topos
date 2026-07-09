"""P1.3 (PLAN_PROVENANCE_SPLIT): role gates in goal_extraction and emo_27.

- GoalExtractionJob extracts ONLY from owner-authored rows: assistant text
  must never mint goals (it was polluting user_goals — Appendix A offender 1).
- Emo27Job still classifies EVERY row (message_emotions is per-message data)
  but stamps each emitted record with its record_role so owner-wellbeing
  aggregation can filter — and the engine payload keeps its exact old shape.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import topos.enrichment.jobs.canonical.emo_27_job as emo_module
import topos.enrichment.jobs.canonical.goal_extraction_job as goal_module
from topos.enrichment.jobs.canonical.emo_27_job import Emo27Job
from topos.enrichment.jobs.canonical.goal_extraction_job import GoalExtractionJob


def _ai_msg(i: int, sender_type: str, content: str) -> Dict[str, Any]:
    return {
        "message_id": f"a{i}",
        "_table": "ai_chat_messages",
        "conversation_id": "chatgpt:conv-1",
        "sender_type": sender_type,
        "content": content,
        "source_id": "chatgpt_file_ingestion",
        "event_at": "2026-06-01T10:00:00+00:00",
    }


def _conv_msg(i: int, *, mine: bool, content: str) -> Dict[str, Any]:
    return {
        "message_id": f"m{i}",
        "_table": "conversation_messages",
        "conversation_id": "c1",
        "sender_type": "human",
        "sender_id": "self" if mine else "+31612345678",
        "is_from_self": 1 if mine else 0,
        "content": content,
        "source_id": "demo_messenger_file",
        "event_at": "2026-06-01T10:00:00+00:00",
    }


class TestGoalExtractionRoleGate:
    def _run(self, monkeypatch, messages) -> tuple[List[Dict[str, Any]], List[str]]:
        engine_called_for: List[str] = []

        async def fake_run_engine_task(engine, *, task_id, subtype, source_id,
                                       record_ids, input_payload, provider=None,
                                       model=None):
            engine_called_for.extend(record_ids)
            return SimpleNamespace(
                status="completed",
                output={
                    "goals": [
                        {"text": "learn the mandolin properly", "confidence": 0.8,
                         "horizon": "quarter"}
                    ],
                    "model": "fake-model",
                },
                error=None,
            )

        monkeypatch.setattr(goal_module, "run_engine_task", fake_run_engine_task)
        job = GoalExtractionJob(engine=SimpleNamespace())
        results = asyncio.run(job.enrich(messages))
        return results, engine_called_for

    def test_assistant_text_produces_no_goals(self, monkeypatch):
        results, called = self._run(
            monkeypatch,
            [
                _ai_msg(1, "assistant", "You should set a goal to run a marathon"),
                _ai_msg(2, "system", "You are a helpful assistant"),
            ],
        )
        assert results == []
        assert called == []

    def test_authored_rows_extract_goals(self, monkeypatch):
        results, called = self._run(
            monkeypatch,
            [
                _ai_msg(1, "human", "I want to learn the mandolin this year"),
                _ai_msg(2, "assistant", "Great goal! You should also try banjo"),
            ],
        )
        assert called == ["a1"]  # assistant row never reached the engine
        assert len(results) == 1
        row = results[0]
        # output schema preserved exactly
        assert set(row) == {
            "message_id", "source_id", "goal_text", "confidence", "horizon",
            "provider", "model",
        }
        assert row["message_id"] == "a1"
        assert row["goal_text"] == "learn the mandolin properly"

    def test_observed_conversation_rows_skipped(self, monkeypatch):
        results, called = self._run(
            monkeypatch,
            [
                _conv_msg(1, mine=False, content="I plan to open a bakery"),
                _conv_msg(2, mine=True, content="I want to ship the provenance split"),
            ],
        )
        assert called == ["m2"]
        assert len(results) == 1 and results[0]["message_id"] == "m2"


class TestEmo27RoleStamp:
    def _run(self, monkeypatch, messages):
        payloads: List[Dict[str, Any]] = []

        async def fake_run_engine_task(engine, *, task_id, subtype, source_id,
                                       record_ids, input_payload, provider=None,
                                       model=None):
            payloads.append(input_payload)
            return SimpleNamespace(
                status="completed",
                output={
                    "items": [
                        {"emotion_label": "joy", "confidence": 0.9,
                         "all_emotions": [], "model": "fake-emo"}
                        for _ in input_payload["items"]
                    ]
                },
                error=None,
            )

        monkeypatch.setattr(emo_module, "run_engine_task", fake_run_engine_task)
        job = Emo27Job(engine=SimpleNamespace())
        results = asyncio.run(job.enrich(messages))
        return results, payloads

    def test_all_rows_classified_with_role_stamp(self, monkeypatch):
        results, _ = self._run(
            monkeypatch,
            [
                _ai_msg(1, "human", "I am thrilled"),
                _ai_msg(2, "assistant", "That is wonderful"),
                _conv_msg(3, mine=False, content="ugh, terrible day"),
                _conv_msg(4, mine=True, content="feeling calm"),
            ],
        )
        # per-message data: EVERY row still classified
        roles = {r["message_id"]: r["role"] for r in results}
        assert roles == {
            "a1": "authored",
            "a2": "addressed",
            "m3": "observed",
            "m4": "authored",
        }

    def test_engine_payload_shape_unchanged(self, monkeypatch):
        _, payloads = self._run(
            monkeypatch,
            [_ai_msg(1, "human", "I am thrilled"), _conv_msg(2, mine=False, content="hey")],
        )
        assert payloads
        for payload in payloads:
            for item in payload["items"]:
                # exact pre-P1.3 payload contract: no role key leaks upstream
                assert set(item) == {"id", "message_id", "text", "source_id"}

    def test_journal_rows_stamp_authored(self, monkeypatch):
        results, _ = self._run(
            monkeypatch,
            [{
                "entry_id": "j1",
                "_table": "journal_entries",
                "entry_at": "2026-06-01T07:00:00+00:00",
                "content": "Morning run felt good",
                "mood_tag": "energized",
                "source_id": "demo_journal_file",
            }],
        )
        assert len(results) == 1
        assert results[0]["role"] == "authored"
