"""Current owner calibration answers; additive, no existing rows changed."""
import sqlite3

MIGRATION_ID = "calibration_answers_v1"


def create_calibration_answers(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS calibration_answers (
        pack_id TEXT NOT NULL, question_id TEXT NOT NULL, value TEXT,
        revision INTEGER NOT NULL, fact_id TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(pack_id, question_id))""")


def apply_calibration_answers_v1_up(conn: sqlite3.Connection) -> None:
    create_calibration_answers(conn)
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()
