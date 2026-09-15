"""New owner-attested native records never outlive their current provenance."""
import json
import sqlite3

import pytest
import pytest_asyncio

from tests.ingestion.test_owner_snapshot import enrolled_snapshot, verified_owner
from topos.ingestion.owner_snapshot import run_snapshot_job
from topos.permissions_v2.evidence import EvidenceResolver, EvidenceReviewStore, ReviewedClassification


@pytest_asyncio.fixture
async def reviewed_ingest(enrolled_snapshot, tmp_path):
    service, job_id, factory, path, snapshot_file = enrolled_snapshot
    result = await run_snapshot_job(service, factory, job_id)
    assert result["status"] == "ok"
    from topos.features.facts.store import FactStore
    from topos.storage.db.migrations.signal_objects import apply_signal_objects_up
    with sqlite3.connect(path) as conn:
        apply_signal_objects_up(conn)
        conn.execute("CREATE TABLE entities(entity_id TEXT PRIMARY KEY,is_self INTEGER)")
        conn.execute("INSERT INTO entities VALUES ('owner-entity',1)")
        conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT,content TEXT)")
        conn.commit()
        fact = FactStore(conn).assert_fact(subject_entity_id="self", predicate="prefers", object_value="history books",
            disclosure="scoped", asserted_by="owner",
            source_refs=[{"table": "conversation_messages", "record_id": "imessage:1", "source_id": "imessage", "dataset_id": "dataset-synthetic"}])
    resolver = EvidenceResolver(path, binding=service.binding)
    with verified_owner():
        reviews = EvidenceReviewStore(tmp_path / "evidence-reviews.db", resolver=resolver)
        snapshot = resolver.inspect_for_review(fact["object_id"])
        classifications = [ReviewedClassification(evidence=version, domains=["reading"], sensitivity="personal",
            subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement", independent_copies="none_known")
            for version in snapshot.artifacts + snapshot.leaves]
        reviews.record_review(resolver=resolver, review_id="synthetic-review", expected_snapshot=snapshot,
                              classifications=classifications, reviewed_at=1789430400)
    assert resolver.qualify(fact["object_id"], reviews=reviews).verdict == "qualified"
    return resolver, reviews, fact["object_id"], service, job_id, snapshot_file


@pytest.mark.asyncio
async def test_owner_revocation_dominates_existing_positive_review(reviewed_ingest):
    resolver, reviews, fact, service, job, _ = reviewed_ingest
    with verified_owner(), sqlite3.connect(resolver.path) as conn:
        enrollment_id = conn.execute("SELECT enrollment_id FROM ingest_provenance_jobs WHERE job_id=?", (job,)).fetchone()[0]
        service.revoke(conn, enrollment_id=enrollment_id)
    result = resolver.qualify(fact, reviews=reviews)
    assert (result.verdict, result.reason_code) == ("withheld", "native_owner_provenance_unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("statement", [
    "DELETE FROM ingest_provenance_records WHERE message_id='imessage:1'",
    "DROP TABLE ingest_provenance_records",
    "UPDATE ingest_provenance_jobs SET status='queued'",
])
async def test_missing_or_stale_origin_state_never_uses_legacy_owner_fallback(reviewed_ingest, statement):
    resolver, reviews, fact, _, _, _ = reviewed_ingest
    with sqlite3.connect(resolver.path) as conn:
        conn.execute(statement)
    result = resolver.qualify(fact, reviews=reviews)
    assert (result.verdict, result.reason_code) == ("withheld", "native_owner_provenance_unavailable")


@pytest.mark.asyncio
async def test_owner_change_and_restore_withholds_but_idempotent_startup_does_not(reviewed_ingest):
    resolver, reviews, fact, _, _, _ = reviewed_ingest
    with sqlite3.connect(resolver.path) as conn:
        conn.execute("INSERT OR REPLACE INTO engine_config VALUES('user_id','owner-synthetic')")
    assert resolver.qualify(fact, reviews=reviews).verdict == "qualified"
    with sqlite3.connect(resolver.path) as conn:
        conn.execute("UPDATE engine_config SET value='other-owner' WHERE key='user_id'")
        conn.execute("UPDATE engine_config SET value='owner-synthetic' WHERE key='user_id'")
    assert resolver.qualify(fact, reviews=reviews).reason_code == "native_owner_provenance_unavailable"


@pytest.mark.asyncio
async def test_origin_snapshot_or_private_marker_disappearance_withholds(reviewed_ingest):
    resolver, reviews, fact, service, _, snapshot_file = reviewed_ingest
    before = snapshot_file.read_bytes()
    snapshot_file.unlink()
    assert resolver.qualify(fact, reviews=reviews).reason_code == "native_owner_provenance_unavailable"
    snapshot_file.write_bytes(before)
    snapshot_file.chmod(0o400)
    service.marker.unlink()
    assert resolver.qualify(fact, reviews=reviews).reason_code == "native_owner_provenance_unavailable"


@pytest.mark.asyncio
async def test_unknown_origin_version_is_withheld_even_with_otherwise_valid_owner_fields(reviewed_ingest):
    resolver, reviews, fact, _, _, _ = reviewed_ingest
    with sqlite3.connect(resolver.path) as conn:
        raw = conn.execute("SELECT metadata_json FROM conversation_messages WHERE message_id='imessage:1'").fetchone()[0]
        metadata = json.loads(raw)
        metadata["topos_owner_ingest"]["version"] = "future"
        conn.execute("UPDATE conversation_messages SET metadata_json=? WHERE message_id='imessage:1'", (json.dumps(metadata),))
    assert resolver.qualify(fact, reviews=reviews).reason_code == "native_owner_provenance_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("statement", [
    "UPDATE conversation_messages SET content='changed text' WHERE message_id='imessage:1'",
    "UPDATE conversation_messages SET actor_role='observed' WHERE message_id='imessage:1'",
])
async def test_changed_native_row_cannot_be_recertified_by_only_a_new_review(reviewed_ingest, statement):
    from topos.permissions_v2.canonical import PolicyError
    resolver, reviews, fact, _, _, _ = reviewed_ingest
    with sqlite3.connect(resolver.path) as conn:
        conn.execute(statement)
    assert resolver.qualify(fact, reviews=reviews).reason_code == "native_owner_provenance_unavailable"
    with verified_owner(), pytest.raises(PolicyError, match="native_owner_provenance_unavailable"):
        resolver.inspect_for_review(fact)


@pytest.mark.asyncio
async def test_removing_origin_metadata_cannot_create_new_legacy_owner_proof(reviewed_ingest):
    from topos.permissions_v2.canonical import PolicyError
    resolver, reviews, fact, service, job, _ = reviewed_ingest
    with verified_owner(), sqlite3.connect(resolver.path) as conn:
        enrollment_id = conn.execute("SELECT enrollment_id FROM ingest_provenance_jobs WHERE job_id=?", (job,)).fetchone()[0]
        service.revoke(conn, enrollment_id=enrollment_id)
    with sqlite3.connect(resolver.path) as conn:
        conn.execute("UPDATE conversation_messages SET metadata_json=NULL WHERE message_id='imessage:1'")
    # A newly saved owner classification must not replace the revoked native
    # account attestation. On the pre-fix code this entire sequence qualified.
    with verified_owner(), pytest.raises(PolicyError, match="native_owner_provenance_unavailable"):
        snapshot = resolver.inspect_for_review(fact)
        classifications = [ReviewedClassification(evidence=version, domains=["reading"], sensitivity="personal",
            subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement", independent_copies="none_known")
            for version in snapshot.artifacts + snapshot.leaves]
        reviews.record_review(resolver=resolver, review_id="replacement-review", expected_snapshot=snapshot,
                              classifications=classifications, reviewed_at=1789430401)
        assert resolver.qualify(fact, reviews=reviews).verdict == "qualified"


@pytest.mark.asyncio
@pytest.mark.parametrize("statement", ["DROP TABLE ingest_provenance_records", "DELETE FROM ingest_provenance_records"])
async def test_lost_or_corrupt_origin_store_cannot_explain_away_stripped_hint(reviewed_ingest, statement):
    resolver, reviews, fact, service, _, _ = reviewed_ingest
    with sqlite3.connect(resolver.path) as conn:
        conn.execute("UPDATE conversation_messages SET metadata_json=NULL WHERE message_id='imessage:1'")
        conn.execute(statement)
    # For deletion of all links, also lose the private marker: an intact store
    # with no link intentionally retains historical compatibility.
    if statement.startswith("DELETE"):
        service.marker.unlink()
    assert resolver.qualify(fact, reviews=reviews).reason_code == "native_owner_provenance_unavailable"


@pytest.mark.asyncio
async def test_attested_dataset_owner_does_not_promote_correspondent_authorship(reviewed_ingest):
    resolver, reviews, fact, _, _, _ = reviewed_ingest
    with sqlite3.connect(resolver.path) as conn:
        refs = [{"table": "conversation_messages", "record_id": "imessage:2", "source_id": "imessage", "dataset_id": "dataset-synthetic"}]
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (json.dumps(refs), fact))
    with verified_owner():
        snapshot = resolver.inspect_for_review(fact)
        classifications = [ReviewedClassification(evidence=version, domains=["reading"], sensitivity="personal",
            subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement", independent_copies="none_known")
            for version in snapshot.artifacts + snapshot.leaves]
        reviews.record_review(resolver=resolver, review_id="correspondent-review", expected_snapshot=snapshot,
                              classifications=classifications, reviewed_at=1789430401)
    result = resolver.qualify(fact, reviews=reviews)
    assert (result.verdict, result.reason_code) == ("withheld", "not_owner_authored")
