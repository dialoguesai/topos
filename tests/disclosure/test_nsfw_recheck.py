"""The stored-score re-check: only rows tagged 1, only the configured classifier's, strictly above the cutoff."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

import pytest

from topos.disclosure import nsfw_recheck
from topos.disclosure.nsfw_recheck import COUNTS, OUTCOMES, outcome, recheck_model_id, recheck_nsfw_tags
from topos.sanitization.nsfw_classifier import DEFAULT_NSFW_CLASSIFIER_THRESHOLD, HEURISTIC_NSFW_SCORE
from topos.storage.db import write_gate

from tests.disclosure.nsfw_recheck_fixture import CLEARED_AT_091, MODEL, OTHER_MODEL, ROWS, build, dump, tags


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    build(connection)
    yield connection
    connection.close()


def _expected():
    tables = {}
    for table, _rid, flag, _score, _model, decided in ROWS:
        counts = tables.setdefault(table, dict.fromkeys(COUNTS, 0))
        if flag == 1:
            counts["flagged"] += 1
            counts[decided] += 1
    return tables


@pytest.mark.parametrize(
    "score, model_id, decided",
    [
        (0.502, MODEL, "below_threshold"),
        (0.91, MODEL, "below_threshold"),           # strict: AT the cutoff is cleared
        (0.9100001, MODEL, "kept_above_threshold"),
        (HEURISTIC_NSFW_SCORE, MODEL, "kept_heuristic"),
        (0.2, OTHER_MODEL, "kept_other_model"),
        (0.2, None, "kept_other_model"),
        (None, MODEL, "kept_no_score"),
        (float("nan"), MODEL, "kept_no_score"),
        ("0.2", MODEL, "kept_no_score"),
        (True, MODEL, "kept_no_score"),
    ],
)
def test_one_rows_outcome(score, model_id, decided):
    assert outcome(score, model_id, model=MODEL, threshold=0.91) == decided


def test_a_heuristic_row_keeps_its_tag_even_at_a_cutoff_above_its_score():
    assert outcome(HEURISTIC_NSFW_SCORE, MODEL, model=MODEL, threshold=0.99) == "kept_heuristic"


def test_a_dry_run_counts_and_writes_nothing(conn):
    before = dump(conn)
    result = recheck_nsfw_tags(conn, threshold=0.91, model=MODEL)
    assert dump(conn) == before
    assert result["dry_run"] is True and result["threshold"] == 0.91 and result["model"] == MODEL
    assert result["comparison"] == "score > threshold"
    assert result["tables"] == _expected()
    assert result["totals"]["flagged"] == sum(1 for row in ROWS if row[2] == 1)
    assert result["totals"]["below_threshold"] == len(CLEARED_AT_091)
    assert result["totals"]["cleared"] == 0 and result["totals"]["not_written"] == 0


def test_a_write_clears_only_flagged_rows_at_or_below_the_cutoff(conn):
    before = dump(conn)
    result = recheck_nsfw_tags(conn, threshold=0.91, model=MODEL, dry_run=False)
    after = dump(conn)

    expected = _expected()
    for table, counts in expected.items():
        counts["cleared"] = counts["below_threshold"]
    assert result["tables"] == expected
    assert result["totals"]["cleared"] == len(CLEARED_AT_091)

    recorded = recheck_model_id(MODEL, 0.91)
    assert recorded == MODEL + "+cutoff-recheck>0.91"
    for table, rid, flag, score, model, _decided in ROWS:
        if (table, rid) in CLEARED_AT_091:
            # Cleared: the tag is 0, the stored score is the classifier's, the model says which rule cleared it.
            assert tags(conn, table, rid) == (0, score, recorded)
        else:
            # Everything else, every column of the row, exactly as it was: kept tags and every row tagged 0.
            assert after[(table, rid)] == before[(table, rid)]


def test_rows_tagged_zero_are_never_written(conn, monkeypatch):
    written = []
    real = nsfw_recheck.upsert_nsfw_fields

    def spy(target, table, record_id, **kwargs):
        written.append((table, record_id))
        return real(target, table, record_id, **kwargs)

    monkeypatch.setattr(nsfw_recheck, "upsert_nsfw_fields", spy)
    # A cutoff that clears every row the classifier scored, so every candidate for a write is written.
    recheck_nsfw_tags(conn, threshold=0.999, model=MODEL, dry_run=False)
    untagged = {(table, rid) for table, rid, flag, *_rest in ROWS if flag != 1}
    assert written and not untagged & set(written)


def test_every_write_goes_through_the_canonical_writer_under_the_write_gate(conn, monkeypatch):
    held = []
    real = nsfw_recheck.upsert_nsfw_fields

    def spy(target, table, record_id, **kwargs):
        held.append(write_gate.db_write_lock()._is_owned())
        assert kwargs["is_nsfw"] is False
        return real(target, table, record_id, **kwargs)

    monkeypatch.setattr(nsfw_recheck, "upsert_nsfw_fields", spy)
    recheck_nsfw_tags(conn, threshold=0.91, model=MODEL, dry_run=False)
    assert held == [True] * len(CLEARED_AT_091)
    assert not write_gate.db_write_lock()._is_owned()
    assert not conn.in_transaction  # every chunk committed, nothing left open


def test_a_large_pass_holds_the_gate_one_chunk_at_a_time(conn, monkeypatch):
    real = nsfw_recheck.batched_writes
    holds = []

    @contextmanager
    def counted(target):
        holds.append(True)
        with real(target):
            yield

    monkeypatch.setattr(nsfw_recheck, "WRITE_CHUNK", 1)
    monkeypatch.setattr(nsfw_recheck, "batched_writes", counted)
    result = recheck_nsfw_tags(conn, threshold=0.91, model=MODEL, dry_run=False)
    assert result["totals"]["cleared"] == len(CLEARED_AT_091) == len(holds)
    assert not conn.in_transaction


def test_a_row_that_changed_after_the_read_is_left_alone(conn, monkeypatch):
    """A re-ingest re-classified a row between the read and the gated write: the newer tag stands."""
    real = nsfw_recheck.batched_writes
    fired = []

    @contextmanager
    def reingest_first(target):
        if not fired:
            fired.append(True)
            target.execute("UPDATE journal_entries SET content_nsfw_score=0.97 WHERE entry_id='j-coinflip'")
            target.commit()
        with real(target):
            yield

    monkeypatch.setattr(nsfw_recheck, "batched_writes", reingest_first)
    result = recheck_nsfw_tags(conn, threshold=0.91, model=MODEL, dry_run=False)
    journal = result["tables"]["journal_entries"]
    assert journal["below_threshold"] == 2 and journal["cleared"] == 1 and journal["not_written"] == 1
    assert tags(conn, "journal_entries", "j-coinflip") == (1, 0.97, MODEL)
    assert tags(conn, "journal_entries", "j-at-cutoff")[0] == 0


def test_a_second_run_finds_nothing_more_to_clear(conn):
    recheck_nsfw_tags(conn, threshold=0.91, model=MODEL, dry_run=False)
    before = dump(conn)
    again = recheck_nsfw_tags(conn, threshold=0.91, model=MODEL, dry_run=False)
    assert dump(conn) == before
    assert again["totals"]["below_threshold"] == 0 and again["totals"]["cleared"] == 0
    assert again["totals"]["flagged"] == sum(1 for row in ROWS if row[2] == 1) - len(CLEARED_AT_091)


def test_a_what_if_cutoff_moves_the_counts_and_not_the_rows(conn):
    before = dump(conn)
    low = recheck_nsfw_tags(conn, threshold=0.5, model=MODEL)
    high = recheck_nsfw_tags(conn, threshold=0.99, model=MODEL)
    assert dump(conn) == before
    assert low["threshold"] == 0.5 and low["totals"]["below_threshold"] == 0
    # 0.99 clears everything the classifier scored, except the heuristic's fixed score.
    assert high["totals"]["kept_heuristic"] == 1
    assert high["totals"]["below_threshold"] == len(CLEARED_AT_091) + 2   # j-above (0.93) and m-high (0.97)


def test_the_cutoff_and_the_model_default_to_the_configured_ones(conn, monkeypatch):
    import topos.config.settings as config

    monkeypatch.setattr(config.settings, "nsfw_classifier_threshold", 0.6)
    monkeypatch.setattr(config.settings, "nsfw_classifier_model", MODEL)
    result = recheck_nsfw_tags(conn)
    assert result["threshold"] == 0.6 and result["model"] == MODEL
    assert result["tables"]["conversation_messages"]["kept_above_threshold"] == 2   # 0.7 and 0.97

    monkeypatch.setattr(config.settings, "nsfw_classifier_threshold", 7)   # out of range -> the default
    assert recheck_nsfw_tags(conn)["threshold"] == DEFAULT_NSFW_CLASSIFIER_THRESHOLD

    # A node configured for another classifier keeps every row this one flagged; only that model's row moves.
    monkeypatch.setattr(config.settings, "nsfw_classifier_model", OTHER_MODEL)
    other = recheck_nsfw_tags(conn)
    assert other["model"] == OTHER_MODEL
    assert other["totals"]["below_threshold"] == 1   # j-other-model, 0.6
    assert other["totals"]["kept_other_model"] == other["totals"]["flagged"] - 1


def test_a_pass_can_be_limited_to_some_tables(conn):
    before = dump(conn)
    result = recheck_nsfw_tags(conn, threshold=0.91, model=MODEL, dry_run=False, tables=["journal_entries"])
    after = dump(conn)
    assert set(result["tables"]) == {"journal_entries"}
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {(table, rid) for table, rid in CLEARED_AT_091 if table == "journal_entries"}


@pytest.mark.parametrize("tables", [[], ["signal_objects"], ["journal_entries", "location_events"]])
def test_a_pass_refuses_tables_it_does_not_tag(conn, tables):
    before = dump(conn)
    with pytest.raises(ValueError):
        recheck_nsfw_tags(conn, threshold=0.91, model=MODEL, dry_run=False, tables=tables)
    assert dump(conn) == before


def test_a_table_that_is_absent_or_untagged_is_skipped(tmp_path):
    connection = sqlite3.connect(tmp_path / "partial.db")
    try:
        build(connection, tables=("journal_entries",))
        connection.execute("CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, content TEXT)")
        result = recheck_nsfw_tags(connection, threshold=0.91, model=MODEL, dry_run=False)
        assert set(result["tables"]) == {"journal_entries"}
    finally:
        connection.close()


def test_the_answer_and_the_log_are_counts_only(conn, caplog):
    caplog.set_level("INFO", logger="topos.disclosure.nsfw_recheck")
    result = recheck_nsfw_tags(conn, threshold=0.91, model=MODEL, dry_run=False)
    text = json.dumps(result) + "\n".join(record.getMessage() for record in caplog.records)
    for _table, rid, _flag, score, _model, _decided in ROWS:
        assert rid not in text
        if score not in (None, 0.91):
            assert repr(score) not in text
    assert "synthetic" not in text
    assert set(result["totals"]) == set(COUNTS) and set(OUTCOMES) <= set(COUNTS)
