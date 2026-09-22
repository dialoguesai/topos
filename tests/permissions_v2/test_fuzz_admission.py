"""Fuzz lane, part 3: admission (the signed envelope and the node ledger's verify/claim).

A1  Any single-field change to a signed envelope -- a hash, an identifier, a time, a
    capability literal, the key id, the signature itself -- is refused by `ledger.admit`
    with a PolicyError, and the unchanged envelope is admitted (the control that keeps the
    property from passing vacuously).
A2  The request context and the payload are bound: a payload the envelope did not hash is
    refused, and a request context naming another actor, client, grant or assignment is
    refused before any row is written.
A3  Admission is one-shot: the same envelope admitted twice is a replay, whatever the order
    of verify and claim; a refused admission burns the id exactly once.
A4  The envelope's lifetime is bounded by the policy's validity and by the TTL: a `now`
    before issue or at or after expiry refuses.
"""
from __future__ import annotations

import itertools

import pytest

pytest.importorskip("hypothesis")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from hypothesis import HealthCheck, assume, given, settings, strategies as st  # noqa: E402

from tests.permissions_v2 import fuzz_support as fz  # noqa: E402
from tests.permissions_v2.test_contract_and_ledger import sample_policy  # noqa: E402
from tests.permissions_v2.test_evidence import owner  # noqa: E402
from topos.permissions_v2.canonical import PolicyError  # noqa: E402
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger  # noqa: E402
from topos.permissions_v2.signing import EnvelopeBody, RequestContext, SignedEnvelope, request_digest, sign_envelope  # noqa: E402

pytestmark = [pytest.mark.fuzz]
PURE = settings(max_examples=fz.examples("pure"), suppress_health_check=[HealthCheck.function_scoped_fixture])
_counter = itertools.count()


class Fixture:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.policy = sample_policy()
        self.key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        self.keys = {"beta-key-1": self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
        identity = NodeIdentity.parse({k: v for k, v in self.policy["binding"].items() if k in NodeIdentity.model_fields})
        self.ledger = PolicyLedger(root / "policy-v2.db", identity=identity, protection_revision="a" * 64,
                                   trusted_keys=self.keys)
        with owner():
            self.ledger.activate(self.policy, grant_generation=1, assignment_generation=1, expected_epoch=0,
                                 command_id="activate-1", now=1100)
            self.authority = self.ledger.authority_snapshot("grant-1", now=1100)

    def request(self, request_id="request-1"):
        return RequestContext.parse({**self.policy["binding"], "request_id": request_id,
                                     "request_type": "permissions.v2.preview"})

    def envelope(self, request, payload, *, issued_at=1100, expires_at=1200, kid="beta-key-1"):
        body = {**self.authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": kid,
                "request_id": request.request_id, "request_type": request.request_type,
                "request_hash": request_digest(request.request_type, payload), "issued_at": issued_at,
                "expires_at": expires_at}
        return sign_envelope(EnvelopeBody.parse(body), self.key).model_dump()


def fresh(tmp_path):
    return Fixture(tmp_path / f"f{next(_counter)}")


PAYLOADS = st.one_of(st.just({"question": "synthetic", "limit": 10}),
                     st.dictionaries(st.sampled_from(["q", "limit", "note"]), st.one_of(st.integers(0, 50), st.text(max_size=8)), max_size=3))
FIELDS = sorted(SignedEnvelope.model_fields)


def replacement(field, original, draw):
    if field == "signature":
        return draw(st.from_regex(r"[A-Za-z0-9_-]{86}", fullmatch=True))
    if field in ("policy_hash", "protection_revision", "request_hash"):
        return draw(fz.hex64)
    if field in ("grant_generation", "assignment_generation", "node_epoch", "issued_at", "expires_at"):
        return draw(st.integers(1, 10_000))
    if field == "capability_version":
        return draw(st.sampled_from(["permissions-beta/p2a-v2", "permissions-beta/p2b-v1", "permissions-beta/p2c-v1"]))
    if field == "request_type":
        return draw(st.sampled_from(["permissions.v2.read", "permissions.v2.fact.read", "permissions.v2.search"]))
    if field == "version":
        return "topos-grantee-envelope/v3"
    return draw(fz.identifiers)


@PURE
@given(PAYLOADS, st.sampled_from(FIELDS), st.data())
def test_A1_any_single_field_change_is_refused_and_the_original_admits(tmp_path, payload, field, data):
    fixture = fresh(tmp_path)
    request = fixture.request()
    envelope = fixture.envelope(request, payload)
    tampered = dict(envelope)
    tampered[field] = replacement(field, envelope[field], data.draw)
    assume(tampered[field] != envelope[field])
    with pytest.raises(PolicyError):
        fixture.ledger.admit(tampered, request=request, payload=payload, now=1101)
    # Nothing was claimed by the refusal at parse or verify: the original still admits once.
    lease = fixture.ledger.admit(envelope, request=request, payload=payload, now=1101)
    assert lease.request_id == "request-1"


@PURE
@given(PAYLOADS, PAYLOADS)
def test_A2_a_payload_the_envelope_did_not_hash_is_refused(tmp_path, payload, other):
    assume(payload != other)
    fixture = fresh(tmp_path)
    request = fixture.request()
    envelope = fixture.envelope(request, payload)
    with pytest.raises(PolicyError) as refused:
        fixture.ledger.admit(envelope, request=request, payload=other, now=1101)
    assert refused.value.code == "request_hash"


@PURE
@given(PAYLOADS, st.sampled_from(["actor_id", "client_id", "grant_id", "assignment_id", "request_id"]), fz.identifiers)
def test_A2_a_request_context_naming_anyone_else_is_refused(tmp_path, payload, field, value):
    fixture = fresh(tmp_path)
    request = fixture.request()
    envelope = fixture.envelope(request, payload)
    assume(value != getattr(request, field))
    other = RequestContext.parse({**request.model_dump(), field: value})
    with pytest.raises(PolicyError):
        fixture.ledger.admit(envelope, request=other, payload=payload, now=1101)


@PURE
@given(PAYLOADS, st.booleans())
def test_A3_admission_is_one_shot_whatever_the_order(tmp_path, payload, verify_first):
    fixture = fresh(tmp_path)
    request = fixture.request()
    envelope = fixture.envelope(request, payload)
    if verify_first:
        admission = fixture.ledger.verify(envelope, request=request, payload=payload, now=1101)
        fixture.ledger.admit_verified(admission, now=1101)
    else:
        fixture.ledger.admit(envelope, request=request, payload=payload, now=1101)
    with pytest.raises(PolicyError) as replay:
        fixture.ledger.admit(envelope, request=request, payload=payload, now=1101)
    assert replay.value.code == "request_replay"
    with pytest.raises(PolicyError) as again:
        fixture.ledger.verify(envelope, request=request, payload=payload, now=1101)
    assert again.value.code == "request_replay"


@PURE
@given(PAYLOADS)
def test_A3_a_refused_admission_burns_the_id_exactly_once(tmp_path, payload):
    fixture = fresh(tmp_path)
    request = fixture.request()
    envelope = fixture.envelope(request, payload)
    admission = fixture.ledger.verify(envelope, request=request, payload=payload, now=1101)
    assert fixture.ledger.refuse(admission, now=1101) is None
    assert fixture.ledger.refuse(admission, now=1101) is None  # a no-op the second time, not a second row
    with pytest.raises(PolicyError) as replay:
        fixture.ledger.admit(envelope, request=request, payload=payload, now=1101)
    assert replay.value.code == "request_replay"


@PURE
@given(PAYLOADS, st.integers(0, 6000))
def test_A4_the_lifetime_is_bounded_by_issue_expiry_and_policy_validity(tmp_path, payload, now):
    fixture = fresh(tmp_path)
    request = fixture.request()
    envelope = fixture.envelope(request, payload, issued_at=1100, expires_at=1200)
    if 1100 <= now < 1200:
        fixture.ledger.admit(envelope, request=request, payload=payload, now=now)
    else:
        with pytest.raises(PolicyError):
            fixture.ledger.admit(envelope, request=request, payload=payload, now=now)


@PURE
@given(PAYLOADS, st.integers(0, 6000), st.integers(1, 200))
def test_A4_an_envelope_outside_the_policy_validity_or_ttl_is_refused_at_issue(tmp_path, payload, issued_at, ttl):
    fixture = fresh(tmp_path)
    request = fixture.request()
    expires_at = issued_at + ttl
    envelope = fixture.envelope(request, payload, issued_at=issued_at, expires_at=expires_at)
    inside = 1000 <= issued_at and expires_at <= 5000 and ttl <= 120
    now = issued_at
    if inside:
        fixture.ledger.admit(envelope, request=request, payload=payload, now=now)
    else:
        with pytest.raises(PolicyError):
            fixture.ledger.admit(envelope, request=request, payload=payload, now=now)
