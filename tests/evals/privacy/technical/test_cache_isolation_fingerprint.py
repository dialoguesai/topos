"""§B.4/§F.8 — cache-isolation fingerprint.

The retrieval fingerprint must encode the disclosure dimensions (tier, grant identity, field
transforms), so a cache entry can never be shared across grants or tiers — the invariant that
must already hold the day §E widens the cache beyond per-session scope. Within a session the
values are constant, so memory hits are unaffected.
"""

from __future__ import annotations

import pytest

from topos.query.fingerprint import compute_retrieval_fingerprint

pytestmark = [pytest.mark.private]

_BASE = dict(scope_id="messages:read", access_mode="raw", data_health_version="v1", source_ids=["s1"])


def test_different_disclosure_tier_differs():
    a = compute_retrieval_fingerprint(**_BASE, disclosure_tier="owner_raw", grant_id="owner")
    b = compute_retrieval_fingerprint(**_BASE, disclosure_tier="default_disclosure", grant_id="owner")
    assert a != b, "a tier change must invalidate / isolate the cache entry"


def test_different_grant_identity_differs():
    a = compute_retrieval_fingerprint(**_BASE, grant_id="requester-A")
    b = compute_retrieval_fingerprint(**_BASE, grant_id="requester-B")
    assert a != b, "two requesters must never share a cache entry"


def test_different_field_transforms_differ():
    a = compute_retrieval_fingerprint(**_BASE, field_transforms=[{"field": "content", "transform_id": "pii_redaction"}])
    b = compute_retrieval_fingerprint(**_BASE, field_transforms=None)
    assert a != b, "a transform change must invalidate the cache entry"


def test_identical_inputs_are_stable():
    kw = dict(**_BASE, disclosure_tier="default_disclosure", grant_id="req", field_transforms=[{"x": 1}])
    assert compute_retrieval_fingerprint(**kw) == compute_retrieval_fingerprint(**kw)


def test_backward_compatible_defaults():
    # Callers that don't pass the new params still get a stable, deterministic fingerprint.
    fp = compute_retrieval_fingerprint(scope_id="messages:read", access_mode="summary")
    assert isinstance(fp, str) and len(fp) == 24
