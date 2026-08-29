"""L1-9 — the read surfaces.

Handlers are called directly with every argument supplied. FastAPI `Query(...)` defaults are
sentinel objects, not values, so a direct call that omits one passes the sentinel through to
`int()` — these tests would pass a Query object where the code expects a number.

Computed data nobody can read is not delivered. These two endpoints are the difference
between L1 being a table and L1 being an answer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.analytics.messenger_communities import _compute_directed_lane

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
DS = "ds"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "api.db"))
    c.execute("""CREATE TABLE conversation_messages (
        conversation_id TEXT, message_id TEXT PRIMARY KEY, dataset_id TEXT,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT)""")
    # the REAL shapes of the label tables — the resolver filters on dataset_id and orders
    # on source_id/updated_at, and a thinner fixture silently matches nothing
    c.execute("""CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, dataset_id TEXT,
        source_id TEXT, display_name TEXT, is_self INTEGER DEFAULT 0,
        known_usernames_json TEXT, created_at TEXT, updated_at TEXT)""")
    c.execute("""CREATE TABLE contact_identifiers (dataset_id TEXT, source_id TEXT,
        identifier TEXT, identifier_type TEXT, contact_id TEXT,
        created_at TEXT, updated_at TEXT)""")
    n = 0
    for peer, count in (("+15125551234", 8), ("262966", 5)):
        for i in range(count):
            for is_self in (0, 1):
                n += 1
                c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                          (f"c_{peer}", f"m{n}", DS, None if is_self else peer,
                           (T0 + timedelta(days=i, minutes=is_self * 3)).isoformat(),
                           is_self, "imessage", None))
    # A REAL contact behind one peer, so label resolution is exercised end to end — the
    # first version mocked the resolver, which hid a keyword-only signature mismatch
    # (HTTP 500 on every request) AND a nested-dict return shape. Mocks are for failure
    # injection; this surface is tested against the real collaborator.
    c.execute("INSERT INTO contacts VALUES ('ct_1', ?, 'address_book', 'Tango Uniform', 0, NULL, 't', 't')", (DS,))
    c.execute("INSERT INTO contact_identifiers VALUES (?, 'address_book', '+15125551234',"
              " 'phone', 'ct_1', 't', 't')", (DS,))
    c.commit()
    _compute_directed_lane(c, DS, None)
    import topos.api.messenger_analytics as api
    monkeypatch.setattr(api, "get_db_connection", lambda: c)
    yield c
    c.close()


def test_relationships_returns_the_owner_dyads(conn):
    from topos.api.messenger_analytics import get_relationships

    res = get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)
    assert res["count"] >= 1
    assert {r["peer_key"] for r in res["relationships"]} == {"+15125551234"}


def test_automated_peers_are_excluded_by_default_and_available_on_request(conn):
    """Stored, not dropped: ranking a carrier shortcode beside a friend makes every
    relationship number meaningless, but dropping it at write loses the honest answer to
    'what is actually filling my inbox'."""
    from topos.api.messenger_analytics import get_relationships

    default = {r["peer_key"] for r in get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)["relationships"]}
    widened = {r["peer_key"] for r in
               get_relationships(dataset_id=DS, tie_state=None, include_automated=True, limit=100)["relationships"]}
    assert "262966" not in default
    assert "262966" in widened


def test_sent_and_received_are_stated_from_the_owners_side(conn):
    """A caller must never have to know which side of the canonical pair the owner landed
    on — that is exactly the sort-order trap that inverted `balance`."""
    from topos.api.messenger_analytics import get_relationships

    r = get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)["relationships"][0]
    assert r["sent"] + r["received"] == r["total_msgs"]
    assert r["sent"] == 8, "the owner sent 8 of the 16"


def test_a_tie_state_filter_narrows(conn):
    from topos.api.messenger_analytics import get_relationships

    res = get_relationships(dataset_id=DS, tie_state="nonexistent_state", include_automated=False, limit=100)
    assert res["count"] == 0


def test_directed_edges_default_to_dm_not_broadcast(conn):
    """Group broadcast fans one message out to every other speaker, so defaulting to it
    would let a busy thread outrank every real correspondence."""
    from topos.api.messenger_analytics import get_directed_edges

    res = get_directed_edges(dataset_id=DS, peer_key=None, edge_kind="dm", limit=200)
    assert res["edge_kind"] == "dm"
    assert all(e["edge_kind"] == "dm" for e in res["edges"])


def test_directed_edges_can_be_scoped_to_one_peer(conn):
    from topos.api.messenger_analytics import get_directed_edges

    res = get_directed_edges(dataset_id=DS, peer_key="+15125551234", edge_kind="dm", limit=200)
    assert res["count"] > 0
    for e in res["edges"]:
        assert "+15125551234" in (e["from_key"], e["to_key"])


def test_a_read_before_any_write_returns_empty_not_an_error(tmp_path, monkeypatch):
    """A read surface must not 500 because a write pass has never run. Empty is the honest
    answer to 'what are my relationships' on a node that has computed none."""
    c = sqlite3.connect(str(tmp_path / "fresh.db"))
    import topos.api.messenger_analytics as api
    monkeypatch.setattr(api, "get_db_connection", lambda: c)
    from topos.api.messenger_analytics import get_directed_edges, get_relationships

    assert get_relationships(dataset_id="nope", tie_state=None, include_automated=False, limit=100)["relationships"] == []
    assert get_directed_edges(dataset_id="nope", peer_key=None, edge_kind="dm", limit=200)["edges"] == []
    c.close()


# --- L5 signals over the same substrate ---

def test_relationship_signals_reports_what_it_declined_to_judge(conn):
    """A dyad under the floor has not been judged and found wanting — it has not been
    judged. Reporting the count is the difference between "you have N relationships" and
    "most of your contacts are events"."""
    from topos.api.messenger_analytics import get_relationship_signals

    res = get_relationship_signals(dataset_id=DS, signal="all")
    assert res["dyads_considered"] >= 1
    assert "excluded_below_floor" in res
    assert res["dyads_above_floor"] + res["excluded_below_floor"] == res["dyads_considered"]


def test_each_signal_can_be_requested_alone(conn):
    from topos.api.messenger_analytics import get_relationship_signals

    only = get_relationship_signals(dataset_id=DS, signal="warmth")
    assert "warmth" in only
    assert "drift_alarms" not in only and "reciprocity" not in only


def test_signals_on_an_unknown_dataset_are_empty_not_an_error(conn):
    from topos.api.messenger_analytics import get_relationship_signals

    res = get_relationship_signals(dataset_id="nope", signal="all")
    assert res["dyads_considered"] == 0
    assert res["warmth"] == []


# --- the breaks the adversarial pass found, kept as regression tests ---

def test_labels_are_flat_strings_from_the_real_resolver(conn):
    """The 500-on-every-request break, and its shadow.

    resolve_participant_labels is keyword-only and returns nested dicts; called
    positionally it raises (HTTP 500), and passed through raw it embeds an object where a
    string is promised. Both were invisible while the tests mocked the resolver.
    """
    from topos.api.messenger_analytics import get_relationships

    res = get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)
    r = res["relationships"][0]
    assert isinstance(r["label"], str)
    assert r["label"] == "Tango Uniform", "the address-book name, not the phone number"


def test_an_owner_owner_row_is_not_presented_as_a_relationship(conn):
    """Corpus damage (a peer indistinguishable from the owner) must not appear as the owner
    being their own contact."""
    from topos.analytics.messenger_directed import MESSENGER_DYAD_STATS_TABLE
    from topos.api.messenger_analytics import get_relationships

    conn.execute(
        f"""INSERT OR REPLACE INTO {MESSENGER_DYAD_STATS_TABLE}
            (dataset_id, a_key, b_key, involves_self, peer_class, total_msgs, a_to_b, b_to_a,
             created_at, updated_at)
            VALUES (?, 'self', 'self', 1, 'human', 9, 5, 4, 't', 't')""", (DS,))
    conn.commit()
    res = get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)
    assert "self" not in {r["peer_key"] for r in res["relationships"]}


def test_a_failing_resolver_degrades_to_keys_not_500(conn, monkeypatch):
    """Labels are decoration; the data must still flow.

    Patched at the RESOLVER module — the read bodies moved into
    analytics/relationship_reads (SGU-1's shared-transport module), which imports the
    resolver at call time, so the module attribute is the one real seam.
    """
    import topos.analytics.messenger_labels as labels_mod
    import topos.api.messenger_analytics as api

    def boom(*a, **k):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(labels_mod, "resolve_participant_labels", boom)
    res = api.get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)
    assert res["relationships"], "data flows"
    assert all(r["label"] == r["peer_key"] for r in res["relationships"])


# --- G2: the naming loop ---

def test_an_unnamed_relationship_says_so_and_carries_its_contact_id(conn):
    """The node cannot conjure names it never ingested (measured: 584 of 1,386 contacts
    named, zero of the top unnamed dyads recoverable by any normalization). What it can do
    is make asking cheap: needs_name plus the contact_id the existing naming endpoint wants."""
    from topos.api.messenger_analytics import get_relationships

    conn.execute("INSERT INTO conversation_messages VALUES"
                 " ('c9','n1',?, '+15550009999', '2026-05-02T09:00:00+00:00', 0, 'imessage', NULL)", (DS,))
    for i in range(8):
        conn.execute("INSERT INTO conversation_messages VALUES"
                     " ('c9', ?, ?, ?, ?, ?, 'imessage', NULL)",
                     (f"n{i+2}", DS, None if i % 2 else "+15550009999",
                      f"2026-05-0{(i%7)+2}T1{i}:00:00+00:00", 1 if i % 2 else 0))
    conn.commit()
    from topos.analytics.messenger_communities import _compute_directed_lane
    _compute_directed_lane(conn, DS, None)
    res = get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)
    rows = {r["peer_key"]: r for r in res["relationships"]}
    named = rows["+15125551234"]
    assert named["needs_name"] is False and named["label"] == "Tango Uniform"
    ghost = rows["+15550009999"]
    assert ghost["needs_name"] is True
    assert res["unnamed_count"] >= 1


def test_naming_a_contact_renames_the_person_end_to_end(conn):
    """The full loop the say-do gap broke: placeholder entity -> owner names the contact ->
    seeding promotes the entity's canonical_name, old surface kept as an alias."""
    import json as _json

    from topos.features.entities.resolver import EntityResolver

    conn.executescript("""
      CREATE TABLE IF NOT EXISTS entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, identifiers_json TEXT,
        is_self INTEGER DEFAULT 0, contact_id TEXT, mention_count INTEGER DEFAULT 0,
        metadata_json TEXT, created_at TEXT, updated_at TEXT, first_seen TEXT, last_seen TEXT);
      CREATE TABLE IF NOT EXISTS entity_mentions (entity_id TEXT, record_id TEXT);
      CREATE TABLE IF NOT EXISTS entity_review (review_id TEXT PRIMARY KEY, surface_text TEXT,
        candidate_entity_id TEXT, score REAL, status TEXT, created_at TEXT);
    """)
    conn.execute("INSERT INTO contacts VALUES ('ct_ghost', ?, 'address_book', NULL, 0, NULL, 't', 't')", (DS,))
    conn.execute("INSERT INTO contact_identifiers VALUES (?, 'address_book', '+15550009999',"
                 " 'phone', 'ct_ghost', 't', 't')", (DS,))
    conn.commit()
    r = EntityResolver(conn)
    r.seed_from_contacts()
    ent = conn.execute("SELECT entity_id, canonical_name FROM entities WHERE contact_id='ct_ghost'").fetchone()
    assert ent and ent[1] == "+15550009999", "placeholder first"

    # the owner names the contact (the existing PUT endpoint's effect)
    conn.execute("UPDATE contacts SET display_name='Marcus Webb' WHERE contact_id='ct_ghost'")
    conn.commit()
    r.seed_from_contacts()
    row = conn.execute("SELECT canonical_name, aliases_json FROM entities"
                       " WHERE contact_id='ct_ghost'").fetchone()
    assert row[0] == "Marcus Webb", "the person is renamed, not just the contact"
    assert "+15550009999" in _json.loads(row[1] or "[]"), "the old surface survives as an alias"


def test_a_real_name_is_never_demoted_by_reseeding(conn):
    """Promotion is placeholder->name only. A hand-curated canonical_name must not be
    overwritten because the contact's display_name differs."""
    from topos.features.entities.resolver import EntityResolver

    conn.executescript("""
      CREATE TABLE IF NOT EXISTS entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, identifiers_json TEXT,
        is_self INTEGER DEFAULT 0, contact_id TEXT, mention_count INTEGER DEFAULT 0,
        metadata_json TEXT, created_at TEXT, updated_at TEXT, first_seen TEXT, last_seen TEXT);
      CREATE TABLE IF NOT EXISTS entity_mentions (entity_id TEXT, record_id TEXT);
      CREATE TABLE IF NOT EXISTS entity_review (review_id TEXT PRIMARY KEY, surface_text TEXT,
        candidate_entity_id TEXT, score REAL, status TEXT, created_at TEXT);
    """)
    conn.execute("INSERT INTO contacts VALUES ('ct_k', ?, 'address_book', 'Kim H.', 0, NULL, 't', 't')", (DS,))
    conn.execute("INSERT INTO contact_identifiers VALUES (?, 'address_book', '+15550001111',"
                 " 'phone', 'ct_k', 't', 't')", (DS,))
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
                 " aliases_json, identifiers_json, contact_id) VALUES"
                 " ('ent_kim', 'person', 'Hotel India', 'hotel india', '[]', '[]', 'ct_k')")
    conn.commit()
    EntityResolver(conn).seed_from_contacts()
    assert conn.execute("SELECT canonical_name FROM entities WHERE entity_id='ent_kim'"
                        ).fetchone()[0] == "Hotel India"


# --- G3: one labeler ---

def test_the_stored_warmth_band_is_the_calibrated_one(conn):
    """tie_state (fixed thresholds) and the warmth kernel (calibrated) were two answers to
    the same question. The lane now stores the kernel's band on the dyad, and the API
    exposes it as primary — one labeler, one answer."""
    from topos.analytics.messenger_communities import _compute_directed_lane
    from topos.api.messenger_analytics import get_relationships

    _compute_directed_lane(conn, DS, None)
    res = get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)
    for r in res["relationships"]:
        assert "warmth_band" in r, "the authoritative label rides on every row"


def test_a_phone_with_a_telephony_crumb_is_not_a_name():
    """G3's nameability edge: '+1512633 ext' is still a phone number — digits dominate and
    the letters are annotation, not identity."""
    from topos.features.derivation.net_subject_policy import _looks_named

    assert not _looks_named("+15555550103 ext")
    assert not _looks_named("555-555-0110 x2")
    assert _looks_named("Bo Xu 512")  # a real name with a number attached survives
    assert _looks_named("Tango Uniform")
