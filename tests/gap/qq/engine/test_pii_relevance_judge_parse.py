"""Parse / locality guards for OllamaPIIRelevanceJudge (no live Ollama required)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Sibling topos-eval + adapter package on path (same layout as QQ runner).
_ROOT = Path(__file__).resolve().parents[5]  # workspace root (…/topos-control-plane)
_EVAL_SRC = _ROOT / "topos-eval" / "src"
_ADAPTER = Path(__file__).resolve().parent / "adapter"
for p in (_EVAL_SRC, _ADAPTER):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

pytest.importorskip("topos_eval", reason="topos-eval (sibling repo) not on PYTHONPATH")

from adapter.pii_relevance_judge import OllamaPIIRelevanceJudge  # noqa: E402
from topos_eval.protocols.judge import NonLocalJudgeError  # noqa: E402


def test_rejects_non_local_endpoint() -> None:
    with pytest.raises(NonLocalJudgeError):
        OllamaPIIRelevanceJudge(endpoint="https://api.openai.com")


def test_parse_labels_json() -> None:
    spans = [
        {"span_id": "a", "text": "Ada", "type": "PERSON"},
        {"span_id": "b", "text": "555", "type": "PHONE"},
    ]
    raw = '{"labels":[{"span_id":"a","label":"query_related","confidence":0.9},'
    raw += '{"span_id":"b","label":"must_mask","confidence":0.8}]}'
    parsed = OllamaPIIRelevanceJudge._parse(raw, spans)
    assert parsed[0]["label"] == "query_related"
    assert parsed[1]["label"] == "must_mask"


def test_parse_defaults_missing_to_must_mask() -> None:
    spans = [{"span_id": "x", "text": "x", "type": "EMAIL"}]
    parsed = OllamaPIIRelevanceJudge._parse('{"labels":[]}', spans)
    assert parsed[0]["label"] == "must_mask"
