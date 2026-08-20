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
from topos.query.types import FilteredContext, RetrievalBundle

pytestmark = [pytest.mark.private]

EMAIL_CANARY = "zx-canary-b3@example-priv.net"
PHONE_CANARY = "+1-555-0142"
#: The owner's own words, in a row. The mode ceiling is the only thing between this
#: sentence and a grantee holding an `access_mode` below `raw`.
RAW_OWNER_SENTENCE = "zx-canary-sg1 I told my sister I am leaving him in the spring"


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


# --------------------------------------------------------------------------------------
# The access-mode CEILING — `disclosure.py:136-146`.
#
# Everything above guards the CONTENT filter: what a grantee may see of the artifacts
# they are allowed to receive at all. The two blocks below decide WHICH ARTIFACTS those
# are, and until now nothing tested them. Both could be severed and 336 privacy tests
# still passed; with them severed an adversarial sweep's probe reported
#
#     rows present: True | row contents: [RAW OWNER SENTENCE]
#
# to a grantee holding a summary-mode grant. The code was correct and simply unguarded,
# which is the worse of the two states: a guard nobody has watched fail is not a guard,
# so the acceptance for these three is that severing each block turns them red.
#
# The bundle carries a real `rows` list, real `summaries` and the `content`/`messages`
# keys, because "rows is absent" asserted against a packet that never had rows is a test
# that cannot fail. The content filter is deliberately NOT relied on here: the sentence
# below holds no email or phone, so PII redaction would leave it fully legible. The
# ceiling is the whole defence.
# --------------------------------------------------------------------------------------


def _ceiling_bundle():
    return RetrievalBundle(
        context_packet={
            "scope_id": "relationship_context:read",
            "rows": [{"_table": "conversation_messages", "content": RAW_OWNER_SENTENCE}],
            "summaries": [{"topic": "family", "summary_text": RAW_OWNER_SENTENCE}],
            "content": RAW_OWNER_SENTENCE,
            "messages": [{"content": RAW_OWNER_SENTENCE}],
        }
    )


def _assert_summary_ceiling(filtered: FilteredContext) -> None:
    """(a). Factored out so the canary below runs the SAME assertions, not a paraphrase."""
    assert "rows" not in filtered.context_packet, (
        "summary mode delivered raw rows: " f"{filtered.context_packet.get('rows')!r}"
    )
    assert "summary_mode_strip_raw" in filtered.filters_applied


def _assert_inference_ceiling(filtered: FilteredContext) -> None:
    """(b). Same."""
    for key in ("rows", "summaries", "content", "messages"):
        assert key not in filtered.context_packet, (
            f"inference mode delivered evidence under {key!r}: "
            f"{filtered.context_packet.get(key)!r}"
        )
    assert "inference_mode_strip_evidence" in filtered.filters_applied


def test_summary_mode_strips_rows_for_a_grantee():
    """(a) A summary-mode grant receives no `rows` key at all, and says so."""
    filtered = DisclosureFilterPipeline().apply(
        _ceiling_bundle(), access_mode="summary", disclosure_tier="default_disclosure"
    )
    _assert_summary_ceiling(filtered)
    assert RAW_OWNER_SENTENCE not in json.dumps(
        {"rows": filtered.context_packet.get("rows")}, default=str
    )


def test_inference_mode_strips_every_evidence_key_for_a_grantee():
    """(b) An inference-mode grant receives no rows, summaries, content or messages."""
    filtered = DisclosureFilterPipeline().apply(
        _ceiling_bundle(), access_mode="inference", disclosure_tier="default_disclosure"
    )
    _assert_inference_ceiling(filtered)
    assert RAW_OWNER_SENTENCE not in json.dumps(filtered.context_packet, default=str), (
        "the owner's sentence survived an inference-mode ceiling"
    )


class _LabelOnlyPipeline:
    """A ceiling that files the receipt and removes nothing.

    This is the exact shape (a) and (b) must not be satisfiable by, and it is not a
    hypothetical: `filters_applied` is a self-report, so a pipeline that appends
    `summary_mode_strip_raw` / `inference_mode_strip_evidence` without calling `pop`
    produces a packet whose own receipt says it was filtered. Every assertion about the
    receipt passes; the owner's sentence is in the packet.
    """

    def apply(self, bundle, *, access_mode: str = "raw", **_: object) -> FilteredContext:
        applied = []
        if access_mode == "summary":
            applied.append("summary_mode_strip_raw")
        if access_mode == "inference":
            applied.append("inference_mode_strip_evidence")
        return FilteredContext(
            context_packet=dict(bundle.context_packet or {}), filters_applied=applied
        )


@pytest.mark.parametrize(
    ("mode", "check"),
    [("summary", _assert_summary_ceiling), ("inference", _assert_inference_ceiling)],
)
def test_a_label_only_ceiling_fails_these_tests(mode, check):
    """(c) The positive control. Without it, (a) and (b) are satisfied by a no-op that
    lies in its own receipt — precisely the failure class this programme keeps finding —
    and the canary would be the test suite itself."""
    filtered = _LabelOnlyPipeline().apply(_ceiling_bundle(), access_mode=mode)
    assert RAW_OWNER_SENTENCE in json.dumps(filtered.context_packet, default=str), (
        "premise gone: the label-only stand-in is no longer leaking, so it cannot show "
        "that the assertions above are what catches a leak"
    )
    with pytest.raises(AssertionError):
        check(filtered)
