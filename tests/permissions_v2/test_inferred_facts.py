"""Private IF-6 guard regressions and explicit legacy-sharing refusal.

Selective ontology sharing retires inferred fact delivery on ordinary shares.
Pure experimental guard tests remain; old public-release expectations are replaced
by checks that neither inferred flags nor legacy policy fields confer consent.
Synthetic fixtures only.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_journal_family import SOURCE, _db, _entry, node, owner  # noqa: F401 (node: fixture)
from tests.permissions_v2.test_journal_typed_items import (ATLAS, _attest_owner, _cites, _code, _fact, _kind, _node, _publish, _search)
from topos.permissions_v2 import inferred_facts
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.evidence_families import JOURNAL_FLAG
from topos.permissions_v2.inferred_facts import FLAG, refusal, value_refusal

PROSE = "Long day on the parser and the release build."   # states nothing a class form would ground


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


def code(value, predicate="works_on", *, entry=ENTRY, label=None, boundary=None, people=PEOPLE, model="none"):
    return refusal(value, predicate, entry, labels() if label is None else label,
                   boundary=Boundary() if boundary is None else boundary, people=people,
                   model_protected_content=model)


@pytest.mark.parametrize("value, predicate", [
    ("Atlas", "works_on"), ("Juniper", "work.project"), ("Contoso", "works_at"), ("Lisbon", "lives_in"),
    ("Northgate", "member_of"), ("Python", "skilled_in"), ("coffee", "prefers"), ("data pipeline", "work.project"),
    ("ship the release", "commit.made"), ("today's build", "works_on"), ("release tooling", "work.project"),
    ("Q3 roadmap", "work.project"), ("search index", "works_on"), ("woodworking", "skilled_in"),
    ("cafe\u0301", None),
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
    got = refusal("Atlas", "works_on", ENTRY, label, boundary=Boundary(), people=PEOPLE, model_protected_content="none")
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
    got = refusal("Atlas", "works_on", ENTRY, labels(), boundary=boundary, people=PEOPLE, model_protected_content="none")
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


@pytest.mark.parametrize("value, predicate", [
    ("Atlas Orion", "work.project"),        # two capitalised words: the exception is one token
    ("Contoso Labs", "works_at"),
    ("Chess Club", "member_of"),
    ("dark roast coffee", "prefers"),       # an ordinary word the vocabulary lacks
    ("the arrears schedule", "work.project"),
    ("backend engineer", "role_is"),
    ("Pine-Ridge", "lives_in"),             # a hyphen makes two tokens
    ("smoke tests", "works_on"),            # "smoke" is special without a verb slot ("smoke test" is the H1 idiom)
    ("quillon", "works_on"),                # one token, but not capitalised: no exception
    ("Psychopathy", "works_on"),            # one capitalised token with a special-category root inside it
])
def test_a_word_the_closed_vocabulary_lacks_withholds_unless_it_is_one_capitalised_token(value, predicate):
    """v1b (blind set 2): the special lists alone released 11 special values in ordinary words. Every word must now
    be one Lane H1's rule has vetted, or the value must be one capitalised token with no special root."""
    assert code(value, predicate) == "inferred_value_special"


def test_the_one_token_exception_still_meets_off_limits_and_the_persons_guard():
    """The exception lets a project, employer or place name through guard 5 only: guard 4 has already run on it,
    and guard 8 runs after it, so an Off-limits term, a name part or a known person's name word never releases."""
    assert code("Kestrel") is None
    assert code("Kestrel", boundary=Boundary("kestrel")) == "inferred_value_protected"
    assert code("Kestrel", boundary=Boundary(parts={"kestrel"})) == "inferred_value_protected"
    assert code("Brennick") == "inferred_value_names_person"         # a name word the node holds (PEOPLE)
    assert code("Ysolde") == "inferred_value_names_person"           # the entry's own people column
    assert code("Baker") == "inferred_value_names_person"            # a trade
    assert code("Florist") == "inferred_value_names_person"          # a person by trade ending
    assert code("Bookseller") == "inferred_value_names_person"       # a compound by trade


@pytest.mark.parametrize("value", ["how to cook", "when to ship", "which plan", "draft 'notes"])
def test_a_question_or_a_stray_apostrophe_withholds(value):
    assert code(value) == "inferred_value_question_or_quote"


@pytest.mark.parametrize("value, predicate", [
    ("TBD", "works_on"), ("Lorem", "works_on"), ("goal 1", "works_on"), ("Untitled", "work.project"),
    ("project", "work.project"), ("Project", "work.project"), ("Description", "commit.made"),
    ("2026", "works_on"), ("12-14", "works_on"),
])
def test_a_placeholder_an_echo_or_a_value_with_no_letter_withholds(value, predicate):
    assert code(value, predicate) == "inferred_value_not_a_value"


@pytest.mark.parametrize("value", ["tbd", "lorem ipsum", "description"])
def test_a_lowercase_placeholder_the_vocabulary_lacks_withholds_at_guard_five(value):
    assert code(value) == "inferred_value_special"


def test_url_and_template_characters_withhold_where_shape_lets_them_through(monkeypatch):
    """Guard 7's URL and template tests sit behind guard 3: with the label syntax out of the way they still fire."""
    from topos.permissions_v2 import inferred_facts as module
    monkeypatch.setattr(module, "_shape_refused", lambda value: False)
    monkeypatch.setattr(module, "_vocabulary", lambda raw, plain: True)
    for value in ("https://example.org", "www.example", "a/b", "@handle", "#tag", "example.org", "{name}", "a=b",
                  "x;y", "<b>", "[x]", "$x"):
        assert value_refusal(value, "works_on", ENTRY, boundary=Boundary(), people=PEOPLE) == \
            "inferred_value_not_a_value", value


@pytest.mark.parametrize("value, predicate", [
    ("Quillon", "works_on"),                # a name word of a person or contact the node holds
    ("Tamsin", "works_on"),                 # the entry's own people column
    ("Landlord", "works_on"),               # a role
    ("Teammates", "works_on"),
    ("Grandparents", "works_on"),           # a relation
    ("date night", "works_on"),             # a third-party phrase, in vetted words
    ("Florist", "works_on"),                # a person by trade ending
    ("Baker", "work.project"),              # a trade (v1b), under every predicate
    ("Carpenter", "lives_in"),
    ("Bookseller", "prefers"),              # a compound by trade (v1b)
    ("Fisherman", "works_at"),
    ("Shoemaker", "works_on"),              # compounds no list names: the ending alone (v1b)
    ("Gatekeeper", "prefers"),
    ("Doorman", "works_at"),
    ("Spokesperson", "member_of"),
    ("Mr", "works_on"),                     # an honorific
    ("Ana's", "works_on"),                  # a possessive other than a time's
    ("the users' build", "works_on"),       # a plural possessive, in vetted words
    ("Python Testing", "skilled_in"),       # a capitalised word after the first, where no proper noun is expected
    ("write the Release docs", "commit.made"),
])
def test_a_value_naming_a_person_withholds(value, predicate):
    assert code(value, predicate) == "inferred_value_names_person"


@pytest.mark.parametrize("value", ["Human", "German", "Specimen", "Ottoman"])
def test_a_word_ending_like_a_trade_but_naming_none_is_not_read_as_one(value):
    assert code(value) is None


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
    """A value failing several guards reports the earliest one: labels, sensitivity, the entry's own marks (v1c),
    the entry's special cues (v1c), shape, Off-limits, special, question, not-a-value, person."""
    marked = {**ENTRY, "content": PROSE + "\nTags: private chiropractor"}
    cued = {**ENTRY, "content": PROSE + " Then the chiropractor."}
    assert code("my therapy?", model="unknown", entry=marked) == "inferred_entry_labels"
    assert code("my therapy?", label=labels(authorship="other")) == "inferred_entry_labels"
    assert code("my therapy?", label=labels(sensitivity="special"), entry=marked) == "inferred_entry_sensitivity"
    assert code("my therapy?", entry=marked) == "inferred_entry_marked_special"
    assert code("my therapy?", entry=cued) == "inferred_entry_special_cue"
    assert code("my therapy?") == "inferred_value_shape"
    assert code("Quillon therapy", boundary=Boundary("quillon therapy")) == "inferred_value_protected"
    assert code("Quillon therapy") == "inferred_value_special"
    assert code("Quillon notes") == "inferred_value_special"           # the vocabulary lacks "quillon"
    assert code("how to build") == "inferred_value_question_or_quote"
    assert code("goal 1") == "inferred_value_not_a_value"
    assert code("Quillon") == "inferred_value_names_person"


def test_every_code_is_declared():
    assert set(inferred_facts.CODES) == {
        "inferred_entry_labels", "inferred_entry_sensitivity", "inferred_entry_marked_special",
        "inferred_entry_special_cue", "inferred_value_shape", "inferred_value_protected",
        "inferred_boundary_unavailable", "inferred_value_special", "inferred_value_question_or_quote",
        "inferred_value_not_a_value", "inferred_value_names_person"}
    assert inferred_facts.VERSION == "inferred-fact-guards/v1c"


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


# --- flag off: today's bytes ------------------------------------------------------------------------------


# --- the grant ---------------------------------------------------------------------------------------------


# --- every other check still applies --------------------------------------------------------------------------


def _special_grant(**policy):
    """A grant whose rule also permits health and special: the inferred path's own checks must still hold."""
    from tests.permissions_v2.test_journal_family import _journal_policy
    raw = _journal_policy(**policy)
    (rule,) = raw["rules"]
    for predicate in (rule["evidence_use"]["predicate"], rule["release"]["predicate"]):
        predicate["terms"][1]["values"] = ["none", "personal", "special"]
    return raw


# --- OD-59: closures -------------------------------------------------------------------------------------------


# --- the index basis -----------------------------------------------------------------------------------------


# --- the §9 must-release mix, as a coverage check (Lane O's blind set is the gate; this is not it) ----------------

# v1b: one capitalised token, or words Lane H1's vocabulary has vetted (a multi-word proper name now withholds).
MUST_RELEASE = [
    *[("work.project", value) for value in ("Atlas", "Kestrel", "Lantern", "Harbor", "Juniper", "Quartz",
                                            "Meridian", "Tidewater", "data pipeline", "parser rewrite",
                                            "release tooling", "billing dashboard", "search index", "build cache",
                                            "docs site")],
    *[("works_at", value) for value in ("Contoso", "Northwind", "Fabrikam", "Tailspin", "Wingtip")],
    *[("lives_in", value) for value in ("Lisbon", "Porto", "Bergen", "Tallinn", "Cork")],
    *[("prefers", value) for value in ("coffee", "tea", "pizza", "pancakes", "hiking")],
    *[("skilled_in", value) for value in ("Python", "Rust", "woodworking")],
    *[("member_of", value) for value in ("Northgate", "Fernhill", "Brightwater")],
    *[("commit.made", value) for value in ("ship the release", "finish the parser", "write the docs",
                                           "clean the garage")],
]
STATED = [("lives_in", "Lisbon", "I live in Lisbon."), ("prefers", "tea", "I prefer tea."),
          ("skilled_in", "Python", "I am skilled in Python."), ("member_of", "Northgate", "I am a member of Northgate."),
          ("works_at", "Contoso", "I work at Contoso.")]


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


# --- v1b, guard 1: the model's own protected_content, before the journal family's floor --------------------------


def _review_of(path, entry_id="e1"):
    from topos.permissions_v2.automatic_message_review import machine_key
    from tests.permissions_v2.test_journal_family import _identity, _resolver
    resolver = _resolver(path)
    with owner():
        reviews = EvidenceReviewStore(path.parent / "reviews.db", resolver=resolver)
        with reviews._db() as db:
            return reviews._current_in(db, machine_key(_identity(resolver, entry_id)))


@pytest.mark.parametrize("model_label, stored", [("unknown", "none"), ("none", "none"), ("present", "present")])
def test_publish_records_the_models_own_label_beside_the_floored_one(node, model_label, stored):
    _entry(node, "e1", PROSE)
    _publish(node, "e1", protected_content=model_label)
    review = _review_of(node)
    assert (review.classifications[0].protected_content, review.model_protected_content) == (stored, model_label)
    assert review.model_dump()["model_protected_content"] == model_label


def test_a_review_without_a_recorded_label_keeps_its_bytes_and_digest():
    """Reviews published before v1b carry no model label: they dump without the key, so every digest the node
    already sealed (review revisions, evidence revisions) is unchanged."""
    from topos.permissions_v2.automatic_message_review import MachineMessageReview
    from topos.permissions_v2.canonical import digest
    from topos.permissions_v2.message_review_contract import MessageClassification, MessageSnapshot
    from topos.permissions_v2.evidence import EvidenceBinding, EvidenceIdentity, EvidenceRevision
    binding = EvidenceBinding(environment_id="e", node_id="n", resource_id="r", owner_id="o")
    evidence = EvidenceRevision(identity=EvidenceIdentity(binding=binding, table="journal_entries", record_id="e1",
                                                          source_id=SOURCE, dataset_kind="node_resource",
                                                          dataset_id=None), revision="0" * 64)
    labels = MessageClassification(evidence=evidence, domains=["work"], sensitivity="personal",
                                   authorship="owner_authored", speech="original_message",
                                   independent_copies="none_known", protected_content="none")
    snapshot = MessageSnapshot(binding=binding, canonical_file_revision="1" * 64, message=evidence,
                               protection_revision="2" * 64)
    old = dict(version="topos-machine-message-review/v1", review_id="auto-legacy", owner_id="o", reviewed_at=1,
               rubric="whole-message-machine-review/v1", model_revision="3" * 64, rubric_revision="4" * 64,
               snapshot=snapshot.model_dump(), context_revision="5" * 64, owner_review_revision=None,
               classifications=[labels.model_dump()])
    review = MachineMessageReview.parse(old)
    assert review.model_protected_content is None
    assert review.model_dump() == old and digest(review.model_dump()) == digest(old)
    recorded = MachineMessageReview.parse({**old, "model_protected_content": "unknown"})
    assert recorded.model_dump()["model_protected_content"] == "unknown"
    assert MachineMessageReview.parse(recorded.model_dump()) == recorded


class _Reviews:
    """The two lookups `_unfloored_protected_content` makes, over a fixed set of reviews."""

    def __init__(self, by_key):
        self.by_key = by_key

    def _current_in(self, _db, key):
        return self.by_key.get(key)


def test_the_fact_reads_the_very_review_that_qualified_its_entry(node, tmp_path, monkeypatch, derived):
    from topos.permissions_v2.automatic_message_review import MachineMessageReview, machine_key
    from topos.permissions_v2.canonical import digest
    from topos.permissions_v2.knowledge_projections import _unfloored_protected_content
    _entry(node, "e1", PROSE)
    _publish(node, "e1", protected_content="unknown")
    review = _review_of(node)
    key = machine_key(review.snapshot.message.identity)
    qualified = SimpleNamespace(snapshot=review.snapshot, review_id=review.review_id,
                                review_revision=digest(review.model_dump()))
    assert _unfloored_protected_content(_Reviews({key: review}), None, qualified) == "unknown"
    clean = review.model_copy(update={"model_protected_content": "none"})
    assert _unfloored_protected_content(_Reviews({key: clean}), None, qualified) is None      # not the same review
    legacy = MachineMessageReview.parse({k: v for k, v in review.model_dump().items() if k != "model_protected_content"})
    legacy_qualified = SimpleNamespace(snapshot=review.snapshot, review_id=legacy.review_id,
                                       review_revision=digest(legacy.model_dump()))
    assert _unfloored_protected_content(_Reviews({key: legacy}), None, legacy_qualified) is None
    assert _unfloored_protected_content(_Reviews({}), None, qualified) is None


def test_an_owner_correction_is_the_owners_own_label(node, tmp_path, monkeypatch, derived):
    """The owner's own review of the entry is no floored model label: it decides the fact as it decides the entry."""
    from topos.permissions_v2.message_evidence import OwnerMessageReview, message_key
    from topos.permissions_v2.canonical import digest
    from topos.permissions_v2.knowledge_projections import _unfloored_protected_content
    _entry(node, "e1", PROSE)
    _publish(node, "e1")
    machine = _review_of(node)
    owner_review = OwnerMessageReview(version="topos-owner-message-review/v1", review_id="owner-1",
                                      owner_id=machine.owner_id, reviewed_at=2, rubric="whole-message-owner-review/v1",
                                      snapshot=machine.snapshot, classifications=machine.classifications)
    qualified = SimpleNamespace(snapshot=machine.snapshot, review_id="owner-1",
                                review_revision=digest(owner_review.model_dump()))
    reviews = _Reviews({message_key(machine.snapshot.message.identity): owner_review})
    assert _unfloored_protected_content(reviews, None, qualified) == "none"


def test_assess_returns_the_models_own_labels_and_publish_floors_them(node):
    """`assess` used to floor its answer, so the model's `unknown` was lost before `publish` could record it."""
    import asyncio
    from topos.permissions_v2.automatic_message_review import MODEL, assess
    from tests.permissions_v2.test_journal_family import _prepare

    class Transport:
        base_url = "http://127.0.0.1:11434"

        async def verify(self):
            return None

        async def post(self, url, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "model": MODEL, "done": True, "message": {"content": json.dumps(
                    {"domains": ["work"], "sensitivity": "none", "speech": "original_message",
                     "protected_content": "unknown"})}})
    transport = Transport()
    transport.client = transport
    _entry(node, "e1", PROSE)
    resolver, reviews, prepared = _prepare(node, "e1")
    labels = asyncio.run(assess(prepared, transport=transport))
    assert (labels.sensitivity, labels.protected_content) == ("none", "unknown")       # the model's own
    from topos.permissions_v2.automatic_message_review import publish
    with owner():
        publish(resolver, reviews, prepared, labels, now=1)
    review = _review_of(node)
    assert (review.classifications[0].sensitivity, review.classifications[0].protected_content,
            review.model_protected_content) == ("personal", "none", "unknown")




# --- v1b, guard 5: the closed vocabulary end to end -----------------------------------------------------------------


def test_the_one_token_exception_needs_no_special_root_of_its_own():
    """Guard independence: `_special` runs first today and catches a root too; the exception does not rest on it."""
    from topos.permissions_v2.inferred_facts import _vocabulary
    assert _vocabulary(["Kestrel"], ["kestrel"]) is True
    assert _vocabulary(["Psychopathy"], ["psychopathy"]) is False
    assert _vocabulary(["Rehabber"], ["rehabber"]) is False


# --- v1c, guards 2a and 2b: the entry itself (after blind set 4) ----------------------------------------------------

def _row(content=PROSE, metadata=None, **columns):
    return {**ENTRY, "content": content,
            "metadata_json": None if metadata is None else json.dumps(metadata), **columns}


GOAL = {"template": "time-log", "goal": "ship the parser"}


@pytest.mark.parametrize("entry", [
    _row(metadata={**GOAL, "private": True}),                        # a key that names a marker, set
    _row(metadata={**GOAL, "sensitivity": "special"}),
    _row(metadata={**GOAL, "sensitivity": "high"}),                  # any level but an unset one
    _row(metadata={**GOAL, "isConfidential": 1}),                     # camelCase, a number
    _row(metadata={**GOAL, "flags": {"sensitive": True}}),            # nested
    _row(metadata={**GOAL, "tags": ["work", "private"]}),             # a label's value
    _row(metadata={**GOAL, "category": "health"}),                   # a special category named as a label
    _row(metadata={**GOAL, "labels": ["Legal"]}),
    _row(metadata={**GOAL, "tags": "work, planning, health, sprint review"}),   # a category among other labels
    _row(PROSE + "\nTags: sprint review, planning, finance, roadmap"),
    _row(metadata={**GOAL, "tags": "therapy"}),                      # a cue by H1's lists, as a label
    _row(metadata={**GOAL, "shareable": False}),                     # a sharing key that refuses
    _row(metadata={**GOAL, "share": "no"}),
    _row(PROSE + "\nLabel: private"),                                 # a label line of the text
    _row(PROSE + "\n**Tags**: work, confidential"),
    _row(PROSE + "\n- Sensitivity = special category"),
    _row(PROSE + "\nCategory: Finance"),
    _row(PROSE + " #private"),                                        # a hashtag
    _row(PROSE + " #mental-health"),
    _row("[Confidential] " + PROSE),                                  # a bracketed tag opening a line
    _row(PROSE + " (private)"),                                       # ... or closing one
    _row(PROSE + "\nPrivate entry"),                                  # a tag line
    _row(PROSE + "\nNote: this is private"),
    _row(PROSE + " Please do not share this one."),                   # an instruction not to share
    _row(PROSE + " For my eyes only."),
    _row(metadata={**GOAL, "note": "off the record"}),
    _row(category="medical"),                                         # the category column is a label
    _row(PROSE + "\nTags: pri\u200bvate"),                           # a format character inside the marker
    {**ENTRY, "metadata_json": "sensitivity: special"},                 # metadata that is not JSON, read as text
])
def test_an_entry_marked_special_or_private_withholds_its_facts(entry):
    assert inferred_facts.entry_refusal(entry) == "inferred_entry_marked_special"
    assert code("Atlas", entry=entry) == "inferred_entry_marked_special"


@pytest.mark.parametrize("entry", [
    _row(PROSE + " Then the chiropractor."),                         # H1's word list
    _row(PROSE + " Booked the psychotherapist."),                    # a root inside a word
    _row(PROSE + " Picked up the blood test results."),              # a phrase
    _row(PROSE + " Then the chiro\u200bpractor."),                    # split by a zero-width character
    _row(PROSE + " Then the chir\u043epractor."),                     # a look-alike letter
    _row(PROSE + " Then the CHIROPRACTOR."),
    _row(metadata={**GOAL, "goal": "see the chiropractor"}),          # the template's own field
    _row(metadata={**GOAL, "chiropractor_visit": "done"}),            # a metadata key
    _row(people="the chiropractor"),                                  # the people column
    _row(place_name="Northgate Clinic"),                              # the place column, where the row has one
    _row(mood_tag="anxious"),
])
def test_an_entry_with_a_special_cue_anywhere_withholds_its_facts(entry):
    assert inferred_facts.entry_refusal(entry) == "inferred_entry_special_cue"
    assert code("Atlas", entry=entry) == "inferred_entry_special_cue"


@pytest.mark.parametrize("entry", [
    ENTRY,
    _row(metadata=GOAL),
    _row(metadata={**GOAL, "sensitivity": "none"}),                  # an unset level
    _row(metadata={**GOAL, "sensitivity": "personal"}),              # the ordinary journal level
    _row(metadata={**GOAL, "privacy": "public"}),
    _row(metadata={**GOAL, "shareable": True}),
    _row(metadata={**GOAL, "tags": ["work", "planning"]}),
    _row(PROSE + "\nCategory: work"),
    _row(PROSE + "\nNothing special today."),                         # prose that uses a marker word
    _row(PROSE + " We demoed it (private beta) to the team."),
    _row(PROSE + " Moved the private method into the parser."),
    {"content": None, "metadata_json": None},
])
def test_an_ordinary_entry_passes_the_entry_guards(entry):
    assert inferred_facts.entry_refusal(entry) is None
    assert code("Atlas", entry=entry) is None


def test_an_entry_whose_metadata_cannot_be_read_whole_withholds(monkeypatch):
    monkeypatch.setattr(inferred_facts, "MAX_METADATA_ITEMS", 3)
    assert inferred_facts.entry_refusal(_row(metadata={**GOAL, "a": 1, "b": 2})) == "inferred_entry_marked_special"


@pytest.mark.parametrize('flag', [False, True])
@pytest.mark.parametrize('content', [ATLAS, PROSE])
@pytest.mark.parametrize('disclosure', ['owner_only', 'scoped'])
def test_legacy_journal_fact_consent_never_becomes_ontology_consent(node, tmp_path, monkeypatch, flag, content, disclosure):
    monkeypatch.setenv(FLAG, 'true' if flag else 'false')
    monkeypatch.setenv('TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED', 'true')
    fid = _inferred(node, content=content)
    with _db(node) as db:
        payload = json.loads(db.execute('SELECT payload_json FROM signal_objects WHERE object_id=?', (fid,)).fetchone()[0])
        payload['disclosure'] = disclosure
        db.execute('UPDATE signal_objects SET payload_json=? WHERE object_id=?', (json.dumps(payload), fid))
    search, state = _node(node, tmp_path, monkeypatch, selective=False)
    assert state['member_count'] == 1
    records, _ = _search(search, monkeypatch, 'Atlas parser')
    assert _kind(records, 'journal_entry') and _kind(records, 'fact') == []
    assert _code(search, 'signal_objects', fid) == 'fact_selection_required'


@pytest.mark.parametrize('flag', [False, True])
def test_explicit_selection_cannot_enable_an_inferred_journal_fact(node, tmp_path, monkeypatch, flag):
    monkeypatch.setenv(FLAG, 'true' if flag else 'false')
    fid = _inferred(node)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state['member_count'] == 1
    records, _ = _search(search, monkeypatch, 'Atlas parser')
    assert _kind(records, 'journal_entry') and not _kind(records, 'fact')
    assert _code(search, 'signal_objects', fid) == 'fact_not_grounded'
