"""Real SQLite authority, rollback, restart and immutable source regressions."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import os
import shutil
import sqlite3

import pytest

from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceBinding
from topos.permissions_v2.ingest_provenance import IngestProvenanceService, OWNER_ATTESTATION
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
from topos.storage.db.migrations.owner_only_records_v1 import apply_owner_only_records_v1_up
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up
from topos.storage.db.migrations.wiki_lifecycle_v1 import apply_wiki_lifecycle_v1_up


@contextmanager
def owner(*, actor="owner-1", cls=OWNER_APP, channel="uds"):
    token = set_principal(Principal(cls=cls, channel=channel, acting_user=actor))
    try:
        yield
    finally:
        reset_principal(token)


@pytest.fixture
def ingest_fixture(tmp_path):
    canonical = tmp_path / "canonical.db"
    binding = EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1", resource_id="resource-1", owner_id="owner-1")
    conn = sqlite3.connect(canonical)
    apply_signal_objects_up(conn)
    apply_owner_only_records_v1_up(conn)
    apply_entity_blackhole_v1_up(conn)
    apply_wiki_lifecycle_v1_up(conn)
    conn.execute("CREATE TABLE engine_config(key TEXT PRIMARY KEY,value TEXT)")
    conn.execute("INSERT INTO engine_config VALUES('user_id','owner-1')")
    conn.execute("CREATE TABLE source_settings(source_id TEXT PRIMARY KEY,enabled INTEGER)")
    conn.execute("INSERT INTO source_settings VALUES('imessage',1)")
    conn.execute("CREATE TABLE user_ingestion_sources(dataset_id TEXT,source_id TEXT,enabled INTEGER,posture TEXT)")
    conn.execute("INSERT INTO user_ingestion_sources VALUES('native-dataset','imessage',1,NULL)")
    conn.execute("CREATE TABLE conversation_messages(message_id TEXT PRIMARY KEY,conversation_id TEXT,dataset_id TEXT,source_id TEXT,source_record_id TEXT,owner_user_id TEXT,sender_id TEXT,sender_type TEXT,is_from_self INTEGER,event_at TEXT,content TEXT,metadata_json TEXT,actor_role TEXT)")
    conn.commit()
    ensure_protection_clock(canonical, owner_id="owner-1")
    durable = tmp_path / "permissions-v2"
    durable.mkdir(mode=0o700)
    snapshots = durable / "ingest-snapshots"
    snapshots.mkdir(mode=0o700)
    snapshot = snapshots / "canary.db"
    with sqlite3.connect(snapshot) as source:
        source.execute("CREATE TABLE message(value TEXT)")
        source.execute("INSERT INTO message VALUES('synthetic only')")
    snapshot.chmod(0o400)
    service = IngestProvenanceService(canonical_database=canonical, binding=binding, snapshot_root=snapshots)
    yield service, conn, snapshot
    conn.close()


def enroll(service, conn):
    with owner():
        desc = service.describe_snapshot(conn, snapshot_id="canary")
        return service.enroll(conn, snapshot_id="canary", dataset_id="native-dataset", snapshot_sha256=desc["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION)


def claimed(fixture):
    service, conn, _ = fixture
    enrollment = enroll(service, conn)
    with owner():
        job = service.enqueue(conn, enrollment_id=enrollment["enrollment_id"])
    return service.claim(conn, job["job_id"])


def result():
    return {"status": "ok", "messages_created": 0, "messages_processed": 0, "conversations_created": 0, "historical_skipped": 0}


def test_enrollment_is_explicit_and_binds_owner_file_dataset_snapshot(ingest_fixture):
    service, conn, snapshot = ingest_fixture
    with owner():
        desc = service.describe_snapshot(conn, snapshot_id="canary")
        assert desc["ownership_basis"] == "owner_attested_snapshot"
        assert desc["snapshot_sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
        assert "synthetic" not in str(desc)
        assert not service.marker.exists()
    ctx = claimed(ingest_fixture)
    assert (ctx.owner_id, ctx.source_id, ctx.dataset_id) == ("owner-1", "imessage", "native-dataset")
    assert service.snapshot_bytes(conn, ctx) == snapshot.read_bytes()
    with ctx.batch(conn):
        service.finish(conn, ctx, result())
    with owner():
        status = service.status(conn, job_id=ctx.job_id)
    assert status["status"] == "done" and status["result"] == result()


@pytest.mark.parametrize("kwargs", [{"cls": THIRD_PARTY}, {"actor": "other"}, {"channel": "local_http"}, {"channel": "internal"}])
def test_owner_label_same_actor_or_native_key_does_not_authorize(ingest_fixture, kwargs):
    service, conn, _ = ingest_fixture
    with owner(**kwargs), pytest.raises(PolicyError, match="owner_authority_required"):
        service.describe_snapshot(conn, snapshot_id="canary")
    assert not service.marker.exists()


@pytest.mark.parametrize("bad", ["../canary", "/canary", "canary.db/..", "canary:1", "a..b", "", " canary"])
def test_request_never_selects_arbitrary_path(ingest_fixture, bad):
    service, conn, _ = ingest_fixture
    with owner(), pytest.raises(PolicyError):
        service.describe_snapshot(conn, snapshot_id=bad)


def test_attestation_and_expected_sha_are_required_before_install(ingest_fixture):
    service, conn, _ = ingest_fixture
    with owner():
        desc = service.describe_snapshot(conn, snapshot_id="canary")
        for sha, text in [(desc["snapshot_sha256"], "yes"), ("0" * 64, OWNER_ATTESTATION)]:
            with pytest.raises(PolicyError):
                service.enroll(conn, snapshot_id="canary", dataset_id="native-dataset", snapshot_sha256=sha, owner_attestation=text)
    assert not service.marker.exists()


@pytest.mark.parametrize("mutation", ["writable", "wal", "symlink", "hardlink", "changed"])
def test_snapshot_replacement_or_mutation_never_enters_batch(ingest_fixture, mutation):
    service, conn, snapshot = ingest_fixture
    ctx = claimed(ingest_fixture)
    if mutation == "writable":
        snapshot.chmod(0o600)
    elif mutation == "wal":
        snapshot.with_name(snapshot.name + "-wal").write_bytes(b"pending")
    elif mutation == "symlink":
        other = snapshot.with_name("other.db")
        snapshot.rename(other)
        snapshot.symlink_to(other)
    elif mutation == "hardlink":
        os.link(snapshot, snapshot.with_name("alias.db"))
    else:
        snapshot.chmod(0o600)
        with sqlite3.connect(snapshot) as source:
            source.execute("UPDATE message SET value='changed'")
        snapshot.chmod(0o400)
    with pytest.raises(PolicyError), ctx.batch(conn):
        pytest.fail("no batch may start")
    assert conn.execute("SELECT count(*) FROM conversation_messages").fetchone()[0] == 0


def test_forged_context_wrong_dataset_and_wrong_connection_fail(ingest_fixture, tmp_path):
    service, conn, _ = ingest_fixture
    ctx = claimed(ingest_fixture)
    for forged in (replace(ctx), replace(ctx, owner_id="other"), replace(ctx, job_id="old-job")):
        with pytest.raises(PolicyError, match="ingest_context_invalid"):
            forged.assert_current(conn, source_id="imessage", dataset_id="native-dataset")
    with pytest.raises(PolicyError, match="ingest_context_invalid"):
        ctx.assert_current(conn, source_id="imessage", dataset_id="other")
    other = sqlite3.connect(tmp_path / "other.db")
    try:
        with pytest.raises(PolicyError, match="ingest_canonical_binding"):
            ctx.assert_current(other, source_id="imessage", dataset_id=ctx.dataset_id)
    finally:
        other.close()


def test_old_unbound_jobs_cannot_claim_and_enqueue_is_idempotent(ingest_fixture):
    service, conn, _ = ingest_fixture
    ctx = claimed(ingest_fixture)
    with pytest.raises(PolicyError, match="ingest_job_unknown"):
        service.claim(conn, "legacy-sync-job")
    with owner():
        assert service.enqueue(conn, enrollment_id=ctx.enrollment_id)["job_id"] == ctx.job_id
    with pytest.raises(PolicyError, match="ingest_job_not_claimable"):
        service.claim(conn, ctx.job_id)


def test_restart_expired_claim_cannot_write_or_fail_new_claim(ingest_fixture, monkeypatch):
    service, conn, _ = ingest_fixture
    monkeypatch.setattr("topos.permissions_v2.ingest_provenance.time.time", lambda: 1000)
    old = claimed(ingest_fixture)
    restarted = IngestProvenanceService(canonical_database=service.resolver.path, binding=service.binding, snapshot_root=service.root)
    monkeypatch.setattr("topos.permissions_v2.ingest_provenance.time.time", lambda: 1301)
    current = restarted.claim(conn, old.job_id)
    assert current.claim_token != old.claim_token
    with pytest.raises(PolicyError, match="ingest_claim_stale"):
        old.assert_current(conn, source_id="imessage", dataset_id=old.dataset_id)
    service.fail(conn, old, "stale_worker")
    current.assert_current(conn, source_id="imessage", dataset_id=current.dataset_id)


@pytest.mark.parametrize("table,sql", [("source_settings", "UPDATE source_settings SET enabled=0"), ("user_ingestion_sources", "UPDATE user_ingestion_sources SET enabled=0"), ("engine_config", "UPDATE engine_config SET value='other' WHERE key='user_id'")])
def test_source_or_owner_disable_enable_aba_invalidates_enrollment(ingest_fixture, table, sql):
    service, conn, _ = ingest_fixture
    ctx = claimed(ingest_fixture)
    conn.execute(sql)
    if table in ("source_settings", "user_ingestion_sources"):
        conn.execute(f"UPDATE {table} SET enabled=1")
    else:
        conn.execute("UPDATE engine_config SET value='owner-1' WHERE key='user_id'")
    conn.commit()
    with pytest.raises(PolicyError, match="ingest_enrollment_stale"):
        service.snapshot_bytes(conn, ctx)


def test_revoke_invalidates_claim_and_enrollment_retry_cannot_resurrect(ingest_fixture):
    service, conn, _ = ingest_fixture
    ctx = claimed(ingest_fixture)
    with owner():
        revoked = service.revoke(conn, enrollment_id=ctx.enrollment_id)
        assert revoked["revision"] == 2 and revoked["state"] == "revoked"
    with pytest.raises(PolicyError, match="ingest_enrollment_stale"):
        service.snapshot_bytes(conn, ctx)
    assert enroll(service, conn) == revoked


def test_uncertain_enrollment_ack_can_be_recovered_without_duplication(ingest_fixture):
    service, conn, _ = ingest_fixture
    first = enroll(service, conn)
    restarted = IngestProvenanceService(canonical_database=service.resolver.path, binding=service.binding, snapshot_root=service.root)
    assert enroll(restarted, conn) == first
    assert conn.execute("SELECT count(*) FROM ingest_provenance_enrollments").fetchone()[0] == 1


def test_idempotent_startup_owner_registration_and_unrelated_config_preserve_claim(ingest_fixture):
    service, conn, _ = ingest_fixture
    ctx = claimed(ingest_fixture)
    conn.execute("INSERT OR REPLACE INTO engine_config VALUES('user_id','owner-1')")
    conn.execute("INSERT OR REPLACE INTO engine_config VALUES('ui_config','changed')")
    conn.commit()
    assert service.snapshot_bytes(conn, ctx)


def test_batch_failure_rolls_back_rows_links_and_job_completion(ingest_fixture):
    service, conn, _ = ingest_fixture
    ctx = claimed(ingest_fixture)
    with pytest.raises(RuntimeError, match="abort"), ctx.batch(conn):
        conn.execute("INSERT INTO conversation_messages VALUES('imessage:1','chat1',?,'imessage','imessage:1',?,'self','human',1,'2026-09-15T00:00:00Z','canary',NULL,'authored')", (ctx.dataset_id, ctx.owner_id))
        ctx.record_insert(conn, "imessage:1")
        service.finish(conn, ctx, {**result(), "messages_created": 1, "messages_processed": 1})
        raise RuntimeError("abort")
    assert conn.execute("SELECT count(*) FROM conversation_messages").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM ingest_provenance_records").fetchone()[0] == 0
    with owner():
        assert service.status(conn, job_id=ctx.job_id)["status"] == "running"


@pytest.mark.parametrize("loss", ["table", "trigger", "marker", "foreign_database", "older_database"])
def test_missing_durable_authority_never_autoheals_after_restart(ingest_fixture, loss):
    service, conn, _ = ingest_fixture
    path = service.resolver.path
    pristine = path.read_bytes()
    ctx = claimed(ingest_fixture)
    if loss == "table":
        conn.execute("DROP TABLE ingest_provenance_records")
        conn.commit()
    elif loss == "trigger":
        conn.execute("DROP TRIGGER ingest_provenance_source_settings_update")
        conn.commit()
    elif loss == "marker":
        service.marker.unlink()
    elif loss == "foreign_database":
        # A different database at the same path: its clock identity differs.
        replacement = path.with_suffix(".replacement")
        shutil.copyfile(path, replacement)
        with sqlite3.connect(replacement) as other:
            other.execute("UPDATE permissions_v2_protection_state SET clock_id=?", ("e" * 64,))
        replacement.replace(path)
        conn.close()
        conn = sqlite3.connect(path)
    else:
        # The pre-enrollment bytes restored in place, same file identity.
        conn.close()
        path.write_bytes(pristine)
        conn = sqlite3.connect(path)
    restarted = IngestProvenanceService(canonical_database=path, binding=service.binding, snapshot_root=service.root)
    with owner(), pytest.raises(PolicyError):
        restarted.consume_command(conn, command_id="command-new", command_hash="a" * 64, allow_install=True)


def test_identical_database_copy_at_the_same_path_keeps_authority(ingest_fixture):
    service, conn, _ = ingest_fixture
    ctx = claimed(ingest_fixture)
    path = service.resolver.path
    replacement = path.with_suffix(".replacement")
    shutil.copyfile(path, replacement)
    replacement.replace(path)
    conn.close()
    conn = sqlite3.connect(path)
    restarted = IngestProvenanceService(canonical_database=path, binding=service.binding, snapshot_root=service.root)
    with owner():
        assert restarted.status(conn, job_id=ctx.job_id)["status"] == "running"
        with pytest.raises(PolicyError, match="ingest_command_replayed"):
            restarted.consume_command(conn, command_id="command-new", command_hash="a" * 64)
            restarted.consume_command(conn, command_id="command-new", command_hash="a" * 64)


def test_enrollment_and_claims_survive_device_and_inode_renumbering(ingest_fixture, monkeypatch):
    from tests.permissions_v2.test_evidence import simulate_remount
    service, conn, snapshot = ingest_fixture
    enrollment = enroll(service, conn)
    with owner():
        job = service.enqueue(conn, enrollment_id=enrollment["enrollment_id"])
        described = service.describe_snapshot(conn, snapshot_id="canary")
    assert not {"device", "inode", "root_device", "root_inode"} & set(described)
    simulate_remount(monkeypatch, service.resolver.path.parent)
    restarted = IngestProvenanceService(canonical_database=service.resolver.path, binding=service.binding, snapshot_root=service.root)
    with owner():
        assert restarted.status(conn, job_id=job["job_id"])["status"] == "queued"
        assert restarted.enroll(conn, snapshot_id="canary", dataset_id="native-dataset",
            snapshot_sha256=described["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION) == enrollment
    context = restarted.claim(conn, job["job_id"])
    assert restarted.snapshot_bytes(conn, context) == snapshot.read_bytes()


def test_signed_command_burn_survives_restart_and_dispatch_failure(ingest_fixture):
    service, conn, _ = ingest_fixture
    with owner():
        service.consume_command(conn, command_id="command-1", command_hash="a" * 64, allow_install=True)
        with pytest.raises(PolicyError):
            service.enqueue(conn, enrollment_id="missing-enrollment")
    restarted = IngestProvenanceService(canonical_database=service.resolver.path, binding=service.binding, snapshot_root=service.root)
    with owner(), pytest.raises(PolicyError, match="ingest_command_replayed"):
        restarted.consume_command(conn, command_id="command-1", command_hash="b" * 64)


@pytest.mark.parametrize("value", [0, None, "true", 2])
def test_disabled_or_malformed_native_source_never_enrolls(ingest_fixture, value):
    service, conn, _ = ingest_fixture
    conn.execute("UPDATE user_ingestion_sources SET enabled=?", (value,))
    conn.commit()
    with pytest.raises(PolicyError, match="ingest_source_disabled"):
        enroll(service, conn)
    assert not service.marker.exists()
