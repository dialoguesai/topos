"""Lane 172 (14 Oct 2026): inside a question's time window, the window's items fill what the words did not match.

Seen live (the owner's node, 14 Oct 02:35Z, gate run as the recipient): "What did <owner> do yesterday?" was
`nothing_matched` with 0 items used. The question carries almost no words, so the lexical lane scored nothing and the
meaning lane found nothing above its floor, while the day's window is itself the subject. With `ANSWER_TIME_FILL` on,
`rank(..., fill=True)` follows the scored members with the window's other members, newest first; every gate the walk
applies is unchanged, and nothing outside the window can enter.

Invented data only.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_answer_time_tr162 import DAY, NOW, P, TIME_SWITCHES, _asked, _bodies, _node
from topos.permissions_v2 import search_lanes, switches
from topos.permissions_v2.search_index import LoadedIndex, Member

# Bodies that share no word with "What did the owner do yesterday?" (and the twin node stores no vectors).
YESTERDAY = ["Kiln fired at dawn; three bowls cracked.", "Glazed the blue jug and left it to dry."]
LONG_AGO = ["Planted garlic along the north bed.", "Fixed the gate latch with a new spring."]


@pytest.fixture(autouse=True)
def every_time_switch_unset(monkeypatch):
    for name in TIME_SWITCHES + (P + "ANSWER_TIME_FILL",):
        monkeypatch.delenv(name, raising=False)


def _yesterday_node(tmp_path):
    # Two items yesterday (12 Oct, the clock is 13 Oct 15:00 UTC), two six weeks old.
    return _node(tmp_path, times=[NOW - DAY, NOW - DAY - 3600, NOW - 42 * DAY, NOW - 43 * DAY],
                 bodies=YESTERDAY + LONG_AGO)


def test_the_switch_is_on_by_default_bound_and_unbound():
    item = switches.BY_NAME[P + "ANSWER_TIME_FILL"]
    assert item.kind == "bool" and item.unbound is True and item.bound is True


def test_a_wordless_day_question_answers_from_that_days_items(tmp_path):
    node = _yesterday_node(tmp_path)
    body, receipt, prompts = _asked(node, "What did the owner do yesterday?",
                                    lambda prompt: "The owner fired the kiln and glazed a jug [1, 2].")
    assert receipt["time_scope"] == "yesterday" and receipt["time_scope_widened"] is False
    assert _bodies(prompts[0]) == sorted(YESTERDAY)              # the day's items, and nothing from six weeks ago
    assert receipt["records_used"] == 2
    assert body["outcome"] == "answered"


def test_with_the_switch_off_the_same_question_matches_nothing_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_TIME_FILL", "false")
    node = _yesterday_node(tmp_path)
    body, receipt, prompts = _asked(node, "What did the owner do yesterday?", lambda prompt: "x [1].")
    assert prompts == [] and receipt["reason"] == "nothing_matched" and body["outcome"] == "no_answer"


def test_a_question_with_no_time_word_is_never_filled(tmp_path):
    # No scope: the whole grant window is ranked by words alone, as before; an unmatched question still finds nothing.
    node = _yesterday_node(tmp_path)
    body, receipt, prompts = _asked(node, "What did the owner do?", lambda prompt: "x [1].")
    assert prompts == [] and receipt["reason"] == "nothing_matched" and receipt["time_scope"] == "none"


# --- rank(..., fill=True) on its own ---------------------------------------------------------------------------------

def _member(opaque, text, day):
    terms = {}
    for word in text.lower().split():
        terms[word] = terms.get(word, 0) + 1
    return Member(opaque_id=opaque, event_at_us=day * DAY * 1_000_000, doc_len=len(text.split()), terms=terms, sealed=b"")


def _index(members):
    return LoadedIndex(basis={}, model=None, dims=0, members=tuple(members), vectors={})


def test_fill_keeps_the_scored_order_first_then_the_window_newest_first_and_nothing_outside_it():
    index = _index([
        _member("a", "kiln bowls", 10), _member("b", "garlic bed", 12), _member("c", "jug glaze", 11),
        _member("d", "kiln shelf", 9), _member("e", "gate latch", 2)])
    low, high = 9 * DAY * 1_000_000, 12 * DAY * 1_000_000
    plain = search_lanes.rank(index, "kiln", None, limit=10, lower_us=low, upper_us=high, precision="day")
    filled = search_lanes.rank(index, "kiln", None, limit=10, lower_us=low, upper_us=high, precision="day", fill=True)
    assert set(plain) == {"a", "d"}
    assert filled[:len(plain)] == plain                     # the matched members keep their order and come first
    assert filled[len(plain):] == ["b", "c"]                # then the window's others, newest first
    assert "e" not in filled                                # day 2 is outside the window


def test_fill_never_exceeds_the_limit_and_the_spare():
    index = _index([_member(str(n), "word", 20 - n) for n in range(10)])
    low, high = 0, 30 * DAY * 1_000_000
    assert len(search_lanes.rank(index, "nothing", None, limit=3, lower_us=low, upper_us=high, precision="day",
                                 fill=True)) == 3
    assert len(search_lanes.rank(index, "nothing", None, limit=3, spare=2, lower_us=low, upper_us=high,
                                 precision="day", fill=True)) == 5
    assert search_lanes.rank(index, "nothing", None, limit=3, lower_us=low, upper_us=high, precision="day") == []
