"""Owner review service, exact relay boundary, durable enrollment and CAS."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from topos.core.handlers import handle_control_plane_request
from topos.permissions_v2 import evidence_reviews as schemas
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.evidence_review_runtime import ReviewEnrollmentRuntime
from topos.permissions_v2.evidence_reviews import (EvidenceLookup, EvidenceReviewService,
    OwnerEvidencePreview, RecordEvidenceReview, RevokeEvidenceReview)
from topos.permissions_v2.runtime import load_runtime
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from tests.permissions_v2.test_evidence import corpus, owner, attest, edit, payload  # noqa: F401


@pytest.fixture
def service(corpus):
    return EvidenceReviewService(corpus[0], corpus[1])


def create_request(corpus, *, review_id="service-review", expected=None):
    review = attest(corpus, review_id="preparation-review")
    with owner():
        corpus[1].revoke_review(review.review_id)
    return RecordEvidenceReview(review_id=review_id, expected_snapshot=review.snapshot,
        expected_current_review_revision=expected, classifications=review.classifications)


@pytest.mark.parametrize("name", ["EvidenceLookup", "RecordEvidenceReview", "RevokeEvidenceReview",
    "EvidenceReviewState", "OwnerEvidencePreview", "EvidenceReviewMutation"])
def test_owner_review_wire_schemas_match_authoritative_models(name):
    path = Path(__file__).resolve().parents[2] / "fixtures/permissions_v2/evidence_reviews" / f"{name}.schema.json"
    assert json.loads(path.read_text()) == getattr(schemas, name).model_json_schema()


def test_owner_preview_preserves_exact_sqlite_cells_and_bound_snapshot(corpus, service):
    edit(corpus, "ALTER TABLE conversation_messages ADD COLUMN binary_test BLOB")
    edit(corpus, "UPDATE conversation_messages SET binary_test=?", (b"\x00\xff",))
    with owner():
        result = service.preview(EvidenceLookup(fact_id=corpus[2]))
    assert result.status == "complete" and len(result.records) == 2
    assert OwnerEvidencePreview.parse(result.model_dump()) == result
    fact, leaf = result.records
    assert fact.cells["confidence"].kind == "float"
    assert float.fromhex(fact.cells["confidence"].value) == 0.7
    assert leaf.cells["content"].value == "I enjoy reading history books."
    assert leaf.cells["binary_test"].value == "00ff"
    assert leaf.cells["deleted_at"].kind == "null"
    assert leaf.cells["is_from_self"].value == "1"
    assert result.current_review is None and result.execution_enabled is False


def test_owner_inspects_incomplete_owner_only_fact_but_cannot_submit_partial_review(corpus, service):
    payload(corpus, disclosure="owner_only")
    edit(corpus, "UPDATE signal_objects SET source_refs_json='[]'")
    with owner():
        result = service.preview(EvidenceLookup(fact_id=corpus[2]))
    assert result.status == "incomplete" and result.snapshot is None and result.reason_code == "lineage_missing"
    assert len(result.records) == 1 and result.records[0].disclosure == "owner_only"
    assert result.qualification.reason_code == "owner_only"
    with pytest.raises(PolicyError, match="schema_invalid"):
        RecordEvidenceReview.parse({"review_id":"bad", "expected_snapshot":None,
            "expected_current_review_revision":None, "classifications":[]})


def test_owner_only_is_never_cleared_by_owner_preview_or_review(corpus, service):
    payload(corpus, disclosure="owner_only")
    request = create_request(corpus)
    with owner():
        result = service.record(request, now=1200)
        preview = service.preview(EvidenceLookup(fact_id=corpus[2]))
    assert result.state.qualification.reason_code == "owner_only"
    assert preview.records[0].disclosure == "owner_only"


def test_review_server_time_retry_current_revision_cas_and_revoke(corpus, service):
    request = create_request(corpus)
    with owner():
        first = service.record(request, now=1200)
        retry = service.record(request, now=1201)
        assert first == retry and first.state.current_review.reviewed_at == 1200
        assert first.state.qualification.verdict == "qualified"
        stale = request.model_copy(update={"review_id":"other-review"})
        with pytest.raises(PolicyError, match="review_conflict"):
            service.record(stale, now=1202)
        current = stale.model_copy(update={"expected_current_review_revision":first.review_revision})
        second = service.record(current, now=1203)
        with pytest.raises(PolicyError, match="review_conflict"):
            service.revoke(RevokeEvidenceReview(fact_id=corpus[2],review_id=first.review_id,expected_review_revision=first.review_revision))
        revoke = RevokeEvidenceReview(fact_id=corpus[2],review_id=second.review_id,expected_review_revision=second.review_revision)
        revoked = service.revoke(revoke)
        assert revoked.state.current_review is None and revoked.state.qualification.reason_code == "owner_review_required"
        assert service.revoke(revoke) == revoked
        with pytest.raises(PolicyError, match="review_id_conflict"):
            service.record(current, now=1204)


def test_changed_source_between_preview_and_review_is_conflict(corpus, service):
    request = create_request(corpus)
    edit(corpus, "UPDATE conversation_messages SET content='changed source'")
    with owner(), pytest.raises(PolicyError, match="review_stale"):
        service.record(request, now=1200)


def test_owner_can_revoke_a_review_after_its_fact_is_deleted(corpus, service):
    request = create_request(corpus)
    with owner():
        created = service.record(request, now=1200)
    edit(corpus,"DELETE FROM signal_objects")
    with owner():
        result = service.revoke(RevokeEvidenceReview(fact_id=corpus[2],review_id=created.review_id,
            expected_review_revision=created.review_revision))
    assert result.action == "revoked" and result.state.current_review is None
    assert result.state.qualification.reason_code == "evidence_missing"


def test_qualified_callback_holds_review_gate_until_delivery_returns(corpus):
    review = attest(corpus)
    entered, attempted, finished, release = (threading.Event() for _ in range(4))
    def deliver(evidence, rows):
        assert evidence.review_id == review.review_id
        assert any(row.get("content") == "I enjoy reading history books." for row in rows.values())
        entered.set()
        assert release.wait(3)
        assert not finished.is_set()
        return "delivered"
    def revoke():
        attempted.set()
        with owner():
            corpus[1].revoke_review(review.review_id)
        finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        delivery = pool.submit(corpus[0].with_qualified, corpus[2], reviews=corpus[1], callback=deliver)
        assert entered.wait(2)
        pending = pool.submit(revoke)
        assert attempted.wait(2) and not finished.is_set()
        release.set()
        assert delivery.result(3) == "delivered"
        pending.result(3)
    with pytest.raises(PolicyError, match="owner_review_required"):
        corpus[0].with_qualified(corpus[2], reviews=corpus[1], callback=lambda *_: pytest.fail("revoked review delivered"))


def test_qualified_callback_also_holds_private_sqlite_write_transaction(corpus):
    attest(corpus)
    def deliver(_evidence,_rows):
        with sqlite3.connect(corpus[1].path,timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError,match="locked"):
                other.execute("UPDATE fact_reviews SET active=0")
        return "current"
    assert corpus[0].with_qualified(corpus[2],reviews=corpus[1],callback=deliver) == "current"


def test_with_qualified_never_invokes_release_for_owner_only_fact(corpus):
    payload(corpus,disclosure="owner_only")
    attest(corpus)
    with pytest.raises(PolicyError,match="owner_only"):
        corpus[0].with_qualified(corpus[2],reviews=corpus[1],callback=lambda *_: pytest.fail("owner-only evidence released"))


@pytest.fixture
def paired_runtime(corpus, tmp_path, monkeypatch):
    from topos.permissions_v2 import runtime as runtime_module
    durable = tmp_path / "permissions-v2"
    durable.mkdir(mode=0o700)
    signing = durable / "node.key"
    signing.write_text(bytes(range(32)).hex())
    signing.chmod(0o600)
    config = {"version":"topos-policy-node-config/v1", "identity":corpus[0].binding.model_dump(),
        "cp_issuer_id":"beta-cp", "frontend_client_id":"permissions-beta-web", "trusted_cp_keys":{"cp-key":"a"*64},
        "node_signing_kid":"node-key", "node_signing_key_path":str(signing), "canonical_database_path":str(corpus[0].path),
        "ledger_path":str(durable / "ledger.db"), "evidence_review_store_path":str(durable / "reviews.db")}
    path = durable / "config.json"
    path.write_text(json.dumps(config)); path.chmod(0o600)
    runtime = load_runtime(path, active_database=corpus[0].path)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ENABLED", "true")
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_CONFIG_PATH", str(path))
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_EVIDENCE_REVIEWS_ENABLED", "true")
    monkeypatch.setattr(runtime_module, "_runtime", runtime)
    try:
        yield runtime, config, path
    finally:
        runtime.close()


def test_existing_only_runtime_cannot_enroll_but_owner_preview_can(corpus, paired_runtime):
    runtime, config, path = paired_runtime
    with pytest.raises(PolicyError, match="evidence_reviews_not_enrolled"):
        runtime.evidence_reviews(require_existing=True)
    assert not Path(config["evidence_review_store_path"]).exists()
    with owner():
        service = runtime.evidence_reviews(require_existing=False)
        assert service.preview(EvidenceLookup(fact_id=corpus[2])).status == "complete"
    assert runtime.evidence_reviews(require_existing=True) is service
    runtime.close()
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        assert reopened.evidence_reviews(require_existing=True).reviews._file_identity == service.reviews._file_identity
    finally:
        reopened.close()


def test_existing_service_read_methods_do_not_become_recipient_preview_access(corpus, paired_runtime):
    with owner():
        paired_runtime[0].evidence_reviews(require_existing=False)
    with owner(cls=THIRD_PARTY,channel="cp_relay"):
        service = paired_runtime[0].evidence_reviews(require_existing=True)
        with pytest.raises(PolicyError,match="owner_authority_required"):
            service.preview(EvidenceLookup(fact_id=corpus[2]))
        with pytest.raises(PolicyError,match="owner_authority_required"):
            service.read(EvidenceLookup(fact_id=corpus[2]))


@pytest.mark.parametrize("field,value", [("evidence_review_store_path","/private/tmp/unbound.db"),
    ("evidence_review_store_path","node.key"), ("evidence_review_store_path",None)])
def test_review_runtime_requires_explicit_private_config(corpus, paired_runtime, field, value):
    runtime, config, path = paired_runtime
    runtime.close()
    config[field] = value
    path.write_text(json.dumps(config))
    if value is not None:
        with pytest.raises(PolicyError,match="review_database_binding"):
            load_runtime(path,active_database=corpus[0].path)
    else:
        reopened = load_runtime(path,active_database=corpus[0].path)
        try:
            with owner(),pytest.raises(PolicyError,match="evidence_reviews_not_configured"):
                reopened.evidence_reviews(require_existing=False)
        finally:
            reopened.close()


@pytest.mark.parametrize("damage", ["store", "marker", "pending", "identity"])
def test_enrolled_runtime_never_recreates_lost_or_incomplete_store(corpus, paired_runtime, damage):
    runtime, config, path = paired_runtime
    with owner():
        runtime.evidence_reviews(require_existing=False)
    store = Path(config["evidence_review_store_path"])
    marker = store.with_name(store.name + ".enrollment.json")
    if damage == "store":
        store.unlink()
    elif damage == "marker":
        marker.unlink()
    elif damage == "pending":
        body = json.loads(marker.read_text()); body["state"] = "pending"; marker.write_text(json.dumps(body))
    else:
        with sqlite3.connect(store) as conn:
            conn.execute("DELETE FROM review_identity")
    runtime.close()
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        with owner(), pytest.raises(PolicyError):
            reopened.evidence_reviews(require_existing=False)
        if damage == "store":
            assert not store.exists()
    finally:
        reopened.close()


def message(corpus, operation="preview", request=None, binding=None):
    return {"id":"request-1", "type":"permissions_v2_evidence_"+operation,
        "payload":{"binding":binding or corpus[0].binding.model_dump(), "request":request or {"fact_id":corpus[2]}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [None, Principal(THIRD_PARTY,"cp_relay",acting_user="owner-1"),
    Principal(OWNER_APP,"local_http",acting_user="owner-1"), Principal(OWNER_APP,"uds"),
    Principal(OWNER_APP,"cp_relay",acting_user="wrong-owner")])
async def test_actual_dispatch_rejects_nonowner_or_unbound_actor(corpus, paired_runtime, principal):
    response = await handle_control_plane_request(message(corpus), principal=principal)
    assert response["code"] == 403 and "payload" not in response
    assert not Path(paired_runtime[1]["evidence_review_store_path"]).exists()


@pytest.mark.asyncio
async def test_actual_dispatch_target_binding_precedes_content_and_mutation(corpus, paired_runtime):
    binding = corpus[0].binding.model_dump() | {"node_id":"wrong-node"}
    response = await handle_control_plane_request(message(corpus,binding=binding),
        principal=Principal(OWNER_APP,"cp_relay",acting_user="owner-1"))
    assert response["code"] == 403 and response["error"] == "evidence_target_binding"
    assert not Path(paired_runtime[1]["evidence_review_store_path"]).exists()


@pytest.mark.asyncio
async def test_actual_dispatch_disabled_and_owner_positive(corpus, paired_runtime, monkeypatch):
    principal = Principal(OWNER_APP,"cp_relay",acting_user="owner-1")
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_EVIDENCE_REVIEWS_ENABLED")
    denied = await handle_control_plane_request(message(corpus),principal=principal)
    assert denied["code"] == 503 and denied["error"] == "evidence_reviews_disabled"
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_EVIDENCE_REVIEWS_ENABLED", "true")
    allowed = await handle_control_plane_request(message(corpus),principal=principal)
    assert allowed["status"] == "ok"
    assert OwnerEvidencePreview.parse(allowed["payload"]).status == "complete"


@pytest.mark.asyncio
async def test_actual_dispatch_owner_snapshot_conflict_and_revoke_errors(corpus, paired_runtime):
    principal = Principal(OWNER_APP,"cp_relay",acting_user="owner-1")
    result = await handle_control_plane_request(message(corpus), principal=principal)
    preview = OwnerEvidencePreview.parse(result["payload"])
    request = create_request(corpus).model_dump() | {"expected_snapshot":preview.snapshot.model_dump()}
    created = await handle_control_plane_request(message(corpus,"review_record",request),principal=principal)
    assert created["status"] == "ok"
    state = await handle_control_plane_request(message(corpus,"review_read"),principal=principal)
    assert state["payload"]["current_review"]["review_id"] == request["review_id"]
    bad_revoke = {"fact_id":corpus[2],"review_id":request["review_id"],"expected_review_revision":"0"*64}
    assert (await handle_control_plane_request(message(corpus,"review_revoke",bad_revoke),principal=principal))["code"] == 409
    bad_revoke["review_id"] = "missing"
    assert (await handle_control_plane_request(message(corpus,"review_revoke",bad_revoke),principal=principal))["code"] == 404
    edit(corpus,"UPDATE conversation_messages SET content='new private content'")
    request["review_id"] = "stale-review"
    conflict = await handle_control_plane_request(message(corpus,"review_record",request),principal=principal)
    assert conflict["code"] == 409 and "new private content" not in json.dumps(conflict)


@pytest.mark.asyncio
async def test_preview_size_limit_never_returns_partial_content(corpus, paired_runtime, monkeypatch):
    monkeypatch.setattr(schemas,"MAX_PREVIEW_BYTES",100)
    result = await handle_control_plane_request(message(corpus),principal=Principal(OWNER_APP,"cp_relay",acting_user="owner-1"))
    assert result["code"] == 413 and "payload" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"request":{"fact_id":"x"}}, {"binding":{},"request":{},"owner":True},
    {"binding":{},"request":{"fact_id":"x","owner_reviewed":True}}])
async def test_request_flags_and_incomplete_binding_are_not_accepted(corpus, paired_runtime, payload):
    request = message(corpus); request["payload"] = payload
    result = await handle_control_plane_request(request,principal=Principal(OWNER_APP,"cp_relay",acting_user="owner-1"))
    assert result["code"] == 400 and "payload" not in result
