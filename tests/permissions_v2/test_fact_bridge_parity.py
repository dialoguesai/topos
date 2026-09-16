"""Arm A of the offline bridge is the serving decision, for every fact capability.

Each case assembles the real signed release (ledger, signed envelope, recipient
dispatch) for one capability, lets it checkpoint its decision, and runs the
bridge's rules arm over the same scratch corpus, reviews and policy. The two
decisions must agree field by field: the bridge may not pick its own subject
rule, output family or view. The prose arm over the same fixtures must never be
shown an entity id, and its evidence stage must never be shown the output value.
"""
import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.permissions_v2.test_evidence import corpus, owner, attest, payload as change_fact
from tests.permissions_v2.test_fact_attested_subject import attested_policy
from tests.permissions_v2.test_fact_bridge import Fake, capsule, judgment
from tests.permissions_v2.test_fact_policy import timed, policy, rule, AS_OF
from tests.permissions_v2.test_fact_release import dispatch, issue
from tests.permissions_v2.test_fact_stated_day import ELAPSED, set_valid_from, stated_day_policy
from tests.permissions_v2.test_fact_work_family import work_policy
from tests.permissions_v2.test_owner_identity_binding import OWNER, add_entity, db, do_attest
from tests.permissions_v2.test_projection_reviews import service as projection_service
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.contract import Binding
from topos.permissions_v2.evidence_reviews import EvidenceLookup
from topos.permissions_v2.experiments.fact_bridge import FactShadowBridge
from topos.permissions_v2.fact_contract import FAMILY, VIEW, WORK_ASSERTION, WORK_FAMILY, WORK_VIEW
from topos.permissions_v2.fact_release import FactProjectionRelease
from topos.permissions_v2.identity import ATTESTED_CONTRACT, LEGACY_CONTRACT
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.projection_reviews import RecordProjectionReview

CAPABILITIES = ["p2b-v1", "p2b-v2", "p2b-v3", "p2b-v4"]
BUILDERS = {"p2b-v1": policy, "p2b-v2": stated_day_policy, "p2b-v3": attested_policy, "p2b-v4": work_policy}
# Synthetic canaries: an output value no evidence leaf contains, so its presence
# in an evidence-stage request can only mean the fact scalar leaked there.
VALUE_CANARY = {"p2b-v3": "Contoso almanacs", "p2b-v4": "Northwind Traders"}


def reviewed_corpus(capability, corpus, service):
    """Record both owner reviews under the contract and family the capability fixes."""
    contract, family, assertion = LEGACY_CONTRACT, FAMILY, "explicit_atomic_preference"
    if capability == "p2b-v2":
        set_valid_from(corpus, ELAPSED)
    if capability == "p2b-v3":
        # Two self rows and an entity subject: the case the legacy rule refuses.
        with db(corpus) as conn:
            add_entity(conn, "second-self")
        change_fact(corpus, subject_entity_id=OWNER, object_value=VALUE_CANARY[capability])
    if capability == "p2b-v4":
        change_fact(corpus, subject_entity_id=OWNER, predicate="works_at", object_value=VALUE_CANARY[capability])
        family, assertion = WORK_FAMILY, WORK_ASSERTION
    if capability in ("p2b-v3", "p2b-v4"):
        contract = ATTESTED_CONTRACT
        with db(corpus) as conn:
            do_attest(conn, OWNER)
    attest(corpus, review_id="review-parity")
    with owner():
        preview = service.preview(EvidenceLookup(fact_id=corpus[2]), now=1200, contract=contract, family=family)
        assert preview.candidate is not None, preview.candidate_reason_code
        service.record(RecordProjectionReview(review_id="output-parity", expected_candidate=preview.candidate,
            expected_candidate_hash=preview.candidate_hash, expected_current_review_revision=None,
            classification={"domains": ["reading"], "sensitivity": "personal", "subject": "self", "assertion": assertion}),
            now=1200, contract=contract, family=family)


def raw_policy(capability, corpus, outcome):
    raw = BUILDERS[capability](corpus)
    if outcome == "deny":
        denied = rule("deny-reading", "deny", "reading")
        denied["release"]["forms"] = raw["rules"][0]["release"]["forms"]
        raw["rules"].append(denied)
    return raw


def serving_decision(corpus, service, raw, tmp_path):
    """The decision the shipped release checkpoints for this policy, permit or not."""
    cp_key, node_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    cp_keys = {"cp-key": cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    with corpus[0]._read() as (_conn, floor):
        ledger = PolicyLedger(tmp_path / "ledger.db", identity=NodeIdentity.parse(corpus[0].binding.model_dump()),
                              protection_revision=floor, trusted_keys=cp_keys)
    protocol = NodePolicyProtocol(ledger, canonical_database=corpus[0].path, cp_issuer_id="cp-issuer",
        frontend_client_id="owner-ui", trusted_cp_keys=cp_keys, node_signing_kid="node-key", node_signing_key=node_key)
    now = [AS_OF]
    setup = (FactProjectionRelease(protocol=protocol, projections=service, clock=lambda: now[0]), raw, cp_key, node_key,
             now, corpus, None)
    envelope, payload = issue(setup)
    try:
        dispatch(setup, envelope, payload)
    except PolicyError as exc:
        assert exc.code == "permission_denied"
    with sqlite3.connect(ledger.path) as conn:
        [row] = conn.execute("SELECT decision_json FROM p2a_receipts").fetchall()
    return json.loads(row[0])


def shadow(corpus, service, raw, transport=None):
    return FactShadowBridge(capsule(raw), projections=service, binding=Binding.parse(raw["binding"]),
                            clock=lambda: AS_OF, transport=transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["permit", "deny"])
@pytest.mark.parametrize("capability", CAPABILITIES)
async def test_rules_arm_is_the_serving_decision_for_every_capability(timed, projection_service, tmp_path, capability, outcome):
    reviewed_corpus(capability, timed, projection_service)
    raw = raw_policy(capability, timed, outcome)
    served = serving_decision(timed, projection_service, raw, tmp_path)
    assert served["verdict"] == outcome
    result = await shadow(timed, projection_service, raw).run(timed[2], request_as_of=AS_OF, arm="rules_v2")
    [decision] = result.stages
    fields = ("stage", "verdict", "reason_code", "policy_hash", "candidate_revision", "matched_allow_clause_ids",
              "matched_deny_clause_ids", "required_projection_id", "missing_context_codes")
    assert {field: getattr(decision, field) for field in fields} == {field: served[field] for field in fields}
    assert decision.withheld_code is None and result.model_calls == 0
    if outcome == "permit":
        assert decision.required_projection_id == (WORK_VIEW if capability == "p2b-v4" else VIEW)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["p2b-v3", "p2b-v4"])
async def test_prose_arm_never_sees_entity_ids_and_evidence_stage_never_sees_the_output_value(timed, projection_service, capability):
    reviewed_corpus(capability, timed, projection_service)
    raw = raw_policy(capability, timed, "permit")
    view = WORK_VIEW if capability == "p2b-v4" else VIEW
    model = Fake([judgment(required_projection_id=view)] * 2)
    result = await shadow(timed, projection_service, raw, transport=model).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert (result.verdict, result.stages[-1].reason_code, result.stages[-1].required_projection_id) == ("permit", "semantic_permit", view)
    with db(timed) as conn:
        entity_ids = [row[0] for row in conn.execute("SELECT entity_id FROM entities")]
    assert OWNER in entity_ids and len(entity_ids) >= 1
    evidence_call, output_call = model.calls
    assert evidence_call.stage == "evidence_use" and output_call.stage == "output_release"
    for call in model.calls:
        serialized = call.model_dump_json()
        assert not [entity for entity in entity_ids if entity in serialized], call.stage
        assert json.loads(call.system.split("\nAPPROVED_POLICY_JSON\n", 1)[1])["form"] == view
    assert VALUE_CANARY[capability] not in evidence_call.model_dump_json()
    # The value is inspected where it is proposed, and only there.
    assert VALUE_CANARY[capability] in output_call.candidate_data
    assert "I enjoy reading history books." in evidence_call.candidate_data


@pytest.mark.asyncio
async def test_a_work_permit_must_name_the_work_view(timed, projection_service):
    reviewed_corpus("p2b-v4", timed, projection_service)
    raw = raw_policy("p2b-v4", timed, "permit")
    model = Fake([judgment(required_projection_id=WORK_VIEW), judgment()])
    result = await shadow(timed, projection_service, raw, transport=model).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert [stage.reason_code for stage in result.stages] == ["semantic_permit", "projection_required"]
    assert result.verdict == "indeterminate"
