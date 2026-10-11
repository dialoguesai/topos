"""The checked-in answer wire is generated from the actual models and keys derived from public labels."""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.answer_wire_fixtures import MODELS, ROOT, vector, vector_v2
from topos.permissions_v2.answer_protocol import verify_node_answer
from topos.permissions_v2.canonical import PolicyError, canonical_bytes, digest
from topos.permissions_v2.knowledge_contract import KnowledgePolicy
from topos.permissions_v2.signing import parse_envelope


def test_answer_schemas_are_exact_exports():
    for model in MODELS:
        expected = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        assert (ROOT / f"{model.__name__}.schema.json").read_text() == expected


def test_answer_golden_signatures_and_policy_bytes():
    saved = json.loads((ROOT / "answer-golden-v1.json").read_text())
    assert saved == vector()
    for mode, entry in saved["policies"].items():
        policy = KnowledgePolicy.parse(entry["policy"])
        assert policy.search.answers == mode
        assert canonical_bytes(policy.model_dump()).decode("ascii") == entry["canonical"]
        assert digest(policy.model_dump()) == entry["hash"]
    for name, output in saved["bodies"].items():
        envelope = parse_envelope(saved["envelopes"][saved["links"][name]])
        verify_node_answer(saved["results"][name], trusted_keys={
            "answer-node-key": bytes.fromhex(saved["public_keys_hex"]["node"])},
            envelope=envelope, output=output,
            mode="with_sources" if name == "with_sources" else "only", now=saved["now"])


def test_answer_golden_v2_carries_the_writer_at_each_level_and_v1_is_untouched():
    """A2A-4 amendment 9: each level's body verifies only at its level; the v1 vector's bytes did not move."""
    saved = json.loads((ROOT / "answer-golden-v2.json").read_text())
    assert saved == vector_v2()
    keys = {"answer-node-key": bytes.fromhex(saved["public_keys_hex"]["node"])}
    for level, entry in saved["levels"].items():
        for mode in ("only", "with_sources"):
            policy = KnowledgePolicy.parse(entry["policies"][mode]["policy"])
            assert policy.search.writer_disclosure == level
            assert canonical_bytes(policy.model_dump()).decode("ascii") == entry["policies"][mode]["canonical"]
            envelope = parse_envelope(entry["envelopes"][mode])
            verify_node_answer(entry["results"][mode], trusted_keys=keys, envelope=envelope,
                               output=entry["bodies"][mode], mode=mode, now=saved["now"], disclosure=level)
            with pytest.raises(PolicyError, match="answer_output_invalid"):
                verify_node_answer(entry["results"][mode], trusted_keys=keys, envelope=envelope,
                                   output=entry["bodies"][mode], mode=mode, now=saved["now"])
    assert "writer" not in json.dumps(json.loads((ROOT / "answer-golden-v1.json").read_text())["bodies"])
