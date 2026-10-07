"""Snapshot-local Off-limits veto for the registered fact/message family.

This checks observed identities, complete stored text surfaces and contact-linked
conversation context. It is NOT semantic entity coverage or an NER absence proof.
Indirect references with no recorded/name/contact association remain a residual
of this contract. Missing schemas, malformed JSON and unbounded context withhold.
No recipient or model can supply this object, its terms or its context.

Journal rows (NAME_PART_TABLES) are also matched on each PART of a protected person's names and aliases
(a whole word of three letters or more, under the normalisation the whole-term scan applies). The OD-58
held-out check (30 Sep) planted 57 named, aliased, possessive, invisible-character, punctuated and
column-only references and every one was withheld; the one bare-first-name row and the one bare-last-name
row were released, because a whole-name term is one skeleton and a bare part is never equal to it. Private
writing fails closed there. Messages and AI-chat rows keep whole-term matching: their rubric reads a
conversation's context, and a bare first name in a message is the classifier's `protected_content` call,
not this veto's; nothing here widens them. A name part that is also an ordinary word over-withholds journal
entries; that is accepted for this family and `name_part_match_only` lets the census count it under the
unchanged `entity_protected` code. One-edit misspellings remain a residual (4 of 4 released).
Since v4 a name word of two or three letters also withholds through its pet-name forms there (short_variants:
"Zeb" as "Zebby", "Jo" as "Joey"; WS0, 1 Oct): a two-letter word only through its forms, never bare or repeated
("Ma" is not "mama"), and not at all when it has no vowel (a title such as "Dr").
Since v8 (N6, decision D7) every kind is read with the parts of a protected name and all the forms the boundary knows,
not only a journal row: a message, an AI-chat turn, a goal, a fact, a relationship, an interest visit and every
derived text a share can release (the OD-58 split above, whole terms for messages, is withdrawn). One difference
stays, measured on a copy of the owner's node: outside a journal row a BARE part withholds where it is written
capitalised (as a name), because a part that is also an ordinary word written in lower case is that word there (on
that copy 665 of the 692 owner messages the uniform rule would have newly withheld held a part only that way); every
form of a part withholds exactly as in a journal row. A journal row, an interest label and an inferred fact's value
keep the v3 rule (a bare part anywhere).
"""
from __future__ import annotations

import copy
import functools
import html
import re
import sqlite3
import unicodedata
from collections import defaultdict, deque

from .canonical import PolicyError, Rows, digest, digest_stream
from .english_short_words import WORDS_2_3, WORDS_4, WORDS_ENDING_S_3_4

# v3 (candidate 10, OD-58): journal rows also match each part of a protected name (name_parts). v4 (Lane P): a short
# term also matches its pet-name and inflected forms (short_variants), and a word split by an apostrophe letter or
# stretched by a repeated letter, or a form whose last letter is doubled, reads as the word (text_hits). v3 has run on
# the owner's node, so the merged rule is v4 and every index built against v3 re-qualifies. v5 (Lane P2): a short term
# also matches its inflected forms (inflected_forms: Slavic case endings and diminutives, a possessive written without
# its apostrophe) where the token is written as a proper noun in running text (proper_tokens). v6 (Lane P3): a short
# name that is not itself an English word (english_short_words) is a name wherever it is written, so its forms also
# withhold where written capitalised but not as a proper noun (a sentence's first word, capitals in prose), and it
# takes Finnish, Dutch, Basque, Yiddish and Korean endings and a doubled first syllable (named_forms); every
# default-ignorable code point is read through (normalized), and text carrying a Unicode tag character withholds
# outright (TAG_CHARACTERS). v7 (Lane P4): every text is also read transliterated from Cyrillic and Greek, with
# look-alike letters folded and with digits and symbols inside a word read as letters (_readings), and a short name
# that is not an English word also takes Hungarian, Turkish, Baltic, Greek, Romanian, Icelandic and Estonian endings.
# v8 (N6, D7): the name rule and the forms reach every kind (every table and derived text), a name in another script counts in its
# Latin transliteration, short names take the endings two independent blind sets still found released whole on 2 Oct
# (45 cases: Finnish -n, Baltic, Romanian, Icelandic, Slavic possessive adjectives, Greek, Italian and Iberian
# diminutives, Hungarian family plurals, Dutch, Basque, Japanese honorifics), a long name takes its endings and stem
# changes (_v8_long), a journal's people column is read as names only, a word broken by a hyphen at a line end is read
# whole, and an Off-limits name equal to a contact's handle or id protects that contact. Matching only widens.
VERSION = "node-observed-entity-boundary/v8"
# Evidence leaves with no conversational context (evidence_families, IF-5).
CONTEXTLESS_TABLES = frozenset({"journal_entries"})
# The family whose rows first matched each part of a protected name (v3, module docstring). Since v8 every row and
# every derived text is read that way (observe, legacy_veto, mentions_protected); the constant stays for the families
# that cite the journal's rule by name (interest labels, inferred fact values).
NAME_PART_TABLES = frozenset({"journal_entries"})
# Columns that hold names only (v8): every word there is read as a name, whatever its case or place, so a form in lower
# case or opening the list withholds as it would as a proper noun. A journal entry's people column.
NAMES_ONLY_COLUMNS = frozenset({"people"})
# The column of an Off-limits entry that lists which of its aliases are a handle, a username or an id and not a name
# (`features.lifecycle.blackhole.IDENTIFIERS_COLUMN`; written by the step that carries the older per-person excludes).
# Optional: a database, or an entry, without it reads every alias as a name.
IDENTIFIER_ALIASES = "identifier_aliases_json"
# The column that says what of an entry the upgrade carried and the owner has not acted on
# (`features.lifecycle.blackhole.WAITING_COLUMN`). Every share reads such an entry like any other: the default below.
CARRIED_WAITING = "carried_waiting_json"
# A part is a whole word of at least this many letters; an initial or a two-letter particle is never one.
# (The interest lane's label rule, IF-5 section 1.3, uses four; a journal entry is private writing and
# a three-letter given name is common enough that the whole-term scan already treats three as a word.)
MIN_NAME_PART_LETTERS = 3
WORDS = re.compile(r"[^\W_]+")  # the same word pattern the interest lane's `_WORDS` uses
UNAVAILABLE = "entity_protection_lineage_unavailable"
MAX_ROWS = 100_000
MAX_CONTEXT_ROWS = 10_000
MAX_SURFACE_BYTES = 2_097_152
MAX_DEPTH = 32
# A deliberately pinned small set, not a claim of Unicode confusable coverage.
CONFUSABLES = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "м": "m",
    "у": "y", "і": "i", "ј": "j", "Α": "a", "Β": "b", "Ε": "e", "Η": "h", "Ι": "i",
    "Κ": "k", "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Χ": "x",
    "α": "a", "β": "b", "ε": "e", "η": "h", "ι": "i", "κ": "k", "μ": "m", "ν": "n",
    "ο": "o", "ρ": "p", "τ": "t", "χ": "x"})


# Default-ignorable code points (Unicode's Default_Ignorable_Code_Point), read through like the format and mark
# characters `normalized` drops (v6): among them the fillers Python counts as letters (U+115F, U+1160, U+3164,
# U+FFA0), the reserved ignorables and the whole tag block. A tag character is an invisible copy of a printable ASCII
# character, so a name can be spelled in tags alone: text_hits withholds any text that carries one.
_IGNORABLE = dict.fromkeys([0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, *range(0x180B, 0x1810),
                            *range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x2070), 0x3164,
                            *range(0xFE00, 0xFE10), 0xFEFF, 0xFFA0, *range(0xFFF0, 0xFFF9), *range(0x1BCA0, 0x1BCA4),
                            *range(0x1D173, 0x1D17B), *range(0xE0000, 0xE1000)])
TAG_CHARACTERS = re.compile("[" + chr(0xE0000) + "-" + chr(0xE007F) + "]")


def normalized(value: str) -> str:
    value = html.unescape(value).translate(_IGNORABLE).translate(CONFUSABLES)
    value = unicodedata.normalize("NFKD", value).casefold().translate(CONFUSABLES)
    return "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() else ch
                   for ch in value if unicodedata.category(ch) not in {"Mn", "Mc", "Me", "Cf"})


def skeleton(value: str) -> str:
    return "".join(ch for ch in normalized(value) if ch.isalnum())


def name_parts(value: str) -> set:
    """The parts of one name: each whitespace/punctuation-separated word with at least
    MIN_NAME_PART_LETTERS letters, as the skeleton `_hits` gives a row's own words."""
    parts = set()
    for word in WORDS.findall(normalized(value)):
        part = skeleton(word)
        if sum(ch.isalpha() for ch in part) >= MIN_NAME_PART_LETTERS:
            parts.add(part)
    return parts


def short_name_words(value: str) -> set:
    """The words of one name that also withhold through their pet-name forms (short_variants) in NAME_PART_TABLES:
    two or three ASCII letters. A three-letter word is a name part as well, so it also matches bare; a two-letter
    word matches only through its forms ("Jo" as Joey or Josie), never bare or repeated (name_word_variants), so a
    particle stays a particle ("de", "la"). A two-letter word with no vowel is a title or a pair of initials ("Dr",
    "St", "Jr") and takes no forms: "Dr" would make "dry" one."""
    words = set()
    for word in WORDS.findall(normalized(value)):
        part = skeleton(word)
        if (2 <= len(part) < SHORT_TERM_CHARS and part.isascii() and part.isalpha()
                and (len(part) == 3 or not _VOWELS.isdisjoint(part))):
            words.add(part)
    return words


# A term shorter than this matches whole tokens only: initials and short names must not match every occurrence
# inside a larger word ("M.E." in "message"). A longer one matches anywhere in the separator-free text.
SHORT_TERM_CHARS = 4
_VOWELS = frozenset("aeiouy")
# English never doubles these before a pet-name ending.
_UNDOUBLED = frozenset("hjqwxy")
# Apostrophe-like LETTERS (Unicode Lm, and the saltillo): `[^\W_]+` keeps them inside a word, so "Abe\u02bcs" does
# not split into "Abe" and "s" the way "Abe's" and "Abe\u2019s" do.
APOSTROPHE_LETTERS = "\u02b9\u02ba\u02bb\u02bc\u02bd\u02be\u02bf\u02c8\u02ee\ua78c"
_SEPARATORS = re.compile(r"[\s@:/<>]+")
_WORD_WITHOUT_APOSTROPHE = re.compile(f"[^\\W_{APOSTROPHE_LETTERS}]+")
_STRETCHED = re.compile(r"([^\W\d_])\1{2,}")
# A pet-name form written with its last letter doubled ("Abeyy", "Sammyy") reads as the form. Only forms: a doubled
# final vowel is ordinary spelling ("too", "boo", "see"), so reading it once would turn short names into words.
_DOUBLED_ENDINGS = frozenset("aeioy")


@functools.lru_cache(maxsize=4096)
def short_variants(term: str) -> frozenset:
    """The pet-name and inflected forms of one short protected term that also withhold, as whole tokens.

    A protected "Abe" written "Abey", or a "Sam" written "Sammy", is one token that is not the term, so whole-token
    matching alone released it. English builds these forms by adding an ending, so for a term of two or three ASCII
    letters (a skeleton: case-folded, marks and format characters removed) this adds:
      - after a consonant (Sam, Pat, Ben): y, ie, ey, i, s, sy, sie, bo, ji (Katie, Sami, Sams, Patsy, Jimbo,
        Benji). After a vowel and one consonant the consonant also doubles before y, ie, ey, i, o, a (Sammy,
        Eddie, Robbo, Gazza), except h, j, q, w, x and y, which English does not double; a c doubles as ck too
        (Vicky, Becky). A two-letter term takes the doubled forms only (Ally, Eddie, Emma): "An" would make
        "any", "It" "its";
      - after the e of a three-letter term (Abe, Joe, Zoe): y (Abey, Joey); a vowel, a consonant and e (Abe, Eve,
        Ike) also drops the e before ie, i (Abie, Evie, Abi);
      - after any other vowel or y (Jo, Lou, Ty): ey, ie, sie (Joey, Louie, Josie), and a two-letter term doubles
        (Jojo);
      - and each form above with a plural or possessive s (Sammys, Joeys). A bare s follows only a three-letter
        term's last consonant (Sams): after a vowel it makes "has", "was", "yes", "days" and "does".
    Endings that turn common short names into ordinary words are left out: "-so" (also), "-e" (same), "-it"
    (edit), "-in" (join), "-es" (times, sales), "-y" after a, i, o or u (joy, boy, day), and an undoubled -o or
    -a (halo, solo, memo, beta, mega, data). A one-letter term (an initial), a two-letter term with no vowel
    (initials or a title: "T.H." would make "this" and "they", "Dr" "dry") and a term with a digit or another
    script get no forms.
    """
    if not (2 <= len(term) < SHORT_TERM_CHARS and term.isascii() and term.isalpha()):
        return frozenset()
    if len(term) == 2 and _VOWELS.isdisjoint(term):
        return frozenset()
    last = term[-1]
    if last not in _VOWELS:
        forms = set() if len(term) == 2 else {term + ending for ending in ("y", "ie", "ey", "i", "s", "sy", "sie",
                                                                          "bo", "ji")}
        if term[-2] in _VOWELS and last not in _UNDOUBLED:
            for double in (("c", "k") if last == "c" else (last,)):
                forms.update(term + double + ending for ending in ("y", "ie", "ey", "i", "o", "a"))
    elif last == "e" and len(term) == 3:
        forms = {term + "y"}
        # Not after two consonants ("Tre" would give "try"), and never e-drop + y ("Ane" would give "any").
        if term[0] in _VOWELS and term[1] not in _VOWELS:
            forms.update(term[:2] + ending for ending in ("ie", "i"))
    else:
        forms = {term + ending for ending in ("ey", "ie", "sie")}
        if len(term) == 2:
            forms.add(term + term)
    forms.update([form + "s" for form in forms if not form.endswith("s")])
    return frozenset(forms - {term})


@functools.lru_cache(maxsize=256)
def _variants(short_terms: frozenset) -> frozenset:
    return frozenset().union(*map(short_variants, short_terms))


@functools.lru_cache(maxsize=4096)
def name_word_variants(word: str) -> frozenset:
    """short_variants of one journal name word (short_name_words), without a two-letter word's repeat: the bare word
    is never matched (a particle, or a short word such as "Ma" or "Ha"), and its repeat is as ordinary ("mama",
    "haha"). A registered alias keeps its repeat (Jojo)."""
    forms = short_variants(word)
    return forms - {word + word, word + word + "s"} if len(word) == 2 else forms


@functools.lru_cache(maxsize=256)
def _word_variants(words: frozenset) -> frozenset:
    return frozenset().union(*map(name_word_variants, words))


# Inflected forms of a short name (v5). English adds a pet-name ending (short_variants); a Slavic language declines the
# name itself ("u Uli", "z Ulą", "do Janka", "s Ivem"), and a possessive can lose its apostrophe ("Iras car"). These
# endings also make ordinary words ("Ana" would make "any", "Doe" "does", "Wa" "was", "Dan" "Dana"), so a form withholds
# only where the text writes it as a proper noun (proper_tokens): capitalised and, in prose, neither the first word of a
# sentence, line or list item nor a word in capitals, which is more often an acronym ("PII" would be Pia's, "IDE"
# Ida's). Measured on the engine's own English (50,584 lines), these forms would newly withhold, in any case, 109 lines
# for 69 typical short aliases, 543 for 15 short Slavic names and 4,080 for 7 that are common-word stems; capitalised
# anywhere, 66, 72 and 97; as proper nouns, 50, 11 and 15. Finnish and Hungarian case endings, Germanic diminutives and
# the rarer Romance ones (-ico, -illa, -inho, -ette) are left out: on a two- or three-letter name they make English
# words and names that merely start with it (Pasta, Malta, Robert, Melissa, Kitchen, Vanilla).
#
# Case endings. A three-letter name ending in a vowel after a consonant declines on its stem (Ula: ul-), whether it
# ends in a (Uli, Ulu, Ulej, Olyu), o (Iva, Ivovi, Ivem), e or i; one ending in a after another vowel too (Mia: Mii).
_VOWEL_STEM_CASE = ("i", "y", "e", "ie", "o", "u", "ou", "oj", "oy", "ej", "om", "ach", "ami",   # Ula: Uli, Ulu, Ulej
                    "yu", "ju", "oyu", "oju", "eyu")                                             # Ola: Olyu, Oloyu
_O_STEM_CASE = ("a", "owi", "ovi", "em")                                                         # Ivo: Iva, Ivovi, Ivem
_VOWEL_PAIR_CASE = ("i", "u", "ou", "yu", "ju", "ey", "ej", "ei")        # Mia: Mii, Miu, Miej (no -e: Lea would be Lee)
_ADJECTIVAL = ("go", "mu", "m", "ego", "emu")                                    # Joe: Joego, Joem; Ali: Aliego, Alim
_CONSONANT_CASE = ("a", "u", "owi", "ovi", "em", "iem", "om", "ach", "ami", "ou", "ovu", "ova", "ove", "ovy")  # Zan: Zana
# Diminutive suffixes, each with the case endings it takes: Polish, Czech, Slovak and Russian (Ulka, Ulki, Ulce, Ulinka,
# Ulicka, Ulechka, Ulya; Zanek, Zanka, Zankiem, Zankom, Zanko, Zanik, Zanicek, Zanushka; Reosia, Reosiem), and Spanish
# and Italian -ita, -ito, -ina, -ino, -etta and -etto (Anita, Zanito).
_VOWEL_STEM_DIMINUTIVES = (
    *("k" + e for e in ("a", "i", "e", "o", "u", "oy", "oj")), "ce",
    *(d + e for d in ("enk", "echk", "ochk", "ushk", "ink", "ick", "eck", "usk", "unk")
      for e in ("a", "i", "e", "o", "u", "y", "ou", "oy", "oj")), "ence",
    *(d + e for d in ("uni", "usi", "ci", "si", "ni", "y") for e in ("", "a", "e", "u", "o")),
    "ita", "ito", "ina", "ino", "etta")
_CONSONANT_DIMINUTIVES = (
    "ek", *("k" + e for e in ("a", "u", "i", "o", "em", "om", "iem", "owi", "ovi")),
    *(d + e for d in ("i", "usi", "uni") for e in ("o", "a", "u", "em", "owi")), "us",
    "ik", *(d + e for d in ("ik", "ick", "ink", "ousk", "ushk", "echk")
            for e in ("a", "u", "y", "i", "e", "ou", "em", "ovi", "owi")), "icek", "inek", "ousek",
    "ito", "ita", "ino", "ina", "etto", "etta")
_NAME_DIMINUTIVES = ("s", *("si" + e for e in ("", "a", "u", "o", "e", "em", "owi")))                  # Reo: Reosia
_PALATAL = {"d": "dz", "t": "c", "g": "dz", "k": "c", "r": "rz"}                                         # Ada: Adzie
# The first word of a sentence, a line or a list item is capitalised whatever it is. Only the last _OPENING_WINDOW
# characters before a word are read, so a long text costs one pass; a longer run of spaces or bullets reads as not
# opening (the form withholds).
_SENTENCE_START = re.compile(r"(?:(?:\A|\n)[\s\-*\u2022\u00b7>#\d.)]*|[.!?]['\"\u2019\u201d)\]]*\s)\s*['\"\u2018\u201c(\[]*\Z")
_OPENING_WINDOW = 64


@functools.lru_cache(maxsize=4096)
def inflected_forms(term: str) -> frozenset:
    """The inflected forms of one short protected term (two or three ASCII letters with a vowel) that withhold where
    written as a proper noun (text_hits):
      - the name with a possessive or diminutive s, and that diminutive's case forms (Reos, Reosia, Reosiem; Iras,
        Bos);
      - a three-letter name ending in a vowel after a consonant declines on its stem: case endings (Uli, Uly, Ulu,
        Ulie, Uloj, Olyu, Oloyu; Iva, Ivovi, Ivem), a palatalised stem (Ada: Adzie; Ota: Ocie) and diminutives in their
        case forms (Ulka, Ulki, Ulce, Ulunia, Ulusiu, Ulenki, Ulechka, Ulinka, Ulicka, Ulya, Ulita); one ending in e or
        i also declines as an adjective (Joego, Joem; Aliego, Alego, Alim), and one ending in a after another vowel
        on its stem (Mii, Miu, Miej);
      - a name ending in a consonant, or in y after a vowel, takes case endings (Zana, Zanu, Zanowi, Zanem; Raya,
        Rayem), a palatalised locative (Ved: Vedzie) and diminutives in their case forms (Zanek, Zanka, Zankiem,
        Zankom, Zanko, Zanio, Zaniem, Zanusia, Zanik, Zanicka, Zanushka, Zanito).
    No "-e" after a consonant (Czech and Russian only, and it makes "same", "time" and "done").
    """
    if not (2 <= len(term) < SHORT_TERM_CHARS and term.isascii() and term.isalpha()) or _VOWELS.isdisjoint(term):
        return frozenset()
    made = [(term, _NAME_DIMINUTIVES)]
    if term[-1] not in _VOWELS or (term[-1] == "y" and term[-2] in _VOWELS):                    # Zan; Ray, Roy
        made.append((term, _CONSONANT_CASE + _CONSONANT_DIMINUTIVES))
        if term[-1] in _PALATAL and term[-2] in _VOWELS:
            made.append((term[:-1] + _PALATAL[term[-1]], ("ie", "e")))
    elif len(term) == 3 and term[1] not in _VOWELS:                                             # Ula, Ivo, Abe, Ali
        stem = term[:2]
        made.append((stem, _VOWEL_STEM_CASE + _VOWEL_STEM_DIMINUTIVES + (_O_STEM_CASE if term[-1] == "o" else ())
                     + (("ego", "emu") if term[-1] == "i" else ())))
        if term[-1] in "ei":
            made.append((term, _ADJECTIVAL))
        if stem[-1] in _PALATAL:
            made.append((stem[:-1] + _PALATAL[stem[-1]], ("ie", "e")))
    elif len(term) == 3 and term[-1] == "a":                                                    # Mia, Lea
        made.append((term[:2], _VOWEL_PAIR_CASE))
    elif len(term) == 3 and term[-1] in "ei":                                                   # Joe, Lee
        made.append((term, _ADJECTIVAL))
    return frozenset({base + ending for base, endings in made for ending in endings} - {term})


@functools.lru_cache(maxsize=256)
def _inflections(short_terms: frozenset) -> frozenset:
    return frozenset().union(*map(inflected_forms, short_terms))


# v6: a short name that is not itself an English word (english_short_words: "ray", "day" and "eve" are) is a name
# wherever it is written, so its forms also withhold where written capitalised but not as a proper noun: a
# sentence's first word ("Ilos car is red"), a word in capitals. A form that is itself a short English word ("Was",
# "Days") does not, and an English word's forms keep v5's place ("Rays of light" opens a sentence). Such a name also
# takes the endings a multilingual owner writes on a name and a doubled first syllable (Mimi for Mia).
_FOREIGN_ENDINGS = (
    "lle", "lla", "lta", "ssa", "sta", "ksi",       # Finnish: Ilolle, Ilossa
    "tje", "je", "pje", "etje",                      # Dutch: Ilotje
    "ren", "rekin", "ri", "ra", "ko", "rentzat",     # Basque: Iloren, Ilorekin
    "ele", "le", "ke", "nyu",                        # Yiddish: Ilole, Iloke
    "ya", "ssi", "nim", "ah", "iya",                 # Korean: Iloya, Ilossi
    # v7 (Lane P4): Hungarian (Ilonak, Ilohoz), Turkish without its apostrophe (Ilonun, Ilodan, Ilolar), Lithuanian
    # (Iloje), Greek written in Latin (Ilaki), Romanian (Ilului), Estonian (Ilosse); on a stem too (Ula: Ulanak)
    "nak", "nek", "val", "vel", "hoz", "hez", "nal", "nel", "tol", "rol", "bol", "ban", "ben", "ert", "kor", "kent",
    "nin", "nun", "den", "dan", "ten", "tan", "yle", "yla", "ler", "lar", "cik", "cuk", "cim", "oje", "eje", "aki",
    "akis", "oula", "itsa", "ului", "uta", "sse")
# v7: the one- and two-letter case endings of those languages (Hungarian -t, -ba, -re; Turkish -e, -a, -in, -de;
# Lithuanian and Latvian -as, -ui, -os; Greek -ou, -i; Romanian -ul; Icelandic -ar, -ur; Estonian -ga, -st) make
# English words that open sentences ("Time", "Most", "More", "URL"), so on a name that is not an English word they
# withhold only where written as a proper noun, like v5's forms, and never as a short English word
# (short_named_forms). Not a single consonant (Finnish -n, Hungarian -t, Estonian -l, -d): on a name it makes other
# names and places that are proper nouns too ("Iran" for Ira, "Abel" for Abe).
_SHORT_FOREIGN_ENDINGS = (
    "ot", "et", "at", "ba", "be", "re", "ig", "in", "un", "e", "a", "ye", "de", "da", "te", "ta", "la", "i", "yi",
    "u", "yu", "as", "is", "ys", "us", "o", "ui", "ai", "os", "ei", "am", "ou", "ul", "ii", "ar", "ur", "nu", "ni",
    "na", "ga", "lt", "st", "ks")


@functools.lru_cache(maxsize=4096)
def named_forms(term: str) -> frozenset:
    """The forms of a short protected term that is not itself an English word which withhold wherever written
    capitalised (text_hits): its inflected_forms, the _FOREIGN_ENDINGS on the name (and on its stem, for a
    three-letter name ending in a vowel after a consonant), and the name or its first two letters doubled; never a
    short English word (english_short_words)."""
    if (term in WORDS_2_3 or not (2 <= len(term) < SHORT_TERM_CHARS and term.isascii() and term.isalpha())
            or _VOWELS.isdisjoint(term)):
        return frozenset()
    bases = [term] + ([term[:2]] if len(term) == 3 and term[-1] in _VOWELS and term[1] not in _VOWELS else [])
    forms = set(inflected_forms(term)) | {base + ending for base in bases for ending in _FOREIGN_ENDINGS}
    forms.update((term * 2, term[:2] * 2))
    if term[-1] not in _VOWELS:                                       # Hungarian -val/-vel after a consonant: Zannal
        forms.update(term + term[-1] + ending for ending in ("al", "el"))
    return frozenset(forms - {term} - WORDS_2_3 - WORDS_ENDING_S_3_4)


@functools.lru_cache(maxsize=256)
def _named(short_terms: frozenset) -> frozenset:
    return frozenset().union(*map(named_forms, short_terms))


@functools.lru_cache(maxsize=4096)
def short_named_forms(term: str) -> frozenset:
    """The _SHORT_FOREIGN_ENDINGS on a short protected term that is not an English word (and on its stem, as
    named_forms), less its named_forms and any short English word: these withhold only where written as a proper
    noun (text_hits)."""
    if not named_forms(term):
        return frozenset()
    bases = [term] + ([term[:2]] if len(term) == 3 and term[-1] in _VOWELS and term[1] not in _VOWELS else [])
    forms = {base + ending for base in bases for ending in _SHORT_FOREIGN_ENDINGS}
    return frozenset(forms - named_forms(term) - {term} - _ENGLISH_UP_TO_4)


# Every short English word a one- or two-letter ending could make: two to four letters, and the plurals of the two-
# and three-letter ones ("ids", "ads", "days").
_ENGLISH_UP_TO_4 = WORDS_2_3 | WORDS_ENDING_S_3_4 | WORDS_4 | frozenset(word + "s" for word in WORDS_2_3)


@functools.lru_cache(maxsize=256)
def _short_named(short_terms: frozenset) -> frozenset:
    return frozenset().union(*map(short_named_forms, short_terms))


def _capital_tokens(text: str) -> tuple:
    """(proper, capitalised, lower): the tokens of the words one text writes as a proper noun (proper_tokens), of every
    word it writes with a capital first letter, and (v8) of every word it writes in lower case."""
    raw = unicodedata.normalize("NFKD", html.unescape(text).translate(_IGNORABLE))
    raw = "".join(ch for ch in raw if unicodedata.category(ch) not in {"Mn", "Mc", "Me", "Cf"})
    words = list(WORDS.finditer(raw))
    prose = any(match.group(0)[0].islower() for match in words)
    proper, capitalised, lower = set(), set(), set()
    for match in words:
        word = match.group(0)
        if word[0].isupper():
            tokens = tokens_of(normalized(word))
            capitalised.update(tokens)
            if not (prose and (len(word) > 1 and word.isupper() or _SENTENCE_START.search(
                    raw, max(0, match.start() - _OPENING_WINDOW), match.start()))):
                proper.update(tokens)
        elif word.islower():
            lower.update(tokens_of(normalized(word)))
    return proper, capitalised, lower


def proper_tokens(text: str) -> set:
    """The tokens of the words one text writes as a proper noun: a capital first letter (before case folding and
    confusable mapping, after invisible and combining characters are removed), not the first word of a sentence, a
    line or a list item, where every word is capitalised, and not a word written in capitals (an acronym). A text with
    no word in lower case is not prose (a people column, a name list, a field value): there every capitalised word
    counts."""
    return _capital_tokens(text)[0]


# v7 (Lane P4): the boundary also reads a text three more ways, each only adding matches (_readings): transliterated
# from Cyrillic and Greek, with stroked letters spelled out (a name written in another script: an independent blind
# set wrote Slavic and Greek case forms that way), with look-alike letters folded beyond CONFUSABLES (a Cyrillic or
# Greek letter that looks Latin), and with digits and symbols inside a word read as the letters they stand for (0 o,
# 1 i or l, 3 e, 4 a, 5 s, 7 t, 8 b, 9 g, @ a, $ s, ! i, | i or l), only in a word that also has a letter, so a plain
# number reads as written.
_TRANSLITERATION = {
    0x00C6: 'Ae', 0x00D0: 'D', 0x00D8: 'O', 0x00DE: 'Th', 0x00E6: 'ae', 0x00F0: 'd', 0x00F8: 'o', 0x00FE: 'th',
    0x0110: 'D', 0x0111: 'd', 0x0126: 'H', 0x0127: 'h', 0x0131: 'i', 0x0141: 'L', 0x0142: 'l', 0x0152: 'Oe',
    0x0153: 'oe', 0x0166: 'T', 0x0167: 't', 0x0180: 'b', 0x019A: 'l', 0x0268: 'i', 0x0289: 'u', 0x0391: 'A',
    0x0392: 'V', 0x0393: 'G', 0x0394: 'D', 0x0395: 'E', 0x0396: 'Z', 0x0397: 'I', 0x0398: 'Th', 0x0399: 'I',
    0x039A: 'K', 0x039B: 'L', 0x039C: 'M', 0x039D: 'N', 0x039E: 'X', 0x039F: 'O', 0x03A0: 'P', 0x03A1: 'R',
    0x03A3: 'S', 0x03A4: 'T', 0x03A5: 'Y', 0x03A6: 'F', 0x03A7: 'Ch', 0x03A8: 'Ps', 0x03A9: 'O', 0x03B1: 'a',
    0x03B2: 'v', 0x03B3: 'g', 0x03B4: 'd', 0x03B5: 'e', 0x03B6: 'z', 0x03B7: 'i', 0x03B8: 'th', 0x03B9: 'i',
    0x03BA: 'k', 0x03BB: 'l', 0x03BC: 'm', 0x03BD: 'n', 0x03BE: 'x', 0x03BF: 'o', 0x03C0: 'p', 0x03C1: 'r',
    0x03C2: 's', 0x03C3: 's', 0x03C4: 't', 0x03C5: 'y', 0x03C6: 'f', 0x03C7: 'ch', 0x03C8: 'ps', 0x03C9: 'o',
    0x0401: 'E', 0x0402: 'Dj', 0x0404: 'Ye', 0x0406: 'I', 0x0407: 'Yi', 0x0408: 'J', 0x0409: 'Lj', 0x040A: 'Nj',
    0x040B: 'C', 0x040E: 'U', 0x040F: 'Dz', 0x0410: 'A', 0x0411: 'B', 0x0412: 'V', 0x0413: 'G', 0x0414: 'D',
    0x0415: 'E', 0x0416: 'Zh', 0x0417: 'Z', 0x0418: 'I', 0x0419: 'Y', 0x041A: 'K', 0x041B: 'L', 0x041C: 'M',
    0x041D: 'N', 0x041E: 'O', 0x041F: 'P', 0x0420: 'R', 0x0421: 'S', 0x0422: 'T', 0x0423: 'U', 0x0424: 'F',
    0x0425: 'Kh', 0x0426: 'Ts', 0x0427: 'Ch', 0x0428: 'Sh', 0x0429: 'Shch', 0x042A: '', 0x042B: 'Y', 0x042C: '',
    0x042D: 'E', 0x042E: 'Yu', 0x042F: 'Ya', 0x0430: 'a', 0x0431: 'b', 0x0432: 'v', 0x0433: 'g', 0x0434: 'd',
    0x0435: 'e', 0x0436: 'zh', 0x0437: 'z', 0x0438: 'i', 0x0439: 'y', 0x043A: 'k', 0x043B: 'l', 0x043C: 'm',
    0x043D: 'n', 0x043E: 'o', 0x043F: 'p', 0x0440: 'r', 0x0441: 's', 0x0442: 't', 0x0443: 'u', 0x0444: 'f',
    0x0445: 'kh', 0x0446: 'ts', 0x0447: 'ch', 0x0448: 'sh', 0x0449: 'shch', 0x044A: '', 0x044B: 'y', 0x044C: '',
    0x044D: 'e', 0x044E: 'yu', 0x044F: 'ya', 0x0451: 'e', 0x0452: 'dj', 0x0454: 'ye', 0x0456: 'i', 0x0457: 'yi',
    0x0458: 'j', 0x0459: 'lj', 0x045A: 'nj', 0x045B: 'c', 0x045E: 'u', 0x045F: 'dz', 0x0490: 'G', 0x0491: 'g'}
_LOOKALIKE_LETTERS = {
    0x00C6: 'Ae', 0x00D0: 'D', 0x00D8: 'O', 0x00DE: 'Th', 0x00E6: 'ae', 0x00F0: 'd', 0x00F8: 'o', 0x00FE: 'th',
    0x0110: 'D', 0x0111: 'd', 0x0126: 'H', 0x0127: 'h', 0x0131: 'i', 0x0141: 'L', 0x0142: 'l', 0x0152: 'Oe',
    0x0153: 'oe', 0x0166: 'T', 0x0167: 't', 0x0180: 'b', 0x019A: 'l', 0x0268: 'i', 0x0289: 'u', 0x0391: 'A',
    0x0392: 'B', 0x0395: 'E', 0x0396: 'Z', 0x0397: 'H', 0x0399: 'I', 0x039A: 'K', 0x039C: 'M', 0x039D: 'N',
    0x039F: 'O', 0x03A1: 'P', 0x03A4: 'T', 0x03A5: 'Y', 0x03A7: 'X', 0x03B1: 'a', 0x03B2: 'b', 0x03B3: 'y',
    0x03B5: 'e', 0x03B7: 'n', 0x03B9: 'i', 0x03BA: 'k', 0x03BD: 'v', 0x03BF: 'o', 0x03C1: 'p', 0x03C4: 't',
    0x03C5: 'u', 0x03C7: 'x', 0x03C9: 'w', 0x03F2: 'c', 0x03F3: 'j', 0x03F9: 'C', 0x0405: 'S', 0x0406: 'I',
    0x0408: 'J', 0x0410: 'A', 0x0412: 'B', 0x0415: 'E', 0x041A: 'K', 0x041C: 'M', 0x041D: 'H', 0x041E: 'O',
    0x0420: 'P', 0x0421: 'C', 0x0422: 'T', 0x0423: 'Y', 0x0425: 'X', 0x0430: 'a', 0x0432: 'b', 0x0433: 'r',
    0x0435: 'e', 0x043A: 'k', 0x043C: 'm', 0x043D: 'h', 0x043E: 'o', 0x043F: 'n', 0x0440: 'p', 0x0441: 'c',
    0x0442: 't', 0x0443: 'y', 0x0445: 'x', 0x044C: 'b', 0x0451: 'e', 0x0455: 's', 0x0456: 'i', 0x0458: 'j',
    0x04AE: 'Y', 0x04AF: 'y', 0x04BA: 'H', 0x04BB: 'h', 0x04C0: 'I', 0x04CF: 'l', 0x0501: 'd', 0x051A: 'Q',
    0x051B: 'q', 0x051C: 'W', 0x051D: 'w'}
_SCRIPTED = re.compile("[" + "".join(re.escape(chr(cp)) for cp in sorted(set(_TRANSLITERATION) | set(_LOOKALIKE_LETTERS)))
                       + "]")
_LEET = {"0": "o", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g", "@": "a", "$": "s", "!": "i"}
_LEET_CHARS = frozenset(_LEET) | {"1", "|"}
_CHUNK = re.compile(r"\S+")
_EDGE_PUNCTUATION = ".,;:?\"'()[]{}<>"


def _deleeted(text: str, one: str) -> str:
    """The text with every digit or symbol of _LEET (and 1 and | as `one`) read as a letter inside each word of four
    or more characters made only of letters (two at least) and those symbols, edge punctuation aside; other words as
    written. A shorter word ("d1", "m3", "k8s") or one with any other character (an id, a version) is not a name."""
    table = dict(_LEET, **{"1": one, "|": one})

    def word(match):
        value = match.group(0)
        core = value.strip(_EDGE_PUNCTUATION)
        if (len(core) < 4 or sum(ch.isalpha() for ch in core) < 2 or _LEET_CHARS.isdisjoint(core)
                or not all(ch.isalpha() or ch in _LEET_CHARS for ch in core)):
            return value
        return value.replace(core, "".join(table.get(ch, ch) for ch in core))
    return _CHUNK.sub(word, text)


# v8: a word broken across a line by a hyphen ("Ostwyn-" at a line end, "del" on the next) reads whole as well.
_LINE_END_HYPHEN = re.compile(r"(?<=[^\W\d_])-[ \t]*\r?\n[ \t]*(?=[^\W\d_])")


def _readings(text: str):
    """The text as written, then each further reading v7 adds that differs from it (see above); since v8 the same
    readings of the text with every word broken at a line-end hyphen joined again."""
    yield text
    seen = {text}
    bases = [text]
    if "\n" in text and _LINE_END_HYPHEN.search(text):
        bases.append(_LINE_END_HYPHEN.sub("", text))
    further = bases[1:]
    for base in bases:
        if _SCRIPTED.search(base):
            decomposed = unicodedata.normalize("NFKD", base)
            further += [decomposed.translate(_TRANSLITERATION), decomposed.translate(_LOOKALIKE_LETTERS)]
        if not _LEET_CHARS.isdisjoint(base):
            further += [_deleeted(base, "i"), _deleeted(base, "l")]
    for reading in further:
        if reading not in seen:
            seen.add(reading)
            yield reading


# --- v8 (N6, D7): the shapes two blind sets still found released whole on 2 Oct ------------------------------------
# Blind sets 3 and 5 (independent authors, invented people) released 45 journal entries whole under v7, each naming a
# protected person only by a form v7 did not know. Each class below is one of those shapes, pinned with coined names in
# every kind a share can release (tests/permissions_v2/test_entity_boundary_v8.py).
#
# A short name (the words v5 reads: a short alias, a journal's three-letter name word), ending in a vowel:
_V8_FINNISH = ("n", "lla", "lle", "lta", "ssa", "sta", "ksi")            # genitive and locative cases: Uvon, Ayusta
_V8_HUNGARIAN = ("nak", "nek", "nal", "nel", "val", "vel", "hoz", "hez", "tol", "rol", "bol", "ban", "ben", "ert")
_V8_IBERIAN = ("zinho", "zinha", "zito", "zita", "cito", "cita", "quinho", "quinha")   # Portuguese, Spanish: Lequinha
# Any short name of three letters:
_V8_FAMILY = ("ek", "eknel", "eknek", "ekhez", "ekkel", "ektol", "eket", "ekre", "ekben",   # Hungarian family: Iluek
              "eknal", "eknak", "ekhoz", "ekkal", "ekban")                                # back-vowel harmony: -eknal
_V8_DUTCH = ("tje", "tjes", "je", "jes", "pje", "pjes", "kje", "kjes")     # Dompje
_V8_DUTCH_DOUBLED = ("etje", "etjes")                                     # after the last consonant doubled: Bemmetje
_V8_BASQUE = ("txu", "txo", "tto", "txi")                                 # Iletxu
_V8_HONORIFICS = ("chan", "kun", "sama", "san", "chin")                   # one word with the name: Suochan
_V8_IBERIAN_CONSONANT = ("cito", "cita", "zinho", "zinha", "inho", "inha")  # after a consonant (-ito, -ita are v5's)
# On the stem of a three-letter name ending in a vowel after a consonant (Ava: av-), beside v5's case endings:
_V8_STEM_CASE = ("a", "ey", "ei", "oi", "os", "ai")    # Icelandic Era (Eri); Russian Taei; Romanian Avei; Lithuanian Agos, Alai
_V8_POSSESSIVE = ("in", "ina", "ine", "inu", "ino", "iny", "ini", "inom", "inoj", "inoi", "inoy", "inou", "inog", "inogo",
                  "inomu", "inych", "inykh", "inym", "inim", "inoyu", "inoiu")  # possessive adjectives: Abine, Avine, Tainogo
_V8_STEM_DIMINUTIVES = (
    "ute", "utes", "yte", "ytes", "ele", "eles", "uke", "ukas",           # Lithuanian: Alute, Alutes
    "ica", "uca", "uta",                                                  # Romanian
    "etto", "etta", "ello", "ella", "uccio", "uccia",                     # Italian (a g or c stem takes an h first: Ighetto)
    "inho", "inha", "illo", "ucho", "ucha",                               # Iberian
    *(d + e for d in ("k", "enk", "echk", "ochk", "ushk", "ink", "ick", "eck", "usk", "unk") for e in ("oi", "ei")))
# Greek diminutives on the stem of any three-letter name ending in a vowel (Zia: zi-; v7 reads Greek ου as "oy").
_V8_GREEK = ("oula", "oulas", "oyla", "oylas", "oulis", "oylis", "itsa", "itsas", "akis", "aki")
# Two-letter English words common enough that their s-form is an ordinary word written in lower case ("has", "ups").
_COMMON_TWO_LETTER = frozenset(
    "ad ah am an as at aw ax be by do eh em en er ex go ha he hi hm ho id if in is it la lo ma me mi mm my no of oh ok "
    "on or ow ox pa pi re so ti to uh um up us we ya ye yo".split())
# The k of a Slavic diminutive on v5's stem forms (Ezka, Taechka): such a form also withholds in lower case.
_DIMINUTIVE_MARKS = ("k", "enk", "echk", "ochk", "ushk", "ink", "ick", "eck", "usk", "unk")


# English words that follow a word without making it a possessor: an ordinary plural or verb ("Rays of light", "the
# eds is", "the otis were late") and a place name ("News from Iran.") end a phrase or come before one of these, while a
# genitive comes before what it owns ("Uvon ladder", "Amas car", "jos hammer") or a postposition ("Akon kanssa").
_FUNCTION_WORDS = frozenset("""
    a an the this that these those some any each every no all both either neither other another such
    i me my mine we us our ours you your yours he him his she her hers it its they them their theirs one ones
    who whom whose which what there here
    is am are was were be been being have has had having do does did done will would shall should can could may might
    must ought get gets got
    of in on at to for from with by about as into onto over under after before between through during without within
    against among around near off up down out upon across along behind beyond toward towards via per than like unlike
    and or but nor so yet if then because while when where though although unless until since whether
    not also too very just only even still again later ago now soon once ever never always often already instead
""".split())


@functools.lru_cache(maxsize=4096)
def v8_forms(term: str) -> tuple:
    """(proper, named, lower, genitive): the forms v8 adds for one short protected word (two or three ASCII letters).

    - proper: withhold where written as a proper noun (v5's place), for every such name, English word or not;
    - named: withhold wherever capitalised (v6's place), for a name that is not an English word, less English words;
    - lower: withhold written in lower case as well, for a name that is not an English word: a form on a foreign ending
      of three letters or more (Finnish and Hungarian cases, Dutch, Basque, Greek and Iberian diminutives,
      honorifics) or a Slavic k-diminutive (ezka), never an English word of up to four letters. Not the -es plural,
      possessive adjectives or Romance diminutives: in lower case they make English words of five letters or more,
      which no list here holds ("loses", "urine", "amino");
    - genitive: (any, proper, opening, lower) sets of a genitive that withholds only where the next word is not an
      English function word (_genitive_hits): the Finnish genitive (Uvon, Akon), anywhere for a name that is not an
      English word and as a proper noun for one that is; and the s-form, a possessive without its apostrophe, where it
      opens a sentence (Amas car) for any three-letter name, and in lower case (jos hammer) for a three-letter
      name that is not an English word and a two-letter name that is not a common one ("ha" makes "has").
    A two-letter name ending in a vowel takes the Iberian endings (Lequinha), one with no vowel only an s (Zrs). Left
    out, because on a short name they make other names: Finnish and Hungarian endings after a consonant (Melissa,
    Robert, Robin), Finnish endings on a two-letter name (Malta), and an -e on any stem (Lee)."""
    none = frozenset()
    if not (2 <= len(term) < SHORT_TERM_CHARS and term.isascii() and term.isalpha()):
        return none, none, none, (none, none, none, none)
    english = term in WORDS_2_3
    proper, lower = set(), set()
    possessive = {term + "s"} - WORDS_2_3 - WORDS_ENDING_S_3_4
    if len(term) == 2:
        if _VOWELS.isdisjoint(term):
            proper.add(term + "s")                                        # Zrs
        elif term[-1] in _VOWELS:
            proper.update(term + ending for ending in _V8_IBERIAN)        # Lequinha
            if not english:
                lower.update(proper)
        poss_lower = possessive if term not in _COMMON_TWO_LETTER and not _VOWELS.isdisjoint(term) else set()
        named = none if english else frozenset(proper) - _ENGLISH_UP_TO_4
        return (frozenset(proper), named, frozenset(lower) - _ENGLISH_UP_TO_4,
                (none, none, none, frozenset(poss_lower)))
    stem = term[:2]
    genitive = set()
    if term[-1] in _VOWELS:
        genitive = {term + "n"} - _ENGLISH_UP_TO_4                        # Uvon; not Eve's "even"
        on_name = _V8_FINNISH[1:] + _V8_HUNGARIAN + _V8_FAMILY + _V8_DUTCH + _V8_BASQUE + _V8_HONORIFICS + _V8_IBERIAN
        proper.update(term + ending for ending in on_name)
        proper.update(stem + ending for ending in _V8_GREEK)
        lower.update(term + ending for ending in on_name if len(ending) >= 3)
        lower.update(stem + ending for ending in _V8_GREEK)
        if term[1] not in _VOWELS:                                        # Ava, Igo: a consonant stem
            proper.update(stem + ending for ending in _V8_STEM_CASE + _V8_POSSESSIVE + _V8_STEM_DIMINUTIVES)
            if stem[-1] in "gc":                                          # Igo: Ighetto, Ighino
                proper.update(stem + "h" + ending for ending in ("etto", "etta", "ello", "ella", "ino", "ina", "i", "e"))
            lower.update(form for form in inflected_forms(term)
                         if form[2:].startswith(_DIMINUTIVE_MARKS) and len(form) - 2 >= 2)
    else:
        on_name = _V8_FAMILY + _V8_DUTCH + _V8_BASQUE + _V8_HONORIFICS + _V8_IBERIAN_CONSONANT
        proper.update(term + ending for ending in on_name)
        if term[-2] in _VOWELS and term[-1] not in _UNDOUBLED:
            proper.update(term + term[-1] + ending for ending in _V8_DUTCH_DOUBLED)
        if term[-1] in "sxz":
            proper.add(term + "es")                                       # a family plural: Toses
        lower.update(form for form in proper if len(form) - len(term) >= 3 and not form.endswith("es"))
    proper.discard(term)
    named = none if english else frozenset(proper) - _ENGLISH_UP_TO_4
    lower = none if english else frozenset(form for form in lower if len(form) >= 4) - _ENGLISH_UP_TO_4
    genitive_any, genitive_proper = (none, frozenset(genitive)) if english else (frozenset(genitive), none)
    return (frozenset(proper), named, lower,
            (genitive_any, genitive_proper, frozenset(possessive), none if english else frozenset(possessive)))


@functools.lru_cache(maxsize=256)
def _v8_short(short_terms: frozenset) -> tuple:
    """v8_forms over these short words: (proper, named, lower, (genitive any, proper, opening, lower))."""
    sets = [set() for _ in range(7)]
    for term in short_terms:
        proper, named, lower, genitive = v8_forms(term)
        for index, forms in enumerate((proper, named, lower, *genitive)):
            sets[index] |= forms
    frozen = [frozenset(forms) for forms in sets]
    return frozen[0], frozen[1], frozen[2], tuple(frozen[3:])


_SENTENCE_BREAK = re.compile(r"[.!?;:\n]")


def _genitive_hits(text: str, genitive: tuple) -> bool:
    """Whether a genitive form of v8_forms stands where it is read as one (see there): followed by a word, in the same
    sentence, that is not an English function word. (In a names-only column every form counts: _reading_hits.)"""
    any_place, proper_only, opening, lower_only = genitive
    raw = unicodedata.normalize("NFKD", html.unescape(text).translate(_IGNORABLE))
    raw = "".join(ch for ch in raw if unicodedata.category(ch) not in {"Mn", "Mc", "Me", "Cf"})
    words = list(WORDS.finditer(raw))
    prose = any(match.group(0)[0].islower() for match in words)
    for index, match in enumerate(words):
        word = match.group(0)
        token = skeleton(word)
        if token not in any_place and token not in proper_only and token not in opening and token not in lower_only:
            continue
        following = words[index + 1] if index + 1 < len(words) else None
        if following is None or _SENTENCE_BREAK.search(raw, match.end(), following.start()):
            continue
        after = following.group(0)
        if not any(ch.isalpha() for ch in after) or after.casefold() in _FUNCTION_WORDS:
            continue
        if word[0].islower():
            place = "lower"
        elif prose and (len(word) > 1 and word.isupper() or _SENTENCE_START.search(
                raw, max(0, match.start() - _OPENING_WINDOW), match.start())):
            place = "caps" if len(word) > 1 and word.isupper() else "opening"
        else:
            place = "proper"
        if (token in any_place or (token in proper_only and place == "proper") or (token in opening and place == "opening")
                or (token in lower_only and place == "lower")):
            return True
    return False


# A long name (a part of four letters or more: a first name, a surname, a long alias) is matched whole anywhere when it
# is a whole term, and as a whole word when it is a part; neither reads an ending or a stem change ("Hallowmeers",
# "Ilmaraksen", "Vasquinzinho", "Torpon", a first name written in Cyrillic and declined). v8 compares long words under
# a loose spelling (the same letters whatever the language or transliteration: ph and f, c and k, w and v, y and i, the
# Russian yu and u, a doubled letter once) and withholds, where written as a proper noun:
#   - the word with an ending (_LONG_ENDINGS; a doubled letter read once also gives Finnish gradation: Torppo, Torpon);
#   - a word that starts with its stem (the word less a last vowel, s or m: Ilmaras, Ilmaraksen; Vasquim, Vasquinzinho),
#     when the stem keeps five letters or more and at most seven letters follow it;
#   - for a name ending in -ya or -ja (Taya: how Russian and Serbian soft-stem names transliterate), v5's and
#     v8's stem forms on what precedes it (Taechka, Taei, Tainogo).
# Not "ch" and "k": that makes another name a form ("Ulricha", Ulrich's genitive, would read as Ulrike's).
_LOOSE_PAIRS = (("ph", "f"), ("th", "t"), ("kh", "h"), ("ck", "k"), ("qu", "k"), ("q", "k"), ("x", "ks"), ("w", "v"),
                ("yu", "u"), ("ya", "a"), ("ye", "e"), ("yo", "o"), ("y", "i"), ("j", "i"))
_C_NOT_CH = re.compile(r"c(?!h)")
_DOUBLED_LETTER = re.compile(r"(.)\1+")
_LONG_ENDINGS = ("s", "es", "n", "en", "in", "a", "e", "i", "o", "u", "y", "ie", "lla", "lle", "lta", "ssa", "sta", "ksi",
                 "na", "ta", "ova", "ovi", "ovo", "em", "om", "ak", "ek", "nak", "nek", "val", "vel", "hoz", "hez", "nal",
                 "nel", "tol", "ban", "ben", "ei", "ul", "ului", "as", "is", "us", "os", "ui", "ai", "ou", "ita", "ito",
                 "ina", "ino", "inho", "inha", "zinho", "zinha", "tje", "je", "pje", "ke", "ka", "ko", "chen", "lein",
                 "sen", "ksen")
_LONG_STEM_FINALS = frozenset("aeiouysm")
_LONG_PREFIX_MIN = 5
_LONG_PREFIX_TAIL = 7


@functools.lru_cache(maxsize=65536)
def loose(word: str) -> str:
    """One skeleton word with the spellings that differ between languages and transliterations made the same (v8):
    ph and f, th and t, Russian kh and h, ck, q, qu and c (not in ch) and k, x and ks, w and v, Russian yu, ya, ye, yo
    and u, a, e, o, y and j and i, and a doubled letter read once."""
    for old, new in _LOOSE_PAIRS:
        word = word.replace(old, new)
    return _DOUBLED_LETTER.sub(r"\1", _C_NOT_CH.sub("k", word))


@functools.lru_cache(maxsize=256)
def _v8_long(parts: frozenset) -> tuple:
    """(loose forms, loose stems, soft-stem forms) of these name parts (see above)."""
    forms, stems, soft = set(), set(), set()
    for part in parts:
        if not (part.isascii() and part.isalpha()) or len(part) < SHORT_TERM_CHARS:
            continue
        key = loose(part)
        if len(key) >= SHORT_TERM_CHARS:
            forms.update(loose(part + ending) for ending in _LONG_ENDINGS)
            stem = key[:-1] if part[-1] in _LONG_STEM_FINALS else key
            if len(stem) >= _LONG_PREFIX_MIN:
                stems.add(stem)
            if part.endswith("nen") and len(part) >= 6:                  # Finnish -nen: Virtanen, Virtasen
                stems.add(loose(part[:-3] + "s"))
        if part.endswith(("ya", "ja")) and len(part) >= 4:
            base = part[:-2]
            soft.update(base + ending for ending in _VOWEL_STEM_CASE + _VOWEL_STEM_DIMINUTIVES + _V8_STEM_CASE
                        + _V8_POSSESSIVE + _V8_STEM_DIMINUTIVES)
    return frozenset(forms), tuple(sorted(stem for stem in stems if len(stem) >= _LONG_PREFIX_MIN)), frozenset(soft - parts)


def _long_tokens(tokens, forms: frozenset, stems: tuple) -> set:
    """The tokens that are a long name's ending or stem form under the loose spelling (case and place not read)."""
    if not forms and not stems:
        return set()
    hits = set()
    for token in tokens:
        if len(token) < SHORT_TERM_CHARS or not token.isalpha():
            continue
        key = loose(token)
        if key in forms or any(key.startswith(stem) and len(key) - len(stem) <= _LONG_PREFIX_TAIL for stem in stems):
            hits.add(token)
    return hits


def name_spellings(value) -> tuple:
    """A name as written and, when it has letters of another script (Cyrillic, Greek) or stroked letters, also as v7's
    readings transliterate a text (v8), so the name's Latin spelling, its parts and its forms are matched there."""
    if isinstance(value, str) and _SCRIPTED.search(value):
        latin = unicodedata.normalize("NFKD", value).translate(_TRANSLITERATION)
        if latin != value:
            return (value, latin)
    return (value,)


def split_terms(terms):
    """(short terms, long terms): see SHORT_TERM_CHARS."""
    long_terms = [term for term in terms if len(term) >= SHORT_TERM_CHARS]
    return frozenset(terms).difference(long_terms), long_terms


def text_hits(text: str, short_terms: frozenset, long_terms, *, parts=frozenset(), part_words=frozenset(),
              names_only=False, bare_parts_anywhere=True, whole_terms=frozenset()) -> bool:
    """Whether one text carries an Off-limits term: a long term anywhere in its separator-free form (which catches
    URLs and invisible punctuation), a short term or one of its short_variants as a whole token (a form also with
    its last vowel or y doubled), any of `parts` (the name parts, NAME_PART_TABLES) as a whole token,
    never inside a longer word, and the forms of `part_words` (the two- and three-letter name words,
    short_name_words; name_word_variants) the same way as a short term's, but never a two-letter word bare. Since
    v5, also a short term's inflected_forms (and a three-letter name word's) where written as a proper noun
    (proper_tokens), and since v6 its named_forms wherever written capitalised. Any text carrying a Unicode tag
    character withholds outright (v6): tags are invisible, and a name can be spelled in them alone. Since v7 every one
    of the text's _readings is matched this way. Since v8 also v8_forms (as a proper noun, capitalised or in lower
    case, as each says) and the long-name forms of `parts` (_v8_long, as a proper noun); with `names_only` (a column
    that holds names, NAMES_ONLY_COLUMNS) every form withholds whatever its case or place. Without
    `bare_parts_anywhere` (v8, every kind but a journal row) a bare part withholds where it is written capitalised,
    or in a names-only column: a part that is also an ordinary word ("young", "rose") written in lower case is that
    word there, while every form of a part still withholds as it does in a journal row.
    `whole_terms` are identifiers that match only as themselves (a handle or a username of letters only, review
    R2-M3): each withholds where it is a whole token of the text, whatever its length, and takes no form."""
    if TAG_CHARACTERS.search(text):
        return True
    return any(_reading_hits(reading, short_terms, long_terms, parts, part_words, names_only, bare_parts_anywhere,
                             whole_terms)
               for reading in _readings(text))


def _reading_hits(text: str, short_terms: frozenset, long_terms, parts, part_words, names_only=False,
                  bare_parts_anywhere=True, whole_terms=frozenset()) -> bool:
    plain = normalized(text)
    if long_terms:
        compact = "".join(ch for ch in plain if ch.isalnum())
        if any(term in compact for term in long_terms):
            return True
    if not short_terms and not parts and not part_words and not whole_terms:
        return False
    tokens = tokens_of(plain)
    if not short_terms.isdisjoint(tokens) or (whole_terms and not whole_terms.isdisjoint(tokens)):
        return True
    if not short_terms and not parts and not part_words:
        return False
    bare = parts & tokens
    if bare and (bare_parts_anywhere or names_only or not bare.isdisjoint(_capital_tokens(text)[1])):
        return True
    variants = _variants(frozenset(short_terms))
    if part_words:
        variants = variants | _word_variants(frozenset(part_words))
    if not variants.isdisjoint(tokens) or any(
            token[-1] == token[-2] and token[-1] in _DOUBLED_ENDINGS and token[:-1] in variants
            for token in tokens if len(token) > 3):
        return True
    # A two-letter name word takes no inflected forms: it is never matched bare, and its forms are place names and
    # articles ("Las", "Des", "Das").
    terms = frozenset(short_terms) | frozenset(word for word in part_words if len(word) == 3)
    inflections, named = _inflections(terms) | _short_named(terms), _named(terms)
    proper8, named8, lower8, genitive = _v8_short(terms)
    long_forms, long_stems, soft = _v8_long(frozenset(parts)) if parts else (frozenset(), (), frozenset())
    inflections, named = inflections | proper8 | soft, named | named8
    long_tokens = _long_tokens(tokens, long_forms, long_stems)
    genitives = frozenset().union(*genitive)
    if (inflections.isdisjoint(tokens) and named.isdisjoint(tokens) and lower8.isdisjoint(tokens) and not long_tokens
            and genitives.isdisjoint(tokens)):
        return False
    if names_only:
        return True
    proper, capitalised, lower = _capital_tokens(text)
    if (not inflections.isdisjoint(proper) or not named.isdisjoint(capitalised) or not lower8.isdisjoint(lower)
            or not long_tokens.isdisjoint(proper)):
        return True
    return not genitives.isdisjoint(tokens) and _genitive_hits(text, genitive)


def tokens_of(plain: str) -> set:
    """The whole tokens of one normalized text that a short term (or one of its forms) must equal."""
    tokens = {skeleton(token) for token in _SEPARATORS.split(plain)}
    tokens.update(skeleton(token) for token in WORDS.findall(plain))
    # Added readings only, so no earlier match is lost: an apostrophe letter splits a word, and a letter repeated
    # three or more times reads once and twice ("Sammyyy" is "Sammy", "Saaam" is "Sam").
    tokens.update(skeleton(token) for token in _WORD_WITHOUT_APOSTROPHE.findall(plain))
    stretched = [token for token in tokens if _STRETCHED.search(token)]
    tokens.update(_STRETCHED.sub(r"\1", token) for token in stretched)
    tokens.update(_STRETCHED.sub(r"\1\1", token) for token in stretched)
    return tokens


def _strings(value, depth=0, keys=True):
    """Every string of a decoded JSON value. With `keys` (the default) the keys of its objects too; without, its
    values only, which is all an identifier is ever looked for in."""
    if depth > MAX_DEPTH:
        raise PolicyError(UNAVAILABLE)
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, child in value.items()
                for text in [*([str(key)] if keys else []), *_strings(child, depth + 1, keys)]]
    if isinstance(value, list):
        return [text for child in value for text in _strings(child, depth + 1, keys)]
    if value is None or type(value) in (int, float, bool):
        return []
    raise PolicyError(UNAVAILABLE)


def _decode(value):
    from .evidence import _json
    try:
        # JSON scalar cells are unsupported: registered metadata and inventories
        # are objects/arrays. Duplicate keys and nonfinite values also refuse.
        return _json(value, (dict, list))
    except PolicyError:
        raise PolicyError(UNAVAILABLE) from None


def keyed_surfaces(row: dict, *, keys=True) -> list[tuple]:
    """(column, texts) for every text surface of one row, in column order (v8: so a names-only column is known).
    Without `keys`, the keys inside a JSON column are left out (`_strings`)."""
    result, size = [], 0
    for key, value in row.items():
        if key.startswith("_p2b_"):
            continue
        if value is None:
            continue
        if isinstance(value, bytes):
            # Unsupported valued binary content cannot be silently skipped.
            raise PolicyError(UNAVAILABLE)
        if isinstance(value, str):
            size += len(value.encode("utf-8"))
            if size > MAX_SURFACE_BYTES:
                raise PolicyError(UNAVAILABLE)
            result.append((key, _strings(_decode(value), keys=keys) if key.endswith("_json") and value else [value]))
        elif type(value) not in (int, float, bool):
            raise PolicyError(UNAVAILABLE)
    return result


def surfaces(row: dict) -> list[str]:
    return [text for _key, texts in keyed_surfaces(row) for text in texts]


def rows_revision(groups):
    # Bounded native groups, streamed so a populated entity spine is not
    # accidentally capped by the signed-request grammar's 1 MiB limit.
    return digest_stream({"version": VERSION, "groups": Rows(
        [index, len(group), *sorted(digest({key: repr(value) for key, value in row.items()}) for row in group)]
        for index, group in enumerate(groups))})


def _spelled(values) -> list:
    """Every spelling of each name (name_spellings), in order."""
    return [spelling for value in values for spelling in name_spellings(value)]


def _handle_keys(value) -> set:
    """The keys a handle is matched by: its skeleton and, for a phone number, its last ten digits. Empty when the
    value names nothing (the closure's own reading of a reached contact's handle withholds on that)."""
    if not isinstance(value, str) or not value.strip():
        return set()
    key = skeleton(value)
    if not key:
        return set()
    keys = {key}
    plain = normalized(value)
    digits = "".join(ch for ch in plain if ch.isdecimal())
    if len(digits) >= 10 and all(ch.isdecimal() or ch in "+-(). " for ch in plain):
        keys.add(digits[-10:])
    return keys


def matches_only_as_itself(value) -> bool:
    """Whether an identifier (a handle, a username, a contact id, an address, a number) is one that must stand as a
    whole token to match: it has no digit and no "@". One with a digit or an "@" keeps the reading it always had,
    anywhere in the text with separators read through, which is what finds a number written with spaces or an
    address inside a link. A bare word is different: the handle "work" was found in "network" and in "slow or",
    "king" in every "-king", and on four invented homes one such handle withheld 83 of 820 of the owner's messages
    and 42 of 150 journal entries from every share (the owner's decision of 7 Oct 2026; review R2-M3)."""
    return isinstance(value, str) and "@" not in value and not any(ch.isdecimal() for ch in normalized(value))


class EntityBoundary:
    """One canonical SQLite read transaction; never retained between reads.

    `waiting` (default True) is whether the boundary reads what the upgrade carried and the owner has not acted on
    (CARRIED_WAITING). Everything a share's release reads builds the boundary with the default: a carried person is
    withheld from every share at once. The one caller that passes False is the legacy read-time guard when it
    filters rows for the owner's own client (`BlackholeGuard.filter_observed_canonical_rows`), which must read as
    it did before the upgrade; no share door, review or index is built that way."""

    def __init__(self, conn, *, waiting=True):
        self.conn = conn
        self.ids, self.contacts, self.terms, self.handles = set(), set(), set(), set()
        # Which terms are names (of an entry, an entity, a contact, a learned mention), which are identifiers (a
        # handle, a username, an id), and which identifiers match only as themselves (`matches_only_as_itself`).
        # A term that is anybody's name is read as a name whatever else lists it: `_term_groups`.
        self.name_terms, self.identifier_terms, self.whole_identifiers = set(), set(), set()
        self._groups = (frozenset(), frozenset(), frozenset())
        # Match-only vocabulary for NAME_PART_TABLES; never a closure key (a shared first name links no one).
        self.name_parts = set()
        # Its two- and three-letter name words, which also withhold through their pet-name forms (short_name_words).
        self.name_short_words = set()
        self._context_cache = {}
        try:
            flags = self._table("entity_blackholes", {"entity_id", "normalized_name", "canonical_name", "aliases_json"})
            if not waiting:
                flags = self._without_waiting(flags)
            self.active = bool(flags)
            if not self.active:
                self.revision = digest({"version": VERSION, "active": False})
                return
            entities = self._table("entities", {"entity_id", "canonical_name", "normalized_name", "aliases_json", "identifiers_json", "contact_id"}, projected=True)
            merges = self._table("entity_merge_tombstones", {"absorbed_entity_id", "merged_into", "canonical_name", "aliases_json", "identifiers_json"})
            contacts = self._table("contacts", {"contact_id", "display_name"})
            identifiers = self._table("contact_identifiers", {"contact_id", "identifier", "identifier_type"})
            for flag in flags:
                if not skeleton(flag["normalized_name"]) or not isinstance(flag["entity_id"], str):
                    raise PolicyError(UNAVAILABLE)
                if flag["entity_id"]:
                    self.ids.add(flag["entity_id"])
                self._names(flag)
            self._close_identities(entities, merges, contacts, identifiers)
            self._mentions_by_record = {}
            for mention in self.mentions:
                self._mentions_by_record.setdefault(mention["record_id"], []).append(mention)
            self.terms.discard("")
            self.name_terms.discard("")
            self._groups = self._term_groups()
            # Recompute the closure against the full current universe, but bind
            # only the protection decisions it produces. Unrelated enrichment
            # must not invalidate every grant. New protected aliases, reminted
            # IDs, merges, contact links and mentions still change this digest.
            self.revision = digest({"version": VERSION, "revision_contract": "protected-closure/v4",
                "ids": sorted(self.ids), "contacts": sorted(self.contacts),
                "terms": sorted(self.terms), "handles": sorted(self.handles),
                "name_parts": sorted(self.name_parts),
                # Two spellings with one skeleton and the same parts can differ in their two-letter words.
                "name_short_words": sorted(self.name_short_words),
                # Only where some term is an identifier and nobody's name (a carried contact's handles): there the
                # reading differs from the one this revision named before. Every other boundary keeps its revision.
                **({"identifiers": sorted(self._groups[1] | self._groups[2]),
                    "whole_identifiers": sorted(self._groups[2])} if (self._groups[1] or self._groups[2]) else {}),
                "mentions": rows_revision([self.mentions])})
        except (sqlite3.Error, TypeError, ValueError, RecursionError):
            raise PolicyError(UNAVAILABLE) from None

    def rebind(self, conn):
        """This closure over another read transaction of the SAME rows, with no per-record read made yet.

        Only for a caller that has proven no commit landed between the snapshot this closure was
        built on and `conn`'s (search_index.SearchVerification). The protected closure and its
        revision are shared; nothing mutates them after construction. The context cache starts
        empty, so every conversation, roster and reply read runs again on `conn`.
        """
        other = copy.copy(self)
        other.conn = conn
        other._context_cache = {}
        return other

    def _close_identities(self, entities, merges, contacts, identifiers):
        """Visit each recorded association once, including learned mention aliases.

        Repeated whole-universe scans made a reverse-ordered merge chain take
        quadratic work. Queued ids/names retain the same conservative closure;
        newly learned mention spellings also close reminted entities/contacts.
        """
        by_id, by_name, by_contact, handles = (defaultdict(list) for _ in range(4))
        entity_rows = [*entities, *merges]
        entity_names = []
        for index, row in enumerate(entity_rows):
            names = set(filter(None, map(skeleton, _spelled(self._name_values(row)))))
            entity_names.append(names)
            for key in ("entity_id", "absorbed_entity_id", "merged_into"):
                if row.get(key):
                    by_id[row[key]].append(index)
            for name in names:
                by_name[name].append(index)
        for row in identifiers:
            handles[row["contact_id"]].append(row["identifier"])
        # v8: a contact is reached by a protected term equal to its display name (as before), to one of its handles,
        # or to its id, so an Off-limits name that is a phone number, an email or a contact's id protects that contact
        # (the upgrade step that carries the older per-person "exclude" choices writes such names).
        contact_names = defaultdict(list)
        for index, row in enumerate(contacts):
            by_contact[row["contact_id"]].append(index)
            for spelling in name_spellings(row["display_name"] or ""):
                contact_names[skeleton(spelling)].append(index)
            if isinstance(row["contact_id"], str) and skeleton(row["contact_id"]):
                contact_names[skeleton(row["contact_id"])].append(index)
            for handle in handles.get(row["contact_id"], ()):
                for key in _handle_keys(handle):
                    contact_names[key].append(index)
        contact_names.pop("", None)
        sets = {"id": self.ids, "term": self.terms, "contact": self.contacts}
        queue = deque((kind, value) for kind, values in sets.items() for value in values)

        def add(kind, values, name=False):
            for value in values:
                if name and value:
                    self.name_terms.add(value)
                if value and value not in sets[kind]:
                    sets[kind].add(value)
                    queue.append((kind, value))

        seen_entities, seen_contacts, queried_ids = set(), set(), set()
        self.mentions = []
        mention_columns = {"entity_id", "record_id", "source_id", "canonical_table", "surface_text"}
        self._table("entity_mentions", mention_columns, where="WHERE 0")
        while True:
            while queue:
                kind, value = queue.popleft()
                linked_entities = by_id[value] if kind == "id" else by_name[value] if kind == "term" else ()
                for index in linked_entities:
                    if index in seen_entities:
                        continue
                    seen_entities.add(index)
                    row = entity_rows[index]
                    add("id", (row.get(key) for key in ("entity_id", "absorbed_entity_id", "merged_into")))
                    add("term", entity_names[index], name=True)
                    # Parts only for the entities the closure reaches, not the whole universe.
                    name_values = _spelled(self._name_values(row))
                    self.name_parts.update(*map(name_parts, name_values))
                    self.name_short_words.update(*map(short_name_words, name_values))
                    add("contact", [row.get("contact_id")])
                    if row.get("identifiers_json"):
                        values = _decode(row["identifiers_json"])
                        if not isinstance(values, list) or any(not isinstance(handle, str) for handle in values):
                            raise PolicyError(UNAVAILABLE)
                        for handle in values:
                            add("term", self._handle(handle))
                linked_contacts = by_contact[value] if kind == "contact" else contact_names[value] if kind == "term" else ()
                for index in linked_contacts:
                    if index in seen_contacts:
                        continue
                    seen_contacts.add(index)
                    row = contacts[index]
                    add("contact", [row["contact_id"]])
                    display = _spelled([row["display_name"] or ""])
                    add("term", map(skeleton, display), name=True)
                    self.name_parts.update(*map(name_parts, display))
                    self.name_short_words.update(*map(short_name_words, display))
                    if row.get("known_usernames_json"):
                        names = _decode(row["known_usernames_json"])
                        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                            raise PolicyError(UNAVAILABLE)
                        for username in names:
                            add("term", self._identifier(username, map(skeleton, name_spellings(username))))
                if kind == "contact":
                    for handle in handles[value]:
                        add("term", self._handle(handle))
            pending = sorted(self.ids - queried_ids)
            if not pending:
                break
            # Bounded batches work on SQLite builds with a 999-variable ceiling.
            for start in range(0, len(pending), 400):
                batch = pending[start:start + 400]
                marks = ",".join("?" for _ in batch)
                found = self._table("entity_mentions", mention_columns,
                    where=f"WHERE entity_id IN ({marks})", args=tuple(batch), limit=MAX_ROWS - len(self.mentions))
                self.mentions.extend(found)
                for mention in found:
                    if mention["surface_text"]:
                        add("term", map(skeleton, name_spellings(mention["surface_text"])), name=True)
            queried_ids.update(pending)

    def _table(self, table, required, *, where="", args=(), limit=MAX_ROWS, projected=False):
        schema = self.conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchmany(2)
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if len(schema) != 1 or schema[0][0] != "table" or not required <= columns:
            raise PolicyError(UNAVAILABLE)
        fields = ",".join(sorted(required)) if projected else "*"
        cursor = self.conn.execute(f"SELECT {fields} FROM {table} {where}", args)
        names = [column[0] for column in cursor.description]
        rows = cursor.fetchmany(limit + 1)
        if len(rows) > limit:
            raise PolicyError(UNAVAILABLE)
        return [dict(zip(names, tuple(row))) for row in rows]

    @staticmethod
    def _name_values(row):
        names = [row[key] for key in ("normalized_name", "canonical_name") if row.get(key)]
        if row.get("aliases_json") is not None:
            aliases = _decode(row["aliases_json"])
            if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
                raise PolicyError(UNAVAILABLE)
            names.extend(aliases)
        if any(not isinstance(name, str) for name in names):
            raise PolicyError(UNAVAILABLE)
        return names

    def _names(self, row):
        """One Off-limits entry's names: terms, name parts and short name words. A value the entry lists among its
        identifiers (IDENTIFIER_ALIASES: a carried contact's handles, usernames and id) is read as the closure reads
        a reached contact's own handles, whole (`_handle`), and gives no name part: the words of an id or of an
        address ("contact", "default", "mail") are nobody's name (review R1 node, R-M5). An entry without the list
        reads every value as a name, as before; a list that cannot be read withholds everything."""
        identifiers = self._identifier_keys(row)
        for value in self._name_values(row):
            spellings = name_spellings(value)
            if identifiers and skeleton(value) in identifiers:
                for spelling in spellings:
                    if _handle_keys(spelling):
                        self.terms.update(self._handle(spelling))
                continue
            named = set(filter(None, map(skeleton, spellings)))
            self.terms.update(named)
            self.name_terms.update(named)
            self.name_parts.update(*map(name_parts, spellings))
            self.name_short_words.update(*map(short_name_words, spellings))

    @staticmethod
    def _without_waiting(flags) -> list:
        """The entries as the owner's own client reads them: none that is carried and waiting, and a full entry
        without the names, identifiers and entity link the upgrade added to it. A mark that cannot be read marks
        nothing, so the entry is read whole."""
        import json

        kept = []
        for flag in flags:
            raw = flag.get(CARRIED_WAITING)
            if not raw:
                kept.append(flag)
                continue
            try:
                mark = json.loads(raw)
            except (TypeError, ValueError):
                mark = None
            if not isinstance(mark, dict) or not isinstance(mark.get("terms", []), list):
                kept.append(flag)
                continue
            if mark.get("whole") is True:
                continue
            gone = {skeleton(term) for term in mark.get("terms", []) if isinstance(term, str)}
            flag = dict(flag)
            aliases = _decode(flag["aliases_json"]) if flag.get("aliases_json") is not None else []
            if isinstance(aliases, list):
                flag["aliases_json"] = json.dumps([alias for alias in aliases
                                                   if not (isinstance(alias, str) and skeleton(alias) in gone)])
            if isinstance(mark.get("entity_id"), str) and mark["entity_id"] and mark["entity_id"] == flag.get("entity_id"):
                flag["entity_id"] = ""
            kept.append(flag)
        return kept

    @staticmethod
    def _identifier_keys(row) -> set:
        """The skeletons of the values one entry marks as identifiers, not names (IDENTIFIER_ALIASES)."""
        raw = row.get(IDENTIFIER_ALIASES)
        if raw is None:
            return set()
        values = _decode(raw)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise PolicyError(UNAVAILABLE)
        return set(filter(None, map(skeleton, values)))

    def _handle(self, value):
        keys = _handle_keys(value)
        if not keys:
            raise PolicyError(UNAVAILABLE)
        self.handles.update(keys)
        self._identifier(value, keys)
        return keys

    def _identifier(self, value, keys):
        """Record these keys as an identifier's, and whether it matches only as itself. Returns the keys."""
        keys = set(filter(None, keys))
        self.identifier_terms.update(keys)
        if matches_only_as_itself(value):
            self.whole_identifiers.update(keys)
        return keys

    def _term_groups(self):
        """(names, identifiers read as always, identifiers that match only as themselves), disjoint and together
        `self.terms`. A term that is anybody's name is a name: nothing an identifier list says can make the
        boundary stop reading a real name as one."""
        identifiers = (self.identifier_terms & self.terms) - self.name_terms
        whole = frozenset(identifiers & self.whole_identifiers)
        return frozenset(self.terms - identifiers), frozenset(identifiers - whole), whole

    def _hits(self, row, name_parts=False, bare_parts_anywhere=True):
        """Whether any surface of the row carries a protected term; with `name_parts`, also a part of a protected
        name as a whole word and the forms of the parts (callers pass the flag positionally). Since v8 every check a
        share's release reads passes it (observe, legacy_veto, mentions_protected); without it this is the whole-term
        reading `name_part_match_only` compares against. A NAMES_ONLY_COLUMNS column reads every word as a name.
        Without `bare_parts_anywhere` a bare part withholds only where written capitalised (text_hits): every
        kind but NAME_PART_TABLES, whose rows kept the v3 rule."""
        surfaces_by_key = keyed_surfaces(row)
        # Short terms (initials, short names) match whole tokens and their pet-name forms only, never inside a
        # larger word ("M.E." in "message"); full names and handles match anywhere (text_hits). A name part is
        # matched as a whole word only, never inside a longer word: the same token sets the short terms use, under
        # the same normalisation (accents, invisible characters, confusables, punctuation), so a possessive or a
        # punctuated spelling of the part still counts.
        names, identifiers, whole_identifiers = self._groups
        short_terms, long_terms = split_terms(names)
        parts = self.name_parts if name_parts else frozenset()
        part_words = self.name_short_words if name_parts else frozenset()
        if any(text_hits(text, short_terms, long_terms, parts=parts, part_words=part_words,
                         names_only=key in NAMES_ONLY_COLUMNS, bare_parts_anywhere=bare_parts_anywhere)
               for key, texts in surfaces_by_key for text in texts):
            return True
        if not identifiers and not whole_identifiers:
            return False
        # An identifier is looked for in VALUES only, never in the keys of a JSON column, and takes no name part
        # and no form. One with a digit or an "@" is read as it always was; a bare word must be a whole token.
        short_ids, long_ids = split_terms(identifiers)
        return any(text_hits(text, short_ids, long_ids, whole_terms=whole_identifiers)
                   for _key, texts in keyed_surfaces(row, keys=False) for text in texts)

    def _linked(self, record_id, table, source_id, *, any_source=False):
        # Unknown legacy table labels are veto signals, not evidence that the
        # match belongs to a different supported family. Dataset collisions
        # similarly withhold because the mention schema has no dataset key.
        known = {"signal_objects", "conversation_messages", "ai_chat_messages", "conversations", "ai_chat_conversations"}
        return any(mention["record_id"] == record_id and mention["entity_id"] in self.ids
            and (mention["canonical_table"] not in known or mention["canonical_table"] == table)
            and (any_source or mention["source_id"] in (None, "", source_id)) for mention in self._mentions_by_record.get(record_id, ()))

    def _context(self, table, row, source_id, dataset_id):
        conversation = row.get("conversation_id")
        if not isinstance(conversation, str) or not conversation:
            raise PolicyError(UNAVAILABLE)
        key = (table, source_id, dataset_id, conversation)
        if key in self._context_cache:
            return self._context_cache[key]
        if table == "conversation_messages":
            args = (conversation, source_id, dataset_id)
            where = "WHERE conversation_id=? AND source_id=? AND dataset_id=?"
            parent = self._table("conversations", {"conversation_id", "source_id", "dataset_id"}, where=where, args=args, limit=1)
            roster = self._table("conversation_participants", {"conversation_id", "source_id", "dataset_id", "contact_id"}, where=where, args=args, limit=MAX_CONTEXT_ROWS)
            self._table(table, {"conversation_id", "source_id", "dataset_id", "sender_id"}, where="WHERE 0")
            siblings = [{"sender_id": item[0]} for item in self.conn.execute(
                f"SELECT DISTINCT sender_id FROM {table} {where} AND sender_id IS NOT NULL", args).fetchmany(MAX_CONTEXT_ROWS + 1)]
        elif table == "ai_chat_messages":
            args = (conversation, source_id)
            where = "WHERE conversation_id=? AND source_id=?"
            parent = self._table("ai_chat_conversations", {"conversation_id", "source_id", "owner_user_id"}, where=where, args=args, limit=1)
            roster = []
            siblings = []
        else:
            raise PolicyError(UNAVAILABLE)
        if len(parent) != 1 or len(siblings) > MAX_CONTEXT_ROWS:
            raise PolicyError(UNAVAILABLE)
        rows = [*parent, *roster, *siblings]
        matched = any(item.get("contact_id") in self.contacts or item.get("sender_id") in self.contacts
                      or item.get("sender_id") in self.ids or self._hits(item, True, False) for item in rows)
        # Parent/ancestor record protection is a direct veto even without names.
        parent_table = "conversations" if table == "conversation_messages" else "ai_chat_conversations"
        matched |= self._linked(conversation, parent_table, source_id)
        matched |= self.conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=?",
                                     (parent_table, conversation)).fetchone() is not None
        revision = rows_revision([rows])
        self._context_cache[key] = (matched, revision)
        return matched, revision

    def _reply_context(self, table, row, source_id, dataset_id):
        """Only declared reply ancestors, never every nearby message's content."""
        rows, seen = [], set()
        reference = row.get("reply_to_message_id")
        while reference:
            if not isinstance(reference, str) or reference in seen or len(seen) >= MAX_DEPTH:
                raise PolicyError(UNAVAILABLE)
            seen.add(reference)
            where, args = "WHERE message_id=? AND source_id=? AND conversation_id=?", [reference, source_id, row.get("conversation_id")]
            if table == "conversation_messages":
                where += " AND dataset_id=?"
                args.append(dataset_id)
            found = self._table(table, {"message_id", "source_id", "conversation_id", "content"}, where=where, args=args, limit=1)
            if len(found) != 1:
                raise PolicyError(UNAVAILABLE)
            parent = found[0]
            if self.conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=?",
                                 (table, reference)).fetchone() is not None:
                raise PolicyError("entity_protected")
            if self._linked(reference, table, source_id):
                raise PolicyError("entity_protected")
            rows.append(parent)
            reference = parent.get("reply_to_message_id")
        return rows

    def observe(self, *, table, record_id, source_id, dataset_id, row):
        """Private owner preview may observe a veto; it cannot authorize release."""
        if not self.active:
            return False, self.revision
        try:
            matched = self._hits(row, True, table in NAME_PART_TABLES)   # v8: every kind reads the name rule
            # Legacy identities that omit table/source can veto by record id;
            # they never prove a negative association.
            matched |= self._linked(record_id, table, source_id)
            if table == "signal_objects":
                payload = _decode(row.get("payload_json"))
                if not isinstance(payload, dict):
                    raise PolicyError(UNAVAILABLE)
                matched |= any(payload.get(key) in self.ids for key in ("subject_entity_id", "object_entity_id"))
                context_revision = self.revision
            elif table in CONTEXTLESS_TABLES:
                # A journal entry has no conversation, roster or replies: the whole row is its own
                # context, and `_hits` above already read every column of it (people, places, metadata),
                # on whole terms and on each part of a protected name.
                context_revision = self.revision
            else:
                context_matched, context_revision = self._context(table, row, source_id, dataset_id)
                matched |= context_matched
                replies = self._reply_context(table, row, source_id, dataset_id)
                matched |= any(self._hits(parent, True, False) for parent in replies)
                context_revision = digest({"context": context_revision, "replies": rows_revision([replies])})
            return matched, digest({"boundary": self.revision, "context": context_revision})
        except (sqlite3.Error, TypeError, ValueError, RecursionError):
            raise PolicyError(UNAVAILABLE) from None

    def check(self, **kwargs):
        """Veto the entire evidence item; return a private context revision."""
        matched, revision = self.observe(**kwargs)
        if matched:
            raise PolicyError("entity_protected")
        return revision

    def name_part_match_only(self, table, row) -> bool:
        """Whether the surface scan matches this row only through a part of a protected name, i.e. the
        whole-term scan alone would not (mention links and record ids are separate vetoes, not read here).

        A counter for the census: `entity_protected` stays one reason code, and this says how many of a
        family's withholds the name-part rule alone accounts for. Never a release path; False for an
        inactive boundary. Since v8 the rule is every table's, so `table` no longer narrows it."""
        if not self.active:
            return False
        return self._hits(row, True, table in NAME_PART_TABLES) and not self._hits(row)

    def mentions_protected(self, *texts) -> bool:
        """Whether any of these texts carries an Off-limits term: the same match ``legacy_veto`` applies
        to a row's surfaces, with the name parts and their forms (v8: every kind). For derived text (a claim a
        model is asked about, a goal, a fact's value, an interest label) that has no row of its own."""
        if not self.active:
            return False
        return self._hits({f"text_{i}": text for i, text in enumerate(texts) if isinstance(text, str)}, True, False)

    def legacy_veto(self, table, row):
        """Observed native rows, before legacy projection/redaction.

        This is a veto only: it grants no permission and does not qualify a v2
        fact. Message context is recovered from its exact canonical identity,
        since legacy public rows can omit parent and sender fields.
        """
        if not self.active:
            return False
        texts = surfaces(row)
        if self._hits(row, True, table in NAME_PART_TABLES) or any(text in self.ids or text in self.contacts
                                                                    for text in texts):
            return True
        record_id = next((row.get(key) for key in ("record_id", "message_id", "id", "event_id", "entry_id", "contact_id", "entity_id") if row.get(key)), None)
        if record_id and self._linked(record_id, table, row.get("source_id"), any_source=row.get("source_id") is None):
            return True
        if table not in {"conversation_messages", "ai_chat_messages", "message_stream"}:
            return False
        if not isinstance(record_id, str) or not record_id:
            raise PolicyError(UNAVAILABLE)
        found = []
        for native_table in (["conversation_messages", "ai_chat_messages"] if table == "message_stream" else [table]):
            where, args = "WHERE message_id=?", [record_id]
            for key in ("source_id", "dataset_id"):
                if row.get(key) is not None and (key != "dataset_id" or native_table == "conversation_messages"):
                    where += f" AND {key}=?"
                    args.append(row[key])
            found.extend((native_table, native) for native in self._table(native_table,
                {"message_id", "source_id", "conversation_id", "content"}, where=where, args=args, limit=1))
        if len(found) != 1:
            raise PolicyError(UNAVAILABLE)
        native_table, native = found[0]
        matched, _revision = self.observe(table=native_table, record_id=record_id,
            source_id=native.get("source_id"), dataset_id=native.get("dataset_id"), row=native)
        return matched
