"""Snapshot-first work canary: one owner sentence, from chat.db to a recipient scalar.

A synthetic iMessage ``chat.db`` holds one owner-authored sentence, "I work at
Northwind <hex12>." This module proves that sentence reaches a p2b-v4 recipient
as exactly ``{family: owner_stated_work, ..., value: 'Northwind <hex12>'}``, and
that no step on the way was a SQL edit to canonical or permission state.

Doors, and which of them are real
---------------------------------
Every owner action goes through the shipped control-plane handler with a
verified owner principal (the same ``handle_control_plane_request`` hand-off the
relay makes after checking its stamp), against ONE real runtime loaded from a
private node config (``load_runtime``):

- identity: ``permissions_v2_identity_command`` with a CP-signed command
  (describe, then attest the self entity) BEFORE the run;
- ingestion: ``permissions_v2_ingest_snapshot`` with CP-signed commands
  (describe, enroll, enqueue, run, status, revoke). ``run`` executes the real
  ``run_snapshot_job`` on ``Runtime.ingestion_connection`` (``sqlite3.Row``
  rows), so ``IngestProvenanceService.derive_owner_facts`` and
  ``extract_snapshot_facts`` run exactly as a paired node runs them;
- evidence review: ``permissions_v2_evidence_preview`` / ``_review_record``;
- output review: ``permissions_v2_projection_preview`` / ``_review_record`` with
  ``subject_contract=owner_attested_v1`` and ``output_family=owner_stated_work``;
- grant: CP-signed ``permissions_v2_status`` then ``permissions_v2_mutate``
  (activate), so the grant carries the node's current epoch and protection
  revision instead of a hand-seeded one;
- recipient read: ``FactProjectionRelease`` built exactly as the shipped fact
  transport builds it (``runtime.protocol`` + ``runtime.projection_reviews``),
  driven by ``tests.permissions_v2.test_fact_release.dispatch`` under the
  recipient principal; and once more in the positive test through
  ``fact_release_transport.dispatch_fact_message`` itself, with a CP relay stamp
  and a socket double, which is the door a recipient actually reaches.

Harness composition, and where it could not be reused unchanged
---------------------------------------------------------------
- ``paired_runtime`` (test_evidence_reviews) and ``projection_runtime``
  (test_projection_reviews) are imported and resolve against THIS module's
  ``corpus`` fixture, which is the lane's canonical database.
- ``enrolled_snapshot`` (tests/ingestion/test_owner_snapshot.py) is NOT used, for
  two reasons. (1) It creates ``<db>/permissions-v2`` itself and enrolls through
  the service with ``verified_owner()``, while ``paired_runtime`` must create that
  same private directory (``mkdir`` without ``exist_ok``) for the node config,
  key and review stores; the two cannot share one canonical database, and a lane
  split across two databases proves nothing. (2) Its hand-written schema is not
  a node schema: ``SQLiteCanonicalStore()`` runs every migration in its
  constructor and fails on that fixture's ``wiki_schema_migrations`` (no
  ``applied_at``), so the legacy re-ingest control could not run against it.
  ``_lane_database`` therefore builds the canonical file with the node's own
  schema writers, and the signed door replaces the service-level enrollment.
  ``native_snapshot`` is reused unchanged to build chat.db.
- ``paired_runtime`` pairs a placeholder CP key ("a" * 64) that no private key
  matches, so no signed command could verify. ``lane`` re-pairs a real CP key the
  way ``projection_runtime`` re-pairs its store path: close, rewrite the private
  config, ``load_runtime`` again. The shared signers in test_identity_dispatch
  (``command``: issuer ``beta-cp``, fixed clock 1100), test_ingest_owner_boundaries
  (``next_command``: issuer ``cp-a``, bound to its HTTP ``setup``) and
  test_fact_release (``signed_mutation``: issuer ``cp-issuer``, client
  ``owner-ui``) cannot all verify against one runtime, so the small signers here
  use this runtime's issuer and client and the real clock.
- ``issue`` (test_fact_release) is not used: it activates through the local
  ledger hook with ``expected_epoch=0``. On a node whose protection revision
  moved after the ledger was created (identity attestation, enrollment and the
  run all move it) the first sync bumps the epoch, so that CAS can never pass
  here. The signed status/mutate pair is the path a real CP takes instead.

Fixture state written directly (not part of the proof): the one ``is_self``
entity row (inserted with test_owner_identity_binding.add_entity) exists before
the protection clock is installed, as it does on a node whose entity resolver
ran before permissions were enabled. Negative controls that simulate a legacy writer
or a second self row say so where they do it.

Reason codes the lane does not return: ``run_snapshot_job`` drops the counts
``extract_snapshot_facts`` returns, so the controls that expect "no fact" read
those counts through ``spy_lane_stats`` (a wrapper on the module attribute that
calls the real function). Each control then shows the lane ran and refused for
its own reason, rather than never extracting at all.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.ingestion.test_owner_snapshot import native_snapshot
from tests.permissions_v2.test_evidence_reviews import paired_runtime  # noqa: F401 (fixture)
from tests.permissions_v2.test_fact_release import dispatch
from tests.permissions_v2.test_fact_work_family import work_policy
from tests.permissions_v2.test_identity_dispatch import node_public
from tests.permissions_v2.test_owner_identity_binding import add_entity
from tests.permissions_v2.test_projection_reviews import projection_runtime  # noqa: F401 (fixture)
from topos.core.handlers import handle_control_plane_request
from topos.features.temporal.records import FactTemporal
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.evidence import EvidenceBinding, EvidenceResolver, ReviewedClassification
from topos.permissions_v2.evidence_reviews import OwnerEvidencePreview, RecordEvidenceReview
from topos.permissions_v2.fact_contract import WORK_FAMILY, WORK_VIEW
from topos.permissions_v2.identity import ATTESTED_CONTRACT
from topos.permissions_v2.identity_protocol import (ATTESTATION_SENTENCE, AttestIdentity, DescribeIdentity,
    IdentityCommandBody, sign_identity_command, verify_identity_ack)
from topos.permissions_v2.ingest_protocol import (OWNER_ATTESTATION, IngestCommandBody, sign_ingest_command,
    verify_ingest_ack)
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.permissions_v2.protocol import (MutationBody, StatusRequestBody, sign_mutation, sign_status_request,
    verify_ack)
from topos.permissions_v2.signing import FactEnvelopeBody, request_digest, sign_envelope
from topos.principal import OWNER_APP, Principal

OWNER_ID = "owner-1"
SELF_ENTITY = "owner-entity"
SECOND_SELF = "owner-entity-2"
CP_ISSUER = "beta-cp"
FRONTEND = "permissions-beta-web"
DATASET = "dataset-work-canary"
SNAPSHOT_ID = "work-canary"
CORRESPONDENT_TEXT = "Synthetic reply from a correspondent."
BINDING = EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1", resource_id="resource-1",
                          owner_id=OWNER_ID)
OWNER_PRINCIPAL = Principal(cls=OWNER_APP, channel="cp_relay", acting_user=OWNER_ID, client_id=FRONTEND)
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
SNAPSHOT_RESULT = {"status": "ok", "messages_processed": 2, "messages_created": 2,
                   "conversations_created": 1, "historical_skipped": 0}


# --- the lane's canonical database -------------------------------------------

def _lane_database(path):
    """The canonical schema a node's own writers create, then the one self row.

    Built by the shipped schema writers rather than hand-written DDL:
    every migration, the conversation tables, the AI-chat tables (the evidence
    resolver's copy check reads both leaf tables) and ``engine_config`` through
    ``set_engine_config_value``. Spine before clock: the entity tables and the
    self row exist before ``ensure_protection_clock``, so the clock watches them
    from install and no coverage resync is needed (contrast
    test_ingest_origin_evidence.reviewed_ingest, which adds them afterwards).
    """
    from topos.core.state import set_engine_config_value
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations
    conn = sqlite3.connect(path)
    try:
        apply_all_migrations(conn)
        ensure_all_tables(conn)
        CanonicalTablesManager(conn)
        set_engine_config_value(conn, "user_id", OWNER_ID)
        add_entity(conn, SELF_ENTITY)
        conn.commit()
    finally:
        conn.close()
    path.chmod(0o600)
    ensure_protection_clock(path, owner_id=OWNER_ID)


@pytest.fixture
def corpus(tmp_path):
    """Overrides test_evidence.corpus for the imported runtime fixtures: [0] is the resolver."""
    path = tmp_path / "canonical.db"
    path.touch(mode=0o600)
    path = path.resolve(strict=True)
    _lane_database(path)
    return EvidenceResolver(path, binding=BINDING), None, None


@pytest.fixture
def lane(corpus, projection_runtime, monkeypatch):  # noqa: F811 (pytest fixture parameter)
    from topos.permissions_v2 import runtime as runtime_module
    from topos.permissions_v2.runtime import load_runtime
    runtime, config, path = projection_runtime
    runtime.close()
    cp_key = Ed25519PrivateKey.generate()
    config["trusted_cp_keys"] = {"cp-key": cp_key.public_key().public_bytes_raw().hex()}
    path.write_text(json.dumps(config))
    reopened = load_runtime(path, active_database=corpus[0].path)
    monkeypatch.setattr(runtime_module, "_runtime", reopened)
    root = reopened.protocol.canonical_database.parent / "permissions-v2" / "ingest-snapshots"
    root.mkdir(mode=0o700)
    for name, value in {"TOPOS_PERMISSIONS_V2_IDENTITY_ATTESTATIONS_ENABLED": "true",
                        "TOPOS_PERMISSIONS_V2_INGEST_SNAPSHOTS_ENABLED": "true",
                        "TOPOS_PERMISSIONS_V2_INGEST_SNAPSHOT_ROOT": str(root)}.items():
        monkeypatch.setenv(name, value)
    try:
        yield SimpleNamespace(runtime=reopened, cp_key=cp_key, root=root, serial=[0], last_run=[0.0],
                              canonical=reopened.protocol.canonical_database, employer=f"Northwind {secrets.token_hex(6)}")
    finally:
        reopened.close()


def _next(lane, prefix):
    lane.serial[0] += 1
    return f"{prefix}-{lane.serial[0]}"


def canonical(lane):
    return sqlite3.connect(lane.canonical)


# --- signed owner doors ------------------------------------------------------

def _authorization(lane):
    identity = lane.runtime.protocol.ledger.identity
    now = int(time.time())
    return identity, now


async def identity_command(lane, request):
    identity, now = _authorization(lane)
    envelope = sign_identity_command(IdentityCommandBody.parse({
        "version": "topos-owner-identity-command/v1", "kid": "cp-key", "issuer_id": CP_ISSUER,
        "audience_id": identity.node_id, "command_id": _next(lane, "identity"), "binding": identity.model_dump(),
        "owner_authorization": {"actor_id": identity.owner_id, "client_id": FRONTEND},
        "request": request.model_dump(), "issued_at": now, "expires_at": now + 100}), lane.cp_key)
    response = await handle_control_plane_request(
        {"id": "identity", "type": "permissions_v2_identity_command", "payload": {"envelope": envelope.model_dump()}},
        principal=OWNER_PRINCIPAL)
    assert response["status"] == "ok", response
    ack = verify_identity_ack(response["payload"]["ack"], trusted_keys=node_public(lane.runtime),
                              issuer_id=identity.node_id, audience_id=CP_ISSUER, request=envelope, now=int(time.time()))
    assert ack.error_code is None, ack.error_code
    return ack.result


async def attest_selves(lane, entity_ids):
    state = await identity_command(lane, DescribeIdentity())
    for entity_id in entity_ids:
        subject = next(item for item in state.subjects if item.entity_id == entity_id)
        entry = await identity_command(lane, AttestIdentity(entity_id=entity_id,
            statement_version="owner-identity-attestation/v1", statement=ATTESTATION_SENTENCE,
            expected_entity_type=subject.entity_type, expected_is_self=1, expected_contact_id=subject.contact_id,
            expected_composition_revision=subject.composition_revision, replaces_entry_id=None))
        assert entry.state == "active"
    return await identity_command(lane, DescribeIdentity())


async def ingest(lane, request, *, expect_error=None):
    identity, now = _authorization(lane)
    envelope = sign_ingest_command(IngestCommandBody.parse({
        "version": "topos-owner-ingest-command/v2", "kid": "cp-key", "issuer_id": CP_ISSUER,
        "audience_id": identity.node_id, "command_id": _next(lane, "ingest"), "binding": identity.model_dump(),
        "owner_authorization": {"actor_id": identity.owner_id, "client_id": FRONTEND},
        "request": {"source_id": "imessage", **request}, "issued_at": now, "expires_at": now + 100}), lane.cp_key)
    response = await handle_control_plane_request(
        {"id": "ingest", "type": "permissions_v2_ingest_snapshot", "payload": {"envelope": envelope.model_dump()}},
        principal=OWNER_PRINCIPAL)
    assert response["status"] == "ok", response
    ack = verify_ingest_ack(response["payload"]["ack"], trusted_keys=node_public(lane.runtime),
                            issuer_id=identity.node_id, audience_id=CP_ISSUER, request=envelope, now=int(time.time()))
    assert ack.error_code == expect_error, ack.error_code
    return ack.result


def event_instant():
    """A native Apple nanosecond date one hour ago, exactly representable in microseconds."""
    event = datetime.now(timezone.utc).replace(microsecond=123456) - timedelta(hours=1)
    delta = event - _MAC_EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return micros * 1000, (_MAC_EPOCH + timedelta(microseconds=micros)).isoformat(timespec="microseconds")


def write_snapshot(lane, messages):
    """messages: [(is_from_me, text)], ROWID order. Built by the shared native_snapshot."""
    native_date, event_at = event_instant()

    def mutate(db):
        for rowid, (from_me, text) in enumerate(messages, 1):
            db.execute("UPDATE message SET text=?, is_from_me=?, date=? WHERE ROWID=?", (text, from_me, native_date, rowid))

    snapshot = lane.root / f"{SNAPSHOT_ID}.db"
    snapshot.write_bytes(native_snapshot(count=len(messages), mutate=mutate))
    snapshot.chmod(0o400)
    return event_at


async def run_lane(lane, messages, *, with_status=True):
    """describe -> enroll -> enqueue -> run (-> status), every step a signed owner command."""
    event_at = write_snapshot(lane, messages)
    described = await ingest(lane, {"operation": "describe", "snapshot_id": SNAPSHOT_ID})
    enrollment = await ingest(lane, {"operation": "enroll", "snapshot_id": SNAPSHOT_ID, "dataset_id": DATASET,
        "snapshot_sha256": described.snapshot_sha256, "owner_attestation": OWNER_ATTESTATION})
    job = await ingest(lane, {"operation": "enqueue", "enrollment_id": enrollment.enrollment_id})
    result = await ingest(lane, {"operation": "run", "job_id": job.job_id})
    lane.last_run[0] = time.time()
    status = await ingest(lane, {"operation": "status", "job_id": job.job_id}) if with_status else None
    return SimpleNamespace(event_at=event_at, enrollment=enrollment, job=job, result=result, status=status)


def owner_messages(lane, text=None):
    return [(1, text or f"I work at {lane.employer}."), (0, CORRESPONDENT_TEXT)]


def facts(lane, *, active_only=False):
    with canonical(lane) as conn:
        rows = conn.execute("SELECT object_id,payload_json,source_refs_json,valid_to,temporal_json FROM signal_objects "
                            "WHERE object_type='fact'" + (" AND valid_to IS NULL" if active_only else "")).fetchall()
    return [SimpleNamespace(object_id=row[0], payload=json.loads(row[1]), refs=json.loads(row[2]), valid_to=row[3],
                            temporal=row[4]) for row in rows]


def counts(lane):
    with canonical(lane) as conn:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("conversation_messages", "ingest_provenance_records", "signal_objects")}


def spy_lane_stats(monkeypatch):
    """Every counts dict the lane's extractor returns, one per run.

    ``run_snapshot_job`` discards what ``derive_owner_facts`` returns
    (topos/ingestion/owner_snapshot.py:248), so a control that only counted fact
    rows could not tell "refused for its reason" from "never extracted at all".
    ``derive_owner_facts`` imports ``extract_snapshot_facts`` at call time, so
    the module attribute is the one the lane runs.
    """
    from topos.permissions_v2 import ingest_snapshot_facts
    real = ingest_snapshot_facts.extract_snapshot_facts
    seen = []

    def spy(conn, service, context):
        stats = real(conn, service, context)
        seen.append(dict(stats))
        return stats

    monkeypatch.setattr(ingest_snapshot_facts, "extract_snapshot_facts", spy)
    return seen


# --- owner reviews through the real handlers ---------------------------------

async def owner_message(kind, operation, request, **extra):
    response = await handle_control_plane_request(
        {"id": f"{kind}-{operation}", "type": f"permissions_v2_{kind}_{operation}",
         "payload": {"binding": BINDING.model_dump(), "request": request, **extra}}, principal=OWNER_PRINCIPAL)
    assert response["status"] == "ok", response
    return response["payload"]


async def review_evidence(fact_id):
    preview = OwnerEvidencePreview.parse(await owner_message("evidence", "preview", {"fact_id": fact_id}))
    assert preview.status == "complete", preview.reason_code
    snapshot = preview.snapshot
    classifications = [ReviewedClassification(evidence=version, domains=["work"], sensitivity="personal",
        subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
        independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves]
    recorded = await owner_message("evidence", "review_record", RecordEvidenceReview(review_id="canary-evidence-review",
        expected_snapshot=snapshot, expected_current_review_revision=None,
        classifications=classifications).model_dump())
    return snapshot, recorded


async def review_output(fact_id):
    family = {"subject_contract": ATTESTED_CONTRACT, "output_family": WORK_FAMILY}
    preview = await owner_message("projection", "preview", {"fact_id": fact_id}, **family)
    assert preview["candidate"] is not None, preview["candidate_reason_code"]
    recorded = await owner_message("projection", "review_record", {
        "review_id": "canary-output-review", "expected_candidate": preview["candidate"],
        "expected_candidate_hash": preview["candidate_hash"],
        "expected_current_review_revision": preview["current_review_revision"],
        "classification": {"domains": ["work"], "sensitivity": "personal", "subject": "self",
                           "assertion": "explicit_atomic_work_engagement"}}, **family)
    return preview, recorded


def qualify(lane, fact_id):
    service = lane.runtime.evidence_reviews(require_existing=True)
    return service.resolver.qualify(fact_id, reviews=service.reviews, contract=ATTESTED_CONTRACT)


# --- the signed grant and the recipient read ---------------------------------

def canary_policy(lane, now):
    raw = work_policy((lane.runtime.evidence_reviews(require_existing=True).resolver, None, None))
    raw["policy_version_id"] = "work-canary-policy-1"
    raw["validity"] = {"starts_at": now - 3600, "expires_at": now + 3600}
    raw["source_universe"]["source_ids"] = ["imessage"]
    for rule in raw["rules"]:
        rule["evidence_use"]["sources"]["values"] = ["imessage"]
        rule["evidence_use"]["predicate"]["values"] = ["work"]
        rule["release"]["predicate"]["values"] = ["work"]
    return raw


async def protocol_call(lane, kind, envelope):
    response = await handle_control_plane_request(
        {"id": kind, "type": f"permissions_v2_{kind}", "payload": {"envelope": envelope.model_dump()}},
        principal=OWNER_PRINCIPAL)
    assert response["status"] == "ok", response
    identity = lane.runtime.protocol.ledger.identity
    return verify_ack(response["payload"]["ack"], trusted_keys=node_public(lane.runtime), issuer_id=identity.node_id,
                      audience_id=CP_ISSUER, request=envelope, now=int(time.time()))


async def signed_grant(lane):
    """CP reads the node's epoch and floor, then activates a p2b-v4 grant against them."""
    now = int(time.time())
    raw = canary_policy(lane, now)
    identity = lane.runtime.protocol.ledger.identity
    status = await protocol_call(lane, "status", sign_status_request(StatusRequestBody.parse({
        "version": "topos-policy-status-request/v2", "kid": "cp-key", "issuer_id": CP_ISSUER,
        "audience_id": identity.node_id, "request_id": _next(lane, "status"), "binding": raw["binding"],
        "command_id": None, "command_hash": None, "issued_at": now, "expires_at": now + 100}), lane.cp_key))
    epoch = status.state.node_epoch
    authority = {**raw["binding"], "grant_generation": 1, "assignment_generation": 1,
                 "policy_version_id": raw["policy_version_id"], "policy_hash": digest(raw),
                 "capability_version": raw["versions"]["capability"],
                 "protection_revision": status.state.protection_revision, "node_epoch": epoch + 1}
    ack = await protocol_call(lane, "mutate", sign_mutation(MutationBody.parse({
        "version": "topos-policy-mutation/v2", "kid": "cp-key", "issuer_id": CP_ISSUER,
        "audience_id": identity.node_id, "command_id": _next(lane, "activate"), "operation": "activate",
        "expected_epoch": epoch, "authority": authority, "policy": raw,
        "owner_authorization": {"actor_id": identity.owner_id, "client_id": FRONTEND},
        "issued_at": now, "expires_at": now + 100}), lane.cp_key))
    assert (ack.outcome, ack.reason_code) == ("applied", "ok")
    assert ack.receipt.authority.capability_version == "permissions-beta/p2b-v4"
    return ack.receipt.authority


def grantee_envelope(lane, authority, fact_id, *, request_id, issued):
    """What the CP signs for a recipient, bound to the authority the node acknowledged."""
    payload = {"query": "fact:" + fact_id}
    envelope = sign_envelope(FactEnvelopeBody.parse({**authority.model_dump(), "version": "topos-grantee-envelope/v2",
        "kid": "cp-key", "request_id": request_id, "request_type": "permissions.v2.fact.read",
        "request_hash": request_digest("permissions.v2.fact.read", payload),
        "issued_at": issued, "expires_at": issued + 100}), lane.cp_key)
    return envelope, payload


def recipient_read(lane, authority, fact_id, *, request_id):
    """The shipped transport's adapter, driven by test_fact_release.dispatch. Returns (outputs, error code)."""
    from topos.permissions_v2.fact_release import FactProjectionRelease
    clock = lambda: int(time.time()) + 1  # noqa: E731 - a whole second past any fact written before this call
    release = FactProjectionRelease(protocol=lane.runtime.protocol,
                                    projections=lane.runtime.projection_reviews(require_existing=True), clock=clock)
    envelope, payload = grantee_envelope(lane, authority, fact_id, request_id=request_id, issued=clock())
    try:
        return dispatch((release,), envelope, payload, request_id=request_id), None
    except PolicyError as exc:
        return [], exc.code


async def socket_read(lane, authority, fact_id, *, request_id, monkeypatch):
    """The recipient door itself: a CP-stamped frame into the fact WebSocket dispatcher, real clock."""
    import base64
    from tests.permissions_v2.test_release_transport import Socket
    from topos.permissions_v2 import fact_release_transport
    from topos.relay_stamp import canonical_signing_payload
    monkeypatch.setenv(fact_release_transport.FLAG, "true")
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(lane.cp_key.public_key().public_bytes_raw()).decode())
    while int(time.time()) <= lane.last_run[0]:  # request_as_of must not precede the fact's valid_from
        time.sleep(0.05)
    now = int(time.time())
    envelope, payload = grantee_envelope(lane, authority, fact_id, request_id=request_id, issued=now)
    message = {"id": request_id, "type": fact_release_transport.MESSAGE_TYPE,
               "payload": {"envelope": envelope.model_dump(), "intent": payload}}
    stamp = {"v": 1, "cls": "third_party", "client_id": "client-1", "acting_user": "actor-1", "iat": now, "exp": now + 100}
    stamp["sig"] = base64.b64encode(lane.cp_key.sign(canonical_signing_payload(
        stamp, msg_id=request_id, msg_type=message["type"]))).decode()
    message["principal_stamp"] = stamp
    socket = Socket()
    await fact_release_transport.dispatch_fact_message(socket, message)
    return socket.sent


async def prove(lane):
    """The whole positive lane. Returns what the controls need to perturb it."""
    await attest_selves(lane, [SELF_ENTITY])
    run = await run_lane(lane, owner_messages(lane))
    assert run.result.model_dump() == SNAPSHOT_RESULT
    [fact] = facts(lane, active_only=True)
    await review_evidence(fact.object_id)
    assert qualify(lane, fact.object_id).verdict == "qualified"
    await review_output(fact.object_id)
    authority = await signed_grant(lane)
    outputs, error = recipient_read(lane, authority, fact.object_id, request_id="canary-read-1")
    assert error is None
    return SimpleNamespace(run=run, fact=fact, authority=authority, outputs=outputs)


def expected_scalar(lane):
    return {"family": "owner_stated_work", "operation": "read", "view_id": "owner_stated_work.scalar.v1",
            "subject": "self", "predicate": "works_at", "value": lane.employer}


# --- positive ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_owner_sentence_in_chat_db_reaches_the_recipient_as_the_exact_work_scalar(lane, monkeypatch):
    state = await attest_selves(lane, [SELF_ENTITY])
    assert state.permitted_count == 2  # the literal subject plus the one attested entity

    stats = spy_lane_stats(monkeypatch)
    run = await run_lane(lane, owner_messages(lane))
    assert run.result.model_dump() == SNAPSHOT_RESULT
    assert run.status.status == "done" and run.status.result.model_dump() == SNAPSHOT_RESULT
    assert stats == [{"rows_linked": 2, "facts_written": 1}]

    with canonical(lane) as conn:
        row = conn.execute("SELECT event_at,is_from_self,actor_role,owner_user_id FROM conversation_messages "
                           "WHERE message_id='imessage:1'").fetchone()
    assert row == (run.event_at, 1, "authored", OWNER_ID)

    [fact] = facts(lane, active_only=True)
    assert len(facts(lane)) == 1
    assert {key: fact.payload.get(key) for key in ("subject_entity_id", "predicate", "object_value", "disclosure",
                                                   "asserted_by")} == {
        "subject_entity_id": SELF_ENTITY, "predicate": "works_at", "object_value": lane.employer,
        "disclosure": "scoped", "asserted_by": "owner"}
    assert fact.refs == [{"table": "conversation_messages", "record_id": "imessage:1", "source_id": "imessage",
                          "dataset_id": DATASET}]
    evidence = FactTemporal.from_json(fact.temporal).evidence
    assert (evidence.provenance, evidence.text) == ("native_source_clock", run.event_at)

    snapshot, recorded = await review_evidence(fact.object_id)
    assert [(item.identity.table, item.identity.record_id) for item in snapshot.leaves] == [
        ("conversation_messages", "imessage:1")]
    assert [item.identity.record_id for item in snapshot.artifacts] == [fact.object_id]
    assert recorded["state"]["qualification"]["verdict"] == "qualified"
    qualification = qualify(lane, fact.object_id)
    assert (qualification.verdict, qualification.evidence.subject_contract) == ("qualified", ATTESTED_CONTRACT)

    preview, output_review = await review_output(fact.object_id)
    assert preview["candidate"]["output"] == expected_scalar(lane)
    assert output_review["state"]["qualification"]["verdict"] == "reviewed"

    authority = await signed_grant(lane)
    outputs, error = recipient_read(lane, authority, fact.object_id, request_id="canary-read-1")
    assert error is None
    [(result, output)] = outputs
    assert output == expected_scalar(lane)
    assert output["view_id"] == WORK_VIEW
    assert result["authority"]["capability_version"] == "permissions-beta/p2b-v4"
    for private in (SELF_ENTITY, "I work at", CORRESPONDENT_TEXT):
        assert private not in json.dumps(output) and private not in json.dumps(result)

    [frame] = await socket_read(lane, authority, fact.object_id, request_id="canary-read-socket", monkeypatch=monkeypatch)
    assert frame["status"] == "ok" and frame["payload"]["output"] == expected_scalar(lane)


# --- negative controls -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_correspondent_saying_the_same_thing_about_another_employer_mints_nothing(lane, monkeypatch):
    """is_from_me=0 with the identical pattern: the authored gate, not the pattern, decides."""
    other = "Harbor Freight Synthetic"
    await attest_selves(lane, [SELF_ENTITY])
    stats = spy_lane_stats(monkeypatch)
    run = await run_lane(lane, [(1, f"I work at {lane.employer}."), (0, f"I work at {other}.")])
    assert run.result.model_dump() == SNAPSHOT_RESULT
    # Both rows reached the extractor and neither value was refused as a label:
    # the correspondent row produced no candidate at all.
    assert stats == [{"rows_linked": 2, "facts_written": 1}]
    assert not [fact for fact in facts(lane) if other.lower() in json.dumps(fact.payload).lower()]
    # The same run did extract the owner's own sentence, so the lane was live.
    assert [fact.payload["object_value"] for fact in facts(lane)] == [lane.employer]


# Controls 3 and 4 share one reason: extract_snapshot_facts counts "no unique
# attested self" as owner_subject_unattested whether there are zero or two
# (topos/permissions_v2/ingest_snapshot_facts.py:101-104). Control 4 tells the
# two apart by confirming both attestations are active before the run.
UNATTESTED_STATS = [{"rows_linked": 2, "owner_subject_unattested": 2}]


@pytest.mark.asyncio
async def test_without_an_attested_self_the_run_succeeds_and_writes_no_fact(lane, monkeypatch):
    stats = spy_lane_stats(monkeypatch)
    run = await run_lane(lane, owner_messages(lane))
    assert run.result.model_dump() == SNAPSHOT_RESULT and run.status.status == "done"
    assert stats == UNATTESTED_STATS  # the lane ran over both rows and refused for this reason
    assert counts(lane) == {"conversation_messages": 2, "ingest_provenance_records": 2, "signal_objects": 0}
    with canonical(lane) as conn:
        # Nor did the extractor fall back to creating or choosing a self entity.
        assert conn.execute("SELECT entity_id FROM entities WHERE is_self=1").fetchall() == [(SELF_ENTITY,)]


@pytest.mark.asyncio
async def test_two_attested_self_entities_are_ambiguous_and_write_no_fact(lane, monkeypatch):
    # Simulated node state, not an adversary: an entity resolver that created a
    # second self row. Inserted like test_owner_identity_binding.add_entity does.
    with canonical(lane) as conn:
        add_entity(conn, SECOND_SELF)
    state = await attest_selves(lane, [SELF_ENTITY, SECOND_SELF])
    # Both attestations are live, so zero facts is the ambiguity rule, not a stale entry.
    assert {item.entity_id: item.entry_state for item in state.subjects} == {SELF_ENTITY: "active", SECOND_SELF: "active"}
    assert state.permitted_count == 3
    stats = spy_lane_stats(monkeypatch)
    run = await run_lane(lane, owner_messages(lane))
    assert run.result.model_dump() == SNAPSHOT_RESULT and run.status.status == "done"
    assert stats == UNATTESTED_STATS
    assert counts(lane) == {"conversation_messages": 2, "ingest_provenance_records": 2, "signal_objects": 0}


@pytest.mark.asyncio
async def test_a_value_that_is_not_one_atomic_label_is_never_written(lane, monkeypatch):
    from topos.permissions_v2.fact_contract import atomic_label_syntax
    refused = lane.employer.replace(" ", "_")
    with pytest.raises(ValueError):
        atomic_label_syntax(refused)
    await attest_selves(lane, [SELF_ENTITY])
    stats = spy_lane_stats(monkeypatch)
    run = await run_lane(lane, owner_messages(lane, text=f"I work at {refused}."))
    assert run.result.model_dump() == SNAPSHOT_RESULT and run.status.status == "done"
    # The extractor produced the candidate and the label gate refused it.
    assert stats == [{"rows_linked": 2, "value_refused": 1, "facts_written": 0}]
    assert facts(lane) == []
    # Refused by the lane's label gate, not missed by the pattern: the shared
    # extractor does emit this value for the stored, owner-authored row.
    from topos.features.facts.extract import extract_message_facts
    with canonical(lane) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM conversation_messages WHERE message_id='imessage:1'").fetchone())
    assert [spec["object_value"] for spec in extract_message_facts(row, table="conversation_messages")] == [refused]


LANE_MODULES = ("topos.ingestion.owner_snapshot", "topos.permissions_v2.ingest_provenance",
                "topos.permissions_v2.ingest_snapshot_facts")


@pytest.mark.asyncio
async def test_the_llm_fact_pass_never_runs_in_the_lane_even_when_enabled(lane, monkeypatch):
    """Traps at two depths, so the import style of a regressed lane cannot slip past.

    Replacing ``facts_llm_enabled`` / ``extract_owner_facts_llm`` on the module
    only catches a lane that looks them up at call time. A lane that bound them
    at import (``from ..features.facts.llm_extract import ...``) keeps the real
    functions. Those still look up ``_resolved_extraction_request`` and the two
    extractor factories as ``llm_extract`` globals when they run, so the traps
    there fire whatever name the lane holds. The last check also refuses any
    lane module that holds an ``llm_extract`` function or the module itself.
    """
    import importlib
    from topos.config.settings import settings
    from topos.features.facts import llm_extract
    monkeypatch.setenv("TOPOS_FACTS_LLM", "1")
    monkeypatch.setattr(settings, "topos_facts_llm", "1", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # The trap is armed: the shared legacy gate would say ON in this environment.
    assert llm_extract.facts_llm_enabled() is True
    calls = []

    def forbidden(name):
        def call(*args, **kwargs):
            calls.append(name)
            pytest.fail(f"{name} ran inside the owner snapshot lane")
        return call

    for name in ("facts_llm_enabled", "extract_owner_facts_llm", "_resolved_extraction_request",
                 "_make_ollama_extractor", "_make_hosted_extractor"):
        assert callable(getattr(llm_extract, name)), name  # a renamed global would disarm this trap
        monkeypatch.setattr(llm_extract, name, forbidden(name))
    await attest_selves(lane, [SELF_ENTITY])
    run = await run_lane(lane, owner_messages(lane))
    assert run.result.model_dump() == SNAPSHOT_RESULT and run.status.status == "done"
    assert calls == []
    assert [fact.payload["object_value"] for fact in facts(lane)] == [lane.employer]
    for module_name in LANE_MODULES:
        module = importlib.import_module(module_name)
        bound = [attr for attr, value in vars(module).items()
                 if value is llm_extract or getattr(value, "__module__", None) == llm_extract.__name__]
        assert bound == [], f"{module_name} binds the LLM pass: {bound}"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["after_the_fact_write", "at_job_finish"])
async def test_a_failure_after_the_fact_is_written_rolls_back_rows_links_facts_and_fails_the_job(
        lane, monkeypatch, failure_point):
    """The failure comes after the real extractor has written the fact inside the batch.

    after_the_fact_write: the real ``extract_rules_facts`` runs, then raises.
    at_job_finish: extraction completes, then ``IngestProvenanceService.finish``
    (the batch's last step) raises. What the batch held is recorded inside and
    asserted afterwards, never asserted inside: the lane turns any Exception,
    an AssertionError included, into ``snapshot_job_unavailable``, so an assert
    there would pass for the wrong reason.
    """
    from topos.permissions_v2 import ingest_snapshot_facts
    from topos.permissions_v2.ingest_provenance import IngestProvenanceService
    inside = []

    def observe_then_fail(conn):
        inside.append({
            **{table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
               for table in ("conversation_messages", "ingest_provenance_records")},
            "facts": conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='fact'").fetchone()[0],
            "in_transaction": conn.in_transaction})
        raise RuntimeError("SYNTHETIC_LANE_FAILURE")

    if failure_point == "after_the_fact_write":
        real_extract = ingest_snapshot_facts.extract_rules_facts

        def failing_extract(conn, rows, **kwargs):
            real_extract(conn, rows, **kwargs)
            observe_then_fail(conn)

        monkeypatch.setattr(ingest_snapshot_facts, "extract_rules_facts", failing_extract)
    else:
        def failing_finish(self, conn, context, result):
            observe_then_fail(conn)

        monkeypatch.setattr(IngestProvenanceService, "finish", failing_finish)
    await attest_selves(lane, [SELF_ENTITY])
    run = await run_lane(lane, owner_messages(lane), with_status=False)
    # The batch really held rows, links and the fact, uncommitted, when it failed.
    assert inside == [{"conversation_messages": 2, "ingest_provenance_records": 2, "facts": 1,
                       "in_transaction": True}]
    assert run.result.model_dump() == {"status": "error", "reason_code": "snapshot_job_unavailable"}
    assert "SYNTHETIC" not in json.dumps(run.result.model_dump())
    assert counts(lane) == {"conversation_messages": 0, "ingest_provenance_records": 0, "signal_objects": 0}
    with canonical(lane) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    status = await ingest(lane, {"operation": "status", "job_id": run.job.job_id})
    assert status.status == "failed"
    assert status.result.model_dump() == {"status": "error", "reason_code": "snapshot_job_unavailable"}


@pytest.mark.asyncio
async def test_revoking_the_enrollment_after_release_withholds_the_fact(lane):
    proved = await prove(lane)
    assert [output for _, output in proved.outputs] == [expected_scalar(lane)]
    revoked = await ingest(lane, {"operation": "revoke", "enrollment_id": proved.run.enrollment.enrollment_id})
    assert revoked.state == "revoked"
    result = qualify(lane, proved.fact.object_id)
    assert (result.verdict, result.reason_code) == ("withheld", "native_owner_provenance_unavailable")
    outputs, error = recipient_read(lane, proved.authority, proved.fact.object_id, request_id="canary-read-2")
    assert outputs == [] and error == "native_owner_provenance_unavailable"


@pytest.mark.asyncio
async def test_a_legacy_reingest_of_the_same_message_cannot_rewrite_the_proved_row(lane):
    """Simulated legacy writer: the shared canonical upsert, same dataset and message id."""
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
    proved = await prove(lane)
    with canonical(lane) as conn:
        before = conn.execute("SELECT content,metadata_json,event_at,is_from_self FROM conversation_messages "
                              "WHERE message_id='imessage:1'").fetchone()
        SQLiteCanonicalStore(conn).upsert("conversation_messages", {
            "message_id": "imessage:1", "conversation_id": "7", "dataset_id": DATASET, "source_id": "imessage",
            "sender_type": "human", "sender_id": "self", "is_from_self": 1, "event_at": proved.run.event_at,
            "content": "I work at Harbor Freight Synthetic."}, sync_batch_id="legacy-resync")
        after = conn.execute("SELECT content,metadata_json,event_at,is_from_self FROM conversation_messages "
                             "WHERE message_id='imessage:1'").fetchone()
        assert conn.execute("SELECT sync_batch_id FROM conversation_messages WHERE message_id='imessage:1'"
                            ).fetchone() == ("legacy-resync",)  # the re-ingest did land; only the body was refused
    assert after == before
    assert qualify(lane, proved.fact.object_id).verdict == "qualified"
    outputs, error = recipient_read(lane, proved.authority, proved.fact.object_id, request_id="canary-read-2")
    assert error is None and [output for _, output in outputs] == [expected_scalar(lane)]


@pytest.mark.asyncio
async def test_a_legacy_extraction_over_the_lane_rows_withholds_rather_than_qualifying(lane):
    """Known availability limit, pinned so it cannot silently become a leak.

    ``extract_facts_from_batch`` is the legacy shared producer. Run over the
    lane's own rows it re-asserts the same (subject, works_at, value), so
    FactStore refreshes the lane fact and appends its own reference, which has
    no ``dataset_id`` (``extract._source_ref``). The fact now has one proven and
    one unprovable leaf. It must withhold until something removes that ref;
    what it must never do is qualify on the proven leaf alone. Today nothing
    removes it, so a node that also runs the legacy extractor over snapshot rows
    loses this release: that is the availability cost, recorded here.
    """
    from topos.features.facts.extract import extract_facts_from_batch
    proved = await prove(lane)
    with canonical(lane) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM conversation_messages ORDER BY message_id")]
        extract_facts_from_batch(conn, rows)
        conn.commit()
    [fact] = facts(lane, active_only=True)
    assert fact.object_id == proved.fact.object_id
    assert fact.refs == proved.fact.refs + [{"table": "conversation_messages", "record_id": "imessage:1",
                                             "source_id": "imessage"}]
    result = qualify(lane, proved.fact.object_id)
    assert (result.verdict, result.reason_code) == ("withheld", "lineage_identity_incomplete")
    outputs, error = recipient_read(lane, proved.authority, proved.fact.object_id, request_id="canary-read-2")
    assert outputs == [] and error == "lineage_identity_incomplete"
