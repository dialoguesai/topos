"""Real signed P2b release against SQLite evidence and owner output reviews."""
from copy import deepcopy
import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.permissions_v2.test_evidence import corpus, owner, edit, attest, payload as change_fact
from tests.permissions_v2.test_fact_policy import timed, policy, AS_OF, utc
from tests.permissions_v2.test_projection_reviews import service as projection_service, prepare
from tests.permissions_v2.test_release import recipient
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.contract import PolicyV2
from topos.permissions_v2.fact_release import FactProjectionRelease
from topos.permissions_v2.forwarding import verify_node_result
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.projection_reviews import RevokeProjectionReview
from topos.permissions_v2.signing import (AuthorityBinding, EnvelopeBody, SignedEnvelope, RequestContext,
    FactEnvelopeBody, SignedFactEnvelope, request_digest, sign_envelope)


@pytest.fixture
def fact_setup(timed, projection_service, tmp_path):
    request = prepare(timed, projection_service)
    with owner(): output_review = projection_service.record(request, now=1200)
    cp_key, node_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    cp_keys = {"cp-key": cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    with timed[0]._read() as (_, floor):
        ledger = PolicyLedger(tmp_path / "ledger.db", identity=NodeIdentity.parse(timed[0].binding.model_dump()),
            protection_revision=floor, trusted_keys=cp_keys)
    protocol = NodePolicyProtocol(ledger, canonical_database=timed[0].path, cp_issuer_id="cp-issuer",
        frontend_client_id="owner-ui", trusted_cp_keys=cp_keys, node_signing_kid="node-key", node_signing_key=node_key)
    now = [AS_OF]
    release = FactProjectionRelease(protocol=protocol, projections=projection_service, clock=lambda: now[0])
    return release, policy(timed), cp_key, node_key, now, timed, output_review


def issue(setup, *, change=None, request_id="fact-read-1"):
    release, raw, cp_key, _, now, corpus, _ = setup
    raw = deepcopy(raw)
    if change: change(raw)
    with owner():
        release.protocol.ledger.activate(raw, grant_generation=1, assignment_generation=1,
            expected_epoch=0, command_id="activate-1", now=now[0])
        authority = release.protocol.ledger.authority_snapshot("grant-1", now=now[0])
    payload = {"query": "fact:" + corpus[2]}
    body = FactEnvelopeBody.parse({**authority.model_dump(), "version":"topos-grantee-envelope/v2", "kid":"cp-key",
        "request_id":request_id, "request_type":"permissions.v2.fact.read", "request_hash":request_digest("permissions.v2.fact.read",payload),
        "issued_at":now[0], "expires_at":now[0]+100})
    return sign_envelope(body, cp_key), payload


def dispatch(setup, envelope, payload, *, send=None, request_id="fact-read-1"):
    captured=[]
    with recipient():
        setup[0].dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id,
            send=send or (lambda result, output: captured.append((result,output))))
    return captured


@pytest.mark.parametrize("ceiling", ["summary", "raw"])
def test_exact_reviewed_scalar_has_signed_full_authority_and_no_raw_records(fact_setup, ceiling):
    envelope,payload=issue(fact_setup,change=lambda p:p["rules"][0]["release"].update(ceiling=ceiling))
    [(result,output)]=dispatch(fact_setup,envelope,payload)
    assert output=={"family":"owner_stated_fact","operation":"read","view_id":"owner_stated_fact.scalar.v1",
        "subject":"self","predicate":"prefers","value":"history books"}
    assert "I enjoy" not in json.dumps(output) and fact_setup[5][2] not in json.dumps(output)
    assert result["authority"]["capability_version"]=="permissions-beta/p2b-v1"
    verify_node_result(result,trusted_keys={"node-key":fact_setup[3].public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)},
        envelope=envelope,output=output,now=AS_OF)
    with pytest.raises(PolicyError,match="request_replay"): dispatch(fact_setup,envelope,payload)
    with fact_setup[0].protocol.ledger._transaction() as db:
        stored=" ".join(str(tuple(r)) for r in db.execute("SELECT * FROM p2a_receipts"))
    assert "history books" not in stored and "I enjoy" not in stored


@pytest.mark.parametrize("kind",["inference","sources","tables","forms","processors","input_domain","output_domain","deny"])
def test_rules_and_empty_bounds_never_expand(fact_setup,kind):
    def change(p):
        rule=p["rules"][0]
        if kind=="inference": rule["release"]["ceiling"]="inference"
        elif kind in {"sources","processors"}: rule["evidence_use"][kind]["values"]=[]
        elif kind=="tables": rule["evidence_use"]["tables"]=[]
        elif kind=="forms": rule["release"]["forms"]=[]
        elif kind=="input_domain": rule["evidence_use"]["predicate"]["values"]=["finance"]
        elif kind=="output_domain": rule["release"]["predicate"]["values"]=["health"]
        else:
            deny=deepcopy(rule);deny.update(rule_id="exclude-reading",effect="deny");p["rules"].append(deny)
    envelope,payload=issue(fact_setup,change=change)
    with pytest.raises(PolicyError): dispatch(fact_setup,envelope,payload,send=lambda *_:pytest.fail("unauthorized output"))


@pytest.mark.parametrize("kind",["owner_only","evidence_revoked","output_revoked","source_changed","fact_changed","event_changed",
    "source_deleted","grant_revoked","expired","key_removed","protected","protect_lift"])
def test_current_authority_and_both_reviews_are_required_at_release(fact_setup,kind):
    envelope,payload=issue(fact_setup)
    service,_,_,_,now,corpus,review=fact_setup
    if kind=="owner_only": change_fact(corpus,disclosure="owner_only")
    elif kind=="evidence_revoked":
        with owner(): corpus[1].revoke_review("review-1")
    elif kind=="output_revoked":
        with owner(): service.projections.revoke(RevokeProjectionReview(fact_id=corpus[2],review_id=review.review_id,
            expected_review_revision=review.review_revision),now=AS_OF)
    elif kind=="source_changed": edit(corpus,"UPDATE conversation_messages SET content='changed private sentence'")
    elif kind=="fact_changed": change_fact(corpus,object_value="science books")
    elif kind=="event_changed": edit(corpus,"UPDATE conversation_messages SET event_at=?",(utc(AS_OF-1000),))
    elif kind=="source_deleted": edit(corpus,"DELETE FROM conversation_messages")
    elif kind=="grant_revoked":
        with owner(): service.protocol.ledger.revoke("grant-1",expected_epoch=1,command_id="revoke-1")
    elif kind=="expired": now[0]+=100
    elif kind=="key_removed": service.protocol.ledger.trusted_keys={}
    else:
        from topos.features.lifecycle.record_protection import RecordProtectionStore
        with owner(),sqlite3.connect(corpus[0].path) as db:
            store=RecordProtectionStore(db);store.protect(canonical_table="conversation_messages",record_id="message-1")
            if kind=="protect_lift": store.unprotect(canonical_table="conversation_messages",record_id="message-1")
    with pytest.raises(PolicyError): dispatch(fact_setup,envelope,payload,send=lambda *_:pytest.fail("stale output"))


@pytest.mark.parametrize("field,value",[("event_at",None),("event_at","2027-01-15T08:00:00"),("event_at",utc(AS_OF+1))])
def test_even_freshly_reviewed_invalid_time_is_not_authorized(fact_setup,field,value):
    service,_,_,_,_,corpus,review=fact_setup
    edit(corpus,f"UPDATE conversation_messages SET {field}=?",(value,))
    attest(corpus,review_id="evidence-new")
    from topos.permissions_v2.evidence_reviews import EvidenceLookup
    from topos.permissions_v2.projection_reviews import RecordProjectionReview
    with owner():
        preview=service.projections.preview(EvidenceLookup(fact_id=corpus[2]),now=AS_OF)
        service.projections.record(RecordProjectionReview(review_id="output-new",expected_candidate=preview.candidate,
            expected_candidate_hash=preview.candidate_hash,expected_current_review_revision=review.review_revision,
            classification={"domains":["reading"],"sensitivity":"personal","subject":"self","assertion":"explicit_atomic_preference"}),now=AS_OF)
    envelope,payload=issue(fact_setup)
    with pytest.raises(PolicyError): dispatch(fact_setup,envelope,payload)


@pytest.mark.parametrize("field",["environment_id","node_id","resource_id","owner_id","actor_id","client_id","grant_id","assignment_id",
    "request_id","request_type","capability_version","request_hash","policy_hash","protection_revision"])
def test_signed_cross_binding_or_tampering_never_dispatches(fact_setup,field):
    envelope,payload=issue(fact_setup)
    raw=envelope.model_dump();raw[field]="0"*64 if field.endswith("hash") or field=="protection_revision" else "other"
    with recipient(),pytest.raises(PolicyError):
        fact_setup[0].dispatch(envelope=raw,payload=payload,request_id="fact-read-1",send=lambda *_:pytest.fail("tampered output"))


def test_legacy_concrete_parsers_still_reject_fact_profile(fact_setup):
    envelope,_=issue(fact_setup)
    for model,raw in [(SignedEnvelope,envelope.model_dump()),(EnvelopeBody,envelope.model_dump(exclude={"signature"})),
        (AuthorityBinding,{key:getattr(envelope,key) for key in AuthorityBinding.model_fields}),
        (PolicyV2,fact_setup[1])]:
        with pytest.raises(PolicyError): model.parse(raw)
    assert isinstance(envelope,SignedFactEnvelope)


def test_output_type_substitution_fails_node_proof(fact_setup):
    envelope,payload=issue(fact_setup)
    [(result,output)]=dispatch(fact_setup,envelope,payload)
    keys={"node-key":fact_setup[3].public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)}
    for bad in [output|{"records":[]},output|{"value":"science books"},
        {"family":"canonical_record","operation":"read","view_id":"canonical.message_disclosure.v1","records":[]}]:
        with pytest.raises(PolicyError):verify_node_result(result,trusted_keys=keys,envelope=envelope,output=bad,now=AS_OF)


@pytest.mark.parametrize("floor",["diverged","unpublished"])
def test_release_requires_the_resolver_floor_it_read_to_equal_signed_protection(fact_setup,monkeypatch,floor):
    # The canonical read lock keeps protection writes out of the callback, so a
    # floor that differs from unchanged signed authority can only come from the
    # resolver's published floor itself; stub what the callback reads.
    envelope,payload=issue(fact_setup)
    resolver=fact_setup[0].projections.resolver
    published=[]
    class DivergedFloor(type(resolver)):
        @property
        def current_floor(self):
            real=self.__dict__.get("current_floor")
            published.append(real)
            return None if real is None or floor=="unpublished" else "f"*64
        @current_floor.setter
        def current_floor(self,value):
            self.__dict__["current_floor"]=value
    monkeypatch.setattr(resolver,"__class__",DivergedFloor)
    with pytest.raises(PolicyError,match="authority_stale"):
        dispatch(fact_setup,envelope,payload,send=lambda *_:pytest.fail("output sent under a mismatched floor"))
    assert published==[envelope.protection_revision] and resolver.__dict__["current_floor"] is None


def test_send_runs_after_every_gate_is_released(fact_setup):
    # R12 (bookkeeping batch 3): the checkpoint is taken under the canonical, both review
    # and the node write gates; the send runs after all of them are released.
    envelope,payload=issue(fact_setup)
    def send(result,output):
        for path in (fact_setup[5][1].path,fact_setup[0].projections.outputs.path):
            with sqlite3.connect(path,timeout=0,isolation_level=None) as db:
                db.execute("BEGIN IMMEDIATE");db.execute("ROLLBACK")
        from concurrent.futures import ThreadPoolExecutor
        from topos.storage.db.write_gate import db_write_lock
        with ThreadPoolExecutor(max_workers=1) as executor:
            def probe():
                acquired=db_write_lock().acquire(blocking=False)
                if acquired:db_write_lock().release()
                return acquired
            assert executor.submit(probe).result() is True
        assert output["value"]=="history books"
    dispatch(fact_setup,envelope,payload,send=send)


def signed_mutation(setup, *, operation="activate", generation=1, epoch=0, raw=None, command_id="signed-activate"):
    from topos.permissions_v2.protocol import MutationBody, sign_mutation
    service,policy,cp_key,_,now,_,_=setup
    raw=deepcopy(raw or policy)
    with service.protocol.ledger._transaction() as db: floor=service.protocol.ledger._node(db)["protection_revision"]
    authority={**raw["binding"],"grant_generation":generation,"assignment_generation":generation,
        "policy_version_id":raw["policy_version_id"],"policy_hash":digest(raw),"capability_version":raw["versions"]["capability"],
        "protection_revision":floor,"node_epoch":epoch+1}
    return sign_mutation(MutationBody.parse({"version":"topos-policy-mutation/v2","kid":"cp-key","issuer_id":"cp-issuer",
        "audience_id":service.protocol.ledger.identity.node_id,"command_id":command_id,"operation":operation,"expected_epoch":epoch,
        "authority":authority,"policy":raw if operation=="activate" else None,
        "owner_authorization":{"actor_id":raw["binding"]["owner_id"],"client_id":"owner-ui"},
        "issued_at":now[0],"expires_at":now[0]+100}),cp_key)


def test_signed_fact_activation_ack_status_restart_revoke(fact_setup):
    from topos.permissions_v2.protocol import StatusRequestBody, sign_status_request, verify_ack
    service,raw,cp_key,node_key,now,_,_=fact_setup
    command=signed_mutation(fact_setup)
    protocol=service.protocol
    ack=protocol.mutate(command.model_dump(),now=now[0])
    keys={"node-key":node_key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)}
    assert verify_ack(ack.model_dump(),trusted_keys=keys,issuer_id=protocol.ledger.identity.node_id,
        audience_id="cp-issuer",request=command,now=now[0]).state.authority.capability_version=="permissions-beta/p2b-v1"
    assert protocol.mutate(command.model_dump(),now=now[0]).outcome=="already_applied"
    with protocol.ledger._transaction() as db: floor=protocol.ledger._node(db)["protection_revision"]
    ledger=PolicyLedger(protocol.ledger.path,identity=protocol.ledger.identity,protection_revision=floor,trusted_keys=protocol.ledger.trusted_keys)
    restarted=NodePolicyProtocol(ledger,canonical_database=protocol.canonical_database,cp_issuer_id="cp-issuer",frontend_client_id="owner-ui",
        trusted_cp_keys=protocol.trusted_cp_keys,node_signing_kid="node-key",node_signing_key=node_key)
    request=sign_status_request(StatusRequestBody.parse({"version":"topos-policy-status-request/v2","kid":"cp-key","issuer_id":"cp-issuer",
        "audience_id":ledger.identity.node_id,"request_id":"status-1","binding":raw["binding"],"command_id":None,"command_hash":None,
        "issued_at":now[0],"expires_at":now[0]+100}),cp_key)
    assert restarted.status(request.model_dump(),now=now[0]).state.authority==ack.state.authority
    revoke=signed_mutation(fact_setup,operation="revoke",generation=2,epoch=1,command_id="signed-revoke")
    assert restarted.mutate(revoke.model_dump(),now=now[0]).state.grant_state=="revoked"
    assert restarted.mutate(command.model_dump(),now=now[0]).state.grant_state=="revoked"


@pytest.mark.parametrize("via_protocol",[False,True])
@pytest.mark.parametrize("revoked",[False,True])
def test_capability_change_needs_new_grant_even_with_owner_signature(fact_setup,via_protocol,revoked):
    from tests.permissions_v2.test_contract_and_ledger import sample_policy
    service,raw,_,_,now,_,_=fact_setup
    old=sample_policy();old["binding"]=raw["binding"];old["validity"]=raw["validity"]
    old["policy_version_id"]="legacy-policy"
    with owner(): service.protocol.ledger.activate(old,grant_generation=1,assignment_generation=1,expected_epoch=0,command_id="legacy",now=now[0])
    epoch,generation=1,2
    if revoked:
        with owner():service.protocol.ledger.revoke("grant-1",expected_epoch=1,command_id="legacy-revoke")
        epoch,generation=2,3
    if via_protocol:
        command=signed_mutation(fact_setup,epoch=epoch,generation=generation)
        ack=service.protocol.mutate(command.model_dump(),now=now[0])
        assert ack.outcome=="rejected" and ack.reason_code=="binding_conflict"
        assert ack.state.authority.capability_version=="permissions-beta/p2a-v1"
    else:
        with owner(),pytest.raises(PolicyError,match="capability_change_requires_new_grant"):
            service.protocol.ledger.activate(raw,grant_generation=generation,assignment_generation=generation,expected_epoch=epoch,
                command_id="new-capability",now=now[0])


def test_even_trusted_signer_cannot_issue_beyond_policy_expiry(fact_setup):
    envelope,payload=issue(fact_setup,change=lambda p:p["validity"].update(expires_at=AS_OF+10))
    with pytest.raises(PolicyError,match="envelope_policy_time"):dispatch(fact_setup,envelope,payload)


def test_expiry_during_evaluation_blocks_release(fact_setup,monkeypatch):
    from topos.permissions_v2 import fact_release
    envelope,payload=issue(fact_setup)
    original=fact_release.fact_projection_decision
    def delayed(**kwargs):
        result=original(**kwargs);fact_setup[4][0]+=100;return result
    monkeypatch.setattr(fact_release,"fact_projection_decision",delayed)
    with pytest.raises(PolicyError):dispatch(fact_setup,envelope,payload,send=lambda *_:pytest.fail("expired output"))


def test_fact_golden_signatures_and_schema_exports_are_exact():
    from pathlib import Path
    from topos.permissions_v2.fact_contract import FactPolicyV2
    from topos.permissions_v2.signing import FactAuthorityBinding,FactRequestContext,verify_envelope,signing_bytes
    from topos.permissions_v2.protocol import SignedMutation,SignedAck,verify_ack,protocol_signing_bytes,command_digest
    from topos.permissions_v2.forwarding import SignedNodeResult,node_result_signing_bytes
    from topos.permissions_v2.canonical import canonical_bytes
    base=Path(__file__).resolve().parents[2]/"fixtures"/"permissions_v2"
    golden=json.loads((base/"fact_policy"/"signed-golden-v1.json").read_text())
    parsed=FactPolicyV2.parse(golden["policy"])
    assert canonical_bytes(parsed.model_dump()).decode("ascii")==golden["policy_canonical"]
    assert digest(parsed.model_dump())==golden["policy_hash"]
    envelope=verify_envelope(golden["envelope"],trusted_keys={"cp-key":bytes.fromhex(golden["cp_public_key_hex"])},
        expected_authority=FactAuthorityBinding.parse(golden["authority"]),request=FactRequestContext.parse(golden["request"]),
        payload=golden["payload"],now=golden["now"])
    keys={"node-key":bytes.fromhex(golden["node_public_key_hex"])}
    result=verify_node_result(golden["result"],trusted_keys=keys,envelope=envelope,output=golden["output"],now=golden["now"])
    mutation=SignedMutation.parse(golden["mutation"])
    ack=verify_ack(golden["ack"],trusted_keys=keys,issuer_id=envelope.node_id,audience_id="beta-cp",request=mutation,now=golden["now"])
    assert command_digest(mutation)==golden["command_hash"]
    for key,value in {"envelope":signing_bytes(envelope),"result":node_result_signing_bytes(result),
        "mutation":protocol_signing_bytes(mutation),"ack":protocol_signing_bytes(ack)}.items():
        assert value.decode("ascii")==golden["signing_text"][key]
    for model in (FactAuthorityBinding,FactEnvelopeBody,SignedFactEnvelope,FactRequestContext):
        assert json.loads((base/"fact_policy"/(model.__name__+".schema.json")).read_text())==model.model_json_schema()
    assert json.loads((base/"SignedNodeResult.schema.json").read_text())==SignedNodeResult.model_json_schema()
