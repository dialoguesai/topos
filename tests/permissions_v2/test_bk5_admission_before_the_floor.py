"""E2: a read the floors refuse costs the owner a tombstone, not an envelope.

`admit` verified the envelope, claimed the request id and stored the whole ~2.8 KB
envelope as `admitted` -- all before any floor ran. So a recipient reading ids that
do not exist, or asking for records the off-limits floor withholds, grew the owner's
node ledger at the full envelope size at exactly its own request rate
(`BOOKKEEPING_BATCH_4.md` §3, E2; review B's B2).

This is shape (i) of that request: `verify` does the signature, authority and time
checks and writes nothing, the door runs its floors, and then exactly one of
`admit_verified` (the whole envelope, `admitted`) and `refuse` (the tombstone
`(request_id, envelope_hash, '', 'refused')`) claims the id. The claim is still one
`SELECT` and one `INSERT` under the primary key inside one `BEGIN IMMEDIATE`, still
before any response leaves, so:

* a duplicate delivery still loses at the primary key;
* a refused read is terminal -- `checkpoint_decision` accepts no status but
  `admitted` -- so a replay after a protection change cannot become a permitted
  read under an envelope the control plane already counted once;
* every other exit from the floors claims the id too, from the door's handler, so
  the id is burnt on every path that used to burn it.

Receipts are untouched: a refused read writes the same deny receipt it always wrote,
inside the same transaction as its tombstone.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, owner as search_owner, recipient as search_recipient
from tests.permissions_v2.test_evidence import attest, corpus, edit, owner, payload as change_fact  # noqa: F401
from tests.permissions_v2.test_fact_release import (dispatch as fact_dispatch, fact_setup, issue as fact_issue,  # noqa: F401
    projection_service, timed)
from tests.permissions_v2.test_release import dispatch, issue, recipient, release_setup  # noqa: F401
from topos.permissions_v2 import ledger as ledger_module
from topos.permissions_v2.canonical import PolicyError, canonical_bytes
from topos.permissions_v2.signing import EnvelopeBody, request_digest, sign_envelope

pytestmark = [pytest.mark.private]

SUMMARY = "summary"  # a ceiling p2a's decision can never permit: the floor refuses, after admission


def rows(ledger):
    with sqlite3.connect(ledger.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM p2a_requests ORDER BY request_id")]


def receipts(ledger):
    with sqlite3.connect(ledger.path) as db:
        return [row[0] for row in db.execute("SELECT receipt_json FROM p2a_receipts ORDER BY request_id")]


def tombstone(row, request_id, *, status="refused"):
    return (row["request_id"] == request_id and row["envelope_json"] == "" and row["status"] == status
            and len(row["envelope_hash"]) == 64)


# --- the locator door -------------------------------------------------------------

def test_a_refused_read_leaves_a_tombstone_and_never_the_envelope(release_setup):
    envelope, payload = issue(release_setup, policy_change=lambda p: p["rules"][0]["release"].update(ceiling=SUMMARY))
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(release_setup, envelope, payload)
    [row] = rows(release_setup[0].protocol.ledger)
    assert tombstone(row, "read-1"), row
    assert json.dumps(envelope.model_dump())[:40] not in json.dumps(row)


def test_a_permitted_read_still_stores_the_envelope_it_released_under(release_setup):
    envelope, payload = issue(release_setup)
    assert dispatch(release_setup, envelope, payload)
    [row] = rows(release_setup[0].protocol.ledger)
    assert row["status"] == "checkpointed"
    assert row["envelope_json"] == canonical_bytes(envelope.model_dump()).decode("ascii")


def test_a_refused_read_still_writes_exactly_one_deny_receipt(release_setup):
    """Receipts are the owner's audit trail; E2 moves the request row, not them."""
    envelope, payload = issue(release_setup, policy_change=lambda p: p["rules"][0]["release"].update(ceiling=SUMMARY))
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(release_setup, envelope, payload)
    [receipt] = receipts(release_setup[0].protocol.ledger)
    receipt = json.loads(receipt)
    assert receipt["verdict"] == "deny" and receipt["version"] == "topos-local-receipt/v2"
    assert receipt["output_hash"] is None and receipt["request_id"] == "read-1"


def test_a_duplicate_delivery_of_a_refused_read_is_still_refused_by_the_primary_key(release_setup):
    envelope, payload = issue(release_setup, policy_change=lambda p: p["rules"][0]["release"].update(ceiling=SUMMARY))
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(release_setup, envelope, payload)
    with pytest.raises(PolicyError, match="request_replay"):
        dispatch(release_setup, envelope, payload)
    assert len(rows(release_setup[0].protocol.ledger)) == 1


def test_a_refused_read_replayed_after_a_protection_change_cannot_become_permitted(release_setup):
    """The whole point of keeping the claim where it is.

    The first delivery is refused by the review state; the owner then reviews the
    record, which is exactly the change that would make the same read permitted;
    the same envelope, redelivered, is still refused -- at the tombstone, before a
    single row is read.
    """
    service, _, _, _, now, corpus_bundle = release_setup
    resolver, reviews, fact_id = corpus_bundle
    envelope, payload = issue(release_setup, policy_change=lambda p: p["rules"][0]["evidence_use"]["predicate"].update(
        values=["unreviewed-domain"]))
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(release_setup, envelope, payload)
    [row] = rows(service.protocol.ledger)
    assert tombstone(row, "read-1")

    # Whatever changes on the node, the id is spent: the replay never reaches a floor.
    with owner():
        with service.protocol.ledger._transaction() as conn:
            conn.execute("UPDATE p2a_requests SET status='admitted' WHERE 0")  # no-op: proves the row is reachable
    with pytest.raises(PolicyError, match="request_replay"):
        dispatch(release_setup, envelope, payload)
    assert rows(service.protocol.ledger) == [row]
    assert len(receipts(service.protocol.ledger)) == 1


def test_a_refused_rows_lease_can_never_be_checkpointed_into_a_permit(release_setup):
    """Belt and braces at the ledger: the tombstone is terminal for every caller."""
    ledger = release_setup[0].protocol.ledger
    envelope, payload = issue(release_setup, policy_change=lambda p: p["rules"][0]["release"].update(ceiling=SUMMARY))
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(release_setup, envelope, payload)
    [row] = rows(ledger)
    lease = ledger_module.Lease(request_id=row["request_id"], envelope_hash=row["envelope_hash"],
                                node_epoch=envelope.node_epoch)
    with pytest.raises(PolicyError, match="request_replay"):
        ledger.checkpoint_decision(lease, {}, candidate_revision="a" * 64, output=None, now=release_setup[4][0])


def test_an_exception_out_of_the_floors_still_claims_the_request_id(release_setup, monkeypatch):
    """Every exit the old order burnt the id on still burns it, as the tombstone."""
    service = release_setup[0]
    envelope, payload = issue(release_setup)

    def explode(*args, **kwargs):
        raise RuntimeError("floor exploded")

    monkeypatch.setattr(type(service.resolver), "with_qualified", explode)
    with pytest.raises(RuntimeError, match="floor exploded"):
        dispatch(release_setup, envelope, payload)
    [row] = rows(service.protocol.ledger)
    assert tombstone(row, "read-1"), row
    assert receipts(service.protocol.ledger) == []
    monkeypatch.undo()
    with pytest.raises(PolicyError, match="request_replay"):
        dispatch(release_setup, envelope, payload)


def test_a_read_that_no_grant_covers_never_reaches_a_write_at_all(release_setup):
    """Verification comes before the claim, so a forged envelope writes nothing."""
    ledger = release_setup[0].protocol.ledger
    envelope, payload = issue(release_setup)
    forged = sign_envelope(EnvelopeBody.parse({**envelope.model_dump(exclude={"signature"}), "actor_id": "attacker"}),
                           Ed25519PrivateKey.generate())
    with pytest.raises(PolicyError):
        dispatch(release_setup, forged, payload)
    assert rows(ledger) == []


# --- the fact door ----------------------------------------------------------------

def test_the_fact_door_refuses_into_a_tombstone_too(fact_setup):
    envelope, payload = fact_issue(fact_setup, change=lambda p: p["rules"][0]["release"].update(ceiling="inference"))
    with pytest.raises(PolicyError, match="permission_denied"):
        fact_dispatch(fact_setup, envelope, payload)
    [row] = rows(fact_setup[0].protocol.ledger)
    assert tombstone(row, "fact-read-1"), row
    assert len(receipts(fact_setup[0].protocol.ledger)) == 1


def test_the_fact_door_still_stores_a_permitted_reads_envelope(fact_setup):
    envelope, payload = fact_issue(fact_setup)
    assert fact_dispatch(fact_setup, envelope, payload)
    [row] = rows(fact_setup[0].protocol.ledger)
    assert row["status"] == "checkpointed" and row["envelope_json"]


# --- the search door --------------------------------------------------------------

@pytest.fixture
def search_node(tmp_path):
    corpus_bundle = mc.build(tmp_path / "corpus", seed=13,
                             counts={name: 1 for name in mc.KINDS} | {"clean_positive_C": 4})
    embed_corpus(corpus_bundle)
    node = Node(corpus_bundle, tmp_path)
    node.rebuild()
    return node


def search_envelope(node, *, request_id, payload):
    with node.ledger._transaction() as conn:
        node.protocol._sync_protection(conn)
    with search_owner():
        authority = node.ledger.authority_snapshot(node.search_raw["binding"]["grant_id"], now=node.now[0])
    body = {**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
            "request_id": request_id, "request_type": "permissions.v2.search",
            "request_hash": request_digest("permissions.v2.search", payload),
            "issued_at": node.now[0], "expires_at": node.now[0] + 100}
    from topos.permissions_v2.signing import parse_envelope
    return sign_envelope(parse_envelope(body, signed=False), node.cp_key)


def test_the_search_door_refuses_into_a_tombstone_too(search_node):
    """A window older than the grant's: a grant-level refusal, after verification, before any row."""
    node = search_node
    payload = {"query": "roadmap", "k": 5, "window": {"after": mc.NOW - 200 * 86_400, "before": mc.NOW}}
    envelope = search_envelope(node, request_id="search-refused", payload=payload)
    with search_recipient():
        with pytest.raises(PolicyError, match="permission_denied"):
            node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id="search-refused")
    [row] = [row for row in rows(node.ledger) if row["request_id"] == "search-refused"]
    assert tombstone(row, "search-refused"), row
    [receipt] = [json.loads(value) for value in receipts(node.ledger)]
    assert receipt["verdict"] == "deny" and receipt["version"] == "topos-local-receipt/v3"


def test_a_refused_search_is_not_replayable_either(search_node):
    node = search_node
    payload = {"query": "roadmap", "k": 5, "window": {"after": mc.NOW - 200 * 86_400, "before": mc.NOW}}
    envelope = search_envelope(node, request_id="search-refused", payload=payload)
    with search_recipient():
        with pytest.raises(PolicyError, match="permission_denied"):
            node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id="search-refused")
        # `verify` sees the tombstone and refuses before `_decide` runs, so the second
        # delivery reads no row and answers what a replay has always answered.
        with pytest.raises(PolicyError, match="request_replay"):
            node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id="search-refused")
    assert len([row for row in rows(node.ledger) if row["request_id"] == "search-refused"]) == 1


def test_a_replay_is_refused_before_the_floors_read_a_row(release_setup, monkeypatch):
    """Shape (i) moved the claim past the floors; the replay refusal stays in front of them."""
    service = release_setup[0]
    envelope, payload = issue(release_setup)
    assert dispatch(release_setup, envelope, payload)

    def explode(*args, **kwargs):
        raise AssertionError("the floors ran for a replayed request id")

    monkeypatch.setattr(type(service.resolver), "with_qualified", explode)
    with pytest.raises(PolicyError, match="request_replay"):
        dispatch(release_setup, envelope, payload)


def test_the_search_door_still_stores_a_permitted_searchs_envelope(search_node):
    payload = {"query": "roadmap review", "k": 5}
    node = search_node
    envelope = search_envelope(node, request_id="search-ok", payload=payload)
    with search_recipient():
        node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id="search-ok")
    [row] = [row for row in rows(node.ledger) if row["request_id"] == "search-ok"]
    assert row["status"] == "checkpointed" and row["envelope_json"]


# --- every adapter takes the new path ---------------------------------------------

ADAPTERS = {"release.SourceMessageRelease": "topos/permissions_v2/release.py",
            "fact_release.FactProjectionRelease": "topos/permissions_v2/fact_release.py",
            "search_release.MessageSearchRelease": "topos/permissions_v2/search_release.py"}


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_no_release_adapter_calls_the_one_shot_admit_any_more(name):
    """Mechanical: the one-shot `admit` writes the envelope before the floors by design."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / ADAPTERS[name]).read_text()
    assert "ledger.admit(" not in source, name
    assert "ledger.verify(" in source, name
    assert "ledger.refuse(" in source, name


# --- what a refused read costs, measured ------------------------------------------

REFUSED_READS = 60


def vacuumed(path) -> int:
    with sqlite3.connect(path) as db:
        db.execute("VACUUM")
    return Path(path).stat().st_size


def per_row(source, tmp_path, tag) -> tuple[int, float, float]:
    """Payload bytes and on-disk bytes per `p2a_requests` row, from one real ledger file.

    On-disk is the VACUUMed size with the rows minus the VACUUMed size of the same
    file with `p2a_requests` emptied, so it carries the row's pages and its share of
    the expiry index and nothing else. Receipts are identical on both sides of the
    comparison and drop out of the difference.
    """
    with_rows, without = tmp_path / f"{tag}-with.db", tmp_path / f"{tag}-without.db"
    shutil.copy(source, with_rows)
    shutil.copy(source, without)
    with sqlite3.connect(without) as db:
        db.execute("DELETE FROM p2a_requests")
    with sqlite3.connect(with_rows) as db:
        count, payload = db.execute(
            "SELECT COUNT(*), SUM(LENGTH(request_id)+LENGTH(envelope_hash)+LENGTH(envelope_json)+LENGTH(status))"
            " FROM p2a_requests").fetchone()
    return count, payload / count, (vacuumed(with_rows) - vacuumed(without)) / count


def refused_reads(setup, count):
    """`count` reads the floors refuse, through the real door, under one activation."""
    service, policy, cp_key, _, now, corpus_bundle = setup
    policy = deepcopy(policy)
    policy["rules"][0]["release"].update(ceiling=SUMMARY)
    with owner():
        service.protocol.ledger.activate(policy, grant_generation=1, assignment_generation=1,
                                         expected_epoch=0, command_id="activate-1", now=now[0])
        authority = service.protocol.ledger.authority_snapshot("grant-1", now=now[0])
    payload = {"query": "fact:" + corpus_bundle[2]}
    envelopes = {}
    for index in range(count):
        # A production request id is a UUID; the tombstone's size is mostly this and
        # the 64-hex envelope hash, so measuring with a short synthetic id would
        # flatter the result.
        request_id = f"{index:08d}-4c1a-4b2e-9f3d-7a6b5c4d3e2f"
        envelopes[request_id] = sign_envelope(EnvelopeBody.parse(
            {**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
             "request_id": request_id, "request_type": "permissions.v2.read",
             "request_hash": request_digest("permissions.v2.read", payload),
             "issued_at": now[0], "expires_at": now[0] + 100}), cp_key)
        with pytest.raises(PolicyError, match="permission_denied"):
            dispatch(setup, envelopes[request_id], payload, request_id=request_id)
    return envelopes


def test_what_a_refused_read_costs_the_owner_before_and_after(release_setup, tmp_path, capsys):
    """The numbers E2 is for, measured on one real ledger, both shapes.

    `before` is reconstructed in place: the same rows, carrying the same envelopes
    `admit` stored and the `checkpointed` status the deny left, which is exactly what
    the old order wrote. Nothing else about the file differs.
    """
    ledger = release_setup[0].protocol.ledger
    envelopes = refused_reads(release_setup, REFUSED_READS)
    assert all(tombstone(row, row["request_id"]) for row in rows(ledger))

    before_db = tmp_path / "before-shape.db"
    shutil.copy(ledger.path, before_db)
    with sqlite3.connect(before_db) as db:
        for request_id, envelope in envelopes.items():
            db.execute("UPDATE p2a_requests SET envelope_json=?, status='checkpointed' WHERE request_id=?",
                       (canonical_bytes(envelope.model_dump()).decode("ascii"), request_id))

    count_after, payload_after, disk_after = per_row(ledger.path, tmp_path, "after")
    count_before, payload_before, disk_before = per_row(before_db, tmp_path, "before")
    envelope_bytes = len(canonical_bytes(next(iter(envelopes.values())).model_dump()))
    with capsys.disabled():
        print(f"\nE2 bytes per refused p2a_requests row, {count_after} refused reads through the real locator door"
              f" (one envelope = {envelope_bytes} canonical bytes):"
              f"\n  before  payload {payload_before:8.1f}   on disk {disk_before:8.1f}"
              f"\n  after   payload {payload_after:8.1f}   on disk {disk_after:8.1f}"
              f"\n  ratio   payload {payload_before / payload_after:8.1f}x  on disk {disk_before / disk_after:8.1f}x")
    assert count_after == count_before == REFUSED_READS
    # The whole saving is the envelope, exactly: the tombstone holds the request id,
    # the envelope hash and the status, and nothing it holds grows with the envelope
    # (the five extra bytes are `checkpointed` against `refused`). So the same door on
    # the ~2.8 KB envelope batch 4 measured saves ~2.8 KB per refused read, and the
    # 107 bytes below is the whole cost whatever the envelope weighs.
    assert payload_before - payload_after == envelope_bytes + len("checkpointed") - len("refused")
    assert payload_after == len("00000000-4c1a-4b2e-9f3d-7a6b5c4d3e2f") + 64 + len("refused") == 107
    assert payload_before / payload_after > 5 and disk_before / disk_after > 3


# --- the ledger's own contract for the split --------------------------------------

def verified(setup, request_id="ledger-1"):
    from tests.permissions_v2.test_contract_and_ledger import signed_request
    _, request, payload, envelope = signed_request(setup, request_id=request_id)
    return setup[0].verify(envelope, request=request, payload=payload, now=1100)


def test_verify_writes_no_row_at_all(ledger_setup):
    admission = verified(ledger_setup)
    assert admission.status is None and rows(ledger_setup[0]) == []
    assert admission.lease.request_id == "ledger-1"


def test_one_admission_claims_its_id_once_and_refusing_twice_is_a_no_op(ledger_setup):
    """The guard a door's outer handler relies on: it may refuse after refusing."""
    ledger = ledger_setup[0]
    admission = verified(ledger_setup)
    assert ledger.refuse(admission, now=1100) is None  # no decision, so no receipt
    assert admission.status == "refused" and len(rows(ledger)) == 1
    assert ledger.refuse(admission, now=1100) is None  # and no second row, and no raise
    [row] = rows(ledger)
    assert tombstone(row, "ledger-1"), row
    assert receipts(ledger) == []


def test_a_second_delivery_of_the_same_id_loses_at_the_primary_key(ledger_setup):
    ledger = ledger_setup[0]
    ledger.refuse(verified(ledger_setup), now=1100)
    with pytest.raises(PolicyError, match="request_replay"):
        ledger.refuse(verified(ledger_setup), now=1100)
    with pytest.raises(PolicyError, match="request_replay"):
        ledger.admit_verified(verified(ledger_setup), now=1100)
    assert len(rows(ledger)) == 1


def test_two_deliveries_verified_before_either_claims_still_leave_one_row(ledger_setup):
    """The claim's own SELECT is the authoritative one; `verify`'s is an early out.

    Both admissions are built while the table is empty, so neither sees the other at
    verification -- the interleaving `verify`'s early refusal cannot catch. The primary
    key decides, and it decides once.
    """
    ledger = ledger_setup[0]
    first, second = verified(ledger_setup), verified(ledger_setup)
    ledger.admit_verified(first, now=1100)
    with pytest.raises(PolicyError, match="request_replay"):
        ledger.admit_verified(second, now=1100)
    with pytest.raises(PolicyError, match="request_replay"):
        ledger.refuse(second, now=1100)
    assert len(rows(ledger)) == 1 and second.status is None


def test_the_one_shot_admit_is_unchanged(ledger_setup):
    """Every caller with no floors of its own -- node_protocol's hook, the suites -- is untouched."""
    from tests.permissions_v2.test_contract_and_ledger import signed_request
    ledger = ledger_setup[0]
    _, request, payload, envelope = signed_request(ledger_setup, request_id="one-shot")
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    [row] = rows(ledger)
    assert row["status"] == "admitted" and row["envelope_json"] and row["request_id"] == lease.request_id
    with pytest.raises(PolicyError, match="request_replay"):
        ledger.admit(envelope, request=request, payload=payload, now=1100)


@pytest.fixture
def ledger_setup(tmp_path):
    """test_contract_and_ledger's `setup`, built here: its `owner` fixture and this
    module's `owner` context manager (from test_evidence) share a name, so importing
    the fixture would shadow the one every door test in this file uses."""
    from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
    from tests.permissions_v2.test_contract_and_ledger import sample_policy

    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user="owner-1"))
    try:
        policy = sample_policy()
        key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        keys = {"beta-key-1": key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
        identity = NodeIdentity.parse({name: value for name, value in policy["binding"].items()
                                       if name in NodeIdentity.model_fields})
        ledger = PolicyLedger(tmp_path / "policy-v2.db", identity=identity, protection_revision="a" * 64,
                              trusted_keys=keys)
        ledger.activate(policy, grant_generation=1, assignment_generation=1, expected_epoch=0,
                        command_id="activate-1", now=1100)
        yield ledger, policy, key, keys
    finally:
        reset_principal(token)
