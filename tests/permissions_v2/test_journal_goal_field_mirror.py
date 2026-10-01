"""The journal goal field and the node's own mirror of the text (`content_disclosure`).

protects: the privacy layer stores, on every journal row it has read, a sanitised copy of `content` in
`content_disclosure`. For a time-log entry that copy opens with the same "Goal: ..." paragraph. The rule read every
column but the text and the metadata as a column a source writes, so the mirror's own Goal paragraph was a "second
Goal line" and every real goal-bearing entry was `goal_field_mismatch` (rule v1, measured on a copy of the owner's
node: 80 of 80). No fixture or blind set carried the column, so nothing caught it. These tests pin what rule v2 does:
  - a row with a mirror states its field: the mirror equal to the text, redacted in the body, or redacted inside the
    Goal paragraph itself (the privacy layer's own placeholders);
  - what the mirror must still clear: no "Goal:" line after its first paragraph, no Goal line that is not its first
    paragraph, and a first paragraph that is the field or the field redacted, never some other goal;
  - the mirror is the only column read this way. A Goal line in `people` or in any other column, the mirror's hash
    and model included, is a mismatch as before, with or without a mirror beside it;
  - a redacted mirror neither releases nor withholds a goal: the field's own guards decide, and what releases is the
    field and the entry's text, never the mirror;
  - the premises: the mirror is the privacy layer's one journal column, no ingest door writes it, the placeholders
    are the privacy layer's own, and the inferred-fact entry guards never read it.
Fixtures are synthetic; every name in them is invented.
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.test_journal_family import SOURCE, _db, node  # noqa: F401 (node is a fixture)
from tests.permissions_v2.test_journal_goal_field import (  # noqa: F401 (field_on and terms are fixtures)
    DONE, SORT, _derive, _field_entry, _goals, _grounded_by_field, _rule, field_on, terms)
from tests.permissions_v2.test_journal_typed_items import _attest_owner, _code, _kind, _node, _publish, _search
from topos.permissions_v2 import journal_goal_field as jgf
from topos.permissions_v2.evidence_families import JOURNAL_FLAG

FRIDAY = "Sort the spare cables before Friday"
BODY = f"Accomplished: {DONE} Mara Example held the ladder."
TEXT = f"Goal: {FRIDAY}\n\n{BODY}"
BODY_REDACTED = f"Accomplished: {DONE} [NAME] held the ladder."
OTHER = "Goal: Run a marathon"


def _row(mirror=None, *, goal=FRIDAY, content=None, **columns):
    """A time-log row as qualification loads it, with the privacy layer's mirror when one is given."""
    row = {"content": TEXT if content is None else content, "metadata_json": json.dumps({"goal": goal}),
           "people": "", "content_nsfw": 0, **columns}
    if mirror is not None:
        row[jgf.MIRROR] = mirror
    return row


# --- a row the privacy layer mirrored states its field ---------------------------------------------------------

@pytest.mark.parametrize("mirror", [
    TEXT,                                                    # nothing to redact: the mirror is the text
    f"Goal: {FRIDAY}\n\n{BODY_REDACTED}",                     # a name redacted in the body
    f"Goal: {FRIDAY}",                                       # a mirror that is the Goal paragraph alone
    f"Goal: {FRIDAY}\n\n{BODY}\n\nMy goal: stay with it.",    # prose about a goal is not a Goal line
])
def test_a_row_with_the_nodes_mirror_states_its_field(mirror):
    assert jgf.field_state(_row(mirror)) == (FRIDAY, None)
    assert jgf.structured_field(_row(mirror)) == FRIDAY


@pytest.mark.parametrize("content, metadata, state", [
    (f"Goal: {FRIDAY}", {"goal": FRIDAY}, (FRIDAY, None)),
    (TEXT, {"goal": FRIDAY}, (FRIDAY, None)),
    ("Goal: walk daily\nand stretch\n\nNotes.", {"goal": "walk daily\nand stretch"}, ("walk daily\nand stretch", None)),
    ("Goal: walk daily\n\nand stretch\n\nNotes.", {"goal": "walk daily\n\nand stretch"},     # a blank line in the field
     ("walk daily\n\nand stretch", None)),
    (TEXT + "\n\n" + OTHER, {"goal": FRIDAY}, (None, "goal_field_mismatch")),      # a second Goal paragraph
    (TEXT, {"goal": "Sort the cables"}, (None, "goal_field_mismatch")),            # the text edited away from the field
    (TEXT, {}, (None, "goal_field_mismatch")),
    (BODY, {"goal": FRIDAY}, (None, "goal_field_mismatch")),
    (BODY, {}, (None, "goal_field_absent")),
])
def test_a_mirror_equal_to_the_text_never_changes_the_state(content, metadata, state):
    """The bug in its general form: the node's copy of the text, merely by existing, must not change the answer."""
    row = {"content": content, "metadata_json": json.dumps(metadata), "people": ""}
    assert jgf.field_state(row) == state
    assert jgf.field_state({**row, jgf.MIRROR: content}) == state


@pytest.mark.parametrize("paragraph", [
    "Goal: Sort the spare cables before [DATE]",             # one span, at the end
    "Goal: Sort the spare cables before[DATE]",              # ... taking the space before it
    "Goal:[NAME] the spare cables before Friday",            # ... or at the start, with the space after the colon
    "Goal: Sort the [NAME] cables before [DATE]",            # two spans
    "Goal: Sort the spare cables[ADDRESS][DATE]",            # two spans side by side
    "Goal: [SECRET]",                                        # the whole field
])
def test_a_goal_paragraph_the_privacy_layer_redacted_still_states_the_field(paragraph):
    """The privacy layer redacts, so the mirror's Goal paragraph may differ from the field: by its placeholders."""
    assert jgf.field_state(_row(f"{paragraph}\n\n{BODY_REDACTED}")) == (FRIDAY, None)
    assert jgf.field_state(_row(paragraph)) == (FRIDAY, None)


@pytest.mark.parametrize("mirror", [None, "", BODY, f"[NAME] cables before Friday\n\n{BODY}"])
def test_a_mirror_with_no_goal_line_changes_nothing(mirror):
    """A disclosure still pending (NULL or empty), or a mirror that renders no Goal paragraph at all."""
    row = _row()
    row[jgf.MIRROR] = mirror
    assert jgf.field_state(row) == (FRIDAY, None)


# --- what the mirror must still clear ----------------------------------------------------------------------------

@pytest.mark.parametrize("first", [f"Goal: {FRIDAY}", "Goal: Sort the spare cables before [DATE]"])
@pytest.mark.parametrize("rest", [
    "\n\n" + OTHER,                                              # a second Goal paragraph
    f"\n\n{BODY}\n\n" + OTHER,                                   # ... after the body
    "\n\nNotes.\n- goal : run a marathon",                       # a Goal line in a list
    "\n\nNotes.\n" + chr(0xFF27) + "oal" + chr(0xFF1A) + " run",  # fullwidth
    "\n\nG" + chr(0x200B) + "oal: run",                          # an invisible character inside
    "\n\n> GOAL: run",                                           # quoted, in capitals
    "\n" + OTHER,                                                # on the very next line
])
def test_a_further_goal_line_in_the_mirror_is_a_mismatch(first, rest):
    assert jgf.field_state(_row(first + rest)) == (None, "goal_field_mismatch")


@pytest.mark.parametrize("mirror", [
    f"Notes first.\n\nGoal: {FRIDAY}\n\n{BODY}",                  # the field itself, but not first
    f"Notes first.\nGoal: {FRIDAY}",
    f"[NAME] came by.\n\nGoal: Sort the spare cables before [DATE]",
    f"\nGoal: {FRIDAY}\n\n{BODY}",                                # not the text's rendering: a line before it
])
def test_a_mirror_whose_goal_line_is_not_its_first_paragraph_is_a_mismatch(mirror):
    assert jgf.field_state(_row(mirror)) == (None, "goal_field_mismatch")


@pytest.mark.parametrize("paragraph", [
    OTHER,                                                       # another goal (a mirror of an earlier text)
    "Goal: Run a [NAME]",                                        # ... with a placeholder in it
    "Goal: Sort the spare cables",                               # a shorter goal
    f"Goal: {FRIDAY} and the boxes",                             # a longer one
    "Goal: Sort the spare[NAME] cables before Friday",           # a placeholder that replaces nothing
    "Goal: Sort[NAME] the spare cables before [DATE]",           # ... beside one that replaces something
    "Goal: Sort the [NAME] boxes",                               # the words after the placeholder are not the field's
    "Goal: Sort the [NAME] boxes before [DATE]",                 # ... nor are the words between two placeholders
    "Goal: Sort the spare cables before [PERSON]",               # not one of the privacy layer's placeholders
    "Goal: Sort the spare cables before [date]",
    f"goal: {FRIDAY}",                                           # not the template's rendering
    f"Goal : {FRIDAY}",
])
def test_a_mirror_paragraph_that_is_not_the_field_or_the_field_redacted_is_a_mismatch(paragraph):
    assert jgf.field_state(_row(f"{paragraph}\n\n{BODY}")) == (None, "goal_field_mismatch")
    assert jgf.field_state(_row(paragraph)) == (None, "goal_field_mismatch")


# --- the mirror is the only column read this way -----------------------------------------------------------------

def _journal_columns(path):
    with _db(path) as conn:
        return [row[1] for row in conn.execute("PRAGMA table_info(journal_entries)")]


def test_a_goal_line_in_any_other_column_is_still_a_mismatch_beside_a_mirror(node):
    """Every column of the node's journal table but the text, the metadata and the mirror, by name: the columns a
    source writes (people, category, place, ...) and the other columns the node derives (the mirror's hash and
    model, the NSFW model, the event-time record)."""
    columns = [name for name in _journal_columns(node) if name not in ("content", "metadata_json", jgf.MIRROR)]
    assert {"people", "category", "mood_tag", "place_name", "duration", "source_record_id", "writer_app_id",
            "content_disclosure_hash", "content_disclosure_model", "content_nsfw_model",
            "event_time_json"} <= set(columns) and jgf.MIRROR in _journal_columns(node)
    redacted = "Goal: Sort the spare cables before [DATE]"
    for mirror in (None, TEXT, redacted + "\n\n" + BODY_REDACTED):
        assert jgf.field_state(_row(mirror)) == (FRIDAY, None)
        for name in columns:
            # Another goal, a Goal line further down, and the very paragraph the mirror may open with: the field's
            # own Goal paragraph, plain or redacted, is a second Goal line in any column but the mirror.
            for value in ("Goal: run a marathon", "Mara Example\nGoal: run", f"Goal: {FRIDAY}", TEXT, redacted):
                assert jgf.field_state(_row(mirror, **{name: value})) == (None, "goal_field_mismatch"), name


@pytest.mark.parametrize("change", [
    {"people": "Goal: run a marathon"},                          # the blind-set case rule v7 was written for
    {"people": "Mara Example\nGoal: run"},
    {"metadata_json": json.dumps({"goal": FRIDAY, "note": "Goal: run a marathon"})},
    {"metadata_json": '{"goal": "run", "goal": "' + FRIDAY + '"}'},
    {"content": TEXT + "\n\n" + OTHER},
])
def test_the_checks_the_rule_already_made_hold_beside_a_mirror(change):
    for mirror in (TEXT, "Goal: Sort the spare cables before [DATE]\n\n" + BODY_REDACTED):
        assert jgf.field_state({**_row(mirror), **change}) == (None, "goal_field_mismatch")


# --- a redacted mirror neither releases nor withholds a goal ----------------------------------------------------

def test_the_fields_own_guards_decide_whatever_the_mirror_redacted(terms):
    plain = _row("Goal: Sort the spare cables before [DATE]\n\n" + BODY_REDACTED)
    assert _rule(FRIDAY, plain, boundary=terms) is None
    # A goal the privacy layer redacted for a person: the field states it, and the third-party guard withholds it,
    # exactly as with no mirror at all. The redaction is never what decides.
    named = "Call Mara about the boxes"
    content = f"Goal: {named}\n\n{BODY}"
    for mirror in (None, content, f"Goal: Call [NAME] about the boxes\n\n{BODY_REDACTED}"):
        row = _row(mirror, goal=named, content=content)
        assert jgf.field_state(row) == (named, None)
        assert _rule(named, row, boundary=terms) == "goal_field_third_party"
    quill = "Email quill the spare cables"                      # an Off-limits alias (the `terms` fixture)
    row = _row("Goal: Email [NAME] the spare cables", goal=quill, content=f"Goal: {quill}")
    assert _rule(quill, row, boundary=terms) == "goal_field_offlimits"


@pytest.mark.parametrize("mirror", ["same", "body", "paragraph"])
def test_a_mirrored_entrys_goal_releases_citing_the_text_never_the_mirror(node, tmp_path, monkeypatch, field_on,
                                                                           mirror):
    stored = {"same": TEXT, "body": f"Goal: {FRIDAY}\n\n{BODY_REDACTED}",
              "paragraph": f"Goal: Sort the spare cables before [DATE]\n\n{BODY_REDACTED}"}[mirror]
    goal_id = _grounded_by_field(node, goal=FRIDAY, content=TEXT, content_disclosure=stored)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 2                                        # the entry, and the goal it grounds
    assert _code(search, "user_goals", goal_id) is None
    records, _bindings = _search(search, monkeypatch, "spare cables Friday ladder")
    (entry,) = _kind(records, "journal_entry")
    (item,) = _kind(records, "goal")
    assert (item["content"], item["status"]) == (FRIDAY, "stated_intention")
    assert entry["content"] == TEXT
    assert item["citations"] == [dict(record_id=entry["record_id"], source_id=SOURCE, content=TEXT)]
    assert "[NAME]" not in json.dumps(records) and "[DATE]" not in json.dumps(records)


@pytest.mark.parametrize("mirror", [f"Goal: {FRIDAY}\n\n{BODY}\n\n{OTHER}", f"{OTHER}\n\n{BODY}",
                                    f"Notes first.\n\nGoal: {FRIDAY}"])
def test_a_mirror_that_fails_the_rule_withholds_the_goal_end_to_end(node, tmp_path, monkeypatch, field_on, mirror):
    goal_id = _grounded_by_field(node, goal=FRIDAY, content=TEXT, content_disclosure=mirror)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 1                                        # the entry alone
    assert _code(search, "user_goals", goal_id) == "goal_not_grounded"
    assert _kind(_search(search, monkeypatch, "spare cables Friday ladder")[0], "goal") == []


def test_the_derivation_stores_a_mirrored_entrys_field_and_refuses_a_mirror_that_fails(node, tmp_path, monkeypatch,
                                                                                       field_on):
    _attest_owner(node)
    rows = {"same": (SORT, None), "redacted": (FRIDAY, "Goal: Sort the spare cables before [DATE]"),
            "second": ("Tidy the toolbox drawers", "Goal: Tidy the toolbox drawers\n\nNotes.\n\n" + OTHER),
            "other": ("Label the pantry boxes by shelf", OTHER), "pending": ("Wash the spare towels", "")}
    for entry_id, (goal, stored) in rows.items():
        content = f"Goal: {goal}\n\nAccomplished: {DONE} ({entry_id})"
        mirror = content if stored is None else stored or None
        _field_entry(node, entry_id, goal, content=content, content_disclosure=mirror)
        _publish(node, entry_id, domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    counts = _derive(search)
    assert counts["journal_members"] == 5 and counts["goal:written"] == 3
    assert counts["refused:goal_field_mismatch"] == 2
    assert sorted(g["record_id"] for g in _goals(node)) == ["pending", "redacted", "same"]
    assert {json.loads(g["payload_json"])["lineage"]["extractor"]["version"] for g in _goals(node)} == {jgf.VERSION}


# --- the premises ---------------------------------------------------------------------------------------------------

def test_the_mirror_is_the_privacy_layers_one_journal_column_and_no_ingest_door_writes_it(node):
    from topos.disclosure.canonical_writer import upsert_disclosure_fields
    from topos.disclosure.field_registry import disclosure_column, fields_for_table
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
    from topos.storage.db.migrations.canonical_disclosure_v1 import _DISCLOSURE_SPECS
    # One disclosed field on a journal row, `content`: a second one would be a second mirror the rule does not read.
    assert fields_for_table("journal_entries") == ("content",)
    assert disclosure_column("content") == jgf.MIRROR == "content_disclosure"
    assert jgf.MIRROR in _DISCLOSURE_SPECS["journal_entries"]
    hidden = TEXT + "\n\n" + OTHER
    with _db(node) as conn:
        SQLiteCanonicalStore(conn).upsert("journal_entries", {
            "entry_id": "door-1", "source_id": SOURCE, "entry_at": "2026-09-10T08:30:00", "content": TEXT,
            "metadata_json": json.dumps({"goal": FRIDAY}), "people": "",
            jgf.MIRROR: hidden, "content_disclosure_hash": "not-a-hash", "content_disclosure_model": "x"})
        row = dict(conn.execute("SELECT * FROM journal_entries WHERE entry_id='door-1'").fetchone())
        assert (row["content"], row[jgf.MIRROR], row["content_disclosure_hash"]) == (TEXT, None, None)
        assert jgf.field_state(row) == (FRIDAY, None)
        # The privacy layer's own writer is what fills it.
        assert upsert_disclosure_fields(conn, "journal_entries", "door-1", {jgf.MIRROR: hidden})
        row = dict(conn.execute("SELECT * FROM journal_entries WHERE entry_id='door-1'").fetchone())
        assert row[jgf.MIRROR] == hidden and jgf.field_state(row) == (None, "goal_field_mismatch")


def test_the_placeholders_are_the_privacy_layers_own():
    """Read by name. A placeholder the privacy layer adds or renames changes which mirrors this rule accepts: this
    list failing is the cue to look at the rule and move its VERSION."""
    from topos.sanitization.privacy_filter import ENTITY_PLACEHOLDERS, TRANSFORM_ENTITY_GROUPS
    marks = set(ENTITY_PLACEHOLDERS.values())
    assert marks == {"[NAME]", "[EMAIL]", "[PHONE]", "[ADDRESS]", "[ACCOUNT]", "[URL]", "[DATE]", "[SECRET]"}
    assert TRANSFORM_ENTITY_GROUPS["pii_redaction"] == frozenset(ENTITY_PLACEHOLDERS)   # no "[PII]" fallback is written
    pattern = jgf._placeholders()
    assert all(pattern.fullmatch(mark) for mark in marks)
    assert pattern.split("a[NAME]b[PII]c[name]d") == ["a", "b[PII]c[name]d"]


def test_the_rule_is_v2_and_no_index_basis_carries_its_version(monkeypatch, field_on):
    from topos.permissions_v2 import inferred_facts
    from topos.permissions_v2.search_index import _family_rubric_basis
    assert jgf.VERSION == "journal-goal-field/v2"
    monkeypatch.setenv(JOURNAL_FLAG, "true")
    monkeypatch.setenv(inferred_facts.FLAG, "true")
    revisions = _family_rubric_basis()["automatic_rubric_revisions"]
    assert set(revisions) == {"journal_entry", "inferred_facts"}             # the journal family and IF-6 only
    assert jgf.VERSION not in json.dumps(revisions)
    assert revisions["inferred_facts"] == inferred_facts.VERSION            # the one rule version a basis carries


def test_the_inferred_fact_entry_guards_never_read_the_mirror():
    """IF-6 guards 2a and 2b read a fixed list of columns and the metadata. The mirror is not one of them, so a
    placeholder such as "[SECRET]" (a marker word in brackets, which guard 2a reads as a tag in the text) or a
    special word that only the mirror holds cannot withhold a fact."""
    from topos.permissions_v2 import inferred_facts
    assert jgf.MIRROR not in inferred_facts.ENTRY_COLUMNS
    plain = {"content": "Planned the attic shelves.\nSorted the spare cables.", "people": "", "metadata_json": "{}"}
    assert inferred_facts.entry_refusal(plain) is None
    for cue, code in (("[SECRET]\nSorted the spare cables.", "inferred_entry_marked_special"),
                      ("Sorted the spare cables after therapy.", "inferred_entry_special_cue")):
        assert inferred_facts.entry_refusal({**plain, "content": cue}) == code          # the guard reads the text
        assert inferred_facts.entry_refusal({**plain, jgf.MIRROR: cue}) is None          # ... and not the mirror
