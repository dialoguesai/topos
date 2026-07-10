"""Guard: the R-series privacy-probe corpus must stay able to validate the judge.

The judge scorecard's gate is rare_leak_recall, computed over the rare-leak positives; the
overall recall over leak positives. If the corpus is trimmed below the runbook's targets
(~20 leaks, ~10 rare) the slice silently loses the power to validate — so pin it here.
"""

from __future__ import annotations

from privacy_probe_corpus import (
    PRIVACY_PROBE_CORPUS,
    PRIVACY_PROBE_CORPUS_VERSION,
    corpus_coverage,
)


def test_corpus_has_enough_leak_and_rare_positives():
    cov = corpus_coverage()
    assert cov["leak_intended"] >= 20, f"need >=20 leak probes, have {cov['leak_intended']}"
    assert cov["rare"] >= 10, f"need >=10 rare-leak probes, have {cov['rare']}"
    assert cov["clean"] >= 8, f"need matched negatives too, have {cov['clean']}"


def test_every_entry_is_well_formed_and_unlabeled():
    seen = set()
    for e in PRIVACY_PROBE_CORPUS:
        assert e["case_id"] and e["case_id"] not in seen, f"dup/empty id: {e['case_id']}"
        seen.add(e["case_id"])
        assert e["answer"] and e["ideal"], f"{e['case_id']} missing answer/ideal"
        assert e["evidence_facts"], f"{e['case_id']} has no evidence facts to judge against"
        # The corpus must never assert truth — the human annotator sets leaked/rare.
        assert "leaked" not in e and "rare" not in e, f"{e['case_id']} pre-labels truth"


def test_rare_probes_cover_the_subtle_taxonomy():
    kinds = {
        e["leak_intent"].split(":", 1)[1]
        for e in PRIVACY_PROBE_CORPUS
        if str(e["leak_intent"]).startswith("rare:")
    }
    # the leak types a weak detector misses (runbook / wiki 10 T4)
    assert {"paraphrase", "mosaic", "inference", "trail", "adjacent", "channel"} <= kinds, kinds


def test_version_is_pinned():
    assert PRIVACY_PROBE_CORPUS_VERSION == "priv-probe-2"


def test_exporter_drops_the_design_note():
    """leak_intent is a coverage aid, not a label — it must not reach the annotator's file."""
    import pytest as _pytest

    _pytest.importorskip("topos_eval", reason="topos-eval (sibling repo) not installed in this env")
    from topos_eval.validity.judge_validation import export_privacy_pool  # noqa: PLC0415
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "p.jsonl"
        export_privacy_pool(PRIVACY_PROBE_CORPUS, out)
        rows = [json.loads(x) for x in out.read_text().splitlines() if x.strip()]
    assert len(rows) == len(PRIVACY_PROBE_CORPUS)
    for r in rows:
        assert "leak_intent" not in r
        assert r["leaked"] is None and r["rare"] is None
