"""Owner identity binding: two sets, a frozen legacy rule, and a watched clock.

These tests pin the properties the binding exists for. The permit set says whom
a release may be about and only ever contains what the owner attested. The
restriction set says what an owner veto matches and contains every spelling of
the owner the node has ever seen. Widening the first can never narrow the
second, and no entity id may leave the node in a label, an output or an error.
"""
import json
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import (attest, corpus, decision, edit, owner, payload)
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.identity import (ATTESTED_CONTRACT, LEGACY_CONTRACT, SELF,
    SUBJECT_CONTRACT_BY_CAPABILITY, attested_subjects, composition_revision, entries,
    identity_fingerprint, legacy_owner_subjects, literal_self_shadowed, permit_subjects,
    registry_ids, rekeyed_facts, restriction_subjects, self_entity_ids)
from topos.permissions_v2.protection_clock import (EVENTS, LEDGER, REGISTRY, TABLE,
    ATTESTATION_STATEMENT, clock_state, identity_coverage, identity_event_key,
    resync_identity_coverage, upgrade_protection_clock_v4)

OWNER = "owner-entity"


def db(corpus):
    return sqlite3.connect(corpus[0].path)


def generation(conn):
    return conn.execute(f"SELECT generation FROM {TABLE} WHERE singleton=1").fetchone()[0]


def add_entity(conn, entity_id, *, is_self=1, entity_type="person"):
    conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) "
                 "VALUES(?,?,?,?,?)", (entity_id, entity_type, "Person", "person", is_self))


def do_attest(conn, entity_id, *, entry_id=None, command_id=None):
    """Write one consent row the way the future service will, under the triggers."""
    entry_id = entry_id or f"entry-{entity_id}"
    row = conn.execute("SELECT entity_type,is_self,contact_id FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
    conn.execute(
        f"INSERT INTO {LEDGER}(entry_id,action,entity_id,target_entry_id,entity_type,is_self,contact_id,"
        "composition_revision,statement_version,command_id,command_hash,generation) "
        "VALUES(?,'attest',?,NULL,?,?,?,?,?,?,?,?)",
        (entry_id, entity_id, row[0], row[1], row[2], composition_revision(conn, entity_id),
         ATTESTATION_STATEMENT, command_id or f"command-{entry_id}", "0" * 64, generation(conn) + 1))
    return entry_id


def do_revoke(conn, entity_id, entry_id):
    conn.execute(
        f"INSERT INTO {LEDGER}(entry_id,action,entity_id,target_entry_id,statement_version,command_id,"
        "command_hash,generation) VALUES(?,'revoke',?,?,?,?,?,?)",
        (f"revoke-{entry_id}", entity_id, entry_id, ATTESTATION_STATEMENT,
         f"command-revoke-{entry_id}", "0" * 64, generation(conn) + 1))


# --- the invariant the whole design rests on -------------------------------

@pytest.mark.parametrize("build", [
    pytest.param(lambda conn: None, id="installed"),
    pytest.param(lambda conn: add_entity(conn, "second-self"), id="two_self_rows"),
    pytest.param(lambda conn: do_attest(conn, OWNER), id="attested"),
    pytest.param(lambda conn: (add_entity(conn, "second-self"), do_attest(conn, "second-self")), id="attested_second"),
    pytest.param(lambda conn: do_revoke(conn, OWNER, do_attest(conn, OWNER)), id="revoked"),
    pytest.param(lambda conn: conn.execute(
        "INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into) VALUES('gone',?)", (OWNER,)),
        id="merged_away"),
    pytest.param(lambda conn: add_entity(conn, SELF), id="literal_self_row"),
])
def test_permit_set_is_always_inside_the_restriction_set(corpus, build):
    """P ⊆ R, in every identity state. A wider permit can never narrow a veto."""
    with db(corpus) as conn:
        build(conn)
    with db(corpus) as conn:
        restrictions = restriction_subjects(conn)
        assert permit_subjects(conn, contract=ATTESTED_CONTRACT) <= restrictions
        try:
            legacy = permit_subjects(conn, contract=LEGACY_CONTRACT)
        except PolicyError as exc:
            assert exc.code == "owner_subject_ambiguous"
        else:
            assert legacy <= restrictions


def test_restriction_set_keeps_every_owner_spelling_the_node_has_seen(corpus):
    with db(corpus) as conn:
        add_entity(conn, "second-self")
        conn.execute("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into) VALUES('old-spelling',?)",
                     ("second-self",))
    with db(corpus) as conn:
        # Transitive: the absorbed id is an owner spelling because what absorbed
        # it is, and a tombstone keyed by it must still veto.
        assert {SELF, OWNER, "second-self", "old-spelling"} <= restriction_subjects(conn)
        # Ambiguity never raises on the restriction side.
        assert "old-spelling" not in permit_subjects(conn, contract=ATTESTED_CONTRACT)


def test_restriction_set_refuses_to_grow_without_bound(corpus):
    with db(corpus) as conn:
        conn.executemany("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into) VALUES(?,?)",
                         [(f"chain-{index}", f"chain-{index + 1}") for index in range(600)])
        conn.execute("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into) VALUES(?,'chain-0')", (OWNER,))
    with db(corpus) as conn, pytest.raises(PolicyError, match="identity_restriction_unbounded"):
        restriction_subjects(conn)


# --- the legacy rule is frozen ---------------------------------------------

def test_legacy_permit_never_reads_identity_state(corpus):
    """Attesting, revoking or registering changes nothing for an older capability."""
    with db(corpus) as conn:
        before = legacy_owner_subjects(conn)
        do_attest(conn, OWNER)
    with db(corpus) as conn:
        conn.execute(f"DROP TABLE {LEDGER}")  # the frozen rule must not touch it
        assert legacy_owner_subjects(conn) == before == {SELF, OWNER}
        assert permit_subjects(conn, contract=LEGACY_CONTRACT) == before


def test_legacy_permit_still_refuses_two_self_rows_with_the_same_code(corpus):
    with db(corpus) as conn:
        add_entity(conn, "second-self")
    with db(corpus) as conn:
        with pytest.raises(PolicyError) as raised:
            permit_subjects(conn, contract=LEGACY_CONTRACT)
        assert raised.value.code == "owner_subject_ambiguous"
        # The attested rule is not blocked by the same ambiguity; it just has
        # nothing attested yet, so it permits only the literal subject.
        assert permit_subjects(conn, contract=ATTESTED_CONTRACT) == {SELF}


def test_multi_self_node_releases_an_attested_subject_where_legacy_withholds(corpus):
    with db(corpus) as conn:
        add_entity(conn, "second-self")
    attest(corpus, review_id="review-multi-self")
    assert decision(corpus).reason_code == "owner_subject_ambiguous"
    with db(corpus) as conn:
        do_attest(conn, OWNER)
    with db(corpus) as conn:
        assert attested_subjects(conn) == {OWNER}
        assert permit_subjects(conn, contract=ATTESTED_CONTRACT) == {SELF, OWNER}
    # The legacy capability is unmoved by the attestation; the attested one now
    # releases, once the owner reviews the evidence the attestation changed.
    attest(corpus, review_id="review-after-attestation")
    assert decision(corpus).reason_code == "owner_subject_ambiguous"
    assert corpus[0].qualify(corpus[2], reviews=corpus[1], contract=ATTESTED_CONTRACT).verdict == "qualified"


# --- the literal subject ----------------------------------------------------

def test_an_entity_row_named_self_shadows_the_producer_constant(corpus):
    with db(corpus) as conn:
        add_entity(conn, SELF, is_self=0)
    with db(corpus) as conn:
        assert literal_self_shadowed(conn)
        assert SELF not in permit_subjects(conn, contract=ATTESTED_CONTRACT)
        assert SELF in restriction_subjects(conn)


# --- entry validity ---------------------------------------------------------

@pytest.mark.parametrize("change,reason", [
    ("DELETE FROM entities WHERE entity_id='owner-entity'", "entity_missing"),
    ("UPDATE entities SET is_self=0 WHERE entity_id='owner-entity'", "identity_columns_changed"),
    ("UPDATE entities SET contact_id='c-9' WHERE entity_id='owner-entity'", "identity_columns_changed"),
    ("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into) VALUES('absorbed','owner-entity')",
     "composition_changed"),
])
def test_an_attested_entity_that_moves_quarantines_until_reattested(corpus, change, reason):
    with db(corpus) as conn:
        do_attest(conn, OWNER)
    with db(corpus) as conn:
        assert entries(conn)[OWNER].state == "active"
        conn.execute(change)
    with db(corpus) as conn:
        entry = entries(conn)[OWNER]
        assert entry.state == "stale" and entry.reason == reason
        assert OWNER not in permit_subjects(conn, contract=ATTESTED_CONTRACT)
        # Quarantine is not forgetting: the veto set still covers it.
        assert OWNER in restriction_subjects(conn)


def test_churn_that_is_not_an_identity_change_leaves_consent_alone(corpus):
    with db(corpus) as conn:
        do_attest(conn, OWNER)
        before = generation(conn)
    with db(corpus) as conn:
        conn.execute("UPDATE entities SET canonical_name='Renamed', aliases_json='[\"a\"]', "
                     "mention_count=mention_count+7, updated_at='2026-01-01' WHERE entity_id=?", (OWNER,))
    with db(corpus) as conn:
        assert generation(conn) == before
        assert entries(conn)[OWNER].state == "active"
        assert OWNER in permit_subjects(conn, contract=ATTESTED_CONTRACT)


def test_revocation_is_terminal_and_reattestation_mints_a_new_entry(corpus):
    with db(corpus) as conn:
        entry_id = do_attest(conn, OWNER)
    with db(corpus) as conn:
        do_revoke(conn, OWNER, entry_id)
    with db(corpus) as conn:
        assert entries(conn)[OWNER].state == "revoked"
        assert OWNER not in permit_subjects(conn, contract=ATTESTED_CONTRACT)
        second = do_attest(conn, OWNER, entry_id="entry-again")
    with db(corpus) as conn:
        assert entries(conn)[OWNER].entry_id == second != entry_id
        assert entries(conn)[OWNER].state == "active"


def test_a_second_live_attestation_for_one_entity_is_refused_by_the_clock(corpus):
    with db(corpus) as conn:
        do_attest(conn, OWNER)
    with db(corpus) as conn, pytest.raises(sqlite3.IntegrityError, match="already active"):
        do_attest(conn, OWNER, entry_id="entry-duplicate", command_id="command-duplicate")


def test_a_revocation_of_something_that_is_not_current_is_refused(corpus):
    with db(corpus) as conn, pytest.raises(sqlite3.IntegrityError, match="not the current attestation"):
        do_revoke(conn, OWNER, "entry-that-never-existed")


# --- the append-only guards -------------------------------------------------

@pytest.mark.parametrize("table", [EVENTS, LEDGER, REGISTRY])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_the_owners_permission_history_cannot_be_rewritten(corpus, table, operation):
    with db(corpus) as conn:
        do_attest(conn, OWNER)
    statement = (f"DELETE FROM {table}" if operation == "DELETE"
                 else f"UPDATE {table} SET entity_id='x'" if table != EVENTS
                 else f"UPDATE {EVENTS} SET artifact_key='x'")
    with db(corpus) as conn, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(statement)


# --- taint ------------------------------------------------------------------

def test_a_fact_whose_subject_was_rewritten_in_place_is_never_attested_evidence(corpus):
    """The merge overlay: another person's fact re-keyed onto the owner's entity."""
    with db(corpus) as conn:
        do_attest(conn, OWNER)
        conn.execute("UPDATE signal_objects SET object_key=object_key||'-remapped' WHERE object_id=?", (corpus[2],))
    with db(corpus) as conn:
        assert rekeyed_facts(conn, [corpus[2]]) == {corpus[2]}
    attest(corpus, review_id="review-after-rekey")
    assert corpus[0].qualify(corpus[2], reviews=corpus[1],
                             contract=ATTESTED_CONTRACT).reason_code == "fact_subject_rewritten"
    # The legacy capability is not retroactively changed by the new rule.
    assert corpus[0].qualify(corpus[2], reviews=corpus[1], contract=LEGACY_CONTRACT).verdict == "qualified"


# --- what the clock watches -------------------------------------------------

def test_the_clock_records_exactly_which_identity_tables_it_watches(corpus):
    with db(corpus) as conn:
        assert identity_coverage(conn) == ("entities", "entity_mentions", "signal_objects")


def test_an_identity_table_that_appears_after_install_fails_closed_until_resynced(tmp_path):
    from tests.permissions_v2.test_evidence import apply_entity_blackhole_v1_up, apply_owner_only_records_v1_up
    from topos.storage.db.migrations.signal_objects import apply_signal_objects_up
    from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up
    from topos.storage.db.migrations.wiki_lifecycle_v1 import apply_wiki_lifecycle_v1_up
    from topos.permissions_v2.protection_clock import ensure_protection_clock
    path = tmp_path / "late.db"
    with sqlite3.connect(path) as conn:
        apply_signal_objects_up(conn); apply_owner_only_records_v1_up(conn)
        apply_entity_blackhole_v1_up(conn); apply_wiki_lifecycle_v1_up(conn)
        conn.execute("CREATE TABLE engine_config(key TEXT PRIMARY KEY,value TEXT)")
        conn.execute("INSERT INTO engine_config VALUES('user_id','owner-1')")
    ensure_protection_clock(path, owner_id="owner-1")
    with sqlite3.connect(path) as conn:
        assert identity_coverage(conn) == ("signal_objects",)
        before = clock_state(conn)
        apply_wiki_entities_v1_up(conn)
    with sqlite3.connect(path) as conn, pytest.raises(PolicyError, match="protection_clock_unavailable"):
        clock_state(conn)
    result = resync_identity_coverage(path, owner_id="owner-1", expected_clock_id=before[0],
                                      expected_generation=before[1])
    assert result["coverage"] == ["entities", "entity_mentions", "signal_objects"]
    assert result["generation"] == before[1] + 1
    with sqlite3.connect(path) as conn:
        assert clock_state(conn) == (before[0], before[1] + 1)
        assert resync_identity_coverage(path, owner_id="owner-1", expected_clock_id=before[0],
                                        expected_generation=before[1] + 1)["already_current"]


def test_resync_refuses_a_clock_whose_other_triggers_were_altered(corpus):
    with db(corpus) as conn:
        before = clock_state(conn)
        conn.execute("DROP TRIGGER permissions_v2_owner_only_records_insert")
    with pytest.raises(PolicyError, match="protection_upgrade_binding"):
        resync_identity_coverage(corpus[0].path, owner_id="owner-1", expected_clock_id=before[0],
                                 expected_generation=before[1])


@pytest.mark.parametrize("change,advances,logs", [
    ("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) "
     "VALUES('new-self','person','P','p',1)", True, "new-self"),
    ("UPDATE entities SET is_self=0 WHERE entity_id='owner-entity'", True, OWNER),
    ("DELETE FROM entities WHERE entity_id='owner-entity'", True, OWNER),
    ("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into) VALUES('absorbed','owner-entity')",
     True, "absorbed"),
    ("UPDATE entities SET canonical_name='Renamed' WHERE entity_id='owner-entity'", False, None),
    ("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) "
     "VALUES('a-contact','person','P','p',0)", False, None),
])
def test_identity_changes_are_observed_where_they_happen(corpus, change, advances, logs):
    with db(corpus) as conn:
        before = generation(conn)
        conn.execute(change)
    with db(corpus) as conn:
        assert (generation(conn) > before) is advances
        keys = {row[0] for row in conn.execute(f"SELECT artifact_key FROM {EVENTS}")}
        assert (identity_event_key(logs) in keys) if logs else not keys


def test_the_registry_starts_from_what_the_node_already_holds(corpus):
    with db(corpus) as conn:
        assert registry_ids(conn) == {OWNER}
        assert conn.execute(f"SELECT basis FROM {REGISTRY} WHERE entity_id=?", (OWNER,)).fetchone()[0] == "installed"


# --- nothing identifying leaves the node ------------------------------------

def test_no_entity_id_appears_in_a_review_an_output_or_a_refusal(corpus):
    with db(corpus) as conn:
        do_attest(conn, OWNER)
    payload(corpus, subject_entity_id=OWNER)
    review = attest(corpus, review_id="review-attested")
    qualified = corpus[0].qualify(corpus[2], reviews=corpus[1], contract=ATTESTED_CONTRACT)
    assert qualified.verdict == "qualified"
    text = json.dumps(qualified.model_dump()) + json.dumps(review.model_dump())
    assert OWNER not in text
    assert all(item.subject_entity_ids == [SELF] for item in qualified.evidence.classifications)


def test_an_unattested_owner_spelling_is_named_for_the_owner_only(corpus):
    with db(corpus) as conn:
        add_entity(conn, "second-self")
    payload(corpus, subject_entity_id="second-self")
    attest(corpus, review_id="review-unattested")
    result = corpus[0].qualify(corpus[2], reviews=corpus[1], contract=ATTESTED_CONTRACT)
    assert result.reason_code == "owner_subject_unattested"
    assert result.evidence is None


def test_identity_state_changes_stale_signed_authority_for_every_capability(corpus):
    from topos.permissions_v2.protection_clock import current_protection_revision
    with db(corpus) as conn:
        before = current_protection_revision(conn, owner_id="owner-1")
        fingerprint = identity_fingerprint(conn)
        do_attest(conn, OWNER)
    with db(corpus) as conn:
        assert current_protection_revision(conn, owner_id="owner-1") != before
        assert identity_fingerprint(conn) != fingerprint


# --- an attestation that the node cannot vouch for any more ------------------

def test_an_identity_event_after_the_attestation_quarantines_it(corpus):
    """Nothing about the entity changed; something happened to it. That counts."""
    with db(corpus) as conn:
        do_attest(conn, OWNER)
        entry = entries(conn)[OWNER]
        assert entry.state == "active"
        pins = conn.execute("SELECT entity_type,is_self,contact_id FROM entities WHERE entity_id=?",
                            (OWNER,)).fetchone()
        composition = composition_revision(conn, OWNER)
    with db(corpus) as conn:
        # Advance the clock on an unrelated identity, then move a mention onto
        # the attested entity. Neither touches a pinned column or the
        # composition, and the mention move deliberately does not advance.
        add_entity(conn, "unrelated-self")
        conn.execute("INSERT INTO entity_mentions(mention_id,entity_id,record_id,surface_text) "
                     "VALUES('mention-1','unrelated-self','message-1','x')")
    with db(corpus) as conn:
        conn.execute("UPDATE entity_mentions SET entity_id=? WHERE mention_id='mention-1'", (OWNER,))
    with db(corpus) as conn:
        assert conn.execute("SELECT entity_type,is_self,contact_id FROM entities WHERE entity_id=?",
                            (OWNER,)).fetchone() == pins
        assert composition_revision(conn, OWNER) == composition
        moved = entries(conn)[OWNER]
        assert moved.state == "stale" and moved.reason == "identity_event_after_attestation"
        assert OWNER not in permit_subjects(conn, contract=ATTESTED_CONTRACT)
        assert OWNER in restriction_subjects(conn)
        # A quarantined entry is still the live one, so confirming again means
        # retiring it and making a new statement. The service does both inside
        # one consumed command; the ledger refuses a second live entry.
        with pytest.raises(sqlite3.IntegrityError, match="already active"):
            do_attest(conn, OWNER, entry_id="entry-after-move", command_id="command-after-move")
        do_revoke(conn, OWNER, entry.entry_id)
        do_attest(conn, OWNER, entry_id="entry-after-move", command_id="command-after-move")
    with db(corpus) as conn:
        assert entries(conn)[OWNER].state == "active"


# --- evidence and policy must agree which rule they are under ----------------

def test_a_policy_cannot_be_evaluated_against_evidence_from_another_contract(corpus):
    from tests.permissions_v2.test_fact_policy import AS_OF, bundle, policy, timed as _timed  # noqa: F401
    from topos.permissions_v2.contract import Binding
    from topos.permissions_v2.fact_contract import FactPolicyV2
    from topos.permissions_v2.fact_eligibility import prepare_fact_eligibility

    for table in ("conversation_messages", "ai_chat_messages"):
        edit(corpus, f"ALTER TABLE {table} ADD COLUMN event_at TEXT")
        edit(corpus, f"UPDATE {table} SET event_at='2026-06-01T00:00:00Z'")
    edit(corpus, "UPDATE signal_objects SET valid_from='2026-06-01T00:00:00Z'")
    raw = policy(corpus)
    supplied = bundle(corpus)
    # The evidence really was qualified under the legacy rule, so the v1 policy
    # evaluates. Relabelling it as attested must not be tolerated.
    prepare_fact_eligibility(policy=FactPolicyV2.parse(raw), **supplied, binding=Binding.parse(raw["binding"]),
                             request_as_of=AS_OF, now=AS_OF)
    relabelled = supplied | {"evidence": supplied["evidence"].model_copy(
        update={"subject_contract": ATTESTED_CONTRACT})}
    with pytest.raises(PolicyError, match="subject_contract_mismatch"):
        prepare_fact_eligibility(policy=FactPolicyV2.parse(raw), **relabelled,
                                 binding=Binding.parse(raw["binding"]), request_as_of=AS_OF, now=AS_OF)


def test_every_shipped_capability_declares_exactly_one_subject_contract(corpus):
    from topos.permissions_v2.fact_contract import FactPolicyV2, StatedDayFactPolicy
    from topos.permissions_v2.identity import SUBJECT_CONTRACTS
    declared = {model.model_fields["versions"].annotation.model_fields["capability"].annotation.__args__[0]
                for model in (FactPolicyV2, StatedDayFactPolicy)}
    assert declared <= set(SUBJECT_CONTRACT_BY_CAPABILITY)
    assert set(SUBJECT_CONTRACT_BY_CAPABILITY.values()) <= set(SUBJECT_CONTRACTS)
    assert SUBJECT_CONTRACT_BY_CAPABILITY["permissions-beta/p2a-v1"] == LEGACY_CONTRACT
