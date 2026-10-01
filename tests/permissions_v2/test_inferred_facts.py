"""IF-6 v1: a fact the extractor drew from one journal entry releases as `assertion: "inferred"`.

protects: the stated-value floor never grounds the extractor's journal facts (on the measured node, 0 of 116), so
OD-63 lets such a fact release with the entry it cites, under a grant that already signs "Journal entries", while
the node flag `TOPOS_PERMISSIONS_V2_DERIVED_FACTS` is on. Dropping a floor is exactly where a boundary leaks, so
these tests pin what an inferred fact must still clear, and what the flag must leave untouched:
  - every value guard of `inferred_facts.refusal` withholds on its own, with a code, and lets a plain value pass;
  - the release path end to end: the entry released under `journal_entry`, the fact released with
    `assertion: "inferred"`, citing the entry as a record, dated at most by its stated day, in today's fact shape
    (no new key, so the CP's unchanged `KnowledgeSearchResult` parses it);
  - flag off: `fact_not_grounded` and today's bytes (a golden fact item, the index basis); flag on without the
    journal family: inert; a grant without `journal_entry`: nothing from the journal, checked where it is used;
  - Off-limits, special categories, third parties, NSFW, owner-only, exclusions, OD-59 closures, a health
    predicate, more than 20 cited records, more than one cited source: each still withholds;
  - the index basis moves with the flag, so every index is rebuilt when it flips.
Synthetic fixtures only: invented names, no owner data.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_journal_family import SOURCE, _db, _entry, node, owner  # noqa: F401 (node: fixture)
from tests.permissions_v2.test_journal_typed_items import (ATLAS, DAY, _attest_owner, _cites, _code, _fact, _kind,
                                                           _node, _off_limits, _publish, _restrict, _search)
from topos.permissions_v2 import inferred_facts
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence_families import JOURNAL_FLAG
from topos.permissions_v2.inferred_facts import FLAG, refusal, value_refusal

PROSE = "Long day on the parser and the release build."   # states nothing a class form would ground
FACT_KEYS = {"kind", "record_id", "content", "source_ids", "citations", "event_at", "assertion"}
CONTRACT_SHA256 = "510a2164f7db4747ac74b7dcd5e4b477449ca95f76a976657cdd9f75df886dea"


@pytest.fixture()
def derived(monkeypatch):
    monkeypatch.setenv(FLAG, "true")


# --- the value guards, one at a time ------------------------------------------------------------------

def labels(**changes):
    base = dict(authorship="owner_authored", speech="original_message", protected_content="none",
                sensitivity="personal")
    return SimpleNamespace(**{**base, **changes})


class Boundary:
    """`EntityBoundary`'s two scans, as guard 4 reads them: `mentions_protected` (whole terms, case-insensitive,
    over every text given) and `name_part_match_only` (a bare part of a protected name, as a whole word)."""

    def __init__(self, *terms, parts=(), fails=None):
        self.terms, self.parts, self.fails, self.seen = [t.casefold() for t in terms], set(parts), fails, []

    def mentions_protected(self, *texts):
        self.seen.append(texts)
        if self.fails is not None:
            raise self.fails
        return any(f" {term} " in f" {text.casefold().rstrip('.')} " for text in texts if isinstance(text, str)
                   for term in self.terms)

    def name_part_match_only(self, table, row):
        assert table == "journal_entries"
        return any(word in self.parts for text in row.values() for word in text.casefold().rstrip(".").split())


PEOPLE = frozenset({"quillon", "brennick"})
ENTRY = {"content": PROSE, "people": "Ysolde, Tamsin", "metadata_json": None}


def code(value, predicate="works_on", *, entry=ENTRY, label=None, boundary=None, people=PEOPLE):
    return refusal(value, predicate, entry, labels() if label is None else label,
                   boundary=Boundary() if boundary is None else boundary, people=people)


@pytest.mark.parametrize("value, predicate", [
    ("Atlas", "works_on"), ("Atlas Orion", "work.project"), ("Contoso Labs", "works_at"), ("Lisbon", "lives_in"),
    ("Chess Club", "member_of"), ("Python", "skilled_in"), ("dark roast coffee", "prefers"),
    ("ship the release", "commit.made"), ("backend engineer", "role_is"), ("today's build", "works_on"),
    ("smoke tests", "works_on"), ("R&D", "works_on"), ("Q3 roadmap", "work.project"), ("Pine-Ridge", "lives_in"),
    ("Smiths", "works_at"), ("cafe\u0301", None),
])
def test_a_plain_value_clears_every_guard(value, predicate):
    if predicate is None:          # a decomposed accent is not NFKC-stable: the one control that must fail
        assert code(value) == "inferred_value_shape"
        return
    assert code(value, predicate) is None


@pytest.mark.parametrize("label, expected", [
    (labels(authorship="other"), "inferred_entry_labels"),
    (labels(authorship="unknown"), "inferred_entry_labels"),
    (labels(speech="third_party_quote"), "inferred_entry_labels"),
    (labels(speech="mixed"), "inferred_entry_labels"),
    (labels(protected_content="present"), "inferred_entry_labels"),
    (labels(protected_content="unknown"), "inferred_entry_labels"),
    (None, "inferred_entry_labels"),
    (labels(sensitivity="special"), "inferred_entry_sensitivity"),
    (labels(sensitivity="unknown"), "inferred_entry_sensitivity"),
    (labels(sensitivity="none"), None),
])
def test_the_entrys_own_labels_come_first(label, expected):
    got = refusal("Atlas", "works_on", ENTRY, label, boundary=Boundary(), people=PEOPLE)
    assert got == expected


@pytest.mark.parametrize("value", [
    123, None, b"Atlas", "A", "x" * 201, " ".join(["word"] * 13),
    "At\u200blas",              # a zero-width space inside a word
    "Atl\u00adas",              # a soft hyphen (a format character)
    "Atlas\x07",                # a control character
    "\u0410tlas",               # a Cyrillic capital A
    "\u03a9mega",               # a Greek letter
    "x\u0301",                  # a combining mark NFKC leaves in place
    "\uff21tlas",               # a fullwidth letter (not NFKC-stable)
    "\ufb01le sync",             # a Latin ligature: NFC-stable, the label syntax accepts it, NFKC does not
    "Q\u00b2 plan",              # a superscript digit, likewise
    "Atlas, Orion",             # prose, not one label (the shared atomic-label syntax)
    " Atlas",                   # stray whitespace
    "Atlas?",                   # a question mark is not label syntax: shape withholds it first
    "example.com",              # so is a dot: a domain withholds as shape before guard 7 sees it
])
def test_a_value_that_is_not_one_plain_label_withholds_as_shape(value):
    assert code(value) == "inferred_value_shape"


def test_off_limits_in_the_value_or_in_the_wire_content_withholds():
    assert code("Zed Works", boundary=Boundary("zed works")) == "inferred_value_protected"
    # A term that spans the predicate's own words and the value is found in the wire content only.
    spanning = Boundary("project zed")
    assert code("Zed", "work.project", boundary=spanning) == "inferred_value_protected"
    assert spanning.seen[-1] == ("Zed", "Owner works on the project Zed.")
    assert code("Zed", "works_on", boundary=Boundary("project zed")) is None


class WholeTermsOnly:
    """A boundary that cannot run the name-part scan: guard 4 cannot answer, so it withholds."""

    def mentions_protected(self, *texts):
        return False


def test_a_bare_part_of_a_protected_name_withholds_and_a_boundary_without_that_scan_withholds():
    assert code("Quillon", boundary=Boundary(parts={"quillon"})) == "inferred_value_protected"
    assert code("Atlas", boundary=Boundary(parts={"quillon"})) is None
    # The template's own words are not the value: a protected name sharing one withholds nothing by it.
    assert code("Atlas", "work.project", boundary=Boundary(parts={"project", "owner"})) is None
    assert code("Atlas", boundary=WholeTermsOnly()) == "inferred_boundary_unavailable"


@pytest.mark.parametrize("boundary", [None, Boundary(fails=PolicyError("entity_protection_lineage_unavailable")),
                                      Boundary(fails=RuntimeError("boom"))])
def test_a_boundary_that_cannot_answer_withholds(boundary):
    got = refusal("Atlas", "works_on", ENTRY, labels(), boundary=boundary, people=PEOPLE)
    assert got == "inferred_boundary_unavailable"


@pytest.mark.parametrize("value", [
    "therapy notes",            # OD-38's SPECIAL, a word
    "psychiatry",               # a root inside a word
    "tonsillectomy",            # a medical ending
    "support group",            # a phrase
    "sobriety",                 # OD-38's SPECIAL
    "weed",                     # no verb slot in a value: weed, fast, scan, smoke count
    "fast",
    "Bible study",
    "union card",
])
def test_a_special_category_withholds(value):
    assert code(value) == "inferred_value_special"


@pytest.mark.parametrize("value", ["how to cook", "today what to build", "rock 'n roll", "should ship"])
def test_a_question_or_a_stray_apostrophe_withholds(value):
    assert code(value) == "inferred_value_question_or_quote"


@pytest.mark.parametrize("value, predicate", [
    ("tbd", "works_on"), ("lorem ipsum", "works_on"), ("goal 1", "works_on"), ("untitled", "work.project"),
    ("project", "work.project"), ("Project", "work.project"), ("description", "commit.made"),
    ("2026", "works_on"), ("12-14", "works_on"),
])
def test_a_placeholder_an_echo_or_a_value_with_no_letter_withholds(value, predicate):
    assert code(value, predicate) == "inferred_value_not_a_value"


def test_url_and_template_characters_withhold_where_shape_lets_them_through(monkeypatch):
    """Guard 7's URL and template tests sit behind guard 3: with the label syntax out of the way they still fire."""
    from topos.permissions_v2 import inferred_facts as module
    monkeypatch.setattr(module, "_shape_refused", lambda value: False)
    for value in ("https://example.org", "www.example", "a/b", "@handle", "#tag", "example.org", "{name}", "a=b",
                  "x;y", "<b>", "[x]", "$x"):
        assert value_refusal(value, "works_on", ENTRY, boundary=Boundary(), people=PEOPLE) == \
            "inferred_value_not_a_value", value


@pytest.mark.parametrize("value, predicate", [
    ("Quillon", "works_on"),                # a name word of a person or contact the node holds
    ("quillon", "work.project"),            # in any case
    ("Tamsin", "works_on"),                 # the entry's own people column
    ("my mom", "works_on"),                 # a relation
    ("landlord", "works_on"),               # a role
    ("teammates", "works_on"),
    ("grandparents", "works_on"),
    ("date night", "works_on"),             # a third-party phrase
    ("florist", "works_on"),                # a person by trade
    ("Mr Robot", "works_on"),               # an honorific
    ("Ms Lantern", "work.project"),
    ("Ana's project", "works_on"),          # a possessive other than a time's
    ("the Smiths' app", "works_on"),        # a plural possessive
    ("Machine Learning", "skilled_in"),     # a capitalised word after the first, where no proper noun is expected
    ("Earl Grey tea", "prefers"),
    ("finish the Atlas migration", "commit.made"),
])
def test_a_value_naming_a_person_withholds(value, predicate):
    assert code(value, predicate) == "inferred_value_names_person"


def test_people_that_could_not_be_read_withhold():
    assert code("Atlas", people=None) == "inferred_value_names_person"


def test_the_people_column_is_read_from_a_row_of_any_shape():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 'Tamsin' AS people").fetchone()
    assert code("Tamsin", entry=row, people=frozenset()) == "inferred_value_names_person"
    assert code("Tamsin", entry={"people": "Tamsin"}, people=frozenset()) == "inferred_value_names_person"
    assert code("Tamsin", entry={"content": PROSE}, people=frozenset()) is None   # no column: no one named there


def test_the_guards_run_in_their_fixed_order():
    """A value failing several guards reports the earliest one: labels, sensitivity, shape, Off-limits, special,
    question, not-a-value, person."""
    assert code("my therapy?", label=labels(authorship="other")) == "inferred_entry_labels"
    assert code("my therapy?", label=labels(sensitivity="special")) == "inferred_entry_sensitivity"
    assert code("my therapy?") == "inferred_value_shape"
    assert code("Quillon therapy", boundary=Boundary("quillon therapy")) == "inferred_value_protected"
    assert code("Quillon therapy") == "inferred_value_special"
    assert code("how Quillon") == "inferred_value_question_or_quote"
    assert code("Quillon tbd") == "inferred_value_not_a_value"
    assert code("Quillon") == "inferred_value_names_person"


def test_every_code_is_declared():
    assert set(inferred_facts.CODES) == {
        "inferred_entry_labels", "inferred_entry_sensitivity", "inferred_value_shape", "inferred_value_protected",
        "inferred_boundary_unavailable", "inferred_value_special", "inferred_value_question_or_quote",
        "inferred_value_not_a_value", "inferred_value_names_person"}
    assert inferred_facts.VERSION == "inferred-fact-guards/v1"


@pytest.mark.parametrize("env, expected", [
    ({}, False),
    ({FLAG: "true"}, False),                                   # inert without the journal family
    ({JOURNAL_FLAG: "true"}, False),
    ({FLAG: "true", JOURNAL_FLAG: "true"}, True),
    ({FLAG: "ON", JOURNAL_FLAG: "1"}, True),                   # read as the family flags are
    ({FLAG: "yes", JOURNAL_FLAG: "yes"}, True),
    ({FLAG: "false", JOURNAL_FLAG: "true"}, False),
    ({FLAG: "", JOURNAL_FLAG: "true"}, False),
])
def test_the_flag_is_the_owners_and_needs_the_journal_family(env, expected):
    assert inferred_facts.enabled(env) is expected


# --- the engine: release path ---------------------------------------------------------------------------

def _inferred(path, value="Atlas", *, predicate="works_on", entry="e1", content=PROSE, **entry_columns):
    """An entry that does not state the fact, the extractor's fact citing it, and the entry's assessment."""
    _attest_owner(path)
    _entry(path, entry, content, **entry_columns)
    fact = _fact(path, _cites(entry), predicate=predicate, value=value)
    _publish(path, entry)
    return fact


@pytest.mark.parametrize("precision, expected", [("none", None), ("day", DAY), ("second", None)])
def test_an_inferred_fact_releases_citing_its_entry(node, tmp_path, monkeypatch, derived, precision, expected):
    from topos.permissions_v2.knowledge_contract import KnowledgeSearchResult
    _inferred(node)
    search, state = _node(node, tmp_path, monkeypatch, precision=precision)
    assert state == {"state": "ready", "member_count": 2}             # the entry, and the fact drawn from it
    records, bindings = _search(search, monkeypatch, "Atlas parser")
    (entry,) = _kind(records, "journal_entry")
    (item,) = _kind(records, "fact")
    assert item == {"kind": "fact", "record_id": item["record_id"], "content": "Owner works on Atlas.",
                    "source_ids": [SOURCE], "assertion": "inferred", "event_at": expected,
                    "citations": [{"record_id": entry["record_id"], "source_id": SOURCE, "content": PROSE}]}
    assert set(item) == FACT_KEYS and "grounding" not in item         # v1: no new key on the wire
    binding = bindings[item["record_id"]]
    assert (binding["kind"], binding["evidence_tables"], binding["source_ids"]) == \
        ("fact", ["journal_entries"], [SOURCE])
    # The CP parses the node's reply with its own copy of this module, byte for byte the engine's.
    contract = Path(inferred_facts.__file__).with_name("knowledge_contract.py").read_bytes()
    assert hashlib.sha256(contract).hexdigest() == CONTRACT_SHA256
    parsed = KnowledgeSearchResult.parse(dict(family="canonical_record", operation="search",
                                              view_id="canonical.knowledge_search.v1", records=records))
    assert [r.model_dump(exclude_none=False) for r in parsed.records if r.kind == "fact"] == [item]


def test_a_stated_fact_is_still_owner_stated_with_the_flag_on(node, tmp_path, monkeypatch, derived):
    _inferred(node, content=ATLAS)                                    # "I work on Atlas." states it
    search, _state = _node(node, tmp_path, monkeypatch)
    (item,) = _kind(_search(search, monkeypatch, "Atlas")[0], "fact")
    assert item["assertion"] == "owner_stated"


def test_a_work_project_fact_releases_through_its_class_scalar(node, tmp_path, monkeypatch, derived):
    fact = _inferred(node, "Orion Kestrel", predicate="work.project")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) is None
    (item,) = _kind(_search(search, monkeypatch, "Orion Kestrel")[0], "fact")
    assert (item["content"], item["assertion"]) == ("Owner works on the project Orion Kestrel.", "inferred")


# --- flag off: today's bytes ------------------------------------------------------------------------------

def test_with_the_flag_off_the_fact_is_not_grounded_and_nothing_inferred_exists(node, tmp_path, monkeypatch):
    from topos.permissions_v2.search_index import _family_rubric_basis
    monkeypatch.delenv(FLAG, raising=False)
    fact = _inferred(node)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 1                                 # the entry only
    assert _code(search, "signal_objects", fact) == "fact_not_grounded"
    records, _bindings = _search(search, monkeypatch, "Atlas parser")
    assert _kind(records, "fact") == [] and "inferred" not in json.dumps(records)
    assert "inferred_facts" not in json.dumps(_family_rubric_basis())


def test_with_the_flag_off_a_stated_fact_item_is_the_golden_one(node, tmp_path, monkeypatch):
    """Golden: today's fact item, key for key, and the same bytes with the flag on (the stated path is untouched)."""
    monkeypatch.delenv(FLAG, raising=False)
    _inferred(node, content=ATLAS)
    search, _state = _node(node, tmp_path, monkeypatch, precision="day")
    records, _bindings = _search(search, monkeypatch, "Atlas")
    (entry,) = _kind(records, "journal_entry")
    (off,) = _kind(records, "fact")
    assert off == {"kind": "fact", "record_id": off["record_id"], "content": "Owner works on Atlas.",
                   "source_ids": [SOURCE], "citations": [{"record_id": entry["record_id"], "source_id": SOURCE,
                                                         "content": ATLAS}],
                   "event_at": DAY, "assertion": "owner_stated"}
    # The order the release's own parse writes (FactResult's fields), which the wire bytes follow.
    assert list(off) == ["record_id", "content", "source_ids", "citations", "event_at", "kind", "assertion"]
    monkeypatch.setenv(FLAG, "true")
    with owner():
        assert search.index.rebuild("grant-search", now=search.now[0])["state"] == "ready"
    (on,) = _kind(_search(search, monkeypatch, "Atlas")[0], "fact")
    assert json.dumps(on, sort_keys=False) == json.dumps(off, sort_keys=False)


def test_with_the_flag_on_but_no_journal_family_nothing_changes(node, tmp_path, monkeypatch, derived):
    from topos.permissions_v2.refresh_loop import RefreshSettings
    from topos.permissions_v2.search_index import _family_rubric_basis
    fact = _inferred(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    from topos.permissions_v2.interest_index import FLAG as INTEREST_FLAG
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)
    monkeypatch.delenv(INTEREST_FLAG, raising=False)
    assert not inferred_facts.enabled()
    assert _family_rubric_basis() == {}                               # a messages-only basis keeps its bytes
    assert _code(search, "signal_objects", fact) == "lineage_unsupported"      # as before IF-6
    restore = {"TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED": "true", FLAG: "true"}
    assert RefreshSettings.from_env(restore).facts is False
    assert RefreshSettings.from_env({**restore, JOURNAL_FLAG: "true"}).facts is True


def test_a_message_cited_fact_is_out_of_scope_and_inert_without_the_flag(node, tmp_path, monkeypatch, derived):
    """A fact the floor does not ground and that cites no journal entry is never inferred (message-cited facts are
    out of v1): `inferred_fact_scope` with the flag on, `fact_not_grounded` with it off, as before."""
    from tests.permissions_v2.test_journal_typed_items import EXPORT, _export_message, _mixed_policy
    _attest_owner(node)
    _export_message(node, "x-1", "Long day on the parser.")
    fact = _fact(node, [{"table": "ai_chat_messages", "record_id": "x-1", "source_id": EXPORT}])
    _publish(node, "x-1", table="ai_chat_messages", source=EXPORT)
    search, _state = _node(node, tmp_path, monkeypatch, grant=_mixed_policy)
    assert _code(search, "signal_objects", fact) == "inferred_fact_scope"
    monkeypatch.delenv(FLAG)
    assert _code(search, "signal_objects", fact) == "fact_not_grounded"


# --- the grant ---------------------------------------------------------------------------------------------

def test_a_grant_without_journal_entries_releases_nothing_from_the_journal(node, tmp_path, monkeypatch, derived):
    fact = _inferred(node)
    search, state = _node(node, tmp_path, monkeypatch, kinds=("message", "fact", "goal", "relationship"))
    assert state["member_count"] == 0
    assert _search(search, monkeypatch, "Atlas parser")[0] == []
    assert _code(search, "signal_objects", fact) == "journal_citation_needs_record_option"


def test_step_seven_checks_the_journal_option_on_its_own(node, tmp_path, monkeypatch, derived):
    """Guard independence: `_journal_citation` refuses first today, so an end-to-end test cannot see step 7's own
    check. With the citation's check removed, the inferred path still refuses a grant without the option."""
    from topos.permissions_v2 import knowledge_projections
    fact = _inferred(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    monkeypatch.setattr(knowledge_projections, "_journal_citation", lambda *args, **kwargs: None)
    from tests.permissions_v2.test_journal_family import _journal_policy
    no_option = _journal_policy(kinds=("message", "fact", "goal", "relationship"))
    assert _code(search, "signal_objects", fact, raw=no_option) == "journal_citation_needs_record_option"
    assert _code(search, "signal_objects", fact) is None


# --- every other check still applies --------------------------------------------------------------------------

def test_a_value_equal_to_a_known_persons_name_part_withholds(node, tmp_path, monkeypatch, derived):
    """Lane K: the node's main project name is also a name word of person entities there. Fail closed."""
    from tests.permissions_v2.test_owner_identity_binding import add_entity
    fact = _inferred(node, "Brennick")
    with _db(node) as conn:
        add_entity(conn, "person-1", is_self=0, entity_type="person")
        conn.execute("UPDATE entities SET canonical_name='Ysolde Brennick' WHERE entity_id='person-1'")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "inferred_value_names_person"
    assert _kind(_search(search, monkeypatch, "Brennick parser")[0], "fact") == []


def test_a_name_in_the_entrys_people_column_withholds(node, tmp_path, monkeypatch, derived):
    fact = _inferred(node, "Tamsin", people="Tamsin")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "inferred_value_names_person"


def test_off_limits_spanning_the_predicate_text_and_the_value_withholds(node, tmp_path, monkeypatch, derived):
    """The fact row's own Off-limits veto (step 2) sees "Zed"; only the wire content says "project Zed"."""
    _attest_owner(node)
    _entry(node, "e1", PROSE)
    fact = _fact(node, _cites("e1"), predicate="work.project", value="Zed")
    _off_limits(node, "Project Zed")       # before the assessment: a later name would stale every entry's context
    _publish(node, "e1")
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 1                                 # the entry is clean and releases
    assert _code(search, "signal_objects", fact) == "inferred_value_protected"


@pytest.mark.parametrize("value, expected", [("Quillon", "inferred_value_protected"),
                                             ("Quillon's notes", "inferred_value_protected"),
                                             ("Marsh Survey", "inferred_value_protected"),
                                             ("Qui\u0301llon", "inferred_value_shape")])   # a mark: shape first
def test_a_bare_part_of_an_off_limits_name_in_the_value_withholds(node, tmp_path, monkeypatch, derived, value,
                                                                  expected):
    """The value is drawn from a journal entry, and a journal entry withholds on a bare part of an Off-limits name
    (NAME_PART_TABLES). `mentions_protected` matches whole terms only, and a name-only Off-limits term is no person
    or contact the node holds, so neither the fact row's veto nor guard 8 would see it: guard 4 applies the journal
    family's name-part rule to the value as well."""
    _attest_owner(node)
    _entry(node, "e1", PROSE)
    fact = _fact(node, _cites("e1"), value=value)
    _off_limits(node)                      # "Quillon Marsh", before the assessment
    _publish(node, "e1")
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 1      # the entry itself names no one and releases
    assert _code(search, "signal_objects", fact) == expected
    assert _kind(_search(search, monkeypatch, "Quillon Marsh parser")[0], "fact") == []


def test_off_limits_in_the_value_is_vetoed_on_the_fact_row_first(node, tmp_path, monkeypatch, derived):
    fact = _inferred(node, "Quillon Marsh Studio")
    _off_limits(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "entity_protected"


def test_off_limits_in_the_entry_withholds_the_inferred_fact(node, tmp_path, monkeypatch, derived):
    _attest_owner(node)
    _entry(node, "e1", PROSE, people="Quillon Marsh")
    fact = _fact(node, _cites("e1"))
    _publish(node, "e1")
    _off_limits(node)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 0
    assert _code(search, "signal_objects", fact) == "entity_protected"


def test_a_special_value_withholds(node, tmp_path, monkeypatch, derived):
    fact = _inferred(node, "physio exercises")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "inferred_value_special"


def test_an_entry_labelled_special_withholds_under_the_grants_decision(node, tmp_path, monkeypatch, derived):
    _attest_owner(node)
    _entry(node, "e1", PROSE)
    fact = _fact(node, _cites("e1"))
    _publish(node, "e1", sensitivity="special")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "evidence_not_permitted"


def _special_grant(**policy):
    """A grant whose rule also permits health and special: the inferred path's own checks must still hold."""
    from tests.permissions_v2.test_journal_family import _journal_policy
    raw = _journal_policy(**policy)
    (rule,) = raw["rules"]
    for predicate in (rule["evidence_use"]["predicate"], rule["release"]["predicate"]):
        predicate["terms"][1]["values"] = ["none", "personal", "special"]
    return raw


def test_an_entry_labelled_special_withholds_even_where_the_grant_permits_it(node, tmp_path, monkeypatch, derived):
    _attest_owner(node)
    _entry(node, "e1", PROSE)
    fact = _fact(node, _cites("e1"))
    _publish(node, "e1", sensitivity="special")
    search, _state = _node(node, tmp_path, monkeypatch, grant=_special_grant)
    assert _code(search, "signal_objects", fact) == "inferred_entry_sensitivity"


@pytest.mark.parametrize("grant, expected", [(None, "evidence_not_permitted"), (_special_grant,
                                                                                "fact_projection_unsupported")])
def test_a_health_predicate_never_releases_as_inferred(node, tmp_path, monkeypatch, derived, grant, expected):
    """Lane K: `practices` passes the head check (PREDICATE_TEXT names it), and only its implicit special label in
    the grant decision stopped it on the measured copy. Step 7 also requires a releasable class."""
    fact = _inferred(node, "yoga", predicate="practices")
    search, _state = _node(node, tmp_path, monkeypatch, **({"grant": grant} if grant else {}))
    assert _code(search, "signal_objects", fact) == expected


def test_an_nsfw_entry_withholds(node, tmp_path, monkeypatch, derived):
    _attest_owner(node)
    _entry(node, "e1", PROSE, content_nsfw=1)
    fact = _fact(node, _cites("e1"))
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "unsupported_message_content"


@pytest.mark.parametrize("restricted", ["entry", "fact"])
def test_owner_only_withholds(node, tmp_path, monkeypatch, derived, restricted):
    fact = _inferred(node)
    _restrict(node, *(("journal_entries", "e1") if restricted == "entry" else ("signal_objects", fact)))
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "owner_only"


@pytest.mark.parametrize("excluded, expected", [("entry", "intelligence_excluded"), ("fact", "owner_opted_out")])
def test_an_exclusion_withholds(node, tmp_path, monkeypatch, derived, excluded, expected):
    fact = _inferred(node)
    with _db(node) as conn:
        conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key, created_at) "
                     "VALUES ('x1','record',?,'t')", ("e1" if excluded == "entry" else fact,))
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == expected


def test_a_subject_that_is_not_the_attested_self_withholds(node, tmp_path, monkeypatch, derived):
    from topos.features.facts.store import FactStore
    _attest_owner(node)
    _entry(node, "e1", PROSE)
    with _db(node) as conn:
        FactStore(conn).assert_fact(subject_entity_id="someone-else", predicate="works_on", object_value="Atlas",
                                    confidence=1, source_refs=_cites("e1"), disclosure="owner_only", asserted_by="owner")
        fact = conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'").fetchone()[0]
    _publish(node, "e1")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "fact_projection_unsupported"


def test_an_entry_outside_the_window_withholds(node, tmp_path, monkeypatch, derived):
    fact = _inferred(node, entry_at="2026-06-01T08:30:00")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "evidence_outside_window"


def test_a_fact_citing_two_entries_or_an_entry_and_a_message_is_out_of_scope(node, tmp_path, monkeypatch, derived):
    from tests.permissions_v2.test_journal_typed_items import EXPORT, _export_message, _mixed_policy
    _attest_owner(node)
    _entry(node, "e1", PROSE)
    _entry(node, "e2", "Another long day, mostly reviews.", entry_at="2026-09-10T09:30:00")
    _export_message(node, "x-1", "Long day on the parser.")
    two = _fact(node, _cites("e1") + _cites("e2"))
    mixed = _fact(node, [{"table": "ai_chat_messages", "record_id": "x-1", "source_id": EXPORT}, *_cites("e1")],
                  value="Atlas Two")
    for record, table, source in (("e1", "journal_entries", SOURCE), ("e2", "journal_entries", SOURCE),
                                  ("x-1", "ai_chat_messages", EXPORT)):
        _publish(node, record, table=table, source=source)
    search, _state = _node(node, tmp_path, monkeypatch, grant=_mixed_policy)
    assert _code(search, "signal_objects", two) == _code(search, "signal_objects", mixed) == "inferred_fact_scope"


def test_a_fact_and_its_twin_cited_together_are_one_source_and_release_once(node, tmp_path, monkeypatch, derived):
    _attest_owner(node)
    _entry(node, "e1", PROSE)
    _entry(node, "e2", PROSE, entry_at="2026-09-10T09:30:00")         # the same words re-pushed: an alias
    fact = _fact(node, _cites("e1") + _cites("e2"))
    _publish(node, "e1")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) is None
    (item,) = _kind(_search(search, monkeypatch, "Atlas parser")[0], "fact")
    assert len(item["citations"]) == 1 and item["assertion"] == "inferred"


def test_a_fact_citing_more_than_twenty_records_is_refused_with_the_flag_on(node, tmp_path, monkeypatch, derived):
    """Lane K: the owner's main project fact cites 80 records. v1 keeps today's rule: every cited record must pass,
    and at most MAX_SUPPORT (20) are read; more is refused before any grounding rule runs."""
    from topos.permissions_v2.knowledge_projections import MAX_SUPPORT
    _attest_owner(node)
    refs = []
    for n in range(MAX_SUPPORT + 1):
        _entry(node, f"e{n}", f"Day {n} on the parser.", entry_at=f"2026-09-{1 + n % 9:02d}T08:30:00")
        refs += _cites(f"e{n}")
    fact = _fact(node, refs)
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "lineage_identity_incomplete"


# --- OD-59: closures -------------------------------------------------------------------------------------------

def test_a_closed_fact_never_releases_and_its_successor_does(node, tmp_path, monkeypatch, derived):
    """A fact closed by re-derivation (OD-59's writer supersession) is not current; it stops withholding the entry,
    and the current successor citing the same entry releases as inferred."""
    from tests.permissions_v2.test_closed_fact_floor import MODEL, fact as stored_fact
    _attest_owner(node)
    _entry(node, "e1", PROSE)
    cite = _cites("e1")[0]
    with _db(node) as conn:
        stored_fact(conn, "f-closed", refs=[cite], value="Atlas", valid_to="2026-09-20T10:00:00.000000+00:00",
                    extractor_version="derivation:t", closed_reason="superseded", extractor=MODEL)
        stored_fact(conn, "f-next", refs=[cite], value="Atlas Two", valid_from="2026-09-19",
                    created_at="2026-09-20T10:00:00.000001+00:00", extractor_version="derivation:t", extractor=MODEL)
    _publish(node, "e1")
    search, state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", "f-closed") == "fact_not_current"
    assert _code(search, "signal_objects", "f-next") is None
    records, _bindings = _search(search, monkeypatch, "Atlas Two parser")
    assert [r["assertion"] for r in _kind(records, "fact")] == ["inferred"] and _kind(records, "journal_entry")


def test_an_owner_closure_keeps_withholding_the_entry_and_its_facts(node, tmp_path, monkeypatch, derived):
    from tests.permissions_v2.test_closed_fact_floor import fact as stored_fact
    _attest_owner(node)
    _entry(node, "e1", PROSE)
    cite = _cites("e1")[0]
    _publish(node, "e1")
    with _db(node) as conn:
        stored_fact(conn, "f-mine", refs=[cite], value="Atlas", valid_to="2026-09-20 10:00:00")   # no machine marker
        stored_fact(conn, "f-other", refs=[cite], value="Atlas Two", key="fact:owner-entity:works_on:two")
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 0
    assert _code(search, "signal_objects", "f-other") == "evidence_deleted"


# --- the index basis -----------------------------------------------------------------------------------------

def test_the_index_basis_moves_with_the_flag_and_the_sweep_drops_the_index(node, tmp_path, monkeypatch):
    from topos.permissions_v2.search_index import _family_rubric_basis
    monkeypatch.delenv(FLAG, raising=False)
    off = _family_rubric_basis()
    _inferred(node)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 1
    assert search.index.sweep(now=search.now[0]) == 0                 # current under its own basis
    monkeypatch.setenv(FLAG, "true")
    on = _family_rubric_basis()
    assert on["automatic_rubric_revisions"] == {**off["automatic_rubric_revisions"],
                                                "inferred_facts": inferred_facts.VERSION}
    assert search.index.sweep(now=search.now[0]) == 1                 # stale("basis"): dropped
    with owner():
        assert search.index.rebuild("grant-search", now=search.now[0])["member_count"] == 2
    monkeypatch.setenv(FLAG, "false")
    assert search.index.sweep(now=search.now[0]) == 1                 # and back again


def test_the_nodes_people_are_read_once_per_snapshot(node, tmp_path, monkeypatch, derived):
    from topos.permissions_v2 import journal_goal_field
    _inferred(node, "Atlas")
    _entry(node, "e2", "Another long day, mostly reviews.", entry_at="2026-09-10T09:30:00")
    _fact(node, _cites("e2"), value="Kestrel")
    _publish(node, "e2")
    calls = []
    real = journal_goal_field.known_people
    monkeypatch.setattr(journal_goal_field, "known_people", lambda conn: calls.append(1) or real(conn))
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 4 and len(calls) == 1             # two inferred facts, one build snapshot


def test_unreadable_people_withhold(node, tmp_path, monkeypatch, derived):
    import sqlite3
    from topos.permissions_v2 import journal_goal_field
    fact = _inferred(node)

    def broken(conn):
        raise sqlite3.OperationalError("synthetic")
    monkeypatch.setattr(journal_goal_field, "known_people", broken)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 1
    assert _code(search, "signal_objects", fact) == "evidence_storage_unavailable"


# --- the §9 must-release mix, as a coverage check (Lane O's blind set is the gate; this is not it) ----------------

MUST_RELEASE = [
    *[("work.project", value) for value in ("Atlas", "Orion Kestrel", "Lantern", "Harbor", "Juniper", "Quartz Bridge",
                                            "Meridian", "Tidewater", "data pipeline", "parser rewrite",
                                            "release tooling", "billing service", "search index", "build cache",
                                            "docs site")],
    *[("works_at", value) for value in ("Contoso Labs", "Northwind", "Fabrikam", "Tailspin Toys", "Wingtip")],
    *[("lives_in", value) for value in ("Lisbon", "Porto", "Bergen", "Tallinn", "Cork")],
    *[("prefers", value) for value in ("dark roast coffee", "green tea", "trail running", "board games", "jazz")],
    *[("skilled_in", value) for value in ("Python", "Rust", "woodworking")],
    *[("member_of", value) for value in ("Chess Club", "Rowing Club", "Hiking Group")],
    *[("commit.made", value) for value in ("ship the release", "finish the parser", "write the docs",
                                           "clean the garage")],
]
STATED = [("lives_in", "Lisbon", "I live in Lisbon."), ("prefers", "green tea", "I prefer green tea."),
          ("skilled_in", "Python", "I am skilled in Python."), ("member_of", "Chess Club", "I am a member of Chess Club."),
          ("works_at", "Contoso Labs", "I work at Contoso Labs.")]


def _assertion(search, record_id):
    from topos.permissions_v2.knowledge_projections import qualify_projection
    from topos.permissions_v2.registry import parse_policy
    policy = parse_policy(search.search_raw)
    now = search.now[0]
    resolver, reviews = search.corpus.resolver, search.corpus.reviews
    with resolver._read() as (conn, floor), reviews._db() as db:
        projected = qualify_projection(resolver, conn, floor, reviews, db, "signal_objects", record_id, policy,
                                       (now - policy.search.window.max_age_seconds) * 10**6, now * 10**6)
    return projected.fields["assertion"]


def test_forty_plain_inferred_facts_release_and_five_stated_controls_stay_stated(node, tmp_path, monkeypatch,
                                                                                 derived):
    from tests.permissions_v2.test_closed_fact_floor import fact as stored_fact
    assert len(MUST_RELEASE) == 40 and sum(p == "work.project" and v[0].isupper() for p, v in MUST_RELEASE) >= 8
    _attest_owner(node)
    facts, stated = [], []
    with _db(node) as conn:      # stored as the extractor leaves them: one current fact per (predicate, value)
        for n, (predicate, value) in enumerate(MUST_RELEASE):
            _entry(node, f"e{n}", f"Notes from session {n}, mostly routine.", entry_at=f"2026-09-{1 + n % 10:02d}T08:30:00")
            stored_fact(conn, f"f-{n}", refs=_cites(f"e{n}"), key=f"fact:owner-entity:{predicate}:{n}", value=value,
                        predicate=predicate)
            facts.append(f"f-{n}")
        for n, (predicate, value, text) in enumerate(STATED):
            _entry(node, f"s{n}", text, entry_at=f"2026-09-0{1 + n}T09:30:00")
            stored_fact(conn, f"f-s{n}", refs=_cites(f"s{n}"), key=f"fact:owner-entity:{predicate}:s{n}", value=value,
                        predicate=predicate)
            stated.append(f"f-s{n}")
    for entry in [f"e{n}" for n in range(len(MUST_RELEASE))] + [f"s{n}" for n in range(len(STATED))]:
        _publish(node, entry)
    search, state = _node(node, tmp_path, monkeypatch)
    codes = {value: _code(search, "signal_objects", fact) for (_p, value), fact in zip(MUST_RELEASE, facts)}
    assert {value: code for value, code in codes.items() if code} == {}
    assert state == {"state": "ready", "member_count": 2 * (len(MUST_RELEASE) + len(STATED))}
    assert [_code(search, "signal_objects", fact) for fact in facts + stated] == [None] * 45
    assert [_assertion(search, fact) for fact in facts] == ["inferred"] * 40
    assert [_assertion(search, fact) for fact in stated] == ["owner_stated"] * 5
