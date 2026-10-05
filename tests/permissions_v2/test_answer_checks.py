"""Answer checks are independent of the model and can be tested on invented text."""
from __future__ import annotations

import pytest

from topos.permissions_v2.answer_checks import (copied_sentence, post_check_citations,
    scrub_sentences, _redact_pii)

RAW = [
    "We booked the lakeside cabin for the second weekend of June and paid the deposit on Friday.",
    "Dentist on Tuesday",
    "Call the plumber about the kitchen sink",
    "We will move the team offsite to the old mill on the fourteenth and stay until the following Tuesday morning.",
]


@pytest.mark.parametrize("sentence,copied", [
    ("They booked the lakeside cabin for the second weekend of June [1].", True),
    ("They reserved a cabin by a lake for a weekend in June [1].", False),
    ("The dentist is on Tuesday [2].", False),
    ("Yes: call the plumber about the kitchen sink, they said [3].", True),
    ("We will move the team offsite to an old mill on the fourteenth and stay [4].", True),
    ("They plan an offsite at a mill from the fourteenth until Tuesday [4].", False),
])
def test_the_six_frozen_copy_vectors(sentence, copied):
    assert copied_sentence(sentence, RAW) is copied


def test_citations_drop_uncited_and_invented_numbers_without_repairing_them():
    checked = post_check_citations("Dr. Lee planned the trip [1]. An unrelated claim. A made-up source [99].", 2)
    assert checked.sentences == ("Dr. Lee planned the trip [1].",)
    assert checked.cited == frozenset({1})
    assert checked.dropped == 2


def test_scrub_redacts_personal_contact_and_drops_nsfw_without_redacting_dates():
    kept, dropped = scrub_sentences(["Write to someone@example.com on 2026-10-05 [1].",
                                      "Call +1 (555) 123-4567 [1].", "That is nsfw [1]."])
    assert dropped == 1
    assert "[REDACTED_EMAIL]" in kept[0]
    assert "2026-10-05" in kept[0]
    assert "[REDACTED_PHONE]" in kept[1]
