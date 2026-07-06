"""§B.3 follow-up — the grantee PII redactor must not treat ISO dates as phone numbers.

Handed off by the dense-intelligence workstream: `2026-07-05` was being redacted as
`[REDACTED_PHONE]`. The fix must NOT under-redact real phones (a leak is worse than an
over-redacted date), so this pins both directions.
"""

from __future__ import annotations

import pytest

from topos.query.disclosure import _redact_pii

pytestmark = [pytest.mark.private]


@pytest.mark.parametrize(
    "text",
    [
        "the meeting is on 2026-07-05",
        "logged at 2026-07-05T12:30:00",
        "range 2026-01-01 to 2026-12-31",
        "born 1990-04-22",
    ],
)
def test_iso_dates_are_not_redacted(text):
    out = _redact_pii(text)
    assert "[REDACTED_PHONE]" not in out, out
    # the date itself survives intact
    assert any(tok in out for tok in ("2026-07-05", "2026-01-01", "1990-04-22", "2026-12-31"))


@pytest.mark.parametrize(
    "text,secret",
    [
        ("call me at +1-555-0199", "+1-555-0199"),
        ("phone (555) 010-0100 ok", "(555) 010-0100"),
        ("reach 555-010-0100 today", "555-010-0100"),
        ("intl +44 20 7946 0958 line", "+44 20 7946 0958"),
    ],
)
def test_real_phones_still_redacted(text, secret):
    out = _redact_pii(text)
    assert "[REDACTED_PHONE]" in out, out
    assert secret not in out, "phone number leaked"


def test_email_still_redacted():
    out = _redact_pii("mail bob@example.com and meet 2026-07-05")
    assert "[REDACTED_EMAIL]" in out
    assert "bob@example.com" not in out
    assert "2026-07-05" in out  # date preserved alongside


def test_date_and_phone_together():
    out = _redact_pii("on 2026-07-05 call +1-555-0142")
    assert "2026-07-05" in out            # date kept
    assert "+1-555-0142" not in out       # phone redacted
    assert "[REDACTED_PHONE]" in out
