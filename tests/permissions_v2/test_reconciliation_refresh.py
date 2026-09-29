"""RD8: an owner refresh re-proves one recovery enrollment against a fresh capture, all or nothing.

  F1  re-proven rows keep their link at the next revision, new rows gain one, aged links are deleted
  F2  canonical rows, the source row and every opaque-id input stay byte for byte; the clock moves once
  F3  a stale (never a revoked) enrollment is brought current; a disabled source refuses
  F4  a whole-message ceiling survives every refresh, even when the row changed
  F5  any refusal or mismatch leaves the ledger, the marker and the previous proof exactly as they were
  F6  only the authenticated owner refreshes, with the attestation, a new capture and a valid window
  F7  a capture no enrollment names is discarded, a named one never is; a capture skips others' rows
  F8  the owner door: local owner socket only, one refresh at a time, the paired owner only
  F9  the owner door end to end: success, a mid-transaction failure, a dry run, an uncovered window
  F10 a window that leaves a young current link uncovered refuses unless acknowledged, and then retires it
  F11 a capture that re-proves under half of the young current links whose rows did not change refuses
  F12 refusals the transaction must make on its own: incomplete, owned elsewhere, wrong lane, late change
  F13 a dry run reports the counts and writes nothing
  F14 no window reaches past the capture reach, whatever the clock says, so a deleted link never returns

Every synthetic message is dated relative to now: a refresh window may not start more than 31 days
before the later of now and the enrollment's last authorization.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.ingestion.test_owner_snapshot import NOW
from tests.permissions_v2.test_imessage_reconciliation import snapshot
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT, parse_reconciliation_snapshot
from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION
from topos.permissions_v2.reconciliation_facts import classification
from topos.permissions_v2.reconciliation_provenance import (
    discard_capture, publish_existing, refresh_existing, validate_existing)

DATASET = "native-dataset"
LEDGER = ("ingest_provenance_enrollments", "ingest_provenance_jobs", "ingest_provenance_records",
          "ingest_provenance_state", "permissions_v2_protection_state")
DAY, DAY_NS, DAY_US = 86_400, 86_400 * 1_000_000_000, 86_400 * 1_000_000
CEILING = classification({"domains": ["work", "health"], "sensitivity": "special"})
T0 = int(time.time())


def native_ns(days_ago: float) -> int:
    """A native (2001-epoch) nanosecond clock `days_ago` before T0, microsecond-exact."""
    return (T0 - 978307200 - int(days_ago * DAY)) * 1_000_000_000 + 123_456_000


def days_ago(rowid: int, offsets=None) -> float:
    """ROWID i defaults to 14 - i days ago (ROWID 1: 13 days; ROWID 13: 1 day)."""
    return (offsets or {}).get(rowid, 14 - rowid)


def capture(service, name, ids, *, offsets=None):
    """A private native capture holding exactly these owner-sent ROWIDs, as the recovery writes it."""
    keep = ",".join(str(i) for i in ids)

    def mutate(db):
        db.execute("UPDATE message SET is_from_me=1")
        for rowid in ids:
            db.execute("UPDATE message SET date=? WHERE ROWID=?", (native_ns(days_ago(rowid, offsets)), rowid))
        db.execute(f"DELETE FROM message WHERE ROWID NOT IN ({keep})")
        db.execute(f"DELETE FROM chat_message_join WHERE message_id NOT IN ({keep})")
    data = snapshot(count=max(ids), mutate=mutate)
    path = service.root / (name + ".db")
    path.write_bytes(data)
    path.chmod(0o400)
    return name, data


def add_canonical(conn, data, *, dataset=DATASET):
    columns = [r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")]
    for native in parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc)):
        if conn.execute("SELECT 1 FROM conversation_messages WHERE message_id=?", (native.message_id,)).fetchone():
            continue
        row = {"message_id": native.message_id, "source_record_id": native.message_id, "source_id": "imessage",
               "dataset_id": dataset, "owner_user_id": None, "conversation_id": native.conversation_id,
               "content": native.content, "event_at": native.event_at, "is_from_self": 1, "sender_id": "self",
               "sender_type": "human", "actor_role": None,
               "metadata_json": json.dumps({"message_guid": native.message_guid, "chat_guid": native.chat_guid,
                                            "chat_identifier": native.chat_identifier, "associated_message_type": 0})}
        conn.execute("INSERT INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                     [row.get(column) for column in columns])
    conn.commit()


def describe(service, conn, name):
    with owner():
        return service.describe_snapshot(conn, snapshot_id=name, reader_contract=ATTRIBUTED_CONTRACT)


def make_store(ingest_fixture, ids=(1, 2), *, offsets=None):
    service, conn, _ = ingest_fixture
    conn.execute("CREATE TABLE IF NOT EXISTS ai_chat_messages(message_id TEXT,content TEXT)")
    name, data = capture(service, "capture-a", list(ids), offsets=offsets)
    add_canonical(conn, data)
    desc = describe(service, conn, name)
    with owner():
        enrollment = service.enroll(conn, snapshot_id=name, dataset_id=DATASET, snapshot_sha256=desc["snapshot_sha256"],
                                    owner_attestation=OWNER_ATTESTATION, reader_contract=ATTRIBUTED_CONTRACT)
    return service, conn, enrollment["enrollment_id"]


@pytest.fixture
def store(ingest_fixture):
    return make_store(ingest_fixture)


def publish(store, **kwargs):
    service, conn, enrollment = store
    with owner():
        return publish_existing(service, conn, enrollment_id=enrollment, **kwargs)


def event_us(conn, message_id):
    return canonical_utc_microseconds(conn.execute("SELECT event_at FROM conversation_messages WHERE message_id=?",
                                                   (message_id,)).fetchone()[0])


def recent_window(now_seconds=None):
    """The runbook's window: the last 30 days up to now."""
    now = int(time.time()) if now_seconds is None else now_seconds
    return (now - 30 * DAY) * 1_000_000, (now + 60) * 1_000_000


def refresh(store, name, **kwargs):
    service, conn, _ = store
    desc = describe(service, conn, name)
    start, end = recent_window(kwargs.get("now_seconds"))
    kwargs.setdefault("window_start_us", start)
    kwargs.setdefault("window_end_us", end)
    with owner(**kwargs.pop("principal", {})):
        return refresh_existing(service, conn, dataset_id=kwargs.pop("dataset_id", DATASET), snapshot_id=name,
                                snapshot_sha256=desc["snapshot_sha256"],
                                owner_attestation=kwargs.pop("owner_attestation", OWNER_ATTESTATION), **kwargs)


def proven(store, message_id):
    service, conn, _ = store
    try:
        validate_existing(service, conn, message_id=message_id, dataset_id=DATASET)
        return True
    except PolicyError:
        return False


def ledger(conn):
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in LEDGER}


def links(conn):
    return [tuple(link) for link in conn.execute(
        "SELECT message_id, enrollment_revision FROM ingest_provenance_records ORDER BY 1")]


def ceiling_of(conn, message_id):
    return json.loads(conn.execute("SELECT row_identity FROM ingest_provenance_records WHERE message_id=?",
                                   (message_id,)).fetchone()[0])["classification"]


# -- F1-F5: what a refresh writes -----------------------------------------------------------------

AGED = {1: 40}


def test_F1_reproves_links_new_rows_and_deletes_aged_ones(ingest_fixture):
    store = make_store(ingest_fixture, offsets=AGED)
    service, conn, enrollment = store
    publish(store)
    name, data = capture(service, "capture-b", [2, 3], offsets=AGED)
    add_canonical(conn, data)
    assert [proven(store, f"imessage:{i}") for i in (1, 2, 3)] == [True, True, False]
    counts = refresh(store, name)
    assert counts == {"dropped_aged": 1, "linked_new": 1, "previous_capture_removed": 1, "reproven": 1}
    assert [proven(store, f"imessage:{i}") for i in (1, 2, 3)] == [False, True, True]
    row = conn.execute("SELECT enrollment_id, revision, state, snapshot_json FROM ingest_provenance_enrollments").fetchall()
    assert [(r[0], r[1], r[2], json.loads(r[3])["snapshot_id"]) for r in row] == [(enrollment, 2, "active", "capture-b")]
    jobs = conn.execute("SELECT enrollment_revision, status FROM ingest_provenance_jobs").fetchall()
    assert [tuple(job) for job in jobs] == [(2, "done")]
    assert links(conn) == [("imessage:2", 2), ("imessage:3", 2)]
    assert not (service.root / "capture-a.db").exists() and (service.root / "capture-b.db").exists()


def test_F2_rows_source_and_opaque_inputs_unchanged_and_clock_moves_once(store):
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [1, 2])
    rows = conn.execute("SELECT * FROM conversation_messages ORDER BY 1").fetchall()
    sources = conn.execute("SELECT * FROM user_ingestion_sources").fetchall()
    clock = conn.execute("SELECT generation FROM permissions_v2_protection_state").fetchone()[0]
    assert refresh(store, name)["reproven"] == 2
    # Every input of an opaque record id -- table, source, dataset, record id -- is read from these rows.
    assert conn.execute("SELECT * FROM conversation_messages ORDER BY 1").fetchall() == rows
    assert conn.execute("SELECT * FROM user_ingestion_sources").fetchall() == sources
    assert conn.execute("SELECT generation FROM permissions_v2_protection_state").fetchone()[0] == clock + 1


def test_F3_a_stale_enrollment_is_brought_current(store):
    service, conn, _ = store
    publish(store)
    conn.execute("UPDATE user_ingestion_sources SET posture='personal'")
    conn.commit()
    assert not proven(store, "imessage:1")
    name, _ = capture(service, "capture-b", [1, 2])
    assert refresh(store, name)["reproven"] == 2
    assert proven(store, "imessage:1") and proven(store, "imessage:2")


def test_F3_a_revoked_enrollment_is_never_refreshed(store):
    service, conn, enrollment = store
    publish(store)
    with owner():
        service.revoke(conn, enrollment_id=enrollment)
    name, _ = capture(service, "capture-b", [1, 2])
    before = ledger(conn)
    with pytest.raises(PolicyError, match="reconciliation_enrollment_revoked"):
        refresh(store, name)
    assert ledger(conn) == before and not proven(store, "imessage:1")


def test_F3_a_disabled_source_refuses(store):
    service, conn, _ = store
    publish(store)
    conn.execute("UPDATE user_ingestion_sources SET enabled=0")
    conn.commit()
    name, _ = capture(service, "capture-b", [1, 2])
    before = ledger(conn)
    with pytest.raises(PolicyError, match="ingest_source_disabled"):
        refresh(store, name)
    assert ledger(conn) == before


def test_F4_a_ceiling_survives_every_refresh_even_when_the_row_changed(store):
    service, conn, _ = store
    publish(store, classifications={"imessage:1": CEILING})
    name, _ = capture(service, "capture-b", [1, 2])
    counts = refresh(store, name)
    assert counts["reproven"] == 2 and counts["ceiling_carried"] == 1 and ceiling_of(conn, "imessage:1") == CEILING
    # Still an exact native match, but a changed reviewed surface. Dropping the ceiling here could
    # widen a release of the same text; keeping it can only withhold more.
    conn.execute("UPDATE conversation_messages SET actor_role='authored' WHERE message_id='imessage:1'")
    conn.commit()
    name, _ = capture(service, "capture-c", [1, 2])
    counts = refresh(store, name)
    assert counts["reproven_row_changed"] == 1 and counts["ceiling_carried"] == 1
    assert ceiling_of(conn, "imessage:1") == CEILING and proven(store, "imessage:1")


def test_F5_a_mismatch_leaves_everything_as_it_was(store):
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [1, 2])
    conn.execute("UPDATE conversation_messages SET content='changed' WHERE message_id='imessage:2'")
    conn.commit()
    before, marker = ledger(conn), service.marker.read_bytes()
    with pytest.raises(PolicyError, match="reconciliation_content_mismatch"):
        refresh(store, name)
    assert ledger(conn) == before and service.marker.read_bytes() == marker
    assert proven(store, "imessage:1") and (service.root / "capture-a.db").exists()


# -- F6: who and what may ask ---------------------------------------------------------------------

def test_F6_only_the_owner_with_the_sentence_a_new_capture_and_a_window(store):
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [1, 2])
    before = ledger(conn)
    with pytest.raises(PolicyError):
        refresh(store, name, principal={"actor": "another"})
    with pytest.raises(PolicyError, match="ingest_owner_attestation_required"):
        refresh(store, name, owner_attestation="I agree")
    with pytest.raises(PolicyError, match="reconciliation_refresh_unchanged"):
        refresh(store, "capture-a")
    with pytest.raises(PolicyError, match="reconciliation_refresh_unenrolled"):
        refresh(store, name, dataset_id="another-dataset")
    for start, end in ((10, 10), (10, 5), (None, 10), (0, 10.5)):
        with pytest.raises(PolicyError, match="reconciliation_window_invalid"):
            refresh(store, name, window_start_us=start, window_end_us=end)
    # Every captured message must lie inside the window the refresh names.
    with pytest.raises(PolicyError, match="reconciliation_capture_outside_window"):
        refresh(store, name, window_start_us=event_us(conn, "imessage:2") - DAY_US // 2)
    assert ledger(conn) == before


def test_F6_a_captured_message_without_its_native_time_refuses_precisely(store, monkeypatch):
    from dataclasses import replace
    from topos.permissions_v2 import reconciliation_provenance
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [1, 2])
    real = reconciliation_provenance.parse_reconciliation_snapshot
    monkeypatch.setattr(reconciliation_provenance, "parse_reconciliation_snapshot", lambda *args, **kwargs: tuple(
        replace(record, native_event_nanoseconds=None) for record in real(*args, **kwargs)))
    before = ledger(conn)
    with pytest.raises(PolicyError, match="reconciliation_capture_time_missing"):
        refresh(store, name)
    assert ledger(conn) == before and proven(store, "imessage:1")


# -- F7: captures ---------------------------------------------------------------------------------

def test_F7_only_an_unnamed_capture_is_discarded_and_discard_never_raises(store):
    service, conn, _ = store
    publish(store)
    named = describe(service, conn, "capture-a")
    assert discard_capture(service, conn, named) is False and (service.root / "capture-a.db").exists()
    name, _ = capture(service, "capture-b", [1, 2])
    unnamed = describe(service, conn, name)

    class Closed:
        def execute(self, *_args):
            raise sqlite3.OperationalError("database is locked")
    assert discard_capture(service, Closed(), unnamed) is False and (service.root / "capture-b.db").exists()
    assert discard_capture(service, conn, unnamed) is True and not (service.root / "capture-b.db").exists()


def test_F7_a_capture_leaves_out_rows_another_enrollment_proves(store, tmp_path, monkeypatch):
    from topos.permissions_v2 import native_imessage_probe as probe
    service, conn, _ = store
    native = tmp_path / "native-chat.db"
    _, data = capture(service, "native-source", [1, 2])
    native.write_bytes(data)
    (service.root / "native-source.db").unlink()
    actual = probe.probe_native_messages
    monkeypatch.setattr(probe, "probe_native_messages", lambda canonical, **kw: actual(canonical, **kw, _native_path=native))
    reader = sqlite3.connect(service.resolver.path.as_uri() + "?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    reader.execute("PRAGMA query_only=ON")
    reader.execute("BEGIN")
    now = datetime.now(timezone.utc)
    try:
        name, measured = probe.capture_matching_snapshot(reader, snapshot_root=service.root, dataset_id=DATASET,
            owner_id="owner-1", starts_at=datetime.fromtimestamp(T0 - 20 * DAY, tz=timezone.utc).isoformat(timespec="microseconds"),
            ends_at=now.isoformat(timespec="microseconds"), now=now,
            skip=lambda message_id: "row_owned_elsewhere" if message_id == "imessage:1" else None)
    finally:
        reader.close()
    parsed = parse_reconciliation_snapshot((service.root / (name + ".db")).read_bytes(), now=now, reader_contract=ATTRIBUTED_CONTRACT)
    assert [record.message_id for record in parsed] == ["imessage:2"]
    assert measured["counts"]["excluded_row_owned_elsewhere"] == 1 and measured["counts"]["canonical_exact_match"] == 2


# -- F10, F11: what a refresh may not silently lose -----------------------------------------------

def test_F10_a_late_start_or_an_early_end_refuses_until_acknowledged_then_retires(store):
    service, conn, _ = store
    publish(store, classifications={"imessage:1": CEILING})
    one, two = event_us(conn, "imessage:1"), event_us(conn, "imessage:2")
    late, _ = capture(service, "capture-late", [2])
    early, _ = capture(service, "capture-early", [1])
    before = ledger(conn)
    with pytest.raises(PolicyError, match="reconciliation_refresh_window_uncovered"):
        refresh(store, late, window_start_us=two - DAY_US // 2, window_end_us=two + DAY_US // 2)
    with pytest.raises(PolicyError, match="reconciliation_refresh_window_uncovered"):
        refresh(store, early, window_start_us=one - DAY_US // 2, window_end_us=one + DAY_US // 2)
    assert ledger(conn) == before and proven(store, "imessage:1") and proven(store, "imessage:2")
    counts = refresh(store, late, window_start_us=two - DAY_US // 2, window_end_us=two + DAY_US // 2,
                     accept_uncovered=True)
    assert counts["retired_uncovered"] == 1 and counts["reproven"] == 1
    # Retired, not deleted: the link stays at its old revision, where it proves nothing.
    assert links(conn) == [("imessage:1", 1), ("imessage:2", 2)] and not proven(store, "imessage:1")
    fixed, _ = capture(service, "capture-fixed", [1, 2])
    counts = refresh(store, fixed)
    assert counts["relinked_retired"] == 1 and counts["ceiling_carried"] == 1
    assert proven(store, "imessage:1") and ceiling_of(conn, "imessage:1") == CEILING
    # Retired again, then 30 days on (both links past the capture reach): deleted.
    again, _ = capture(service, "capture-again", [2])
    refresh(store, again, window_start_us=two - DAY_US // 2, window_end_us=two + DAY_US // 2, accept_uncovered=True)
    fresh, data = capture(service, "capture-fresh", [13], offsets={13: 0.5})
    add_canonical(conn, data)
    # 30 days on, the reach starts a day before T0: that is where this window may start.
    counts = refresh(store, fresh, now_seconds=T0 + 30 * DAY, window_start_us=(T0 - DAY + 60) * 1_000_000)
    assert counts["dropped_aged"] == 2 and counts["linked_new"] == 1 and links(conn) == [("imessage:13", 5)]


def test_F10_a_link_a_later_capture_could_still_reach_is_retired_not_deleted(ingest_fixture):
    band = {1: 31 - 1 / 24}
    store = make_store(ingest_fixture, offsets=band)
    service, conn, _ = store
    publish(store, classifications={"imessage:1": CEILING})
    one = event_us(conn, "imessage:1")
    two, _ = capture(service, "capture-two", [2], offsets=band)
    # 31 days old less an hour: past every 30-day grant but within a capture's reach. Retired, no refusal.
    counts = refresh(store, two)
    assert counts["retired_aged"] == 1 and "dropped_aged" not in counts
    assert links(conn)[0] == ("imessage:1", 1) and not proven(store, "imessage:1")
    # A capture that can still reach it relinks it with the ceiling it had, never without.
    both, _ = capture(service, "capture-both", [1, 2], offsets=band)
    counts = refresh(store, both, window_start_us=one - 1_800 * 1_000_000)
    assert counts["relinked_retired"] == 1 and ceiling_of(conn, "imessage:1") == CEILING
    # Only once no capture can reach it (31 days and a day's margin) is it deleted.
    again, _ = capture(service, "capture-again", [2], offsets=band)
    counts = refresh(store, again, now_seconds=T0 + 2 * DAY)
    assert counts["dropped_aged"] == 1 and [link[0] for link in links(conn)] == ["imessage:2"]


def test_F11_a_mass_unproven_capture_refuses_until_acknowledged(ingest_fixture):
    store = make_store(ingest_fixture, ids=(1, 2, 3))
    service, conn, _ = store
    publish(store, classifications={"imessage:2": CEILING})
    lost, _ = capture(service, "capture-lost", [1])
    before = ledger(conn)
    # The window covers all three and the rows did not change: the reader lost two of three.
    with pytest.raises(PolicyError, match="reconciliation_refresh_mass_unproven"):
        refresh(store, lost)
    assert ledger(conn) == before
    counts = refresh(store, lost, accept_unproven=True)
    assert counts["retired_unmatched"] == 2 and not proven(store, "imessage:2")
    # Already retired, they are not current proofs: the next refresh neither counts them toward the
    # floor nor asks again.
    same, _ = capture(service, "capture-same", [1])
    counts = refresh(store, same)
    assert counts["still_retired"] == 2 and "retired_unmatched" not in counts
    back, _ = capture(service, "capture-back", [1, 2, 3])
    counts = refresh(store, back)
    assert counts["relinked_retired"] == 2 and ceiling_of(conn, "imessage:2") == CEILING
    assert all(proven(store, f"imessage:{i}") for i in (1, 2, 3))


def test_F11_one_unproven_link_below_the_floor_is_retired_and_changed_rows_do_not_count(ingest_fixture):
    store = make_store(ingest_fixture, ids=(1, 2, 3))
    service, conn, _ = store
    publish(store)
    two, _ = capture(service, "capture-two", [1, 2])
    counts = refresh(store, two)
    assert counts["retired_unmatched"] == 1 and counts["reproven"] == 2
    again, _ = capture(service, "capture-again", [1, 2, 3])
    refresh(store, again)
    conn.execute("UPDATE conversation_messages SET content='edited' WHERE message_id IN ('imessage:2','imessage:3')")
    conn.commit()
    one, _ = capture(service, "capture-one", [1])
    # Two of three unproven, but both rows changed: their proofs no longer validated anyway.
    counts = refresh(store, one)
    assert counts["retired_row_changed"] == 2 and "retired_unmatched" not in counts


def test_F11_the_floor_is_a_share_of_the_links_the_capture_could_have_re_proven(ingest_fixture):
    ids = tuple(range(1, 13))
    store = make_store(ingest_fixture, ids=ids)
    service, conn, _ = store
    publish(store)
    # Twelve young links. Two re-prove unchanged; two re-prove with a changed reviewed surface; four
    # rows changed and are not captured; four unchanged rows the capture lost.
    conn.execute("UPDATE conversation_messages SET actor_role='authored' WHERE message_id IN ('imessage:3','imessage:4')")
    conn.execute("UPDATE conversation_messages SET content='edited' "
                 "WHERE message_id IN ('imessage:5','imessage:6','imessage:7','imessage:8')")
    conn.commit()
    partial, _ = capture(service, "capture-partial", [1, 2, 3, 4])
    before = ledger(conn)
    # Four lost of six that could have been re-proven: more than half, although only a third of twelve.
    with pytest.raises(PolicyError, match="reconciliation_refresh_mass_unproven"):
        refresh(store, partial)
    assert ledger(conn) == before
    counts = refresh(store, partial, accept_unproven=True)
    assert (counts["reproven"], counts["reproven_row_changed"], counts["retired_row_changed"],
            counts["retired_unmatched"]) == (2, 2, 4, 4)


# -- F12: refusals inside the transaction ---------------------------------------------------------

def test_F12_an_enrollment_whose_publication_never_completed_refuses(store):
    service, conn, _ = store
    name, _ = capture(service, "capture-b", [1, 2])
    before = ledger(conn)
    with pytest.raises(PolicyError, match="reconciliation_refresh_incomplete"):
        refresh(store, name)
    assert ledger(conn) == before


def test_F12_a_row_another_enrollment_proves_refuses_inside_the_transaction(store):
    service, conn, _ = store
    publish(store)
    other = "second-dataset"
    name, data = capture(service, "capture-other", [5])
    add_canonical(conn, data, dataset=other)
    desc = describe(service, conn, name)
    with owner():
        enrollment = service.enroll(conn, snapshot_id=name, dataset_id=other, snapshot_sha256=desc["snapshot_sha256"],
                                    owner_attestation=OWNER_ATTESTATION, reader_contract=ATTRIBUTED_CONTRACT)
        publish_existing(service, conn, enrollment_id=enrollment["enrollment_id"])
    conn.execute("UPDATE conversation_messages SET dataset_id=? WHERE message_id='imessage:5'", (DATASET,))
    conn.commit()
    both, _ = capture(service, "capture-both", [1, 2, 5])
    before = ledger(conn)
    with pytest.raises(PolicyError, match="reconciliation_row_owned_elsewhere"):
        refresh(store, both)
    assert ledger(conn) == before


def test_F12_an_enrollment_of_another_lane_refuses(store):
    service, conn, _ = store
    conn.execute("INSERT INTO user_ingestion_sources VALUES('lane-dataset','imessage',1,NULL)")
    conn.commit()
    with owner():
        desc = service.describe_snapshot(conn, snapshot_id="canary")
        service.enroll(conn, snapshot_id="canary", dataset_id="lane-dataset", snapshot_sha256=desc["snapshot_sha256"],
                       owner_attestation=OWNER_ATTESTATION)
    name, _ = capture(service, "capture-b", [1, 2])
    with pytest.raises(PolicyError, match="reconciliation_lane_required"):
        refresh(store, name, dataset_id="lane-dataset")


def test_F12_a_capture_changed_after_the_links_were_written_rolls_back(store, monkeypatch):
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [1, 2])
    desc = describe(service, conn, name)
    start, end = recent_window()
    real, calls = service._snapshot, []

    def changed(*args, **kwargs):
        calls.append(True)
        descriptor, data = real(*args, **kwargs)
        # The first read is the check before the transaction; the second is the re-hash after the
        # links and the aged deletions were written. Only that one sees a capture that moved.
        return ({**descriptor, "snapshot_sha256": "0" * 64} if len(calls) == 2 else descriptor), data
    monkeypatch.setattr(service, "_snapshot", changed)
    before, marker = ledger(conn), service.marker.read_bytes()
    with owner(), pytest.raises(PolicyError, match="ingest_snapshot_changed"):
        refresh_existing(service, conn, dataset_id=DATASET, snapshot_id=name, snapshot_sha256=desc["snapshot_sha256"],
                         owner_attestation=OWNER_ATTESTATION, window_start_us=start, window_end_us=end)
    assert len(calls) == 2
    assert ledger(conn) == before and service.marker.read_bytes() == marker and proven(store, "imessage:1")


# -- F13: dry run ---------------------------------------------------------------------------------

def test_F13_a_dry_run_reports_and_writes_nothing(ingest_fixture):
    store = make_store(ingest_fixture, offsets=AGED)
    service, conn, _ = store
    publish(store)
    name, data = capture(service, "capture-b", [2, 3], offsets=AGED)
    add_canonical(conn, data)
    before, marker = ledger(conn), service.marker.read_bytes()
    counts = refresh(store, name, dry_run=True)
    assert counts == {"dropped_aged": 1, "dry_run": 1, "linked_new": 1, "reproven": 1}
    assert ledger(conn) == before and service.marker.read_bytes() == marker
    assert (service.root / "capture-a.db").exists() and proven(store, "imessage:1")


# -- F14: the capture reach ----------------------------------------------------------------------

def test_F14_a_deleted_link_never_returns_through_a_past_window_or_a_clock_set_back(ingest_fixture, monkeypatch):
    from topos.permissions_v2 import reconciliation_provenance
    store = make_store(ingest_fixture, offsets=AGED)
    service, conn, _ = store
    publish(store, classifications={"imessage:1": CEILING})
    two, _ = capture(service, "capture-two", [2], offsets=AGED)
    assert refresh(store, two)["dropped_aged"] == 1
    back, _ = capture(service, "capture-back", [1, 2], offsets=AGED)
    before = ledger(conn)
    # A window dated back to the deleted message: refused, so it cannot relink without its ceiling.
    with pytest.raises(PolicyError, match="reconciliation_refresh_window_too_old"):
        refresh(store, back, window_start_us=(T0 - 41 * DAY) * 1_000_000, window_end_us=(T0 - 11 * DAY) * 1_000_000)
    # A recent window with the old message smuggled into the capture: refused too.
    with pytest.raises(PolicyError, match="reconciliation_capture_outside_window"):
        refresh(store, back)
    # A clock set back five days cannot move the reach back: it starts from the last authorization.
    authorized = conn.execute("SELECT authorized_at FROM ingest_provenance_enrollments").fetchone()[0]
    monkeypatch.setattr(reconciliation_provenance, "time", SimpleNamespace(time=lambda: T0 - 5 * DAY))
    with pytest.raises(PolicyError, match="reconciliation_refresh_window_too_old"):
        refresh(store, back, window_start_us=(T0 - 34 * DAY) * 1_000_000, window_end_us=(T0 - 3 * DAY) * 1_000_000)
    assert ledger(conn) == before
    # A refresh that does run under the set-back clock never moves the authorization time back.
    again, _ = capture(service, "capture-again", [2], offsets=AGED)
    refresh(store, again, window_start_us=(T0 - 30 * DAY) * 1_000_000, window_end_us=(T0 + 60) * 1_000_000)
    assert conn.execute("SELECT authorized_at FROM ingest_provenance_enrollments").fetchone()[0] >= authorized


# -- F8, F9: the owner door -----------------------------------------------------------------------

REFRESH = "/v1/permissions-beta/v2/imessage/refresh"


def body(**changes):
    """A door request over the last 20 days, dated when it is sent."""
    now = datetime.now(timezone.utc)
    return {"dataset_id": DATASET,
            "starts_at": datetime.fromtimestamp(now.timestamp() - 20 * DAY, tz=timezone.utc).isoformat(timespec="microseconds"),
            "ends_at": now.isoformat(timespec="microseconds"), "owner_attestation": OWNER_ATTESTATION, **changes}


@pytest.fixture
def door(monkeypatch):
    from fastapi import FastAPI
    from topos.api.permissions_native_probe import router
    from topos.permissions_v2 import runtime
    calls = []
    monkeypatch.setattr(runtime, "get_runtime", lambda: calls.append(True) or (_ for _ in ()).throw(AssertionError))
    app = FastAPI()
    app.include_router(router)
    return app, calls


@pytest.mark.parametrize("channel", ["local_http", "cp_relay"])
def test_F8_refresh_is_not_a_remote_owner_or_recipient_operation(door, channel):
    from fastapi.testclient import TestClient
    from topos.auth import resolve_request_principal
    from topos.principal import OWNER_APP, Principal
    app, calls = door
    app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_APP, channel, acting_user="owner-1")
    with TestClient(app) as client:
        response = client.post(REFRESH, json=body())
    assert response.status_code == 403 and calls == []


def test_F8_a_wrong_attestation_is_refused_before_anything_runs(door):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, calls = door
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(REFRESH, json=body(owner_attestation="yes"))
    assert response.status_code == 422 and calls == []


def test_F8_one_recovery_or_refresh_at_a_time(door):
    from fastapi.testclient import TestClient
    from topos.api.permissions_native_probe import _RECOVERY_LOCK
    from topos.uds import UDSChannelApp
    app, calls = door
    assert _RECOVERY_LOCK.acquire(blocking=False)
    try:
        with TestClient(UDSChannelApp(app)) as client:
            response = client.post(REFRESH, json=body())
    finally:
        _RECOVERY_LOCK.release()
    assert response.status_code == 409 and response.json()["detail"] == "native_recovery_running" and calls == []


def test_F8_resync_reports_counts_and_never_raises(monkeypatch):
    from contextlib import contextmanager
    from topos.api import permissions_native_probe as door_module
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", "true")
    synced = []

    @contextmanager
    def transaction():
        yield "ledger-connection"
    index = SimpleNamespace(sweep=lambda: 0, rebuild_all=lambda: {"g1": "ready", "g2": "stale"})
    node = SimpleNamespace(protocol=SimpleNamespace(ledger=SimpleNamespace(_transaction=transaction),
                                                    _sync_protection=synced.append),
                           message_search_index=lambda: index)
    assert door_module._resync_search(node) == {"protection_synced": True, "grants": 2, "ready": 1}
    assert synced == ["ledger-connection"]
    node.protocol._sync_protection = lambda _conn: (_ for _ in ()).throw(RuntimeError("closed"))
    assert door_module._resync_search(node) == {"protection_synced": False, "grants": 0, "ready": 0}


def door_over(store, tmp_path, monkeypatch, native_ids):
    """The real route over the real service and a synthetic native database; only the runtime is a stand-in."""
    from contextlib import contextmanager
    from fastapi import FastAPI
    from topos.api.permissions_native_probe import router
    from topos.permissions_v2 import native_imessage_probe as probe, runtime
    service, conn, _ = store
    native = tmp_path / "native-chat.db"
    _, data = capture(service, "native-source", native_ids)
    native.write_bytes(data)
    (service.root / "native-source.db").unlink()
    add_canonical(conn, data)
    actual = probe.probe_native_messages
    monkeypatch.setattr(probe, "probe_native_messages", lambda canonical, **kw: actual(canonical, **kw, _native_path=native))
    synced = []

    @contextmanager
    def ledger_transaction():
        yield "ledger"

    def connect():
        opened = sqlite3.connect(service.resolver.path.as_uri() + "?mode=rw", uri=True, timeout=30)
        opened.row_factory = sqlite3.Row
        return opened
    node = SimpleNamespace(ingestion=lambda: service, ingestion_connection=connect,
                           protocol=SimpleNamespace(ledger=SimpleNamespace(identity=SimpleNamespace(owner_id="owner-1"),
                                                                           _transaction=ledger_transaction),
                                                    _sync_protection=synced.append))
    monkeypatch.setattr(runtime, "get_runtime", lambda: node)
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(router)
    return app, synced


def post(app, payload):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    with TestClient(UDSChannelApp(app)) as client:
        return client.post(REFRESH, json=payload)


def test_F9_the_owner_door_refreshes_end_to_end(store, tmp_path, monkeypatch):
    publish(store)
    app, synced = door_over(store, tmp_path, monkeypatch, [1, 2, 3])
    response = post(app, body())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["authority_created"] is True and payload["counts"]["canonical_exact_match"] == 3
    assert payload["refresh"] == {"linked_new": 1, "previous_capture_removed": 1, "reproven": 2}
    assert payload["search"] == {"protection_synced": True, "grants": 0, "ready": 0} and synced == ["ledger"]
    assert all(proven(store, f"imessage:{i}") for i in (1, 2, 3))
    assert response.headers["cache-control"] == "no-store"


def test_F9_a_failed_refresh_changes_nothing_and_leaves_no_capture(store, tmp_path, monkeypatch):
    from topos.permissions_v2 import reconciliation_provenance
    service, conn, _ = store
    publish(store)
    app, synced = door_over(store, tmp_path, monkeypatch, [1, 2, 3])
    real, calls = reconciliation_provenance.compare_existing_message, []

    def flaky(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise PolicyError("reconciliation_content_mismatch")
        return real(*args, **kwargs)
    monkeypatch.setattr(reconciliation_provenance, "compare_existing_message", flaky)
    before, files = ledger(conn), sorted(path.name for path in service.root.iterdir())
    response = post(app, body())
    assert response.status_code == 503 and response.json()["detail"] == "reconciliation_content_mismatch"
    assert ledger(conn) == before and synced == []
    assert sorted(path.name for path in service.root.iterdir()) == files
    assert proven(store, "imessage:1") and not proven(store, "imessage:3")


def test_F9_a_dry_run_through_the_door_writes_nothing_and_keeps_no_capture(store, tmp_path, monkeypatch):
    service, conn, _ = store
    publish(store)
    app, synced = door_over(store, tmp_path, monkeypatch, [1, 2, 3])
    before, files = ledger(conn), sorted(path.name for path in service.root.iterdir())
    response = post(app, body(dry_run=True))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["authority_created"] is False and "search" not in payload
    assert payload["refresh"] == {"dry_run": 1, "linked_new": 1, "reproven": 2}
    assert ledger(conn) == before and synced == [] and sorted(path.name for path in service.root.iterdir()) == files


def test_F9_the_door_passes_its_window_to_the_coverage_guard(store, tmp_path, monkeypatch):
    service, conn, _ = store
    publish(store)
    app, synced = door_over(store, tmp_path, monkeypatch, [1, 2])
    late = datetime.fromtimestamp(event_us(conn, "imessage:2") / 1_000_000 - 3_600, tz=timezone.utc)
    payload = body(starts_at=late.isoformat(timespec="microseconds"))
    before = ledger(conn)
    response = post(app, payload)
    assert response.status_code == 503 and response.json()["detail"] == "reconciliation_refresh_window_uncovered"
    assert ledger(conn) == before and synced == []
    response = post(app, body(starts_at=late.isoformat(timespec="microseconds"), accept_uncovered_links=True))
    assert response.status_code == 200, response.text
    assert response.json()["refresh"]["retired_uncovered"] == 1


def test_F9_the_door_refuses_a_window_past_the_capture_reach(store, tmp_path, monkeypatch):
    service, conn, _ = store
    publish(store)
    app, synced = door_over(store, tmp_path, monkeypatch, [1, 2])
    old = datetime.fromtimestamp(T0 - 45 * DAY, tz=timezone.utc)
    payload = body(starts_at=old.isoformat(timespec="microseconds"),
                   ends_at=datetime.fromtimestamp(T0 - 15 * DAY, tz=timezone.utc).isoformat(timespec="microseconds"))
    before, files = ledger(conn), sorted(path.name for path in service.root.iterdir())
    response = post(app, payload)
    assert response.status_code == 503 and response.json()["detail"] == "reconciliation_refresh_window_too_old"
    assert ledger(conn) == before and synced == [] and sorted(path.name for path in service.root.iterdir()) == files


def test_F8_the_door_answers_only_the_paired_owner(store, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from topos.auth import resolve_request_principal
    from topos.principal import OWNER_APP, Principal
    publish(store)
    app, synced = door_over(store, tmp_path, monkeypatch, [1, 2])
    app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_APP, "uds", acting_user="another-owner")
    with TestClient(app) as client:
        response = client.post(REFRESH, json=body())
    assert response.status_code == 503 and response.json()["detail"] == "owner_binding" and synced == []
