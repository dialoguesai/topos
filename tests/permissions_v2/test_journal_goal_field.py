"""IF-5 Lane H1: a journal goal grounded by the owner's own structured goal field, and the lane step that stores it.

protects: the time-log app's `goal` field is rendered as the entry's first paragraph ("Goal: <text>") and stored as
`metadata_json.goal`. A goal equal to that field, verbatim, is grounded for the journal family only, behind
`TOPOS_PERMISSIONS_V2_JOURNAL_GOAL_FIELD` (default off). Opening a grounding form is where a boundary leaks, so these
tests pin what the field must still clear:
  - every gate a journal-cited goal already clears: the grant's `journal_entry` option, owner proof, NSFW, owner-only,
    Off-limits over the entry, the window, the assessment, and a derived goal's lineage revision;
  - the field itself: rendered as the first paragraph AND equal to `metadata_json.goal` AND equal to the goal;
  - the goal text, under a grant that does not release the entry whole (the rule on its own, `_rule`, and
    `_goal_field` with no such grant): Off-limits (alias, diacritics, zero-width), special categories stated
    indirectly, speech acts, a third party's goal or task, words the rule has not vetted, and anything that is not
    an intention;
  - under a grant that releases the entry whole (owner decision, 1 Oct 2026: "when an entry is shared in full, its
    goal goes with it"), the text-form guards are set aside and Off-limits on the goal text still holds
    (test_journal_goal_with_entry pins that case in full);
  - with the flag off, nothing changes;
  - every index build stores exactly one goal per qualifying entry of its grant (no owner command), the owner's
    route runs the same pass and finds them unchanged, and the census agrees with the engine.
Fixtures are synthetic; every name in them is invented.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.permissions_v2.test_journal_family import (  # noqa: F401 (node is a fixture)
    DATASET, OWNER, SOURCE, _db, _entry, _journal_policy, node, owner)
from tests.permissions_v2.test_journal_typed_items import (
    _attest_owner, _census_copy, _code, _goal, _kind, _node, _off_limits, _publish, _restrict, _script, _search)
from topos.permissions_v2 import journal_goal_field as jgf
from topos.permissions_v2 import permitted_derivation as pd
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence_families import JOURNAL_FLAG

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"
SORT = "Sort the spare cables in the attic"
PORCH = "I want to repaint the spare porch railing"
DONE = "Labelled the spare cables and the boxes."


@pytest.fixture
def field_on(monkeypatch):
    monkeypatch.setenv(jgf.FLAG, "true")


def _field_entry(path, entry_id, goal, *, accomplished=DONE, metadata=None, content=None, **extra):
    """A time-log entry as `build_time_log_content` and the journal mapper write it, unless told otherwise."""
    if content is None:
        content = f"Goal: {goal}" + (f"\n\nAccomplished: {accomplished}" if accomplished else "")
    meta = {"goal": goal, "duration_minutes": "45"} if metadata is None else metadata
    _entry(path, entry_id, content, metadata_json=meta if isinstance(meta, str) else json.dumps(meta), **extra)


def _grounded_by_field(path, entry_id="e1", goal=SORT, *, goal_id="goal-1", **extra):
    """An attested owner, a time-log entry with its goal field, the goal stored verbatim, the entry assessed."""
    _attest_owner(path)
    _field_entry(path, entry_id, goal, **extra)
    _goal(path, entry_id, goal, goal_id=goal_id)
    _publish(path, entry_id, domains=["work", "plans"])
    return goal_id


def _goals(path):
    with _db(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM user_goals ORDER BY goal_id")]


def _derive(search):
    with owner():
        return pd.JournalGoalFieldPass(search.index).run(now=search.now[0])


def _field_grounds(search, goal_id, *, policy):
    """`knowledge_projections._goal_field` on its own, as `goal_projection` asks it: the stored goal against the
    entry as this grant's read qualifies it, with the grant's policy and window, or with none (no grant releases
    the entry whole, so every text guard applies)."""
    from topos.permissions_v2 import knowledge_projections as kp
    from topos.permissions_v2.message_evidence import qualify_automatic_message
    from topos.permissions_v2.registry import parse_policy
    parsed = parse_policy(policy) if policy is not None else None
    resolver, reviews, now = search.corpus.resolver, search.corpus.reviews, search.now[0]
    with resolver._read() as (conn, floor), reviews._db() as db:
        goal_row = dict(conn.execute("SELECT * FROM user_goals WHERE goal_id=?", (goal_id,)).fetchone())
        identity = resolver._identity("journal_entries", goal_row["record_id"], goal_row["source_id"])
        qualified, rows = qualify_automatic_message(resolver, conn, floor, identity, reviews, db)
        window = {} if parsed is None else dict(policy=parsed, upper_us=now * 10**6,
                                                 lower_us=(now - parsed.search.window.max_age_seconds) * 10**6)
        return kp._goal_field(conn, qualified, rows, goal_row, resolver.entity_boundary(conn), **window)


class Terms:
    """Off-limits terms matched the way `EntityBoundary._hits` matches them, without a database."""

    def __init__(self, *terms):
        from entailment_eval import TermBoundary  # noqa: PLC0415 -- scripts/permissions_v2 on the path below
        self.inner = TermBoundary(terms)

    def mentions_protected(self, *texts):
        return self.inner.mentions_protected(*texts)


@pytest.fixture
def terms(monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("entailment_eval", None)
    return Terms("Quillon Marsh", "Quill")


def _rule(goal, entry=None, *, boundary=None, people=frozenset(), sensitivity="personal", author=True, attested=True):
    entry = entry if entry is not None else {"content": f"Goal: {goal}\n\nAccomplished: {DONE}",
                                             "metadata_json": json.dumps({"goal": goal}), "content_nsfw": 0}
    return jgf.refusal(goal, entry, boundary=boundary, author_is_owner=author, subject_attested=attested,
                       sensitivity=sensitivity, people=people, env={jgf.FLAG: "true"})


# --- the rule on its own -----------------------------------------------------------------------------------

@pytest.mark.parametrize("goal", [
    SORT, PORCH, "Tidy the toolbox drawers", "Label the pantry boxes by shelf", "Today I'll bake two loaves of bread",
    "My goal is to finish the second draft of the wiki page", "Run 5k before the spare standup",
    "Deep clean the spare oven racks",
    "I will back up the laptop to the spare disk", "Reply to the spare support tickets in the queue",
])
def test_a_plain_goal_field_first_person_or_imperative_is_grounded(terms, goal):
    assert _rule(goal, boundary=terms) is None


@pytest.mark.parametrize("entry, goal, code", [
    ({"content": f"Goal: {SORT}", "metadata_json": json.dumps({"goal": "Sort the cables"})}, SORT,
     "goal_field_mismatch"),
    ({"content": "Goal: Sort the cables\n\nAccomplished: x", "metadata_json": json.dumps({"goal": SORT})}, SORT,
     "goal_field_mismatch"),                                                    # the text was edited, not the field
    ({"content": f"Goal: {SORT}", "metadata_json": "{}"}, SORT, "goal_field_mismatch"),
    ({"content": f"Goal: {SORT}", "metadata_json": "not json"}, SORT, "goal_field_mismatch"),
    ({"content": f"Accomplished: {DONE}", "metadata_json": json.dumps({"goal": SORT})}, SORT, "goal_field_mismatch"),
    ({"content": f"Accomplished: {DONE}", "metadata_json": "{}"}, SORT, "goal_field_absent"),
    ({"content": f"goal: {SORT}", "metadata_json": json.dumps({"goal": SORT})}, SORT, "goal_field_mismatch"),
    ({"content": f"Goal: {SORT}", "metadata_json": json.dumps({"goal": SORT})}, SORT.lower(), "goal_field_mismatch"),
    ({"content": f"Goal: {SORT}", "metadata_json": json.dumps({"goal": SORT})}, SORT + ".", "goal_field_mismatch"),
    ({"content": f"Goal: {SORT} \n\nAccomplished: x", "metadata_json": json.dumps({"goal": SORT})}, SORT,
     "goal_field_mismatch"),
    ({"content": f"Goal: {SORT}", "metadata_json": json.dumps({"goal": SORT}), "content_nsfw": 1}, SORT,
     "goal_field_nsfw"),
    ({"content": f"Goal: {SORT}", "metadata_json": json.dumps({"goal": SORT, "nsfw": True})}, SORT, "goal_field_nsfw"),
])
def test_the_field_must_be_rendered_stored_and_equal_verbatim(terms, entry, goal, code):
    """A field that differs from the stored goal (an edited or re-synced row) withholds, and so does any spelling
    difference between the goal and the field."""
    assert _rule(goal, entry, boundary=terms) == code


def test_the_owner_attested_and_the_entrys_own_labels_are_checked_where_the_rule_reads_them(terms):
    assert _rule(SORT, boundary=terms, author=False) == "goal_field_author"
    assert _rule(SORT, boundary=terms, attested=False) == "goal_field_author"
    assert _rule(SORT, boundary=terms, sensitivity="special") == "goal_field_special_category"
    assert _rule(SORT, boundary=terms, sensitivity="unknown") == "goal_field_special_category"
    assert _rule(SORT, boundary=None) == "goal_field_boundary_unavailable"
    assert jgf.refusal(SORT, {"content": f"Goal: {SORT}", "metadata_json": json.dumps({"goal": SORT})},
                       boundary=terms, author_is_owner=True, subject_attested=True, sensitivity="personal",
                       env={}) == "goal_field_disabled"


@pytest.mark.parametrize("goal", [
    "Call Quillon Marsh about the boxes", "call quillon marsh about the boxes", "Email quill the spare cables",
    "Call Quillön Marsh about the boxes", "Call Quil​lon Marsh about the boxes",
    "Call Quіllon Marsh about the boxes",                                # a Cyrillic look-alike letter
])
def test_an_off_limits_name_in_the_goal_withholds_in_any_spelling(terms, goal):
    assert _rule(goal, boundary=terms) in ("goal_field_offlimits", "goal_field_shape", "goal_field_third_party")
    assert _rule(goal, boundary=terms) is not None


@pytest.mark.parametrize("goal", [
    # A filler word ("spare", "zinc") sits in each, so no fixture reads as anyone's real goal.
    "Pick up the zinc refill at the pharmacy", "Log my zinc blood sugar", "Book the zinc follow-up scan",
    "Do my zinc knee stretches", "Light the zinc candles before sundown", "Read the zinc verse for Sunday",
    "Go to my zinc group session", "Renew my spare green card", "Knock on zinc doors for the campaign",
    "Pay my spare union dues", "Mail the zinc spit kit", "Update my zinc pronouns", "Order a zinc binder",
    "Finish my zinc community service", "Change my name on my zinc license", "Start the zinc sertraline taper",
    "Get tested again this zinc week", "Count my days at zinc", "Do my zinc pelvic floor routine",
    "Register as an independent zinc voter", "Bring the zinc walker to the car",
])
def test_a_special_category_stated_indirectly_withholds(terms, goal):
    assert _rule(goal, boundary=terms) in ("goal_field_special_category", "goal_field_unvetted_word",
                                            "goal_field_third_party", "goal_field_not_intention")


@pytest.mark.parametrize("goal", [
    "Help Tavrin finish her slides", "Remind the zinc team about the offsite", "Write my nephew's toast",
    "Finish varo's slides", "Get the zinc kids to bed early", "Our goal is to close the zinc round",
    "Dad wants to retire to zinc",
    "Call varo about the boxes", "Cover for varo on the shift", "Proofread the students' essays",
    "Book the zinc photographer for the launch",
])
def test_a_third_partys_goal_or_task_withholds(terms, goal):
    assert _rule(goal, boundary=terms) is not None


@pytest.mark.parametrize("goal, code", [
    # Each of these is made only of words the vocabulary vets, so exactly one guard can catch it.
    ("Get tested again by noon", "goal_field_special_category"),     # a phrase of ordinary words
    ("Call the lead about the contract", "goal_field_third_party"),    # a contact verb's object that is a person
    ("Review the user's draft", "goal_field_third_party"),             # a possessive other than a time's
    ("Do my pelvic spare routine", "goal_field_unvetted_word"),         # a word no list has vetted
    ("Sorting the spare cables", "goal_field_not_intention"),          # no task verb opens it
    ("Goal 1", "goal_field_not_intention"),                             # a template placeholder
])
def test_each_guard_catches_what_only_it_can(terms, goal, code):
    assert _rule(goal, boundary=terms) == code


def test_the_nodes_own_people_and_the_entrys_people_are_third_parties(terms):
    goal = "Sort the spare cables with zentith"
    assert _rule(goal, boundary=terms) == "goal_field_unvetted_word"          # unknown to the vocabulary
    assert _rule(goal, boundary=terms, people=frozenset({"zentith"})) == "goal_field_third_party"
    entry = {"content": f"Goal: {goal}", "metadata_json": json.dumps({"goal": goal}), "people": "Varo Zentith"}
    assert _rule(goal, entry, boundary=terms) == "goal_field_third_party"


def test_the_nodes_people_are_its_person_entities_aliases_and_contacts_never_the_owner(node):
    with _db(node) as conn:
        conn.executemany("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, "
                         "is_self) VALUES (?,?,?,?,?,?)", [
                             ("p1", "person", "Varo Zentith", "varo zentith", json.dumps(["Zen"]), 0),
                             ("p2", "person", "Ossory Park", "ossory park", "[]", 0),          # "park" is a word
                             ("me", "person", "Tavrin Quell", "tavrin quell", "[]", 1),        # the owner's self
                             ("o1", "organization", "Brenvik", "brenvik", "[]", 0)])
        conn.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self) "
                     "VALUES ('c1', ?, 'imessage', 'Yarrow Thessaly', 0)", (DATASET,))
        people = jgf.known_people(conn)
    assert {"varo", "zentith", "zen", "ossory", "yarrow", "thessaly"} <= people
    assert not {"park", "tavrin", "quell", "brenvik"} & people


@pytest.mark.parametrize("goal, code", [
    ("Maybe sort the spare cables", "goal_field_hedged"),
    ("Try to sort the spare cables", "goal_field_hedged"),
    ("Don't skip the attic boxes", "goal_field_negated"),
    ("Never skip the attic boxes", "goal_field_negated"),
    ("Sort the spare cables lol", "goal_field_sarcasm"),
    ("Sort ALL the spare cables", "goal_field_sarcasm"),
    ("Sort the spare cables yeah right", "goal_field_sarcasm"),
    ("Should I sort the spare cables", "goal_field_question_or_quote"),
    ("Sort the spare cables?", "goal_field_question_or_quote"),
    ("Used to sort the cables every week", "goal_field_ended"),
    ("Already sorted the spare cables", "goal_field_ended"),
    ("Sort the spare cables someday", "goal_field_hedged"),
    ("Sort the spare cables one day", "goal_field_not_yet"),
    ("Sort the spare cables at some point", "goal_field_not_yet"),
])
def test_the_speech_act_guards_run_on_the_goal_text_itself(terms, goal, code):
    assert _rule(goal, boundary=terms) == code


@pytest.mark.parametrize("goal", [
    "Attic tasks:", "Work stuff", "Shopping list for the week", "What should I sort next",
    "“Stay hungry, stay foolish”", "Done is better than perfect — a poster", "Stay hungry stay foolish",
    "https://example.com/attic", "[insert goal here]", "<goal>", "TBD", "Lorem ipsum dolor", "Goal 1",
    "Move fast and break things", "Make it happen", "Sorting the spare cables", "Sorted the spare cables",
])
def test_what_is_not_an_intention_withholds(terms, goal):
    """A list title, a question, a pasted quote, a URL, a template placeholder, a motto, a gerund or a past tense."""
    assert _rule(goal, boundary=terms) is not None


# --- through a signed knowledge grant ---------------------------------------------------------------------

@pytest.mark.parametrize("goal", [SORT, PORCH])
def test_a_goal_that_is_the_entrys_goal_field_releases_citing_the_entry(node, tmp_path, monkeypatch, field_on, goal):
    goal_id = _grounded_by_field(node, goal=goal)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 2                                        # the entry, and the goal it grounds
    records, bindings = _search(search, monkeypatch, "spare cables porch railing attic")
    (entry,) = _kind(records, "journal_entry")
    (item,) = _kind(records, "goal")
    assert (item["content"], item["status"]) == (goal, "stated_intention")
    assert item["citations"] == [dict(record_id=entry["record_id"], source_id=SOURCE, content=entry["content"])]
    assert bindings[item["record_id"]]["evidence_tables"] == ["journal_entries"]
    assert goal_id not in json.dumps(records)
    assert _code(search, "user_goals", goal_id) is None


def test_with_the_flag_off_nothing_changes(node, tmp_path, monkeypatch):
    goal_id = _grounded_by_field(node)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 1                                        # the entry alone, as before
    assert _code(search, "user_goals", goal_id) == "goal_not_grounded"
    assert _kind(_search(search, monkeypatch, "spare cables attic")[0], "goal") == []
    with pytest.raises(PolicyError, match="journal_goal_field_disabled"):
        _derive(search)
    assert [g["goal_id"] for g in _goals(node)] == [goal_id]


def test_the_journal_family_off_still_withholds_a_journal_goal_as_before(node, tmp_path, monkeypatch, field_on):
    goal_id = _grounded_by_field(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)
    assert _code(search, "user_goals", goal_id) == "lineage_identity_ambiguous"
    with pytest.raises(PolicyError, match="journal_goal_field_disabled"):
        _derive(search)


@pytest.mark.parametrize("change, code", [
    ("metadata", "goal_not_grounded"),                 # the stored field no longer says it
    ("content", "goal_not_grounded"),                  # the text was edited away from the field
    ("goal_text", "goal_not_grounded"),                # the goal is not the field verbatim
])
def test_a_field_that_differs_from_the_goal_withholds_end_to_end(node, tmp_path, monkeypatch, field_on, change, code):
    goal_id = _grounded_by_field(node)
    with _db(node) as conn:
        if change == "metadata":
            conn.execute("UPDATE journal_entries SET metadata_json=? WHERE entry_id='e1'",
                         (json.dumps({"goal": "Sort the cables"}),))
        elif change == "goal_text":
            conn.execute("UPDATE user_goals SET goal_text=? WHERE goal_id=?", (SORT.lower(), goal_id))
    if change == "content":
        _field_entry(node, "e1", SORT, content=f"Goal: Sort the cables\n\nAccomplished: {DONE}")
    if change != "goal_text":
        _publish(node, "e1", domains=["work", "plans"])   # the edit staled the assessment; assess it again
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "user_goals", goal_id) == code
    released = _kind(_search(search, monkeypatch, "spare cables attic")[0], "goal")
    if change == "goal_text":
        # The stored goal is not the field; the build stored the field itself as the entry's one goal.
        assert [(r["content"], r["record_id"] != goal_id) for r in released] == [(SORT, True)]
    else:
        assert released == []


NO_RECORD_OPTION = ("message", "fact", "goal", "relationship")


@pytest.mark.parametrize("goal", [
    "Pick up the zinc refill at the pharmacy", "Help Tavrin finish her slides", "Maybe sort the spare cables",
    "Attic tasks:", "Sort the spare cables with zentith",
])
def test_a_field_the_text_guards_withhold_releases_only_with_its_entry(node, tmp_path, monkeypatch, field_on, goal):
    """Under the grant that releases the entry whole, the field is its goal (owner decision). Under a grant that
    does not, the rule's text guards still withhold it, and the grant without the record option never reaches
    the rule at all: a journal citation needs the option, as before."""
    goal_id = _grounded_by_field(node, goal=goal)
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "user_goals", goal_id) is None
    (item,) = _kind(_search(search, monkeypatch, goal)[0], "goal")
    assert item["content"] == goal and item["citations"][0]["content"].startswith(f"Goal: {goal}\n\n")
    assert _field_grounds(search, goal_id, policy=search.search_raw) is True
    assert _field_grounds(search, goal_id, policy=_journal_policy(kinds=NO_RECORD_OPTION)) is False
    assert _field_grounds(search, goal_id, policy=None) is False
    assert _code(search, "user_goals", goal_id, raw=_journal_policy(kinds=NO_RECORD_OPTION)) == \
        "journal_citation_needs_record_option"


@pytest.mark.parametrize("goal", ["Email quill the spare cables", "Sort the cables with quillon marsh"])
def test_an_off_limits_name_in_the_field_withholds_end_to_end(node, tmp_path, monkeypatch, field_on, goal):
    """The real boundary, with an alias: the entry's own Off-limits check withholds it before the rule is asked, and
    the rule's own check (guard independence) withholds the field on its own."""
    from topos.permissions_v2.entity_boundary import EntityBoundary
    goal_id = _grounded_by_field(node, goal=goal)                      # assessed before the name is Off-limits
    _off_limits(node)
    with _db(node) as conn:
        conn.execute("UPDATE entity_blackholes SET aliases_json='[\"Quill\"]'")
        assert _rule(goal, boundary=EntityBoundary(conn)) == "goal_field_offlimits"
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 0
    assert _code(search, "user_goals", goal_id) == "entity_protected"


def test_a_person_the_node_knows_withholds_only_without_the_entry(node, tmp_path, monkeypatch, field_on):
    goal = "Sort the spare cables for zentith"
    goal_id = _grounded_by_field(node, goal=goal)
    with _db(node) as conn:
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self) "
                     "VALUES ('p1', 'person', 'Varo Zentith', 'varo zentith', 0)")
        assert "zentith" in jgf.known_people(conn)
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "user_goals", goal_id) is None                 # the entry names that person already
    assert _field_grounds(search, goal_id, policy=_journal_policy(kinds=NO_RECORD_OPTION)) is False


@pytest.mark.parametrize("veto, code", [
    ("nsfw", "unsupported_message_content"), ("owner_only", "owner_only"),
    ("outside_window", "evidence_outside_window"), ("quote", "not_original_message"),
    ("special", "evidence_not_permitted"), ("option", "journal_citation_needs_record_option"),
])
def test_every_gate_a_journal_goal_clears_still_applies(node, tmp_path, monkeypatch, field_on, veto, code):
    _attest_owner(node)
    extra = ({"content_nsfw": 1} if veto == "nsfw" else
             {"entry_at": "2026-06-01T08:30:00"} if veto == "outside_window" else {})
    _field_entry(node, "e1", SORT, **extra)
    goal_id = _goal(node, "e1", SORT)
    if veto == "nsfw":
        from topos.permissions_v2 import message_evidence
        monkeypatch.setattr(message_evidence, "_journal_source_checks", lambda *args, **kwargs: None)
    labels = ({"speech": "third_party_quote"} if veto == "quote" else
              {"sensitivity": "special"} if veto == "special" else {})
    _publish(node, "e1", **{"domains": ["work", "plans"], **labels})
    if veto == "owner_only":
        _restrict(node, "journal_entries", "e1")
    kinds = {"kinds": ("message", "fact", "goal", "relationship")} if veto == "option" else {}
    search, _state = _node(node, tmp_path, monkeypatch, **kinds)
    assert _code(search, "user_goals", goal_id) == code


# --- the derivation ------------------------------------------------------------------------------------------

def _fields(path):
    """Five qualifying entries (three plain, a hedge and an indirect special category, which the entry's own
    release carries) and two that are not, each with a goal field."""
    _attest_owner(path)
    _off_limits(path)
    with _db(path) as conn:                                              # the alias the offlimits entry uses
        conn.execute("UPDATE entity_blackholes SET aliases_json='[\"Quill\"]'")
    rows = {"q1": SORT, "q2": PORCH, "q3": "Tidy the toolbox drawers", "hedged": "Maybe sort the attic boxes",
            "special": "Pick up the zinc refill at the pharmacy", "offlimits": "Email quill the spare cables",
            "mismatch": "Label the pantry boxes by shelf"}
    for entry_id, goal in rows.items():
        _field_entry(path, entry_id, goal, metadata={"goal": "Label the boxes"} if entry_id == "mismatch" else None,
                     accomplished=f"{DONE} ({entry_id})" if entry_id != "q1" else DONE)
    for entry_id in rows:
        if entry_id != "offlimits":                                     # the boundary keeps it from assessment
            _publish(path, entry_id, domains=["work", "plans"])
    return rows


QUALIFYING = ("q1", "q2", "q3", "hedged", "special")


def test_the_build_stores_one_goal_per_qualifying_entry_with_the_lane_lineage(node, tmp_path, monkeypatch, field_on):
    """No owner command: the index build (`_node`) stores the fields of its grant's members before it builds, and
    the owner's route finds every one of them unchanged."""
    from topos.storage.derived_row_identity import derived_row_id
    rows = _fields(node)
    goals = _goals(node)
    assert goals == []
    search, state = _node(node, tmp_path, monkeypatch)
    goals = _goals(node)
    assert sorted((g["record_id"], g["source_id"], g["goal_text"]) for g in goals) == sorted(
        (entry_id, SOURCE, rows[entry_id]) for entry_id in QUALIFYING)
    assert state["member_count"] == 6 + len(QUALIFYING)                     # the entries, and the goals they ground
    counts = _derive(search)
    assert counts["goal:unchanged"] == len(QUALIFYING) and counts["journal_members"] == 6
    assert counts["refused:goal_field_mismatch"] == 1 and "goal:written" not in counts and "rebuilt" not in counts
    assert _goals(node) == goals
    resolver = search.index.resolver
    with _db(node) as conn:
        content = conn.execute("SELECT content FROM journal_entries WHERE entry_id='q1'").fetchone()[0]
    q1 = next(g for g in goals if g["record_id"] == "q1")
    lineage = json.loads(q1["payload_json"])["lineage"]
    identity = resolver._identity("journal_entries", "q1", SOURCE)
    assert q1["goal_id"] == derived_row_id("user_goals", ("q1", SORT))
    assert (lineage["lane"], lineage["message"], lineage["message_revision"], lineage["extractor"]["kind"]) == (
        pd.LANE, identity.model_dump(), pd.message_revision(identity, content), "journal_goal_field")
    assert (q1["model"], q1["provider"]) == (None, None)
    assert lineage["extractor"]["version"] == jgf.VERSION
    records, _bindings = _search(search, monkeypatch, "spare cables porch railing toolbox drawers attic pharmacy")
    assert sorted(r["content"] for r in _kind(records, "goal")) == sorted(rows[e] for e in QUALIFYING)


def test_an_edited_entry_has_its_goal_superseded_at_the_next_build(node, tmp_path, monkeypatch, field_on):
    _fields(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    before = _goals(node)
    assert len(before) == len(QUALIFYING)
    # The entry's accomplished text changes: its goal's lineage is stale until the next build stores it again.
    q1 = next(g for g in before if g["record_id"] == "q1")
    _field_entry(node, "q1", SORT, accomplished="Labelled the cables, and the shelf too.")
    _publish(node, "q1", domains=["work", "plans"])
    assert _code(search, "user_goals", q1["goal_id"]) == "lineage_revision_stale"
    with owner():
        search.index.rebuild_all(now=search.now[0])                        # the build supersedes it, no command
    after = _goals(node)
    assert [g["goal_id"] for g in after] == [g["goal_id"] for g in before] and after != before
    assert _code(search, "user_goals", q1["goal_id"]) is None
    again = _derive(search)
    assert again["goal:unchanged"] == len(QUALIFYING) and "goal:superseded" not in again


def test_an_entry_that_changes_between_selection_and_the_write_is_not_written(node, tmp_path, monkeypatch, field_on):
    _attest_owner(node)
    _field_entry(node, "e1", SORT)
    _publish(node, "e1", domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    with _db(node) as conn:
        conn.execute("DELETE FROM user_goals")                                # the build stored it; start over
    lane = pd.JournalGoalFieldPass(search.index)
    selected = lane._selected

    def select_then_edit(now, grant_id=None):
        out = selected(now, grant_id)
        _field_entry(node, "e1", SORT, accomplished="Something else entirely.")
        return out
    lane._selected = select_then_edit
    with owner():
        counts = lane.run(now=search.now[0])
    assert counts["refused:message_changed"] == 1 and _goals(node) == []


def test_a_goal_the_node_already_stored_verbatim_is_not_twinned(node, tmp_path, monkeypatch, field_on):
    """The node's own goal extraction keys a goal by (record, text); the lane uses the same id and leaves it be."""
    from topos.storage.derived_row_identity import derived_row_id
    _attest_owner(node)
    _field_entry(node, "e1", SORT)
    _goal(node, "e1", SORT, goal_id=derived_row_id("user_goals", ("e1", SORT)))
    _publish(node, "e1", domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    counts = _derive(search)
    assert counts["goal:already_stored"] == 1 and "goal:written" not in counts
    assert len(_goals(node)) == 1
    assert [r["content"] for r in _kind(_search(search, monkeypatch, "spare cables attic")[0], "goal")] == [SORT]


def test_the_derivation_reads_only_entries_a_grant_admits(node, tmp_path, monkeypatch, field_on):
    _attest_owner(node)
    _field_entry(node, "unassessed", SORT)
    _field_entry(node, "old", PORCH, entry_at="2026-06-01T08:30:00")
    _field_entry(node, "special", "Tidy the toolbox drawers")
    _field_entry(node, "opted", "Label the pantry boxes by shelf")
    _publish(node, "old", domains=["work", "plans"])
    _publish(node, "special", domains=["work"], sensitivity="special")
    _publish(node, "opted", domains=["work"])
    from topos.permissions_v2.evidence import EvidenceReviewStore
    from topos.permissions_v2.message_evidence import message_key
    from tests.permissions_v2.test_journal_family import AFTER_ITS_DAY, _resolver
    resolver = _resolver(node)
    with owner():
        EvidenceReviewStore(node.parent / "reviews.db", resolver=resolver).opt_out(
            message_key(resolver._identity("journal_entries", "opted", SOURCE)), now=AFTER_ITS_DAY)
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _goals(node) == []                                                   # the build admitted none of them
    counts = _derive(search)
    assert counts["journal_members"] == 0 and _goals(node) == []


def test_a_grant_that_cannot_cite_a_journal_entry_selects_nothing(node, tmp_path, monkeypatch, field_on):
    _grounded_by_field(node)
    search, _state = _node(node, tmp_path, monkeypatch, kinds=("message", "fact", "goal", "relationship"))
    assert _derive(search)["journal_members"] == 0


def test_an_unattested_owner_gets_nothing(node, tmp_path, monkeypatch, field_on):
    _field_entry(node, "e1", SORT)
    _publish(node, "e1", domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    counts = _derive(search)
    assert counts["refused:owner_subject_unattested"] == 1 and _goals(node) == []


def test_only_the_owner_may_run_the_derivation(node, tmp_path, monkeypatch, field_on):
    _grounded_by_field(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    with pytest.raises(PolicyError, match="owner_authority_required"):
        pd.JournalGoalFieldPass(search.index).run(now=search.now[0])


def test_the_relationship_follows_a_derived_goal_after_the_graph_rebuild(node, tmp_path, monkeypatch, field_on):
    """Relationships are untouched: the graph rebuild's `pursues` edge to a derived goal releases as for any goal."""
    from tests.permissions_v2.test_owner_identity_binding import add_entity
    _attest_owner(node)
    _field_entry(node, "e1", SORT)
    _publish(node, "e1", domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    _derive(search)
    (goal,) = _goals(node)
    with _db(node) as conn:                                             # what the graph rebuild writes for a goal
        add_entity(conn, "goal-node", is_self=0, entity_type="goal")
        conn.execute("UPDATE entities SET canonical_name=?, normalized_name=? WHERE entity_id='goal-node'",
                     (SORT, SORT))
        conn.execute("INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type, metadata_json) "
                     "VALUES ('edge-1', 'owner-entity', 'goal-node', 'pursues', ?)",
                     (json.dumps({"source_object_id": goal["goal_id"], "actor_role": "authored"}),))
    with owner():
        search.index.rebuild_all(now=search.now[0])
    records, _bindings = _search(search, monkeypatch, "spare cables attic")
    (edge,) = _kind(records, "relationship")
    assert (edge["relation"], edge["object"]) == ("pursues", SORT)


# --- the owner-socket route --------------------------------------------------------------------------------

ROUTE = "/v1/sharing/message-search/permitted-derivation"


@pytest.fixture
def field_route(node, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from topos.api.permissions_search_maintenance import router
    from topos.permissions_v2 import runtime
    _grounded_by_field(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    binding = search.index.resolver.binding
    fake = SimpleNamespace(protocol=SimpleNamespace(ledger=SimpleNamespace(identity=binding)),
                           message_search_index=lambda: search.index)
    monkeypatch.setattr(runtime, "get_runtime", lambda: fake)
    monkeypatch.setattr(pd.time, "time", lambda: search.now[0])
    monkeypatch.setenv(pd.FLAG, "true")
    app = FastAPI()
    app.include_router(router)
    return app, binding.model_dump()


def test_the_owner_runs_the_step_on_the_route_and_gets_counts_only(field_route, node, monkeypatch, field_on):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, binding = field_route
    with _db(node) as conn:
        conn.execute("DELETE FROM user_goals")                                  # the step writes it itself
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(ROUTE, json={"binding": binding, "operation": "journal_goal_field"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["counts"]["goal:written"] == 1 and body["rule"] == jgf.VERSION
    assert "spare cables" not in response.text and "e1" not in response.text   # codes and counts, never a value
    assert [g["goal_text"] for g in _goals(node)] == [SORT]


@pytest.mark.parametrize("body, status", [
    ({"operation": "journal_goal_field", "budget": 5}, 400),
    ({"operation": "journal_goal_field", "packs": []}, 400),
    ({"operation": "journal_goal_field"}, 404),                                # the rule's own flag is off
])
def test_the_step_refuses_a_widened_request_and_runs_only_with_its_flag(field_route, node, monkeypatch, body, status):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, binding = field_route
    if status == 400:
        monkeypatch.setenv(jgf.FLAG, "true")
    with TestClient(UDSChannelApp(app)) as client:
        assert client.post(ROUTE, json={**body, "binding": binding}).status_code == status
    assert [g["goal_id"] for g in _goals(node)] == ["goal-1"]


def test_nobody_but_the_owner_can_run_the_step(field_route, node, monkeypatch, field_on):
    from fastapi.testclient import TestClient
    from topos.auth import resolve_request_principal
    from topos.principal import OWNER_APP, THIRD_PARTY, Principal
    from topos.uds import UDSChannelApp
    app, binding = field_route
    for principal in (Principal(THIRD_PARTY, "cp_relay", acting_user=OWNER),
                      Principal(OWNER_APP, "local_http", acting_user=OWNER),
                      Principal(OWNER_APP, "uds", acting_user="someone-else")):
        app.dependency_overrides[resolve_request_principal] = lambda principal=principal: principal
        with TestClient(app) as client:
            assert client.post(ROUTE, json={"binding": binding, "operation": "journal_goal_field"}).status_code == 403
    app.dependency_overrides.clear()
    with TestClient(UDSChannelApp(app)) as client:
        other = {**binding, "owner_id": "owner-2"}
        assert client.post(ROUTE, json={"binding": other, "operation": "journal_goal_field"}).status_code in (400, 403)
    assert [g["goal_id"] for g in _goals(node)] == ["goal-1"]


# --- the census agrees ---------------------------------------------------------------------------------------

def test_the_census_goal_field_count_and_the_engine_agree_on_a_fixture(node, tmp_path, monkeypatch, field_on):
    """od46_journal_grounding's goal-field column (the engine's own rule, with the census gates) against what the
    engine derives and releases at the grant's 90 days, on entries the two must judge alike."""
    _fields(node)
    _field_entry(node, "q-old", "Wash the spare towels", entry_at="2026-06-01T08:30:00")   # inside 365 days only
    _publish(node, "q-old", domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    counts = _derive(search)
    records, _bindings = _search(search, monkeypatch, "spare cables porch railing toolbox drawers towels attic pharmacy")
    engine = len(_kind(records, "goal"))
    assert counts["goal:unchanged"] == engine == len(QUALIFYING)
    measured = _script("od46_journal_grounding").measure(_census_copy(node, tmp_path, search.now[0]))
    field = measured["structured_goal_field"]
    assert field["90d"]["releasable:engine_rule"] == engine
    assert field["365d"]["releasable:engine_rule"] == engine + 1           # the old entry's field qualifies too
    goals = measured["by_window"]["goal"]
    assert goals["90d"]["(d) goal_field_rule"] == engine
    assert goals["90d"]["(a) fullmatch_whole_entry"] == 0                  # the node's floor grounds none of them
