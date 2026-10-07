"""Fourth round, the share boundary and names written without spaces (the third round's backlog B1).

A name in Han, kana, Thai, Lao or Khmer stands inside a run of other letters of the same script, so it is never a
whole token of running text. The boundary matched a term of fewer than four characters as a whole token only, so a
message, a journal entry and a goal that named a protected person by a two- or three-character name in such a script
were RELEASED: "我今天和王伟一起吃饭。" with 王伟 Off-limits left the node (4 of 4 in the third round's probe). And
since identifiers match only as themselves (ruling M), a handle or a username in such a script was released at any
length.

The rule (`entity_boundary.UNSPACED`, `unspaced_terms`, `_reading_hits`): a term written wholly in one of these
scripts is found anywhere in the text, from two characters. A tightening only: every other match is made as before,
and nothing that was withheld is released.

protects:
  - the probe's eight cases: the four that were released are withheld, the four that were withheld still are;
  - the floor (one character is not a name), the scripts it is for, and that every other term is read as before;
  - identifiers in such a script;
  - that the change only ever adds a match, over the whole invented corpus;
  - the boundary's revision: it moves exactly where some term is read this way;
  - THE MEASURED SET: how much ordinary text that names nobody is withheld more, by script and by name, on invented
    text (`unspaced_script_corpus.py`). The numbers are held here so that the cost stays visible.
Every person here is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.test_entity_boundary_identifier_aliases import gate
from tests.permissions_v2.test_entity_boundary_v8 import SCHEMA, withheld
from tests.permissions_v2.unspaced_script_corpus import SCRIPTS
from topos.permissions_v2 import entity_boundary
from topos.permissions_v2.entity_boundary import EntityBoundary, skeleton, unspaced_terms

pytestmark = pytest.mark.public

KINDS = ("message", "journal_entry", "goal")


def protecting(name):
    """The boundary over one Off-limits entry for this name, as the owner's own mark writes it."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO entity_blackholes VALUES('b1','',?,?,?)", (name.lower(), name, json.dumps([name])))
    return EntityBoundary(conn)


@pytest.fixture
def before(monkeypatch):
    """The boundary as it read these scripts before the rule: no term is long enough to be found in a run."""
    def switch(on):
        monkeypatch.setattr(entity_boundary, "UNSPACED_TERM_CHARS", 10 ** 6 if on else 2)
        unspaced_terms.cache_clear()
    yield switch
    monkeypatch.undo()
    unspaced_terms.cache_clear()


#: The third round's probe (its report, backlog B1), case for case: (name, text, was it withheld before).
PROBE = [
    ("王伟", "我今天和王伟一起吃饭。", False), ("王小明", "下周要给王小明打电话。", False),
    ("田中", "明日は田中さんと会議です。", False), ("たなか", "きのうたなかさんにあいました。", False),
    ("สมชาย", "วันนี้ไปกินข้าวกับสมชายที่ร้าน", True),            # five characters: a long term, found anywhere already
    ("王伟", "我今天和 王伟 一起吃饭。", True),                     # set off by spaces: a whole token
    ("김민준", "오늘 김민준 만났다", True), ("김민준", "오늘 김민준이랑 만났다", True),   # Hangul is written with spaces
]


@pytest.mark.parametrize("name, text, was_withheld", PROBE, ids=[f"{n}-{i}" for i, (n, _t, _w) in enumerate(PROBE)])
def test_the_probe_four_that_were_released_are_withheld_and_four_that_were_withheld_still_are(name, text, was_withheld,
                                                                                            before):
    """Rule: `_reading_hits` looks for a term of `unspaced_terms` anywhere in the text. Take it out and the first
    four cases are released again, message, journal entry and goal alike."""
    before(True)
    assert [withheld(kind, protecting(name), text) for kind in KINDS] == [was_withheld] * 3
    before(False)
    assert [withheld(kind, protecting(name), text) for kind in KINDS] == [True] * 3


def test_the_floor_is_two_characters():
    """One character is not a name: "王" alone stays a whole-token match, or every sentence with a king in it goes."""
    assert entity_boundary.UNSPACED_TERM_CHARS == 2
    assert unspaced_terms(frozenset({"王", "王伟", "たなか", "田中太郎"})) == frozenset({"王伟", "たなか", "田中太郎"})
    one = protecting("王")
    assert not withheld("message", one, "那位国王的决定改变了历史。")
    assert withheld("message", one, "我今天和 王 一起吃饭。")                       # as a whole token: as before
    # a Thai nickname whose marks leave one letter is below the floor too, and is read as before
    assert skeleton("บี") == "บ" and unspaced_terms(frozenset({skeleton("บี")})) == frozenset()


def test_it_is_for_these_scripts_and_every_other_term_is_read_as_before(before):
    """A Latin, Cyrillic, Greek or Hangul name of two or three letters is still a whole token and its forms, never a
    piece of a longer word; a name that mixes scripts is not of these scripts."""
    assert unspaced_terms(frozenset({"sam", "al", "ed", "ана", "민준", "田中san", "ab12"})) == frozenset()
    for name, inside in (("Sam", "The same samples came late."), ("Al", "We also finished the normal walk."),
                         ("Ана", "Банан и ананас лежали на столе.")):
        assert not withheld("message", protecting(name), inside), name
    for script, (sentences, names, _naming) in SCRIPTS.items():
        for name in names:
            term = skeleton(name)
            assert (term in unspaced_terms(frozenset({term}))) == (len(term) >= 2), (script, name)
    # a long name in these scripts was found anywhere already (in the separator-free text) and still is
    assert withheld("message", protecting("田中太郎"), "明日は田中太郎さんと会議です。")
    before(True)
    assert withheld("message", protecting("田中太郎"), "明日は田中太郎さんと会議です。")


def test_the_list_of_scripts_is_the_one_the_clean_up_reads():
    from topos.features.lifecycle import blackhole_rebuild

    assert entity_boundary.UNSPACED.pattern == blackhole_rebuild.UNSPACED.pattern
    assert entity_boundary.UNSPACED_TERM_CHARS == blackhole_rebuild.MIN_UNSPACED_TERM_CHARS


@pytest.mark.parametrize("handle, text", [("小明", "下周要给小明打电话。"), ("田中太郎", "明日は田中太郎さんと会議です。"),
                                          ("たなか", "きのうたなかさんにあいました。")])
def test_a_handle_or_a_username_in_such_a_script_is_found_in_running_text(handle, text, before):
    """An identifier matches only as itself (ruling M): for a bare word that means a whole token, and in these scripts
    a handle is never one. Since that ruling a contact carried under such a username was released wherever the
    username was written in a sentence, at any length. In a run, an identifier is found as a name is."""
    boundary = lambda: gate(canonical="Quorra Vellaby", aliases=("quorra vellaby", handle), identifiers=[handle])   # noqa: E731
    before(True)
    assert skeleton(handle) in boundary()._groups[2]                       # an identifier that matches only as itself
    assert not withheld("message", boundary(), text)
    before(False)
    assert all(withheld(kind, boundary(), text) for kind in KINDS)
    assert not withheld("message", boundary(), "今日は朝から雨が降っています。")


def _measure(switch):
    """{script: {name: (ordinary sentences withheld before, after, of how many)}} over the three kinds a sentence can
    be released as: a sentence counts once if any kind withholds it."""
    found = {}
    for script, (sentences, names, _naming) in SCRIPTS.items():
        found[script] = {}
        for name in names:
            counts = []
            for on in (True, False):
                switch(on)
                boundary = protecting(name)
                counts.append(sum(any(withheld(kind, boundary, text) for kind in KINDS) for text in sentences))
            found[script][name] = (counts[0], counts[1], len(sentences))
    return found


def test_nothing_that_was_withheld_is_released(before):
    """A tightening only. Every sentence of the corpus, and a sentence that names each name, as a message, a journal
    entry and a goal: whatever the boundary withheld before the rule it withholds with it."""
    checked = 0
    for script, (sentences, names, naming) in SCRIPTS.items():
        for name in names:
            texts = [*sentences, naming.format(name=name), f"{name}", f"a {name} b"]
            before(True)
            old = protecting(name)
            was = {(kind, text) for kind in KINDS for text in texts if withheld(kind, old, text)}
            before(False)
            new = protecting(name)
            now = {(kind, text) for kind in KINDS for text in texts if withheld(kind, new, text)}
            assert was <= now, (script, name, sorted(was - now)[:3])
            checked += len(texts) * len(KINDS)
    assert checked > 5000


def test_each_name_is_withheld_where_it_is_written_inside_a_sentence(before):
    """Recall on the corpus's own names: a sentence that names the person in running text, in each script. Before
    the rule only the names of four characters or more were found."""
    released_before, released_now = [], []
    for script, (_sentences, names, naming) in SCRIPTS.items():
        for name in names:
            text = naming.format(name=name)
            before(True)
            if not all(withheld(kind, protecting(name), text) for kind in KINDS):
                released_before.append(name)
            before(False)
            if not all(withheld(kind, protecting(name), text) for kind in KINDS):
                released_now.append(name)
    assert released_before == [name for _sentences, names, _naming in SCRIPTS.values() for name in names]   # all 46
    # What the rule does not reach: a name whose letters, once the marks are read through, are fewer than two.
    assert released_now == ["บี"]


def test_the_revision_moves_exactly_where_a_term_is_read_this_way(before):
    """So an index built at the revision before is re-qualified on a node whose boundary holds such a term, and on
    no other node."""
    def revisions(make):
        before(True)
        old = make().revision
        before(False)
        return old, make().revision

    for unchanged in (lambda: protecting("Quorra Vellaby"), lambda: protecting("Sam"), lambda: protecting("田中太郎"),
                      lambda: protecting("김민준"), lambda: gate()):
        old, new = revisions(unchanged)
        assert old == new
    for moved in (lambda: protecting("王伟"), lambda: protecting("たなか"), lambda: protecting("นก"),
                  lambda: gate(canonical="Quorra Vellaby", aliases=("quorra vellaby", "田中太郎"), identifiers=["田中太郎"])):
        old, new = revisions(moved)
        assert old != new


def test_the_measured_set(before, capsys):
    """How much ordinary text that names nobody is withheld more. Invented sentences, each as a message, a journal
    entry and a goal; a sentence counts once. Before the rule: none, for every name here."""
    found = _measure(before)
    with capsys.disabled():
        print()
        for script, names in found.items():
            total = next(iter(names.values()))[2]
            after = sorted(count for _before, count, _total in names.values())
            print(f"MEASURED {script}: {total} sentences, {len(names)} names; withheld more per name: "
                  f"median {after[len(after) // 2]}, max {after[-1]}, names with any {sum(count > 0 for count in after)}; "
                  + ", ".join(f"{name} {count}" for name, (_b, count, _t) in names.items()))
    assert all(was == 0 for names in found.values() for was, _now, _total in names.values())
    measured = {script: {name: now for name, (_was, now, _total) in names.items() if now} for script, names in found.items()}
    # The names that cost anything, and how many of the script's ordinary sentences each now withholds. They are the
    # ones that are also a word, or that two neighbouring words spell between them ("国王" + "伟大" holds "王伟",
    # "紧张" + "敏感" holds "张敏", "山田" + "中学校" holds "田中"), and in Thai, Lao and Khmer the short nicknames
    # whose letters, with the vowel signs read through as the boundary reads every script, are a pair of consonants
    # many words hold ("น้ำ", water, is read "นา" and is in "นาที", a minute).
    assert measured == {
        "Chinese": {"王伟": 1, "张敏": 1, "高兴": 1, "文静": 1, "小明": 1},                                   # of 60
        "Japanese": {"田中": 1, "山田": 1, "たなか": 1, "さくら": 1, "ゆき": 3, "はな": 3, "あい": 1, "けん": 1,
                     "ケン": 1, "マリ": 1},                                                               # of 64
        "Thai": {"นก": 3, "ต้น": 1, "สม": 3, "ฝน": 2, "น้ำ": 8},                                              # of 40
        "Lao": {"ນົກ": 2, "ຝົນ": 1, "ນ້ອຍ": 1, "ຄຳ": 2},                                                     # of 15
        "Khmer": {"ស្រី": 2, "ចាន់": 1},                                                                    # of 15
    }
    # Pooled, sentences withheld more over (names x sentences): 5 of 660, 14 of 960, 17 of 400, 6 of 75, 3 of 75.
    assert {script: (sum(names.values()), len(SCRIPTS[script][1]) * len(SCRIPTS[script][0]))
            for script, names in measured.items()} == {
        "Chinese": (5, 660), "Japanese": (14, 960), "Thai": (17, 400), "Lao": (6, 75), "Khmer": (3, 75)}
