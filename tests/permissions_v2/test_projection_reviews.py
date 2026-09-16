"""Authenticated output review persistence, revocation and final-callback gates."""
import json
import sqlite3
from pathlib import Path

import pytest

from tests.permissions_v2.test_evidence import corpus, owner, attest, edit, payload
from tests.permissions_v2.test_evidence_reviews import paired_runtime
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence_reviews import EvidenceLookup
from topos.permissions_v2.projection_reviews import (ProjectionReviewStore, ProjectionReviewService,
    RecordProjectionReview, RevokeProjectionReview, OwnerProjectionPreview)
from topos.permissions_v2.runtime import load_runtime
from topos.principal import THIRD_PARTY, OWNER_APP, Principal


@pytest.fixture
def service(corpus, tmp_path):
    with owner():
        store = ProjectionReviewStore(tmp_path / "outputs.db", resolver=corpus[0])
    return ProjectionReviewService(corpus[0], corpus[1], store)


def prepare(corpus, service):
    attest(corpus)
    with owner():
        preview = service.preview(EvidenceLookup(fact_id=corpus[2]), now=1200)
    return RecordProjectionReview(review_id="output-review-1", expected_candidate=preview.candidate,
        expected_candidate_hash=preview.candidate_hash, expected_current_review_revision=None,
        classification={"domains":["reading"], "sensitivity":"personal", "subject":"self", "assertion":"explicit_atomic_preference"})


def lookup(corpus):
    return EvidenceLookup(fact_id=corpus[2])


def test_owner_preview_requires_evidence_review_and_returns_exact_scalar(corpus, service):
    with owner():
        absent = service.preview(lookup(corpus), now=1200)
    assert absent.candidate is None and absent.candidate_reason_code == "owner_review_required"
    request = prepare(corpus, service)
    with owner():
        preview = service.preview(lookup(corpus), now=1200)
    assert OwnerProjectionPreview.parse(preview.model_dump()) == preview
    assert preview.candidate.output.value == "history books"
    assert preview.qualification.reason_code == "output_review_required"
    assert preview.minimum_output_sensitivity == "personal"
    assert "I enjoy reading" not in preview.model_dump_json()
    assert preview.execution_enabled is False and preview.authorization_status == "not_evaluated"
    assert request.expected_candidate == preview.candidate


def test_review_current_revision_cas_idempotency_revoke_and_reopen(corpus, service):
    request = prepare(corpus, service)
    with owner():
        first = service.record(request, now=1200)
        assert first == service.record(request, now=1201)
        assert first.state.qualification.verdict == "reviewed"
        replacement = request.model_copy(update={"review_id":"output-review-2"})
        with pytest.raises(PolicyError, match="output_review_conflict"):
            service.record(replacement, now=1202)
        second = service.record(replacement.model_copy(update={"expected_current_review_revision":first.review_revision}), now=1203)
        with pytest.raises(PolicyError, match="output_review_conflict"):
            service.revoke(RevokeProjectionReview(fact_id=corpus[2], review_id=first.review_id, expected_review_revision=first.review_revision), now=1204)
        revoke = RevokeProjectionReview(fact_id=corpus[2], review_id=second.review_id, expected_review_revision=second.review_revision)
        revoked = service.revoke(revoke, now=1204)
        assert service.revoke(revoke, now=1205) == revoked
        assert revoked.state.current_review is None
        with pytest.raises(PolicyError, match="output_review_id_conflict"):
            service.record(replacement, now=1206)
    reopened = ProjectionReviewService(corpus[0], corpus[1], ProjectionReviewStore(service.outputs.path, resolver=corpus[0], _existing_only=True))
    with owner():
        assert reopened.read(lookup(corpus), now=1207).current_review is None
    with pytest.raises(PolicyError, match="output_review_required"):
        reopened.with_reviewed(corpus[2], now=1207, callback=lambda *_: pytest.fail("revoked output released"))


@pytest.mark.parametrize("change", ["source", "fact", "evidence_revoke", "evidence_replace", "owner_only", "missing"])
def test_evidence_and_output_revisions_are_independent_and_current(corpus, service, change):
    request = prepare(corpus, service)
    with owner():
        created = service.record(request, now=1200)
    if change == "source": edit(corpus, "UPDATE conversation_messages SET content='changed private text'")
    elif change == "fact": payload(corpus, object_value="science books")
    elif change == "evidence_revoke":
        with owner(): corpus[1].revoke_review("review-1")
    elif change == "evidence_replace": attest(corpus, review_id="evidence-2")
    elif change == "owner_only": payload(corpus, disclosure="owner_only")
    else: edit(corpus, "DELETE FROM signal_objects")
    with owner():
        state = service.read(lookup(corpus), now=1201)
        assert state.qualification.verdict == "withheld"
        assert state.current_review_revision == created.review_revision
        revoked = service.revoke(RevokeProjectionReview(fact_id=corpus[2], review_id=created.review_id,
            expected_review_revision=created.review_revision), now=1202)
    assert revoked.state.current_review is None
    with pytest.raises(PolicyError):
        service.with_reviewed(corpus[2], now=1203, callback=lambda *_: pytest.fail("stale output released"))


@pytest.mark.parametrize("kind", ["stale_candidate", "lower_sensitivity", "hash", "false_execution"])
def test_output_review_cannot_override_evidence_or_widen_output(corpus, service, kind):
    request = prepare(corpus, service)
    if kind == "stale_candidate": edit(corpus, "UPDATE conversation_messages SET content='changed'")
    elif kind == "lower_sensitivity": request = request.model_copy(update={"classification":request.classification.model_copy(update={"sensitivity":"none"})})
    elif kind == "hash": request = request.model_copy(update={"expected_candidate_hash":"0"*64})
    else:
        with pytest.raises(PolicyError): RecordProjectionReview.parse(request.model_dump() | {"execution_enabled":True})
        return
    with owner(), pytest.raises(PolicyError): service.record(request, now=1200)
    with service.outputs._db() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_reviews").fetchone() == (0,)


@pytest.mark.parametrize("operation", ["preview", "read", "record", "revoke"])
def test_recipient_and_wrong_owner_cannot_inspect_or_mutate(corpus, service, operation):
    request = prepare(corpus, service)
    with owner(): result = service.record(request, now=1200)
    arguments = {"preview":lookup(corpus), "read":lookup(corpus), "record":request,
        "revoke":RevokeProjectionReview(fact_id=corpus[2], review_id=result.review_id, expected_review_revision=result.review_revision)}
    for actor, cls in [("owner-1", THIRD_PARTY),("wrong-owner", OWNER_APP)]:
        with owner(actor=actor, cls=cls, channel="cp_relay"), pytest.raises(PolicyError, match="owner_authority_required"):
            getattr(service, operation)(arguments[operation], now=1201)


def test_final_callback_holds_both_review_sqlite_writes_and_returns_minimal_output(corpus, service):
    request = prepare(corpus, service)
    with owner(): service.record(request, now=1200)
    def deliver(evidence, projection, rows, permits):
        assert projection.candidate.evidence_review_revision == evidence.review_revision
        for path in (service.outputs.path, corpus[1].path):
            with sqlite3.connect(path, timeout=0) as other, pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE fact_reviews SET active=0")
        assert rows
        return projection.candidate.output.model_dump()
    assert service.with_reviewed(corpus[2], now=1201, callback=deliver)["value"] == "history books"


@pytest.mark.parametrize("damage", ["contract", "identity", "rows", "inode", "permissions"])
def test_store_damage_never_resets_output_authority(corpus, service, damage):
    request = prepare(corpus, service)
    with owner(): service.record(request, now=1200)
    if damage == "inode":
        replacement = service.outputs.path.with_suffix(".replacement")
        replacement.write_bytes(service.outputs.path.read_bytes()); replacement.chmod(0o600)
        replacement.replace(service.outputs.path)
    elif damage == "permissions": service.outputs.path.chmod(0o644)
    else:
        with sqlite3.connect(service.outputs.path) as db:
            db.execute({"contract":"DELETE FROM projection_contract", "identity":"DELETE FROM review_identity", "rows":"DROP TABLE fact_reviews"}[damage])
    with owner(), pytest.raises(PolicyError): service.read(lookup(corpus), now=1201)


def test_evidence_store_cannot_be_reused_as_output_store(corpus):
    with owner(), pytest.raises(PolicyError): ProjectionReviewStore(corpus[1].path, resolver=corpus[0])
    with sqlite3.connect(corpus[1].path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='projection_contract'").fetchall()


@pytest.fixture
def projection_runtime(corpus, paired_runtime, monkeypatch):
    runtime, config, path = paired_runtime
    runtime.close()
    config["projection_review_store_path"] = str(path.parent / "outputs.db")
    path.write_text(json.dumps(config))
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_PROJECTION_REVIEWS_ENABLED", "true")
    reopened = load_runtime(path, active_database=corpus[0].path)
    from topos.permissions_v2 import runtime as module
    monkeypatch.setattr(module, "_runtime", reopened)
    try: yield reopened, config, path
    finally: reopened.close()


def test_output_enrollment_is_explicit_separate_and_survives_restart(corpus, projection_runtime):
    runtime, config, path = projection_runtime
    with owner(), pytest.raises(PolicyError, match="evidence_reviews_not_enrolled"):
        runtime.projection_reviews(require_existing=False)
    with owner(): runtime.evidence_reviews(require_existing=False)
    with pytest.raises(PolicyError, match="projection_reviews_not_enrolled"):
        runtime.projection_reviews()
    assert not Path(config["projection_review_store_path"]).exists()
    with owner(cls=THIRD_PARTY), pytest.raises(PolicyError, match="owner_authority_required"):
        runtime.projection_reviews(require_existing=False)
    with owner(): first = runtime.projection_reviews(require_existing=False)
    runtime.close()
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        again = reopened.projection_reviews()
        assert again.outputs.store_id == first.outputs.store_id
        assert again.evidence_reviews.store_id != first.outputs.store_id
        marker = json.loads(Path(config["projection_review_store_path"] + ".enrollment.json").read_text())
        assert marker["version"] == "topos-owner-projection-enrollment/v2" and marker["store_id"] == first.outputs.store_id
    finally: reopened.close()


@pytest.mark.parametrize("damage", ["store", "marker", "wrong_family"])
def test_output_enrollment_loss_is_not_recreated(corpus, projection_runtime, damage):
    runtime, config, path = projection_runtime
    with owner():
        runtime.evidence_reviews(require_existing=False)
        runtime.projection_reviews(require_existing=False)
    store = Path(config["projection_review_store_path"])
    marker = store.with_name(store.name + ".enrollment.json")
    if damage == "store": store.unlink()
    elif damage == "marker": marker.unlink()
    else:
        raw = json.loads(marker.read_text()); raw["version"] = "topos-owner-evidence-enrollment/v2"; marker.write_text(json.dumps(raw))
    runtime.close()
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        with owner(), pytest.raises(PolicyError): reopened.projection_reviews(require_existing=False)
        if damage == "store": assert not store.exists()
    finally: reopened.close()


@pytest.mark.parametrize("name", ["EvidenceLookup", "ProjectionReviewState", "OwnerProjectionPreview",
    "RecordProjectionReview", "RevokeProjectionReview", "ProjectionReviewMutation"])
def test_projection_wire_schemas_are_authoritative(name):
    from topos.permissions_v2 import projection_reviews as schemas
    path = Path(__file__).resolve().parents[2] / "fixtures/permissions_v2/projection_reviews" / f"{name}.schema.json"
    assert json.loads(path.read_text()) == getattr(schemas,name).model_json_schema()


def message(corpus, operation="preview", request=None, binding=None):
    return {"id":"projection-request", "type":"permissions_v2_projection_"+operation,
        "payload":{"binding":binding or corpus[0].binding.model_dump(), "request":request or {"fact_id":corpus[2]}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [None, Principal(THIRD_PARTY,"cp_relay",acting_user="owner-1"),
    Principal(OWNER_APP,"local_http",acting_user="owner-1"), Principal(OWNER_APP,"uds"),
    Principal(OWNER_APP,"cp_relay",acting_user="wrong-owner")])
async def test_actual_dispatch_rejects_unverified_owner_before_creating_stores(corpus, projection_runtime, principal):
    from topos.core.handlers import handle_control_plane_request
    result = await handle_control_plane_request(message(corpus), principal=principal)
    assert result["code"] == 403 and "payload" not in result
    assert not Path(projection_runtime[1]["projection_review_store_path"]).exists()


@pytest.mark.asyncio
async def test_actual_dispatch_disabled_target_bound_and_owner_positive(corpus, projection_runtime, monkeypatch):
    from topos.core.handlers import handle_control_plane_request
    principal = Principal(OWNER_APP,"cp_relay",acting_user="owner-1")
    runtime = projection_runtime[0]
    with owner():
        evidence = runtime.evidence_reviews(require_existing=False)
        attest((corpus[0], evidence.reviews, corpus[2]))
    bad = await handle_control_plane_request(message(corpus,binding=corpus[0].binding.model_dump()|{"node_id":"other"}),principal=principal)
    assert bad["code"] == 403
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_PROJECTION_REVIEWS_ENABLED")
    disabled = await handle_control_plane_request(message(corpus),principal=principal)
    assert disabled["code"] == 503 and disabled["error"] == "projection_reviews_disabled"
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_PROJECTION_REVIEWS_ENABLED", "true")
    preview = await handle_control_plane_request(message(corpus),principal=principal)
    assert preview["status"] == "ok"
    candidate = OwnerProjectionPreview.parse(preview["payload"])
    request = RecordProjectionReview(review_id="transport-review", expected_candidate=candidate.candidate,
        expected_candidate_hash=candidate.candidate_hash,expected_current_review_revision=None,
        classification={"domains":["reading"],"sensitivity":"personal","subject":"self","assertion":"explicit_atomic_preference"})
    result = await handle_control_plane_request(message(corpus,"review_record",request.model_dump()),principal=principal)
    assert result["status"] == "ok" and result["payload"]["state"]["qualification"]["verdict"] == "reviewed"
    revoke = {"fact_id":corpus[2],"review_id":"transport-review","expected_review_revision":"0"*64}
    conflict = await handle_control_plane_request(message(corpus,"review_revoke",revoke),principal=principal)
    assert conflict["code"] == 409 and "history books" not in json.dumps(conflict)
    revoke["expected_review_revision"] = result["payload"]["review_revision"]
    revoked = await handle_control_plane_request(message(corpus,"review_revoke",revoke),principal=principal)
    assert revoked["payload"]["state"]["current_review"] is None


@pytest.mark.parametrize("target", ["evidence", "evidence_marker", "key", "relative", "outside"])
def test_projection_runtime_config_rejects_colliding_or_unbound_paths(corpus, projection_runtime, target):
    runtime, config, path = projection_runtime
    runtime.close()
    config["projection_review_store_path"] = {"evidence":config["evidence_review_store_path"],
        "evidence_marker":config["evidence_review_store_path"]+".enrollment.json", "key":config["node_signing_key_path"],
        "relative":"outputs.db", "outside":str(path.parent.parent / "unbound-output.db")}[target]
    path.write_text(json.dumps(config))
    with pytest.raises(PolicyError,match="review_database_binding"):
        load_runtime(path,active_database=corpus[0].path)


def test_output_enrollment_survives_device_and_inode_renumbering(corpus, projection_runtime, monkeypatch):
    from tests.permissions_v2.test_evidence import simulate_remount
    from tests.permissions_v2.test_evidence_reviews import create_request
    runtime, config, path = projection_runtime
    with owner():
        runtime.evidence_reviews(require_existing=False).record(create_request(corpus), now=1200)
        outputs = runtime.projection_reviews(require_existing=False)
        preview = outputs.preview(lookup(corpus), now=1200)
        request = RecordProjectionReview(review_id="output-review-1", expected_candidate=preview.candidate,
            expected_candidate_hash=preview.candidate_hash, expected_current_review_revision=None,
            classification={"domains":["reading"], "sensitivity":"personal", "subject":"self", "assertion":"explicit_atomic_preference"})
        recorded = outputs.record(request, now=1200)
    runtime.close()
    simulate_remount(monkeypatch, path.parents[1])
    reopened = load_runtime(path, active_database=corpus[0].path)
    try:
        with owner():
            state = reopened.projection_reviews().read(lookup(corpus), now=1201)
        assert state.current_review_revision == recorded.review_revision
        assert state.qualification.verdict == "reviewed"
    finally:
        reopened.close()
