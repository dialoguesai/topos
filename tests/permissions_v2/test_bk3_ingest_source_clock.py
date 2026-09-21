"""W3/ING-3: the ingest source clock stops firing on sync receipts (source clock v2).

Under v1 every write to `user_ingestion_sources`, including the receipt a sync
leaves (`last_sync_at`, `last_error`, `updated_at`), advanced the store's
generation and staled every snapshot enrollment for good. v2 advances it only on
an insert, a delete, or an update of a column an enrollment rests on.

  S1  a sync receipt, success or failure, leaves an enrollment current (v2)
  S2  every watched column still stales it
  S3  a v1 store keeps v1 until the owner upgrades; a marker and a schema that
      disagree refuse; the upgrade runs once and advances the generation once
  S4  a torn upgrade leaves the store closed
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2 import production_corpus as pc
from tests.permissions_v2.test_ingest_provenance import OWNER_ATTESTATION, owner
from topos.permissions_v2 import ingest_provenance
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.ingest_provenance import IngestProvenanceService
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.storage import source_settings

DATASET = "native-dataset"


@pytest.fixture
def store(tmp_path):
    canonical = tmp_path / "canonical.db"
    with sqlite3.connect(canonical) as setup:
        pc.production_schema(setup)
        source_settings.ensure_table(setup)
        source_settings.put_source_settings(setup, DATASET, "imessage", enabled=True)
    ensure_protection_clock(canonical, owner_id=pc.OWNER_ID)
    # A fresh connection: the migration runner leaves a temp schema on its own, which the store refuses.
    conn = sqlite3.connect(canonical)
    durable = tmp_path / "permissions-v2"
    durable.mkdir(mode=0o700)
    snapshots = durable / "ingest-snapshots"
    snapshots.mkdir(mode=0o700)
    snapshot = snapshots / "canary.db"
    with sqlite3.connect(snapshot) as source:
        source.execute("CREATE TABLE message(value TEXT)")
        source.execute("INSERT INTO message VALUES('synthetic only')")
    snapshot.chmod(0o400)
    # The ingest store serves only a permissions-beta environment.
    binding = pc.BINDING.model_copy(update={"environment_id": "permissions-beta-test"})
    yield (lambda: IngestProvenanceService(canonical_database=canonical, binding=binding, snapshot_root=snapshots)), conn
    conn.close()


def claim(service, conn):
    with owner():
        desc = service.describe_snapshot(conn, snapshot_id="canary")
        enrollment = service.enroll(conn, snapshot_id="canary", dataset_id=DATASET, snapshot_sha256=desc["snapshot_sha256"],
                                    owner_attestation=OWNER_ATTESTATION)
        job = service.enqueue(conn, enrollment_id=enrollment["enrollment_id"])
    return service.claim(conn, job["job_id"])


def generation(conn):
    return conn.execute("SELECT generation FROM ingest_provenance_state").fetchone()[0]


@pytest.mark.parametrize("success", [True, False])
def test_S1_a_sync_receipt_leaves_the_enrollment_current(store, success):
    make, conn = store
    service = make()
    ctx = claim(service, conn)
    before = generation(conn)
    source_settings.update_sync_result(conn, DATASET, "imessage", success=success,
                                       last_sync_at="2026-09-18T00:00:00Z", last_error=None if success else "timeout")
    conn.commit()
    assert generation(conn) == before
    assert service.snapshot_bytes(conn, ctx)


@pytest.mark.parametrize("sql", [
    "UPDATE user_ingestion_sources SET enabled=0",
    "UPDATE user_ingestion_sources SET posture='ambient'",
    "UPDATE user_ingestion_sources SET dataset_id='other'",
    "INSERT INTO user_ingestion_sources(dataset_id, source_id, enabled) VALUES ('second','imessage',1)",
    "DELETE FROM user_ingestion_sources WHERE dataset_id='nothing-matches'",
])
def test_S2_every_watched_change_still_stales(store, sql):
    make, conn = store
    service = make()
    ctx = claim(service, conn)
    before = generation(conn)
    conn.execute(sql)
    conn.commit()
    if sql.startswith("DELETE"):
        # A DELETE statement that removes no row fires no row trigger: nothing an enrollment rests on moved.
        assert generation(conn) == before
        return
    with pytest.raises(PolicyError, match="ingest_enrollment_stale|ingest_source_disabled"):
        service.snapshot_bytes(conn, ctx)


def test_S3_a_v1_store_upgrades_once_and_advances_once(store, monkeypatch):
    make, conn = store
    monkeypatch.setattr(ingest_provenance, "SOURCE_CLOCK_VERSION", 1)
    service = make()
    ctx = claim(service, conn)
    monkeypatch.undo()
    source_settings.update_sync_result(conn, DATASET, "imessage", success=True, last_sync_at="t")
    conn.commit()
    with pytest.raises(PolicyError, match="ingest_enrollment_stale"):
        service.snapshot_bytes(conn, ctx)  # v1: the receipt staled it
    before = generation(conn)
    with owner():
        result = service.upgrade_source_clock_v2(conn)
    assert result == {"source_clock_version": 2, "generation": before + 1}
    source_settings.update_sync_result(conn, DATASET, "imessage", success=True, last_sync_at="t2")
    conn.commit()
    assert generation(conn) == before + 1
    with owner(), pytest.raises(PolicyError, match="ingest_source_clock_current"):
        service.upgrade_source_clock_v2(conn)
    make()._check(conn)  # a fresh process reads the v2 marker and schema


def test_S3_marker_and_schema_that_disagree_refuse(store, monkeypatch):
    make, conn = store
    service = make()
    claim(service, conn)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='ingest_provenance_user_ingestion_sources_update'").fetchone()[0]
    conn.execute("DROP TRIGGER ingest_provenance_user_ingestion_sources_update")
    conn.execute(sql.replace("UPDATE OF dataset_id, source_id, enabled, posture", "UPDATE"))
    conn.commit()
    with pytest.raises(PolicyError, match="ingest_ledger_invalid"):
        make()._check(conn)


def test_S3_a_version_field_ahead_of_the_schema_refuses(store, monkeypatch):
    """A marker whose source_clock_version says v2 while its digest and triggers are v1 --
    an older store file paired with a newer version field -- must not be read as either."""
    make, conn = store
    monkeypatch.setattr(ingest_provenance, "SOURCE_CLOCK_VERSION", 1)
    service = make()
    claim(service, conn)
    monkeypatch.undo()
    marker = json.loads(service.marker.read_text())
    service.marker.chmod(0o600)
    service.marker.write_text(json.dumps({**marker, "source_clock_version": 2}))
    with pytest.raises(PolicyError, match="ingest_ledger_invalid"):
        make()._check(conn)


def test_S4_a_torn_upgrade_stays_closed(store, monkeypatch):
    make, conn = store
    monkeypatch.setattr(ingest_provenance, "SOURCE_CLOCK_VERSION", 1)
    service = make()
    claim(service, conn)
    monkeypatch.undo()

    real, calls = service._authority_digest, []

    def torn(conn):
        calls.append(1)
        if len(calls) > 1:  # the first call is the pre-upgrade check; the second follows the pending marker
            raise RuntimeError("power lost")
        return real(conn)
    monkeypatch.setattr(service, "_authority_digest", torn)
    with owner(), pytest.raises(RuntimeError):
        service.upgrade_source_clock_v2(conn)
    monkeypatch.undo()
    with pytest.raises(PolicyError):
        make()._check(conn)
