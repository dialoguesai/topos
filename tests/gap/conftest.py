"""Observed protection context for positive non-owner adapter fixtures."""
import sqlite3

import pytest

from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def observed_empty_protection(tmp_path):
    with sqlite3.connect(tmp_path / "empty-protection.db") as conn:
        apply_all_migrations(conn)
        yield conn
    conn.close()
