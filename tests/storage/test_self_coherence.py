"""The dashboard must measure comparisons, never turn missing evidence into disagreement."""
from datetime import datetime, timezone
import sqlite3

from topos.dashboard.coherence import coherence_series, read_claims

NOW = datetime(2026, 11, 11, 18, tzinfo=timezone.utc)


def claim(id, text="I shipped the blue package.", **kw):
    return dict(check_id=id, record_table="journal_entries", record_id=id,
                speaker_kind="owner", category="did", claim_text=text,
                verdict="no_evidence", created_at="2026-11-10T10:00:00Z",
                checked_at="2026-11-10T10:00:00Z", source_at=f"2026-11-09T{10+int(id):02}:00:00Z",
                **kw)


def latest(rows):
    return coherence_series(rows, "2026-11-09", "2026-11-12", now=NOW)["buckets"][-1]


def test_no_comparisons_is_unknown_not_perfect():
    assert latest([])["score"] is None
    assert latest([claim("1")])["score"] is None


def test_literal_repeats_one_vote_and_coverage():
    rows = [claim(str(i)) for i in range(1, 5)]
    b = latest(rows)
    assert (b["aligned_groups"], b["tension_groups"], b["compared_claims"], b["score"]) == (1, 0, 4, 1.0)
    assert b["status"] == "provisional"
    assert latest(rows + [rows[0]]) == b


def test_distinct_days_numbers_and_order_do_not_merge():
    a = claim("1")
    assert latest([a, {**claim("2"), "source_at": "2026-11-08T12:00:00Z"}])["score"] is None
    assert latest([claim("1", "I shipped 5 blue packages."), claim("2", "I shipped 8 blue packages.")])["score"] is None
    assert latest([claim("1", "I helped Sam."), claim("2", "Sam helped I.")])["score"] is None


def test_identical_import_timestamp_is_not_a_second_observation():
    a = claim("1")
    assert latest([a, {**claim("2"), "source_at": a["source_at"]}])["score"] is None


def test_failures_contacts_dismissals_corrections_and_splits_do_not_vote():
    a = claim("1")
    for change in ({"verdict": "unable"}, {"verdict": "not_checkable"}, {"speaker_kind": "contact"},
                   {"dismissed": True}, {"label": "false"}, {"label": "partly"},
                   {"group_override": "separate"}, {"category": "plan"}, {"source_at": None}):
        assert latest([a, {**claim("2"), **change}])["score"] is None


def test_explicit_negation_is_only_potential_tension_with_same_closed_period():
    reading = {"claims": [{"type": "did_or_made", "subject": "owner", "reported_speech": False,
                            "date_from": "2026-11-08", "date_to": "2026-11-08"}]}
    a = claim("1", "I have shipped the blue package.", reading=reading)
    b = claim("2", "I have not shipped the blue package.", reading=reading)
    point = latest([a, b])
    assert (point["aligned_groups"], point["tension_groups"], point["score"]) == (0, 1, 0.0)
    b["reading"] = {"claims": [{**reading["claims"][0], "date_from": "2026-11-07", "date_to": "2026-11-07"}]}
    assert latest([a, b])["score"] is None
    # Legacy text, unknown period and double negatives are never assigned polarity.
    assert latest([claim("1", a["claim_text"]), claim("2", b["claim_text"])])["score"] is None
    assert latest([a, claim("2", "I have not never shipped the blue package.", reading=reading)])["score"] is None


def test_future_checks_and_window_expiry_never_make_historical_points():
    rows = [claim("1"), claim("2")]
    series = coherence_series(rows, "2026-11-09", "2026-11-13", now=NOW)
    assert [b["score"] for b in series["buckets"]] == [None, 1.0, 1.0, None]
    assert latest([{**r, "checked_at": "2026-11-12T00:00:00Z"} for r in rows])["score"] is None
    future = coherence_series(rows, "2027-01-01", "2027-01-02", now=datetime(2027, 1, 2, tzinfo=timezone.utc))
    assert future["buckets"][0]["score"] is None


def test_timezone_same_day_and_source_erasure():
    from zoneinfo import ZoneInfo
    rows = [{**claim("1"), "source_at": "2026-11-10T23:00:00-06:00"},
            {**claim("2"), "source_at": "2026-11-11T01:00:00-06:00"}]
    assert latest(rows)["score"] == 1.0  # UTC puts both on November 11
    r = coherence_series(rows, "2026-11-09", "2026-11-12", now=NOW, zone=ZoneInfo("America/Chicago"))
    assert r["buckets"][-1]["score"] is None
    with sqlite3.connect(":memory:") as db:
        assert read_claims(db) is None
        db.executescript("""CREATE TABLE truth_checks(check_id, record_table, record_id, speaker_kind,
          category, claim_text, verdict, created_at);
          CREATE TABLE journal_entries(entry_id, entry_at);
          INSERT INTO journal_entries VALUES ('1', '2026-11-09T11:00:00Z');
          INSERT INTO truth_checks VALUES ('1','journal_entries','1','owner','did','hello','no_evidence','2026-11-10T10:00:00Z');""")
        assert len(read_claims(db)) == 1
        db.execute("DELETE FROM journal_entries")
        assert read_claims(db) == []


def test_dashboard_exposes_aggregate_in_same_read_snapshot_without_writing():
    from topos.dashboard.measurements import daily_measurements
    from topos.storage.db.migrations.dashboard_measurements_v1 import install_measurements
    with sqlite3.connect(":memory:") as db:
        db.executescript("""CREATE TABLE journal_entries(entry_id TEXT PRIMARY KEY, entry_at TEXT);
          CREATE TABLE truth_checks(check_id,record_table,record_id,speaker_kind,category,claim_text,verdict,created_at);
          INSERT INTO journal_entries VALUES ('1','2026-11-09 11:00:00'),('2','2026-11-09 12:00:00');
          INSERT INTO truth_checks VALUES ('1','journal_entries','1','owner','did','synthetic statement','no_evidence','2026-11-10T10:00:00Z'),
          ('2','journal_entries','2','owner','did','synthetic statement','no_evidence','2026-11-10T10:00:00Z');""")
        install_measurements(db)
        db.commit()
        before = db.total_changes
        result = daily_measurements(db, "2026-11-09", "2026-11-12", now=NOW, coherence_window_days=90)
        assert result["self_coherence"]["window_days"] == 90
        assert result["self_coherence"]["buckets"][-1]["score"] == 1
        assert "synthetic statement" not in str(result)
        assert db.total_changes == before
        assert not db.in_transaction
        db.execute("DELETE FROM journal_entries WHERE entry_id='2'")
        assert daily_measurements(db, "2026-11-09", "2026-11-12", now=NOW)["self_coherence"]["buckets"][-1]["score"] is None


def test_selected_window_recovers_older_pairs_without_changing_group_rules():
    rows = [{**claim(str(i)), "created_at": "2026-10-01T10:00:00Z",
             "checked_at": "2026-10-01T10:00:00Z", "source_at": f"2026-10-01T{10+i}:00:00Z"} for i in (1, 2)]
    assert latest(rows)["score"] is None
    result = coherence_series(rows, "2026-11-11", "2026-11-12", now=NOW, window_days=90)
    assert result["window_days"] == 90
    assert result["buckets"][0]["score"] == 1
    rows[1]["source_at"] = "2026-10-02T12:00:00Z"
    assert coherence_series(rows, "2026-11-11", "2026-11-12", now=NOW, window_days=90)["buckets"][0]["score"] is None


def test_invalid_window_rejected_even_when_storage_unavailable():
    import pytest
    from topos.dashboard.measurements import daily_measurements
    for value in (None, True, 0, 29, 366, 60.5, "90"):
        with pytest.raises(ValueError):
            coherence_series([], "2026-11-11", "2026-11-12", window_days=value)
        with pytest.raises(ValueError):
            daily_measurements(None, "2026-11-11", "2026-11-12", coherence_window_days=value)


def test_canonical_naive_journal_timestamp_uses_the_same_clock_as_calibrate():
    # SQLite stores legacy instants without offsets. They are not missing dates.
    from topos.query.truth_checks import instant
    from topos.dashboard.coherence import _stamp
    stamp = "2026-11-09 11:00:00"
    assert _stamp(stamp) == instant(stamp)
    for invalid in (None, "", "2026-11-09", "not a time"):
        assert _stamp(invalid) is None
    rows = [{**claim(str(i)), "source_at": f"2026-11-09 {10+i}:00:00"} for i in (1, 2)]
    assert latest(rows)["score"] == 1
