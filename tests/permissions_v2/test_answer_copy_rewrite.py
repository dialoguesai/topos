"""Recover paraphrases in only mode without relaxing any release check."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from tests.permissions_v2.test_answer_generation import _record, Boundary
from topos.permissions_v2.answer_generation import build_prompt, post_check_answer
from topos.permissions_v2.answer_release import _generate_local
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2 import shadow_labeler_local as local


SOURCE = "I postponed the navigation exercise because the compass was missing."
COPIED = "The owner postponed the navigation exercise because the compass was missing [1]."
REWRITTEN = "A missing compass led the owner to delay navigation practice [1]."


def host(monkeypatch, proposals, *, clock=None, expire_after=None, rewrite_model=local.MODEL, rewrite_done=True):
    requests, closed = [], []
    remaining = iter(proposals)
    class Client:
        async def post(self, url, **kwargs):
            requests.append(kwargs)
            if expire_after == len(requests):
                clock[0] = 201
            text = json.dumps(dict(requested_information="what happened with navigation", answers_question=True,
                claims_supported=True, evidence=[dict(item=1, quote=SOURCE)])) if kwargs["json"].get("format") else next(remaining)
            model = rewrite_model if len(requests) == 2 and not kwargs["json"].get("format") else local.MODEL
            done = rewrite_done if len(requests) == 2 and not kwargs["json"].get("format") else True
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: dict(model=model,
                done=done, message=dict(content=text)))
        async def aclose(self):
            closed.append(True)
    class Transport:
        client = Client()
        base_url = "http://synthetic.invalid"
        async def verify(self):
            pass
    monkeypatch.setattr(local, "open_transport", lambda **kwargs: Transport())
    return requests, closed


def test_copied_proposal_gets_one_rewrite_and_full_verification(monkeypatch):
    records = [_record("a", SOURCE)]
    prompt = build_prompt("What happened with navigation?", records, precision="none", mode="only")
    before = post_check_answer(COPIED, records, prompt, mode="only", boundary=Boundary())
    assert before.body.outcome == "no_answer" and before.dropped_copy == 1
    requests, closed = host(monkeypatch, [COPIED, REWRITTEN])
    raw = asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
    assert raw == REWRITTEN and len(requests) == 3 and closed == [True]
    assert requests[1]["json"]["messages"][1]["content"] == prompt.user + "\n\nCopied proposal (quoted data):\n" + COPIED
    assert requests[2]["json"]["messages"][1]["content"] == prompt.user + "\n\nProposed reply (quoted data):\n" + REWRITTEN
    after = post_check_answer(raw, records, prompt, mode="only", boundary=Boundary())
    assert after.body.outcome == "answered" and after.dropped_copy == 0


@pytest.mark.parametrize("mode,raw", [("with_sources", COPIED), ("only", REWRITTEN)])
def test_no_rewrite_for_source_mode_or_noncopy(monkeypatch, mode, raw):
    prompt = build_prompt("What happened with navigation?", [_record("a", SOURCE)], precision="none", mode=mode)
    requests, closed = host(monkeypatch, [raw])
    assert asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110)) == raw
    assert len(requests) == 2 and closed == [True]
    assert requests[1]["json"].get("format") == "json"


def test_rewrite_is_bounded_even_when_it_copies_again(monkeypatch):
    records = [_record("a", SOURCE)]
    prompt = build_prompt("What happened with navigation?", records, precision="none", mode="only")
    requests, closed = host(monkeypatch, [COPIED, COPIED])
    raw = asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
    assert raw == "" and len(requests) == 2 and closed == [True]
    assert post_check_answer(raw, records, prompt, mode="only", boundary=Boundary()).body.outcome == "no_answer"


def test_rewrite_and_verification_share_the_original_deadline(monkeypatch):
    clock = [100]
    monkeypatch.setattr("topos.permissions_v2.answer_release.time.time", lambda: clock[0])
    prompt = build_prompt("What happened with navigation?", [_record("a", SOURCE)], precision="none", mode="only")
    requests, closed = host(monkeypatch, [COPIED, REWRITTEN], clock=clock, expire_after=2)
    with pytest.raises(PolicyError, match="model_error"):
        asyncio.run(_generate_local(prompt, deadline=200))
    assert len(requests) == 2 and closed == [True]


def test_invalid_citations_never_trigger_rewrite_or_verification(monkeypatch):
    prompt = build_prompt("What happened with navigation?", [_record("a", SOURCE)], precision="none", mode="only")
    requests, closed = host(monkeypatch, [COPIED.replace("[1]", "[9]")])
    assert asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110)) == ""
    assert len(requests) == 1 and closed == [True]


@pytest.mark.parametrize("model,done,content", [("wrong-model", True, REWRITTEN),
    (local.MODEL, False, REWRITTEN), (local.MODEL, True, None)])
def test_rewrite_must_finish_on_the_pinned_model_with_text(monkeypatch, model, done, content):
    prompt = build_prompt("What happened with navigation?", [_record("a", SOURCE)], precision="none", mode="only")
    requests, closed = host(monkeypatch, [COPIED, content], rewrite_model=model, rewrite_done=done)
    with pytest.raises(PolicyError, match="model_error"):
        asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
    assert len(requests) == 2 and closed == [True]


def test_model_approval_of_rewritten_protected_text_does_not_bypass_release_checks(monkeypatch):
    records = [_record("a", SOURCE)]
    prompt = build_prompt("What happened with navigation?", records, precision="none", mode="only")
    unsafe = "Secretperson led the owner to delay navigation practice owing to a missing compass [1]."
    requests, closed = host(monkeypatch, [COPIED, unsafe])
    raw = asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
    assert raw == unsafe and len(requests) == 3 and closed == [True]
    checked = post_check_answer(raw, records, prompt, mode="only", boundary=Boundary())
    assert checked.body.outcome == "no_answer" and checked.reason == "answer_protected"


def test_mixed_proposal_keeps_noncopy_without_an_extra_rewrite(monkeypatch):
    records = [_record("a", SOURCE)]
    prompt = build_prompt("What happened with navigation?", records, precision="none", mode="only")
    requests, closed = host(monkeypatch, [COPIED + "\n" + REWRITTEN])
    raw = asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
    assert raw == REWRITTEN and len(requests) == 2 and closed == [True]
    assert post_check_answer(raw, records, prompt, mode="only", boundary=Boundary()).body.outcome == "answered"
