"""BL-112 (2), the owner's ruling of 8 Oct 2026: an Off-limits entry hides its items, not the whole answer.

The query pipeline had an older floor (`retrieval._derived_floor_applies`): while any Off-limits entry existed, every
caller but the owner's app (his outside AI client, his routines) got NO summaries and no scores at all. Its own
comment says it is for record protections, and the owner's rule of 26 Sep calls such a floor a stopgap: only the
protected items are off limits. Now:

  - an entry no longer closes the derived modes; the rule for the entries a request reads
    (`blackhole_guard.entries_items`, the share boundary's own matcher and ids) takes every item that names the
    person, or was built from a record they are mentioned in, out of every list of the answer, and the rest stays;
  - a record protection still closes them (no name to look for: the floor's own reason);
  - a caller that is neither the owner nor the routine lane, while an entry is carried and waiting, is still closed
    out as before (such a caller reads that entry whole);
  - a rule that cannot be built closes them, as before.

Every person here is invented (the privacy battery's corpus).
"""
from __future__ import annotations

import json

import pytest

from tests.topos.test_carried_entry_outward_paths import RECIPIENT, ROUTINE
from tests.topos.test_carried_entry_owner_paths import LOCAL_CLIENT, OUTSIDE_CLIENT, as_caller, the_node_knows_its_owner  # noqa: F401
from tests.topos.test_carried_entry_waits import ORDINARY, excluded
from tests.topos.test_routine_lane_carried_items import _corpus, _retrieve
from topos.features.lifecycle import blackhole_guard
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.contact_excludes import carry_contact_excludes

pytestmark = pytest.mark.public

CALLERS = {"his outside client": OUTSIDE_CLIENT, "a client at his own door": LOCAL_CLIENT, "his routine": ROUTINE,
           "a recipient": RECIPIENT}


def _split(c, entries, name, ids):
    """(naming the person, built from a record they are mentioned in, the rest) of one list of a packet."""
    naming = [entry for entry in entries if name.lower() in json.dumps(entry).lower()]
    linked = [entry for entry in entries if entry not in naming and entry.get("record_id") in ids]
    return naming, linked, [entry for entry in entries if entry not in naming and entry not in linked]


@pytest.mark.parametrize("who", sorted(CALLERS))
@pytest.mark.parametrize("mode,key", [("summary", "summaries"), ("inference", "scores")])
def test_an_entry_the_owner_made_withholds_its_items_and_releases_the_rest(tmp_path, who, mode, key):
    """Rule: `_derived_floor_applies` no longer closes for an entry, and `_retrieve_bundle` applies `entries_items`
    to the whole packet. Restore the floor and the rest is not released (the packet is empty); drop the item rule
    and the items that name the person, or were built from the records they are in, are released."""
    from tests.evals.privacy.blackhole.corpus import BH_CANONICAL, BH_ID

    c = _corpus(tmp_path)
    principal = CALLERS[who]
    before = _retrieve(c, principal, mode=mode)[key]
    theirs = {row[0] for row in c.execute("SELECT record_id FROM entity_mentions WHERE entity_id=?", (BH_ID,))}
    naming, linked, others = _split(c, before, BH_CANONICAL, theirs)
    assert naming and others, "the corpus must hold both kinds for this test to mean anything"
    BlackholeStore(c).blackhole_entity(entity_ref=BH_ID, processing_tier="secure", note=None)
    c.commit()
    after = _retrieve(c, principal, mode=mode)
    assert after[key] == others, who
    assert BH_CANONICAL.lower() not in json.dumps(after).lower()
    assert not ({entry.get("record_id") for entry in after[key]} & theirs)


def test_a_record_protection_still_closes_them(tmp_path):
    c = _corpus(tmp_path)
    assert _retrieve(c, OUTSIDE_CLIENT)["summaries"]
    first = c.execute("SELECT message_id FROM conversation_messages LIMIT 1").fetchone()[0]
    c.execute("INSERT INTO owner_only_records (canonical_table, record_id) VALUES ('conversation_messages', ?)", (first,))
    c.commit()
    for principal in CALLERS.values():
        assert _retrieve(c, principal)["summaries"] == []
        assert _retrieve(c, principal, mode="inference")["scores"] == []


def test_a_carried_entry_still_closes_them_for_a_caller_that_reads_it_whole(tmp_path):
    """Not the ruling's: an entry carried and waiting, for a caller that is neither the owner nor the routine lane."""
    c = _corpus(tmp_path)
    excluded(c, ORDINARY["username al"])
    carry_contact_excludes(c)
    c.commit()
    assert _retrieve(c, RECIPIENT)["summaries"] == []
    assert _retrieve(c, OUTSIDE_CLIENT)["summaries"] and _retrieve(c, ROUTINE)["summaries"]


def test_if_the_rule_cannot_be_built_they_are_closed(tmp_path, monkeypatch):
    c = _corpus(tmp_path)
    BlackholeStore(c).blackhole_entity(entity_ref="Perrin Ashgrove", processing_tier="secure", note=None)
    c.commit()
    assert _retrieve(c, OUTSIDE_CLIENT)["summaries"]

    def refuses(*_args, **_kwargs):
        raise RuntimeError("the boundary cannot be built")

    monkeypatch.setattr(blackhole_guard, "entries_items", refuses)
    for principal in CALLERS.values():
        assert _retrieve(c, principal)["summaries"] == []
        assert _retrieve(c, principal, mode="inference")["scores"] == []


def test_a_topic_cluster_quoting_a_withheld_unnamed_message_is_closed(tmp_path, monkeypatch):
    """Review R-N1-151 H1. A cluster quotes its central member's own text (`centroid_preview`) and carries no member
    record id, so the item rule can judge it only by names in it. A message the boundary withholds without naming the
    person (one they are mentioned in by link only) would be quoted to every non-owner caller. Rule: topic clusters
    are aggregates with no lineage, closed with the others while an entry is in view (`unproven_closed`)."""
    from tests.evals.privacy.blackhole.corpus import BH_CANONICAL, BH_ID
    from topos.features.lifecycle.blackhole import EVERYONE
    from topos.query import retrieval

    c = _corpus(tmp_path)
    linked = [row[0] for row in c.execute("SELECT record_id FROM entity_mentions WHERE entity_id=?", (BH_ID,))]
    quoted = None
    for record_id in linked:
        row = c.execute("SELECT content FROM conversation_messages WHERE message_id=?", (record_id,)).fetchone()
        if row and row[0] and BH_CANONICAL.split()[0].lower() not in row[0].lower():
            quoted = row[0]
            break
    assert quoted, "the corpus must hold a message linked to the person that does not name them"
    cluster = {"cluster_id": "c-quote", "label": "Weekend plans", "label_terms": ["weekend"],
               "centroid_preview": quoted[:120], "size": 4}
    monkeypatch.setattr(retrieval, "_bundle_is_global_db", lambda adapters: True)
    monkeypatch.setattr(retrieval, "_semantic_hits", lambda *args, **kwargs: ([], None))
    monkeypatch.setattr(retrieval, "_load_ranked_clusters", lambda *args, **kwargs: [dict(cluster)])
    before = _retrieve(c, OUTSIDE_CLIENT)
    assert before.get("topic_clusters"), "control: with nothing Off-limits the cluster is answered"
    BlackholeStore(c).blackhole_entity(entity_ref=BH_ID, processing_tier="secure", note=None)
    c.commit()
    # The hole the rule closes: the item rule alone keeps this cluster (no name, no id in it).
    rule = blackhole_guard.entries_items(c, EVERYONE)
    try:
        assert not rule.names(cluster)
    finally:
        rule.close()
    for principal in CALLERS.values():
        for mode in ("summary", "inference"):
            after = _retrieve(c, principal, mode=mode)
            assert not after.get("topic_clusters"), (principal, mode)
            assert quoted[:60] not in json.dumps(after)
