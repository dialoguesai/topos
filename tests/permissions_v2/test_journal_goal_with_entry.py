"""The journal goal field goes with its entry (owner decision, 1 Oct 2026; rule `journal-goal-field/v3`).

"When an entry is shared in full, its goal goes with it. Off-limits, NSFW and sensitivity are already enforced on the
entry itself." A grant that releases the cited journal entry whole, as a `journal_entry` record, already releases the
field's words: they are the entry's first paragraph. There the structured goal field is typed as a goal without the
rule's guards on the text's form. Pinned here:
  - what is set aside then: shape, special-category words, a question or quote, reported speech, negation, hedges,
    sarcasm, an ended state, a deferral, a third party (the node's people included), an unvetted word, not an
    intention; only `entry_released=True` sets them aside;
  - what still withholds then: the flag, the field's structure (with the mirror rule), NSFW, the owner's authorship
    and the attested self, the entry's own sensitivity, Off-limits on the goal text, and a field made only of the
    explicit placeholder lists;
  - "released whole" is decided per grant, at the build, at release and at the lane's write, by one function
    (`knowledge_projections.journal_entry_released`), which agrees with what the build and the release do with the
    entry itself: a grant without `journal_entry` refuses the goal as before; a grant that signs it but does not
    permit the entry refuses the goal at its citation already (a knowledge grant judges each of the entry's domains
    on its own, and a goal only adds `plans`); and the rule, asked for any read that does not release the entry
    whole, applies every guard;
  - the field is one goal, the member's: a goal stored for a same-text copy is not the field;
  - no owner command: every index build stores its own grant's fields first, safely again and again, and a failure
    there never fails the build; the refresh loop rebuilds the grants that can hold such a goal when the rule's state
    (version and flags) moves, and keeps the new state only after their restores;
  - the invariant: every goal released under a grant has its entry released whole under that same grant, and its
    text is the entry's own Goal paragraph.
Fixtures are synthetic; every name in them is invented.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_journal_family import (  # noqa: F401 (node is a fixture)
    SOURCE, _db, _journal_policy, node, owner)
from tests.permissions_v2.test_journal_goal_field import (
    DONE, SORT, _derive, _field_entry, _field_grounds, _goals, field_on, terms)  # noqa: F401 (fixtures)
from tests.permissions_v2.test_journal_typed_items import (
    _attest_owner, _code, _goal, _kind, _node, _publish, _search)
from tests.permissions_v2.test_refresh_loop import Clock, settings
from topos.permissions_v2 import journal_goal_field as jgf
from topos.permissions_v2 import knowledge_projections as kp
from topos.permissions_v2 import permitted_derivation as pd
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence_families import JOURNAL_FLAG
from topos.permissions_v2.refresh_loop import (STATE_FILE, STATE_VERSION, RefreshLoop, RestoreReceipt,
                                               goal_field_state)
from topos.permissions_v2.registry import parse_policy
from topos.permissions_v2.search_index import index_path

NO_RECORD_OPTION = ("message", "fact", "goal", "relationship")
# A goal field the rule's text guards withhold, with the code each one gives on its own.
WITHHELD_BY_THE_TEXT = [
    ("Attic tasks:", "goal_field_not_intention"),
    ("Help Tavrin finish her slides", "goal_field_third_party"),
    ("Errands", "goal_field_shape"),
    ("Sort the spare cables " + "and the boxes " * 25, "goal_field_shape"),
    ("Sort the\ncables", "goal_field_shape"),
    ("Formerly sorted the spare cables", "goal_field_ended"),
    ("Pick up the zinc refill at the pharmacy", "goal_field_special_category"),
    ("Maybe sort the spare cables", "goal_field_hedged"),
    ("Don't sort the spare cables", "goal_field_negated"),
    ("Should I sort the spare cables?", "goal_field_question_or_quote"),
    ("She said to sort the spare cables", "goal_field_reported"),
    ("Sort the spare cables ugh", "goal_field_sarcasm"),
    ("Sort the spare cables later", "goal_field_not_yet"),
    ("Sort the spare cables with zentith", "goal_field_unvetted_word"),
]


def _entry_of(goal, **extra):
    return {"content": f"Goal: {goal}\n\nAccomplished: {DONE}", "metadata_json": json.dumps({"goal": goal}),
            "content_nsfw": 0, **extra}


def _rule(goal, entry=None, *, released=True, boundary=None, people=frozenset(), sensitivity="personal",
          author=True, attested=True, env=None):
    return jgf.refusal(goal, _entry_of(goal) if entry is None else entry, boundary=boundary, author_is_owner=author,
                       subject_attested=attested, sensitivity=sensitivity, people=people, entry_released=released,
                       env={jgf.FLAG: "true"} if env is None else env)


def _plans_only(kinds=("message", "fact", "goal", "relationship", "journal_entry")):
    """A grant that signs `journal_entry` but permits only what is labelled `plans` alone: a knowledge grant judges
    every domain of an entry on its own, so an entry labelled `work` is not released by it, and neither is a goal
    citing that entry (`_support` merges the goal's `plans` into the entry's labels; it never removes `work`)."""
    raw = _journal_policy(kinds=kinds)
    for rule in raw["rules"]:
        for predicate in (rule["evidence_use"]["predicate"], rule["release"]["predicate"]):
            for term in predicate["terms"]:
                if term.get("attribute") == "domain":
                    term["values"] = ["plans"]
    return raw


# --- the rule on its own -------------------------------------------------------------------------------------

@pytest.mark.parametrize("goal, code", WITHHELD_BY_THE_TEXT)
def test_with_its_entry_released_the_text_guards_do_not_withhold(terms, goal, code):
    assert _rule(goal, released=False, boundary=terms) == code          # without the entry: as before
    assert _rule(goal, boundary=terms) is None                          # with it: the field is the goal


def test_with_its_entry_released_the_nodes_people_and_the_entrys_people_are_not_read(terms):
    goal = "Sort the spare cables for Varo"
    people = frozenset({"varo"})
    assert _rule(goal, released=False, boundary=terms, people=people) == "goal_field_third_party"
    assert _rule(goal, boundary=terms, people=people) is None
    listed = _entry_of("Sort the spare cables with Ana", people="Ana")
    assert _rule("Sort the spare cables with Ana", listed, released=False, boundary=terms) == "goal_field_third_party"
    assert _rule("Sort the spare cables with Ana", listed, boundary=terms) is None


@pytest.mark.parametrize("released", [False, None, 1, "true", "yes"])
def test_only_true_sets_the_text_guards_aside(terms, released):
    assert _rule("Maybe sort the spare cables", released=released, boundary=terms) == "goal_field_hedged"


ZERO_WIDTH, CYRILLIC_I, O_UMLAUT = chr(0x200B), chr(0x0456), chr(0x00F6)


@pytest.mark.parametrize("goal", [
    "Call Quillon Marsh about the boxes", "call quillon marsh about the boxes", "Email quill the spare cables",
    f"Call Quill{O_UMLAUT}n Marsh about the boxes", f"Call Quil{ZERO_WIDTH}lon Marsh about the boxes",
    f"Call Qu{CYRILLIC_I}llon Marsh about the boxes",                     # a Cyrillic look-alike letter
])
def test_with_its_entry_released_off_limits_on_the_goal_text_still_withholds(terms, goal):
    assert _rule(goal, boundary=terms) == "goal_field_offlimits"


class _Broken:
    def mentions_protected(self, *texts):
        raise RuntimeError("boundary unreadable")


@pytest.mark.parametrize("case, code", [
    ("flag_off", "goal_field_disabled"),
    ("nsfw_column", "goal_field_nsfw"),
    ("nsfw_metadata", "goal_field_nsfw"),
    ("not_the_field", "goal_field_mismatch"),
    ("metadata_differs", "goal_field_mismatch"),
    ("second_goal_line", "goal_field_mismatch"),
    ("mirror_states_another_goal", "goal_field_mismatch"),
    ("no_field", "goal_field_absent"),
    ("not_the_owners_words", "goal_field_author"),
    ("no_attested_self", "goal_field_author"),
    ("special", "goal_field_special_category"),
    ("unknown", "goal_field_special_category"),
    ("no_boundary", "goal_field_boundary_unavailable"),
    ("boundary_fails", "goal_field_boundary_unavailable"),
])
def test_with_its_entry_released_everything_before_the_text_still_withholds(terms, case, code):
    goal = "Maybe sort the spare cables"
    entry = _entry_of(goal)
    kwargs = {"boundary": terms}
    if case == "flag_off":
        kwargs["env"] = {}
    elif case == "nsfw_column":
        entry["content_nsfw"] = 1
    elif case == "nsfw_metadata":
        entry["metadata_json"] = json.dumps({"goal": goal, "nsfw": True})
    elif case == "metadata_differs":
        entry["metadata_json"] = json.dumps({"goal": "Sort the spare cables"})
    elif case == "second_goal_line":
        entry["content"] += f"\n\nGoal: {goal}"
    elif case == "mirror_states_another_goal":
        entry[jgf.MIRROR] = f"Goal: Sort the spare boxes\n\nAccomplished: {DONE}"
    elif case == "no_field":
        entry = {"content": DONE, "metadata_json": "{}", "content_nsfw": 0}
    elif case == "not_the_owners_words":
        kwargs["author"] = False
    elif case == "no_attested_self":
        kwargs["attested"] = False
    elif case in ("special", "unknown"):
        kwargs["sensitivity"] = case
    elif case == "no_boundary":
        kwargs["boundary"] = None
    elif case == "boundary_fails":
        kwargs["boundary"] = _Broken()
    text = "Maybe sort the spare boxes" if case == "not_the_field" else goal
    assert _rule(text, entry, **kwargs) == code


@pytest.mark.parametrize("goal", ["TBD", "todo", "misc", "Your goal here", "Goal one", "  "])
def test_a_field_made_only_of_the_explicit_placeholder_lists_withholds(terms, goal):
    assert _rule(goal, boundary=terms) == "goal_field_placeholder"


@pytest.mark.parametrize("goal", ["Write the checklist for the trip", "Tidy up etc", "Insert the new shelf pegs",
                                  "Sort the todo cards"])
def test_a_field_that_only_uses_a_placeholder_word_stands(terms, goal):
    assert _rule(goal, boundary=terms) is None


def test_the_lanes_own_goal_shape_is_not_asked_of_the_field_but_off_limits_is():
    long = "Sort the spare cables " + "and the boxes " * 25
    assert pd.refusal(pd.Spec("goal", "goal", long), None) == "goal_shape"
    assert pd.refusal(pd.Spec("goal", "goal", long), None, form=False) is None
    assert pd.refusal(pd.Spec("goal", "goal", 7), None, form=False) == "goal_shape"

    class Vetoing:
        active = True

        def legacy_veto(self, table, row):
            return "quill" in row["payload_json"].lower()
    assert pd.refusal(pd.Spec("goal", "goal", "Email Quill the cables"), Vetoing(), form=False) == "entity_protected"
    assert pd.refusal(pd.Spec("goal", "goal", "Sort the cables"), Vetoing(), form=False) is None


# --- per grant ---------------------------------------------------------------------------------------------

def _released_whole(search, entry_id, raw):
    """`journal_entry_released` for one entry under one grant, on the entry as that grant's read qualifies it."""
    from topos.permissions_v2.message_evidence import qualify_automatic_message
    policy = parse_policy(raw)
    resolver, reviews, now = search.corpus.resolver, search.corpus.reviews, search.now[0]
    with resolver._read() as (conn, floor), reviews._db() as db:
        identity = resolver._identity("journal_entries", entry_id, SOURCE)
        try:
            qualified, rows = qualify_automatic_message(resolver, conn, floor, identity, reviews, db)
        except PolicyError:
            return False
        return kp.journal_entry_released(policy, qualified, rows, (now - policy.search.window.max_age_seconds)
                                         * 10**6, now * 10**6)


def _hedged(path, entry_id="e1", goal="Maybe sort the spare cables", *, domains=("plans",), **extra):
    _attest_owner(path)
    _field_entry(path, entry_id, goal, **extra)
    _publish(path, entry_id, domains=list(domains))
    return goal


def _grounds_in(search, goal_id, raw, lower_us, upper_us):
    """`knowledge_projections._goal_field` as `goal_projection` asks it, with this grant and this read's window."""
    from topos.permissions_v2.message_evidence import qualify_automatic_message
    resolver, reviews = search.corpus.resolver, search.corpus.reviews
    with resolver._read() as (conn, floor), reviews._db() as db:
        goal_row = dict(conn.execute("SELECT * FROM user_goals WHERE goal_id=?", (goal_id,)).fetchone())
        identity = resolver._identity("journal_entries", goal_row["record_id"], goal_row["source_id"])
        qualified, rows = qualify_automatic_message(resolver, conn, floor, identity, reviews, db)
        return kp._goal_field(conn, qualified, rows, goal_row, resolver.entity_boundary(conn),
                              policy=parse_policy(raw), lower_us=lower_us, upper_us=upper_us)


GRANT_SHAPES = {"plain": _journal_policy, "plans-only": _plans_only,
                "no-option": lambda: _journal_policy(kinds=NO_RECORD_OPTION)}


def test_one_stored_goal_releases_with_its_entry_and_is_withheld_under_grants_that_do_not_release_it(
        node, tmp_path, monkeypatch, field_on):
    """One stored goal (a hedge, which the text guards withhold), three grants: the grant that releases its entry
    whole types it as a goal; a grant without the record option refuses it as before; a grant that signs the option
    but does not permit the entry refuses both the entry and the goal. The rule itself, asked for any grant that
    does not release the entry whole, applies every guard."""
    goal = _hedged(node, domains=("work",))
    search, _state = _node(node, tmp_path, monkeypatch)
    (stored,) = _goals(node)                                     # the build stored it, under the grant it serves
    assert stored["goal_text"] == goal
    assert _released_whole(search, "e1", search.search_raw) is True
    assert _code(search, "user_goals", stored["goal_id"]) is None
    assert _field_grounds(search, stored["goal_id"], policy=search.search_raw) is True
    records, _bindings = _search(search, monkeypatch, "spare cables")
    assert [r["content"] for r in _kind(records, "goal")] == [goal] and len(_kind(records, "journal_entry")) == 1

    without = _journal_policy(kinds=NO_RECORD_OPTION)
    assert _released_whole(search, "e1", without) is False
    assert _code(search, "user_goals", stored["goal_id"], raw=without) == "journal_citation_needs_record_option"
    assert _field_grounds(search, stored["goal_id"], policy=without) is False

    plans = _plans_only()
    assert _released_whole(search, "e1", plans) is False
    assert _code(search, "user_goals", stored["goal_id"], raw=plans) == "evidence_not_permitted"
    assert _field_grounds(search, stored["goal_id"], policy=plans) is False
    assert _field_grounds(search, stored["goal_id"], policy=None) is False


@pytest.mark.parametrize("goal, grounded", [("Maybe sort the spare cables", False), (SORT, True)])
def test_asked_for_a_read_that_does_not_release_the_entry_the_rule_applies_every_guard(
        node, tmp_path, monkeypatch, field_on, goal, grounded):
    """`_goal_field` decides whole release itself, on the policy and window it is handed (guard independence):
    under the grant that signs the option, a window that leaves the entry out is a read that does not release it,
    so a plain field still grounds by the full rule and a hedge does not."""
    _hedged(node, goal=goal)
    search, _state = _node(node, tmp_path, monkeypatch)
    (stored,) = _goals(node)
    now = search.now[0]
    inside = ((now - 90 * 86400) * 10**6, now * 10**6)
    before = (inside[0] - 30 * 86400 * 10**6, inside[0] - 86400 * 10**6)
    assert _grounds_in(search, stored["goal_id"], search.search_raw, *inside) is True
    assert _grounds_in(search, stored["goal_id"], search.search_raw, *before) is grounded


def test_a_build_under_a_grant_that_does_not_release_the_entry_stores_and_releases_nothing(
        node, tmp_path, monkeypatch, field_on):
    _hedged(node, domains=("work",))
    search, state = _node(node, tmp_path, monkeypatch, grant=_plans_only)
    assert _goals(node) == [] and state["member_count"] == 0
    assert _derive(search)["journal_members"] == 0


@pytest.mark.parametrize("grant", sorted(GRANT_SHAPES))
@pytest.mark.parametrize("entry_id, extra, labels, whole", [
    ("plain", {}, {}, {"plain", "plans-only"}),
    ("old", {"entry_at": "2026-06-01T08:30:00"}, {}, set()),                 # outside the 90 days
    ("special", {}, {"sensitivity": "special"}, set()),                      # its own labels are not permitted
    ("work", {}, {"domains": ["work", "plans"]}, {"plain"}),                 # `work` too: the plain grant only
])
def test_released_whole_agrees_with_what_the_build_and_the_release_do_with_the_entry(
        node, tmp_path, monkeypatch, field_on, grant, entry_id, extra, labels, whole):
    """The entry is released whole exactly when the build and the release give it out as a `journal_entry` record
    under that grant, and a goal the text guards withhold releases exactly then."""
    goal = "Maybe sort the spare cables"
    _attest_owner(node)
    _field_entry(node, entry_id, goal, **extra)
    _publish(node, entry_id, **{"domains": ["plans"], **labels})
    search, _state = _node(node, tmp_path, monkeypatch, grant=GRANT_SHAPES[grant])
    expected = grant in whole
    assert _released_whole(search, entry_id, search.search_raw) is expected
    records, _bindings = _search(search, monkeypatch, "spare cables")
    assert len(_kind(records, "journal_entry")) == int(expected)
    assert [r["content"] for r in _kind(records, "goal")] == ([goal] if expected else [])
    assert len(_goals(node)) == int(expected)                                  # the build stored it only then


def test_released_whole_reads_the_grant_and_the_row_it_is_handed(node, tmp_path, monkeypatch, field_on):
    """Each condition on its own: a knowledge grant that signs `journal_entry` and lists the journal table, the
    journal family, the NSFW withhold, at most 8,000 characters (an entry that long is never assessed, so no build
    reaches it), and the window; what cannot be decided is not released."""
    from topos.permissions_v2.search_contract import CAPABILITY_KNOWLEDGE_SEARCH
    _hedged(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    policy = parse_policy(search.search_raw)
    assert policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH
    other = SimpleNamespace(versions=SimpleNamespace(capability="message-search"), search=policy.search)
    no_kind = SimpleNamespace(versions=policy.versions, search=SimpleNamespace(
        result_types=[kind for kind in policy.search.result_types if kind != "journal_entry"],
        tables=policy.search.tables))
    no_table = SimpleNamespace(versions=policy.versions, search=SimpleNamespace(
        result_types=policy.search.result_types, tables=["conversation_messages"]))
    with search.corpus.resolver._read() as (conn, floor), search.corpus.reviews._db() as db:
        from topos.permissions_v2.message_evidence import qualify_automatic_message
        identity = search.corpus.resolver._identity("journal_entries", "e1", SOURCE)
        qualified, rows = qualify_automatic_message(search.corpus.resolver, conn, floor, identity,
                                                    search.corpus.reviews, db)
    window = ((search.now[0] - policy.search.window.max_age_seconds) * 10**6, search.now[0] * 10**6)
    assert kp.journal_entry_released(policy, qualified, rows, *window) is True
    # The grant's shape is checked on its own, never left to the decision: with a decision that permits anything,
    # these still are not released.
    real = kp.source_message_decision
    permits = SimpleNamespace(verdict="permit")
    monkeypatch.setattr(kp, "source_message_decision", lambda grant, evidence: permits)
    for grant in (other, no_kind, no_table):
        assert kp.journal_entry_released(grant, qualified, rows, *window) is False
    monkeypatch.setattr(kp, "source_message_decision", lambda grant, evidence: SimpleNamespace(verdict="deny"))
    assert kp.journal_entry_released(policy, qualified, rows, *window) is False
    monkeypatch.setattr(kp, "source_message_decision", real)
    assert kp.journal_entry_released(policy, qualified, rows, *window) is True
    (key,) = rows
    for changed in ({"content_nsfw": 1}, {"content": rows[key]["content"] + "x" * 8000}, {"content": None}):
        assert kp.journal_entry_released(policy, qualified, {key: {**rows[key], **changed}}, *window) is False
    assert kp.journal_entry_released(policy, qualified, rows, window[0] - 10**12, window[0] - 10**11) is False
    assert kp.journal_entry_released(policy, qualified, {}, *window) is False             # cannot be decided
    monkeypatch.delenv(JOURNAL_FLAG)
    assert kp.journal_entry_released(policy, qualified, rows, *window) is False


def test_a_goal_stored_for_a_same_text_copy_is_not_the_field(node, tmp_path, monkeypatch, field_on):
    """The copy resolves to the member (IF-5 §1.2) and would release beside the member's own goal, word for word.
    The field is one goal: the member's, which the build stores."""
    goal = "Maybe sort the spare cables"
    _attest_owner(node)
    _field_entry(node, "e1", goal)
    _field_entry(node, "e2", goal, entry_at="2026-09-10T09:30:00")         # the same text, later: a copy
    copy_goal = _goal(node, "e2", goal, goal_id="goal-on-the-copy")
    _publish(node, "e1", domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "user_goals", copy_goal) == "goal_not_grounded"
    member = [g for g in _goals(node) if g["record_id"] == "e1"]
    assert len(member) == 1 and _code(search, "user_goals", member[0]["goal_id"]) is None
    assert [r["content"] for r in _kind(_search(search, monkeypatch, "spare cables")[0], "goal")] == [goal]


# --- the lane --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", ["nsfw", "outside_window"])
def test_the_lane_asks_again_at_the_write_whether_the_entry_is_released(node, tmp_path, monkeypatch, field_on,
                                                                         change):
    goal = _hedged(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    with _db(node) as conn:
        conn.execute("DELETE FROM user_goals")
    lane = pd.JournalGoalFieldPass(search.index)
    selected = lane._selected

    def select_then_change(now, grant_id=None):
        out = selected(now, grant_id)
        with _db(node) as conn:                                        # the text is unchanged: same revision
            if change == "nsfw":
                conn.execute("UPDATE journal_entries SET content_nsfw=1 WHERE entry_id='e1'")
            else:
                conn.execute("UPDATE journal_entries SET entry_at='2026-06-01T08:30:00' WHERE entry_id='e1'")
        return out
    lane._selected = select_then_change
    with owner():
        counts = lane.run(now=search.now[0])
    assert counts == {"journal_members": 1, "refused:entry_not_released": 1} and _goals(node) == []
    assert goal


def test_the_lane_selects_only_entries_its_grant_releases_whole(node, tmp_path, monkeypatch, field_on):
    """The selection asks release's own question too (it is the build's admission otherwise), so an entry the
    grant does not release whole is never selected, let alone written."""
    _hedged(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    with _db(node) as conn:
        conn.execute("DELETE FROM user_goals")
    monkeypatch.setattr(kp, "journal_entry_released", lambda *args, **kwargs: False)
    assert _derive(search) == {"journal_members": 0} and _goals(node) == []


def test_the_lane_adds_no_twin_beside_an_older_writers_row(node, tmp_path, monkeypatch, field_on):
    goal = _hedged(node)
    _goal(node, "e1", goal, goal_id="an-older-writers-id")                 # same entry and text, no lineage
    search, _state = _node(node, tmp_path, monkeypatch)
    assert [g["goal_id"] for g in _goals(node)] == ["an-older-writers-id"]
    assert _derive(search)["goal:already_stored"] == 1
    assert [r["content"] for r in _kind(_search(search, monkeypatch, "spare cables")[0], "goal")] == [goal]


def test_the_lane_opens_no_write_when_nothing_is_selected(node, tmp_path, monkeypatch, field_on):
    _hedged(node)
    search, _state = _node(node, tmp_path, monkeypatch, kinds=NO_RECORD_OPTION)

    def no_write(*args, **kwargs):
        raise AssertionError("a write was opened")
    monkeypatch.setattr("topos.storage.db.write_gate.with_db_write", no_write)
    assert _derive(search) == {"journal_members": 0}


def test_the_lane_stores_under_the_grant_that_releases_the_entry_whole(node, tmp_path, monkeypatch, field_on):
    from topos.storage.derived_row_identity import derived_row_id
    goals = {"t1": "Help Tavrin finish her slides", "t2": "Attic tasks:", "t3": "Errands",
             "t4": "Formerly sorted the spare cables", "t5": "Pick up the zinc refill at the pharmacy",
             "tbd": "TBD"}
    _attest_owner(node)
    for entry_id, goal in goals.items():
        _field_entry(node, entry_id, goal, accomplished=f"{DONE} ({entry_id})")
        _publish(node, entry_id, domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    stored = {(g["goal_id"], g["record_id"], g["goal_text"]) for g in _goals(node)}
    assert stored == {(derived_row_id("user_goals", (e, g)), e, g) for e, g in goals.items() if e != "tbd"}
    counts = _derive(search)
    assert counts == {"journal_members": 6, "goal:unchanged": 5, "refused:goal_field_placeholder": 1}
    for goal_id, _entry_id, _text in stored:
        payload = json.loads(next(g["payload_json"] for g in _goals(node) if g["goal_id"] == goal_id))
        assert payload["lineage"]["extractor"] == {"kind": "journal_goal_field", "version": jgf.VERSION}


def test_the_lane_qualifies_only_the_entries_that_carry_a_goal(node, tmp_path, monkeypatch, field_on):
    """Most members carry no goal field; the lane does not qualify them again, so a build pays only for the few."""
    from tests.permissions_v2.test_journal_family import _entry
    from topos.permissions_v2 import message_evidence
    _hedged(node)
    _entry(node, "plain", "Walked home the long way after the spare cables meeting.")
    _field_entry(node, "mismatch", SORT, metadata={"goal": "Sort the cables"})
    for entry_id in ("plain", "mismatch"):
        _publish(node, entry_id, domains=["plans"])
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 4                                  # three entries, one goal
    seen = []
    real = message_evidence.qualify_automatic_message

    def spy(resolver, conn, floor, identity, *args, **kwargs):
        seen.append(identity.record_id)
        return real(resolver, conn, floor, identity, *args, **kwargs)
    monkeypatch.setattr(message_evidence, "qualify_automatic_message", spy)
    counts = _derive(search)
    assert sorted(seen) == ["e1", "mismatch"]
    assert counts == {"journal_members": 2, "goal:unchanged": 1, "refused:goal_field_mismatch": 1}


# --- no owner command ---------------------------------------------------------------------------------------

def test_a_new_journal_member_has_its_goal_at_the_next_build_with_no_command(node, tmp_path, monkeypatch, field_on):
    first = _hedged(node)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 2 and [g["goal_text"] for g in _goals(node)] == [first]
    second = "Help Tavrin finish her slides"
    _field_entry(node, "e2", second, accomplished=f"{DONE} (2)")
    _publish(node, "e2", domains=["work", "plans"])
    with owner():
        again = search.index.rebuild("grant-search", now=search.now[0])   # as the restore or the review refresh
    assert again["member_count"] == 4
    assert sorted(g["goal_text"] for g in _goals(node)) == sorted([first, second])
    before = _goals(node)
    with owner():
        search.index.rebuild("grant-search", now=search.now[0])
    assert _goals(node) == before                                          # repeatable: nothing new, nothing moved


def _graph(path):
    """The node's graph rebuild for goals (`materialize_graph_enrichments`), with one cluster for every text."""
    from topos.features.entities.graph_enrichers import materialize_graph_enrichments
    with _db(path) as conn:
        materialize_graph_enrichments(conn, goal_embed_fn=lambda batch: [[1.0, 0.0] for _ in batch])


def test_the_graph_names_the_field_so_its_relationship_releases_with_it(node, tmp_path, monkeypatch, field_on):
    """An extracted goal with the field's words (another record, met first) and a variant with more occurrences sit
    in the field's group and cluster. The graph names the field's own row and labels the node with its text, so after
    the next build the relationship releases beside the goal, under the grant that releases the entry whole."""
    goal = _hedged(node, domains=("plans",))
    with _db(node) as conn:
        for goal_id, record, text in (("x-same", "msg-1", goal.lower()), ("x-more1", "msg-2", goal + " this week"),
                                      ("x-more2", "msg-3", goal + " this week")):
            conn.execute("INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
                         "VALUES (?,?,'chatgpt_file_ingestion',?,'{}')", (goal_id, record, text))
    search, _state = _node(node, tmp_path, monkeypatch)               # the build stores the field's goal
    (field,) = [g for g in _goals(node) if g["record_id"] == "e1"]
    _graph(node)
    with owner():
        search.index.rebuild("grant-search", now=search.now[0])        # the relationship joins at the next build
    records, _bindings = _search(search, monkeypatch, "spare cables")
    (edge,) = _kind(records, "relationship")
    assert (edge["relation"], edge["object"]) == ("pursues", goal)
    assert [r["content"] for r in _kind(records, "goal")] == [goal]
    with _db(node) as conn:
        named = [json.loads(m)["source_object_id"] for (m,) in conn.execute(
            "SELECT metadata_json FROM entity_edges WHERE edge_type='pursues' AND valid_to IS NULL")]
    assert named == [field["goal_id"]]


def _graph_state(path):
    with _db(path) as conn:
        return tuple(conn.execute("SELECT dirty_generation, materialized_generation FROM graph_materialization_state "
                                  "WHERE id=1").fetchone())


def test_a_goal_the_build_stores_marks_the_graph_dirty_so_relationships_need_no_enrichment_run(
        node, tmp_path, monkeypatch, field_on):
    """The build stores the field and marks the graph dirty in that write's transaction, and arms the graph's own
    debounced rebuild: its `pursues` edge does not wait for the next enrichment run, and a node that stops before the
    debounce fires rebuilds the graph at startup. A build that writes nothing marks nothing."""
    from topos.features.entities import graph_refresh
    armed = []
    monkeypatch.setattr(graph_refresh, "schedule_graph_refresh", lambda: armed.append(1))
    _hedged(node)
    assert _graph_state(node) == (0, 0)
    search, _state = _node(node, tmp_path, monkeypatch)
    assert len(_goals(node)) == 1 and _graph_state(node) == (1, 0) and armed == [1]
    with owner():
        search.index.rebuild("grant-search", now=search.now[0])            # nothing new to store
    assert _graph_state(node) == (1, 0) and armed == [1]
    rebuilt = []
    graph_refresh.reset_for_tests(rebuild_fn=lambda: rebuilt.append(1))
    try:
        with _db(node) as conn:
            graph_refresh.reconcile_graph_on_startup(conn)
    finally:
        graph_refresh.reset_for_tests()
    assert rebuilt == [1]


@pytest.mark.parametrize("state", ["recorded", "no_state_row", "record_fails"])
def test_the_lane_counts_the_mark_and_arms_the_rebuild_whatever_the_state_row(node, tmp_path, monkeypatch, field_on,
                                                                             state):
    import sqlite3 as _sqlite3
    from topos.features.entities import graph_refresh
    armed = []
    monkeypatch.setattr(graph_refresh, "schedule_graph_refresh", lambda: armed.append(1))
    _hedged(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    with _db(node) as conn:
        conn.execute("DELETE FROM user_goals")                              # so the lane writes again
        if state == "no_state_row":
            conn.execute("DROP TABLE graph_materialization_state")
    if state == "record_fails":
        def broken(conn):
            raise _sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(graph_refresh, "record_graph_dirty", broken)
    counts = _derive(search)
    marked = "graph:marked_dirty" if state == "recorded" else "graph:dirty_not_recorded"
    assert counts["goal:written"] == 1 and counts[marked] == 1 and armed == [1, 1]
    assert len(_goals(node)) == 1                                           # the goal stands either way


@pytest.mark.parametrize("off", [jgf.FLAG, JOURNAL_FLAG])
def test_with_the_flag_or_the_family_off_the_build_stores_nothing(node, tmp_path, monkeypatch, field_on, off):
    _hedged(node)
    calls = []
    real = pd.JournalGoalFieldPass.run
    monkeypatch.setattr(pd.JournalGoalFieldPass, "run", lambda self, **kw: calls.append(kw) or real(self, **kw))
    monkeypatch.delenv(off)
    _node(node, tmp_path, monkeypatch)
    assert _goals(node) == [] and calls == []


def test_a_failing_goal_field_step_never_fails_the_build(node, tmp_path, monkeypatch, field_on, caplog):
    _hedged(node)

    def broken(self, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(pd.JournalGoalFieldPass, "run", broken)
    with caplog.at_level("WARNING", logger="topos.permissions_v2.search_index"):
        _search_node, state = _node(node, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 1} and _goals(node) == []
    assert "OperationalError" in caplog.text and "locked" not in caplog.text


def test_the_build_asks_only_its_own_grant(node, tmp_path, monkeypatch, field_on):
    _hedged(node)
    seen = []
    real = pd.JournalGoalFieldPass.run

    def spy(self, **kwargs):
        seen.append((kwargs.get("grant_id"), kwargs.get("rebuild")))
        return real(self, **kwargs)
    monkeypatch.setattr(pd.JournalGoalFieldPass, "run", spy)
    _node(node, tmp_path, monkeypatch)
    assert seen == [("grant-search", False)]


# --- the refresh loop: the rule's state ------------------------------------------------------------------------

GRANTS = {
    "g-goals": lambda: _journal_policy(),                                          # releases entries and goals
    "g-relationships": lambda: _journal_policy(kinds=("relationship", "journal_entry")),
    "g-unbuilt": lambda: _journal_policy(),                                        # could; never had an index
    "g-no-option": lambda: _journal_policy(kinds=NO_RECORD_OPTION),
    "g-entries-only": lambda: _journal_policy(kinds=("message", "journal_entry")),
}
BUILT = ("g-goals", "g-relationships", "g-no-option", "g-entries-only")


class _Service:
    def __init__(self, root):
        self.root, self.rebuilds, self.state = root, [], "ready"
        self.resolver = SimpleNamespace(path=root / "canonical.db")

    def rebuild(self, grant_id, *, now):
        self.rebuilds.append(grant_id)
        return {"state": self.state, "member_count": 1}


class _Loop(RefreshLoop):
    def __init__(self, root, service, clock, **overrides):
        ledger = SimpleNamespace(identity=SimpleNamespace(owner_id="owner-1"))
        super().__init__(ledger=ledger, root=root, index=lambda: service, worker=None,
                         settings=settings(**{"debounce": 30, **overrides}), clock=clock)
        self.recorded = []

    def _active_grants(self, now):
        return [(grant_id, None, parse_policy(make())) for grant_id, make in GRANTS.items()]

    def _policy_hash(self, grant_id, now):
        return "a" * 64

    def _current_signals(self, service):
        return ("digest", ("clock", 1))

    def _record(self, receipt):
        self.recorded.append(receipt)


def _loop(tmp_path, clock, *, kept="absent", **overrides):
    """`kept`: "absent" leaves the state file as it is (none on a fresh root); "before" writes one from before this
    rule state existed (an upgrading node's); anything else writes that rule state."""
    root = tmp_path / "index"
    root.mkdir(exist_ok=True)
    for grant_id in BUILT:
        index_path(root, grant_id).write_bytes(b"")
    if kept != "absent":
        state = {"version": STATE_VERSION, "names": sorted(index_path(root, g).name for g in BUILT),
                 "ingest_high_water": None, "last_full_pass_at": None, "assessment_revisions": None,
                 "proof_digest": None, "continuation": None, "fact_digest": None}
        if kept != "before":
            state["goal_field"] = kept
        (root / STATE_FILE).write_text(json.dumps(state), "utf-8")
    service = _Service(root)
    return _Loop(root, service, clock, **overrides), service


def _kept(loop):
    return json.loads((loop.root / STATE_FILE).read_text("utf-8")).get("goal_field")


@pytest.fixture
def rule_on(monkeypatch):
    monkeypatch.setenv(jgf.FLAG, "true")
    monkeypatch.setenv(JOURNAL_FLAG, "true")


def test_the_rules_state_is_its_version_while_both_flags_are_on():
    on = {jgf.FLAG: "true", JOURNAL_FLAG: "true"}
    assert goal_field_state(on) == jgf.VERSION == "journal-goal-field/v3"
    assert goal_field_state({jgf.FLAG: "true"}) is None
    assert goal_field_state({JOURNAL_FLAG: "true"}) is None
    assert goal_field_state({}) is None


def test_an_upgrade_that_brings_the_rule_rebuilds_the_grants_that_can_hold_its_goals_once(tmp_path, rule_on):
    clock = Clock(1000)
    loop, service = _loop(tmp_path, clock, kept="before")      # a state file from before: no rule state kept
    loop.observe(service)
    assert {g: e["causes"] for g, e in loop._pending.items()} == {
        "g-goals": {"goal_field_changed"}, "g-relationships": {"goal_field_changed"}}
    loop.observe(service)                                      # queued once
    assert set(loop._pending) == {"g-goals", "g-relationships"}
    clock.now = 1029
    assert loop.run_pending() is None and service.rebuilds == []         # the restore's own debounce
    clock.now = 1030
    receipt = loop.run_pending()
    assert receipt.cause_classes == ["goal_field_changed"] and service.rebuilds == ["g-goals", "g-relationships"]
    assert loop._pending == {} and _kept(loop) == jgf.VERSION
    loop.observe(service)
    assert loop._pending == {}                                 # kept: nothing more to do


def test_the_state_is_kept_only_after_the_restores_so_a_restart_queues_them_again(tmp_path, rule_on):
    clock = Clock(1000)
    loop, service = _loop(tmp_path, clock, kept="journal-goal-field/v2")
    loop.observe(service)
    assert set(loop._pending) == {"g-goals", "g-relationships"} and _kept(loop) == "journal-goal-field/v2"
    restarted, service = _loop(tmp_path, clock)                # the node restarts before the restore ran
    restarted.observe(service)
    assert set(restarted._pending) == {"g-goals", "g-relationships"}


def test_a_restore_that_gives_up_still_keeps_the_state(tmp_path, rule_on):
    clock = Clock(1000)
    loop, service = _loop(tmp_path, clock, kept="journal-goal-field/v2", max_attempts=1)
    service.state = "stale"
    loop.observe(service)
    clock.now = 1030
    loop.run_pending()
    assert loop._pending == {} and _kept(loop) == jgf.VERSION


def test_turning_the_rule_off_rebuilds_them_once_so_its_goals_leave(tmp_path, monkeypatch):
    monkeypatch.delenv(jgf.FLAG, raising=False)
    clock = Clock(1000)
    loop, service = _loop(tmp_path, clock, kept=jgf.VERSION)
    loop.observe(service)
    assert set(loop._pending) == {"g-goals", "g-relationships"}
    clock.now = 1030
    loop.run_pending()
    assert _kept(loop) is None and service.rebuilds == ["g-goals", "g-relationships"]


def test_with_the_rule_off_and_never_on_nothing_is_queued(tmp_path, monkeypatch):
    monkeypatch.delenv(jgf.FLAG, raising=False)
    loop, service = _loop(tmp_path, Clock(1000))
    loop.observe(service)
    assert loop._pending == {}


def test_with_no_index_that_can_hold_a_goal_the_state_is_kept_at_once(tmp_path, rule_on):
    loop, service = _loop(tmp_path, Clock(1000))
    for grant_id in ("g-goals", "g-relationships"):
        index_path(loop.root, grant_id).unlink()
    loop.observe(service)
    assert loop._pending == {} and _kept(loop) == jgf.VERSION


def test_a_failing_rule_check_never_stops_the_drop_check_and_is_seen_again(tmp_path, rule_on, monkeypatch):
    loop, service = _loop(tmp_path, Clock(1000), kept=None)

    def unavailable(now):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(loop, "_goal_field_grants", unavailable)
    loop.observe(service)                                      # records the names; the rule check fails quietly
    index_path(loop.root, "g-entries-only").unlink()
    loop._dropped_grants = lambda now, names: ["g-entries-only"]
    loop.observe(service)                                      # never raises; the drop is still queued
    assert set(loop._pending) == {"g-entries-only"} and _kept(loop) is None
    monkeypatch.undo()
    monkeypatch.setenv(jgf.FLAG, "true")
    monkeypatch.setenv(JOURNAL_FLAG, "true")
    loop.observe(service)                                      # the same move, seen on the next sweep
    assert {g for g, e in loop._pending.items() if "goal_field_changed" in e["causes"]} == {"g-goals",
                                                                                         "g-relationships"}


def test_without_the_restore_flag_nothing_is_observed(tmp_path, rule_on):
    loop, service = _loop(tmp_path, Clock(1000), restore=False)
    loop.observe(service)
    assert loop._pending == {} and not (loop.root / STATE_FILE).exists()


def test_the_receipt_with_the_new_cause_reads_back():
    receipt = RestoreReceipt(version="topos-node-system-action/v1", action="search_index_restore",
                             actor="node_system", cause_classes=["goal_field_changed"], first_drop_at=1, started_at=2,
                             finished_at=3, protection_synced=False, grants=[])
    assert RestoreReceipt.model_validate(json.loads(json.dumps(receipt.model_dump()))) == receipt


@pytest.mark.parametrize("make, holds", [
    (lambda: _journal_policy(), True),
    (lambda: _journal_policy(kinds=("relationship", "journal_entry")), True),
    (lambda: _journal_policy(kinds=NO_RECORD_OPTION), False),
    (lambda: _journal_policy(kinds=("message", "journal_entry")), False),
])
def test_one_function_names_the_grants_that_can_hold_a_goal_field(make, holds):
    assert pd.goal_field_grant(parse_policy(make())) is holds


# --- the invariant ----------------------------------------------------------------------------------------------

def test_every_released_goal_has_its_entry_released_whole_under_the_same_grant_and_is_its_goal_paragraph(
        node, tmp_path, monkeypatch, field_on):
    """Under each grant shape, every stored goal `qualify_projection` releases (the build's and the release's own
    call) is its entry's Goal paragraph, cites the entry's whole text, and has its entry released whole under that
    same grant; a grant without the record option releases none of them."""
    _attest_owner(node)
    goals = {"a": "Maybe sort the spare cables", "b": "Help Tavrin finish her slides", "c": SORT,
             "d": "Pick up the zinc refill at the pharmacy", "e": "Errands"}
    labels = {"a": ["plans"], "b": ["work"], "c": ["work", "plans"], "d": ["plans"], "e": ["plans"]}
    for entry_id, goal in goals.items():
        _field_entry(node, entry_id, goal, accomplished=f"{DONE} ({entry_id})")
        _publish(node, entry_id, domains=labels[entry_id])
    search, _state = _node(node, tmp_path, monkeypatch)
    stored = _goals(node)
    assert sorted(g["goal_text"] for g in stored) == sorted(goals.values())
    with _db(node) as conn:
        texts = {r["entry_id"]: r["content"] for r in conn.execute("SELECT entry_id, content FROM journal_entries")}
    released = {}
    for name, make in sorted(GRANT_SHAPES.items()):
        raw = make()
        for goal in stored:
            if _code(search, "user_goals", goal["goal_id"], raw=raw) is not None:
                continue
            entry_id = goal["record_id"]
            released.setdefault(name, []).append(entry_id)
            assert texts[entry_id].split("\n\n", 1)[0] == "Goal: " + goal["goal_text"]
            assert _released_whole(search, entry_id, raw), (name, entry_id)
    assert sorted(released["plain"]) == sorted(goals)
    assert sorted(released["plans-only"]) == ["a", "d", "e"]
    assert "no-option" not in released
    records, _bindings = _search(search, monkeypatch, "spare cables slides pharmacy errands")
    entries = {r["record_id"]: r["content"] for r in _kind(records, "journal_entry")}
    for goal in _kind(records, "goal"):
        (citation,) = goal["citations"]
        assert entries.get(citation["record_id"]) == citation["content"]       # released whole, in the same answer
