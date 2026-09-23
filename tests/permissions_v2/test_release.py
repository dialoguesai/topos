"""Real SQLite evidence, real signed authority and actual bounded dispatch tests."""
from contextlib import contextmanager
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.permissions_v2.test_evidence import corpus, attest, edit, owner, payload as change_fact
from tests.permissions_v2.test_contract_and_ledger import sample_policy
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.contract import PolicyV2
from topos.permissions_v2.forwarding import verify_node_result
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.release import SourceMessageRelease, VOCABULARY
from topos.permissions_v2.signing import EnvelopeBody, request_digest, sign_envelope
from topos.principal import THIRD_PARTY, Principal, reset_principal, set_principal


@contextmanager
def recipient(**changes):
    fields = dict(cls=THIRD_PARTY, channel="cp_relay", acting_user="actor-1", client_id="client-1")
    fields.update(changes)
    token = set_principal(Principal(**fields))
    try:
        yield
    finally:
        reset_principal(token)


@pytest.fixture
def release_setup(corpus, tmp_path):
    resolver, reviews, fact_id = corpus
    attest(corpus)
    cp_key, node_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    cp_keys = {"cp-key": cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    with resolver._read() as (_, floor):
        ledger = PolicyLedger(tmp_path / "ledger.db", identity=NodeIdentity.parse(resolver.binding.model_dump()),
                              protection_revision=floor, trusted_keys=cp_keys)
    protocol = NodePolicyProtocol(ledger, canonical_database=resolver.path, cp_issuer_id="cp-issuer",
        frontend_client_id="owner-ui", trusted_cp_keys=cp_keys, node_signing_kid="node-key", node_signing_key=node_key)
    policy = sample_policy()
    policy["binding"].update(resolver.binding.model_dump())
    policy["versions"]["vocabulary"] = VOCABULARY
    policy["source_universe"]["source_ids"] = ["source-1", "ai-source-1"]
    policy["rules"][0]["evidence_use"]["sources"]["values"] = ["source-1"]
    reading = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["reading"]}
    policy["rules"][0]["evidence_use"]["predicate"] = reading
    policy["rules"][0]["release"]["predicate"] = reading
    now = [1101]
    service = SourceMessageRelease(protocol=protocol, resolver=resolver, reviews=reviews, clock=lambda: now[0])
    return service, policy, cp_key, node_key, now, corpus


def issue(setup, *, policy_change=None, request_id="read-1", query=None):
    service, policy, cp_key, _, now, corpus = setup
    policy = deepcopy(policy)
    if policy_change:
        policy_change(policy)
    with owner():
        service.protocol.ledger.activate(policy, grant_generation=1, assignment_generation=1,
            expected_epoch=0, command_id="activate-1", now=now[0])
        authority = service.protocol.ledger.authority_snapshot("grant-1", now=now[0])
    payload = {"query": query if query is not None else "fact:" + corpus[2]}
    body = EnvelopeBody.parse({**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
        "request_id": request_id, "request_type": "permissions.v2.read", "request_hash": request_digest("permissions.v2.read", payload),
        "issued_at": now[0], "expires_at": now[0] + 100})
    envelope = sign_envelope(body, cp_key)
    return envelope, payload


def dispatch(setup, envelope, payload, *, send=None, request_id="read-1"):
    captured = []
    with recipient():
        setup[0].dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id,
                          send=send or (lambda result, output: captured.append((result, output))))
    return captured


def test_positive_returns_only_exact_authorized_terminal_source_and_signed_binding(release_setup):
    envelope, payload = issue(release_setup)
    [(result, output)] = dispatch(release_setup, envelope, payload)
    assert output == {"family": "canonical_record", "operation": "read", "view_id": "canonical.message_disclosure.v1",
        "records": [{"record_id": "message-1", "source_id": "source-1", "canonical_table": "conversation_messages", "content": "I enjoy reading history books."}]}
    assert release_setup[5][2] not in json.dumps(output)
    verify_node_result(result, trusted_keys={"node-key": release_setup[3].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)},
                       envelope=envelope, output=output, now=1101)
    with pytest.raises(PolicyError, match="request_replay"):
        dispatch(release_setup, envelope, payload)


@pytest.mark.parametrize("ceiling", ["summary", "inference"])
def test_lower_ceiling_never_returns_raw(release_setup, ceiling):
    envelope, payload = issue(release_setup, policy_change=lambda p: p["rules"][0]["release"].update(ceiling=ceiling))
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(release_setup, envelope, payload)


@pytest.mark.parametrize("mutation", [
    lambda p: p["rules"][0]["evidence_use"]["sources"].update(values=[]),
    lambda p: p["rules"][0]["release"].update(forms=[]),
    lambda p: p["rules"][0]["release"]["forms"][0].update(tables=[]),
    lambda p: p["rules"][0]["evidence_use"]["processors"].update(values=[]),
    lambda p: p["rules"][0]["release"]["predicate"].update(values=["health"]),
    lambda p: p["rules"][0]["evidence_use"]["predicate"].update(values=["finance"]),
])
def test_empty_sets_and_nonmatching_scope_withhold(release_setup, mutation):
    envelope, payload = issue(release_setup, policy_change=mutation)
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(release_setup, envelope, payload)


def test_unknown_vocabulary_never_reuses_owner_review_labels(release_setup):
    envelope, payload = issue(release_setup, policy_change=lambda p: p["versions"].update(vocabulary="different-v1"))
    with pytest.raises(PolicyError, match="unsupported_vocabulary"):
        dispatch(release_setup, envelope, payload)


@pytest.mark.parametrize("lift", [False, True])
def test_new_protection_or_protect_lift_cycle_invalidates_issued_request(release_setup, lift):
    from topos.features.lifecycle.record_protection import RecordProtectionStore
    envelope, payload = issue(release_setup)
    with owner(), sqlite3.connect(release_setup[5][0].path) as conn:
        protection = RecordProtectionStore(conn)
        protection.protect(canonical_table="conversation_messages", record_id="message-1")
        if lift:
            protection.unprotect(canonical_table="conversation_messages", record_id="message-1")
    with pytest.raises(PolicyError):
        dispatch(release_setup, envelope, payload, send=lambda *args: pytest.fail("protected data sent"))


def test_rule_can_authorize_complete_multi_source_closure(release_setup):
    corpus = release_setup[5]
    refs = [{"table":"conversation_messages","dataset_id":"dataset-1","source_id":"source-1","record_id":"message-1"},
            {"table":"ai_chat_messages","source_id":"ai-source-1","record_id":"ai-message-1"}]
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps(refs),))
    attest(corpus, review_id="review-2")
    def complete(policy):
        rule = policy["rules"][0]
        rule["evidence_use"]["sources"]["values"] = ["source-1", "ai-source-1"]
        rule["release"]["forms"][0]["tables"] = ["conversation_messages", "ai_chat_messages"]
    envelope, payload = issue(release_setup, policy_change=complete)
    [(result, output)] = dispatch(release_setup, envelope, payload)
    assert {record["record_id"] for record in output["records"]} == {"message-1", "ai-message-1"}
    assert result["output_hash"] == digest(output)


def test_deny_from_second_clause_wins(release_setup):
    def exclude(policy):
        deny = deepcopy(policy["rules"][0])
        deny.update(rule_id="deny-reading", effect="deny")
        policy["rules"].append(deny)
    envelope, payload = issue(release_setup, policy_change=exclude)
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(release_setup, envelope, payload)


def test_split_source_table_clauses_cannot_be_combined(release_setup):
    corpus = release_setup[5]
    refs = [{"table":"conversation_messages","dataset_id":"dataset-1","source_id":"source-1","record_id":"message-1"},
            {"table":"ai_chat_messages","source_id":"ai-source-1","record_id":"ai-message-1"}]
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps(refs),))
    attest(corpus, review_id="review-2")
    def split(policy):
        rule = deepcopy(policy["rules"][0])
        rule["rule_id"] = "ai-only"
        rule["evidence_use"]["sources"]["values"] = ["ai-source-1"]
        rule["release"]["forms"][0]["tables"] = ["ai_chat_messages"]
        policy["rules"].append(rule)
    envelope, payload = issue(release_setup, policy_change=split)
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(release_setup, envelope, payload)


@pytest.mark.parametrize("kind", ["owner_only", "review_revoked", "source_changed", "source_deleted", "grant_revoked", "expired", "key_removed"])
def test_changes_after_issuance_prevent_any_dispatch(release_setup, kind):
    envelope, payload = issue(release_setup)
    service, _, _, _, now, corpus = release_setup
    if kind == "owner_only":
        change_fact(corpus, disclosure="owner_only")
    elif kind == "review_revoked":
        with owner():
            corpus[1].revoke_review("review-1")
    elif kind == "source_changed":
        edit(corpus, "UPDATE conversation_messages SET content='Different statement.'")
    elif kind == "source_deleted":
        edit(corpus, "DELETE FROM conversation_messages")
    elif kind == "grant_revoked":
        with owner():
            service.protocol.ledger.revoke("grant-1", expected_epoch=1, command_id="cancel")
    elif kind == "expired":
        now[0] = envelope.expires_at
    else:
        service.protocol.ledger.trusted_keys.clear()
    sent = []
    with pytest.raises(PolicyError):
        dispatch(release_setup, envelope, payload, send=lambda *args: sent.append(args))
    assert sent == []


@pytest.mark.parametrize("changes", [{"cls":"owner_app"}, {"cls":"cp_relay"}, {"channel":"local_http"},
    {"acting_user":"wrong-actor"}, {"client_id":"wrong-client"}, {"client_id":""}])
def test_actual_channel_actor_and_client_required(release_setup, changes):
    envelope, payload = issue(release_setup)
    with recipient(**changes), pytest.raises(PolicyError):
        release_setup[0].dispatch(envelope=envelope.model_dump(), payload=payload, request_id="read-1", send=lambda *args: pytest.fail("sent"))


@pytest.mark.parametrize("query", ["all facts", "fact:", "fact:x?mode=owner", "fact:x\n", "select * from signal_objects"])
def test_recipient_cannot_select_query_backend_or_shape(release_setup, query):
    envelope, payload = issue(release_setup, query=query)
    with pytest.raises(PolicyError):
        dispatch(release_setup, envelope, payload)


def test_uncertain_send_is_durably_non_replayable(release_setup):
    envelope, payload = issue(release_setup)
    def failed(*args):
        raise ConnectionError("closed transport")
    with pytest.raises(ConnectionError):
        dispatch(release_setup, envelope, payload, send=failed)
    with pytest.raises(PolicyError, match="request_replay"):
        dispatch(release_setup, envelope, payload)


def test_review_revoke_no_longer_waits_for_the_send(release_setup):
    # R12 (bookkeeping batch 3): the checkpoint, under the gate, is the linearization
    # point, and the send runs with no gate held. An owner's revoke during the send
    # commits at once; the send it races was decided before it.
    envelope, payload = issue(release_setup)
    started, attempted, revoked = threading.Event(), threading.Event(), threading.Event()
    def revoke():
        assert started.wait(3)
        attempted.set()
        with owner():
            release_setup[5][1].revoke_review("review-1")
        revoked.set()
    def send(*args):
        started.set()
        assert attempted.wait(3)
        assert revoked.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(revoke)
        dispatch(release_setup, envelope, payload, send=send)
        future.result(timeout=3)
    assert revoked.is_set()


@pytest.mark.parametrize("floor", ["diverged", "unpublished"])
def test_release_requires_the_resolver_floor_it_read_to_equal_signed_protection(release_setup, monkeypatch, floor):
    # A real protection write cannot land mid-read: the read holds the canonical lock
    # and _sync_protection re-reads protection first, so stub the published floor.
    envelope, payload = issue(release_setup)
    resolver = release_setup[0].resolver
    published = []

    class DivergedFloor(type(resolver)):
        @property
        def current_floor(self):
            real = self.__dict__.get("current_floor")
            published.append(real)
            return None if real is None or floor == "unpublished" else "f" * 64

        @current_floor.setter
        def current_floor(self, value):
            self.__dict__["current_floor"] = value

    monkeypatch.setattr(resolver, "__class__", DivergedFloor)
    with pytest.raises(PolicyError, match="authority_stale"):
        dispatch(release_setup, envelope, payload, send=lambda *_: pytest.fail("output sent under a mismatched floor"))
    assert published and set(published) == {envelope.protection_revision}
    assert resolver.__dict__["current_floor"] is None
