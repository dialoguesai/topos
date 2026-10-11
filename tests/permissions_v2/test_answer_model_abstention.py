"""A completed abstention is distinct from an unavailable or malformed writer."""
import asyncio
from types import SimpleNamespace
import time

import pytest

from topos.permissions_v2.answer_release import _generate_local
from topos.permissions_v2.answer_generation import Prompt
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2 import shadow_labeler_local as local


@pytest.mark.parametrize("content,done,valid", [("", True, True), ("  ", True, True),
                                               (None, True, False), ("", False, False)])
def test_completed_blank_is_abstention_but_missing_or_incomplete_content_is_error(monkeypatch, content, done, valid):
    closed = []
    class Client:
        async def post(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: dict(model=local.MODEL, done=done, message={"content": content}))
        async def aclose(self):
            closed.append(True)
    class Transport:
        client = Client()
        base_url = "http://synthetic.invalid"
        async def verify(self):
            pass
    monkeypatch.setattr(local, "open_transport", lambda **kwargs: Transport())
    prompt = SimpleNamespace(system="s", user="u")
    if valid:
        assert asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110)) == content
    else:
        with pytest.raises(PolicyError, match="model_error"):
            asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
    assert closed == [True]


@pytest.mark.parametrize("verdict,done,model,expected", [
    ('{"answers_question": true, "claims_supported": true}', True, local.MODEL, "answered"),
    ('{"answers_question": false, "claims_supported": true}', True, local.MODEL, "abstained"),
    ('{"answers_question": true, "claims_supported": false}', True, local.MODEL, "abstained"),
    ('{"answers_question": false, "claims_supported": false}', True, local.MODEL, "abstained"),
    ('{"answers_question": "true", "claims_supported": true}', True, local.MODEL, "error"),
    ('{"answers_question": 1, "claims_supported": true}', True, local.MODEL, "error"),
    ('{"answers_question": true, "claims_supported": true, "extra": "ignored"}', True, local.MODEL, "error"),
    ('{"answers_question": true}', True, local.MODEL, "error"),
    ('{"answers_question": false, "answers_question": true, "claims_supported": true}', True, local.MODEL, "error"),
    ('true', True, local.MODEL, "error"),
    ('not-json', True, local.MODEL, "error"),
    ('{"answers_question": true, "claims_supported": true}', False, local.MODEL, "error"),
    ('{"answers_question": true, "claims_supported": true}', True, "wrong-model", "error"),
])
def test_second_pass_can_only_accept_or_withhold_and_malformed_checks_fail_closed(monkeypatch, verdict, done, model, expected):
    # Retain each original grammar mutation while adding the new evidence fields.
    # Literal insertion preserves duplicate-key probes that json.loads would erase.
    if verdict.startswith("{") and verdict.endswith("}"):
        verdict = verdict[:-1] + ', "requested_information": "the ferry delay", "evidence": [{"item": 1, "quote": "The ferry was delayed by a tide."}]}'
    requests, closed, verified = [], [], []
    raw = "The ferry was delayed by a tide [1]."
    class Client:
        async def post(self, url, **kwargs):
            requests.append(kwargs)
            body = dict(model=local.MODEL, done=True, message=dict(content=raw)) if len(requests) == 1 else dict(
                model=model, done=done, message=dict(content=verdict))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
        async def aclose(self):
            closed.append(True)
    class Transport:
        client = Client()
        base_url = "http://synthetic.invalid"
        async def verify(self):
            verified.append(True)
    monkeypatch.setattr(local, "open_transport", lambda **kwargs: Transport())
    prompt = Prompt(system="writer", question="What happened with the ferry?",
                    raw_texts=("The ferry was delayed by a tide.",), record_texts=("The ferry was delayed by a tide.",), user="Question: What happened with the ferry?\nPermitted items:\n[1] message\nThe ferry was delayed by a tide.",
                             item_texts=("[1] message\nThe ferry was delayed by a tide.",))
    if expected == "error":
        with pytest.raises(PolicyError, match="model_error"):
            asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
    else:
        result = asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
        assert result == (raw if expected == "answered" else "")
    assert len(requests) == 2 and verified == [True] and closed == [True]
    check = requests[1]["json"]
    assert check["format"] == "json" and check["options"]["temperature"] == 0
    assert check["messages"][1]["content"] == prompt.user + "\n\nProposed reply (quoted data):\n" + raw


@pytest.mark.parametrize("already_expired", [False, True])
def test_generation_and_verification_share_one_deadline(monkeypatch, already_expired):
    requests, closed = [], []
    clock = [201 if already_expired else 100]
    monkeypatch.setattr("topos.permissions_v2.answer_release.time.time", lambda: clock[0])
    class Client:
        async def post(self, url, **kwargs):
            requests.append(kwargs)
            clock[0] = 201
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: dict(
                model=local.MODEL, done=True, message=dict(content="A supported proposal [1].")))
        async def aclose(self):
            closed.append(True)
    class Transport:
        client = Client()
        base_url = "http://synthetic.invalid"
        async def verify(self):
            pass
    monkeypatch.setattr(local, "open_transport", lambda **kwargs: Transport())
    with pytest.raises(PolicyError, match="model_error"):
        asyncio.run(_generate_local(SimpleNamespace(system="s", user="u"), deadline=200))
    assert len(requests) == (0 if already_expired else 1) and closed == [True]
