import asyncio
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_automatic_message_review import setup, answer
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
from topos.permissions_v2.message_review_contract import AutomaticReviewRequest
from topos.permissions_v2.canonical import PolicyError


def request_for(legacy):
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    now = canonical_utc_microseconds(legacy[1].execute('SELECT event_at FROM conversation_messages').fetchone()[0])//1000000 + 1
    return AutomaticReviewRequest(after=now-86400, before=now)


def test_repeated_pass_reuses_durable_assessment_without_new_model_call(legacy):
    resolver, reviews, _, _ = setup(legacy)
    calls = []
    async def classify(prepared):
        calls.append(1)
        return answer(prepared)
    worker = AutomaticReviewWorker(resolver, reviews, classifier=classify)
    with owner():
        asyncio.run(worker._process(request_for(legacy)))
        assert worker.status().assessed == 1
        restarted = AutomaticReviewWorker(resolver, reviews, classifier=classify)
        asyncio.run(restarted._process(request_for(legacy)))
        assert restarted.status().current == 1
    assert len(calls) == 1


def test_cancel_during_model_call_does_not_publish(legacy):
    resolver, reviews, identity, _ = setup(legacy)
    async def classify(prepared):
        worker.cancel()
        return answer(prepared)
    worker = AutomaticReviewWorker(resolver, reviews, classifier=classify)
    with owner():
        asyncio.run(worker._process(request_for(legacy)))
        assert worker.status().assessed == 0


def test_out_of_window_does_not_call_classifier(legacy):
    resolver, reviews, _, _ = setup(legacy)
    async def classify(prepared):
        pytest.fail('out-of-window content was classified')
    worker = AutomaticReviewWorker(resolver, reviews, classifier=classify)
    with owner():
        asyncio.run(worker._process(AutomaticReviewRequest(after=1,before=2)))
        assert worker.status().scanned == 0


def test_full_history_window_rejected(legacy):
    resolver, reviews, _, _ = setup(legacy)
    worker = AutomaticReviewWorker(resolver,reviews)
    with owner(), pytest.raises(PolicyError, match='message_review_window_invalid'):
        worker.start(AutomaticReviewRequest(after=1,before=40*86400),now=40*86400)


def test_recipient_cannot_read_job_status(legacy):
    resolver,reviews,_,_ = setup(legacy)
    worker = AutomaticReviewWorker(resolver,reviews)
    with owner(actor='recipient'), pytest.raises(PolicyError):
        worker.status()
