"""Invented AI-coding-tool transcripts and stand-ins for the pieces other lanes own.

Everything here is synthetic: records are built in memory (CONTRACTS.md §2.2), the
reader is a fake with the §2.7b signature, the parser and the ai_chat mapper are fakes
registered for the test only, and the node is a SQLite file under ``tmp_path``.
Nothing reads ``~/.topos``, ``~/.claude``, ``~/.codex`` or ``~/.cursor``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import pytest

from topos.ingestion.parsers.base import NormalizedRecord
from topos.ingestion.validation.base import ValidationResult
from topos.storage.canonical.ai_chat.mapper import CanonicalMapper
from topos.storage.canonical.ai_chat.model import CanonicalAIChatMessage

OWNER = "owner-0000-1111"
TOPOS = "topos-abc123def456"
DATASET = f"{OWNER}:topos:{TOPOS}"
OTHER_DATASET = "someone-else:topos:elsewhere"
TOOLS = ("claude_code", "codex", "cursor")
SOURCE_IDS = {
    "claude_code": "claude_code_sessions",
    "codex": "codex_sessions",
    "cursor": "cursor_sessions",
}
SCHEMA_IDS = {
    "claude_code": "claude_code.transcript.v1",
    "codex": "codex.transcript.v1",
    "cursor": "cursor.transcript.v1",
}
REQUIRED = ("id", "thread_id", "role", "content", "created_at")


def record(
    tool: str,
    uuid: str,
    session: str,
    role: str,
    content: str,
    *,
    when: str = "2026-10-14T10:00:00.000Z",
    turn_index: int = 0,
    title: str = "Build the lantern",
    share_excluded: bool = False,
    project_key: Optional[str] = None,
    origin: str = "keyboard",
) -> Dict[str, Any]:
    """One §2.2 flat record. ``project_key`` rides as a top-level sibling key (L6), never in ``_metadata``."""
    row: Dict[str, Any] = {
        "id": f"{tool}:{uuid}",
        "thread_id": session,
        "role": role,
        "content": content,
        "created_at": when,
        "_metadata": {
            "tool": tool,
            "session_id": session,
            "turn_index": turn_index,
            "conversation_title": title,
            "origin": origin,
            "share_excluded": share_excluded,
            "timestamp_source": "record",
        },
    }
    if project_key is not None:
        # L6's readers: the facet rides at the TOP level of the row, outside the §2.2 record.
        row["project_key"] = project_key
    return row


class FakeLedger:
    """The §2.3 DropLedger shape: counts only."""

    def __init__(self) -> None:
        self.turns_emitted = 0
        self.dropped: Dict[str, int] = {}
        self.scrubbed_counts: Dict[str, int] = {}
        self.tainted_turns = 0
        # L4's shape: a per-session map of sightings, and the number of sessions with any.
        self.secret_sightings: Dict[str, int] = {}
        self.sessions_with_secrets = 0

    def drop(self, reason: str, n: int = 1) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + n

    def scrubbed(self, name: str, n: int = 1) -> None:
        self.scrubbed_counts[name] = self.scrubbed_counts.get(name, 0) + n

    def tainted(self, n: int = 1) -> None:
        self.tainted_turns += n

    def as_dict(self) -> Dict[str, Any]:
        return {
            "turns_emitted": self.turns_emitted,
            "dropped": dict(self.dropped),
            "dropped_total": sum(self.dropped.values()),
            "scrubbed": dict(self.scrubbed_counts),
            "tainted_turns": self.tainted_turns,
            "secret_sightings": dict(self.secret_sightings),
            "sessions_with_secrets": self.sessions_with_secrets,
        }


class FakeReader:
    """``read_<tool>_batch`` with the §2.7b signature; hands out the batches it was given, in order."""

    def __init__(self, *batches: Sequence[Dict[str, Any]], secret_sightings: int = 0,
                 receipt: Optional[Dict[str, Any]] = None,
                 touches: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        self.batches: List[List[Dict[str, Any]]] = [list(b) for b in batches]
        # The real Ledger's ``touches`` (owner ruling 20 Oct 2026), handed out with every batch that has rows.
        self.touches = [dict(t) for t in (touches or ())]
        self.calls: List[Dict[str, Any]] = []
        self.secret_sightings = secret_sightings
        self.receipt = dict(receipt or {})

    def __call__(
        self,
        *,
        root_path: Optional[Path] = None,
        cursors: Any,
        batch_size: int = 2000,
        since_unix: Optional[float] = None,
        session_path: Optional[str] = None,
        skip_folders: Sequence[str] = (),
        secrets: Sequence[str] = (),
        **extra: Any,
    ) -> Any:
        self.calls.append(
            {
                "root_path": root_path,
                "cursors": cursors,
                "batch_size": batch_size,
                "since_unix": since_unix,
                "session_path": session_path,
                "skip_folders": tuple(skip_folders),
                "secrets": tuple(secrets),
                **extra,
            }
        )
        rows = self.batches.pop(0) if self.batches else []
        ledger = FakeLedger()
        ledger.turns_emitted = len(rows)
        if rows:
            ledger.drop("tool_use", 3)
        if rows and self.secret_sightings:
            ledger.sessions_with_secrets = self.secret_sightings
            ledger.secret_sightings = {f"sess-{i}": 1 for i in range(self.secret_sightings)}
            ledger.scrubbed("topos_secret", self.secret_sightings)
        for row in rows:
            if row["_metadata"]["share_excluded"]:
                ledger.tainted()
        ledger.touches = [dict(t) for t in self.touches] if rows else []
        newest = max((str(r["created_at"]) for r in rows), default=None)
        return SimpleNamespace(
            rows=[{**r, "_metadata": dict(r["_metadata"]), **({"_facets": dict(r["_facets"])} if "_facets" in r else {})} for r in rows],
            ledger=ledger,
            scanned_files=1 if rows else 0,
            files_touched=1 if rows else 0,
            newest_created_at=newest,
            **(dict(self.receipt) if rows else {}),
        )


class MemoryCursors:
    """The two ``FileCursorStore`` methods, in memory."""

    def __init__(self) -> None:
        self.rows: Dict[tuple, Dict[str, Any]] = {}

    def get(self, source_id: str, path: str):
        return self.rows.get((source_id, str(path)))

    def put(self, source_id: str, path: str, *, size: int, mtime: float, offset: int, record_count: int) -> None:
        self.rows[(source_id, str(path))] = {
            "size": size, "mtime": mtime, "offset": offset, "record_count": record_count,
        }


@dataclass
class FakeAiCodingToolParser:
    """Validates and parses §2.2 records the way L4's shared parser base will."""

    dataset_id: str
    _schema_id: str = "claude_code.transcript.v1"

    def validate(self, record: Any) -> ValidationResult:
        payload = record.payload
        missing = [key for key in REQUIRED if key not in payload]
        errors = [f"Missing required field: {m}" for m in missing]
        if not missing and not str(payload.get("content") or "").strip():
            errors.append("empty")
        return ValidationResult(is_valid=not errors, errors=errors, metadata={})

    def parse(self, raw: Any) -> NormalizedRecord:
        payload = raw.payload
        sender_type = {"user": "human", "assistant": "assistant", "system": "system"}[payload["role"]]
        normalized = {
            "message_id": payload["id"],
            "dataset_id": self.dataset_id,
            "thread_id": payload["thread_id"],
            "ts": payload["created_at"],
            "sender_type": sender_type,
            "content": payload["content"],
            "_metadata": dict(payload.get("_metadata") or {}),
        }
        return NormalizedRecord(record_id=payload["id"], payload=normalized)

    def schema_id(self) -> str:
        return self._schema_id


class FakeAiCodingToolMapper(CanonicalMapper):
    """The ai_chat-lane mapper ``ai_coding_tool``: conversation id ``<tool>:<session_id>``, never ``chatgpt:``."""

    def map_to_canonical(self, staging_record: Dict[str, Any], source: str = "ai_coding_tool") -> List[CanonicalAIChatMessage]:
        meta = dict(staging_record.get("_metadata") or {})
        message = CanonicalAIChatMessage(
            message_id=staging_record.get("message_id", ""),
            conversation_id=self.extract_conversation_id(staging_record),
            sender_type=staging_record.get("sender_type", ""),
            ts=staging_record.get("ts", ""),
            content=staging_record.get("content") or "",
            metadata_json={**meta, "thread_id": staging_record.get("thread_id"), "original_source": meta.get("tool") or source},
            source_id=staging_record.get("source_id") or source,
        )
        return [message]

    def extract_conversation_id(self, staging_record: Dict[str, Any]) -> str:
        meta = dict(staging_record.get("_metadata") or {})
        tool = meta.get("tool") or "claude_code"
        return f"{tool}:{meta.get('session_id') or staging_record.get('thread_id') or ''}"


def build_node(path: Path, *, user_id: str = OWNER, topos_id: Optional[str] = TOPOS) -> sqlite3.Connection:
    """A migrated node database with the AI-chat tables, the job table and a stored identity."""
    from topos.core.state import set_engine_config_value
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.db.migrations import apply_all_migrations
    from topos.storage.source_settings import ensure_table as ensure_source_settings_table

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    CanonicalTablesManager(conn)
    # The lazy ai_chat tables came after the migrations ran: the always-run step adds the column on the next pass.
    apply_all_migrations(conn)
    ensure_source_settings_table(conn)
    set_engine_config_value(conn, "user_id", user_id)
    if topos_id:
        set_engine_config_value(conn, "topos_id", topos_id)
    return conn


def arm(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, *, reader: Optional[FakeReader] = None,
        post_canonical: Any = "stub") -> FakeReader:
    """Point the process at ``conn`` and install the stand-ins. Returns the reader in use."""
    import topos.core.handlers as hub
    import topos.core.state as state
    import topos.ingestion.ai_tool_sync as lane
    from topos.ingestion.parsers import PARSER_REGISTRY
    from topos.permissions_v2 import switches
    from topos.storage.canonical.ai_chat import mapper as ai_chat_mapper

    import topos.sources.install_service as install_service

    # Integration (16 Oct): L7's hardening pass and owner-key check are real in this tree.
    # Point the node's home at a scratch folder holding an owner key, so `enable` clamps
    # and checks THAT folder — never the person's real ~/.topos (the conftest guard refuses it).
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="topos-ai-tool-home-"))
    topos_dir = home / ".topos"
    topos_dir.mkdir()
    (topos_dir / ".env").write_text("TOPOS_OWNER_KEY=ok_invented_owner_key_for_these_tests_only\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOPOS_ENV_FILE", str(topos_dir / ".env"))
    monkeypatch.setattr(state, "get_db_connection", lambda: conn)
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    # install_service binds the accessor at import; the install row must land in this database.
    monkeypatch.setattr(install_service, "get_db_connection", lambda: conn)
    monkeypatch.setattr(switches, "is_bound", lambda env=None: False)
    from topos.canonicalization.mappers import MAPPER_REGISTRY as LEGACY_MAPPERS

    for schema_id in SCHEMA_IDS.values():
        monkeypatch.setitem(PARSER_REGISTRY, schema_id, FakeAiCodingToolParser)
    monkeypatch.setitem(ai_chat_mapper._MAPPER_REGISTRY, "ai_coding_tool", FakeAiCodingToolMapper)
    # A runtime install builds dynamic mappers for an id it cannot find in the legacy registry and
    # would clobber the fake above; a placeholder there keeps the install from doing so.
    monkeypatch.setitem(LEGACY_MAPPERS, "ai_coding_tool", object)
    # The runner refuses a parser that is not bundled; the fake stands in for L4's bundled class.
    monkeypatch.setattr(lane, "_parser_for", lambda schema_id: FakeAiCodingToolParser)
    reader = reader or FakeReader([])
    monkeypatch.setattr(lane, "_reader_for", lambda tool: reader)
    monkeypatch.setattr(lane, "_cursor_store_for", lambda db: MemoryCursors())
    if post_canonical == "stub":
        monkeypatch.setattr(lane, "_post_canonical", lambda **kw: {"stubbed": True})
    elif post_canonical is not None:
        monkeypatch.setattr(lane, "_post_canonical", post_canonical)
    return reader


def enable(conn: sqlite3.Connection, source_id: str, *, skip: Sequence[str] = (), backfill: str = "all") -> Dict[str, Any]:
    """Enable through the lane's own helper (what the enable door calls)."""
    from topos.ingestion.ai_tool_sync import enable_ai_tool_source

    return enable_ai_tool_source(conn, source_id, skip=list(skip), backfill=backfill)


def chat_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ai_chat_messages)").fetchall()]
    return [dict(zip(cols, row)) for row in conn.execute(f"SELECT {', '.join(cols)} FROM ai_chat_messages ORDER BY message_id").fetchall()]
