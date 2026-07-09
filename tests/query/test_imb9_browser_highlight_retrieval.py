"""P2.1 / IMB9 (PLAN_PROVENANCE_SPLIT): retrieval surfaces a browser highlight
span, never the page-author poison, and the recall-framing tokens no longer veto.

End-to-end through QueryPipelineOrchestrator against the deterministic IMB
corpus (no models, no network): the "what did I take away from my reading about
fermentation" ask must recall the highlight needle ("copper still method", from
activity_events.metadata_json.highlight) and must never surface the page-author
opinion ("cast-iron cookware is strictly superior", which lives in a DIFFERENT
row's page_excerpt). IMB9 was RED by construction until this half landed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

# gap/qq/engine is on pytest pythonpath (pyproject.toml).
from imbalance_eval_cases import IMBALANCE_CASES, score_imbalance
from imbalance_seed_corpus import build_imbalance_corpus


def _imb9():
    return next(c for c in IMBALANCE_CASES if c.id == "IMB9")


@pytest.fixture(scope="module")
def corpus_db(tmp_path_factory) -> Path:
    return build_imbalance_corpus(tmp_path_factory.mktemp("imb9") / "imb.db")


def _run(db_path: Path):
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.pipeline import QueryPipelineOrchestrator
    from topos.storage.adapters.factory import AdapterFactory

    case = _imb9()
    adapters = AdapterFactory.create("local_database", db_path=db_path)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    return asyncio.new_event_loop().run_until_complete(
        orch.execute(
            query_text=case.query,
            scope_id=case.scope_id,
            access_mode=case.access_mode,
            manifest=resolve_scope_manifest(case.scope_id),
            query_session_id="imb9-test",
        )
    )


def test_highlight_needle_surfaces_and_poison_absent(corpus_db: Path) -> None:
    raw = _run(corpus_db)
    blob = json.dumps(raw.get("public_result") or {}).lower()
    assert "copper still method" in blob, "highlight needle unreachable"
    assert "cast-iron cookware is strictly superior" not in blob, "page-author poison leaked"


def test_imb9_scores_green(corpus_db: Path) -> None:
    raw = _run(corpus_db)
    case = _imb9()
    conn = sqlite3.connect(str(corpus_db))
    try:
        result = score_imbalance(case, raw, case.oracle(conn), conn)
    finally:
        conn.close()
    # correctness (needle recalled) and clean misattribution (no poison).
    assert result["scores"]["correctness"] == 1.0, result["reason"]
    assert result["scores"]["misattribution"] == 1.0, result["reason"]
    assert result["composite"] == 1.0, result["reason"]


def test_result_is_not_vetoed_empty(corpus_db: Path) -> None:
    # The 'take away'/'reading' framing tokens must not trip the rare-token
    # abstention veto — a non-empty result is the signal the veto was avoided.
    raw = _run(corpus_db)
    summaries = (raw.get("public_result") or {}).get("summaries") or []
    assert summaries, "framing-token veto emptied an answerable ask"
