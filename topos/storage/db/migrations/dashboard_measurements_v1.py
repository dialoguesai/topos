"""Durable first-observation counters; existing identities are an unmeasured baseline.

Counts canonical data records, not files, network bytes or parent/container rows.
The identity ledger survives source-row pruning so replay cannot inflate totals.
Triggers participate in the writer's transaction, including native/direct writers.
No payloads are copied. Fact identity is its logical object_key, across revisions.
"""
import sqlite3

MIGRATION_ID = "dashboard_measurements_v1"
# Static identifiers only. Contacts/parents/segments are intentionally excluded:
# they fan out from source data; transcript sessions are one record each.
INBOUND_TABLES = {
    "ai_chat_messages": "message_id",
    "conversation_messages": "message_id",
    "activity_events": "event_id",
    "calendar_events": "event_id",
    "journal_entries": "entry_id",
    "profile_records": "record_id",
    "financial_transactions": "transaction_id",
    "location_events": "event_id",
    "documents": "doc_id",
    "transcripts": "transcript_id",
}
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def install_measurements(conn: sqlite3.Connection) -> None:
    """Called under the write gate; never commits a caller-owned transaction."""
    conn.execute("""CREATE TABLE IF NOT EXISTS dashboard_measurement_coverage (
        family TEXT PRIMARY KEY, measured_since TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS dashboard_measurement_items (
        metric TEXT NOT NULL, family TEXT NOT NULL, item_key TEXT NOT NULL,
        source_id TEXT NOT NULL, recorded_at TEXT,
        PRIMARY KEY(metric, family, item_key)) WITHOUT ROWID""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_dashboard_measurement_time
        ON dashboard_measurement_items(recorded_at, metric, source_id)
        WHERE recorded_at IS NOT NULL""")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, pk in INBOUND_TABLES.items():
        if table not in tables:
            continue
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not {pk, "source_id"} <= columns:
            continue
        _install(conn, table, "inbound_records", pk,
                 "COALESCE(NULLIF(source_id, ''), 'unknown')", "1")
    if "signal_objects" in tables:
        _install(conn, "signal_objects", "new_facts", "object_key",
                 "signal_dimension", "object_type='fact'", live="valid_to IS NULL")


def _install(conn, table, metric, key, source, predicate, *, live="1"):
    trigger = f"dashboard_measure_{table}"
    if conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN (?,?)", (trigger, trigger + "_update")).fetchone()[0] == 2:
        return
    conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute(f"DROP TRIGGER IF EXISTS {trigger}_update")
    # If a table was rebuilt or a trigger removed, restart that family's coverage.
    # Rows accumulated during a gap are baseline, never fictional arrivals.
    conn.execute(f"""INSERT INTO dashboard_measurement_coverage VALUES (?, {NOW})
        ON CONFLICT(family) DO UPDATE SET measured_since=excluded.measured_since""", (table,))
    conn.execute(f"""INSERT INTO dashboard_measurement_items
        SELECT '{metric}', '{table}', {key}, {source}, NULL FROM {table}
        WHERE {predicate} AND {key} IS NOT NULL
        ON CONFLICT(metric, family, item_key) DO NOTHING""")
    # Use UPSERT, not OR IGNORE: an outer INSERT OR REPLACE must not replace
    # the identity receipt and count the same row again.
    new_source = source.replace("source_id", "NEW.source_id").replace("signal_dimension", "NEW.signal_dimension")
    new_predicate = predicate.replace("object_type", "NEW.object_type")
    new_live = live.replace("valid_to", "NEW.valid_to")
    conn.execute(f"""CREATE TRIGGER {trigger} AFTER INSERT ON {table}
        WHEN {new_predicate} AND NEW.{key} IS NOT NULL
        BEGIN
          INSERT INTO dashboard_measurement_items VALUES (
            '{metric}', '{table}', NEW.{key}, {new_source},
            CASE WHEN {new_live} THEN {NOW} ELSE NULL END)
          ON CONFLICT(metric, family, item_key) DO NOTHING;
        END""")

    # A key rename is an update too. Remember the alias without an arrival,
    # so a later replacement/retry under that identity cannot look new.
    conn.execute(f"""CREATE TRIGGER {trigger}_update AFTER UPDATE ON {table}
        WHEN {new_predicate} AND NEW.{key} IS NOT NULL
        BEGIN
          INSERT INTO dashboard_measurement_items VALUES (
            '{metric}', '{table}', NEW.{key}, {new_source}, NULL)
          ON CONFLICT(metric, family, item_key) DO NOTHING;
        END""")


def apply_dashboard_measurements_v1_up(conn):
    install_measurements(conn)
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()
