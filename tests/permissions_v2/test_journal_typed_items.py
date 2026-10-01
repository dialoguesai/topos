"""IF-5 Lane B: facts, goals and relationships grounded in the owner's journal entries, cited as records.

protects: `knowledge_projections` grounded typed items in the two message tables only, so on the measured node
the 116 facts and 1,928 goals that cite a journal entry could never reach a grant. Opening a citation path is
exactly where a boundary leaks, so these tests pin what a journal-grounded item must still clear:
  - the grant signs the "Journal entries" option (IF-5 §2 citation scope). Without it the item is withheld as
    `journal_citation_needs_record_option`, even when the entry itself qualifies, and even beside a message;
  - the entry clears everything a journal member clears (the owner proof, the NSFW hard withhold, owner-only,
    Off-limits over every column, the machine assessment) and is inside the window by every instant its stated
    day can denote;
  - the item cites the entry as a record: the raw member's opaque id, its whole text, its one source, and a time
    never finer than the stated day;
  - a citation of a same-source twin resolves to the member, and the twin's own vetoes still apply;
  - with the family off, a journal citation is unsupported exactly as before;
  - the census's grounding count (`od46_journal_grounding`) and its mirror of the build agree with the engine.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_journal_family import (  # noqa: F401 (node is a fixture)
    AFTER_ITS_DAY, DATASET, OWNER, RESOURCE, SOURCE, _db, _entry, _journal_policy, _labels, _resolver, node, owner)
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.evidence_families import JOURNAL_FLAG

SELF = "owner-entity"
EXPORT = "chatgpt_file_ingestion"
DAY = 1788998400                      # 2026-09-10T00:00:00Z, the default entry's stated day
ATLAS = "I work on Atlas."            # states works_on "Atlas" under the fullmatch floor
GOAL = "finish the compiler by Friday"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"


def _script(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


# --- fixture ----------------------------------------------------------------------------------------

def _attest_owner(path):
    """OD-29: the owner attested one self entity, the subject an owner fact may be about."""
    from tests.permissions_v2.test_owner_identity_binding import add_entity, do_attest
    with _db(path) as conn:
        add_entity(conn, SELF)
        do_attest(conn, SELF)


def _fact(path, refs, *, predicate="works_on", value="Atlas"):
    from topos.features.facts.store import FactStore
    with _db(path) as conn:
        FactStore(conn).assert_fact(subject_entity_id=SELF, predicate=predicate, object_value=value, confidence=1,
                                    source_refs=refs, disclosure="owner_only", asserted_by="owner")
        return conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact' ORDER BY rowid DESC"
                            ).fetchone()[0]


def _cites(entry_id, shape="record_id"):
    """A fact's journal reference (IF-5 W6): `record_id` + source, or a rule-extractor object's bare `id`."""
    if shape == "id":
        return [{"table": "journal_entries", "id": entry_id}]
    return [{"table": "journal_entries", "record_id": entry_id, "source_id": SOURCE}]


def _goal(path, entry_id, text=GOAL, *, goal_id="goal-1", edge=False):
    from tests.permissions_v2.test_owner_identity_binding import add_entity
    with _db(path) as conn:
        conn.execute("INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
                     "VALUES (?,?,?,?,?)", (goal_id, entry_id, SOURCE, text, "{}"))
        if edge:
            add_entity(conn, "goal-node", is_self=0, entity_type="goal")
            conn.execute("UPDATE entities SET canonical_name=?, normalized_name=? WHERE entity_id='goal-node'",
                         (text, text))
            conn.execute("INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type, metadata_json) "
                         "VALUES ('edge-1', ?, 'goal-node', 'pursues', ?)",
                         (SELF, json.dumps({"source_object_id": goal_id, "actor_role": "authored"})))
    return goal_id


def _publish(path, record_id, *, table="journal_entries", source=SOURCE, **labels):
    """The machine assessment the automatic review would publish, with ordinary labels unless told otherwise."""
    from topos.permissions_v2 import automatic_message_review as amr
    resolver = _resolver(path)
    with owner():
        reviews = EvidenceReviewStore(path.parent / "reviews.db", resolver=resolver)
        prepared = amr.prepare(resolver, reviews, resolver._identity(table, record_id, source))
        amr.publish(resolver, reviews, prepared, _labels(prepared, **{"domains": ["work"], **labels}), now=1)
    return resolver, reviews


def _restrict(path, table, record_id):
    with _db(path) as conn:
        conn.execute("INSERT INTO owner_only_records (canonical_table, record_id, created_at, updated_at) "
                     "VALUES (?,?,'t','t')", (table, record_id))


def _off_limits(path, name="Quillon Marsh"):
    from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
    with _db(path) as conn:
        apply_entity_blackhole_v1_up(conn)
        conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, "
                     "aliases_json, created_at) VALUES ('b1','',?,?,'[]','t')", (name.lower(), name))


def _node(path, tmp_path, monkeypatch, *, now=AFTER_ITS_DAY, grant=_journal_policy, **policy):
    """A node over a signed knowledge grant (`grant(**policy)`, signed at `now`), its index built."""
    from tests.permissions_v2 import message_search_corpus as mc
    from tests.permissions_v2.message_search_harness import Node
    resolver = _resolver(path)
    with owner():
        reviews = EvidenceReviewStore(path.parent / "reviews.db", resolver=resolver)
    monkeypatch.setattr(mc, "NOW", now)
    search = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), path.parent / "search-node",
                  model=None, search_raw=grant(**policy), now=now)
    with owner():
        state = search.index.rebuild("grant-search", now=now)
    return search, state


def _search(search, monkeypatch, query):
    """The released records and the member bindings the node signed for them."""
    signed = []
    walk = search.search._walk

    def spy(*args, **kwargs):
        result = walk(*args, **kwargs)
        signed.append(result[3])
        return result
    monkeypatch.setattr(search.search, "_walk", spy)
    output, refused = search.search_request(query, k=10)
    assert refused is None
    return output["records"], dict(zip((r["record_id"] for r in output["records"]), signed[-1]))


def _code(search, table, record_id, raw=None):
    """`qualify_projection` on its own, as the build and the release call it: None when it qualifies, else its code."""
    from topos.permissions_v2.knowledge_projections import qualify_projection
    from topos.permissions_v2.registry import parse_policy
    policy = parse_policy(raw or search.search_raw)
    now = search.now[0]
    lower, upper = (now - policy.search.window.max_age_seconds) * 10**6, now * 10**6
    resolver, reviews = search.corpus.resolver, search.corpus.reviews
    with resolver._read() as (conn, floor), reviews._db() as db:
        try:
            qualify_projection(resolver, conn, floor, reviews, db, table, record_id, policy, lower, upper)
        except PolicyError as exc:
            return exc.code
    return None


def _kind(records, kind):
    return [record for record in records if record["kind"] == kind]


# --- a journal-grounded fact --------------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["record_id", "id"])
def test_a_journal_grounded_fact_releases_citing_the_entry_as_a_record(node, tmp_path, monkeypatch, shape):
    _attest_owner(node)
    _entry(node, "e1", ATLAS)
    fact = _fact(node, _cites("e1", shape))
    _publish(node, "e1")
    search, state = _node(node, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 2}            # the entry, and the fact it grounds
    records, bindings = _search(search, monkeypatch, "Atlas")
    (entry,) = _kind(records, "journal_entry")
    (item,) = _kind(records, "fact")
    assert (item["content"], item["assertion"], item["source_ids"]) == \
        ("Owner works on Atlas.", "owner_stated", [SOURCE])
    # The citation is the entry as a record: the raw member's own opaque id, its whole text, its one source.
    assert item["citations"] == [dict(record_id=entry["record_id"], source_id=SOURCE, content=ATLAS)]
    assert item["event_at"] is None                                   # the grant releases no time
    binding = bindings[item["record_id"]]
    assert (binding["kind"], binding["evidence_tables"], binding["source_ids"]) == \
        ("fact", ["journal_entries"], [SOURCE])
    assert fact not in json.dumps(records)                            # no canonical id leaves


@pytest.mark.parametrize("precision, expected", [("day", DAY), ("second", None), ("none", None)])
def test_a_journal_grounded_item_is_dated_at_most_by_the_stated_day(node, tmp_path, monkeypatch, precision, expected):
    _attest_owner(node)
    _entry(node, "e1", ATLAS)
    _fact(node, _cites("e1"))
    _publish(node, "e1")
    search, _state = _node(node, tmp_path, monkeypatch, precision=precision)
    records, _bindings = _search(search, monkeypatch, "Atlas")
    (item,) = _kind(records, "fact")
    assert item["event_at"] == expected


def test_without_the_journal_option_a_journal_grounded_item_is_withheld(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", ATLAS)
    fact = _fact(node, _cites("e1"))
    _publish(node, "e1")
    base = ("message", "fact", "goal", "relationship")
    search, state = _node(node, tmp_path, monkeypatch, kinds=base)
    assert state["member_count"] == 0
    records, _bindings = _search(search, monkeypatch, "Atlas")
    assert records == []
    # The entry itself qualifies: the same item under a grant that signs the option is released.
    assert _code(search, "signal_objects", fact) == "journal_citation_needs_record_option"
    assert _code(search, "signal_objects", fact, raw=_journal_policy()) is None


def test_the_option_is_checked_where_the_citation_is_resolved(node, tmp_path, monkeypatch):
    """Guard independence: the build drops raw journal members without the option, so an end-to-end test
    cannot see the projection's own check. `_support` must refuse a journal citation on its own."""
    from topos.permissions_v2.knowledge_projections import _support
    from topos.permissions_v2.registry import parse_policy
    _attest_owner(node)
    _entry(node, "e1", ATLAS)
    _publish(node, "e1")
    search, _state = _node(node, tmp_path, monkeypatch)
    resolver, reviews = search.corpus.resolver, search.corpus.reviews
    now = search.now[0]
    lower, upper = (now - 90 * 86_400) * 10**6, now * 10**6
    for kinds, expected in ((("message", "fact", "goal", "relationship"), "journal_citation_needs_record_option"),
                            (("fact", "journal_entry"), None)):
        policy = parse_policy(_journal_policy(kinds=kinds))
        with resolver._read() as (conn, floor), reviews._db() as db:
            try:
                sources, _clause = _support(resolver, conn, floor, reviews, db, _cites("e1"), policy, lower, upper)
                code = None
            except PolicyError as exc:
                code = exc.code
        assert code == expected
    assert [q.snapshot.message.identity.table for q, _rows in sources] == ["journal_entries"]


# --- every journal guard still applies -----------------------------------------------------------------

def _grounded(path):
    _attest_owner(path)
    _entry(path, "e1", ATLAS)
    fact = _fact(path, _cites("e1"))
    _publish(path, "e1")
    return fact


def test_an_entry_flagged_nsfw_after_indexing_withholds_the_item(node, tmp_path, monkeypatch):
    fact = _grounded(node)
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _kind(_search(search, monkeypatch, "Atlas")[0], "fact")
    with _db(node) as conn:
        conn.execute("UPDATE journal_entries SET content_nsfw=1 WHERE entry_id='e1'")
    assert _code(search, "signal_objects", fact) == "unsupported_message_content"
    output, refused = search.search_request("Atlas", k=10)
    assert refused is not None or output["records"] == []


def test_the_projection_holds_a_journal_citation_to_the_nsfw_withhold_on_its_own(node, tmp_path, monkeypatch):
    """Defence in depth: with the family's own source check disabled, the citation's NSFW flag still withholds."""
    from topos.permissions_v2 import message_evidence
    monkeypatch.setattr(message_evidence, "_journal_source_checks", lambda *args, **kwargs: None)
    _attest_owner(node)
    _entry(node, "e1", ATLAS, content_nsfw=1)
    fact = _fact(node, _cites("e1"))
    _publish(node, "e1")                       # assessable only because the family check is disabled
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "unsupported_message_content"


def test_an_off_limits_name_in_any_column_of_the_entry_withholds_the_item(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", ATLAS, people="Quillon Marsh")          # the text is clean; the people column is not
    fact = _fact(node, _cites("e1"))
    _publish(node, "e1")
    _off_limits(node)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 0
    assert _code(search, "signal_objects", fact) == "entity_protected"
    assert _search(search, monkeypatch, "Atlas")[0] == []


@pytest.mark.parametrize("restricted", ["entry", "fact"])
def test_owner_only_on_the_entry_or_the_fact_withholds_the_item(node, tmp_path, monkeypatch, restricted):
    fact = _grounded(node)
    _restrict(node, *(("journal_entries", "e1") if restricted == "entry" else ("signal_objects", fact)))
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "owner_only"
    assert _kind(_search(search, monkeypatch, "Atlas")[0], "fact") == []


def test_an_entry_the_owner_never_proved_withholds_the_item(node, tmp_path, monkeypatch):
    fact = _grounded(node)
    with _db(node) as conn:   # now written through the relay: nothing binds it to the owner
        conn.execute("UPDATE journal_entries SET writer_class='cp_relay' WHERE entry_id='e1'")
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 0
    assert _code(search, "signal_objects", fact) == "journal_owner_unproven"


def test_an_unassessed_or_special_entry_withholds_the_item(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", ATLAS)
    _entry(node, "e2", "I work on Contoso.")
    unassessed, special = _fact(node, _cites("e1")), _fact(node, _cites("e2"), value="Contoso")
    _publish(node, "e2", sensitivity="special")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", unassessed) == "machine_review_required"
    assert _code(search, "signal_objects", special) == "evidence_not_permitted"


@pytest.mark.parametrize("now, entry_at", [
    (AFTER_ITS_DAY - 120, "2026-09-10T08:30:00"),   # the stated day has not ended everywhere yet
    (AFTER_ITS_DAY, "2026-06-01T08:30:00"),         # older than the grant's 90 days
])
def test_an_entry_outside_the_window_withholds_the_item(node, tmp_path, monkeypatch, now, entry_at):
    _attest_owner(node)
    _entry(node, "e1", ATLAS, entry_at=entry_at)
    fact = _fact(node, _cites("e1"))
    _publish(node, "e1")
    search, state = _node(node, tmp_path, monkeypatch, now=now)
    assert state["member_count"] == 0
    assert _code(search, "signal_objects", fact) == "evidence_outside_window"


def test_an_entry_whose_time_says_nothing_withholds_the_item(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", ATLAS, entry_at="sometime")
    fact = _fact(node, _cites("e1"))
    _publish(node, "e1")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "journal_time_unknown"


@pytest.mark.parametrize("content", ["Maybe I work on Atlas.", "I do not work on Atlas.", "I work on Atlas?"])
def test_the_grounding_floor_runs_on_the_entrys_text(node, tmp_path, monkeypatch, content):
    _attest_owner(node)
    _entry(node, "e1", content)
    fact = _fact(node, _cites("e1"))
    _publish(node, "e1")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "fact_not_grounded"


def test_with_the_family_off_a_journal_citation_is_unsupported_as_before(node, tmp_path, monkeypatch):
    fact = _grounded(node)
    by_id = _fact(node, _cites("e1", "id"), value="Atlas Two")
    goal = _goal(node, "e1", "work on Atlas")
    search, _state = _node(node, tmp_path, monkeypatch)
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)
    assert _code(search, "signal_objects", fact) == "lineage_unsupported"
    assert _code(search, "signal_objects", by_id) == "lineage_identity_incomplete"
    assert _code(search, "user_goals", goal) == "lineage_identity_ambiguous"


# --- references: exactly one row, or a refusal ------------------------------------------------------------

@pytest.mark.parametrize("ref", [
    {"table": "journal_entries", "record_id": "e1", "source_id": SOURCE},
    {"table": "journal_entries", "record_id": "e1"},                     # the source fills from the one row
    {"table": "journal_entries", "id": "e1"},                            # a rule-extractor object
    {"table": "journal_entries", "record_id": "e1", "id": "e1"},
])
def test_a_journal_reference_resolves_to_its_one_row(node, ref):
    from topos.permissions_v2.knowledge_projections import resolve_reference
    _entry(node, "e1", ATLAS)
    resolver = _resolver(node)
    with resolver._read() as (conn, _floor):
        identity = resolve_reference(resolver, conn, ref)
    assert (identity.table, identity.record_id, identity.source_id, identity.dataset_id) == \
        ("journal_entries", "e1", SOURCE, None)


@pytest.mark.parametrize("ref, code", [
    ({"table": "journal_entries"}, "lineage_identity_incomplete"),
    ({"table": "journal_entries", "record_id": ["e1"]}, "lineage_identity_incomplete"),
    ({"table": "journal_entries", "id": 7}, "lineage_identity_incomplete"),
    ({"table": "journal_entries", "record_id": "e1", "dataset_id": DATASET}, "lineage_identity_incomplete"),
    ({"table": "journal_entries", "record_id": "e1", "id": "e2"}, "lineage_identity_ambiguous"),
    ({"table": "journal_entries", "record_id": "e1", "source_id": "other_journal"}, "lineage_identity_ambiguous"),
    ({"table": "journal_entries", "record_id": "missing"}, "lineage_identity_ambiguous"),
])
def test_a_journal_reference_that_names_no_single_row_refuses(node, ref, code):
    from topos.permissions_v2.knowledge_projections import resolve_reference
    _entry(node, "e1", ATLAS)
    _entry(node, "e2", "Another synthetic entry.")
    resolver = _resolver(node)
    with resolver._read() as (conn, _floor), pytest.raises(PolicyError) as refused:
        resolve_reference(resolver, conn, ref)
    assert refused.value.code == code


# --- copies: a twin resolves to the member, and still vetoes ------------------------------------------

def test_a_citation_of_a_same_source_twin_resolves_to_the_member(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", ATLAS)
    _entry(node, "e2", ATLAS, entry_at="2026-09-10T09:30:00")     # a later re-push of the same words: an alias
    _fact(node, _cites("e2"))
    _publish(node, "e1")
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 2
    records, bindings = _search(search, monkeypatch, "Atlas")
    (entry,) = _kind(records, "journal_entry")
    (item,) = _kind(records, "fact")
    assert item["citations"] == [dict(record_id=entry["record_id"], source_id=SOURCE, content=ATLAS)]
    assert bindings[item["record_id"]]["evidence_tables"] == ["journal_entries"]


def test_a_member_and_its_twin_cited_together_are_one_citation(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", ATLAS)
    _entry(node, "e2", ATLAS, entry_at="2026-09-10T09:30:00")
    _fact(node, _cites("e1") + _cites("e2", "id"))
    _publish(node, "e1")
    search, _state = _node(node, tmp_path, monkeypatch)
    (item,) = _kind(_search(search, monkeypatch, "Atlas")[0], "fact")
    assert len(item["citations"]) == 1


@pytest.mark.parametrize("veto, code", [
    ("owner_only", "owner_only"),
    ("off_limits", "entity_protected"),
    ("nsfw", "unsupported_message_content"),
    ("excluded", "intelligence_excluded"),
])
def test_the_cited_twin_still_vetoes(node, tmp_path, monkeypatch, veto, code):
    _attest_owner(node)
    _entry(node, "e1", ATLAS)
    _entry(node, "e2", ATLAS, entry_at="2026-09-10T09:30:00",
           **({"people": "Quillon Marsh"} if veto == "off_limits" else {"content_nsfw": 1} if veto == "nsfw" else {}))
    fact = _fact(node, _cites("e2"))
    _publish(node, "e1")
    if veto == "owner_only":
        _restrict(node, "journal_entries", "e2")
    elif veto == "off_limits":
        _off_limits(node)
    elif veto == "excluded":
        with _db(node) as conn:
            conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key, created_at) "
                         "VALUES ('x1','record','e2','t')")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == code


def test_a_twin_in_another_source_withholds_every_copy(node, tmp_path, monkeypatch):
    fact = _grounded(node)
    _entry(node, "x1", ATLAS, source="other_journal", dataset=f"{OWNER}:x:y")
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "signal_objects", fact) == "independent_copy_lineage"


# --- goals, and the relationship that follows one -------------------------------------------------------

def test_a_goal_grounded_in_an_entry_and_its_relationship_release(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", "My goal is to finish the compiler by Friday.")
    _goal(node, "e1", edge=True)
    _publish(node, "e1", domains=["work", "plans"])
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 3
    records, bindings = _search(search, monkeypatch, "compiler Friday")
    (entry,) = _kind(records, "journal_entry")
    (goal,) = _kind(records, "goal")
    (edge,) = _kind(records, "relationship")
    citation = [dict(record_id=entry["record_id"], source_id=SOURCE, content=entry["content"])]
    assert (goal["content"], goal["status"], goal["citations"]) == (GOAL, "stated_intention", citation)
    assert (edge["subject"], edge["relation"], edge["object"], edge["citations"]) == \
        ("Owner", "pursues", GOAL, citation)
    assert {bindings[r["record_id"]]["kind"]: bindings[r["record_id"]]["evidence_tables"] for r in (goal, edge)} == \
        {"goal": ["journal_entries"], "relationship": ["journal_entries"]}
    assert "goal-node" not in json.dumps(records)


def test_without_the_option_neither_the_goal_nor_its_relationship_releases(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", "My goal is to finish the compiler by Friday.")
    goal = _goal(node, "e1", edge=True)
    _publish(node, "e1", domains=["work", "plans"])
    search, state = _node(node, tmp_path, monkeypatch, kinds=("message", "fact", "goal", "relationship"))
    assert state["member_count"] == 0
    assert _code(search, "user_goals", goal) == _code(search, "entity_edges", "edge-1") == \
        "journal_citation_needs_record_option"


def test_a_goal_naming_a_twin_resolves_to_the_member(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", "My goal is to finish the compiler by Friday.")
    _entry(node, "e2", "My goal is to finish the compiler by Friday.", entry_at="2026-09-10T10:00:00")
    _goal(node, "e2")
    _publish(node, "e1", domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    records, _bindings = _search(search, monkeypatch, "compiler Friday")
    (entry,) = _kind(records, "journal_entry")
    (goal,) = _kind(records, "goal")
    assert [c["record_id"] for c in goal["citations"]] == [entry["record_id"]]


def test_a_goal_the_entry_does_not_state_is_withheld(node, tmp_path, monkeypatch):
    _attest_owner(node)
    _entry(node, "e1", "Spent the morning on the compiler, Friday looks tight.")
    goal = _goal(node, "e1")
    _publish(node, "e1", domains=["work", "plans"])
    search, _state = _node(node, tmp_path, monkeypatch)
    assert _code(search, "user_goals", goal) == "goal_not_grounded"


# --- a fact grounded in a message and a journal entry -------------------------------------------------------

def _export_message(path, message_id, content, *, event_at="2026-09-09T10:00:00Z"):
    """The owner's ChatGPT export, imported through the owner's own door (owner_import, the install's dataset)."""
    with _db(path) as conn:
        conn.execute("INSERT INTO source_runtime_installs (install_id, scope_key, source_id, version_id, status, "
                     "is_active, source_definition_json) VALUES ('install-export',?,?,'v1','active',1,?)",
                     (json.dumps({"user_id": OWNER, "topos_id": RESOURCE, "device_id": "*", "dataset_id": DATASET}),
                      EXPORT, json.dumps({"source_id": EXPORT})))
        conn.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, "
                     "updated_at) VALUES ('chatgpt:thread-1',?,NULL,?,'2026-09-01','2026-09-01')", (OWNER, EXPORT))
        conn.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, "
                     "source_id, writer_class, writer_dataset_id) "
                     "VALUES (?,'chatgpt:thread-1','user',?,?,?,'owner_import',?)",
                     (message_id, event_at, content, EXPORT, DATASET))


def _mixed_policy(**policy):
    raw = _journal_policy(**policy)
    tables = ["ai_chat_messages", "conversation_messages", "journal_entries"]
    (rule,) = raw["rules"]
    rule["evidence_use"]["sources"]["values"] = [EXPORT, SOURCE]
    for form in rule["release"]["forms"]:
        form["tables"] = tables
    raw["source_universe"]["source_ids"] = [*raw["source_universe"]["source_ids"], EXPORT]
    raw["search"]["tables"] = tables
    return raw


def _mixed(path):
    _attest_owner(path)
    _entry(path, "e1", ATLAS)
    _export_message(path, "x-1", "I am working on Atlas at work.")
    fact = _fact(path, [{"table": "ai_chat_messages", "record_id": "x-1", "source_id": EXPORT}, *_cites("e1")])
    _publish(path, "e1")
    _publish(path, "x-1", table="ai_chat_messages", source=EXPORT)
    return fact


@pytest.mark.parametrize("precision, expected", [("day", DAY - 86_400), ("second", None)])
def test_a_fact_grounded_in_a_message_and_an_entry_cites_both(node, tmp_path, monkeypatch, precision, expected):
    _mixed(node)
    search, _state = _node(node, tmp_path, monkeypatch, grant=_mixed_policy, precision=precision)
    records, bindings = _search(search, monkeypatch, "Atlas")
    (entry,) = _kind(records, "journal_entry")
    (message,) = _kind(records, "message")
    (item,) = _kind(records, "fact")
    assert item["source_ids"] == sorted([EXPORT, SOURCE])
    assert sorted(item["citations"], key=lambda c: c["source_id"]) == [
        dict(record_id=message["record_id"], source_id=EXPORT, content="I am working on Atlas at work."),
        dict(record_id=entry["record_id"], source_id=SOURCE, content=ATLAS)]
    assert bindings[item["record_id"]]["evidence_tables"] == ["ai_chat_messages", "journal_entries"]
    # The earliest source dates the item; an entry's stated day can date nothing at `second`.
    assert item["event_at"] == expected


def test_beside_a_message_the_journal_citation_still_needs_the_option(node, tmp_path, monkeypatch):
    fact = _mixed(node)
    search, _state = _node(node, tmp_path, monkeypatch, grant=_mixed_policy,
                           kinds=("message", "fact", "goal", "relationship"))
    records, _bindings = _search(search, monkeypatch, "Atlas")
    assert [r["kind"] for r in records] == ["message"]
    assert _code(search, "signal_objects", fact) == "journal_citation_needs_record_option"


# --- the census agrees ----------------------------------------------------------------------------------------

def _census_copy(path, tmp_path, now):
    """A closed census copy of the fixture node with the manifest the measurement scripts read."""
    copy = tmp_path / "census-copy"
    copy.mkdir()
    source, target = sqlite3.connect(path), sqlite3.connect(copy / "database.db")
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        source.close()
        target.close()
    (copy / "census-copy-manifest.json").write_text(json.dumps(
        {"consistency": {"consistent": True}, "copied_at": now, "run_id": "synthetic", "copied_at_utc": "synthetic"}))
    return copy


def test_the_census_grounding_count_and_the_engine_agree_on_a_fixture(node, tmp_path, monkeypatch):
    """od46_journal_grounding's node rule (a: fullmatch over the whole entry, with its gates) against what the
    engine releases at the grant's 90 days, on entries the two must judge alike. Each withheld item is withheld
    by the engine for the reason the census gates it."""
    _attest_owner(node)
    _entry(node, "e-fact", ATLAS)
    _entry(node, "e-goal", "My goal is to finish the compiler by Friday.")
    _entry(node, "e-hedged", "Maybe I work on Contoso.")
    _entry(node, "e-nsfw", "I work on Fabrikam.", content_nsfw=1)
    _entry(node, "e-owner-only", "I work on Northwind.")
    _entry(node, "e-old", "I work on Kestrel.", entry_at="2026-06-01T08:30:00")
    _entry(node, "e-off-limits", "I work on Orion.", people="Quillon Marsh")
    _entry(node, "e-paraphrase", "Spent the morning on the compiler, the parser is next.")
    facts = {value: _fact(node, _cites(entry), value=value) for entry, value in (
        ("e-fact", "Atlas"), ("e-hedged", "Contoso"), ("e-nsfw", "Fabrikam"), ("e-owner-only", "Northwind"),
        ("e-old", "Kestrel"), ("e-off-limits", "Orion"))}
    goals = {"stated": _goal(node, "e-goal"), "paraphrased": _goal(node, "e-paraphrase", "ship the parser",
                                                                    goal_id="goal-2")}
    # Off-limits first: a name added later moves every entry's assessment context, which would stale them all.
    # The Off-limits and NSFW entries cannot be assessed at all; the others are, as the automatic review would.
    _off_limits(node)
    for entry in ("e-fact", "e-goal", "e-hedged", "e-owner-only", "e-old", "e-paraphrase"):
        _publish(node, entry, domains=["work", "plans"])
    _restrict(node, "journal_entries", "e-owner-only")
    search, _state = _node(node, tmp_path, monkeypatch)
    query = "Atlas compiler Friday Contoso Fabrikam Northwind Kestrel Orion parser"
    records, _bindings = _search(search, monkeypatch, query)
    engine = {"fact": len(_kind(records, "fact")), "goal": len(_kind(records, "goal"))}
    assert engine == {"fact": 1, "goal": 1}
    assert {value: _code(search, "signal_objects", fact) for value, fact in facts.items()} == {
        "Atlas": None, "Contoso": "fact_not_grounded", "Fabrikam": "unsupported_message_content",
        "Northwind": "owner_only", "Kestrel": "evidence_outside_window", "Orion": "entity_protected"}
    assert {name: _code(search, "user_goals", goal) for name, goal in goals.items()} == {
        "stated": None, "paraphrased": "goal_not_grounded"}

    measured = _script("od46_journal_grounding").measure(_census_copy(node, tmp_path, search.now[0]))
    window = measured["by_window"]
    assert {family: window[family]["90d"]["(a) fullmatch_whole_entry"] for family in ("fact", "goal")} == engine
    # The census saw every cited pair in the window and gated the same ones out.
    assert window["fact"]["90d"]["cited_in_window"] == 5 and window["fact"]["365d"]["cited_in_window"] == 6
    assert window["fact"]["90d"]["support_ok"] == 2                      # Atlas, and the hedged Contoso


def test_the_census_mirror_builds_a_journal_grounded_member_as_the_node_does(node, tmp_path, monkeypatch):
    """grant_census.run mirrors `_rebuild_once`'s typed loop; its rank time is the build's own call. A fact
    grounded in a walked message and a journal entry is a census member with the index member's id and time."""
    gc = _script("grant_census")
    _mixed(node)              # no native provenance store on this node: its export prompt is door-proven
    search, _state = _node(node, tmp_path, monkeypatch, grant=_mixed_policy, precision="day")
    resolver = search.index.resolver
    census = gc.run(canonical=Path(resolver.path), reviews=Path(search.index.reviews.path), ledger=search.ledger.path,
                    index_root=search.index.root, keys=search.index.root / "keys.db", binding=resolver.binding,
                    live_canonical=None, now=search.now[0])
    (typed,) = [o for o in census.members.values() if o.family == "fact"]
    live = census.index["members"][typed.opaque_id]
    assert typed.event_us == live["event_us"] == (DAY - 86_400 + 10 * 3600) * 1_000_000   # the message's instant
    assert set(typed.evidence) == {("ai_chat_messages", EXPORT), ("journal_entries", SOURCE)}
    # The census does not walk journal entries yet (WS1): the raw entry is the one index member it cannot explain.
    comparison = gc.compare_index(census)
    assert (comparison["census_only"], comparison["index_only"]) == (0, 1)
