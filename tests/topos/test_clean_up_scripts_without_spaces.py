"""Third fix round, review R2-M1: the clean-up finds a short name written in a script with no spaces between words.

protects: since the clean-up matches whole words of three letters or more (the owner's decision, review R1 R-B1), a
name in Han, kana, Thai, Lao or Khmer was never found inside a sentence: it stands in a run of other letters of the
same script, so "no letter touches either end" is never true of it, and at two characters it was under the floor as
well. The re-check ran 30 pairs of a flagged name and a sentence; three of the sentences the narrower clean-up left
were also released by the share boundary ("王伟", "王小明" and "田中" inside a sentence), so a goal or a topic label
naming such a person was neither cleaned up nor withheld. A term written wholly in one of those scripts now matches
anywhere, with a floor of two characters. Nothing changes for a term in a script that does space its words.
Every name here is invented or a common placeholder name.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.blackhole import BlackholeStore, normalize_entity_name
from topos.features.lifecycle.blackhole_rebuild import _mentions, _unspaced, rebuild_for_blackhole
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public


@pytest.mark.parametrize("text, term, expected", [
    # the re-check's three, each inside a sentence of the same script
    ("我今天和王伟一起吃饭。", "王伟", True),
    ("下周要给王小明打电话。", "王小明", True),
    ("明日は田中さんと会議です。", "田中", True),
    # kana, Thai, Lao and Khmer names inside running text
    ("きのうたなかさんにあいました。", "たなか", True),
    ("カタカナのタナカさん。", "タナカ", True),
    ("วันนี้ไปกินข้าวกับสมชายที่ร้าน", "สมชาย", True),
    ("ມື້ນີ້ໄປກິນເຂົ້າກັບສົມສັກ", "ສົມສັກ", True),
    ("ថ្ងៃនេះទៅផ្សារជាមួយសុខា", "សុខា", True),
    # the floor is two characters: one character is a common word in every one of these scripts
    ("王先生今天来了。", "王", False),
    ("我今天一个人吃饭。", "王伟", False),                                  # and a sentence that does not name them
    # a term of two scripts, and every term in a script that spaces its words: the whole-word rule, unchanged
    ("田中kenさんと会議です。", "田中 ken", False),
    ("Lunch with 田中 ken today.", "田中 ken", True),
    ("Samples of the same paint.", "sam", False),
    ("Lunch with Sam on Friday.", "sam", True),
    ("We also finished the normal walk.", "al", False),
    ("Al came by.", "al", False),                                          # two Latin letters: still under the floor
    ("Привет, Ал пришёл.", "ал", False),                                   # Cyrillic spaces its words: floor of three
])
def test_a_term_in_a_script_without_spaces_matches_anywhere_from_two_characters(text, term, expected):
    """Rule: `_term_pattern` matches a wholly unspaced-script term with no word boundary and a floor of two. Remove
    it and the first eight cases are missed, as the re-check found. The term is normalised as the store keeps it
    (the normaliser writes a Lao or Khmer vowel sign as a space, in the term and in the text alike)."""
    assert _mentions(text, {normalize_entity_name(term)}) is expected


def test_which_terms_count_as_written_without_spaces():
    assert all(_unspaced(normalize_entity_name(term))
               for term in ("王伟", "田中", "たなか", "タナカ", "สมชาย", "ສົມສັກ", "សុខា", "王 小明"))
    assert not any(_unspaced(term) for term in ("sam", "田中 ken", "ал", "김민준", "محمد", "42", "", "-"))


def test_the_owners_clean_up_withdraws_a_goal_that_names_such_a_person():
    """The thing R2-M1 said was lost: before, the owner's clean-up deleted a goal that named them; then it stayed."""
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    conn.execute("INSERT INTO user_goals (goal_id, goal_text, payload_json, created_at) VALUES "
                 "('g1','下周要给王伟打电话','{}','2026-10-01'), ('g2','下周要给妈妈打电话','{}','2026-10-01')")
    conn.commit()
    BlackholeStore(conn).blackhole_entity(entity_ref="王伟")
    report = rebuild_for_blackhole(conn, "王伟")
    assert report.details["status"] == "complete" and report.goals_withdrawn == 1
    assert [row[0] for row in conn.execute("SELECT goal_id FROM user_goals")] == ["g2"]
