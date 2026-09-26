"""p2a-v2: raw message release on the owner-attested subject rule the fact labels use.

p2a-v1 decides whose messages it may release with the frozen legacy rule
(exactly one ``is_self`` row, never an attestation), while p2b-v3/v4 read what
the owner attested. So withdrawing an attestation stopped label grants but not
raw grants, a second ``is_self`` row stopped only raw grants, and a node with
several self rows could never release a raw message. p2a-v2 is the p2a-v1
grammar and view with the p2b-v3 subject block; p2a-v1 is unchanged.

Doors: these cases drive the real ``SourceMessageRelease`` (and once the
shipped socket transport) over the evidence corpus with a real ledger and real
signatures, as test_release does. Identity state on the corpus is written with
test_owner_identity_binding's helpers, under the clock's triggers. The same
cases through every signed owner and CP door are in
test_source_release_attested_lane.

The schema exports pinned here are written by tests.permissions_v2.source_attested_schemas.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.permissions_v2.source_attested_schemas import (ATTESTED_FIXTURES, ATTESTED_MODELS, FIXTURES, PROTOCOL_MODELS,
    export)
from tests.permissions_v2.test_contract_and_ledger import sample_policy
from tests.permissions_v2.test_evidence import attest, corpus, owner, payload as change_fact  # noqa: F401 (fixture)
from tests.permissions_v2.test_fact_attested_subject import SUBJECT_BINDING
from tests.permissions_v2.test_fact_work_family import work_policy
from tests.permissions_v2.test_owner_identity_binding import OWNER, add_entity, db, do_attest, do_revoke
from tests.permissions_v2.test_release import dispatch
from tests.permissions_v2.test_release_transport import Socket
from topos.permissions_v2 import fact_contract, release_transport
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.contract import Decision, MessageDisclosure, PolicyV2, capability_document
from topos.permissions_v2.forwarding import verify_node_result
from topos.permissions_v2.identity import ATTESTED_CONTRACT, LEGACY_CONTRACT, SUBJECT_CONTRACT_BY_CAPABILITY, entries
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.registry import (AttestedSubjectSourceDecision, AttestedSubjectSourcePolicy, parse_decision,
    parse_disclosure, parse_policy)
from topos.permissions_v2.release import SourceMessageRelease, VOCABULARY, parse_source_envelope, source_message_decision
from topos.permissions_v2.signing import (AttestedSourceAuthorityBinding, AuthorityBinding,
    RequestContext, SignedAttestedSourceEnvelope, SignedEnvelope, SignedFactEnvelope, parse_authority,
    parse_envelope, request_digest, sign_envelope, verify_current_signature)

V1, V2 = "permissions-beta/p2a-v1", "permissions-beta/p2a-v2"
SECOND = "second-self"
MESSAGE = "I enjoy reading history books."
# sha256 of the p2a-v1 exports as committed before p2a-v2 existed (engine 2c95177).
# The frontend pins the same six bytes in its own provenance file.
#
# PolicyV2's value moved once, in bookkeeping batch 5 (E1), when the grammar gained
# the optional `read_budget_per_day`. No p2a-v1 DOCUMENT changed: the key is omitted
# when undeclared, so `golden-v1.json` below still hashes to its pinned value and
# every signature over a p2a-v1 policy still verifies. What moved is the schema
# document, which now describes one more accepted key.
# test_bk5_read_budget_in_policy pins the old bytes from the other side: strip that
# one property back out of this file and it is the value that shipped, exactly.
# The frontend's provenance file carries the old hash and must be regenerated with
# this change; that is a merge-gate item, not an engine one.
FROZEN_V1 = {
    "PolicyV2.schema.json": "10559c55aba4c122078fdd62fe9403d913ef361eed8f2ef9ab79f0ab9367f14b",
    "Decision.schema.json": "dd5e317d70dfa1b52cf3fa46d160ac0525e4e7c573fc89496f4ada6146058fb6",
    "MessageDisclosure.schema.json": "0c45464528541959362056f3af35a678bd58338f201314a9fdf22c6c6d07fd31",
    "AuthorityBinding.schema.json": "5b901b4854525057fff29543877b9de9b1797177bb3be54a94f5722ce51339b9",
    "SignedEnvelope.schema.json": "9331d3ba38d5b84be639085df1f20f22651169f9169af51ff9361ebc6ad46c13",
    "RequestContext.schema.json": "a327033efcb5a2436ea0b412a727b00da25cddaa776646ca58fceeb46d02b6a4",
    "golden-v1.json": "914f93ba2c861c4bf191852884e28eb2e7de779650700b57ab80d166ef0b8532",
    "protocol-golden-v1.json": "37b8bb1978c6ee2ca64ab1591ba87f830a00c0e9e4b600d1849c6358c9db441a",
}
FROZEN_V1_MODELS = {"PolicyV2": PolicyV2, "Decision": Decision, "MessageDisclosure": MessageDisclosure,
                    "AuthorityBinding": AuthorityBinding, "SignedEnvelope": SignedEnvelope,
                    "RequestContext": RequestContext}


def as_v2(raw):
    """The same document on the attested rule: capability, subject block and evaluator, nothing else."""
    raw = deepcopy(raw)
    raw["versions"] = {"vocabulary": raw["versions"]["vocabulary"], "capability": V2,
                       "subject_binding": deepcopy(SUBJECT_BINDING)}
    raw["evaluator"] = {"kind": "hard_rules", "version": "hard-rules/p2a-v2"}
    return raw


def reading_policy(binding):
    """test_release's positive p2a-v1 grant: the reviewed `reading` domain over source-1 messages."""
    raw = sample_policy()
    raw["binding"].update(binding.model_dump())
    raw["versions"]["vocabulary"] = VOCABULARY
    raw["source_universe"]["source_ids"] = ["source-1", "ai-source-1"]
    rule = raw["rules"][0]
    rule["evidence_use"]["sources"]["values"] = ["source-1"]
    reading = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["reading"]}
    rule["evidence_use"]["predicate"], rule["release"]["predicate"] = reading, deepcopy(reading)
    return raw


def node(corpus, tmp_path, raw):
    """test_release.release_setup's node, built AFTER the identity state it reads is in place."""
    resolver, reviews, _ = corpus
    cp_key, node_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    cp_keys = {"cp-key": cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    with resolver._read() as (_, floor):
        ledger = PolicyLedger(tmp_path / "ledger.db", identity=NodeIdentity.parse(resolver.binding.model_dump()),
                              protection_revision=floor, trusted_keys=cp_keys)
    node_protocol = NodePolicyProtocol(ledger, canonical_database=resolver.path, cp_issuer_id="cp-issuer",
        frontend_client_id="owner-ui", trusted_cp_keys=cp_keys, node_signing_kid="node-key", node_signing_key=node_key)
    now = [1101]
    service = SourceMessageRelease(protocol=node_protocol, resolver=resolver, reviews=reviews, clock=lambda: now[0])
    return service, raw(resolver.binding), cp_key, node_key, now, corpus


@pytest.fixture
def two_selves(corpus, tmp_path):
    """Two `is_self` rows, both attested, and a reviewed fact whose subject is one of them."""
    with db(corpus) as conn:
        add_entity(conn, SECOND)
        do_attest(conn, OWNER)
        do_attest(conn, SECOND)
    change_fact(corpus, subject_entity_id=OWNER)
    attest(corpus)
    return node(corpus, tmp_path, lambda binding: as_v2(reading_policy(binding)))


@pytest.fixture
def one_self(corpus, tmp_path):
    """The corpus's single `is_self` row, attested, and a reviewed fact whose subject is that entity."""
    with db(corpus) as conn:
        do_attest(conn, OWNER)
    change_fact(corpus, subject_entity_id=OWNER)
    attest(corpus)
    return node(corpus, tmp_path, reading_policy)


def named(raw, name):
    """The same policy as its own grant, assignment and immutable version."""
    raw = deepcopy(raw)
    raw["binding"].update(grant_id=f"grant-{name}", assignment_id=f"assignment-{name}")
    raw["policy_version_id"] = f"policy-{name}"
    return raw


def activate(setup, raw, *, name):
    """One more grant through the owner's local hook, at the node's current epoch."""
    service, _, _, _, now, _ = setup
    ledger = service.protocol.ledger
    raw = named(raw, name)
    with owner():
        with ledger._transaction() as conn:
            service.protocol._sync_protection(conn)
            epoch = ledger._node(conn)["epoch"]
        ledger.activate(raw, grant_generation=1, assignment_generation=1, expected_epoch=epoch,
                        command_id=f"activate-{name}", now=now[0])
    return raw["binding"]["grant_id"]


def issue(setup, grant_id, *, request_id, changes=None):
    """A fresh CP issuance: the node's current authority for that grant, signed as the CP signs it."""
    service, _, cp_key, _, now, corpus = setup
    ledger = service.protocol.ledger
    with ledger._transaction() as conn:
        service.protocol._sync_protection(conn)
    with owner():
        authority = ledger.authority_snapshot(grant_id, now=now[0])
    payload = {"query": "fact:" + corpus[2]}
    body = parse_envelope({**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
        "request_id": request_id, "request_type": "permissions.v2.read",
        "request_hash": request_digest("permissions.v2.read", payload), "issued_at": now[0], "expires_at": now[0] + 100,
        **(changes or {})}, signed=False)
    return sign_envelope(body, cp_key), payload


def read(setup, grant_id, *, request_id, changes=None):
    """((result, output), None) on release, or (None, the adapter's reason); nothing is sent on a refusal."""
    envelope, payload = issue(setup, grant_id, request_id=request_id, changes=changes)
    sent = []
    try:
        dispatch(setup, envelope, payload, request_id=request_id, send=lambda result, output: sent.append((result, output)))
    except PolicyError as exc:
        assert sent == []
        return None, exc.code
    [released] = sent
    verify_node_result(released[0], trusted_keys={"node-key": setup[3].public_key().public_bytes_raw()},
                       envelope=envelope, output=released[1], now=setup[4][0])
    return released, None


def receipt_decision(setup, request_id):
    with sqlite3.connect(setup[0].protocol.ledger.path) as conn:
        row = conn.execute("SELECT decision_json FROM p2a_receipts WHERE request_id=?", (request_id,)).fetchone()
    return None if row is None else json.loads(row[0])


RELEASED = {"family": "canonical_record", "operation": "read", "view_id": "canonical.message_disclosure.v1",
            "records": [{"record_id": "message-1", "source_id": "source-1", "canonical_table": "conversation_messages",
                         "content": MESSAGE}]}


# --- p2a-v1 is frozen ------------------------------------------------------------

def test_every_p2a_v1_export_and_golden_is_byte_identical_to_what_shipped():
    for name, pinned in FROZEN_V1.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == pinned, name
    for name, model in FROZEN_V1_MODELS.items():
        # Byte-for-byte, not just equal JSON: the frontend pins these bytes.
        assert export(model) == (FIXTURES / f"{name}.schema.json").read_bytes(), name
    golden = json.loads((FIXTURES / "golden-v1.json").read_text())
    parsed = parse_policy(golden["policy"])
    assert type(parsed) is PolicyV2 and digest(parsed.model_dump()) == golden["policy_hash"]
    assert type(parse_envelope(golden["envelope"])) is SignedEnvelope
    assert type(parse_authority({key: golden["envelope"][key] for key in AuthorityBinding.model_fields})) is AuthorityBinding
    assert type(parse_disclosure(RELEASED, capability=V1)) is MessageDisclosure


@pytest.fixture
def release_setup_v1(corpus, tmp_path):
    attest(corpus)
    return node(corpus, tmp_path, reading_policy)


def test_the_p2a_v1_decision_is_unchanged_for_the_same_evidence(release_setup_v1):
    """The legacy evaluator still names itself and emits the class it always did."""
    service, raw, _, _, _, corpus = release_setup_v1
    evidence = corpus[0].qualify(corpus[2], reviews=corpus[1]).evidence
    decision = source_message_decision(PolicyV2.parse(raw), evidence)
    assert type(decision) is Decision and decision.evaluator_version == "hard-rules/p2a-v1"
    assert decision.verdict == "permit" and decision.matched_allow_clause_ids == ["rule-A"]


@pytest.mark.parametrize("model", ATTESTED_MODELS, ids=lambda model: model.__name__)
def test_the_p2a_v2_exports_are_pinned(model):
    assert export(model) == (ATTESTED_FIXTURES / f"{model.__name__}.schema.json").read_bytes()


@pytest.mark.parametrize("model", PROTOCOL_MODELS, ids=lambda model: model.__name__)
def test_the_protocol_exports_carry_the_v2_authority_and_policy(model):
    assert export(model) == (FIXTURES / f"{model.__name__}.schema.json").read_bytes()
    text = json.dumps(model.model_json_schema())
    assert "AttestedSourceAuthorityBinding" in text


# --- the contract ----------------------------------------------------------------

def test_the_v2_subject_block_is_the_fact_contracts_own():
    versions = AttestedSubjectSourcePolicy.model_fields["versions"].annotation
    assert versions.model_fields["subject_binding"].annotation is fact_contract.OwnerAttestedSubjectBinding
    source = AttestedSubjectSourcePolicy.model_json_schema()["$defs"]["OwnerAttestedSubjectBinding"]
    fact = fact_contract.AttestedSubjectFactPolicy.model_json_schema()["$defs"]["OwnerAttestedSubjectBinding"]
    assert source == fact
    # The rest of the grammar is p2a-v1's: only the versions, evaluator and class name differ.
    v1, v2 = PolicyV2.model_json_schema(), AttestedSubjectSourcePolicy.model_json_schema()
    assert v1["properties"].keys() == v2["properties"].keys() and v1["required"] == v2["required"]
    for name in ("Rule", "Release", "EvidenceUse", "OutputForm", "HardConstraints", "SourceUniverse", "Binding"):
        assert v1["$defs"][name] == v2["$defs"][name], name


def test_v1_and_v2_documents_never_parse_as_each_other():
    v1 = sample_policy()
    v2 = as_v2(v1)
    assert type(parse_policy(v1)) is PolicyV2
    parsed = parse_policy(v2)
    assert type(parsed) is AttestedSubjectSourcePolicy
    assert SUBJECT_CONTRACT_BY_CAPABILITY[parsed.versions.capability] == ATTESTED_CONTRACT
    assert parsed.versions.subject_binding.contract == ATTESTED_CONTRACT
    with pytest.raises(PolicyError, match="schema_invalid"):
        PolicyV2.parse(v2)
    with pytest.raises(PolicyError, match="schema_invalid"):
        AttestedSubjectSourcePolicy.parse(v1)
    relabelled_v1 = deepcopy(v1)
    relabelled_v1["versions"]["capability"] = V2
    relabelled_v2 = deepcopy(v2)
    relabelled_v2["versions"]["capability"] = V1
    v1_evaluator = deepcopy(v2)
    v1_evaluator["evaluator"]["version"] = "hard-rules/p2a-v1"
    v2_evaluator = deepcopy(v1)
    v2_evaluator["evaluator"]["version"] = "hard-rules/p2a-v2"
    fact_evaluator = deepcopy(v2)
    fact_evaluator["evaluator"]["version"] = "hard-rules/p2b-v3"
    for raw in (relabelled_v1, relabelled_v2, v1_evaluator, v2_evaluator, fact_evaluator):
        with pytest.raises(PolicyError):
            parse_policy(raw)
    # A fact capability never parses a raw message document, whatever it carries.
    for capability in ("permissions-beta/p2b-v3", "permissions-beta/p2b-v4"):
        relabelled = deepcopy(v2)
        relabelled["versions"]["capability"] = capability
        with pytest.raises(PolicyError):
            parse_policy(relabelled)


@pytest.mark.parametrize("change", ["contract", "unattested", "rekeyed_facts", "moved", "shadowed", "statement",
                                    "missing_binding", "extra"])
def test_every_withholding_choice_in_the_v2_subject_block_is_the_only_choice(change):
    raw = as_v2(sample_policy())
    block = raw["versions"]["subject_binding"]
    if change == "contract": block["contract"] = "legacy_single_self_v1"
    elif change == "unattested": block["unattested"] = "allow"
    elif change == "rekeyed_facts": block["rekeyed_facts"] = "allow"
    elif change == "moved": block["moved_since_attestation"] = "allow"
    elif change == "shadowed": block["literal_self_when_shadowed"] = "allow"
    elif change == "statement": block["statement_version"] = "owner-identity-attestation/v2"
    elif change == "missing_binding": raw["versions"].pop("subject_binding")
    else: block["subjects_also"] = ["self"]
    with pytest.raises(PolicyError, match="schema_invalid"):
        parse_policy(raw)


def test_decisions_and_disclosures_parse_only_under_their_own_capability():
    policy_hash = digest(sample_policy())
    base = {"stage": "output_release", "verdict": "permit", "policy_hash": policy_hash, "candidate_revision": "b" * 64,
            "matched_allow_clause_ids": ["rule-A"], "matched_deny_clause_ids": [], "reason_code": "rule_permit",
            "required_projection_id": "canonical.message_disclosure.v1", "missing_context_codes": []}
    v1, v2 = {**base, "evaluator_version": "hard-rules/p2a-v1"}, {**base, "evaluator_version": "hard-rules/p2a-v2"}
    assert type(parse_decision(v1, capability=V1)) is Decision
    assert type(parse_decision(v2, capability=V2)) is AttestedSubjectSourceDecision
    for raw, capability in ((v1, V2), (v2, V1), (v2, "permissions-beta/p2b-v3")):
        with pytest.raises(PolicyError):
            parse_decision(raw, capability=capability)
    for capability in (V1, V2):
        assert type(parse_disclosure(RELEASED, capability=capability)) is MessageDisclosure


def test_envelopes_and_authority_dispatch_on_the_signed_capability():
    raw = as_v2(sample_policy())
    key = Ed25519PrivateKey.generate()
    authority = {**raw["binding"], "grant_generation": 1, "assignment_generation": 1,
                 "policy_version_id": raw["policy_version_id"], "policy_hash": digest(raw), "capability_version": V2,
                 "protection_revision": "a" * 64, "node_epoch": 1}
    payload = {"query": "fact:fact-1"}
    body = {**authority, "version": "topos-grantee-envelope/v2", "kid": "cp-key", "request_id": "read-1",
            "request_type": "permissions.v2.read", "request_hash": request_digest("permissions.v2.read", payload),
            "issued_at": 1100, "expires_at": 1200}
    envelope = sign_envelope(parse_envelope(body, signed=False), key)
    assert type(envelope) is SignedAttestedSourceEnvelope
    assert type(parse_authority(authority)) is AttestedSourceAuthorityBinding
    assert type(parse_source_envelope(envelope.model_dump())) is SignedAttestedSourceEnvelope
    for strict in (SignedEnvelope, SignedFactEnvelope):
        with pytest.raises(PolicyError, match="schema_invalid"):
            strict.parse(envelope.model_dump())
    with pytest.raises(PolicyError, match="schema_invalid"):
        AuthorityBinding.parse(authority)
    # Relabelling a signed v2 envelope as v1 parses, and never verifies.
    relabelled = {**envelope.model_dump(), "capability_version": V1}
    assert type(parse_source_envelope(relabelled)) is SignedEnvelope
    with pytest.raises(PolicyError, match="signature_invalid"):
        verify_current_signature(SignedEnvelope.parse(relabelled),
                                 trusted_keys={"cp-key": key.public_key().public_bytes_raw()}, now=1100)
    # A fact read never becomes a raw message read, under either spelling.
    fact = {**body, "capability_version": "permissions-beta/p2b-v3", "request_type": "permissions.v2.fact.read"}
    with pytest.raises(PolicyError, match="schema_invalid"):
        parse_source_envelope(fact)
    with pytest.raises(PolicyError, match="schema_invalid"):
        SignedAttestedSourceEnvelope.parse({**envelope.model_dump(), "request_type": "permissions.v2.fact.read"})


def test_the_node_lists_both_capabilities_and_maps_each_to_its_own_rule():
    assert SUBJECT_CONTRACT_BY_CAPABILITY[V1] == LEGACY_CONTRACT
    assert SUBJECT_CONTRACT_BY_CAPABILITY[V2] == ATTESTED_CONTRACT
    document = capability_document()
    assert document["version"] == V1
    # p2a-v3 (bookkeeping batch 3) joins with the opaque-id view; v1 and v2 keep theirs.
    assert document["capabilities"] == [V1, V2, "permissions-beta/p2a-v3"]
    assert document["registered_forms"] == [{"family": "canonical_record", "operation": "read",
                                             "view_id": "canonical.message_disclosure.v1"},
                                            {"family": "canonical_record", "operation": "read",
                                             "view_id": "canonical.message_disclosure.v2"}]
    assert document["executable_forms"] == [] and document["natural_language"] is False


# --- the case p2a-v1 cannot serve ------------------------------------------------

def test_a_v2_grant_releases_the_message_behind_an_attested_entity_on_a_node_with_two_attested_selves(two_selves):
    v2 = activate(two_selves, two_selves[1], name="v2")
    (result, output), error = read(two_selves, v2, request_id="v2-read-1")
    assert error is None and output == RELEASED
    assert result["authority"]["capability_version"] == V2
    decision = receipt_decision(two_selves, "v2-read-1")
    assert (decision["verdict"], decision["evaluator_version"]) == ("permit", "hard-rules/p2a-v2")
    for private in (OWNER, SECOND):
        assert private not in json.dumps(output) + json.dumps(result) + json.dumps(decision)
    # The same node, fact, review and rules under p2a-v1: the legacy rule refuses
    # a node with two self rows before it looks at any rule.
    v1 = activate(two_selves, reading_policy(two_selves[5][0].binding), name="v1")
    assert read(two_selves, v1, request_id="v1-read-1") == (None, "owner_subject_ambiguous")
    assert receipt_decision(two_selves, "v1-read-1") is None


@pytest.mark.parametrize("subject_state", ["fact_subject", "review_label"])
def test_an_unattested_self_entity_withholds_under_v2(corpus, tmp_path, subject_state):
    """A second `is_self` row the owner has NOT attested: as the fact's subject, or in the owner's labels."""
    with db(corpus) as conn:
        add_entity(conn, SECOND)
        do_attest(conn, OWNER)
    if subject_state == "fact_subject":
        change_fact(corpus, subject_entity_id=SECOND)
        attest(corpus)
    else:
        change_fact(corpus, subject_entity_id=OWNER)
        attest(corpus, transform=lambda items: [item.model_copy(update={"subject_entity_ids": [SECOND]})
                                                for item in items])
    setup = node(corpus, tmp_path, lambda binding: as_v2(reading_policy(binding)))
    v2 = activate(setup, setup[1], name="v2")
    expected = "owner_subject_unattested" if subject_state == "fact_subject" else "classification_unknown_or_mixed"
    assert read(setup, v2, request_id="v2-read-1") == (None, expected)
    assert receipt_decision(setup, "v2-read-1") is None


def test_withdrawing_the_attestation_stops_the_next_v2_read_and_leaves_v1_releasing(one_self):
    v1 = activate(one_self, one_self[1], name="v1")
    v2 = activate(one_self, as_v2(one_self[1]), name="v2")
    assert read(one_self, v1, request_id="v1-read-1")[0][1] == RELEASED
    assert read(one_self, v2, request_id="v2-read-1")[0][1] == RELEASED
    stale, payload = issue(one_self, v2, request_id="v2-read-stale")
    with db(one_self[5]) as conn:
        do_revoke(conn, OWNER, entries(conn)[OWNER].entry_id)
    # An issuance from before the withdrawal is refused like any stale authority...
    with pytest.raises(PolicyError, match="authority_binding"):
        dispatch(one_self, stale, payload, request_id="v2-read-stale",
                 send=lambda *_: pytest.fail("released after the owner withdrew the attestation"))
    # ...and so is every fresh one, whichever capability: the owner's evidence
    # review bound that entity's attestation state, which just changed.
    assert read(one_self, v2, request_id="v2-read-2") == (None, "review_stale")
    assert read(one_self, v1, request_id="v1-read-2") == (None, "review_stale")
    # Once the owner reviews the current evidence again, the withdrawal itself is
    # what refuses v2, and p2a-v1, which never read an attestation, releases.
    attest(one_self[5], review_id="review-after-withdrawal")
    assert read(one_self, v2, request_id="v2-read-3") == (None, "owner_subject_unattested")
    assert receipt_decision(one_self, "v2-read-3") is None
    assert read(one_self, v1, request_id="v1-read-3")[0][1] == RELEASED
    assert receipt_decision(one_self, "v1-read-3")["evaluator_version"] == "hard-rules/p2a-v1"


def test_the_deselected_sibling_floor_still_withholds_under_v2(two_selves):
    from tests.permissions_v2.test_source_release_sibling_facts import sibling
    v2 = activate(two_selves, two_selves[1], name="v2")
    assert read(two_selves, v2, request_id="v2-read-1")[0][1] == RELEASED
    sibling(two_selves)
    assert read(two_selves, v2, request_id="v2-read-2") == (None, "owner_opted_out")


@pytest.mark.asyncio
async def test_the_shipped_transport_sends_a_v2_release_and_refuses_a_v1_one_on_the_same_node(two_selves, monkeypatch):
    from types import SimpleNamespace
    from topos.relay_stamp import canonical_signing_payload
    service, raw, cp_key, _, now, _ = two_selves
    v2 = activate(two_selves, raw, name="v2")
    v1 = activate(two_selves, reading_policy(two_selves[5][0].binding), name="v1")
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", "true")
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(cp_key.public_key().public_bytes_raw()).decode())
    monkeypatch.setattr(release_transport.time, "time", lambda: now[0])
    runtime_double = SimpleNamespace(protocol=service.protocol,
        evidence_reviews=lambda **_: SimpleNamespace(resolver=service.resolver, reviews=service.reviews))
    monkeypatch.setattr(release_transport, "get_runtime", lambda: runtime_double)

    async def frame(grant_id, request_id):
        envelope, payload = issue(two_selves, grant_id, request_id=request_id)
        message = {"id": request_id, "type": release_transport.MESSAGE_TYPE,
                   "payload": {"envelope": envelope.model_dump(), "intent": payload}}
        stamp = {"v": 1, "cls": "third_party", "client_id": "client-1", "acting_user": "actor-1",
                 "iat": now[0], "exp": now[0] + 100}
        stamp["sig"] = base64.b64encode(cp_key.sign(canonical_signing_payload(
            stamp, msg_id=request_id, msg_type=message["type"]))).decode()
        message["principal_stamp"] = stamp
        socket = Socket()
        await release_transport.dispatch_source_message(socket, message)
        return socket.sent

    [released] = await frame(v2, "v2-socket-1")
    assert released["status"] == "ok" and released["payload"]["output"] == RELEASED
    assert released["payload"]["result"]["authority"]["capability_version"] == V2
    assert await frame(v1, "v1-socket-1") == [{"id": "v1-socket-1", "type": release_transport.MESSAGE_TYPE,
                                              "status": "error", "code": 403, "error": "permission_denied"}]


# --- no confusion between the two ------------------------------------------------

def test_a_signed_authority_relabelled_to_the_other_capability_is_never_admitted(one_self):
    v1 = activate(one_self, one_self[1], name="v1")
    v2 = activate(one_self, as_v2(one_self[1]), name="v2")
    # The CP key signs a correct envelope for each grant but names the other
    # capability: the node's recorded authority, not the envelope, decides.
    assert read(one_self, v1, request_id="relabel-1", changes={"capability_version": V2}) == (None, "authority_binding")
    assert read(one_self, v2, request_id="relabel-2", changes={"capability_version": V1}) == (None, "authority_binding")
    with sqlite3.connect(one_self[0].protocol.ledger.path) as conn:
        assert conn.execute("SELECT count(*) FROM p2a_requests").fetchone()[0] == 0


def test_a_grant_cannot_change_between_v1_and_v2_in_place(one_self):
    activate(one_self, one_self[1], name="same")
    service, raw, _, _, now, _ = one_self
    changed = as_v2(raw)
    changed["binding"].update(grant_id="grant-same", assignment_id="assignment-same")
    changed["policy_version_id"] = "policy-same-v2"
    with owner(), service.protocol.ledger._transaction() as conn:
        epoch = service.protocol.ledger._node(conn)["epoch"]
    with owner(), pytest.raises(PolicyError, match="capability_change_requires_new_grant"):
        service.protocol.ledger.activate(changed, grant_generation=2, assignment_generation=2, expected_epoch=epoch,
                                         command_id="activate-same-v2", now=now[0])


def test_evidence_qualified_under_one_rule_is_never_evaluated_under_the_other(one_self):
    _, raw, _, _, _, corpus = one_self
    captured = {}
    for contract_name in (LEGACY_CONTRACT, ATTESTED_CONTRACT):
        corpus[0].with_qualified(corpus[2], reviews=corpus[1], contract=contract_name,
                                 callback=lambda evidence, _rows, name=contract_name: captured.update({name: evidence}))
    v1, v2 = PolicyV2.parse(raw), AttestedSubjectSourcePolicy.parse(as_v2(raw))
    assert source_message_decision(v1, captured[LEGACY_CONTRACT]).evaluator_version == "hard-rules/p2a-v1"
    assert source_message_decision(v2, captured[ATTESTED_CONTRACT]).evaluator_version == "hard-rules/p2a-v2"
    for policy, evidence in ((v1, captured[ATTESTED_CONTRACT]), (v2, captured[LEGACY_CONTRACT])):
        with pytest.raises(PolicyError, match="subject_contract_mismatch"):
            source_message_decision(policy, evidence)
    # A fact policy has no raw message decision, whichever rule it names.
    work = parse_policy(work_policy((corpus[0], None, None)))
    with pytest.raises(PolicyError, match="unsupported_vocabulary|unsupported_capability"):
        source_message_decision(work, captured[ATTESTED_CONTRACT])


# --- the offline bridge reads the rule its capsule's capability names -------------

@pytest.mark.asyncio
async def test_bridge_arm_a_is_the_served_v2_decision_and_a_v1_capsule_withholds_on_this_node(two_selves):
    from tests.permissions_v2.test_source_bridge import FIELDS, bridge
    raw = named(two_selves[1], "parity")
    released, code = read(two_selves, activate(two_selves, raw, name="parity"), request_id="parity-1")
    served = receipt_decision(two_selves, "parity-1")
    assert (code, released[1]) == (None, RELEASED)
    assert (served["verdict"], served["evaluator_version"]) == ("permit", "hard-rules/p2a-v2")
    result = await bridge(two_selves, raw=raw).run(two_selves[5][2], arm="rules_v2")
    [decision] = result.stages
    assert {field: getattr(decision, field) for field in FIELDS} == {field: served[field] for field in FIELDS}
    assert decision.withheld_code is None and result.model_calls == 0
    # The same capsule written as p2a-v1: its capture takes the legacy rule and withholds.
    legacy = reading_policy(two_selves[5][0].binding)
    [withheld] = (await bridge(two_selves, raw=legacy).run(two_selves[5][2], arm="rules_v2")).stages
    assert (withheld.verdict, withheld.reason_code, withheld.withheld_code) == (
        "indeterminate", "evidence_withheld", "owner_subject_ambiguous")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["rule_deny", "release_predicate_mismatch", "summary_ceiling", "unknown_classification"])
async def test_bridge_parity_under_v2_for_every_refusing_rule(two_selves, monkeypatch, case):
    from tests.permissions_v2.test_source_bridge import FIELDS, PARITY, bridge, changed, unknown_domain
    from topos.permissions_v2 import release
    changes, unknown, verdict, reason = PARITY[case]
    if unknown:
        monkeypatch.setattr(release, "_attributes", unknown_domain)
    raw = named(changed(two_selves, *changes), "parity")
    assert read(two_selves, activate(two_selves, raw, name="parity"), request_id="parity-1") == (None, "permission_denied")
    served = receipt_decision(two_selves, "parity-1")
    assert (served["verdict"], served["reason_code"], served["evaluator_version"]) == (verdict, reason, "hard-rules/p2a-v2")
    [decision] = (await bridge(two_selves, raw=raw).run(two_selves[5][2], arm="rules_v2")).stages
    assert {field: getattr(decision, field) for field in FIELDS} == {field: served[field] for field in FIELDS}
