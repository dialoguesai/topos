"""The only writer of the consent ledger, and what it refuses to write."""
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import corpus, owner
from tests.permissions_v2.test_owner_identity_binding import OWNER, add_entity, db, generation
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.canonical_floor import CanonicalFloorStore
from topos.permissions_v2.identity import ATTESTED_CONTRACT, entries, permit_subjects
from topos.permissions_v2.identity_attestation import IdentityAttestationService
from topos.permissions_v2.identity_protocol import (ATTESTATION_SENTENCE, AttestIdentity, DescribeIdentity,
    RevokeIdentity)
from topos.permissions_v2.protection_clock import LEDGER


@pytest.fixture
def service(corpus, tmp_path):
    floor = CanonicalFloorStore(tmp_path / "canonical-floor.json", owner_id="owner-1", node_id="node-1",
                                resource_id="resource-1")
    with db(corpus) as conn:
        floor.install(conn)
    return IdentityAttestationService(resolver=corpus[0], floor=floor), corpus, floor


def state(service):
    with owner():
        return service[0].describe(DescribeIdentity())


def subject(result, entity_id):
    return next(item for item in result.subjects if item.entity_id == entity_id)


def attest_request(result, entity_id, *, replaces=None):
    found = subject(result, entity_id)
    return AttestIdentity(entity_id=entity_id, statement_version="owner-identity-attestation/v1",
        statement=ATTESTATION_SENTENCE, expected_entity_type=found.entity_type, expected_is_self=1,
        expected_contact_id=found.contact_id, expected_composition_revision=found.composition_revision,
        replaces_entry_id=replaces)


def do(service, request, *, command="command-1"):
    with owner():
        method = {"attest": service[0].attest, "revoke": service[0].revoke}[request.operation]
        return method(request, command_id=command, command_hash=digest({"command": command}))


def test_describe_shows_the_owners_own_spellings_and_never_a_name(service):
    result = state(service)
    assert result.contract == ATTESTED_CONTRACT
    assert [item.entity_id for item in result.subjects] == [OWNER]
    assert subject(result, OWNER).basis == "installed" and subject(result, OWNER).is_self
    assert subject(result, OWNER).entry_state is None
    # Only the literal subject is permitted before anything is attested.
    assert result.permitted_count == 1 and result.restricted_count >= 1
    text = result.model_dump_json()
    for name in ("Owner", "owner-1", "canonical_name", "aliases"):
        assert name not in text or name == "owner-1"


def test_describe_never_writes(service):
    with db(service[1]) as conn:
        before = (generation(conn), conn.execute(f"SELECT count(*) FROM {LEDGER}").fetchone()[0])
    state(service)
    state(service)
    with db(service[1]) as conn:
        assert (generation(conn), conn.execute(f"SELECT count(*) FROM {LEDGER}").fetchone()[0]) == before


def test_an_attestation_is_recorded_and_takes_effect(service):
    result = do(service, attest_request(state(service), OWNER))
    assert result.entity_id == OWNER and result.state == "active"
    with db(service[1]) as conn:
        assert entries(conn)[OWNER].entry_id == result.entry_id
        assert permit_subjects(conn, contract=ATTESTED_CONTRACT) == {"self", OWNER}
    after = state(service)
    assert subject(after, OWNER).entry_state == "active" and after.permitted_count == 2


def test_an_entity_that_moved_since_the_owner_looked_is_refused(service):
    request = attest_request(state(service), OWNER)
    with db(service[1]) as conn:
        conn.execute("UPDATE entities SET contact_id='contact-9' WHERE entity_id=?", (OWNER,))
    with pytest.raises(PolicyError, match="identity_subject_moved"):
        do(service, request)
    with db(service[1]) as conn:
        assert conn.execute(f"SELECT count(*) FROM {LEDGER}").fetchone()[0] == 0
    # And the floor reopened, because nothing was written.
    state(service)


def test_an_entity_the_node_does_not_have_is_refused(service):
    request = attest_request(state(service), OWNER).model_copy(update={"entity_id": "not-an-entity"})
    with pytest.raises(PolicyError, match="identity_subject_unknown"):
        do(service, request)
    state(service)


def test_a_replayed_command_is_refused(service):
    do(service, attest_request(state(service), OWNER))
    do(service, RevokeIdentity(entity_id=OWNER, entry_id=state(service).subjects[0].entry_id), command="command-2")
    with pytest.raises(PolicyError, match="identity_command_replayed"):
        do(service, attest_request(state(service), OWNER), command="command-1")


def test_confirming_a_quarantined_entry_retires_the_one_it_replaces(service):
    first = do(service, attest_request(state(service), OWNER))
    with db(service[1]) as conn:
        add_entity(conn, "unrelated-self")
        conn.execute("INSERT INTO entity_mentions(mention_id,entity_id,record_id,surface_text) "
                     "VALUES('m-1','unrelated-self','message-1','x')")
    with db(service[1]) as conn:
        conn.execute("UPDATE entity_mentions SET entity_id=? WHERE mention_id='m-1'", (OWNER,))
    quarantined = state(service)
    assert subject(quarantined, OWNER).entry_state == "stale"
    # Without naming the entry it replaces, the confirmation is refused.
    with pytest.raises(PolicyError, match="identity_attestation_conflict"):
        do(service, attest_request(quarantined, OWNER), command="command-2")
    again = do(service, attest_request(quarantined, OWNER, replaces=first.entry_id), command="command-3")
    assert again.entry_id != first.entry_id
    with db(service[1]) as conn:
        assert entries(conn)[OWNER].state == "active"
        actions = [row[0] for row in conn.execute(f"SELECT action FROM {LEDGER} ORDER BY sequence")]
    assert actions == ["attest", "revoke", "attest"]


def test_revocation_needs_the_entry_the_owner_is_looking_at(service):
    entry = do(service, attest_request(state(service), OWNER))
    with pytest.raises(PolicyError, match="identity_attestation_conflict"):
        do(service, RevokeIdentity(entity_id=OWNER, entry_id="entry-something-else"), command="command-2")
    result = do(service, RevokeIdentity(entity_id=OWNER, entry_id=entry.entry_id), command="command-3")
    assert result.state == "revoked"
    with db(service[1]) as conn:
        assert OWNER not in permit_subjects(conn, contract=ATTESTED_CONTRACT)


def test_the_floor_adopts_the_new_ledger_exactly_once(service, tmp_path):
    _service, corpus, floor = service
    do(service, attest_request(state(service), OWNER))
    with db(corpus) as conn:
        adopted = floor.check(conn)
    assert adopted.state == "active" and adopted.ledger_sequence == 1
    # A row written outside the service still fails every read closed. The
    # clock refuses an invented generation outright, so this one uses the real
    # next generation, which is exactly what a determined writer would do.
    live = state(service).subjects[0].entry_id
    with db(corpus) as conn:
        conn.execute(f"INSERT INTO {LEDGER}(entry_id,action,entity_id,target_entry_id,statement_version,"
                     "command_id,command_hash,generation) VALUES('smuggled','revoke',?,?,?,?,?,"
                     f"(SELECT generation+1 FROM permissions_v2_protection_state WHERE singleton=1))",
                     (OWNER, live, "owner-identity-attestation/v1", "command-smuggled", "0" * 64))
    with db(corpus) as conn, pytest.raises(PolicyError, match="identity_ledger_unpinned"):
        floor.check(conn)


def test_only_the_owner_may_read_or_write_identity_state(service):
    with pytest.raises(PolicyError, match="owner_authority_required"):
        service[0].describe(DescribeIdentity())
    with pytest.raises(PolicyError, match="owner_authority_required"):
        service[0].attest(attest_request(state(service), OWNER), command_id="c", command_hash="0" * 64)
    with pytest.raises(PolicyError, match="owner_authority_required"):
        service[0].revoke(RevokeIdentity(entity_id=OWNER, entry_id="entry-x"), command_id="c",
                          command_hash="0" * 64)


def test_a_different_sentence_is_a_different_statement(service):
    """The owner confirms one exact sentence. A paraphrase is not that consent."""
    raw = attest_request(state(service), OWNER).model_dump()
    with pytest.raises(ValueError):
        AttestIdentity.parse(raw | {"statement": "I agree."})
    with pytest.raises(ValueError):
        AttestIdentity.parse(raw | {"statement_version": "owner-identity-attestation/v2"})
    # And the service re-parses, so a hand-built model never reaches the ledger.
    with pytest.raises(Exception):
        do(service, AttestIdentity.model_construct(**(raw | {"statement": "I agree."})))


def test_the_sentence_in_the_contract_is_the_sentence_the_constant_names(service):
    """Edit one without the other and old entries mean something they did not."""
    from topos.permissions_v2 import identity, identity_protocol, protection_clock
    assert AttestIdentity.model_fields["statement"].annotation.__args__ == (
        identity_protocol.ATTESTATION_SENTENCE,)
    # Three copies of the statement version: the table CHECK that pins every
    # ledger row, the wire contract the control plane mirrors, and the identity
    # module. They are separate because the control plane has no clock, so
    # nothing but this test stops them drifting apart.
    assert (identity_protocol.ATTESTATION_STATEMENT == protection_clock.ATTESTATION_STATEMENT
            == identity.ATTESTATION_STATEMENT)
    assert AttestIdentity.model_fields["statement_version"].annotation.__args__ == (
        protection_clock.ATTESTATION_STATEMENT,)
    assert f"statement_version='{protection_clock.ATTESTATION_STATEMENT}'" in protection_clock.LEDGER_SQL
    assert state(service).statement_version == protection_clock.ATTESTATION_STATEMENT
