"""The frozen evaluation must not silently change or mislabel consent scope."""
import hashlib
import importlib.util
import json
from pathlib import Path

from topos.permissions_v2.knowledge_contract import KnowledgeSearchResult


def corpus():
    path = Path(__file__).resolve().parents[2] / "scripts/build_grant_eval_corpus.py"
    spec = importlib.util.spec_from_file_location("grant_eval_corpus", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


def test_frozen_corpus_and_group_disjoint_holdout():
    data = corpus()
    encoded = (json.dumps(data, indent=2, ensure_ascii=False)+"\n").encode()
    assert hashlib.sha256(encoded).hexdigest() == "887072c05b3c642810fd2005586edf7de9476f1102b9d6411ab30b2067460786"
    for family in ("answers", "retrieval"):
        cases = data[family]
        assert len({case["id"] for case in cases}) == len(cases)
        development = {case["group"] for case in cases if case["split"] == "development"}
        heldout = {case["group"] for case in cases if case["split"] == "heldout"}
        assert development and heldout and development.isdisjoint(heldout)


def test_all_expected_hits_are_permitted_and_within_the_effective_window():
    data = corpus()
    for case in data["retrieval"]:
        docs = data["corpora"][case["corpus_id"]]
        for n in case["expected_indices"]:
            assert docs[n]["permitted"] and 0 <= docs[n]["event_us"] <= 2_000_000


def test_writer_cases_validate_against_the_actual_strict_wire_contract():
    for case in corpus()["answers"]:
        result = KnowledgeSearchResult.parse(dict(family="canonical_record", operation="search",
            view_id="canonical.knowledge_search.v1", records=case["records"]))
        assert len(result.records) <= 8
        assert bool(case["oracle"]["required_any"]) == case["answerable"]
