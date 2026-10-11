"""BL-172: the Off-limits short forms the IF-6 set-6 gate still released whole on 1.5.3 (entity boundary v10).

protects: lane FG (10 Oct) re-ran the set-6 derived-facts gate on 1.5.3 with permissive model labels. 16 of the 75
journal entries that name a protected person only by a short form were released WHOLE by the journal-entry kind,
which is on by default in shares, and the same misses made the 4 false fact releases that keep inferred facts off.
The model's own label was the only backstop left. Ten classes, each rebuilt here with coined names (none of the set's
own), withheld in every kind a share releases through the check that kind's release runs, and each released before v10:

  - the five ways of WRITING a name the readings did not undo: a Latin look-alike letter no normalisation folds
    (homoglyph), a right-to-left override (bidi_override), letters spaced apart (spaced), a digit inside a
    three-character word (leet), a Scandinavian or German vowel written as two letters (diacritic_variant), and kana or
    Hangul (transliteration);
  - the forms of a short name the endings did not make: the Slavic fleeting vowel (stem_change, sentence_initial), the
    Russian yo-diminutive (diminutive, diminutive_inflected), the Finnish pet ending (diminutive) and the Irish and
    Welsh mutations (stem_change), read wherever written with a capital first letter even for a name that is also a
    short English word.

Each step is load-bearing: take it out and its class is released again (the mutant tests). And v10 only widens: with
every v10 hook off the boundary reads as v9 did, and nothing v9 withheld is released (the last tests), while the
ordinary words a form would collide with stay released. Every person here is invented.
"""
from __future__ import annotations

import re
import unicodedata

import pytest

from tests.permissions_v2.test_entity_boundary_v8 import gate, withheld
from topos.permissions_v2 import entity_boundary

pytestmark = pytest.mark.public

KINDS = ("journal_entry", "message", "goal", "fact")
NONE = frozenset()
CACHED = ("v10_forms", "_v10_short", "_v10_long", "celtic_forms")


def _clear_caches():
    """The v10 form builders are cached on their arguments; a mutant changes what they build (a builder a mutant
    replaced has no cache)."""
    for name in CACHED:
        getattr(getattr(entity_boundary, name), "cache_clear", lambda: None)()


@pytest.fixture(autouse=True)
def _fresh_form_caches():
    _clear_caches()
    yield
    _clear_caches()


def _without_lookalike_latin(monkeypatch):
    letters = {cp: ch for cp, ch in entity_boundary._LOOKALIKE_LETTERS.items()
               if cp not in entity_boundary._LOOKALIKE_LATIN}
    monkeypatch.setattr(entity_boundary, "_LOOKALIKE_LETTERS", letters)
    monkeypatch.setattr(entity_boundary, "_SCRIPTED", re.compile(
        "[" + "".join(re.escape(chr(cp)) for cp in sorted(set(entity_boundary._TRANSLITERATION) | set(letters))) + "]"))


# The step each mutant takes out, by name.
MUTANTS = {
    "lookalike_latin": _without_lookalike_latin,
    "override_reading": lambda mp: mp.setattr(entity_boundary, "_overrides_undone", lambda text: None),
    "spacing_reading": lambda mp: mp.setattr(entity_boundary, "_spacing_closed", lambda text: None),
    "yo_reading": lambda mp: mp.setattr(entity_boundary, "_yo_spelled", lambda text: None),
    "kana_hangul_reading": lambda mp: mp.setattr(entity_boundary, "_romanized", lambda text: None),
    "leet_three_chars": lambda mp: mp.setattr(entity_boundary, "LEET_MIN_CHARS", 4),
    "digraph_spellings": lambda mp: mp.setattr(entity_boundary, "_DIGRAPH_LETTERS", frozenset()),
    "fleeting_vowel": lambda mp: mp.setattr(entity_boundary, "_FLEETING_ENDINGS", ()),
    "yo_diminutive": lambda mp: mp.setattr(entity_boundary, "_YO", ()),
    "finnish_pet": lambda mp: mp.setattr(entity_boundary, "_FINNISH_PET", ()),
    "irish_slender": lambda mp: mp.setattr(entity_boundary, "_slender", lambda word: None),
    "celtic_mutation": lambda mp: mp.setattr(entity_boundary, "celtic_forms", lambda word: (NONE, NONE)),
    "titled_place": lambda mp: mp.setattr(entity_boundary, "v10_titled_tokens", lambda text: set()),
    "rendered_reading": lambda mp: mp.setattr(entity_boundary, "_rendered", lambda text: text),
    "eclipsis_place": lambda mp: mp.setattr(entity_boundary, "v10_eclipsed_tokens", lambda text: set()),
}


def without_v10(monkeypatch):
    """The boundary as v9 read every kind: no v10 reading, spelling, form or place."""
    for mutant in MUTANTS.values():
        mutant(monkeypatch)
    monkeypatch.setattr(entity_boundary, "_v10_short", lambda terms: (NONE, NONE))
    monkeypatch.setattr(entity_boundary, "_v10_long", lambda parts: (NONE, NONE))


# (set-6 class, the step that catches it, canonical name, aliases, the text as a journal entry writes it, the same
# form in running text for every other kind, the column of the journal row)
CASES = [
    ("sentence_initial", "titled_place", "Tev Morrant", ("Tev",), "Тва не было дома, оставил записку соседу.", None),
    ("stem_change", "fleeting_vowel", "Tev Morrant", ("Tev",), "Позвонил Тву насчёт ключей от гаража.", None),
    ("stem_change", "fleeting_vowel", "Tev Morrant", ("Tev",), "Обед со Твом в столовой.", None),
    ("stem_change", "fleeting_vowel", "Tev Morrant", ("Tev",), "vecherom u Tva, chinili koleso.", None),
    ("stem_change", "irish_slender", "Pód Ó Ruairc", ("Pód",), "Tráthnóna i dteach Phóid ag péinteáil.", None),
    ("stem_change", "celtic_mutation", "Dei Prothero-Lake", ("Dei",), "Rhoddais yr allwedd i Ddei cyn gadael.", None),
    ("diminutive", "yo_diminutive", "Tev Morrant", ("Tev",), "Tyova came round to help move the bookcase.", None),
    ("diminutive", "finnish_pet", "Rei Halvorsen", ("Rei",), "Reitsu tuli käymään toimistolle.", None),
    ("diminutive_inflected", "yo_diminutive", "Tev Morrant", ("Tev",), "Napisal Tyove pro ponedel'nik.", None),
    ("homoglyph", "lookalike_latin", "Igo Varnetti", ("Igo",), "Iɡo", "people"),
    ("homoglyph", "lookalike_latin", "Igo Varnetti", ("Igo",), "Lunch with ɪɢᴏ at noon.", None),
    ("transliteration", "kana_hangul_reading", "Mio Tanberg", ("Mio",), "昼ごはんはミオくんと。", None),
    ("transliteration", "kana_hangul_reading", "Tae Kestrup", ("Tae",), "12:30 태랑 점심 먹었다.", None),
    ("bidi_override", "override_reading", "Ezo Brandvik", ("Ezo",), "Lunch with \u202eozE\u202c at the bar.", None),
    ("diacritic_variant", "digraph_spellings", "Åke Lindmark", ("Åke",), "Ringde Aake om stugnyckeln.", None),
    ("diacritic_variant", "digraph_spellings", "Jürg Imhofer", ("Jürg",), "Called Juerg about the van.", None),
    ("spaced", "spacing_reading", "Rui Calloway", ("Rui",), "Quiz night; team was R u i, me and two others.", None),
    ("spaced", "spacing_reading", "Rui Calloway", ("Rui",), "Quiz night; team was R. u. i. and me.", None),
    ("leet", "leet_three_chars", "Eda Pennock", ("Eda",), "Ed4 handed me the spare batteries.", None),
    ("yo_reading", "yo_reading", "Ted Fairbrook", ("Ted",), "Тёда не было дома.", None),
    # Review R-B172 B: the Irish eclipsis, a lower-case prefix before the capital.
    ("stem_change", "eclipsis_place", "Dai Wynne", ("Dai",), "Bhí mé i nDai inné.", None),
    ("stem_change", "eclipsis_place", "Ben Ostrander", ("Ben",), "Ár mBen a bhí ann.", None),
    ("stem_change", "eclipsis_place", "Tor Vandersloot", ("Tor",), "Ár dTor a bhí ann.", None),
    ("stem_change", "eclipsis_place", "Pód Ó Ruairc", ("Pód",), "Ár bPód a bhí ann.", None),
    # Review R-B172 A: the same classes encoded the way `normalized` already reads them (entities, other spaces, a
    # zero-width space, half-width katakana, decomposed and compatibility Hangul jamo).
    ("bidi_override", "rendered_reading", "Teo Brannick", ("Teo",), "Lunch with &#x202E;oeT&#x202C; today.", None),
    ("spaced", "rendered_reading", "Teo Brannick", ("Teo",), "Quiz team: T&nbsp;e&nbsp;o and me.", None),
    ("spaced", "rendered_reading", "Teo Brannick", ("Teo",), "Quiz team: T\u00a0e\u00a0o and me.", None),
    ("spaced", "rendered_reading", "Teo Brannick", ("Teo",), "Quiz team: T\u2009e\u2009o and me.", None),
    ("spaced", "rendered_reading", "Teo Brannick", ("Teo",), "Quiz team: T\u3000e\u3000o and me.", None),
    ("spaced", "rendered_reading", "Teo Brannick", ("Teo",), "Quiz team: T\u2002e\u2002o and me.", None),
    ("spaced", "rendered_reading", "Teo Brannick", ("Teo",), "Quiz team: T\u200ae\u200ao and me.", None),
    ("spaced", "rendered_reading", "Teo Brannick", ("Teo",), "Quiz team: T\u202fe\u202fo and me.", None),
    ("spaced", "rendered_reading", "Teo Brannick", ("Teo",), "Quiz team: T\u200b e\u200b o and me.", None),
    ("transliteration", "rendered_reading", "Teo Brannick", ("Teo",), "\uff83\uff75\u3068\u4f1a\u3063\u305f\u3002",
     None),
    ("transliteration", "rendered_reading", "Teo Brannick", ("Teo",),
     unicodedata.normalize("NFD", "\ud14c\uc624\ub791 \uc810\uc2ec."), None),
    ("transliteration", "rendered_reading", "Teo Brannick", ("Teo",), "\u314c\u3154\u3147\u3157\ub791 \uc810\uc2ec.", None),
]
IDS = [f"{case[0]}-{number}" for number, case in enumerate(CASES)]


def _verdicts(case) -> list:
    _class, _step, canonical, aliases, text, column = case
    boundary = gate(canonical, aliases)
    if column:
        return [withheld("journal_entry", boundary, text, column=column)]
    return [withheld(kind, boundary, text) for kind in KINDS]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_each_short_form_is_withheld_in_every_kind(case):
    """Rule: the v10 reading, spelling or form named in the case. Every kind's own release check withholds it."""
    assert all(_verdicts(case))


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_each_short_form_was_released_before_v10(case, monkeypatch):
    """The finding: with every v10 hook off (the v9 boundary), each case is released in its journal row."""
    without_v10(monkeypatch)
    assert not _verdicts(case)[0]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_each_step_is_load_bearing(case, monkeypatch):
    """Mutants: take out the one step the case names and its journal row is released again."""
    MUTANTS[case[1]](monkeypatch)
    assert not _verdicts(case)[0]


def test_every_step_has_a_case():
    assert {case[1] for case in CASES} == set(MUTANTS)


def test_the_set6_classes_each_have_a_case():
    assert {case[0] for case in CASES} >= {"stem_change", "transliteration", "diminutive", "sentence_initial",
                                           "diminutive_inflected", "homoglyph", "bidi_override", "diacritic_variant",
                                           "spaced", "leet"}


# --- the places v10 must not reach -----------------------------------------------------------------------------------

ORDINARY = [
    ("Sam Rivington", ("Sam",), "Met at 5am by the gate, 3rd time this week."),
    ("Den Holloway", ("Den",), "The DNA results came back."),
    ("Teo Brannick", ("Teo",), "Theo came over for tea."),
    ("Pil Arkwright", ("Pil",), "Phil came over for tea."),
    ("Ben Ostrander", ("Ben",), "Bein und Fuss, sagte er."),
    ("Ken Varga", ("Ken",), "Kein Problem, sagte sie."),
    ("Tev Morrant", ("Tev",), "Love the new place; the lava lamp works."),
    ("Peter Ambrose", ("Peter",), "Petra came round with the plans."),
    ("Alexander Moorcroft", ("Alexander",), "Alexandra came round with the plans."),
    ("Kai Pellinkari", ("Kai",), "Kaikoura trip next week; kaiser rolls for lunch."),
    ("Ivo Kestrel", ("Ivo",), "Options a b c and d are all fine."),
    ("Ugo Marz", ("Ugo",), "The k8s cluster is up and v1.5 shipped."),
    ("Dai Wynne", ("Dai",), "Daily standup moved; the DDA rules apply."),
    ("Åke Lindmark", ("Åke",), "A database phase aside, the lake was calm."),
    ("Mio Tanberg", ("Mio",), "カイロでミオソチスを買った。"),
    ("Na Brennick", ("Na",), "나도 갔다. 그는 학교에 갔다."),                 # one Hangul syllable is never a name
    ("Ha Brennick", ("Ha",), "これはテストです。"),                           # nor is hiragana
]

# Review R-B172 B: an acronym or a lower-case word that spells an eclipsed form is not one.
ACRONYMS = [("Ba Trinh", ("Ba",), "She finished her MBA last year."), ("Da Brennick", ("Da",), "Signed the NDA today."),
            ("Go Brennick", ("Go",), "Volunteered at an NGO."), ("Pa Brennick", ("Pa",), "The bottle is BPA free."),
            ("Cal Dunmore", ("Cal",), "Add the dentist to gcal."), ("Tor Vandersloot", ("Tor",), "The dtor frees it."),
            ("Cam Brennick", ("Cam",), "Installed the GCam port."), ("Gen Brennick", ("Gen",), "Ran ngen on the build."),
            ("Tim Brennick", ("Tim",), "Set the DTIM interval.")]


@pytest.mark.parametrize("canonical, aliases, text", ACRONYMS)
def test_an_acronym_is_never_an_eclipsed_name(canonical, aliases, text):
    boundary = gate(canonical, aliases)
    assert [withheld(kind, boundary, text) for kind in KINDS] == [False] * len(KINDS)


def test_an_eclipsed_form_in_a_people_column_withholds_in_any_case():
    assert withheld("journal_entry", gate("Pód Ó Ruairc", ("Pód",)), "bpod", column="people")


# Review R-B172 C: "!" is a leet letter but also how an exclamation ends, so a two-letter word and "!" stays a word.
EXCLAMATIONS = [("Hai Brennick", ("Hai",), "Ha! That was funny."), ("Mai Brennick", ("Mai",), "Ma! Dinner is ready."),
                ("Ali Brennick", ("Ali",), "Al! Over here."), ("Dai Wynne", ("Dai",), "Da! Look at this."),
                ("Tai Brennick", ("Tai",), "Ta! Thanks for that."), ("Ugo Marz", ("Ugo",), "Ug! The rain again.")]


@pytest.mark.parametrize("canonical, aliases, text", EXCLAMATIONS)
def test_a_two_letter_exclamation_is_not_a_leet_name(canonical, aliases, text):
    boundary = gate(canonical, aliases)
    assert [withheld(kind, boundary, text) for kind in KINDS] == [False] * len(KINDS)
    assert all(withheld(kind, boundary, "Ug0 handed me the batteries.") for kind in KINDS) == (aliases == ("Ugo",))


@pytest.mark.parametrize("canonical, aliases, text", ORDINARY)
def test_ordinary_words_near_a_form_stay_released(canonical, aliases, text):
    """A time, an acronym, another name, a German word, a list of letters, a version: none of them is a form."""
    boundary = gate(canonical, aliases)
    assert [withheld(kind, boundary, text) for kind in KINDS] == [False] * len(KINDS)


WIDEN_TEXTS = ["Lunch with Quentin today.", "Kolacja u Uli w piatek.", "We borrowed Iras car.", "Theo came by.",
               "Tva was late.", "Notes:\n- Tyova plan worked.", "T e o and me.", "Ug0 came.", "\u202eoeT\u202c",
               "テオと会った。", "테오랑 점심.", "Aake rang.", "Phóid is here.", "Ddai called.", "Kaitsu is back."]
WIDEN_GATES = [("Teo Brannick", ("Teo",)), ("Tev Morrant", ("Tev",)), ("Ula Varnell", ("Ula",)),
               ("Ira Example", ("Ira",)), ("Pód Ó Ruairc", ("Pód",)), ("Åke Lindmark", ("Åke",)),
               ("Kai Pellinkari", ("Kai",)), ("Dai Wynne", ("Dai",)), ("Ugo Marz", ("Ugo",))]


def test_v10_only_widens_v9(monkeypatch):
    """Everything the v9 rule (v10 with its hooks off) withholds, v10 withholds; and v10 withholds more."""
    def verdicts():
        return [withheld(kind, gate(*person), text) for person in WIDEN_GATES for text in WIDEN_TEXTS
                for kind in ("journal_entry", "message")]
    v10 = verdicts()
    without_v10(monkeypatch)
    _clear_caches()
    before = verdicts()
    assert all(new or not old for new, old in zip(v10, before))
    assert sum(new and not old for new, old in zip(v10, before)) >= 15


def test_the_readings_never_leave_an_invisible_control_in_the_source():
    """The override and spacing readings name their characters by escape; the module holds no bidi control."""
    source = open(entity_boundary.__file__, encoding="utf-8").read()
    assert not set(source) & {"\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068",
                              "\u2069"}


def test_the_boundary_version_is_v10():
    assert entity_boundary.VERSION == "node-observed-entity-boundary/v10"   # v10: BL-172 (1.5.4)
    current = gate("Tev Morrant", ("Tev",)).revision
    for earlier in ("node-observed-entity-boundary/v8", "node-observed-entity-boundary/v9"):
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(entity_boundary, "VERSION", earlier)
            assert gate("Tev Morrant", ("Tev",)).revision != current
