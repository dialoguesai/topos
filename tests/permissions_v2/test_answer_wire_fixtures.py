"""The checked-in answer wire is generated from the actual models and keys derived from public labels."""
from __future__ import annotations

import json

from tests.permissions_v2.answer_wire_fixtures import MODELS, ROOT, vector
from topos.permissions_v2.answer_protocol import verify_node_answer
from topos.permissions_v2.canonical import canonical_bytes, digest
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
