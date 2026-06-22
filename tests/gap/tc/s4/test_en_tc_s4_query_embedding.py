"""Tests for query embedding hook and supplemental terms."""

from __future__ import annotations

from unittest.mock import patch

from topos.features.signal.topic_clustering import (
    embed_query_text_for_ranking,
    supplemental_label_terms,
)


def test_supplemental_label_terms_excludes_label_tokens() -> None:
    cluster = {"label": "git / commit / signal", "label_terms": ["git", "commit", "uma", "topos"]}
    assert supplemental_label_terms(cluster) == ["uma", "topos"]


def test_embed_query_text_for_ranking_returns_none_on_failure() -> None:
    with patch("topos.engine.backends.huggingface.HuggingFaceAdapter", side_effect=RuntimeError("offline")):
        assert embed_query_text_for_ranking("git commit") is None


def test_embed_query_text_for_ranking_normalizes_vector() -> None:
    class FakeHF:
        def run_inference(self, _payload, _opts):
            return {"vectors": [[3.0, 4.0]]}

    with patch("topos.engine.backends.huggingface.HuggingFaceAdapter", return_value=FakeHF()):
        vec = embed_query_text_for_ranking("git")
    assert vec is not None
    assert abs(sum(x * x for x in vec) - 1.0) < 0.01
