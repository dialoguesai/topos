"""Block D of the isolation battery, the node's half (isolation charter S0 §7.2; claims C9, C12, C13).

The control plane's half lives in the control-plane repository (tests/control_plane/s1/test_s1_keys_and_bind.py).

* O9.2: an envelope, a batch, a mutation, a status request, an identity command and an ingest command addressed to
  node a, delivered to node b, are refused by node b, for the ordered owner pairs (a, b), (b, c) and (c, a); node b's
  ledger does not move. One control-plane key signs for every node (A2A-1 §3.8.3), so only the node's own identity
  keeps another node's command out.
* The search door builds a recipient's request from its own identity, never from the envelope (mutant M33): an
  envelope addressed to another node is refused there.
* A command stamped as the owner's app for another owner is refused by the evidence owner check (mutant M34), on a
  path that calls `evidence._owner` (the evidence handlers keep their own check, so it is not one of those).
* O12's replays at the wire module: a proof for one owner's bind never verifies for another owner's bind, nor for a
  later bind of the same owner; a bind signed by another control plane's stamp key, or past its expiry, is refused.
  The node's whole handler (owner, Topos, signature, expiry refusals writing nothing) is N2's
  `test_self_bind.py::test_each_refusal_writes_nothing`, which the S1 mutant group names too.

Everything is synthetic: invented owners, nodes and Topoi; keys are made at run time.
"""
from __future__ import annotations

import copy
import hashlib
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.permissions_v2.test_contract_and_ledger import sample_policy
from topos.permissions_v2 import bind_protocol as bp
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.identity_protocol import IdentityCommandBody, sign_identity_command, verify_identity_command
from topos.permissions_v2.ingest_protocol import IngestCommandBody, sign_ingest_command, verify_ingest_command
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.protection_clock import current_protection_revision, ensure_protection_clock
from topos.permissions_v2.protocol import MutationBody, StatusRequestBody, sign_mutation, sign_status_request
from topos.permissions_v2.signing import EnvelopeBody, RequestContext, request_digest, sign_envelope
from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
from topos.storage.db.migrations.owner_only_records_v1 import apply_owner_only_records_v1_up
from topos.storage.db.migrations.wiki_lifecycle_v1 import apply_wiki_lifecycle_v1_up

ENV, CP_ISSUER, CP_KID, FRONTEND = "permissions-beta-s1node", "cp-s1node", "cp-key-s1node", "beta-ui-s1node"
LETTERS = ("a", "b", "c")
PAIRS = [("a", "b"), ("b", "c"), ("c", "a")]
NOW = 1100


def owner_of(letter: str) -> str:
    return f"owner-{letter}"


def binding_of(letter: str) -> dict:
    return {"environment_id": ENV, "node_id": f"node-{letter}-s1", "resource_id": f"topos-{letter}-s1",
            "owner_id": owner_of(letter), "actor_id": "actor-1", "client_id": "client-1",
            "grant_id": f"grant-{letter}", "assignment_id": f"assignment-{letter}"}


def policy_of(letter: str) -> dict:
    policy = sample_policy()
    policy["binding"] = binding_of(letter)
    policy["policy_version_id"] = f"policy-{letter}"
    return policy


class Node:
    """One owner's node: its database with the protection clock, its ledger, its protocol and its own key."""

    def __init__(self, tmp_path, letter: str, cp_key: Ed25519PrivateKey):
        self.letter, self.owner = letter, owner_of(letter)
        canonical = tmp_path / f"canonical-{letter}.db"
        with sqlite3.connect(canonical) as conn:
            conn.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
            apply_owner_only_records_v1_up(conn)
            apply_entity_blackhole_v1_up(conn)
            apply_wiki_lifecycle_v1_up(conn)
            conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO engine_config VALUES ('user_id', ?)", (self.owner,))
            conn.execute("CREATE TABLE conversation_messages(message_id TEXT PRIMARY KEY, source_id TEXT, content TEXT)")
            conn.execute("INSERT INTO conversation_messages VALUES ('record-1','source-A','synthetic')")
            conn.commit()
        ensure_protection_clock(canonical, owner_id=self.owner)
        with sqlite3.connect(canonical) as conn:
            conn.execute("BEGIN")
            floor = current_protection_revision(conn, owner_id=self.owner)
        self.keys = {CP_KID: cp_key.public_key().public_bytes_raw()}
        self.identity = NodeIdentity.parse({k: v for k, v in binding_of(letter).items() if k in NodeIdentity.model_fields})
        self.ledger = PolicyLedger(tmp_path / f"ledger-{letter}.db", identity=self.identity, protection_revision=floor,
                                   trusted_keys=self.keys)
        self.protocol = NodePolicyProtocol(self.ledger, canonical_database=canonical, cp_issuer_id=CP_ISSUER,
                                           frontend_client_id=FRONTEND, trusted_cp_keys=self.keys,
                                           node_signing_kid=f"nk-{letter}", node_signing_key=Ed25519PrivateKey.generate())
        self.policy = policy_of(letter)

    def rows(self) -> dict:
        """Every table of the node's ledger, as rows (what a refused command must not move)."""
        conn = sqlite3.connect(self.ledger.path.as_uri() + "?mode=ro", uri=True)
        try:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {table: sorted(map(repr, conn.execute(f'SELECT * FROM "{table}"').fetchall())) for table in tables}
        finally:
            conn.close()


@pytest.fixture
def nodes(tmp_path):
    cp_key = Ed25519PrivateKey.generate()
    made = {letter: Node(tmp_path, letter, cp_key) for letter in LETTERS}
    for node in made.values():          # each node holds its own owner's grant, active
        command = mutation_for(node, cp_key)
        ack = node.protocol.mutate(command.model_dump(), now=NOW)
        assert ack.outcome == "applied", ack
    return made, cp_key


# ---- what the control plane signs for one node (one CP key for every node) --------------------------------------

def mutation_for(node: Node, cp_key, *, command_id: str = "activate-1"):
    policy = copy.deepcopy(node.policy)
    with node.ledger._transaction() as conn:
        floor = node.ledger._node(conn)["protection_revision"]
        epoch = node.ledger._node(conn)["epoch"]
    authority = {**policy["binding"], "grant_generation": 1, "assignment_generation": 1,
                 "policy_version_id": policy["policy_version_id"], "policy_hash": digest(policy),
                 "capability_version": policy["versions"]["capability"], "protection_revision": floor,
                 "node_epoch": epoch + 1}
    return sign_mutation(MutationBody.parse({
        "version": "topos-policy-mutation/v2", "kid": CP_KID, "issuer_id": CP_ISSUER, "audience_id": node.identity.node_id,
        "command_id": command_id, "operation": "activate", "expected_epoch": epoch, "authority": authority,
        "policy": policy, "owner_authorization": {"actor_id": node.owner, "client_id": FRONTEND},
        "issued_at": NOW, "expires_at": NOW + 120}), cp_key)


def status_for(node: Node, cp_key):
    return sign_status_request(StatusRequestBody.parse({
        "version": "topos-policy-status-request/v2", "kid": CP_KID, "issuer_id": CP_ISSUER,
        "audience_id": node.identity.node_id, "request_id": f"status-{node.letter}", "binding": node.policy["binding"],
        "command_id": None, "command_hash": None, "issued_at": NOW, "expires_at": NOW + 120}), cp_key)


def envelope_for(node: Node, cp_key, *, request_id: str):
    with owner_stamp(node.owner):
        authority = node.ledger.authority_snapshot(node.policy["binding"]["grant_id"], now=NOW)
    payload = {"question": "synthetic", "limit": 1}
    body = {**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": CP_KID, "request_id": request_id,
            "request_type": "permissions.v2.preview", "request_hash": request_digest("permissions.v2.preview", payload),
            "issued_at": NOW, "expires_at": NOW + 100}
    return sign_envelope(EnvelopeBody.parse(body), cp_key).model_dump(), payload


def identity_command_for(node: Node, cp_key):
    return sign_identity_command(IdentityCommandBody.parse({
        "version": "topos-owner-identity-command/v1", "kid": CP_KID, "issuer_id": CP_ISSUER,
        "audience_id": node.identity.node_id, "command_id": f"identity-{node.letter}",
        "binding": node.identity.model_dump(), "owner_authorization": {"actor_id": node.owner, "client_id": FRONTEND},
        "request": {"operation": "describe"}, "issued_at": NOW, "expires_at": NOW + 120}), cp_key)


def ingest_command_for(node: Node, cp_key):
    return sign_ingest_command(IngestCommandBody.parse({
        "version": "topos-owner-ingest-command/v2", "kid": CP_KID, "issuer_id": CP_ISSUER,
        "audience_id": node.identity.node_id, "command_id": f"ingest-{node.letter}",
        "binding": node.identity.model_dump(), "owner_authorization": {"actor_id": node.owner, "client_id": FRONTEND},
        "request": {"operation": "status", "job_id": "job-s1"}, "issued_at": NOW, "expires_at": NOW + 120}), cp_key)


class owner_stamp:
    def __init__(self, owner: str, channel: str = "uds"):
        self.principal = Principal(cls=OWNER_APP, channel=channel, acting_user=owner)

    def __enter__(self):
        self.token = set_principal(self.principal)

    def __exit__(self, *exc):
        reset_principal(self.token)


def refused(call) -> str:
    """The refusal a node gives a command: the code it raises, or the rejected ACK's reason. Never an acceptance."""
    try:
        result = call()
    except PolicyError as exc:
        return exc.code
    outcome = getattr(result, "outcome", None)
    assert outcome in {"rejected"}, f"accepted: {outcome}"
    return result.reason_code


# ---- O9.2: commands addressed to node a are refused by node b ------------------------------------------------------

KINDS = ["envelope", "batch", "mutation", "status", "identity_command", "ingest_command"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("addressed,delivered_to", PAIRS)
def test_D_a_command_addressed_to_one_node_is_refused_by_another_and_moves_nothing(nodes, kind, addressed,
                                                                                    delivered_to):
    made, cp_key = nodes
    target, other = made[addressed], made[delivered_to]
    before = other.rows()
    if kind == "mutation":
        code = refused(lambda: other.protocol.mutate(mutation_for(target, cp_key, command_id="activate-x").model_dump(),
                                                     now=NOW))
    elif kind == "status":
        code = refused(lambda: other.protocol.status(status_for(target, cp_key).model_dump(), now=NOW))
    elif kind in {"envelope", "batch"}:
        codes = []
        for index in range(1 if kind == "envelope" else 2):
            envelope, payload = envelope_for(target, cp_key, request_id=f"request-{kind}-{index}")
            # The node's door builds the request from ITS OWN identity (search_release, release), never the envelope's.
            own = RequestContext.parse({**other.identity.model_dump(), "actor_id": "actor-1", "client_id": "client-1",
                                        "grant_id": target.policy["binding"]["grant_id"],
                                        "assignment_id": target.policy["binding"]["assignment_id"],
                                        "request_id": f"request-{kind}-{index}",
                                        "request_type": "permissions.v2.preview"})
            codes.append(refused(lambda: other.ledger.verify(envelope, request=own, payload=payload, now=NOW)))
            # And a request that claims the envelope's node is refused by the ledger's own identity check.
            theirs = RequestContext.parse({**own.model_dump(), **target.identity.model_dump()})
            assert refused(lambda: other.ledger.verify(envelope, request=theirs, payload=payload, now=NOW)) == (
                "request_binding")
        code = codes[0]
    elif kind == "identity_command":
        code = refused(lambda: verify_identity_command(identity_command_for(target, cp_key).model_dump(),
                                                       trusted_keys=other.keys, issuer_id=CP_ISSUER,
                                                       identity=other.identity, frontend_client_id=FRONTEND, now=NOW))
    else:
        code = refused(lambda: verify_ingest_command(ingest_command_for(target, cp_key).model_dump(),
                                                     trusted_keys=other.keys, issuer_id=CP_ISSUER,
                                                     identity=other.identity, frontend_client_id=FRONTEND, now=NOW))
    assert code, kind
    assert other.rows() == before, f"{kind} for node {addressed} moved node {delivered_to}'s ledger"


def test_D_each_command_is_accepted_by_its_own_node(nodes):
    """The positive control: the same commands, delivered to the node they name, are accepted."""
    made, cp_key = nodes
    for node in made.values():
        assert node.protocol.status(status_for(node, cp_key).model_dump(), now=NOW).outcome == "status"
        envelope, payload = envelope_for(node, cp_key, request_id="request-own")
        own = RequestContext.parse({**node.policy["binding"], "request_id": "request-own",
                                    "request_type": "permissions.v2.preview"})
        assert node.ledger.verify(envelope, request=own, payload=payload, now=NOW)
        assert verify_identity_command(identity_command_for(node, cp_key).model_dump(), trusted_keys=node.keys,
                                       issuer_id=CP_ISSUER, identity=node.identity, frontend_client_id=FRONTEND, now=NOW)
        assert verify_ingest_command(ingest_command_for(node, cp_key).model_dump(), trusted_keys=node.keys,
                                     issuer_id=CP_ISSUER, identity=node.identity, frontend_client_id=FRONTEND, now=NOW)


# ---- the search door's request comes from the node's own identity (M33) ----------------------------------------

def test_D_the_search_door_refuses_an_envelope_addressed_to_another_node(tmp_path):
    """A recipient's search envelope signed by the trusted control-plane key for another node (the same grant ids,
    another node id): the node's search door refuses it, and its ledger does not move."""
    from tests.permissions_v2 import message_search_corpus as mc
    from tests.permissions_v2.message_search_harness import embed_corpus, recipient
    from tests.permissions_v2.message_search_harness import Node as SearchNode
    corpus = mc.build(tmp_path / "corpus", seed=7, counts={"clean_positive_C": 4})
    embed_corpus(corpus)
    node = SearchNode(corpus, tmp_path / "node")
    node.rebuild()
    output, reason = node.search_request("roadmap")
    assert reason is None and output is not None, reason
    grant = node.search_raw["binding"]["grant_id"]
    payload = {"query": "roadmap", "k": 5}
    honest = node._envelope(grant, "permissions.v2.search", payload, "search-misaddressed")
    from topos.permissions_v2.signing import parse_envelope
    elsewhere = parse_envelope({**honest.model_dump(exclude={"signature"}), "node_id": "node-elsewhere-s1"},
                               signed=False)
    forged = sign_envelope(elsewhere, node.cp_key)
    before = ledger_rows(node.ledger.path)
    with recipient():
        with pytest.raises(PolicyError):
            node.search.dispatch(envelope=forged.model_dump(), payload=payload, request_id="search-misaddressed")
    assert ledger_rows(node.ledger.path) == before


def ledger_rows(path) -> dict:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: sorted(map(repr, conn.execute(f'SELECT * FROM "{table}"').fetchall())) for table in tables}
    finally:
        conn.close()


# ---- an owner command for another owner is refused by the evidence owner check (M34) ----------------------------

def test_D_an_owner_stamp_for_another_owner_cannot_change_this_owners_reviews(tmp_path):
    """`EvidenceReviewStore.opt_out` (it calls `evidence._owner`): stamped as the owner's app over the relay but acting
    for another owner, refused `owner_authority_required` and nothing moves; the node's own owner can."""
    from tests.permissions_v2 import message_search_corpus as mc
    corpus = mc.build(tmp_path / "corpus", seed=11, counts={"clean_positive_C": 3})
    fact_id = next(unit.fact_id for unit in corpus.units if getattr(unit, "fact_id", None))
    before = ledger_rows(corpus.reviews.path) if hasattr(corpus.reviews, "path") else None
    with owner_stamp("owner-elsewhere-s1", channel="cp_relay"):
        with pytest.raises(PolicyError, match="owner_authority_required"):
            corpus.reviews.opt_out(fact_id, now=mc.NOW)
    if before is not None:
        assert ledger_rows(corpus.reviews.path) == before
    with owner_stamp(mc.OWNER_ID, channel="cp_relay"):
        assert corpus.reviews.opt_out(fact_id, now=mc.NOW) in {True, False}


# ---- O12 at the wire module: proofs and binds are tied to one bind, one owner, one control plane -----------------

def derived(label: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode("ascii")).digest())


STAMP, OTHER_STAMP, CP = derived("S1 node: stamp key"), derived("S1 node: another CP's stamp key"), derived("S1 node: cp")


def bind_for(owner: str, topos: str, *, node_id=None, new_key_allowed=True, issued_at=None, label="1") -> bp.SignedBind:
    now = int(time.time()) if issued_at is None else issued_at
    return bp.sign_bind(bp.BindBody.parse({
        "version": "topos-node-bind/v1", "request_id": f"s1-bind-{owner}-{label}", "environment_id": ENV,
        "resource_id": topos, "owner_id": owner, "node_id": node_id, "new_key_allowed": new_key_allowed,
        "cp_issuer_id": CP_ISSUER, "trusted_cp_keys": {CP_KID: CP.public_key().public_bytes_raw().hex()},
        "frontend_client_id": FRONTEND, "nonce": hashlib.sha256(f"{owner}{topos}{label}".encode()).hexdigest(),
        "issued_at": now, "expires_at": now + 120}), STAMP)


def proof_for(bind: bp.SignedBind, key: Ed25519PrivateKey, node_id: str, outcome: str = "bound"):
    return bp.sign_bind_proof(bind=bind, node_id=node_id, node_key=key,
                              kid=bp.node_key_id(key.public_key().public_bytes_raw()), outcome=outcome,
                              engine_version="1.5.0", now=int(time.time()))


@pytest.mark.parametrize("other", ["another_owners_bind", "a_later_bind_of_the_same_owner"])
def test_D_a_proof_for_one_bind_never_verifies_for_another(other):
    key = Ed25519PrivateKey.generate()
    first = bind_for("owner-a", "topos-a-s1")
    proof = proof_for(first, key, "node_" + "a1" * 16)
    assert bp.verify_bind_proof(proof.model_dump(), bind=first, now=int(time.time())).outcome == "bound"
    second = (bind_for("owner-b", "topos-b-s1") if other == "another_owners_bind"
              else bind_for("owner-a", "topos-a-s1", label="2"))
    with pytest.raises(PolicyError, match="proof_binding"):
        bp.verify_bind_proof(proof.model_dump(), bind=second, now=int(time.time()))


def test_D_a_bind_signed_by_another_control_planes_stamp_key_is_refused():
    bind = bp.sign_bind(bp.BindBody.parse(bind_for("owner-a", "topos-a-s1").model_dump(exclude={"signature"})),
                        OTHER_STAMP)
    with pytest.raises(PolicyError, match="bind_signature_invalid"):
        bp.verify_bind(bind.model_dump(), stamp_public_key=STAMP.public_key().public_bytes_raw(),
                       message_id=bind.request_id, now=int(time.time()))


def test_D_an_expired_bind_and_another_frames_bind_are_refused():
    bind = bind_for("owner-a", "topos-a-s1", issued_at=int(time.time()) - 600)
    with pytest.raises(PolicyError, match="bind_expired"):
        bp.verify_bind(bind.model_dump(), stamp_public_key=STAMP.public_key().public_bytes_raw(),
                       message_id=bind.request_id, now=int(time.time()))
    fresh = bind_for("owner-a", "topos-a-s1", label="3")
    with pytest.raises(PolicyError, match="bind_frame_mismatch"):
        bp.verify_bind(fresh.model_dump(), stamp_public_key=STAMP.public_key().public_bytes_raw(),
                       message_id="another-frame", now=int(time.time()))


def test_D_a_key_made_when_the_bind_allowed_none_is_refused():
    bind = bind_for("owner-a", "topos-a-s1", node_id="node_" + "a1" * 16, new_key_allowed=False)
    proof = proof_for(bind, Ed25519PrivateKey.generate(), "node_" + "a1" * 16, outcome="bound")
    with pytest.raises(PolicyError, match="proof_new_key_not_allowed"):
        bp.verify_bind_proof(proof.model_dump(), bind=bind, now=int(time.time()))


def test_D_a_proof_for_a_bind_with_this_frame_id_and_another_nonce_is_refused():
    """Mutant M18: the proof binds the bind's nonce and hash, not only its frame id."""
    key = Ed25519PrivateKey.generate()
    bind = bind_for("owner-a", "topos-a-s1")
    earlier = bp.sign_bind(bp.BindBody.parse({**bind.model_dump(exclude={"signature"}), "nonce": "e1" * 32}), STAMP)
    proof = proof_for(earlier, key, "node_" + "a1" * 16)
    with pytest.raises(PolicyError, match="proof_binding"):
        bp.verify_bind_proof(proof.model_dump(), bind=bind, now=int(time.time()))
