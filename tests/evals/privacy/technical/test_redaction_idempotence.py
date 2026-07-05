"""§F.8 — Redaction idempotence.

Invariant: applying the grantee disclosure transform twice must equal applying it once.
The retrieval path applies it more than once (the canonical store's `list` swaps disclosure
columns; the SQL path pre-swaps via the _disclosure spec), so a non-idempotent transform
corrupts legitimate grantee reads by mistaking already-redacted content for pending raw and
overwriting it with the "[disclosure pending]" placeholder.

Fixed by: (a) an applied-marker so a second swap is a no-op, (b) treating the placeholder as
a fixed point, and (c) removing the redundant re-application in retrieval._list_canonical_rows
(the store already discloses). This eval is a permanent gate against regressions.
"""

from __future__ import annotations

import pytest

from topos.disclosure.field_registry import DISCLOSURE_PENDING_PLACEHOLDER
from topos.disclosure.tier import apply_disclosure_tier_to_rows

pytestmark = [pytest.mark.private]


def _apply(rows):
    return apply_disclosure_tier_to_rows(
        [dict(r) for r in rows], table="conversation_messages", tier="default_disclosure"
    )


def test_grantee_disclosure_swap_is_idempotent():
    """Swap once then again: content must be stable (redacted, never downgraded to placeholder)."""
    rows = [
        {
            "record_id": "m1",
            "content": "raw secret sk-xyz reach me at a@b.com",
            "content_disclosure": "redacted [REDACTED_EMAIL]",
        }
    ]
    once = _apply(rows)
    twice = _apply(once)
    assert once[0]["content"] == "redacted [REDACTED_EMAIL]"
    assert once[0]["content"] == twice[0]["content"], (
        "disclosure swap is not idempotent: "
        f"{once[0]['content']!r} != {twice[0]['content']!r}"
    )


def test_pending_placeholder_is_a_fixed_point():
    """A row already reduced to the pending placeholder must not be re-processed into raw or churn."""
    rows = [{"record_id": "m2", "content": DISCLOSURE_PENDING_PLACEHOLDER}]
    once = _apply(rows)
    twice = _apply(once)
    assert once[0]["content"] == DISCLOSURE_PENDING_PLACEHOLDER
    assert twice[0]["content"] == DISCLOSURE_PENDING_PLACEHOLDER


def test_genuinely_pending_raw_fails_closed():
    """Raw content with no disclosure column (never processed) must fail closed to placeholder."""
    rows = [{"record_id": "m3", "content": "raw secret contact a@b.com"}]
    once = _apply(rows)
    assert once[0]["content"] == DISCLOSURE_PENDING_PLACEHOLDER
    # And stays there under re-application.
    twice = _apply(once)
    assert twice[0]["content"] == DISCLOSURE_PENDING_PLACEHOLDER


def test_owner_tier_is_idempotent_noop():
    """Owner tier is a pass-through, so it is trivially idempotent (guards against regressions)."""
    rows = [{"record_id": "m1", "content": "raw", "content_disclosure": "redacted"}]
    once = apply_disclosure_tier_to_rows(
        [dict(r) for r in rows], table="conversation_messages", tier="owner_raw"
    )
    twice = apply_disclosure_tier_to_rows(
        [dict(r) for r in once], table="conversation_messages", tier="owner_raw"
    )
    assert once[0]["content"] == twice[0]["content"] == "raw"
