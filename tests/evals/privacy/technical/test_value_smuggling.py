"""M0 probe — the public_result envelope is key-name-only; value smuggling passes.

protects: envelope inspects values, not just key names.

PLAN_QUERY_LOOP.md M0.2. `validate_public_result` rejects seven key NAMES
(FORBIDDEN_ARTIFACT_KEYS) and never inspects values, so verbatim substrate
under an allowed alias (``SELECT content AS summary``-shaped output) walks
through clean. That hole is the core leak class the S9 execute tier must
close with a value-aware check.

The xfail marks are strict: the day a value-aware check lands, these tests
XPASS and RED the suite — at that point delete the marks and keep the
assertions as the permanent battery. Until then they document the hole
without hiding it behind a skip.
"""

from __future__ import annotations

import pytest

from topos.query.session_utils import validate_public_result

# Fabricated content shaped like a verbatim canonical row body. No owner data.
RAW_ROW_TEXT = (
    "verbatim message body: dinner at 7 with Alex Example to review invoice "
    "#12345, then pick up the prescription refill before the pharmacy closes"
)


def test_forbidden_key_names_still_rejected() -> None:
    """Control (non-vacuity): the existing key-name gate works and must stay."""
    with pytest.raises(ValueError):
        validate_public_result({"evidence": [RAW_ROW_TEXT]})
    with pytest.raises(ValueError):
        validate_public_result({"items": [{"source_rows": [RAW_ROW_TEXT]}]})


@pytest.mark.xfail(
    strict=True,
    reason="M0 probe: validator is key-name-only; the S9 value-aware check flips this",
)
def test_verbatim_row_text_under_allowed_alias_rejected() -> None:
    """``SELECT content AS summary`` — substrate verbatim under an allowed key."""
    payload = {
        "access_mode": "summary",
        "answer_type": "summary",
        "summaries": [{"summary_text": RAW_ROW_TEXT}],
    }
    with pytest.raises(ValueError):
        validate_public_result(payload)


@pytest.mark.xfail(
    strict=True,
    reason="M0 probe: row values concatenated into a 'scalar' pass the envelope",
)
def test_aggregate_scalar_smuggle_rejected() -> None:
    """group_concat-style smuggling: many row bodies folded into one scalar."""
    payload = {
        "access_mode": "raw",
        "answer_type": "raw",
        "rows": [{"count": (RAW_ROW_TEXT + " | ") * 3}],
    }
    with pytest.raises(ValueError):
        validate_public_result(payload)
