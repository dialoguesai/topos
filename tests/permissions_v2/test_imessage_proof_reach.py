"""The proof's reach follows the longest grant window, at most 365 days (owner decision, 1 Oct 2026).

  R1  the bounds: coverage C of 30 to 365 days, reach C + 1 day, deletion C + 2 days; anything else refuses
  R2  the coverage is the longest window any grant the node ledger holds active can release; a grant that is
      not active does not count; an unreadable ledger refuses rather than shrink the coverage
  R3  under a 90-day coverage a 60-day-old link the capture does not re-prove is retired; under the floor
      it is deleted
  R4  a window may start up to the reach back, from the later of now and the last authorization, and no further
  R5  a link with a ceiling is never deleted, so a coverage that grows relinks it with its ceiling; a link without
      one is deleted at its refresh's horizon and loses nothing; a v2 enrollment a fixed-bound wheel refreshed is
      not refreshed
  R6  a capture reads its window in slices of at most 31 days, oldest first, contiguous and disjoint; a row on a
      slice boundary is read exactly once
  R7  a slice that hits one read's bound is read again as two halves, down to a day; a failed read keeps no row;
      one capture's reads are bounded; one message past the per-message bound is left out, never split over
  R8  the v3 reader reads a capture of up to twelve reads' worth and checks every row of it; v1 and v2 keep one
      read's bound
  R9  the owner doors take their bounds from the node's grants

Every fixture is synthetic. Captures are dated from the clock as each test runs.
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_imessage_reconciliation import snapshot
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- R2's real ledger
from tests.permissions_v2 import test_reconciliation_refresh as refresh_tests
from tests.permissions_v2.test_reconciliation_refresh import (
    CEILING, DATASET, DAY, DAY_US, add_canonical, capture, ceiling_of, event_us, links, make_store, proven, publish,
    refresh)
from topos.ingestion.owner_snapshot import FORMS_MAX_MESSAGES, MAX_MESSAGES, SnapshotRejected
from topos.permissions_v2 import native_imessage_probe as probe
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.imessage_reconciliation import (
    ATTRIBUTED_CONTRACT, FORMS_CONTRACT, parse_reconciliation_snapshot)
from topos.permissions_v2.reconciliation_provenance import (
    PROOF_COVERAGE_CAP_SECONDS, REFRESH_MINIMUM_COVERAGE_SECONDS, proof_bounds, proof_coverage_seconds)


@pytest.fixture(autouse=True)
def _t0_per_test(monkeypatch):
    """The refresh helpers date everything from their module's T0; read it as each test starts."""
    import time
    monkeypatch.setattr(sys.modules[refresh_tests.__name__], "T0", int(time.time()))


def t0():
    return refresh_tests.T0


# -- R1: the bounds -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("days,expected", [(30, (30, 31, 32)), (90, (90, 91, 92)), (365, (365, 366, 367))])
def test_R1_coverage_reach_and_deletion(days, expected):
    assert proof_bounds(days * DAY) == tuple(value * DAY for value in expected)


@pytest.mark.parametrize("value", [29 * DAY, 30 * DAY - 1, 365 * DAY + 1, 366 * DAY, 0, -DAY, float(90 * DAY),
                                   str(90 * DAY), True, None])
def test_R1_a_coverage_outside_30_to_365_days_refuses(value):
    with pytest.raises(PolicyError, match="reconciliation_coverage_invalid"):
        proof_bounds(value)


def test_R1_the_floor_is_the_fixed_bounds_of_before_and_the_cap_a_year():
    from topos.permissions_v2.reconciliation_provenance import (REFRESH_CAPTURE_REACH_SECONDS,
                                                               REFRESH_DELETE_AFTER_SECONDS)
    assert proof_bounds(REFRESH_MINIMUM_COVERAGE_SECONDS) == (30 * DAY, REFRESH_CAPTURE_REACH_SECONDS,
                                                              REFRESH_DELETE_AFTER_SECONDS)
    assert PROOF_COVERAGE_CAP_SECONDS == 365 * DAY


# -- R2: the coverage the node's grants ask for --------------------------------------------------------------

class GrantLedger:
    """A node ledger stand-in: `_transaction` yields a connection holding these grants, `_authority` answers each
    one's policy as `model_dump()` gives it, or raises what an inactive grant raises."""

    def __init__(self, grants, *, broken=False):
        self.grants, self.broken = grants, broken

    @contextmanager
    def _transaction(self):
        if self.broken:
            raise sqlite3.OperationalError("database is locked")
        db = sqlite3.connect(":memory:")
        try:
            db.execute("CREATE TABLE p2a_grants(grant_id TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO p2a_grants VALUES(?)", [(grant,) for grant in self.grants])
            yield db
        finally:
            db.close()

    def _authority(self, db, grant_id, now):
        policy = self.grants[grant_id]
        if isinstance(policy, PolicyError):
            raise policy
        return None, SimpleNamespace(model_dump=lambda: policy)


def search_policy(days):
    return {"search": {"window": {"max_age_seconds": days * DAY}}, "rules": [{"effect": "permit"}]}


def fact_policy(days):
    return {"uses": [{"purpose": "owner-stated-fact-projection", "event_window": {"kind": "rolling",
                                                                                   "max_age_seconds": days * DAY}}]}


@pytest.mark.parametrize("grants,days", [
    ({}, 30),
    ({"g1": search_policy(14)}, 30),
    ({"g1": search_policy(90)}, 90),
    ({"g1": search_policy(30), "g2": fact_policy(120), "g3": search_policy(90)}, 120),
    ({"g1": search_policy(90), "g2": PolicyError("grant_inactive"), "g3": PolicyError("policy_time")}, 90),
    ({"g1": search_policy(400)}, 365),
    # Only a positive integer is a window: a string, a float, a flag or a negative number never widens the coverage.
    ({"g1": {"search": {"window": {"max_age_seconds": str(90 * DAY)}}}}, 30),
    ({"g1": {"search": {"window": {"max_age_seconds": float(90 * DAY)}}}}, 30),
    ({"g1": {"search": {"window": {"max_age_seconds": True}}}, "g2": search_policy(40)}, 40),
    ({"g1": {"search": {"window": {"max_age_seconds": -90 * DAY}}}}, 30),
])
def test_R2_the_coverage_is_the_longest_active_grant_window_clamped(grants, days):
    assert proof_coverage_seconds(GrantLedger(grants), 1_700_000_000) == days * DAY


@pytest.mark.parametrize("code", ["policy_unknown", "policy_integrity", "ledger_binding"])
def test_R2_a_grant_whose_policy_cannot_be_read_refuses_rather_than_shrink_the_coverage(code):
    ledger = GrantLedger({"g1": search_policy(90), "g2": PolicyError(code)})
    with pytest.raises(PolicyError, match="reconciliation_coverage_unavailable"):
        proof_coverage_seconds(ledger, 1_700_000_000)


def test_R2_the_real_ledgers_90_day_grant_is_the_coverage(legacy, tmp_path, monkeypatch):
    """The node ledger's own p2c-v3 grant (the corpus's 90-day window), read through `Ledger._authority`."""
    from tests.permissions_v2.test_knowledge_search import node_for
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    now = node.now[0]
    assert proof_coverage_seconds(node.ledger, now) == 90 * DAY
    # Past the grant's validity it is not active: the floor.
    assert proof_coverage_seconds(node.ledger, now + 400 * DAY) == REFRESH_MINIMUM_COVERAGE_SECONDS


def test_R2_a_grant_that_is_not_active_does_not_count():
    ledger = GrantLedger({"g1": search_policy(60), "expired": PolicyError("policy_time")})
    assert proof_coverage_seconds(ledger, 1_700_000_000) == 60 * DAY
    ledger.grants["expired"] = search_policy(200)
    assert proof_coverage_seconds(ledger, 1_700_000_000) == 200 * DAY


@pytest.mark.parametrize("ledger", [GrantLedger({}, broken=True), SimpleNamespace(), None])
def test_R2_an_unreadable_ledger_refuses_rather_than_shrink_the_coverage(ledger):
    with pytest.raises(PolicyError, match="reconciliation_coverage_unavailable"):
        proof_coverage_seconds(ledger, 1_700_000_000)


# -- R3, R4: what a longer coverage keeps and how far back it reaches ---------------------------------------

def window_from(days_back, *, now=None):
    now = t0() if now is None else now
    return {"window_start_us": (now - int(days_back * DAY)) * 1_000_000, "window_end_us": (now + 60) * 1_000_000}


def test_R3_a_longer_coverage_retires_what_the_floor_deletes(ingest_fixture):
    store = make_store(ingest_fixture, offsets={1: 60})
    service, conn, _ = store
    publish(store)
    two, _ = capture(service, "capture-two", [2], offsets={1: 60})
    counts = refresh(store, two, coverage_seconds=90 * DAY, **window_from(90))
    assert counts["retired_unmatched"] == 1 and "dropped_aged" not in counts
    assert [link[0] for link in links(conn)] == ["imessage:1", "imessage:2"] and not proven(store, "imessage:1")


def test_R3_at_the_floor_the_same_link_is_deleted(ingest_fixture):
    store = make_store(ingest_fixture, offsets={1: 60})
    service, conn, _ = store
    publish(store)
    two, _ = capture(service, "capture-two", [2], offsets={1: 60})
    counts = refresh(store, two)
    assert counts["dropped_aged"] == 1 and [link[0] for link in links(conn)] == ["imessage:2"]


def test_R3_a_young_link_a_longer_coverage_does_not_cover_refuses_until_acknowledged(ingest_fixture):
    """The coverage moves the uncovered-window guard too: a 60-day-old link outside a 30-day window is young under
    a 90-day coverage, so leaving it out is a loss the refresh will not take silently."""
    store = make_store(ingest_fixture, offsets={1: 60})
    service, conn, _ = store
    publish(store)
    two, _ = capture(service, "capture-two", [2], offsets={1: 60})
    with pytest.raises(PolicyError, match="reconciliation_refresh_window_uncovered"):
        refresh(store, two, coverage_seconds=90 * DAY)
    assert refresh(store, two, coverage_seconds=90 * DAY, accept_uncovered=True)["retired_uncovered"] == 1


def test_R4_a_window_may_start_up_to_the_reach_back_and_no_further(ingest_fixture):
    store = make_store(ingest_fixture)
    service, conn, _ = store
    publish(store)
    inside, _ = capture(service, "capture-inside", [1, 2])
    beyond, _ = capture(service, "capture-beyond", [1, 2])
    with pytest.raises(PolicyError, match="reconciliation_refresh_window_too_old"):
        refresh(store, beyond, coverage_seconds=90 * DAY, **window_from(91 + 1 / 24))
    with pytest.raises(PolicyError, match="reconciliation_refresh_window_too_old"):
        refresh(store, inside, **window_from(31 + 1 / 24))
    assert refresh(store, inside, coverage_seconds=90 * DAY, **window_from(91 - 1 / 24))["reproven"] == 2


# -- R5: ceilings outlive every reach -----------------------------------------------------------------------

def test_R5_a_ceiling_survives_a_coverage_that_shrinks_and_grows_again(ingest_fixture):
    store = make_store(ingest_fixture, offsets={1: 60})
    service, conn, _ = store
    publish(store, classifications={"imessage:1": CEILING})
    two, _ = capture(service, "capture-two", [2], offsets={1: 60})
    # At the floor the 60-day-old link is past the reach, but it carries a ceiling: retired, never deleted.
    counts = refresh(store, two)
    assert counts["retired_aged"] == 1 and "dropped_aged" not in counts
    # A grant with a 90-day window arrives: the capture reaches the message again and relinks it with its ceiling.
    both, _ = capture(service, "capture-both", [1, 2], offsets={1: 60})
    counts = refresh(store, both, coverage_seconds=90 * DAY, **window_from(90))
    assert counts["relinked_retired"] == 1 and counts["ceiling_carried"] == 1
    assert proven(store, "imessage:1") and ceiling_of(conn, "imessage:1") == CEILING


def test_R5_a_link_without_a_ceiling_is_deleted_at_its_horizon_and_relinks_without_loss(ingest_fixture):
    store = make_store(ingest_fixture, offsets={1: 60})
    service, conn, _ = store
    publish(store)
    two, data = capture(service, "capture-two", [2], offsets={1: 60})
    assert refresh(store, two)["dropped_aged"] == 1
    both, _ = capture(service, "capture-both", [1, 2], offsets={1: 60})
    counts = refresh(store, both, coverage_seconds=90 * DAY, **window_from(90))
    assert counts["linked_new"] == 1 and "ceiling_carried" not in counts
    assert proven(store, "imessage:1") and ceiling_of(conn, "imessage:1") is None


@pytest.mark.parametrize("ceiling,age,kept", [(True, 368, True), (True, 2000, True), (False, 368, False),
                                              (False, 366, True)])
def test_R5_a_ceiling_is_never_deleted_and_a_link_without_one_only_past_its_reach(ingest_fixture, ceiling, age, kept):
    store = make_store(ingest_fixture, offsets={1: age})
    service, conn, _ = store
    publish(store, classifications={"imessage:1": CEILING} if ceiling else None)
    two, _ = capture(service, "capture-two", [2], offsets={1: age})
    counts = refresh(store, two, coverage_seconds=365 * DAY, accept_uncovered=True)
    assert ("imessage:1" in [link[0] for link in links(conn)]) is kept
    assert ("dropped_aged" in counts) is not kept


def test_R5_a_v2_enrollment_a_fixed_bound_wheel_refreshed_is_not_refreshed(ingest_fixture):
    """Revision 2 of a v2 enrollment: only a wheel without v3, with the fixed 31/32-day bounds, made it, and its
    deletions (ceilings included) cannot be seen. The refresh refuses at any coverage; the links stand."""
    store = make_store(ingest_fixture)
    service, conn, _ = store
    publish(store, classifications={"imessage:1": CEILING})
    conn.execute("UPDATE ingest_provenance_enrollments SET revision=2")
    conn.execute("UPDATE ingest_provenance_jobs SET enrollment_revision=2")
    conn.execute("UPDATE ingest_provenance_records SET enrollment_revision=2")
    conn.commit()
    _reseal(service, conn)
    fresh, _ = capture(service, "capture-fresh", [1, 2])
    for coverage in (30 * DAY, 90 * DAY):
        with pytest.raises(PolicyError, match="reconciliation_refresh_legacy_enrollment"):
            refresh(store, fresh, coverage_seconds=coverage)
    assert proven(store, "imessage:1") and proven(store, "imessage:2")


def _reseal(service, conn):
    """Re-pin the store's marker to the ledger rows this test rewrote (as a wheel's own refresh would have)."""
    marker = service._marker_read()
    service._publish_marker({**marker, "revision": marker["revision"] + 1,
                             "authority_digest": service._authority_digest(conn)})


# -- R6, R7: the capture reads its window in slices ---------------------------------------------------------

NOW = datetime(2023, 3, 9, tzinfo=timezone.utc)


def iso(moment):
    return moment.isoformat(timespec="microseconds")


def test_R6_a_window_is_read_in_contiguous_slices_of_at_most_31_days():
    start = NOW - timedelta(days=90)
    slices = probe.capture_slices(iso(start), iso(NOW), NOW)
    assert [round((upper - lower) / DAY_US) for lower, upper in slices] == [31, 31, 28]
    assert all(left[1] == right[0] for left, right in zip(slices, slices[1:]))
    assert slices[0][0] == int(start.timestamp()) * 1_000_000 and slices[-1][1] == int(NOW.timestamp()) * 1_000_000
    assert len(probe.capture_slices(iso(NOW - timedelta(days=372)), iso(NOW), NOW)) == 12


@pytest.mark.parametrize("start,end", [
    (NOW - timedelta(days=372, seconds=1), NOW),
    (NOW, NOW - timedelta(days=1)),
    (NOW - timedelta(days=1), NOW + timedelta(seconds=1)),
])
def test_R6_a_window_past_twelve_slices_reversed_or_in_the_future_refuses(start, end):
    with pytest.raises(PolicyError, match="native_probe_window_invalid"):
        probe.capture_slices(iso(start), iso(end), NOW)


def spread_native(tmp_path, days):
    """A native database of owner-sent rows, ROWID i dated days[i - 1] days before NOW, and canonical rows for them."""
    from tests.permissions_v2.test_native_imessage_probe import canonical_as_ingested
    native, canonical = tmp_path / "native.db", tmp_path / "canonical.db"
    base = (int(NOW.timestamp()) - 978307200) * 1_000_000_000

    def mutate(db):
        db.execute("UPDATE message SET is_from_me=1")
        for rowid, age in enumerate(days, start=1):
            db.execute("UPDATE message SET date=? WHERE ROWID=?", (base - int(age * DAY) * 1_000_000_000, rowid))
    native.write_bytes(snapshot(count=len(days), mutate=mutate))
    with sqlite3.connect(canonical) as db:
        db.execute("CREATE TABLE conversation_messages (message_id TEXT, source_record_id TEXT, source_id TEXT, "
                   "dataset_id TEXT, owner_user_id TEXT, conversation_id TEXT, content TEXT, event_at TEXT, "
                   "is_from_self INTEGER, sender_id TEXT, sender_type TEXT, actor_role TEXT, message_type TEXT, "
                   "metadata_json TEXT)")
    canonical_as_ingested((native, canonical))
    return native, canonical


def capture_over(tmp_path, monkeypatch, native, canonical, start, *, wrap=None):
    root = tmp_path / "private" / "snapshots"
    root.parent.mkdir(mode=0o700, exist_ok=True)
    root.mkdir(mode=0o700, exist_ok=True)
    actual = probe.probe_native_messages

    def read(conn, **kw):
        return actual(conn, **kw, _native_path=native)
    monkeypatch.setattr(probe, "probe_native_messages", wrap(read) if wrap else read)
    with sqlite3.connect(canonical) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        identifier, measured = probe.capture_matching_snapshot(
            conn, snapshot_root=root, dataset_id="dataset-native", owner_id="owner-synthetic",
            starts_at=iso(start), ends_at=iso(NOW), now=NOW)
    data = (root / (identifier + ".db")).read_bytes()
    return parse_reconciliation_snapshot(data, now=NOW, reader_contract=FORMS_CONTRACT), measured["counts"]


def test_R6_a_row_on_a_slice_boundary_is_read_exactly_once(tmp_path, monkeypatch):
    """ROWID 2 is dated exactly where the first 31-day slice ends and the second begins (`date>=?` and `date<?`)."""
    from tests.permissions_v2.test_native_imessage_probe import canonical_as_ingested
    native, canonical = spread_native(tmp_path, [80, 50, 2])
    start = NOW - timedelta(days=90)
    boundary_unix_us = int(start.timestamp()) * 1_000_000 + probe.SLICE_SECONDS * 1_000_000
    with sqlite3.connect(native) as db:
        db.execute("UPDATE message SET date=? WHERE ROWID=2", ((boundary_unix_us - 978307200 * 1_000_000) * 1000,))
    canonical_as_ingested((native, canonical))
    reads = []

    def wrap(read):
        def counted(conn, **kw):
            result = read(conn, **kw)
            reads.append(result["counts"].get("canonical_exact_match", 0))
            return result
        return counted
    parsed, counts = capture_over(tmp_path, monkeypatch, native, canonical, start, wrap=wrap)
    assert [record.message_id for record in parsed] == ["imessage:1", "imessage:2", "imessage:3"]
    assert reads == [1, 1, 1] and counts["canonical_exact_match"] == 3


def test_R6_a_90_day_capture_holds_every_slice_in_one_capture(tmp_path, monkeypatch):
    ages = [80, 70, 45, 32, 31.5, 20, 2]
    native, canonical = spread_native(tmp_path, ages)
    seen = []

    def wrap(read):
        def measured(conn, **kw):
            seen.append((kw["starts_at"], kw["ends_at"]))
            return read(conn, **kw)
        return measured
    parsed, counts = capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=90), wrap=wrap)
    assert [record.message_id for record in parsed] == [f"imessage:{i}" for i in range(1, len(ages) + 1)]
    assert counts["native_capture_reads"] == 3 and counts["canonical_exact_match"] == len(ages)
    assert counts["native_owner_sent"] == len(ages) and len(seen) == 3


def test_R7_a_slice_over_one_reads_bound_is_read_again_as_halves(tmp_path, monkeypatch):
    """A read of more than ten days refuses as over the message bound, after it has matched some rows: the halves
    are read instead, and the refused read's rows are not kept twice."""
    ages = [80, 70, 45, 32, 20, 12, 2]
    native, canonical = spread_native(tmp_path, ages)

    def wrap(read):
        def bounded(conn, **kw):
            result = read(conn, **kw)
            span = datetime.fromisoformat(kw["ends_at"]) - datetime.fromisoformat(kw["starts_at"])
            if span > timedelta(days=10):
                raise PolicyError("native_probe_message_limit")
            return result
        return bounded
    parsed, counts = capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=90), wrap=wrap)
    assert [record.message_id for record in parsed] == [f"imessage:{i}" for i in range(1, len(ages) + 1)]
    assert counts["canonical_exact_match"] == len(ages) and counts["native_capture_split"] >= 3


@pytest.mark.parametrize("code", ["native_probe_text_limit", "native_probe_archive_limit", "native_probe_time_limit"])
def test_R7_every_bound_of_one_read_splits_a_slice(tmp_path, monkeypatch, code):
    native, canonical = spread_native(tmp_path, [20, 2])
    calls = []

    def wrap(read):
        def bounded(conn, **kw):
            calls.append(kw["starts_at"])
            if len(calls) == 1:
                raise PolicyError(code)
            return read(conn, **kw)
        return bounded
    parsed, counts = capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=30), wrap=wrap)
    assert len(parsed) == 2 and counts["native_capture_split"] == 1 and counts["native_capture_reads"] == 2


def test_R7_one_message_past_the_per_message_bound_is_left_out_and_never_split_over(tmp_path, monkeypatch):
    """An archived body decoding past 64 KiB: that row is not read (as a `text` column that long is not), and the
    read goes on. Before, it refused the read as `native_probe_text_limit`, which no split could help."""
    native, canonical = spread_native(tmp_path, [20, 10, 2])
    with sqlite3.connect(native) as db:
        db.execute("UPDATE message SET text=NULL, attributedBody=x'0102' WHERE ROWID=2")
    real = probe.decode_attributed_text
    monkeypatch.setattr(probe, "decode_attributed_text",
                        lambda raw: "x" * (64 * 1024 + 1) if raw == b"\x01\x02" else real(raw))
    parsed, counts = capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=30))
    assert [record.message_id for record in parsed] == ["imessage:1", "imessage:3"]
    assert counts["native_text_unsupported"] == 1 and "native_capture_split" not in counts


def test_R7_the_reads_count_the_capture_bytes_before_anything_is_written(tmp_path, monkeypatch):
    """The reads refuse once their bodies and archives pass the capture file's bound, before a file exists."""
    native, canonical = spread_native(tmp_path, [20, 2])
    actual = probe.probe_native_messages
    monkeypatch.setattr(probe, "probe_native_messages", lambda conn, **kw: actual(conn, **kw, _native_path=native))
    monkeypatch.setattr(probe, "MAX_SNAPSHOT_BYTES", 10)  # the two bodies alone are more
    slices = probe.capture_slices(iso(NOW - timedelta(days=30)), iso(NOW), NOW)
    with sqlite3.connect(canonical) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        with pytest.raises(PolicyError, match="native_probe_capture_limit"):
            probe._read_slices(conn, slices, dataset_id="dataset-native", owner_id="owner-synthetic", now=NOW,
                               skip=None)
        monkeypatch.setattr(probe, "MAX_SNAPSHOT_BYTES", 10_000)
        captured, _ = probe._read_slices(conn, slices, dataset_id="dataset-native", owner_id="owner-synthetic",
                                         now=NOW, skip=None)
    assert len(captured) == 2


def test_R7_a_day_that_is_still_over_the_bound_refuses_the_capture(tmp_path, monkeypatch):
    native, canonical = spread_native(tmp_path, [20, 2])

    def wrap(read):
        def bounded(conn, **kw):
            raise PolicyError("native_probe_message_limit")
        return bounded
    with pytest.raises(PolicyError, match="native_probe_message_limit"):
        capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=30), wrap=wrap)
    assert list((tmp_path / "private" / "snapshots").iterdir()) == []


@pytest.mark.parametrize("code", ["native_probe_window_invalid", "native_probe_unavailable",
                                  "native_probe_schema_unsupported"])
def test_R7_any_other_refusal_refuses_the_capture_without_a_split(tmp_path, monkeypatch, code):
    native, canonical = spread_native(tmp_path, [20, 2])
    calls = []

    def wrap(read):
        def refused(conn, **kw):
            calls.append(True)
            raise PolicyError(code)
        return refused
    with pytest.raises(PolicyError, match=code):
        capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=30), wrap=wrap)
    assert calls == [True]


def test_R7_one_captures_reads_are_bounded(tmp_path, monkeypatch):
    """However its reads are refused, one capture makes at most `_CAPTURE_READS` reads, within `_CAPTURE_SECONDS`."""
    native, canonical = spread_native(tmp_path, [20, 2])
    # A 30-day slice splits five times before a half is a day or less; four reads are all one capture may make.
    monkeypatch.setattr(probe, "_CAPTURE_READS", 4)

    def wrap(read):
        def bounded(conn, **kw):
            raise PolicyError("native_probe_time_limit")
        return bounded
    with pytest.raises(PolicyError, match="native_probe_capture_limit"):
        capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=30), wrap=wrap)


def test_R7_a_row_read_twice_refuses_the_capture(tmp_path, monkeypatch):
    native, canonical = spread_native(tmp_path, [40, 2])

    def wrap(read):
        def doubled(conn, **kw):
            hook = kw.pop("_on_match")

            def twice(row, chat):
                hook(row, chat)
                if row["ROWID"] == 2:
                    hook(dict(row), chat)
            return read(conn, **kw, _on_match=twice)
        return doubled
    with pytest.raises(PolicyError, match="native_probe_capture_changed"):
        capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=60), wrap=wrap)


# -- R8: the v3 reader reads twelve reads' worth, every row checked ------------------------------------------

def many(count, *, mutate=None):
    def adapt(db):
        db.execute("UPDATE message SET is_from_me=1")
        if mutate:
            mutate(db)
    return snapshot(count=count, mutate=adapt)


def test_R8_v3_reads_a_capture_larger_than_one_read():
    parsed = parse_reconciliation_snapshot(many(MAX_MESSAGES + 500), now=NOW, reader_contract=FORMS_CONTRACT)
    assert len(parsed) == MAX_MESSAGES + 500
    for contract in (ATTRIBUTED_CONTRACT, "imessage-existing-comparison/v1"):
        with pytest.raises(SnapshotRejected):
            parse_reconciliation_snapshot(many(MAX_MESSAGES + 1), now=NOW, reader_contract=contract)


@pytest.mark.parametrize("column,value", [("is_forward", 1), ("quoted_message_guid", "synthetic-quote"),
                                          ("is_spam", 1), ("is_deleted", 1), ("is_system_message", 1)])
def test_R8_a_form_on_a_row_past_one_reads_bound_is_still_refused(column, value):
    kind = "TEXT" if column == "quoted_message_guid" else "INTEGER"

    def mutate(db):
        db.execute(f'ALTER TABLE message ADD COLUMN "{column}" {kind}')
        db.execute(f'UPDATE message SET "{column}"=? WHERE ROWID=?', (value, MAX_MESSAGES + 400))
    with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
        parse_reconciliation_snapshot(many(MAX_MESSAGES + 500, mutate=mutate), now=NOW, reader_contract=FORMS_CONTRACT)


def test_R8_v3_reads_more_text_than_one_read_holds():
    """1,100 rows of 1,000 bytes: past one read's 1 MiB of text, within twelve reads'."""
    body = "x" * 1000
    data = many(1100, mutate=lambda db: db.execute("UPDATE message SET text=?", (body,)))
    assert len(parse_reconciliation_snapshot(data, now=NOW, reader_contract=FORMS_CONTRACT)) == 1100


def test_R8_no_reader_takes_more_than_twelve_reads_worth():
    from topos.ingestion.owner_snapshot import _parse_snapshot
    for slices in (0, 13, 12.0, "12", True):
        with pytest.raises(SnapshotRejected, match="snapshot_size_unsupported"):
            _parse_snapshot(many(2), "dataset-synthetic", now=NOW, attributed=True, slices=slices)


def test_R8_v3_refuses_more_than_twelve_reads_worth():
    with pytest.raises(SnapshotRejected):
        parse_reconciliation_snapshot(many(FORMS_MAX_MESSAGES + 1), now=NOW, reader_contract=FORMS_CONTRACT)


def test_R8_the_capture_refuses_a_file_larger_than_the_reader_reads(tmp_path, monkeypatch):
    native, canonical = spread_native(tmp_path, [20, 2])
    monkeypatch.setattr(probe, "MAX_SNAPSHOT_BYTES", 1024)
    with pytest.raises(PolicyError, match="native_probe_capture_limit"):
        capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=30))
    assert list((tmp_path / "private" / "snapshots").iterdir()) == []


def test_R8_the_capture_refuses_more_rows_than_the_reader_reads(tmp_path, monkeypatch):
    native, canonical = spread_native(tmp_path, [20, 2])
    monkeypatch.setattr(probe, "FORMS_MAX_MESSAGES", 1)
    with pytest.raises(PolicyError, match="native_probe_capture_limit"):
        capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=30))


# -- R9: the owner doors take their bounds from the node's grants --------------------------------------------

def door_with_grants(store, tmp_path, monkeypatch, native_ids, grants, *, offsets=None):
    from topos.permissions_v2 import runtime
    app, synced = refresh_tests.door_over(store, tmp_path, monkeypatch, native_ids, offsets=offsets)
    node = runtime.get_runtime()
    ledger = GrantLedger(grants)
    node.protocol.ledger._transaction = ledger._transaction
    node.protocol.ledger._authority = ledger._authority
    return app, synced


def door_body(days_back, **changes):
    now = datetime.now(timezone.utc)
    return {"dataset_id": DATASET, "starts_at": iso(now - timedelta(days=days_back)), "ends_at": iso(now),
            "owner_attestation": refresh_tests.OWNER_ATTESTATION, **changes}


def test_R9_the_refresh_door_reaches_as_far_as_the_longest_active_grant(store_60, tmp_path, monkeypatch):
    service, conn, _ = store_60
    publish(store_60)
    app, synced = door_with_grants(store_60, tmp_path, monkeypatch, [1, 2], {"g1": search_policy(90)}, offsets=AGE_60)
    response = refresh_tests.post(app, door_body(75))
    assert response.status_code == 200, response.text
    assert response.json()["refresh"]["reproven"] == 2 and synced == ["ledger"]


def test_R9_without_such_a_grant_the_same_window_is_past_the_reach(store_60, tmp_path, monkeypatch):
    publish(store_60)
    app, synced = door_with_grants(store_60, tmp_path, monkeypatch, [1, 2], {"g1": search_policy(14)}, offsets=AGE_60)
    response = refresh_tests.post(app, door_body(75))
    assert response.status_code == 503 and response.json()["detail"] == "reconciliation_refresh_window_too_old"
    assert synced == []


def test_R9_the_door_refuses_when_it_cannot_read_the_grants(store_60, tmp_path, monkeypatch):
    publish(store_60)
    app, synced = door_with_grants(store_60, tmp_path, monkeypatch, [1, 2], {})
    from topos.permissions_v2 import runtime

    @contextmanager
    def broken():
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover
    runtime.get_runtime().protocol.ledger._transaction = broken
    response = refresh_tests.post(app, door_body(20))
    assert response.status_code == 503 and response.json()["detail"] == "reconciliation_coverage_unavailable"
    assert synced == []


def test_R9_the_door_passes_its_coverage_to_the_refresh(store_60, tmp_path, monkeypatch):
    """Under a 90-day grant the 60-day-old link that the 20-day window leaves out is young: refused, not deleted."""
    service, conn, _ = store_60
    publish(store_60)
    app, synced = door_with_grants(store_60, tmp_path, monkeypatch, [2], {"g1": search_policy(90)}, offsets=AGE_60)
    response = refresh_tests.post(app, door_body(20))
    assert response.status_code == 503 and response.json()["detail"] == "reconciliation_refresh_window_uncovered"
    assert "imessage:1" in [link[0] for link in links(conn)]


def test_R9_the_recovery_door_refuses_a_window_past_the_reach(ingest_fixture, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    service, conn, _ = ingest_fixture
    conn.execute("CREATE TABLE IF NOT EXISTS ai_chat_messages(message_id TEXT,content TEXT)")
    app, _ = door_with_grants((service, conn, None), tmp_path, monkeypatch, [1, 2], {"g1": search_policy(14)})
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post("/v1/sharing/imessage/recover", json=door_body(45))
    assert response.status_code == 503 and response.json()["detail"] == "reconciliation_refresh_window_too_old"
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='ingest_provenance_enrollments'").fetchone() is None


AGE_60 = {1: 60}


@pytest.fixture
def store_60(ingest_fixture):
    """An enrollment of ROWID 1 (60 days old) and ROWID 2 (12 days old)."""
    return make_store(ingest_fixture, offsets=AGE_60)


def test_R7_one_captures_reads_share_one_deadline(tmp_path, monkeypatch):
    native, canonical = spread_native(tmp_path, [40, 2])
    monkeypatch.setattr(probe, "_CAPTURE_SECONDS", -1)
    with pytest.raises(PolicyError, match="native_probe_capture_limit"):
        capture_over(tmp_path, monkeypatch, native, canonical, NOW - timedelta(days=60))
