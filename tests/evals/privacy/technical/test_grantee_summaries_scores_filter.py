"""§B.3 — grantee content filter for non-row artifacts (summaries / scores / facts).

The grantee disclosure branch historically filtered only `rows`; `summaries` and `scores`
(and future `facts`/dossier keys) passed through unscrubbed. Since every dense artifact
surfaces through those keys, a grantee could receive raw PII in a summary_text/topic that
never touched the row-level filters. This gate asserts the disclosure pipeline PII-redacts
and NSFW-excludes non-row artifacts for grantees, while the owner still sees raw (non-vacuous).
"""

from __future__ import annotations

import json

import pytest

from topos.query.disclosure import DisclosureFilterPipeline
from topos.query.types import RetrievalBundle

pytestmark = [pytest.mark.private]

EMAIL_CANARY = "zx-canary-b3@example-priv.net"
PHONE_CANARY = "+1-555-0142"


def _bundle():
    return RetrievalBundle(
        context_packet={
            "scope_id": "relationship_context:read",
            "summaries": [
                {"topic": "intro", "summary_text": f"reach Ada at {EMAIL_CANARY} soon"},
                {"topic": "logistics", "summary_text": f"call {PHONE_CANARY} to confirm"},
            ],
            "scores": [
                {"label": "warmth", "summary_text": f"contact {EMAIL_CANARY}", "value": 0.7},
            ],
        }
    )


def _apply(tier: str, mode: str = "summary"):
    return DisclosureFilterPipeline().apply(
        _bundle(), access_mode=mode, disclosure_tier=tier
    )


def test_grantee_summaries_and_scores_are_pii_redacted():
    filtered = _apply("default_disclosure")
    blob = json.dumps(filtered.context_packet, default=str)
    assert EMAIL_CANARY not in blob, "email canary leaked to grantee via summaries/scores"
    assert PHONE_CANARY not in blob, "phone canary leaked to grantee via summaries/scores"
    assert "[REDACTED_EMAIL]" in blob and "[REDACTED_PHONE]" in blob
    assert "grantee_scrub_summaries" in filtered.filters_applied
    assert "grantee_scrub_scores" in filtered.filters_applied


def test_owner_still_sees_raw_in_summaries_and_scores():
    """Non-vacuous: the owner path preserves the raw values, proving the scrub is grantee-only."""
    filtered = _apply("owner_raw")
    blob = json.dumps(filtered.context_packet, default=str)
    assert EMAIL_CANARY in blob
    assert PHONE_CANARY in blob
    assert "grantee_scrub_summaries" not in filtered.filters_applied


def test_grantee_nsfw_summary_item_is_dropped():
    bundle = RetrievalBundle(
        context_packet={
            "summaries": [
                {"topic": "ok", "summary_text": "safe content"},
                {"topic": "bad", "summary_text": "explicit stuff", "content_nsfw": 1},
            ]
        }
    )
    filtered = DisclosureFilterPipeline().apply(
        bundle, access_mode="summary", disclosure_tier="default_disclosure"
    )
    summaries = filtered.context_packet.get("summaries") or []
    assert len(summaries) == 1
    assert summaries[0]["topic"] == "ok"


def test_inference_mode_scores_are_scrubbed_for_grantee():
    """Even in inference mode (rows stripped), score text must be scrubbed for grantees."""
    bundle = RetrievalBundle(
        context_packet={
            "scores": [{"summary_text": f"lead at {EMAIL_CANARY}", "value": 0.9}],
        }
    )
    filtered = DisclosureFilterPipeline().apply(
        bundle, access_mode="inference", disclosure_tier="default_disclosure"
    )
    blob = json.dumps(filtered.context_packet, default=str)
    assert EMAIL_CANARY not in blob
    assert "grantee_scrub_scores" in filtered.filters_applied
