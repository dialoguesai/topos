"""The bind wire module both repositories copy (any-to-any A2A-1 Appendix A), against its vector (Appendix B).

The node signs nothing with the stamp key, so its half of the vector is: ``verify_bind`` accepts the vector bind,
and ``sign_bind_proof`` reproduces the vector proof, byte for byte, for both the first bind and the rebind. The
control plane pins the other half (``sign_bind`` and ``verify_bind_proof``); they are checked here too because the
module is one file, and a one-byte change to it or to canonical JSON must fail on both sides.

Keys are derived from their labels at run time; the fixture holds labels, plain fields, hashes and signatures.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from topos.permissions_v2 import bind_protocol as bp
from topos.permissions_v2.canonical import PolicyError

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((ROOT / "fixtures" / "permissions_v2" / "bind-golden-v1.json").read_text())
MODULE = ROOT / "topos" / "permissions_v2" / "bind_protocol.py"


def derived(name: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(GOLDEN["labels"][name].encode("ascii")).digest())


def public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


STAMP, CP, NODE = derived("relay_stamp"), derived("cp_signing"), derived("node")
EXPECTED = GOLDEN["expected"]


def body(**changes) -> dict:
    raw = dict(GOLDEN["bind"])
    raw["trusted_cp_keys"] = {kid: public(derived(source["public_hex_of"])).hex()
                              for kid, source in raw["trusted_cp_keys"].items()}
    return {**raw, **changes}


def rebind_body() -> dict:
    rebind = GOLDEN["rebind"]
    return body(request_id=rebind["request_id"], node_id=rebind["node_id"], new_key_allowed=rebind["new_key_allowed"],
                nonce=rebind["nonce"])


def proof_for(bind: bp.SignedBind, *, node_id=None, outcome=None, now=None, key=NODE) -> bp.SignedBindProof:
    spec = GOLDEN["proof"]
    return bp.sign_bind_proof(bind=bind, node_id=node_id or spec["node_id"], node_key=key,
                              kid=bp.node_key_id(public(key)), outcome=outcome or spec["outcome"],
                              engine_version=spec["engine_version"], now=spec["now"] if now is None else now)


def test_the_module_is_the_contracts_byte_for_byte():
    assert hashlib.sha256(MODULE.read_bytes()).hexdigest() == GOLDEN["module_sha256"]
    assert MODULE.read_bytes().count(b"\n") == 200 and MODULE.read_bytes().endswith(b"proof\n")


def test_the_node_accepts_the_vector_bind_and_reproduces_the_vector_proof():
    stamp_b64 = base64.b64encode(public(STAMP)).decode("ascii")       # as GET /v1/relay/stamp-public-key gives it
    assert hashlib.sha256(stamp_b64.encode("ascii")).hexdigest() == EXPECTED["stamp_public_b64_sha256"]
    assert bp.node_key_id(public(NODE)) == EXPECTED["node_kid"]
    raw = {**body(), "signature": EXPECTED["bind_signature"]}
    bind = bp.verify_bind(raw, stamp_public_key=public(STAMP), message_id=GOLDEN["bind"]["request_id"],
                          now=GOLDEN["verify_at"])
    signing = bp.signing_bytes(bp.BindBody.parse(body()))
    assert len(signing) == EXPECTED["bind_signing_bytes_length"]
    assert hashlib.sha256(signing).hexdigest() == EXPECTED["bind_signing_bytes_sha256"]
    assert signing.startswith(b"topos-node-bind/v1\n{")
    assert bp.bind_hash(bind) == EXPECTED["bind_hash"]
    proof = proof_for(bind)
    assert proof.signature == EXPECTED["proof_signature"]
    assert proof.kid == EXPECTED["node_kid"] and proof.bind_hash == EXPECTED["bind_hash"]
    proof_body = bp.BindProofBody.parse(proof.model_dump(exclude={"signature"}))
    assert hashlib.sha256(bp.signing_bytes(proof_body)).hexdigest() == EXPECTED["proof_signing_bytes_sha256"]


def test_the_node_accepts_the_vector_rebind_and_answers_it_already_bound_byte_for_byte():
    rebind = GOLDEN["rebind"]
    raw = {**rebind_body(), "signature": EXPECTED["rebind_signature"]}
    bind = bp.verify_bind(raw, stamp_public_key=public(STAMP), message_id=rebind["request_id"],
                          now=GOLDEN["verify_at"])
    assert bp.bind_hash(bind) == EXPECTED["rebind_hash"]
    proof = proof_for(bind, node_id=rebind["node_id"], outcome=rebind["proof_outcome"], now=rebind["proof_now"])
    assert proof.signature == EXPECTED["rebind_proof_signature"]


def test_the_control_plane_half_of_the_vector_holds_against_this_copy():
    """C2 pins these in its own repository; a drift in this copy must fail here first."""
    assert bp.sign_bind(bp.BindBody.parse(body()), STAMP).signature == EXPECTED["bind_signature"]
    assert bp.sign_bind(bp.BindBody.parse(rebind_body()), STAMP).signature == EXPECTED["rebind_signature"]
    bind = bp.SignedBind.parse({**body(), "signature": EXPECTED["bind_signature"]})
    accepted = bp.verify_bind_proof(proof_for(bind).model_dump(), bind=bind, now=GOLDEN["verify_at"])
    assert accepted.node_public_key == public(NODE).hex() and accepted.outcome == "bound"


def _first() -> bp.SignedBind:
    return bp.SignedBind.parse({**body(), "signature": EXPECTED["bind_signature"]})


def _second() -> bp.SignedBind:
    return bp.SignedBind.parse({**rebind_body(), "signature": EXPECTED["rebind_signature"]})


def _verify(raw, *, message_id=None, now=None):
    return bp.verify_bind(raw, stamp_public_key=public(STAMP), message_id=message_id or raw["request_id"],
                          now=GOLDEN["verify_at"] if now is None else now)


def refusal_a_key_made_when_the_bind_did_not_allow_one():
    second = _second()
    proof = proof_for(second, node_id=GOLDEN["rebind"]["node_id"], outcome="bound")
    bp.verify_bind_proof(proof.model_dump(), bind=second, now=GOLDEN["verify_at"])


def refusal_bind_field_changed():
    _verify({**_first().model_dump(), "owner_id": "a2a1-vector-other-owner"})


def refusal_bind_signed_by_another_key():
    raw = body()
    _verify({**raw, "signature": bp.sign_bind(bp.BindBody.parse(raw), CP).signature})


def refusal_bound_proof_whose_key_id_is_not_derived():
    raw = proof_for(_first()).model_dump()
    bp.verify_bind_proof({**raw, "kid": "nk_" + "0" * 32}, bind=_first(), now=GOLDEN["verify_at"])


def refusal_environment_without_the_beta_prefix():
    bp.BindBody.parse(body(environment_id="production-vector"))


def refusal_expired_bind():
    _verify(_first().model_dump(), now=GOLDEN["bind"]["expires_at"])


def refusal_first_bind_without_a_new_key():
    bp.BindBody.parse(body(new_key_allowed=False))


def refusal_proof_for_another_bind():
    bp.verify_bind_proof(proof_for(_first()).model_dump(), bind=_second(), now=GOLDEN["verify_at"])


def refusal_proof_signature_by_another_key():
    proof = proof_for(_first())
    stranger = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"A2A-1 vector: another key").digest())
    forged = bp.sign_bind_proof(bind=_first(), node_id=GOLDEN["proof"]["node_id"], node_key=stranger,
                                kid=bp.node_key_id(public(stranger)), outcome="bound", engine_version="1.5.0",
                                now=GOLDEN["proof"]["now"])
    bp.verify_bind_proof({**proof.model_dump(), "signature": forged.signature}, bind=_first(), now=GOLDEN["verify_at"])


def refusal_two_control_plane_keys():
    raw = body()
    bp.BindBody.parse({**raw, "trusted_cp_keys": {**raw["trusted_cp_keys"], "ck_other": public(NODE).hex()}})


def refusal_wrong_frame_id():
    _verify(_first().model_dump(), message_id="a2a1-vector-other-frame")


REFUSALS = {name[len("refusal_"):].replace("_", " "): case for name, case in globals().items()
            if name.startswith("refusal_")}


def test_every_refusal_of_the_vector_is_covered_once():
    assert sorted(REFUSALS) == sorted(case.replace("-", " ") for case in GOLDEN["refusals"])


@pytest.mark.parametrize("case", sorted(GOLDEN["refusals"]))
def test_each_refusal_of_the_vector_raises_its_code(case):
    with pytest.raises(PolicyError) as raised:
        REFUSALS[case.replace("-", " ")]()
    assert raised.value.code == GOLDEN["refusals"][case]


def test_a_bind_with_no_signature_is_malformed():
    raw = _first().model_dump()
    del raw["signature"]
    with pytest.raises(PolicyError) as raised:
        _verify(raw)
    assert raised.value.code == "schema_invalid"


def test_a_node_id_is_random_and_never_a_key():
    minted = {bp.mint_node_id() for _ in range(64)}
    assert len(minted) == 64 and all(len(value) == 37 and value.startswith("node_") for value in minted)
    assert bp.node_key_id(public(NODE)) not in minted
