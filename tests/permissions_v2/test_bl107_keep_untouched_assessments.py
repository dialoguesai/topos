"""BL-107, the owner's decision of 8 Oct 2026: an Off-limits change keeps the assessments it does not touch.

Until 1.5.1 every row of the Off-limits list was folded into the revision of every item's assessment (the message
snapshot's `protection_revision`, and the protected vocabulary in a machine review's context revision), so ONE new
entry put every assessment on the node out of date, whatever it reached: every share was empty until the node had
assessed everything again (BL-101 measured it: no member for 85 to 90 s on the rig with an instant labeller, one model
call per item on a real node, 500 a pass). Now the list is folded in only for an item it reaches.

"Touched" (`message_evidence.touched_by_off_limits`, `automatic_message_review._bound_terms`): the share boundary's own
veto of the item, over every entry (carried and waiting ones included), the same check that withholds it at every
qualification (`_floors`): its text by every name, part, form and handle of every entry and its closure (the linked
entity's aliases and learned spellings, merges, contacts and their handles), the records a protected person is
mentioned in, and for a message its conversation, roster and replies; for the context revision also a protected term
in any neighbour the classifier is shown. What cannot be decided is touched.

protects:
  - an entry that reaches nothing keeps an owner's review current (no re-assessment);
  - an entry that reaches the item withdraws its assessment (the revision moves) and the item is withheld meanwhile,
    so it is never served from an assessment made before the entry;
  - a boundary that cannot be read counts as touching (the 1.5.0 rule);
  - an assessment stored under the 1.5.0 revision stays current while the list has not changed since, and not after.
Invented data only.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_direct_message_evidence import qualify, setup
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import message_evidence
from topos.permissions_v2.automatic_message_review import context_for
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.message_evidence import current_snapshot, message_key, snapshot_message

pytestmark = pytest.mark.public

UNRELATED = "Unrelated Private Person"
IN_THE_MESSAGE = "Synthetic message"          # the fixture's message says it


def conversation_tables(legacy):
    from topos.storage.canonical.conversations_tables import (ensure_contact_identifiers_table, ensure_contacts_table,
        ensure_conversation_participants_table, ensure_conversations_table)
    conn = legacy[1]
    for create in (ensure_contacts_table, ensure_contact_identifiers_table, ensure_conversations_table,
                   ensure_conversation_participants_table):
        create(conn)
    conversation = conn.execute("SELECT conversation_id FROM conversation_messages").fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO conversations(conversation_id,dataset_id,source_id) VALUES(?,'native-dataset','imessage')",
                 (conversation,))
    conn.commit()


def add_entry(legacy, name, blackhole_id):
    conn = legacy[1]
    conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                 "VALUES(?,'',?,?,'complete')", (blackhole_id, name, name.lower()))
    conn.commit()


def remove_entry(legacy, blackhole_id):
    legacy[1].execute("DELETE FROM entity_blackholes WHERE blackhole_id=?", (blackhole_id,))
    legacy[1].commit()


def snapshot(resolver, identity):
    with resolver._read() as (conn, floor):
        return snapshot_message(resolver, conn, floor, identity)[0]


def context_revision(resolver, identity):
    with resolver._read() as (conn, _floor):
        row = resolver._load(conn, identity)
        return context_for(conn, identity, row, boundary=resolver.entity_boundary(conn))[0]


def stored(reviews, identity):
    return reviews._load_current(message_key(identity))


def test_an_entry_that_reaches_nothing_keeps_the_owners_assessment(legacy):
    """Rule: the list is left out of an untouched item's revision. Fold it in again (1.5.0) and this is `review_stale`
    and the owner, or the node's labeller, must assess the message again."""
    resolver, reviews, identity = setup(legacy)
    conversation_tables(legacy)
    before, context_before = snapshot(resolver, identity), context_revision(resolver, identity)
    add_entry(legacy, UNRELATED, "entry-1")
    assert snapshot(resolver, identity) == before and context_revision(resolver, identity) == context_before
    assert qualify(resolver, reviews, identity)
    add_entry(legacy, "Another Stranger", "entry-2")                       # a second, a removal: still kept
    remove_entry(legacy, "entry-1")
    assert qualify(resolver, reviews, identity)


def test_an_entry_that_reaches_the_message_withdraws_its_assessment_and_withholds_it(legacy):
    """Rule: `touched_by_off_limits`. Take it out (every item untouched) and the revision does not move: the assessment
    made before the entry would still be current for a message the entry reaches."""
    resolver, reviews, identity = setup(legacy)
    conversation_tables(legacy)
    before, context_before = snapshot(resolver, identity), context_revision(resolver, identity)
    add_entry(legacy, IN_THE_MESSAGE, "entry-1")
    after = snapshot(resolver, identity)
    assert after.message == before.message and after.protection_revision != before.protection_revision
    assert context_revision(resolver, identity) != context_before
    with resolver._read() as (conn, floor):
        assert not current_snapshot(stored(reviews, identity).snapshot, after, resolver, conn, floor)
    with pytest.raises(PolicyError, match="entity_protected"):
        qualify(resolver, reviews, identity)                               # never served meanwhile


def test_a_boundary_that_cannot_be_read_counts_as_touching(legacy, monkeypatch):
    resolver, reviews, identity = setup(legacy)
    conversation_tables(legacy)
    real = resolver.entity_boundary

    class Broken:
        def __init__(self, inner):
            self.inner = inner

        def observe(self, **_kwargs):
            raise PolicyError("entity_protection_lineage_unavailable")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    add_entry(legacy, UNRELATED, "entry-1")
    monkeypatch.setattr(resolver, "entity_boundary", lambda conn: Broken(real(conn)))
    with resolver._read() as (conn, _floor):
        assert message_evidence.touched_by_off_limits(resolver, conn, identity, resolver._load(conn, identity))


def test_an_assessment_stored_under_the_150_revision_is_kept_while_the_list_is_unchanged(legacy, monkeypatch):
    """The upgrade: a review the owner recorded on 1.5.0 while an entry existed carries the whole list. It stays
    current under 1.5.1 until the list changes; the first change after the upgrade then puts it out of date once
    (its revision cannot say which entries it saw), and the next assessment is kept from then on."""
    conversation_tables(legacy)
    add_entry(legacy, UNRELATED, "entry-1")
    monkeypatch.setattr(message_evidence, "touched_by_off_limits", lambda *args: True)   # record as 1.5.0 did
    resolver, reviews, identity = setup(legacy)
    monkeypatch.undo()
    assert snapshot(resolver, identity) != stored(reviews, identity).snapshot
    assert qualify(resolver, reviews, identity)                            # current under its 1.5.0 revision
    add_entry(legacy, "Another Stranger", "entry-2")
    with pytest.raises(PolicyError, match="review_stale"):
        qualify(resolver, reviews, identity)


# --- the node's own labeller (machine reviews) ----------------------------------------------------------------------

def machine_setup(legacy):
    from tests.permissions_v2.test_automatic_message_review import answer, setup as automatic_setup
    from topos.permissions_v2.automatic_message_review import publish
    resolver, reviews, identity, prepared = automatic_setup(legacy)
    conversation_tables(legacy)
    with owner():
        review = publish(resolver, reviews, prepared, answer(prepared), now=1)
    return resolver, reviews, identity, review


def machine_current(resolver, reviews, identity, review):
    from topos.permissions_v2.automatic_message_review import is_current, prepare
    with owner():
        return is_current(review, prepare(resolver, reviews, identity))


def qualify_machine(resolver, reviews, identity):
    from topos.permissions_v2.message_evidence import qualify_automatic_message
    with resolver._read() as (conn, floor), reviews._db() as db:
        return qualify_automatic_message(resolver, conn, floor, identity, reviews, db)


def test_an_entry_that_reaches_nothing_keeps_the_labellers_assessment(legacy):
    """Rule: `_bound_terms` leaves the vocabulary out of an untouched item's context revision. Bind it again (1.5.0)
    and the labeller's assessment is out of date: one model call per item on the node to recover."""
    resolver, reviews, identity, review = machine_setup(legacy)
    assert machine_current(resolver, reviews, identity, review)
    add_entry(legacy, UNRELATED, "entry-1")
    assert machine_current(resolver, reviews, identity, review)
    assert qualify_machine(resolver, reviews, identity)


def test_an_entry_that_reaches_a_neighbour_the_labeller_was_shown_withdraws_it(legacy):
    """The classifier was shown the item's neighbours. An entry for a name a neighbour says reaches the context."""
    from tests.permissions_v2.test_automatic_message_review import answer, setup as automatic_setup
    from topos.permissions_v2.automatic_message_review import publish
    conn = legacy[1]
    columns = [r[1] for r in conn.execute('PRAGMA table_info(conversation_messages)')]
    original = dict(zip(columns, conn.execute('SELECT * FROM conversation_messages').fetchone()))
    original.update(message_id='imessage:0', content='Dinner with Orla Brightwater went well.', is_from_self=0,
                    event_at='2000-01-01T00:00:00Z')
    conn.execute('INSERT INTO conversation_messages VALUES(' + ','.join('?' for _ in columns) + ')',
                 [original.get(c) for c in columns])
    conn.commit()
    resolver, reviews, identity, prepared = automatic_setup(legacy)
    conversation_tables(legacy)
    with owner():
        review = publish(resolver, reviews, prepared, answer(prepared), now=1)
    assert machine_current(resolver, reviews, identity, review)
    add_entry(legacy, "Orla Brightwater", "entry-1")
    assert not machine_current(resolver, reviews, identity, review)


def test_a_labellers_assessment_stored_under_the_150_revision_is_kept_while_the_list_is_unchanged(legacy, monkeypatch):
    from topos.permissions_v2 import automatic_message_review
    conversation_tables(legacy)
    add_entry(legacy, UNRELATED, "entry-1")
    monkeypatch.setattr(automatic_message_review, "_bound_terms",
                        lambda boundary, identity, row, texts, terms, legacy: terms)        # 1.5.0's context
    monkeypatch.setattr(message_evidence, "touched_by_off_limits", lambda *args: True)        # 1.5.0's snapshot
    resolver, reviews, identity, review = machine_setup(legacy)
    monkeypatch.undo()
    assert machine_current(resolver, reviews, identity, review)
    assert qualify_machine(resolver, reviews, identity)
    add_entry(legacy, "Another Stranger", "entry-2")
    assert not machine_current(resolver, reviews, identity, review)


def test_a_labellers_150_assessment_is_not_kept_when_its_neighbours_change(legacy, monkeypatch):
    """Review R-N1-151 M3. The 1.5.0 acceptance compares the context revision too, not the snapshot alone: a nearer
    neighbour inserted after the upgrade puts the 1.5.0 assessment out of date while the list is unchanged."""
    from topos.permissions_v2 import automatic_message_review
    conversation_tables(legacy)
    add_entry(legacy, UNRELATED, "entry-1")
    monkeypatch.setattr(automatic_message_review, "_bound_terms",
                        lambda boundary, identity, row, texts, terms, legacy: terms)        # 1.5.0's context
    monkeypatch.setattr(message_evidence, "touched_by_off_limits", lambda *args: True)        # 1.5.0's snapshot
    resolver, reviews, identity, review = machine_setup(legacy)
    monkeypatch.undo()
    assert machine_current(resolver, reviews, identity, review)
    conn = legacy[1]
    columns = [r[1] for r in conn.execute('PRAGMA table_info(conversation_messages)')]
    original = dict(zip(columns, conn.execute("SELECT * FROM conversation_messages WHERE message_id='imessage:1'").fetchone()))
    original.update(message_id='imessage:0b', content='Running late, see you soon.', is_from_self=0)
    conn.execute('INSERT INTO conversation_messages VALUES(' + ','.join('?' for _ in columns) + ')',
                 [original.get(c) for c in columns])
    conn.commit()
    assert not machine_current(resolver, reviews, identity, review)


def test_the_grant_census_mirrors_the_150_acceptance(legacy, monkeypatch):
    """Review R-N1-151 H3: the census's stale-reason mirror (`grant_census._refine`) knows BL-107's 1.5.0 acceptance.
    A 1.5.0 review the engine accepts (the list unchanged) is never filed as `review_stale_protection`; once the list
    changes, it is."""
    from tests.permissions_v2.test_grant_census import gc
    from topos.permissions_v2 import automatic_message_review
    conversation_tables(legacy)
    add_entry(legacy, UNRELATED, "entry-1")
    monkeypatch.setattr(automatic_message_review, "_bound_terms",
                        lambda boundary, identity, row, texts, terms, legacy: terms)
    monkeypatch.setattr(message_evidence, "touched_by_off_limits", lambda *args: True)
    resolver, reviews, identity, review = machine_setup(legacy)
    monkeypatch.undo()

    def reason():
        with resolver._read() as (conn, floor), reviews._db() as db:
            raw = resolver._load(conn, identity)
            return gc._refine("review_stale", resolver=resolver, conn=conn, floor=floor, frozen=reviews.freeze(db),
                              identity=identity, raw=raw)

    assert machine_current(resolver, reviews, identity, review)
    # No revision of it is stale (snapshot, context): the mirror finds no cause of its own, as the engine finds none.
    assert reason() == "review_stale_other"
    add_entry(legacy, "Another Stranger", "entry-2")
    assert not machine_current(resolver, reviews, identity, review)
    assert reason() == "review_stale_protection"
