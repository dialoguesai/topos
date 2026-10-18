from datetime import datetime, timedelta, timezone
import sqlite3
import pytest

from topos.dashboard.measurements import daily_measurements
from topos.storage.db.migrations.dashboard_measurements_v1 import install_measurements


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE documents(doc_id TEXT PRIMARY KEY, source_id TEXT, content TEXT);
        CREATE TABLE signal_objects(object_id TEXT PRIMARY KEY, object_key TEXT,
          object_type TEXT, signal_dimension TEXT, valid_to TEXT);
        INSERT INTO documents VALUES('old', 'test_source', 'baseline');
        INSERT INTO signal_objects VALUES('oldfact', 'fact:synthetic:role', 'fact', 'profile', NULL);
    """)
    install_measurements(conn)
    conn.commit()
    yield conn
    conn.close()


def report(db):
    day = datetime.now(timezone.utc).date()
    return daily_measurements(db, (day-timedelta(days=1)).isoformat(), (day+timedelta(days=1)).isoformat())


def count(db, metric):
    return report(db)["metrics"][metric]["buckets"][-1]["count"]


def test_baseline_and_unmeasured_history(db):
    result = report(db)
    for metric in result["metrics"].values():
        assert metric["buckets"][0]["count"] is None
        assert metric["buckets"][0]["coverage"] == "unavailable"
        assert metric["buckets"][1]["count"] == 0
        assert metric["buckets"][1]["coverage"] == "partial"
    db.execute("INSERT OR REPLACE INTO documents VALUES('old', 'test_source', 'updated')")
    assert count(db, "inbound_records") == 0


def test_retries_updates_replace_delete_reimport_and_rollback(db):
    db.execute("INSERT INTO documents VALUES('fresh', 'test_source', 'new')")
    db.commit()
    for sql in [
        "INSERT OR IGNORE INTO documents VALUES('fresh', 'test_source', 'retry')",
        "UPDATE documents SET content='changed' WHERE doc_id='fresh'",
        "INSERT OR REPLACE INTO documents VALUES('fresh', 'test_source', 'replacement')",
        "DELETE FROM documents WHERE doc_id='fresh'",
        "INSERT INTO documents VALUES('fresh', 'test_source', 'reimport')",
    ]:
        db.execute(sql)
        assert count(db, "inbound_records") == 1
    db.commit()
    db.execute("INSERT INTO documents VALUES('rolledback', 'test_source', 'not committed')")
    db.rollback()
    assert count(db, "inbound_records") == 1
    install_measurements(db)
    assert count(db, "inbound_records") == 1


def test_logical_fact_revisions_and_history(db):
    db.execute("INSERT INTO signal_objects VALUES('fresh', 'fact:synthetic:preference', 'fact', 'profile', NULL)")
    db.execute("UPDATE signal_objects SET valid_to='closed' WHERE object_id='fresh'")
    db.execute("INSERT INTO signal_objects VALUES('revision', 'fact:synthetic:preference', 'fact', 'profile', NULL)")
    db.execute("INSERT INTO signal_objects VALUES('oldrevision', 'fact:synthetic:role', 'fact', 'profile', NULL)")
    db.execute("INSERT INTO signal_objects VALUES('history', 'fact:synthetic:historic', 'fact', 'profile', 'closed')")
    db.execute("INSERT INTO signal_objects VALUES('insight', 'different', 'insight', 'profile', NULL)")
    assert count(db, "new_facts") == 1
    assert report(db)["metrics"]["new_facts"]["buckets"][-1]["sources"] == [{"source_id": "profile", "count": 1}]


def test_erased_receipts_invalidate_coverage_and_rebaseline(db):
    db.execute("INSERT INTO documents VALUES('fresh', 'erase_source', 'new')")
    db.commit()
    db.execute("DELETE FROM dashboard_measurement_items WHERE source_id='erase_source'")
    assert count(db, "inbound_records") is None
    db.rollback()
    assert count(db, "inbound_records") == 1
    db.execute("DELETE FROM documents WHERE source_id='erase_source'")
    db.execute("DELETE FROM dashboard_measurement_items WHERE source_id='erase_source'")
    assert count(db, "inbound_records") is None
    install_measurements(db)
    assert count(db, "inbound_records") == 0
    assert report(db)["metrics"]["inbound_records"]["buckets"][0]["count"] is None
    db.execute("INSERT OR REPLACE INTO documents VALUES('old', 'test_source', 'retry')")
    assert count(db, "inbound_records") == 0
    db.execute("INSERT INTO documents VALUES('fresh', 'erase_source', 'deliberate new import')")
    db.execute("INSERT OR REPLACE INTO documents VALUES('fresh', 'erase_source', 'retry')")
    assert count(db, "inbound_records") == 1


def test_late_table_and_coverage_gap_are_baselined(db):
    db.execute("CREATE TABLE ai_chat_messages(message_id TEXT PRIMARY KEY, source_id TEXT)")
    db.execute("INSERT INTO ai_chat_messages VALUES('missed', 'synthetic_chat')")
    install_measurements(db)
    db.execute("INSERT INTO ai_chat_messages VALUES('measured', 'synthetic_chat')")
    assert count(db, "inbound_records") == 1
    db.execute("DROP TRIGGER dashboard_measure_ai_chat_messages")
    assert count(db, "inbound_records") is None
    db.execute("INSERT INTO ai_chat_messages VALUES('gap', 'synthetic_chat')")
    install_measurements(db)
    assert count(db, "inbound_records") == 1
    db.execute("INSERT OR REPLACE INTO ai_chat_messages VALUES('gap', 'synthetic_chat')")
    assert count(db, "inbound_records") == 1


def test_complete_empty_days_and_future(db):
    db.execute("UPDATE dashboard_measurement_coverage SET measured_since='2020-01-01T12:00:00Z'")
    result = daily_measurements(db, "2020-01-01", "2020-01-05", now=datetime(2020,1,3,12,tzinfo=timezone.utc))
    buckets = result["metrics"]["inbound_records"]["buckets"]
    assert [(b["count"], b["coverage"]) for b in buckets] == [(0,"partial"),(0,"complete"),(0,"partial"),(None,"unavailable")]


def test_old_node_unavailable_and_range_validation():
    with sqlite3.connect(":memory:") as conn:
        assert daily_measurements(conn, "2026-01-01", "2026-01-02") == {"available": False}
        for start, end in [("invalid", "2026-01-02"), ("2026-01-01", "2026-01-01"), ("2020-01-01", "2026-01-01")]:
            with pytest.raises(ValueError):
                daily_measurements(conn, start, end)


def test_migration_registry_and_real_stores():
    from topos.storage.db.migrations import ensure_migrations_applied
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
    from topos.features.facts.store import FactStore
    with sqlite3.connect(":memory:") as conn:
        ensure_migrations_applied(conn)
        store = SQLiteCanonicalStore(conn)
        record = {"doc_id":"synthetic-doc", "source_id":"test_source", "content":"synthetic source text"}
        store.upsert("documents", record)
        store.upsert("documents", {**record, "content":"updated synthetic text"})
        facts = FactStore(conn)
        for value in ["first", "first", "second"]:
            facts.assert_fact(subject_entity_id="synthetic-entity", predicate="occupation", object_value=value,
                              disclosure="owner_only", asserted_by="owner", confidence=.9)
        assert count(conn, "inbound_records") == 1
        assert count(conn, "new_facts") == 1


def test_renaming_an_identity_is_an_update(db):
    db.execute("UPDATE documents SET doc_id='renamed' WHERE doc_id='old'")
    db.execute("INSERT OR REPLACE INTO documents VALUES('renamed', 'test_source', 'retry')")
    assert count(db, "inbound_records") == 0


def test_concurrent_retry_has_one_receipt(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = str(tmp_path / "counters.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE documents(doc_id TEXT PRIMARY KEY, source_id TEXT)")
        install_measurements(conn)
    def write(_):
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT OR REPLACE INTO documents VALUES('same', 'test_source')")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(12)))
    with sqlite3.connect(path) as conn:
        assert count(conn, "inbound_records") == 1
