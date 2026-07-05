"""Retrieval eval scorecard: real local embedding model over the Ada Voss persona.

Run explicitly:
    .venv/bin/python -m pytest -m retrieval_eval tests/evals/retrieval -q

Writes scorecard.json next to this file. If baseline.json exists, fails on
metric regressions beyond tolerance. To (re)pin the baseline:
    cp tests/evals/retrieval/scorecard.json tests/evals/retrieval/baseline.json
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from topos.features.signal.service import SignalService
from topos.storage.adapters.factory import AdapterFactory

from . import harness

pytestmark = pytest.mark.retrieval_eval

_HERE = Path(__file__).parent


@pytest.fixture(scope="module")
def embedder():
    torch = pytest.importorskip("torch")  # noqa: F841
    st = pytest.importorskip("sentence_transformers")
    from topos.engine.backends.huggingface import HuggingFaceAdapter

    adapter = HuggingFaceAdapter()

    def embed(texts):
        out = adapter.run_inference(
            {"texts": texts}, {"subtype": "embedding", "input_role": "passage"}
        )
        vectors = out.get("vectors") or []
        assert len(vectors) == len(texts), out.get("error")
        return vectors

    embed.model_name = None  # resolved after first call
    first = embed(["warmup"])
    assert first and len(first[0]) > 0
    embed.model_name = adapter.run_inference(
        {"texts": ["warmup"]}, {"subtype": "embedding"}
    ).get("model")
    return embed


def test_retrieval_eval_scorecard(tmp_path, embedder):
    conn = harness.build_persona_db(
        str(tmp_path / "persona.db"), embedder, model_name=embedder.model_name
    )
    bundle = AdapterFactory.create("local_database", conn=conn)
    service = SignalService(bundle, conn=conn)

    def search(query: str):
        return (service.search_vectors(query=query, limit=harness.TOP_K) or {}).get(
            "items"
        ) or []

    scorecard = harness.run_eval(conn, search)
    (_HERE / "scorecard.json").write_text(json.dumps(scorecard, indent=2))

    summary = scorecard["summary"]
    print("\n=== retrieval eval summary ===")
    print(json.dumps(summary, indent=2))

    # Absolute floor: the harness itself must retrieve *something* sensible.
    assert summary["cases_total"] >= 12
    assert summary["recall_at_10"] >= 0.5, "retrieval quality below sanity floor"

    problems = harness.compare_to_baseline(scorecard, _HERE / "baseline.json")
    assert not problems, "; ".join(problems)
