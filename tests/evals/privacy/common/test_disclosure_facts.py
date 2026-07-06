"""Unit tests for the disclosed-fact extractor + sensitivity classifier (§F.5)."""

from __future__ import annotations

import pytest

from tests.evals.privacy.common.disclosure_facts import (
    count_sensitive,
    disclosure_profile,
    extract_disclosed_facts,
)

pytestmark = [pytest.mark.private]


def test_extract_counts_rows_and_summaries():
    pr = {
        "rows": [{"content": "a"}, {"content": "b"}],
        "summaries": [{"summary_text": "c"}],
        "answer_type": "raw",
    }
    facts = extract_disclosed_facts(pr)
    assert set(facts) == {"a", "b", "c"}


def test_denied_or_narrow_discloses_nothing():
    assert extract_disclosed_facts(None) == []
    assert extract_disclosed_facts({}) == []


def test_count_sensitive_pii_and_markers():
    facts = [
        "reach me at bob@example.com",
        "call +1-555-0100 now",
        "met with Vasquez Oleander",
        "the weather is nice",
    ]
    assert count_sensitive(facts) == 2  # email + phone
    assert count_sensitive(facts, markers=["Vasquez Oleander"]) == 3


def test_disclosure_profile():
    pr = {"rows": [{"content": "reach me at a@b.com"}, {"content": "safe note about launch"}]}
    prof = disclosure_profile(pr, sensitive_markers=[])
    assert prof["total_facts"] == 2
    assert prof["sensitive_facts"] == 1
