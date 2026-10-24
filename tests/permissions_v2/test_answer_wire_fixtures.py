"""The checked-in answer wire is generated from the actual models and keys derived from public labels."""
from __future__ import annotations

import base64
import hashlib
import json
import re

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.permissions_v2.answer_wire_fixtures import (MODELS, ROOT, kept_reading, kept_stamp_vector, vector, vector_v2,
    vector_v3, vector_v4)
from topos.permissions_v2 import kept_stamp
from topos.permissions_v2.answer_protocol import (ANSWER_PROTOCOL_VERSION, K_ANSWER, K_ANSWER_LEGACY, K_ANSWER_V4,
    LEGACY_ACCEPTED_VERSION, MAX_ANSWER_BYTES_V4, MAX_ANSWER_CHARS, MAX_ANSWER_CHARS_V4,
    MAX_ANSWER_WITH_SOURCES_BYTES_V4, verify_node_answer)
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


def test_answer_golden_v3_carries_sixteen_records_and_a_seventeenth_is_refused():
    """A2A-4 amendment 13 (LA164): the with-sources cap is sixteen; the v1 and v2 vectors' bytes did not move."""
    saved = json.loads((ROOT / "answer-golden-v3.json").read_text())
    assert saved == vector_v3()
    assert (saved["records_cap"], saved["records_cap_legacy"], saved["protocol_version"]) == (K_ANSWER, K_ANSWER_LEGACY, 3)
    assert len(saved["body"]["records"]) == K_ANSWER == 16
    keys = {"answer-node-key": bytes.fromhex(saved["public_keys_hex"]["node"])}
    envelope = parse_envelope(saved["envelope"])
    policy = KnowledgePolicy.parse(saved["policy"]["policy"])
    assert policy.search.answers == "with_sources" and digest(policy.model_dump()) == saved["policy"]["hash"]
    verify_node_answer(saved["result"], trusted_keys=keys, envelope=envelope, output=saved["body"],
                       mode="with_sources", now=saved["now"])
    seventeenth = {**saved["body"]["records"][0], "record_id": "r." + "0" * 64}
    with pytest.raises(PolicyError, match="answer_output_invalid"):
        verify_node_answer(saved["result"], trusted_keys=keys, envelope=envelope, mode="with_sources", now=saved["now"],
                           output={**saved["body"], "answer": saved["body"]["answer"] + "\nOne more step [17].",
                                   "records": [*saved["body"]["records"], seventeenth]})
    for name in ("answer-golden-v1.json", "answer-golden-v2.json"):
        assert "sixteen" not in (ROOT / name).read_text()


def test_answer_golden_v4_carries_thirty_records_and_a_long_answer_to_a_v4_asker_only():
    """Answer protocol 4 (15 Oct 2026): thirty records and an answer longer than 8,000 characters verify for an asker
    that accepts 4; the same bodies are refused to one that accepts 3, to one that says nothing, and to a caller that
    names no version (3's reading). Past the grammar's own caps (a thirty-first record, a 32,001st character) a body
    is refused even at 4. The v1, v2 and v3 vectors' bytes did not move."""
    saved = json.loads((ROOT / "answer-golden-v4.json").read_text())
    assert saved == vector_v4()
    assert (saved["protocol_version"], saved["records_cap"], saved["records_cap_v3"], saved["records_cap_legacy"],
            saved["chars_cap"], saved["chars_cap_v3"]) == (ANSWER_PROTOCOL_VERSION, K_ANSWER_V4, K_ANSWER,
                                                           K_ANSWER_LEGACY, MAX_ANSWER_CHARS_V4, MAX_ANSWER_CHARS)
    assert (saved["protocol_version"], saved["records_cap"], saved["chars_cap"]) == (4, 30, 32_000)
    assert saved["bytes_cap"] == {"only": MAX_ANSWER_BYTES_V4, "with_sources": MAX_ANSWER_WITH_SOURCES_BYTES_V4}
    assert len(saved["bodies"]["with_sources"]["records"]) == K_ANSWER_V4
    keys = {"answer-node-key": bytes.fromhex(saved["public_keys_hex"]["node"])}
    for mode in ("only", "with_sources"):
        policy = KnowledgePolicy.parse(saved["policies"][mode]["policy"])
        assert policy.search.answers == mode and digest(policy.model_dump()) == saved["policies"][mode]["hash"]
        envelope = parse_envelope(saved["envelopes"][mode])
        body, result = saved["bodies"][mode], saved["results"][mode]
        assert MAX_ANSWER_CHARS < len(body["answer"]) <= MAX_ANSWER_CHARS_V4
        verify_node_answer(result, trusted_keys=keys, envelope=envelope, output=body, mode=mode, now=saved["now"],
                           accepted_version=4)
        verify_node_answer(result, trusted_keys=keys, envelope=envelope, output=body, mode=mode, now=saved["now"],
                           accepted_version=99)
        for older in (3, LEGACY_ACCEPTED_VERSION, None):
            with pytest.raises(PolicyError, match="answer_output_invalid"):
                verify_node_answer(result, trusted_keys=keys, envelope=envelope, output=body, mode=mode,
                                   now=saved["now"], **({"accepted_version": older} if older is not None else {}))
        longest = {**body, "answer": body["answer"] + "x" * (MAX_ANSWER_CHARS_V4 + 1 - len(body["answer"]))}
        with pytest.raises(PolicyError, match="answer_output_invalid"):
            verify_node_answer(result, trusted_keys=keys, envelope=envelope, output=longest, mode=mode,
                               now=saved["now"], accepted_version=4)
    body = saved["bodies"]["with_sources"]
    extra = {**body["records"][0], "record_id": "r." + "e" * 64}
    with pytest.raises(PolicyError, match="answer_output_invalid"):
        verify_node_answer(saved["results"]["with_sources"], trusted_keys=keys, mode="with_sources", now=saved["now"],
                           envelope=parse_envelope(saved["envelopes"]["with_sources"]), accepted_version=4,
                           output={**body, "answer": body["answer"] + "\nOne more step [31].",
                                   "records": [*body["records"], extra]})
    for name in ("answer-golden-v1.json", "answer-golden-v2.json", "answer-golden-v3.json"):
        assert "thirty" not in (ROOT / name).read_text() and "answer-golden-v4" not in (ROOT / name).read_text()


def test_kept_stamp_golden_vectors_hold_and_the_answer_vectors_did_not_move():
    """Lane OFF1N: the control plane's `kept_stamp`, signed with a key derived from a public label, is kept exactly
    where the vector says, and every other case is refused with the code it states; each signature really is the
    stated key's over the stated text, so a refusal is the binding's, the grammar's or the window's, never a broken
    vector. The answer vectors v1 to v4 did not move (their own tests above), and none of them carries the field."""
    saved = json.loads((ROOT / "kept-stamp-golden-v1.json").read_text())
    assert saved == kept_stamp_vector()
    assert (saved["field"], saved["domain"], saved["message_type"], saved["skew_seconds"]) == (
        kept_stamp.FIELD, kept_stamp.DOMAIN.decode("ascii"), kept_stamp.MESSAGE_TYPE, kept_stamp.SKEW_SECONDS)
    assert saved["capability"] == {"permissions_v2_kept_answers": kept_stamp.CAPABILITY_VERSION}
    keys = {name: Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode("ascii")).digest())
            for name, label in saved["key_labels"].items()}
    public = base64.b64decode(saved["public_key_b64"], validate=True)
    assert keys["cp"].public_key().public_bytes_raw() == public
    assert {"kept", "refused"} == {next(iter(case["expect"])) for case in saved["cases"].values()}
    for name, case in saved["cases"].items():
        assert kept_reading(case, public, saved["now"]) == case["expect"], name
        signed = case["signed"]
        keys[signed["by"]].public_key().verify(base64.urlsafe_b64decode(case["stamp"]["sig"] + "=="),
                                                signed["text"].encode("ascii"))
        if "kept" in case["expect"]:
            stamp, message = case["stamp"], case["message"]
            assert case["expect"]["kept"] == {"keep_until": stamp["keep_until"], "kq": stamp["kq"]}
            assert signed["text"].encode("ascii") == kept_stamp.signing_bytes(
                stamp["keep_until"], stamp["kq"], msg_id=message["id"], msg_type=message["type"])
            assert stamp == kept_stamp.sign_stamp(stamp["keep_until"], stamp["kq"], msg_id=message["id"],
                                                  msg_type=message["type"], signer=keys["cp"])
    assert {"valid", "wrong_type", "wrong_message_id", "expired", "bad_signature_other_key", "kq_upper_case",
            "kq_short", "kq_changed_after_signing"} <= set(saved["cases"])
    text = (ROOT / "kept-stamp-golden-v1.json").read_text()
    assert not re.search(r"\d{10}", text)                  # no long run of digits anywhere in the file
    for name in ("answer-golden-v1.json", "answer-golden-v2.json", "answer-golden-v3.json", "answer-golden-v4.json"):
        assert kept_stamp.FIELD not in (ROOT / name).read_text()
