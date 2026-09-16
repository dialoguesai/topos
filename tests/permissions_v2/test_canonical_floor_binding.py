"""The external floor as a gate, not a record: on every read, and in the ledger.

`test_canonical_floor.py` covers what the floor itself accepts and refuses.
These cover the two places it is consulted: the resolver, on every read, and the
node protocol, which mirrors it so that removing the file reads as a rollback
rather than as a node that never had one.
"""
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import attest, corpus, decision, owner
from tests.permissions_v2.test_node_protocol import protocol, status_request
from tests.permissions_v2.test_owner_identity_binding import OWNER, db, do_attest
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.canonical_floor import CanonicalFloorStore
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.protection_clock import EVENTS, TABLE


def floor_for(resolver, tmp_path, name="canonical-floor.json"):
    identity = resolver.binding
    store = CanonicalFloorStore(tmp_path / name, owner_id=identity.owner_id, node_id=identity.node_id,
                                resource_id=identity.resource_id)
    with sqlite3.connect(resolver.path) as conn:
        store.install(conn)
    return store


# --- the resolver -----------------------------------------------------------

def test_a_resolver_without_a_floor_reads_exactly_as_before(corpus):
    """A node that never enabled identity attestations is untouched by this."""
    assert corpus[0].canonical_floor is None
    attest(corpus)
    assert decision(corpus).verdict == "qualified"


def test_every_read_checks_the_floor_once_one_exists(corpus, tmp_path):
    corpus[0].canonical_floor = floor_for(corpus[0], tmp_path)
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    # A consent row written outside the attestation handler is exactly what the
    # floor pins, and it must stop a read, not only a write.
    with db(corpus) as conn:
        do_attest(conn, OWNER)
    with pytest.raises(PolicyError, match="identity_ledger_unpinned"):
        with owner():
            corpus[0].inspect_for_review(corpus[2])


def test_a_read_refuses_a_canonical_database_that_went_backwards(corpus, tmp_path):
    corpus[0].canonical_floor = floor_for(corpus[0], tmp_path)
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    with db(corpus) as conn:
        conn.execute(f"UPDATE {TABLE} SET generation=generation+5 WHERE singleton=1")
    with db(corpus) as conn:
        corpus[0].canonical_floor.check(conn)
    with db(corpus) as conn:
        conn.execute(f"UPDATE {TABLE} SET generation=generation-5 WHERE singleton=1")
    with pytest.raises(PolicyError, match="canonical_floor_rollback"):
        with owner():
            corpus[0].inspect_for_review(corpus[2])


def test_a_missing_floor_file_stops_reads_rather_than_reinstalling(corpus, tmp_path):
    corpus[0].canonical_floor = floor_for(corpus[0], tmp_path)
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    (tmp_path / "canonical-floor.json").unlink()
    with pytest.raises(PolicyError, match="canonical_floor_unavailable"):
        with owner():
            corpus[0].inspect_for_review(corpus[2])


# --- the ledger mirror ------------------------------------------------------

def rebuild(protocol_fixture, *, floor):
    node, policy, cp_key, node_key = protocol_fixture
    return NodePolicyProtocol(node.ledger, canonical_database=node.canonical_database, cp_issuer_id="beta-cp",
        frontend_client_id="permissions-beta-web", trusted_cp_keys=node.trusted_cp_keys,
        node_signing_kid="node-key", node_signing_key=node_key, canonical_floor=floor)


def store_for(protocol_fixture, tmp_path, name="node-floor.json"):
    node = protocol_fixture[0]
    identity = node.ledger.identity
    store = CanonicalFloorStore(tmp_path / name, owner_id=identity.owner_id, node_id=identity.node_id,
                                resource_id=identity.resource_id)
    with sqlite3.connect(node.canonical_database) as conn:
        store.install(conn)
    return store


def test_a_node_without_a_floor_records_none_and_keeps_serving(protocol, tmp_path):
    node = protocol[0]
    with node.ledger._transaction() as conn:
        assert conn.execute("SELECT * FROM p2a_canonical_floor WHERE singleton=1").fetchone() is None
    assert node.status(status_request(protocol).model_dump(), now=1100).outcome == "status"


def test_the_first_floor_is_recorded_and_then_required(protocol, tmp_path):
    store = store_for(protocol, tmp_path)
    bound = rebuild(protocol, floor=store)
    with bound.ledger._transaction() as conn:
        recorded = conn.execute("SELECT * FROM p2a_canonical_floor WHERE singleton=1").fetchone()
    assert recorded is not None and recorded["clock_id"] == store._current.clock_id
    assert bound.status(status_request(protocol).model_dump(), now=1100).outcome == "status"
    # The same ledger, now asked to serve with the floor switched off. It is
    # refused at startup rather than at the first request, so a node that lost
    # its floor never reaches the point of signing anything.
    with pytest.raises(PolicyError, match="canonical_floor_unavailable"):
        rebuild(protocol, floor=None)


def test_a_swapped_consent_ledger_stops_the_node_signing_anything(protocol, tmp_path):
    """The floor pins the attestation ledger exactly, and the node checks it
    before it signs. A consent row that appeared any other way is a tamper.
    """
    store = store_for(protocol, tmp_path)
    bound = rebuild(protocol, floor=store)
    assert bound.status(status_request(protocol, request_id="status-2").model_dump(), now=1100).outcome == "status"
    import json
    body = json.loads((tmp_path / "node-floor.json").read_text())
    body["ledger_digest"] = "d" * 64
    (tmp_path / "node-floor.json").write_text(json.dumps(body))
    with pytest.raises(PolicyError, match="identity_ledger_unpinned"):
        rebuild(protocol, floor=reopened(protocol, tmp_path))


def test_a_rolled_back_database_stops_the_node_signing_anything(protocol, tmp_path):
    """Not the floor's job: the protection observation has always caught this,
    and the floor mirror deliberately does not duplicate it.
    """
    from topos.features.lifecycle.record_protection import RecordProtectionStore
    store = store_for(protocol, tmp_path)
    bound = rebuild(protocol, floor=store)
    with sqlite3.connect(bound.canonical_database) as conn:
        RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="record-1")
    bound.status(status_request(protocol, request_id="status-2").model_dump(), now=1100)
    with sqlite3.connect(bound.canonical_database) as conn:
        conn.execute(f"UPDATE {TABLE} SET generation=generation-1 WHERE singleton=1")
    with pytest.raises(PolicyError, match="protection_clock_rollback"):
        bound.status(status_request(protocol, request_id="status-3").model_dump(), now=1100)


def test_a_floor_for_another_node_is_not_this_nodes_floor(protocol, tmp_path):
    store = store_for(protocol, tmp_path)
    rebuild(protocol, floor=store)
    other = CanonicalFloorStore(tmp_path / "node-floor.json", owner_id="owner-1", node_id="somewhere-else",
                                resource_id=protocol[0].ledger.identity.resource_id)
    with pytest.raises(PolicyError, match="canonical_floor_unavailable"):
        rebuild(protocol, floor=other)


def reopened(protocol_fixture, tmp_path, name="node-floor.json"):
    """A fresh store over the same file, as a restarted process would build."""
    identity = protocol_fixture[0].ledger.identity
    return CanonicalFloorStore(tmp_path / name, owner_id=identity.owner_id, node_id=identity.node_id,
                               resource_id=identity.resource_id)


def test_a_floor_removed_while_the_node_runs_stops_the_next_signature(protocol, tmp_path):
    """Checked on every protection sync, not only at startup.

    A node that has been serving for days is exactly where a floor would go
    missing, and startup is long past by then.
    """
    store = store_for(protocol, tmp_path)
    bound = rebuild(protocol, floor=store)
    assert bound.status(status_request(protocol, request_id="status-2").model_dump(), now=1100).outcome == "status"
    (tmp_path / "node-floor.json").unlink()
    with pytest.raises(PolicyError, match="canonical_floor_unavailable"):
        bound.status(status_request(protocol, request_id="status-3").model_dump(), now=1100)
