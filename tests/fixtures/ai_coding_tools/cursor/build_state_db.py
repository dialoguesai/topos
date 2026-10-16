"""Build a synthetic Cursor ``state.vscdb`` (WAL mode) from ``state_vscdb_seed.json``.

The real file is ``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``;
``build_state_db(user_dir)`` writes ``<user_dir>/globalStorage/state.vscdb`` and the
``<user_dir>/workspaceStorage/<id>/workspace.json`` files the reader maps workspaces
through. Shapes follow wf1-verdicts-full.txt [transcripts.V8]: ``cursorDiskKV``
holds ``composerData:<id>`` (``fullConversationHeadersOnly``, ``createdAt`` int ms,
``name``) and ``bubbleId:<composerId>:<bubbleId>`` rows; ``composerHeaders`` is the
newer table (``workspaceId`` may be the literal ``empty-window``).

``append_bubble`` is the concurrent-writer half of the WAL freshness guard: it adds a
bubble row and rewrites the composer's header list through the caller's connection
(which the test keeps open with ``wal_autocheckpoint=0``, so the write stays in the
``-wal`` file and the main file's mtime does not move).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SEED_PATH = Path(__file__).with_name("state_vscdb_seed.json")

COMPOSER_NORMAL = "c0c0c0c0-0001-4a0a-8a0a-a0a0a0a0a001"
COMPOSER_SUBAGENT_HEADER = "c0c0c0c0-0002-4a0a-8a0a-a0a0a0a0a002"
COMPOSER_EMPTY_WINDOW = "c0c0c0c0-0003-4a0a-8a0a-a0a0a0a0a003"
COMPOSER_JSONL = "c0c0c0c0-0004-4a0a-8a0a-a0a0a0a0a004"
COMPOSER_DRAFT = "c0c0c0c0-0005-4a0a-8a0a-a0a0a0a0a005"
COMPOSER_SUBAGENT_INFO = "c0c0c0c0-0006-4a0a-8a0a-a0a0a0a0a006"
WORKSPACE_BETA = "1f2e3d4c5b6a79880000aaaabbbbcccc"
PROJECT_SLUG = "-Users-someone-dev-beta-project"


def load_seed() -> Dict[str, Any]:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


def iso_to_ms(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(stamp.astimezone(timezone.utc).timestamp() * 1000)


def _header_entry(bubble: Dict[str, Any]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"bubbleId": bubble["bubbleId"], "type": bubble["type"]}
    if bubble.get("headerCreatedAt"):
        entry["createdAt"] = bubble["headerCreatedAt"]
    return entry


def _bubble_row(composer_id: str, bubble: Dict[str, Any]) -> tuple[str, str]:
    body = {k: v for k, v in bubble.items() if k != "headerCreatedAt"}
    return f"bubbleId:{composer_id}:{bubble['bubbleId']}", json.dumps(body)


def _composer_data(composer: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(composer.get("composerData") or {})
    created = iso_to_ms(data.get("createdAt"))
    if created is None:
        data.pop("createdAt", None)
    else:
        data["createdAt"] = created
    data["composerId"] = composer["composerId"]
    data["fullConversationHeadersOnly"] = [_header_entry(b) for b in composer.get("bubbles") or []]
    return data


def build_state_db(user_dir: Path, seed: Optional[Dict[str, Any]] = None) -> Path:
    """Write ``<user_dir>/globalStorage/state.vscdb`` in WAL mode and return its path."""
    seed = seed or load_seed()
    global_dir = user_dir / "globalStorage"
    global_dir.mkdir(parents=True, exist_ok=True)
    for workspace_id, spec in (seed.get("workspaces") or {}).items():
        ws_dir = user_dir / "workspaceStorage" / workspace_id
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "workspace.json").write_text(json.dumps(spec), encoding="utf-8")

    path = global_dir / "state.vscdb"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        conn.execute(
            "CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt TEXT,"
            " lastUpdatedAt TEXT, isArchived INTEGER, isSubagent INTEGER, recency TEXT, checkpointAt TEXT,"
            " value BLOB, subagentTypeName TEXT)"
        )
        conn.execute(
            "INSERT INTO ItemTable VALUES (?, ?)", ("composer.composerHeaders.tableGateEnabled", "true")
        )
        for composer in seed["composers"]:
            cid = composer["composerId"]
            conn.execute(
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                (f"composerData:{cid}", json.dumps(_composer_data(composer))),
            )
            for bubble in (composer.get("bubbles") or []) + (composer.get("orphan_bubbles") or []):
                conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", _bubble_row(cid, bubble))
            header = composer.get("header")
            if header is not None:
                conn.execute(
                    "INSERT INTO composerHeaders (composerId, workspaceId, createdAt, lastUpdatedAt,"
                    " isArchived, isSubagent, subagentTypeName) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        cid,
                        header.get("workspaceId"),
                        header.get("createdAt"),
                        header.get("lastUpdatedAt"),
                        int(header.get("isArchived") or 0),
                        int(header.get("isSubagent") or 0),
                        header.get("subagentTypeName"),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return path


def append_bubble(conn: sqlite3.Connection, composer_id: str, bubble: Dict[str, Any]) -> None:
    """Writer side of the WAL freshness guard: add ``bubble`` to ``composer_id``."""
    key = f"composerData:{composer_id}"
    row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
    raw = row[0]
    data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    data.setdefault("fullConversationHeadersOnly", []).append(_header_entry(bubble))
    conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", _bubble_row(composer_id, bubble))
    conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, json.dumps(data)))
    conn.commit()
