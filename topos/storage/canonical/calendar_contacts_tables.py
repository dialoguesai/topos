"""Calendar + contacts stub tables (Phase 1 — schema only)."""

from __future__ import annotations

import sqlite3


def ensure_stub_tables(conn: sqlite3.Connection) -> None:
    from ..db.migrations import ensure_migrations_applied

    ensure_migrations_applied(conn)
