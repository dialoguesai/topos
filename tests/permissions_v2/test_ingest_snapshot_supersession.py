"""The evidence-time guard through the real snapshot lane, across two enrollments.

The store's unit tests use a stand-in trust. This drives the trust the lane
actually hands the store (``LinkedRowTrust``) through the signed doors: the
owner enrolls a snapshot holding a newer statement, then a second snapshot
(its own dataset) holding an older one. The older statement must not bring the
old employer back, and must stay in history. Run in the other order, the newer
statement supersedes as it always has.

It also pins the ceiling the lane adds. The trusted writer stamps
``ingested_at`` at insert, but the snapshot's bytes were fixed earlier, when the
owner attested them, so a message dated after that moment cannot be a genuine
native time and must never be ordered.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_ingest_snapshot_work_canary import (  # noqa: F401 (fixtures)
    OWNER_ATTESTATION, SELF_ENTITY, _MAC_EPOCH, attest_selves, canonical, corpus, facts, ingest, lane, native_snapshot,
    paired_runtime, projection_runtime, spy_lane_stats)


def native(hours_ago):
    event = datetime.now(timezone.utc).replace(microsecond=654321) - timedelta(hours=hours_ago)
    delta = event - _MAC_EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return micros * 1000


async def enroll_and_run(lane, *, snapshot_id, rowid, text, hours_ago, attested_hours_ago=None, monkeypatch=None):
    """One owner message at ROWID ``rowid``, in its own snapshot and dataset."""
    def mutate(db):
        db.execute("UPDATE message SET ROWID=?, text=?, is_from_me=1, date=? WHERE ROWID=1", (rowid, text, native(hours_ago)))
        db.execute("UPDATE chat_message_join SET message_id=? WHERE message_id=1", (rowid,))

    path = lane.root / f"{snapshot_id}.db"
    path.write_bytes(native_snapshot(count=1, mutate=mutate))
    path.chmod(0o400)
    described = await ingest(lane, {"operation": "describe", "snapshot_id": snapshot_id})
    if attested_hours_ago is not None:
        # Only the enrollment's authorized_at reads this clock; command signing does not.
        from topos.permissions_v2 import ingest_provenance
        monkeypatch.setattr(ingest_provenance, "time", SimpleNamespace(time=lambda: time.time() - attested_hours_ago * 3600))
    enrollment = await ingest(lane, {"operation": "enroll", "snapshot_id": snapshot_id, "dataset_id": snapshot_id + "-dataset",
                                     "snapshot_sha256": described.snapshot_sha256, "owner_attestation": OWNER_ATTESTATION})
    if attested_hours_ago is not None:
        monkeypatch.undo()
    job = await ingest(lane, {"operation": "enqueue", "enrollment_id": enrollment.enrollment_id})
    result = await ingest(lane, {"operation": "run", "job_id": job.job_id})
    assert result.model_dump()["status"] == "ok" and result.model_dump()["messages_created"] == 1
    return enrollment


def employers(lane):
    return sorted((fact.payload["object_value"], fact.valid_to is None) for fact in facts(lane))


@pytest.mark.asyncio
async def test_an_older_snapshot_never_brings_back_an_employer_a_newer_one_replaced(lane, monkeypatch):
    await attest_selves(lane, [SELF_ENTITY])
    stats = spy_lane_stats(monkeypatch)
    await enroll_and_run(lane, snapshot_id="newer-snapshot", rowid=301, text="I work at Northwind Current.", hours_ago=1)
    await enroll_and_run(lane, snapshot_id="older-snapshot", rowid=201, text="I work at Northwind Former.", hours_ago=3)
    assert employers(lane) == [("Northwind Current", True), ("Northwind Former", False)]
    assert stats[-1] == {"rows_linked": 1, "facts_written": 0, "older_evidence_kept_as_history": 1}
    with canonical(lane) as conn:
        valid_from, valid_to = conn.execute("SELECT valid_from, valid_to FROM signal_objects "
                                            "WHERE payload_json LIKE '%Northwind Former%'").fetchone()
    assert valid_from == valid_to  # closed on arrival, never current


@pytest.mark.asyncio
async def test_a_newer_snapshot_still_supersedes_an_older_one(lane, monkeypatch):
    await attest_selves(lane, [SELF_ENTITY])
    stats = spy_lane_stats(monkeypatch)
    await enroll_and_run(lane, snapshot_id="older-snapshot", rowid=201, text="I work at Northwind Former.", hours_ago=3)
    await enroll_and_run(lane, snapshot_id="newer-snapshot", rowid=301, text="I work at Northwind Current.", hours_ago=1)
    assert employers(lane) == [("Northwind Current", True), ("Northwind Former", False)]
    assert stats[-1] == {"rows_linked": 1, "facts_written": 1}


@pytest.mark.asyncio
async def test_revoking_the_newer_enrollment_removes_its_support(lane, monkeypatch):
    """A revoked enrollment no longer vouches, so its time cannot hold back an older statement."""
    await attest_selves(lane, [SELF_ENTITY])
    newer = await enroll_and_run(lane, snapshot_id="newer-snapshot", rowid=301, text="I work at Northwind Current.", hours_ago=1)
    await ingest(lane, {"operation": "revoke", "enrollment_id": newer.enrollment_id})
    stats = spy_lane_stats(monkeypatch)
    await enroll_and_run(lane, snapshot_id="older-snapshot", rowid=201, text="I work at Northwind Former.", hours_ago=3)
    assert employers(lane) == [("Northwind Current", False), ("Northwind Former", True)]
    assert stats[-1] == {"rows_linked": 1, "facts_written": 1}


@pytest.mark.asyncio
async def test_a_message_dated_after_its_snapshot_was_attested_is_never_ordered(lane):
    """Its row was ingested later still, so only the attestation time rules it out."""
    await attest_selves(lane, [SELF_ENTITY])
    with pytest.MonkeyPatch.context() as clock:
        await enroll_and_run(lane, snapshot_id="newer-snapshot", rowid=301, text="I work at Northwind Current.",
                             hours_ago=1, attested_hours_ago=2, monkeypatch=clock)
    await enroll_and_run(lane, snapshot_id="older-snapshot", rowid=201, text="I work at Northwind Former.", hours_ago=3)
    assert employers(lane) == [("Northwind Current", False), ("Northwind Former", True)]


@pytest.mark.asyncio
async def test_a_weaker_statement_than_a_strong_existing_fact_is_queued_not_counted_as_written(lane, monkeypatch):
    """Known limit: a stronger incumbent (a resume fact is 0.9, a message 0.6) keeps the lane's fact out."""
    from topos.features.facts.store import FactStore
    await attest_selves(lane, [SELF_ENTITY])
    with canonical(lane) as conn:
        FactStore(conn).assert_fact(subject_entity_id=SELF_ENTITY, predicate="works_at", object_value="Lumon Industries",
                                    confidence=0.9, source_refs=[{"table": "profile_records", "record_id": "prof-1"}])
    stats = spy_lane_stats(monkeypatch)
    await enroll_and_run(lane, snapshot_id="newer-snapshot", rowid=301, text="I work at Northwind Current.", hours_ago=1)
    assert employers(lane) == [("Lumon Industries", True)]
    assert stats[-1] == {"rows_linked": 1, "facts_written": 0, "conflict_queued": 1}
    with canonical(lane) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_conflicts").fetchone() == (1,)
