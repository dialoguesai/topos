"""A completed abstention is distinct from an unavailable or malformed writer."""
import asyncio
from types import SimpleNamespace
import time

import pytest

from topos.permissions_v2.answer_release import _generate_local
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
