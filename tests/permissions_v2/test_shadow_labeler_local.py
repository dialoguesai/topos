"""The pinned local model and rubric the sharing review and answer paths ask (C6, kept through 1.5.0).

1.5.0 removed the shadow audit and, with it, the second labeler that scored a release against its policy. What is
left in `shadow_labeler_local` still ships and is what this file pins: the rubric by its bytes, the model by its
digest, the closed label vocabulary, and the transport's digest check. `test_shadow_labeler_reasons.py` covers the
transport's request and every way it can fail.

Every text in this file is synthetic, written here. No test in it opens a socket.

  L1  the rubric and the model binding are pinned by bytes and by digest, not by prose
  L2  the vocabulary is closed and nothing is coerced: an answer outside it is no labels at all
  L4  the installed model must be the reviewed revision
"""
from __future__ import annotations

import hashlib

import pytest

from topos.permissions_v2 import shadow_labeler_local as local

pytestmark = [pytest.mark.p0]


# --------------------------------------------------------------------------- L1


def test_L1_the_rubric_is_pinned_by_bytes_and_carries_the_closed_vocabulary():
    raw = local.RUBRIC_PATH.read_bytes()
    assert len(raw) == local.RUBRIC_BYTES == 1702
    assert hashlib.sha256(raw).hexdigest() == local.RUBRIC_SHA256
    # The same bytes the gold set and the M1 machine-label track pin, so three readers cannot drift into scoring
    # different things while all claiming "the rubric".
    assert local.RUBRIC_SHA256 == "d03b3358357cc44f85976a5eb3e80840158b701fc4e30d0e4ec5a819c959701d"
    text = local.rubric()
    for value in local.DOMAINS + local.SENSITIVITIES:
        assert "`%s`" % value in text, value
    assert local.system_prompt().endswith(text) and "JSON only" in local.system_prompt()


def test_L1b_the_model_binding_is_the_campaigns_own():
    assert local.MODEL == "qwen3.5:9b-mlx"
    assert local.MODEL_REVISION == "203e30078279db51132b9e026ceb7bb21330e5b1af67ef190671b375c9770404"
    assert local.ORIGIN == "http://127.0.0.1:11434"


def test_L1c_a_rubric_that_is_not_the_reviewed_one_refuses_rather_than_labels(monkeypatch, tmp_path):
    other = tmp_path / "rubric.md"
    other.write_text("## A rubric somebody edited\n")
    monkeypatch.setattr(local, "RUBRIC_PATH", other)
    with pytest.raises(local.RubricMismatch):
        local.rubric()
    # No prompt is built from it either: the automatic review and the transport both ask through these.
    with pytest.raises(local.RubricMismatch):
        local.system_prompt()
    # A rubric that is not there at all is the same refusal, not a crash.
    monkeypatch.setattr(local, "RUBRIC_PATH", tmp_path / "missing.md")
    with pytest.raises(local.RubricMismatch):
        local.rubric()
    assert issubclass(local.RubricMismatch, ValueError) and local.RubricMismatch.reason == "labeler_rubric_mismatch"


# --------------------------------------------------------------------------- L2


@pytest.mark.parametrize("answer", [
    '{"domains": ["work"], "sensitivity": "none"}',
])
def test_L2_a_well_formed_answer_in_the_vocabulary_parses(answer):
    assert local.parse_labels(answer) == {"domains": ["work"], "sensitivity": "none"}


@pytest.mark.parametrize("answer", [
    '{"domains": ["work", "banking"], "sensitivity": "none"}',      # a domain the rubric does not define
    '{"domains": ["work"], "sensitivity": "sensitive"}',            # a sensitivity it does not define
    '{"domains": ["work"], "sensitivity": ["none"]}',               # the right value, the wrong shape
    '{"domains": "work", "sensitivity": "none"}',
    '{"domains": ["work", "work"], "sensitivity": "none"}',         # a duplicate is not a label set
    '{"domains": ["work"]}',                                        # a missing field
    '{"domains": ["work"], "sensitivity": "none", "why": "it is about the job"}',   # a rationale
    '{"domains": ["Work"], "sensitivity": "none"}',                 # case is not coerced
    "I would say this one is about work.",
    "", "null", "[]", "{}",
])
def test_L2b_an_answer_outside_the_vocabulary_is_refused_and_never_coerced(answer):
    assert local.parse_labels(answer) is None


def test_L2c_bytes_and_an_already_parsed_answer_are_read_the_same_way():
    assert local.parse_labels(b'{"domains": ["work"], "sensitivity": "none"}') == {
        "domains": ["work"], "sensitivity": "none"}
    assert local.parse_labels({"domains": ["health"], "sensitivity": "special"}) == {
        "domains": ["health"], "sensitivity": "special"}
    assert local.parse_labels(None) is None and local.parse_labels(b"not json") is None


# --------------------------------------------------------------------------- L4


def test_L4c_the_installed_model_must_be_the_reviewed_revision():
    """A re-score by a model nobody reviewed is not a second opinion, and must not become one by a newer pull."""
    import asyncio

    class _Tags:
        def __init__(self, digest):
            self.digest = digest

        def raise_for_status(self):
            return None

        def json(self):
            return {"models": [{"name": local.MODEL, "digest": self.digest}]}

    class _Client:
        def __init__(self, digest):
            self.digest = digest

        async def get(self, url, **kwargs):
            return _Tags(self.digest)

    asyncio.run(local._PinnedTransport(_Client(local.MODEL_REVISION)).verify())
    with pytest.raises(ValueError):
        asyncio.run(local._PinnedTransport(_Client("0" * 64)).verify())
