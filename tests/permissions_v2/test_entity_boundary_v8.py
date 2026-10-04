"""N6 (decision D7): Off-limits name forms in every kind a share can release (entity boundary v8).

protects: two independent blind sets (3 and 5, invented people) still released 45 journal entries whole under v7, each
naming a protected person only by a form the boundary did not know; and outside journal rows the boundary read no
part of a protected name at all, so a message, an AI-chat turn, a goal, a fact, a relationship or an interest naming
the person by a first name, a surname or any form of one released. These tests pin:
  - each of the 45 shapes, rebuilt with coined names (none of the blind sets' own), withheld in every kind through the
    check that kind's release runs, and each a miss in a journal row before v8 (the 2 Oct finding);
  - the places v8 must not reach: a genitive before a function word or ending a sentence, an ordinary word in lower
    case, a bare part that is an ordinary word written in lower case outside a journal row, an ordinary word in a
    people column that is no form;
  - that v8 only widens v7 (the v7 rule is v8 with its hooks off), and the contact link the D24 step relies on.
Every person, place and handle here is invented.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import NamedTuple

import pytest

from topos.permissions_v2 import entity_boundary, interest_family, journal_goal_field
from topos.permissions_v2.english_short_words import WORDS_2_3
from topos.permissions_v2.entity_boundary import EntityBoundary

SCHEMA = """
CREATE TABLE entity_blackholes(blackhole_id TEXT, entity_id TEXT, normalized_name TEXT, canonical_name TEXT,
    aliases_json TEXT);
CREATE TABLE entities(entity_id TEXT, canonical_name TEXT, normalized_name TEXT, aliases_json TEXT,
    identifiers_json TEXT, contact_id TEXT);
CREATE TABLE entity_merge_tombstones(absorbed_entity_id TEXT, merged_into TEXT, canonical_name TEXT,
    aliases_json TEXT, identifiers_json TEXT);
CREATE TABLE contacts(contact_id TEXT, display_name TEXT);
CREATE TABLE contact_identifiers(contact_id TEXT, identifier TEXT, identifier_type TEXT);
CREATE TABLE entity_mentions(entity_id TEXT, record_id TEXT, source_id TEXT, canonical_table TEXT, surface_text TEXT);
CREATE TABLE conversations(conversation_id TEXT, source_id TEXT, dataset_id TEXT);
CREATE TABLE conversation_participants(conversation_id TEXT, source_id TEXT, dataset_id TEXT, contact_id TEXT);
CREATE TABLE conversation_messages(message_id TEXT, conversation_id TEXT, source_id TEXT, dataset_id TEXT,
    sender_id TEXT, content TEXT, reply_to_message_id TEXT);
CREATE TABLE ai_chat_conversations(conversation_id TEXT, source_id TEXT, owner_user_id TEXT, title TEXT);
CREATE TABLE ai_chat_messages(message_id TEXT, conversation_id TEXT, source_id TEXT, content TEXT);
CREATE TABLE owner_only_records(canonical_table TEXT, record_id TEXT);
INSERT INTO conversations VALUES('thread-1', 'source-1', 'dataset-1');
INSERT INTO conversation_messages VALUES('message-1', 'thread-1', 'source-1', 'dataset-1', 'owner-handle', '', NULL);
INSERT INTO ai_chat_conversations VALUES('chat-1', 'chat-source', 'owner-1', 'Plans');
"""
KINDS = ("message", "ai_chat", "journal_entry", "interest", "goal", "relationship", "fact")
NO_KEYS = (frozenset(), frozenset())          # interest_family's (whole names, name words) for nobody


def gate(canonical, aliases=(), *, contacts=(), identifiers=(), participants=()):
    """The real boundary over the tables it reads, one protected person, the conversation context a message needs."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO entity_blackholes VALUES('b1','',?,?,?)",
                 (canonical.lower(), canonical, json.dumps(list(aliases))))
    conn.executemany("INSERT INTO contacts VALUES(?,?)", contacts)
    conn.executemany("INSERT INTO contact_identifiers VALUES(?,?,?)", identifiers)
    conn.executemany("INSERT INTO conversation_participants VALUES('thread-1','source-1','dataset-1',?)",
                     [(contact,) for contact in participants])
    return EntityBoundary(conn)


def withheld(kind, boundary, text, column="content"):
    """Whether `kind`'s release check withholds an item whose released text is `text` (its own check, not a proxy)."""
    if kind == "message":
        row = {"message_id": "message-1", "conversation_id": "thread-1", "source_id": "source-1",
               "dataset_id": "dataset-1", "sender_id": "owner-handle", "content": text, "reply_to_message_id": None}
        return boundary.observe(table="conversation_messages", record_id="message-1", source_id="source-1",
                                dataset_id="dataset-1", row=row)[0]
    if kind == "ai_chat":
        row = {"message_id": "turn-1", "conversation_id": "chat-1", "source_id": "chat-source", "content": text}
        return boundary.observe(table="ai_chat_messages", record_id="turn-1", source_id="chat-source",
                                dataset_id=None, row=row)[0]
    if kind == "journal_entry":
        row = {"entry_id": "entry-1", "source_id": "journal", "content": "Notes for the week.", "people": None,
               "metadata_json": None}
        if column == "metadata_json":
            row["metadata_json"] = json.dumps({"template": "time-log", "note": text})
        else:
            row[column] = text
        return boundary.observe(table="journal_entries", record_id="entry-1", source_id="journal",
                                dataset_id=None, row=row)[0]
    if kind == "interest":                 # the label's checks and the month's visits, as interest_family reads them
        label = "offlimits" in interest_family._label_failures(
            "cluster-1", text, [], [], NO_KEYS, NO_KEYS, {"record": set()}, frozenset(), boundary)
        visit = interest_family._visits_protected(
            boundary, [{"event_id": "visit-1", "title": text, "url": "https://example.org/page"}])
        return label and visit
    if kind == "goal":                     # the goal row's veto and the goal field's own text check
        return (boundary.legacy_veto("user_goals", {"goal_id": "goal-1", "goal_text": text})
                and journal_goal_field.released_text_refusal(text, boundary=boundary) == "goal_field_offlimits")
    if kind == "relationship":             # "Owner intends to <target>": the endpoint entity's veto
        return boundary.legacy_veto("entities", {"entity_id": "target-1", "canonical_name": text})
    if kind == "fact":                     # "Owner <predicate> <value>": the fact row's veto
        return boundary.legacy_veto("signal_objects", {"object_id": "fact-1", "payload_json": json.dumps(
            {"predicate": "plans", "object_value": text})})
    raise AssertionError(kind)


NONE = frozenset()


def without_v8(monkeypatch):
    """The boundary as v7 read a journal row: no v8 forms, long-name forms, names-only column, name spellings or
    line-end reading (a journal row read the name rule before v8 too)."""
    monkeypatch.setattr(entity_boundary, "_v8_short", lambda terms: (NONE, NONE, NONE, (NONE, NONE, NONE, NONE)))
    monkeypatch.setattr(entity_boundary, "_v8_long", lambda parts: (NONE, (), NONE))
    monkeypatch.setattr(entity_boundary, "NAMES_ONLY_COLUMNS", NONE)
    monkeypatch.setattr(entity_boundary, "name_spellings", lambda value: (value,))
    monkeypatch.setattr(entity_boundary, "_LINE_END_HYPHEN", re.compile("(?!)"))


class Shape(NamedTuple):
    case: str            # the class of the escaped blind-set case it rebuilds
    note: str
    canonical: str
    aliases: tuple
    text: str            # as the journal row writes it, in `column`
    column: str = "content"
    prose: str = ""      # the same form in running text, for a kind with no people column


TAYA = ("Тая Кремнёва", ("Тая",))                     # a Cyrillic name and alias
SHAPES = [
    Shape("short.nickname", "a Japanese honorific written as one word with a short alias",
          "Suo Varrenfeld", ("Suo",), "Messaged Suochan about the copy."),
    Shape("short.plural", "the family plural of a two-letter alias with no vowel",
          "Brevan Zr", ("Zr",), "Cycling to the lighthouse with the Zrs."),
    Shape("short.nickname", "a Basque -txu diminutive of a short alias",
          "Ile Garaitzoa", ("Ile",), "Bake bread and take it to Iletxu at the market."),
    Shape("short.plural", "an -es household plural of an alias ending in s",
          "Tos Amberfinch", ("Tos",), "A new frame for the Toses next door."),
    Shape("short.nickname", "a Dutch diminutive after a doubled consonant, opening a sentence",
          "Bem van Oosterweel", ("Bem",), "Bemmetje fixed the shed door so we can paint."),
    Shape("short.vowel_e_ending", "a possessive without its apostrophe opening a sentence, alias an English word",
          "Ama Brekkenholt", ("Ama",), "Amas bike needs a new chain."),
    Shape("short.inflected", "a Finnish genitive in English prose",
          "Uvo Vahterlund", ("Uvo",), "Borrowed Uvon ladder to fix the bathroom shelf."),
    Shape("short.plural", "the plural of a two-letter alias with no vowel, only in the people column",
          "Brevan Zr", ("Zr",), "Zrs", "people", "Dinner with the Zrs on Sunday."),
    Shape("short.inflection_baltic", "a Lithuanian dative, final vowel replaced, alias an English word",
          "Ala Varnupe", ("Ala",), "Gave the keys to Alai yesterday."),
    Shape("short.lowercase", "a Slavic diminutive in lower case in the people column",
          "Ezolinde Marsk", ("Ezo",), "ezka, the neighbours", "people", "Went to the market with ezka and the kids."),
    Shape("name.inflected_finnic", "a Finnish genitive of a long nickname with consonant gradation",
          "Teodor Kalliomaa", ("Torppo",), "Kävin Torpon kanssa hakemassa kompostilaatikon."),
    Shape("short.inflection_slavic", "a Croatian possessive adjective, final vowel replaced, alias an English word",
          "Aba Penvarra", ("Aba",), "We stayed at Abine house by the lake."),
    Shape("short.invisible", "a zero-width non-joiner inside a Dutch diminutive",
          "Ado Plongsteeg", ("Ado",), "Coffee with Ado\u200ctje after the market."),
    Shape("short.inflection_finnic", "a Finnish genitive",
          "Ilo Kaarnamaa", ("Ilo",), "Vein Ilon koiran ulos aamulla."),
    Shape("short.homoglyph", "a Latin capital inside a Cyrillic diminutive in the people column",
          *TAYA, "Tаечка", "people",
          "Вчера пришла Tаечка."),
    Shape("short.inflection_romance", "a Romanian genitive, final vowel replaced, alias an English word",
          "Ava Cristescar", ("Ava",), "Cartea Avei e pe masă."),
    Shape("short.diminutive", "a Russian diminutive of a Cyrillic alias in the people column",
          *TAYA, "Таечка", "people",
          "Вчера звонила Таечка."),
    Shape("name.transliterated", "a first name written in Cyrillic and declined",
          "Dorimant Vaskelund", (),
          "Вчера видели "
          "Дориманта у дома."),
    Shape("short.diminutive_inflected", "a Greek diminutive in the genitive of a Greek alias",
          "Ζιά Κολοβάκη", ("Ζιά",),
          "Η κουζίνα της "
          "Ζιούλας είναι μικρή."),
    Shape("name.genitive_germanic", "a German genitive of the surname",
          "Seraphel Hallowmeer", (), "Wir mähen morgen Hallowmeers Rasen."),
    Shape("short.inflection_greek", "a Greek genitive of a Greek alias",
          "Ρόα Μακροδάκη", ("Ρόα",),
          "Πήγα στο σπίτι της "
          "Ρόας χθες."),
    Shape("short.inflection_finnic", "a Finnish genitive before a postposition, alias an English word",
          "Ako Vuorenmaa", ("Ako",), "Kävin Akon kanssa rautakaupassa ostamassa maalia."),
    Shape("name.inflected_finnic", "a Finnish genitive of a first name with a stem change",
          "Ilmaras Pellinkoski", (), "Lainasimme Ilmaraksen peräkärryä huonekaluille."),
    Shape("short.inflection_slavic", "a Russian possessive adjective in the genitive of a Cyrillic alias",
          *TAYA, "Мы были у Таиного "
                 "брата вчера."),
    Shape("short.lowercase", "a Finnish genitive in lower case",
          "Oku Rimmelvaara", ("Oku",), "Järjestin okun tavarat kirpputorilla."),
    Shape("name.line_break", "a surname broken by a hyphen at a line end",
          "Wendel Ostwyndel", (), "Dropped the boxes with Ostwyn-\ndel at the depot."),
    Shape("short.inflection_baltic", "a Lithuanian genitive, final vowel replaced, alias an English word",
          "Aga Vilnoraite", ("Aga",), "Tai Agos dviratis prie namų."),
    Shape("short.diminutive", "a Portuguese diminutive of a two-letter alias in the people column",
          "Lé Ventarolha", ("Lé",), "Lequinha", "people", "Pintámos o portão com a Lequinha."),
    Shape("short.inflection_finnic", "a Finnish elative, alias an English word",
          "Ayu Tandomaa", ("Ayu",), "Iltapäivällä puhuimme paljon Ayusta."),
    Shape("short.sentence_initial", "a Finnish genitive opening the people column",
          "Ivu Rantamo", ("Ivu",), "Ivun perhe, Marelle Dask", "people", "Ivun koira haukkui koko yön."),
    Shape("short.inflection_germanic", "an Icelandic dative, final vowel replaced, the form an English word",
          "Eri Thorvaldsstad", ("Eri",), "Ég gaf Era skrúfjárnið í gær."),
    Shape("short.inflection_hungarian", "a Hungarian family plural in the people column",
          "Ilu Kertvarga", ("Ilu",), "Elmentem Iluékhez", "people", "Tegnap meglátogattuk Iluéket."),
    Shape("short.inflection_hungarian", "a Hungarian family-plural locative",
          "Ebe Szalmavár", ("Ebe",), "Tegnap Ebééknél ebédeltünk, utána sétáltunk."),
    Shape("short.lowercase", "a Hungarian dative in lower case",
          "Ibu Kertmező", ("Ibu",), "oda kell adni ibunak, kell-e még?"),
    Shape("short.lowercase", "a possessive without its apostrophe in lower case, a two-letter alias an English word",
          "Jo Arnestvik", ("Jo",), "fixed the gravel path with jos wheelbarrow."),
    Shape("short.diminutive", "a Spanish diminutive opening the people column, alias an English word",
          "Ama Peñarrosa", ("Ama",), "Amita, la vecina", "people", "Comimos con Amita en la despensa."),
    Shape("name.diminutive", "a Portuguese diminutive of a first name with a consonant change",
          "Vasquim Orrelhado", (), "Arrumei o canteiro com o Vasquinzinho e as tábuas."),
    Shape("short.invisible", "a right-to-left mark inside a Cyrillic form",
          *TAYA, "Мы гуляли с Та\u200fей "
                 "вчера."),
    Shape("short.diminutive", "an Italian diminutive with a stem change",
          "Igo Ferramonti", ("Igo",), "Il carrello di Ighetto è nella cantina."),
    Shape("short.diminutive_inflected", "a Russian diminutive in the instrumental",
          *TAYA, "Гуляли вчера с "
                 "Таечкой по парку."),
    Shape("short.inflection_slavic", "a Russian instrumental, final vowel replaced",
          *TAYA, "Мы долго говорили "
                 "с Таей о полках."),
    Shape("short.combining", "a combining diaeresis in a Dutch diminutive in the people column, alias an English word",
          "Dom Ploegveld", ("Dom",), "Do\u0308mpje, de buren", "people", "Koffie met Do\u0308mpje na de markt."),
    Shape("short.inflection_slavic", "a Russian dative, final vowel replaced",
          *TAYA, "Я позвонил Тае "
                 "вечером."),
    Shape("short.inflection_baltic", "a Lithuanian diminutive in the genitive, alias an English word",
          "Ala Varnupe", ("Ala",), "Pasodinome Alutės tulpes darže."),
    Shape("short.inflection_slavic", "a Czech possessive adjective, inflected, alias an English word",
          "Ava Cristescar", ("Ava",), "Koupili jsme barvu na Avině kolo."),
]
SHAPE_IDS = [f"{index:02d}-{shape.case}" for index, shape in enumerate(SHAPES)]


def test_the_45_shapes_of_2_october_are_all_here():
    assert len(SHAPES) == 45
    blind = {"short.nickname": 3, "short.plural": 3, "short.inflected": 1, "short.vowel_e_ending": 1,
             "short.inflection_baltic": 3, "short.lowercase": 4, "name.inflected_finnic": 2,
             "short.inflection_slavic": 5, "short.invisible": 2, "short.inflection_finnic": 3, "short.homoglyph": 1,
             "short.inflection_romance": 1, "short.diminutive": 4, "name.transliterated": 1,
             "short.diminutive_inflected": 2, "name.genitive_germanic": 1, "short.inflection_greek": 1,
             "name.line_break": 1, "short.sentence_initial": 1, "short.inflection_germanic": 1,
             "short.inflection_hungarian": 2, "name.diminutive": 1, "short.combining": 1}
    assert sum(blind.values()) == 45
    counted = {}
    for shape in SHAPES:
        counted[shape.case] = counted.get(shape.case, 0) + 1
    assert counted == blind
    # the shapes that turn on an alias being an English word (web2) keep that property with the coined alias
    english = {"Ama", "Ala", "Aba", "Ado", "Ava", "Ako", "Aga", "Ayu", "Jo", "Dom"}
    for shape in SHAPES:
        for alias in shape.aliases:
            if alias in english:
                assert entity_boundary.skeleton(alias) in WORDS_2_3, alias


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
def test_each_shape_is_withheld_in_every_kind(shape, kind):
    boundary = gate(shape.canonical, shape.aliases)
    if kind == "journal_entry":
        assert withheld(kind, boundary, shape.text, shape.column)
    else:
        assert withheld(kind, boundary, shape.prose or shape.text)


@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
def test_each_shape_was_released_whole_in_a_journal_row_before_v8(shape, monkeypatch):
    without_v8(monkeypatch)
    assert not withheld("journal_entry", gate(shape.canonical, shape.aliases), shape.text, shape.column)


@pytest.mark.parametrize("kind", KINDS)
def test_a_bare_first_name_or_surname_written_as_a_name_withholds_in_every_kind(kind):
    boundary = gate("Quillon Marsh-Edevane")
    for text in ("Lunch with Quillon after the run.", "Edevane called about the boat."):
        assert withheld(kind, boundary, text), text
    assert not withheld(kind, boundary, "Lunch after the run, then the boat.")


@pytest.mark.parametrize("kind", [kind for kind in KINDS if kind not in ("journal_entry", "interest")])
def test_outside_a_journal_row_a_part_that_is_an_ordinary_word_in_lower_case_is_that_word(kind):
    """Measured on a copy of the owner's node: the uniform rule would newly withhold 692 of 39,080 owner messages,
    665 of them for a part written only as an ordinary lower-case word. A form of the part still withholds."""
    boundary = gate("Wren Thistledown", ("Brook Mallory",))
    assert not withheld(kind, boundary, "We walked along the brook at dawn.")
    assert withheld(kind, boundary, "We walked with Brook at dawn.")
    assert withheld(kind, boundary, "Brook came by at dawn.")                       # capitalised anywhere
    assert withheld(kind, boundary, "We walked to Brooks place at dawn.")           # a form, as in a journal row


def test_a_journal_row_and_an_interest_label_keep_the_part_anywhere():
    boundary = gate("Wren Thistledown", ("Brook Mallory",))
    assert withheld("journal_entry", boundary, "We walked along the brook at dawn.")
    assert "offlimits" in interest_family._label_failures(
        "cluster-1", "brook walks", [], [], NO_KEYS, NO_KEYS, {"record": set()}, frozenset(), boundary)
    assert boundary.name_part_match_only("journal_entries", {"content": "along the brook"})
    assert not boundary.name_part_match_only("conversation_messages", {"content": "along the brook"})
    assert boundary.name_part_match_only("conversation_messages", {"content": "with Brook"})


@pytest.mark.parametrize("alias, text", [
    ("Uvo", "We flew home over Uvon."),                      # a genitive ending a sentence reads as a place
    ("Uvo", "Uvon is a long way from here."),                # before a function word
    ("Ira", "News from Iran."), ("Ira", "Iran is far."),
    ("Ama", "Amas are rare birds."),                         # an s-form opening a sentence, before a verb
    ("Jo", "The jos is on the list."),                       # a lower-case s-form before a verb
    ("Kes", "the keses were late."),                         # no lower-case -es plural
    ("Los", "He loses the keys."),
    ("Ana", "Any ideas?"), ("Ha", "She has two."),           # an English word is never a form
])
def test_a_genitive_or_an_s_form_reads_as_one_only_before_what_it_owns(alias, text):
    assert not withheld("message", gate("Quillon Marsh", (alias,)), text)


@pytest.mark.parametrize("alias, text", [
    ("Uvo", "Borrowed Uvon ladder."), ("Uvo", "uvon ladder is in the shed."), ("Ira", "Iran sanctions were eased."),
    ("Ama", "Amas car is outside."), ("Jo", "fixed it with jos hammer."),
])
def test_a_genitive_before_what_it_owns_withholds(alias, text):
    """The cost of the gate, stated: a place name that is also a genitive withholds before a noun ("Iran sanctions")."""
    assert withheld("message", gate("Quillon Marsh", (alias,)), text)


def test_a_people_column_reads_every_word_as_a_name_but_an_ordinary_word_is_no_form():
    boundary = gate("Ezolinde Marsk", ("Ezo",))
    assert withheld("journal_entry", boundary, "ezka", "people")
    assert withheld("journal_entry", boundary, "Ezon perhe", "people")
    assert not withheld("journal_entry", boundary, "the neighbours, a cousin", "people")
    # in prose a lower-case form withholds only on a name no English list holds (Ezo) and never as an English word
    assert withheld("message", boundary, "Went with ezka today.")
    assert not withheld("message", gate("Ula Varnell", ("Ula",)), "kolacja u ulki.")    # "ula" is an English word


def test_a_long_name_takes_endings_and_stem_changes_but_not_another_name():
    boundary = gate("Ulrike Varnell", ("Ula",))
    for text in ("Lunch with Ulrikes sister.", "Kolacja z Ulriką w piatek.", "Kahvi Varnellin kanssa."):
        assert withheld("message", boundary, text), text
    for text in ("Kolacja u Ulricha w piatek.", "Lunch with Varna today.", "the varnell is old"):
        assert not withheld("message", boundary, text), text


def test_a_name_in_another_script_counts_in_its_latin_spelling_and_the_reverse():
    cyrillic = gate(*TAYA)
    assert withheld("message", cyrillic, "Lunch with Taechka on Friday.")
    latin = gate("Taya Kremneva", ("Taya",))
    assert withheld("message", latin, "Обед с Таечкой.")


def test_an_off_limits_name_equal_to_a_contacts_handle_or_id_protects_that_contact():
    """The D24 upgrade step names a carried contact by a handle or its id: the closure must reach the contact, and
    with it every conversation the contact takes part in (the older exclude's reach)."""
    contact = [("contact-77", None)]
    by_email = gate("kestrel.vane@example.org", (), contacts=contact,
                    identifiers=[("contact-77", "kestrel.vane@example.org", "email")], participants=["contact-77"])
    assert "contact-77" in by_email.contacts
    assert withheld("message", by_email, "Plans for the weekend.")
    by_id = gate("contact-77", (), contacts=contact, participants=["contact-77"])
    assert "contact-77" in by_id.contacts
    assert withheld("message", by_id, "Plans for the weekend.")
    stranger = gate("contact-78", (), contacts=contact, participants=["contact-77"])
    assert "contact-77" not in stranger.contacts
    assert not withheld("message", stranger, "Plans for the weekend.")


def test_v8_only_ever_adds_to_v7(monkeypatch):
    """Wherever the rule with v8's forms, readings, spellings and names-only column off matched (v7's rule for a
    journal row), v8 matches, over every shape text and a set of ordinary sentences; and v8 adds matches."""
    texts = [shape.text for shape in SHAPES] + [shape.prose for shape in SHAPES if shape.prose] + [
        "Lunch with Quentin today.", "Kolacja u Uli w piatek.", "We borrowed Iras car.", "Otis car is red.",
        "The brook was cold.", "Any ideas for dinner?", "M4rta came by.", "Notes:\n- Ula plan worked."]
    gates = [(shape.canonical, shape.aliases) for shape in SHAPES[::5]] + [("Ula Varnell", ("Ula",)),
                                                                           ("Ira Example", ("Ira",))]
    def verdicts():
        return [withheld(kind, gate(*person), text) for person in gates for text in texts
                for kind in ("message", "goal", "fact")]
    v8 = verdicts()
    without_v8(monkeypatch)
    before = verdicts()
    assert all(new or not old for new, old in zip(v8, before))
    assert sum(new and not old for new, old in zip(v8, before)) >= 20


def test_the_boundary_version_is_v8():
    assert entity_boundary.VERSION == "node-observed-entity-boundary/v8"
