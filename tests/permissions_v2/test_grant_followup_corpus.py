"""Keep fresh follow-up measurements reproducible and grouped correctly."""
import hashlib
import importlib.util
import json
from pathlib import Path

from topos.permissions_v2.knowledge_contract import KnowledgeSearchResult


def corpus():
    path = Path(__file__).resolve().parents[2] / "scripts/build_grant_followup_corpus.py"
    spec = importlib.util.spec_from_file_location("grant_followup_corpus", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


def test_followup_corpus_is_frozen_and_heldout_groups_are_disjoint():
    data = corpus()
    encoded = (json.dumps(data, indent=2, ensure_ascii=False)+"\n").encode()
    assert hashlib.sha256(encoded).hexdigest() == "2856e836be1d3b60eb39d6c41557b4506107ba0cd476435054c89da3d7a8355c"
    rows = data["answers"]
    assert len(rows) == len({row["id"] for row in rows}) == 144
    dev = {row["group"] for row in rows if row["split"] == "development"}
    held = {row["group"] for row in rows if row["split"] == "heldout"}
    assert len(dev) == 24 and len(held) == 12 and dev.isdisjoint(held)


def test_followup_records_and_domains_obey_the_wire_and_prompt_shapes():
    for case in corpus()["answers"]:
        result = KnowledgeSearchResult.parse(dict(family="canonical_record", operation="search",
            view_id="canonical.knowledge_search.v1", records=case["records"]))
        assert 1 <= len(result.records) <= 8
        assert bool(case["oracle"]["required_any"]) == case["answerable"]
        if "domains" in case:
            assert len(case["domains"]) == len(result.records)
