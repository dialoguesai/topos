"""Fuzz lane, part 6: the label-free floors on whole generated corpora (design §3.4, §6.2).

Each example builds a real SQLite corpus (tests/permissions_v2/message_search_corpus.py: the
production DDL, real facts, real owner reviews, the protection clock's triggers), computes
the permitted set P(g) of cell C's raw-message grant through the real resolver and the real
decision, then applies a drawn sequence of owner-side events and recomputes P after each:

F1  Every floor event narrows: an owner-only record, a record tombstone, a fact tombstone,
    an entity tombstone, a black hole, a supersession, an edited message (stale review), a
    message no longer from self, a quote marker, an independent copy, an owner-only sibling
    fact, a fact turned owner-only, a revoked review, a deleted message and a review that
    labels an item unknown -- after any one of them, and after any sequence, P is a subset
    of what it was. Nothing an owner does to restrict ever releases a fact that was withheld.
    An event the owner's own store refuses (an unreviewed fact has no review to revoke; a
    fact whose evidence the floor deleted has no snapshot to review against) wrote nothing,
    so P must be exactly what it was, not merely no wider: a refusal that moved it either
    way would be a partial write. The deep profile found both refusals at 500 examples.
F2  Every failure of the floors is a PolicyError: no other exception type ever escapes the
    resolver on any unit of any kind, so nothing can reach a door as an unmapped error.
F3  Unknown withholds through the floors: a review that leaves an item's sensitivity
    unknown, its domains empty, or any floor field unknown withholds the fact, whatever
    the policy would have decided.
F4  Non-vacuity: on a fixed corpus each event removes the fact it targets, so F1 is not
    passing because P was empty.
"""
from __future__ import annotations

import itertools
import json
import sqlite3

import pytest

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

from tests.permissions_v2 import fuzz_support as fz  # noqa: E402
from tests.permissions_v2 import message_search_corpus as mc  # noqa: E402
from tests.permissions_v2.test_evidence import owner  # noqa: E402
from topos.features.facts.store import FactStore  # noqa: E402
from topos.permissions_v2.canonical import PolicyError  # noqa: E402
from topos.permissions_v2.evidence import ReviewedClassification  # noqa: E402
from topos.permissions_v2.identity import ATTESTED_CONTRACT  # noqa: E402
from topos.permissions_v2.registry import parse_policy  # noqa: E402
from topos.permissions_v2.release import source_message_decision  # noqa: E402

pytestmark = [pytest.mark.fuzz]
DOOR = settings(max_examples=fz.examples("door"), deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large])
_counter = itertools.count()
EVENTS = ("owner_only_record", "record_tombstone", "fact_tombstone", "entity_tombstone", "blackhole", "supersede",
          "edit_message", "not_from_self", "quote_marker", "independent_copy", "owner_only_sibling", "fact_owner_only",
          "revoke_review", "delete_message", "unknown_review", "empty_domains_review", "quote_review")
POLICY = parse_policy(mc.p2a_v2_policy())


def permitted(corpus) -> dict:
    """fact_id -> verdict for every unit, through the real floors and the real decision; F2 inline."""
    out = {}
    for unit in corpus.units:
        try:
            evidence, _rows = corpus.resolver.with_qualified(unit.fact_id, reviews=corpus.reviews, contract=ATTESTED_CONTRACT,
                                                             discloses_sources=True, callback=lambda e, r: (e, r))
        except PolicyError as refused:
            out[unit.fact_id] = "withheld:" + refused.code
            continue
        except Exception as error:  # noqa: BLE001 -- F2: this is the finding
            raise AssertionError(f"a non-PolicyError escaped the floors on kind {unit.kind}: {type(error).__name__}") from error
        out[unit.fact_id] = source_message_decision(POLICY, evidence).verdict
    return out


def released(verdicts: dict) -> set:
    return {fact for fact, verdict in verdicts.items() if verdict == "permit"}


def _row(conn, unit):
    return conn.execute("SELECT * FROM signal_objects WHERE object_id=?", (unit.fact_id,)).fetchone()


def apply_event(corpus, event: str, unit) -> bool:
    """One owner-side restriction aimed at `unit` (or the whole node), written as an owner would.

    Returns False when the owner's own store refuses the event because the unit is not in a state that admits it
    (no current review to revoke, or evidence the floor already deleted): a refusal is not an application, and the
    permitted set must be unchanged rather than merely not wider. Only a PolicyError counts as a refusal; anything
    else still escapes, which is F2's finding.
    """
    if event in ("revoke_review", "unknown_review", "empty_domains_review", "quote_review"):
      try:
        with owner():
            current = corpus.reviews._load_current(unit.fact_id)
            if event == "revoke_review":
                corpus.reviews.revoke_review(current.review_id, fact_id=unit.fact_id)
                return True
            snapshot = corpus.resolver.inspect_for_review(unit.fact_id)
            items = []
            for version in snapshot.artifacts + snapshot.leaves:
                fields = dict(evidence=version, domains=["work"], sensitivity="none", subject_entity_ids=["self"],
                              authorship="owner_authored", speech="direct_self_statement", independent_copies="none_known")
                if event == "unknown_review":
                    fields["sensitivity"] = "unknown"
                elif event == "empty_domains_review":
                    fields["domains"] = []
                else:
                    fields["speech"] = "third_party_quote"
                items.append(ReviewedClassification(**fields))
            corpus.reviews.record_review(resolver=corpus.resolver, review_id=f"re-{event}-{next(_counter)}",
                                         expected_snapshot=snapshot, classifications=items, reviewed_at=mc.NOW)
      except PolicyError:
        # The owner's store refused: an unreviewed fact has no review to revoke, and a fact whose evidence the
        # floor deleted has no snapshot to review against. Nothing was written.
        return False
      return True
    with sqlite3.connect(corpus.path) as conn:
        if event == "owner_only_record":
            conn.execute("INSERT OR IGNORE INTO owner_only_records(canonical_table,record_id) VALUES('conversation_messages',?)",
                         (unit.message_id,))
        elif event == "record_tombstone":
            conn.execute("INSERT OR IGNORE INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) VALUES(?,?,?)",
                         (f"excl-record-{unit.message_id}", "record", unit.message_id))
        elif event == "fact_tombstone":
            payload = json.loads(_row(conn, unit)["payload_json"] if isinstance(_row(conn, unit), sqlite3.Row) else
                                 conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?", (unit.fact_id,)).fetchone()[0])
            key = (payload["subject_entity_id"] + ":" + payload["predicate"] + ":" + payload["object_value"]).lower()
            conn.execute("INSERT OR IGNORE INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) VALUES(?,?,?)",
                         (f"excl-fact-{unit.fact_id}", "fact", key))
        elif event == "entity_tombstone":
            conn.execute("INSERT OR IGNORE INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) VALUES(?,?,?)",
                         ("excl-entity-1", "entity", "entity-synthetic-1"))
        elif event == "blackhole":
            conn.execute("INSERT OR IGNORE INTO entity_blackholes(entity_id, canonical_name, normalized_name) "
                         "VALUES('eb-fuzz','Synthetic Person','synthetic person')")
        elif event == "supersede":
            conn.execute("UPDATE signal_objects SET valid_to=? WHERE object_id=?", (mc._iso(mc.NOW - 10), unit.fact_id))
        elif event == "edit_message":
            conn.execute("UPDATE conversation_messages SET content=content || ' edited' WHERE message_id=?", (unit.message_id,))
        elif event == "not_from_self":
            conn.execute("UPDATE conversation_messages SET is_from_self=0 WHERE message_id=?", (unit.message_id,))
        elif event == "quote_marker":
            conn.execute("UPDATE conversation_messages SET metadata_json=? WHERE message_id=?",
                         (json.dumps({"quoted_text": "someone else said this"}), unit.message_id))
        elif event == "independent_copy":
            row = conn.execute("SELECT content, event_at, source_id FROM conversation_messages WHERE message_id=?",
                               (unit.message_id,)).fetchone()
            mc.insert_message(conn, message_id=f"imessage:{7_000_000 + next(_counter)}", source_id=row[2],
                              content=row[0], event_at=row[1])
        elif event == "owner_only_sibling":
            FactStore(conn).assert_fact(subject_entity_id=mc.OWNER_ENTITY, predicate="lives_in",
                                        object_value=f"place fuzz {next(_counter)}", disclosure="owner_only",
                                        source_refs=[{"table": "conversation_messages", "dataset_id": mc.DATASET,
                                                      "source_id": unit.source_id, "record_id": unit.message_id}],
                                        asserted_by="owner")
        elif event == "fact_owner_only":
            payload = json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?",
                                              (unit.fact_id,)).fetchone()[0])
            payload["disclosure"] = "owner_only"
            conn.execute("UPDATE signal_objects SET payload_json=? WHERE object_id=?", (json.dumps(payload), unit.fact_id))
        elif event == "delete_message":
            # This corpus's DDL carries no deleted_at column; the row is removed outright, as a sync would.
            conn.execute("DELETE FROM conversation_messages WHERE message_id=?", (unit.message_id,))
        else:
            raise ValueError(event)
        conn.commit()
    return True


def build(tmp_path, seed: int, counts: dict) -> mc.Corpus:
    return mc.build(tmp_path / f"corpus-{next(_counter)}", seed=seed, counts=counts)


def counts_strategy():
    return st.fixed_dictionaries({name: st.integers(0, 2) for name in mc.KINDS}).filter(lambda c: sum(c.values()) > 0)


@DOOR
@given(st.integers(1, 2**31 - 1), counts_strategy(), st.lists(st.sampled_from(EVENTS), min_size=1, max_size=5), st.data())
def test_F1_every_sequence_of_floor_events_narrows_the_permitted_set(tmp_path, seed, counts, events, data):
    corpus = build(tmp_path, seed, counts)
    before = permitted(corpus)
    history = [released(before)]
    for event in events:
        unit = corpus.units[data.draw(st.integers(0, len(corpus.units) - 1))]
        applied = apply_event(corpus, event, unit)
        now = released(permitted(corpus))
        if applied:
            assert now <= history[-1], (event, unit.kind, sorted(history[-1] - now), sorted(now - history[-1]))
        else:
            # The owner's store refused the event, so nothing was written: the permitted set must be exactly what
            # it was, not merely no wider. A refusal that moved it either way would mean a partial write.
            assert now == history[-1], (event, unit.kind, sorted(history[-1] ^ now))
        history.append(now)


@DOOR
@given(st.integers(1, 2**31 - 1), counts_strategy())
def test_F2_every_unit_of_every_kind_is_decided_or_refused_with_a_policy_error(tmp_path, seed, counts):
    corpus = build(tmp_path, seed, counts)
    verdicts = permitted(corpus)
    assert set(verdicts) == {unit.fact_id for unit in corpus.units}
    for unit in corpus.units:
        verdict = verdicts[unit.fact_id]
        if unit.p2a_release:
            assert verdict == "permit", (unit.kind, verdict)
        else:
            assert verdict != "permit", (unit.kind, verdict)


@DOOR
@given(st.integers(1, 2**31 - 1), st.sampled_from(["unknown_review", "empty_domains_review", "quote_review"]))
def test_F3_a_review_that_leaves_anything_unknown_withholds(tmp_path, seed, event):
    corpus = build(tmp_path, seed, {"clean_positive_C": 2})
    before = permitted(corpus)
    assert released(before) == {unit.fact_id for unit in corpus.units}
    target = corpus.units[0]
    apply_event(corpus, event, target)
    after = permitted(corpus)
    assert after[target.fact_id].startswith("withheld:"), after[target.fact_id]
    assert after[corpus.units[1].fact_id] == "permit"


@pytest.mark.parametrize("event", [e for e in EVENTS if e not in ("entity_tombstone", "blackhole")])
def test_F4_each_targeted_event_removes_exactly_its_fact(tmp_path, event):
    corpus = build(tmp_path, 4242, {"clean_positive_C": 3})
    facts = {unit.fact_id for unit in corpus.units}
    assert released(permitted(corpus)) == facts
    target = corpus.units[1]
    apply_event(corpus, event, target)
    verdicts = permitted(corpus)
    after = released(verdicts)
    assert target.fact_id not in after, event
    if event == "fact_tombstone":
        # Observed 22 Sep 2026: a fact tombstone is a protection event whose prefix (owner:predicate) is in the
        # closure identity of every fact with that predicate, so their reviews go stale together. Narrowing,
        # and an availability cost the design records (F7: positives refuse until re-review), not a leak.
        assert verdicts[target.fact_id] == "withheld:intelligence_excluded", verdicts
        assert all(verdicts[unit.fact_id] in ("permit", "withheld:review_stale") for unit in corpus.units
                   if unit is not target), verdicts
    else:
        assert after == facts - {target.fact_id}, (event, sorted(facts - after))


@pytest.mark.parametrize("event", ["entity_tombstone", "blackhole"])
def test_F4_a_node_wide_floor_removes_everything(tmp_path, event):
    corpus = build(tmp_path, 4243, {"clean_positive_C": 2})
    assert released(permitted(corpus))
    apply_event(corpus, event, corpus.units[0])
    assert released(permitted(corpus)) == set()


@settings(max_examples=fz.examples("pure"), deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet="abcXYZ ,.-_'", min_size=1, max_size=24), st.sampled_from(["writer", "store"]))
def test_F5_a_fact_tombstone_matches_under_both_value_spellings(value, spelling):
    """The exclusion writer stores `strip().lower()`, the FactStore stores the collapsed-whitespace spelling; a
    tombstone written either way vetoes the fact (the battery's `fact_tombstone_value_key_dropped` survived a lane
    whose values never put the two spellings apart)."""
    from hypothesis import assume
    from topos.features.facts.store import _normalize_value
    from topos.permissions_v2.exclusion_floor import fact_excluded
    assume(value.strip())
    stored = value.strip().lower() if spelling == "writer" else _normalize_value(value)
    prefix = "self:works_at"
    payload = {"subject_entity_id": "self", "predicate": "works_at", "object_value": value}
    assert fact_excluded(payload, {prefix + ":" + stored}, {"self"}) is True
    assert fact_excluded(payload, {prefix + ":" + stored + "x"}, {"self"}) is False
