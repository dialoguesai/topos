"""Build a synthetic Codex ``state_5.sqlite`` next to a copied fixture root.

``*.sqlite`` is gitignored, so the index is built at test time. Only the
``threads`` columns the reader consults are modelled (``id``, ``title``,
``cwd``, ``rollout_path``); the real table has more.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

THREAD_A = "0a1b2c3d-1111-4aaa-8aaa-aaaaaaaa0001"
THREAD_B = "0a1b2c3d-2222-4bbb-8bbb-bbbbbbbb0002"
TITLE_A = "Retry helper for fetch"
TITLE_B = "Fork: log the attempts"


def build_state_db(root: Path) -> Path:
    """Write ``<root>/state_5.sqlite`` with titles for threads A and B only."""
    path = root / "state_5.sqlite"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, title TEXT, cwd TEXT,"
            " created_at INTEGER, updated_at INTEGER, source TEXT)"
        )
        sessions = root / "sessions" / "2026" / "10" / "15"
        rows = [
            (THREAD_A, str(sessions / f"rollout-2026-10-15T10-00-00-{THREAD_A}.jsonl"), TITLE_A,
             "/Users/someone/dev/alpha-project", 1, 2, "vscode"),
            (THREAD_B, str(sessions / f"rollout-2026-10-15T10-05-00-{THREAD_B}.jsonl"), TITLE_B,
             "/Users/someone/dev/alpha-project", 3, 4, "vscode"),
        ]
        conn.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    return path
