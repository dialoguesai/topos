"""Cursor reader: ``agent-transcripts`` JSONL first, then ``state.vscdb``, to §2.2 records.

Two on-disk sources (wf1-verdicts-full.txt [transcripts.V8]; DIGEST F3; spike A):

(a) ``~/.cursor/projects/<slug>/agent-transcripts/<composerId>/<composerId>.jsonl``:
    append-only, ``{role, message: {content: [text | tool_use]}}`` plus
    ``{type: "turn_ended", status}``; no timestamps and no ids. Read by byte
    offset. Where a composer has one it is the primary source and its DB bubbles
    are never emitted (the two would be the same conversation under different
    ids). It covered 15/434 composers on the measured machine.

(b) ``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``:
    ``cursorDiskKV`` holds ``composerData:<id>`` (ordered
    ``fullConversationHeadersOnly``, ``createdAt`` ms, ``name``, optional
    ``subagentInfo``) and ``bubbleId:<composerId>:<bubbleId>`` rows (``type``
    1 user / 2 assistant, ``text``, ``thinking``, ``toolFormerData``, ...);
    ``composerHeaders`` (newer, sparse: 23 of 434 composers) adds
    ``workspaceId`` (may be the literal ``empty-window``) and ``isSubagent``.
    The DB is in WAL mode and rewritten in place, so it is opened
    ``file:…?mode=ro`` on the LIVE file, never ``immutable=1`` (which ignores
    the WAL, where the newest turn lives: rev-1 feasibility M7) and never a
    copy. It is reopened only when its WAL-aware signature (max of the main and
    ``-wal`` mtimes, sum of their sizes) changed. The open is one function,
    ``_open_state_db``, so spike P0.14 can revise the method in one place.

Classification follows spike A (P0.12, measured 2026-10-15): a user record is a
bubble of ``type == 1`` listed in its composer's ``fullConversationHeadersOnly``,
in a composer that is not a subagent (``composerHeaders.isSubagent`` or
``composerData.subagentInfo``), without ``toolFormerData``
(``tool_bubble``), ``subagentSpawnTaskToolCallId``, ``isSimulatedMsg`` or
``isNudge`` (``non_keyboard_origin:*``), with non-blank text; ``isSteer`` is
kept (typed mid-run). An assistant record is ``type == 2`` without
``toolFormerData`` (``tool_use``) and not ``isThought`` (``thinking``), with
non-blank text. Timestamps: bubble ``createdAt`` → header ``createdAt`` →
``composerData.createdAt`` → DB mtime, recorded in ``timestamp_source``.

Typed text is what the person wrote, not the wrapping Cursor adds: when a
user text holds ``<user_query>…</user_query>`` the query is the text and the
spans beside it (``<timestamp>``, ``<system_notification>``, …) are counted in
``harness_spans_stripped``; a user text made only of ``CURSOR_HARNESS_TAGS``
spans is dropped ``harness_injected``.

Assistant rows are per reply: consecutive assistant text (lines or bubbles)
joined across the tool calls, tool bubbles and thoughts between them, under
the first one's id and stamp. Any user text (typed or injected), a
``turn_ended`` line or the taint instant ends a reply. A reply still running
when read is left for the next run: in a transcript younger than
``OPEN_REPLY_HOLD_SECONDS`` whose last assistant line ends in a tool call, or
in a composer Cursor marks as generating while the DB is that young.

Cursors (``FileCursorStore``, L4): one per JSONL path (byte offset), one for the
DB file (its signature), one per composer (``offset`` = headers consumed), and a
``covered`` marker per JSONL-served composer so a JSONL deleted between runs
never rewinds that composer to its first bubble.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlparse

from topos.ingestion.parsers.ai_coding_tool.common import Ledger, make_record, scrub_text, taints

if TYPE_CHECKING:  # L4 owns the store; the reader only needs its two methods.
    from topos.ingestion.checkpoints.file_cursors import FileCursorStore

logger = logging.getLogger("topos.ingestion.sources.cursor_reader")

TOOL = "cursor"
SOURCE_ID = "cursor_sessions"
PROJECTS_DIRNAME = "projects"
TRANSCRIPTS_DIRNAME = "agent-transcripts"
STATE_DB_RELATIVE = Path("Library") / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
EMPTY_WINDOW_WORKSPACE = "empty-window"

BUBBLE_USER = 1
BUBBLE_ASSISTANT = 2
ORIGIN_COMPOSER = "composer"
ORIGIN_AGENT_TRANSCRIPT = "agent_transcript"
ORIGIN_ASSISTANT = "assistant"

# --- Spike A (P0.12) data tables -------------------------------------------------
# A type-1 bubble with any of these truthy is not keyboard-typed.
USER_BUBBLE_DROP_FLAGS = (
    ("isSimulatedMsg", "non_keyboard_origin:simulated"),
    ("isNudge", "non_keyboard_origin:nudge"),
    ("subagentSpawnTaskToolCallId", "non_keyboard_origin:subagent_spawn"),
)
# composerData stamps that bound a composer's activity (ms epoch or ISO).
COMPOSER_ACTIVITY_KEYS = ("createdAt", "lastUpdatedAt", "conversationCheckpointLastUpdatedAt")
# ---------------------------------------------------------------------------------

_RECORD_NAMESPACE = uuid.UUID("5d2c7b1e-9a4f-4c3e-8e6b-2f1a0c9d7e55")
SETTLE_SECONDS = 30.0
# A reply still running when read waits this long at most for the rest of its turn (see the docstring).
OPEN_REPLY_HOLD_SECONDS = 3600.0
# Spans Cursor puts around typed text or sends as a user message of its own (measured 20 Oct, counts only:
# transcripts wrap the query in <timestamp> + <user_query>; DB bubbles add <system_notification>).
CURSOR_HARNESS_TAGS = (
    "timestamp",
    "system_notification",
    "image_files",
    "available_subagent_types",
    "available_subagent_models",
    "dynamic_tools",
)
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)
_ANY_SPAN_RE = re.compile(r"<([A-Za-z_][\w-]*)(?:\s[^>]*)?>.*?</\1>", re.DOTALL)
_HARNESS_ALTERNATION = "|".join(re.escape(tag) for tag in CURSOR_HARNESS_TAGS)
_LEADING_HARNESS_SPAN_RE = re.compile(
    r"\A\s*<(" + _HARNESS_ALTERNATION + r")(?:\s[^>]*)?>.*?</\1>", re.DOTALL,
)
_TRAILING_HARNESS_SPAN_RE = re.compile(
    r"(?:\A|\n)[ \t]*<(" + _HARNESS_ALTERNATION + r")(?:\s[^>]*)?>(?:(?!</?\1[\s>]).)*</\1>\s*\Z", re.DOTALL,
)
STATUS_GENERATING = "generating"
TITLE_MAX = 120


@dataclass(frozen=True)
class ReadBatch:
    """One batch of §2.2 records. ``scanned_files`` counts JSONL files plus the DB
    (when present); ``files_touched`` the ones actually read.
    ``composers_jsonl_preferred`` counts DB composers skipped because a JSONL
    served them; ``composers_no_rewind`` counts composers whose JSONL vanished
    and that were aligned to their current bubble count instead of reread."""

    rows: List[Dict[str, Any]]
    ledger: Ledger
    scanned_files: int
    files_touched: int
    newest_created_at: Optional[str]
    composers_jsonl_preferred: int = 0
    composers_no_rewind: int = 0


@dataclass
class _ComposerState:
    composer_id: str
    title: Optional[str] = None
    first_prompt: Optional[str] = None
    project_key: Optional[str] = None
    tainted: bool = False
    turn_index: int = 0
    session_time: Optional[datetime] = None

    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.first_prompt:
            return _title_from_prompt(self.first_prompt)
        return f"Cursor session {self.composer_id[:8]}"


def _default_root() -> Path:
    custom = os.environ.get("CURSOR_HOME", "").strip()
    return Path(custom) if custom else Path.home() / ".cursor"


def _default_state_db(root_given: bool, root: Path) -> Path:
    custom = os.environ.get("CURSOR_STATE_DB", "").strip()
    if custom:
        return Path(custom)
    if root_given:
        # A synthetic root never reaches the real Application Support folder.
        return root / "globalStorage" / "state.vscdb"
    return Path.home() / STATE_DB_RELATIVE


# Spike P0.14 (measured 2026-10-15 on a synthetic WAL DB with a concurrent writer):
# `mode=ro` sees every committed WAL frame (264 reads, 0 stale, 0 busy) and never
# blocks the IDE; `immutable=1` ignores the WAL (3 probes saw 0 of 437+ rows, and
# looks fresh right after a checkpoint); the backup API costs ~13 s and ~4.8 GB of
# I/O per hourly sync at 2.4 GB. Only if the IDE ever holds an exclusive lock:
# clonefile (`cp -c`, 18 ms for 253 MB, O(1) in data) db + -wal into scratch, read
# the clone `mode=ro`, delete it. Never the backup API, never a chunked backup.
STATE_DB_BUSY_TIMEOUT_SECONDS = 2.0
_CLONE_PREFIX = "topos-cursor-state-"


class _CloneConnection(sqlite3.Connection):
    """A read-only connection on a clonefile copy; closing it deletes the copy."""

    clone_dir: Optional[str] = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            scratch, self.clone_dir = self.clone_dir, None
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)


def _is_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _clone_file(src: Path, dst: Path) -> None:
    if sys.platform == "darwin":
        done = subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True, check=False)
        if done.returncode == 0:
            return
    shutil.copy2(src, dst)


def _open_clone(path: Path) -> sqlite3.Connection:
    scratch = tempfile.mkdtemp(prefix=_CLONE_PREFIX)
    try:
        clone = Path(scratch) / path.name
        _clone_file(path, clone)
        wal = Path(str(path) + "-wal")
        if wal.is_file():
            _clone_file(wal, Path(str(clone) + "-wal"))
        conn = sqlite3.connect(
            f"{clone.resolve().as_uri()}?mode=ro", uri=True,
            timeout=STATE_DB_BUSY_TIMEOUT_SECONDS, isolation_level=None, factory=_CloneConnection,
        )
        conn.clone_dir = scratch
        return conn
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def _open_state_db(path: Path) -> sqlite3.Connection:
    """Open the LIVE ``state.vscdb`` read-only, autocommit (every statement is its
    own short read transaction, so the IDE's checkpoints are never held up for
    long). ``PRAGMA journal_mode`` is the one health probe: it is where an
    exclusive lock surfaces (``database is locked``), and then the clonefile
    fallback takes over. All of P0.14 lives in this function."""
    conn = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True,
        timeout=STATE_DB_BUSY_TIMEOUT_SECONDS, isolation_level=None,
    )
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.OperationalError as exc:
        conn.close()
        if not _is_lock_error(exc):
            raise
        logger.info("state.vscdb is exclusively locked; reading a clonefile copy instead")
        return _open_clone(path)
    if mode and str(mode[0]).lower() != "wal":
        logger.debug("state.vscdb journal_mode=%s (expected wal)", mode[0])
    return conn


def _db_signature(path: Path) -> Tuple[float, int]:
    """WAL-aware ``(mtime, size)``: a write that only reached ``-wal`` moves the
    WAL's mtime, not the main file's."""
    stat = path.stat()
    mtime, size = stat.st_mtime, stat.st_size
    wal = Path(str(path) + "-wal")
    try:
        wal_stat = wal.stat()
    except OSError:
        return mtime, size
    if wal_stat.st_size > 0:
        mtime = max(mtime, wal_stat.st_mtime)
        size += wal_stat.st_size
    return mtime, size


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _typed_text(text: str) -> Tuple[str, int]:
    """``(typed text, harness spans removed)``; empty typed text with spans removed means harness only.

    The ``<user_query>`` contents when there are any; else the text less the complete
    ``CURSOR_HARNESS_TAGS`` spans at its start and end. A tag named inline stays: it is the person's.
    """
    queries = _USER_QUERY_RE.findall(text or "")
    if queries:
        outside = _USER_QUERY_RE.sub("", text)
        typed = "\n\n".join(query.strip() for query in queries if query.strip())
        return typed, len(_ANY_SPAN_RE.findall(outside))
    kept = (text or "").strip()
    removed = 0
    while True:
        match = _LEADING_HARNESS_SPAN_RE.match(kept)
        if match is None:
            break
        kept, removed = kept[match.end():].strip(), removed + 1
    while True:
        match = _TRAILING_HARNESS_SPAN_RE.search(kept)
        if match is None:
            break
        kept, removed = kept[:match.start()].strip(), removed + 1
    return kept, removed


@dataclass
class _Reply:
    """An assistant reply being assembled from consecutive text lines or bubbles."""

    record_uuid: str
    created_at: datetime
    timestamp_source: str
    share_excluded: bool
    texts: List[str] = field(default_factory=list)


def _flush_reply(reply: Optional[_Reply], *, state: "_ComposerState", ledger: Ledger, origin: str,
                 secrets: Sequence[str], rows: List[Dict[str, Any]]) -> None:
    if reply is None:
        return
    record = _emit(
        state=state, ledger=ledger, record_uuid=reply.record_uuid, role="assistant", text="\n\n".join(reply.texts),
        created_at=reply.created_at, timestamp_source=reply.timestamp_source, origin=origin,
        share_excluded=reply.share_excluded, secrets=secrets,
    )
    if record is not None:
        rows.append(record)


def _title_from_prompt(text: str) -> str:
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if len(first_line) > TITLE_MAX:
        first_line = first_line[: TITLE_MAX - 1].rstrip() + "…"
    return first_line


def _decode_json(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _composer_key(db: Path, composer_id: str) -> str:
    return f"{db}::composer::{composer_id}"


def _covered_key(db: Path, composer_id: str) -> str:
    return f"{db}::jsonl_covered::{composer_id}"


def _within(path: Path, root: Path) -> bool:
    """Whether ``path`` lies under ``root`` once both are resolved (security re-check N4): a hook's
    ``transcript_path`` may name only the tool's own files, never another file on the disk."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _iter_transcripts(root: Path) -> List[Path]:
    projects = root / PROJECTS_DIRNAME
    if not projects.is_dir():
        return []
    found: List[Path] = []
    for path in sorted(projects.glob(f"*/{TRANSCRIPTS_DIRNAME}/*/*.jsonl")):
        if path.is_file() and path.stem == path.parent.name:
            found.append(path)
    return found


def _iter_complete_lines(path: Path, start: int, *, settled: bool) -> Iterator[Tuple[int, int, Optional[Dict[str, Any]]]]:
    """Yield ``(start_offset, end_offset, parsed)`` per complete line."""
    with path.open("rb") as handle:
        handle.seek(start)
        position = start
        for raw in handle:
            begin = position
            position += len(raw)
            if not raw.endswith(b"\n") and not settled:
                break
            stripped = raw.strip()
            if not stripped:
                yield begin, position, {}
                continue
            try:
                parsed = json.loads(stripped)
            except ValueError:
                yield begin, position, None
                continue
            yield begin, position, parsed if isinstance(parsed, dict) else None


class _StateDb:
    """Lazy read-only handle on ``state.vscdb`` shared by one batch."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._failed = False
        self._headers: Optional[Dict[str, Dict[str, Any]]] = None
        self._workspaces: Dict[str, Optional[str]] = {}

    def connection(self) -> Optional[sqlite3.Connection]:
        if self._conn is None and not self._failed:
            if not self.path.is_file():
                self._failed = True
                return None
            try:
                self._conn = _open_state_db(self.path)
            except sqlite3.Error as exc:
                logger.warning("state.vscdb not readable: %s", exc)
                self._failed = True
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def headers(self) -> Dict[str, Dict[str, Any]]:
        if self._headers is not None:
            return self._headers
        self._headers = {}
        conn = self.connection()
        if conn is None:
            return self._headers
        try:
            columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(composerHeaders)").fetchall()}
            if "composerId" not in columns:
                return self._headers
            wanted = ["composerId"] + [
                name if name in columns else "NULL"
                for name in ("workspaceId", "createdAt", "lastUpdatedAt", "isArchived", "isSubagent")
            ]
            for composer_id, workspace_id, created, updated, archived, subagent in conn.execute(
                f"SELECT {', '.join(wanted)} FROM composerHeaders"
            ):
                if composer_id:
                    self._headers[str(composer_id)] = {
                        "workspaceId": workspace_id,
                        "createdAt": created,
                        "lastUpdatedAt": updated,
                        "isArchived": archived,
                        "isSubagent": subagent,
                    }
        except sqlite3.Error as exc:
            logger.debug("composerHeaders not readable: %s", exc)
        return self._headers

    def composer_data(self, composer_id: str) -> Optional[Dict[str, Any]]:
        conn = self.connection()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{composer_id}",)
            ).fetchone()
        except sqlite3.Error:
            return None
        return _decode_json(row[0]) if row else None

    def composer_ids(self) -> List[str]:
        conn = self.connection()
        if conn is None:
            return []
        try:
            # A key range on the UNIQUE(key) autoindex; `LIKE 'prefix%'` is
            # case-insensitive by default and would scan the whole 2.4 GB table.
            rows = conn.execute(
                "SELECT key FROM cursorDiskKV WHERE key >= ? AND key < ?", ("composerData:", "composerData;")
            ).fetchall()
        except sqlite3.Error:
            return []
        return [str(r[0])[len("composerData:"):] for r in rows if r[0]]

    def bubble(self, composer_id: str, bubble_id: str) -> Optional[Dict[str, Any]]:
        conn = self.connection()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?", (f"bubbleId:{composer_id}:{bubble_id}",)
            ).fetchone()
        except sqlite3.Error:
            return None
        return _decode_json(row[0]) if row else None

    def project_key(self, workspace_id: Any) -> Optional[str]:
        """Basename of the workspace folder, from
        ``<User>/workspaceStorage/<id>/workspace.json``; ``empty-window`` and an
        unknown workspace give None. The path itself is never kept."""
        if not isinstance(workspace_id, str) or not workspace_id or workspace_id == EMPTY_WINDOW_WORKSPACE:
            return None
        if workspace_id in self._workspaces:
            return self._workspaces[workspace_id]
        result: Optional[str] = None
        meta = self.path.parent.parent / "workspaceStorage" / workspace_id / "workspace.json"
        try:
            spec = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            spec = None
        if isinstance(spec, dict):
            target = spec.get("folder") or spec.get("workspace")
            if isinstance(target, str) and target:
                local = unquote(urlparse(target).path) if "://" in target else target
                name = Path(local.rstrip("/")).name
                if spec.get("folder") is None and name.endswith(".code-workspace"):
                    name = name[: -len(".code-workspace")]
                result = name or None
        self._workspaces[workspace_id] = result
        return result


def _composer_activity(data: Dict[str, Any], header: Optional[Dict[str, Any]], entries: List[Dict[str, Any]]) -> Optional[datetime]:
    stamps: List[datetime] = []
    for key in COMPOSER_ACTIVITY_KEYS:
        parsed = _parse_time(data.get(key))
        if parsed is not None:
            stamps.append(parsed)
    if header:
        for key in ("createdAt", "lastUpdatedAt"):
            parsed = _parse_time(header.get(key))
            if parsed is not None:
                stamps.append(parsed)
    for entry in entries:
        parsed = _parse_time(entry.get("createdAt")) if isinstance(entry, dict) else None
        if parsed is not None:
            stamps.append(parsed)
    return max(stamps) if stamps else None


def _bubble_time(
    bubble: Dict[str, Any], entry: Dict[str, Any], state: _ComposerState, db_mtime: float
) -> Tuple[datetime, str]:
    stamp = _parse_time(bubble.get("createdAt"))
    if stamp is not None:
        return stamp, "record"
    stamp = _parse_time(entry.get("createdAt"))
    if stamp is not None:
        return stamp, "record"
    if state.session_time is not None:
        return state.session_time, "session"
    return datetime.fromtimestamp(db_mtime, tz=timezone.utc), "file_mtime"


def _tool_former_taints(tool_former: Any) -> bool:
    if not isinstance(tool_former, dict):
        return False
    name = tool_former.get("name") or tool_former.get("tool")
    name = str(name) if isinstance(name, (str, int)) else None
    command: Optional[str] = None
    for key in ("rawArgs", "params", "args"):
        value = tool_former.get(key)
        if isinstance(value, str):
            command = value
            break
        if value is not None:
            command = json.dumps(value, default=str)
            break
    return taints(name, command)


def _emit(
    *,
    state: _ComposerState,
    ledger: Ledger,
    record_uuid: str,
    role: str,
    text: str,
    created_at: datetime,
    timestamp_source: str,
    origin: str,
    share_excluded: bool,
    secrets: Sequence[str],
) -> Optional[Dict[str, Any]]:
    text, counts = scrub_text(text, secrets=secrets)
    for name, count in counts.items():
        ledger.scrubbed(name, count)
    if role == "user" and state.first_prompt is None:
        state.first_prompt = text
    try:
        record = make_record(
            tool=TOOL,
            record_uuid=record_uuid,
            session_id=state.composer_id,
            role=role,
            content=text,
            created_at=created_at,
            turn_index=state.turn_index,
            title=state.display_title(),
            origin=origin,
            share_excluded=share_excluded,
            timestamp_source=timestamp_source,
        )
    except ValueError:
        ledger.drop("empty")
        return None
    if share_excluded:
        ledger.tainted()
    record["project_key"] = state.project_key
    state.turn_index += 1
    ledger.turns_emitted += 1
    return record


# --- (a) agent-transcripts JSONL ---------------------------------------------------

def _transcript_blocks(line: Dict[str, Any]) -> List[Dict[str, Any]]:
    message = line.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _prescan_transcript(path: Path, end: int, state: _ComposerState) -> None:
    for _begin, position, line in _iter_complete_lines(path, 0, settled=True):
        if position > end:
            break
        if not line or "role" not in line:
            continue
        for block in _transcript_blocks(line):
            kind = block.get("type")
            if kind == "tool_use" and taints(
                str(block.get("name")) if block.get("name") is not None else None,
                json.dumps(block.get("input"), default=str) if block.get("input") is not None else None,
            ):
                state.tainted = True
            elif kind == "text" and line.get("role") == "user" and state.first_prompt is None:
                text = block.get("text")
                typed = _typed_text(text)[0] if isinstance(text, str) else ""
                if typed:
                    state.first_prompt = typed


def _open_transcript_reply(entries: Sequence[Tuple[int, int, Optional[Dict[str, Any]]]]) -> Optional[int]:
    """Index of the line that starts a reply whose last assistant line ends in a tool call, else None."""
    start: Optional[int] = None
    awaiting = False
    for index, (_begin, _end, line) in enumerate(entries):
        if not line:
            continue
        if "role" not in line:
            if line.get("type") == "turn_ended":
                start, awaiting = None, False
            continue
        blocks = _transcript_blocks(line)
        if line.get("role") == "user":
            if any(block.get("type") == "text" for block in blocks):
                start, awaiting = None, False
            elif any(block.get("type") == "tool_result" for block in blocks):
                awaiting = True
        elif line.get("role") == "assistant":
            if start is None and any(block.get("type") == "text" and isinstance(block.get("text"), str)
                                     for block in blocks):
                start = index
            awaiting = bool(blocks) and blocks[-1].get("type") == "tool_use"
    return start if awaiting else None


def _read_transcript(
    path: Path,
    *,
    composer_id: str,
    start: int,
    base_count: int,
    ledger: Ledger,
    db: _StateDb,
    skip: Set[str],
    secrets: Sequence[str],
    limit: int,
    settled: bool,
    file_mtime: float,
) -> Tuple[List[Dict[str, Any]], int]:
    state = _ComposerState(composer_id=composer_id, turn_index=base_count)
    data = db.composer_data(composer_id)
    if data is not None and isinstance(data.get("name"), str) and data["name"].strip():
        state.title = data["name"].strip()
    header = db.headers().get(composer_id)
    if header is not None:
        state.project_key = db.project_key(header.get("workspaceId"))
    if state.project_key is not None and state.project_key in skip:
        return [], start  # left for a later run in case the folder is un-skipped
    if start > 0:
        _prescan_transcript(path, start, state)

    created_at = datetime.fromtimestamp(file_mtime, tz=timezone.utc)
    rows: List[Dict[str, Any]] = []
    consumed = start
    entries = list(_iter_complete_lines(path, start, settled=settled))
    if time.time() - file_mtime < OPEN_REPLY_HOLD_SECONDS:
        hold = _open_transcript_reply(entries)
        if hold is not None:
            entries = entries[:hold]
    reply: Optional[_Reply] = None

    def flush() -> None:
        nonlocal reply
        _flush_reply(reply, state=state, ledger=ledger, origin=ORIGIN_AGENT_TRANSCRIPT, secrets=secrets, rows=rows)
        reply = None

    for begin, end, line in entries:
        consumed = end
        if reply is None and len(rows) >= limit:
            consumed = begin
            break
        if line is None:
            ledger.drop("unparseable")
            continue
        if not line:
            continue
        if "role" not in line:
            if line.get("type") == "turn_ended":
                flush()
            continue
        ledger.message_nodes += 1
        role = line.get("role")
        blocks = _transcript_blocks(line)
        if role == "user":
            texts = [b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)]
            for block in blocks:
                if block.get("type") == "tool_result":
                    ledger.drop("tool_result")
            if any(block.get("type") == "text" for block in blocks):
                flush()  # typed or injected, what follows answers it
            text = "\n".join(texts).strip()
            if not text:
                if not texts:
                    continue
                ledger.drop("empty")
                continue
            text, removed = _typed_text(text)
            if not text:
                ledger.drop("harness_injected" if removed else "empty")
                continue
            ledger.harness_spans_stripped += removed
            origin = ORIGIN_AGENT_TRANSCRIPT
            share_excluded = False
        elif role == "assistant":
            # Text on each side of a Topos call in this line is judged on its own side: before it, the
            # session's state so far; after it, excluded.
            segments: List[Tuple[bool, List[str]]] = [(state.tainted, [])]
            for block in blocks:
                kind = block.get("type")
                if kind == "text" and isinstance(block.get("text"), str):
                    segments[-1][1].append(block["text"])
                elif kind == "tool_use":
                    ledger.drop("tool_use")
                    name = block.get("name")
                    payload = block.get("input")
                    if taints(
                        str(name) if name is not None else None,
                        json.dumps(payload, default=str) if payload is not None else None,
                    ):
                        state.tainted = True
                        segments.append((True, []))
                elif kind == "thinking":
                    ledger.drop("thinking")
            first_piece = True
            for index, (excluded, texts) in enumerate(segments):
                text = "\n".join(texts).strip()
                if not text:
                    continue
                if reply is not None and reply.share_excluded != excluded:
                    flush()  # the taint instant falls between this text and the reply so far
                if reply is None:
                    key = f"{TOOL}:{composer_id}:{begin}" + (f":{index}" if index else "")
                    reply = _Reply(record_uuid=str(uuid.uuid5(_RECORD_NAMESPACE, key)),
                                   created_at=created_at, timestamp_source="file_mtime", share_excluded=excluded)
                elif first_piece:
                    ledger.merged_blocks += 1
                first_piece = False
                reply.texts.append(text)
            continue
        else:
            ledger.drop("system")
            continue
        record = _emit(
            state=state, ledger=ledger,
            record_uuid=str(uuid.uuid5(_RECORD_NAMESPACE, f"{TOOL}:{composer_id}:{begin}")),
            role=role, text=text, created_at=created_at, timestamp_source="file_mtime",
            origin=origin, share_excluded=share_excluded, secrets=secrets,
        )
        if record is not None:
            rows.append(record)
    flush()
    title = state.display_title()
    for row in rows:
        row["_metadata"]["conversation_title"] = title
    return rows, consumed


# --- (b) state.vscdb composers ------------------------------------------------------

def _classify_bubble(bubble: Dict[str, Any], ledger: Ledger) -> Optional[Tuple[str, str]]:
    """Return ``(role, text)`` for a human or assistant text bubble, else count
    the drop and return None."""
    kind = bubble.get("type")
    text = bubble.get("text")
    text = text.strip() if isinstance(text, str) else ""
    if kind == BUBBLE_USER:
        if bubble.get("toolFormerData") is not None:
            ledger.drop("tool_bubble")
            return None
        for key, reason in USER_BUBBLE_DROP_FLAGS:
            value = bubble.get(key)
            if value is True or (isinstance(value, str) and value):
                ledger.drop(reason)
                return None
        if not text:
            ledger.drop("empty")
            return None
        typed, removed = _typed_text(text)
        if not typed:
            ledger.drop("harness_injected" if removed else "empty")
            return None
        ledger.harness_spans_stripped += removed
        return "user", typed
    if kind == BUBBLE_ASSISTANT:
        if bubble.get("toolFormerData") is not None:
            ledger.drop("tool_use")
            return None
        if bubble.get("isThought") is True:
            ledger.drop("thinking")
            return None
        if not text:
            ledger.drop("thinking" if bubble.get("thinking") else "empty")
            return None
        return "assistant", text
    ledger.drop(f"other_bubble_type:{kind}")
    return None


def _prescan_composer(db: _StateDb, composer_id: str, entries: List[Dict[str, Any]], state: _ComposerState) -> None:
    for entry in entries:
        bubble_id = entry.get("bubbleId") if isinstance(entry, dict) else None
        if not isinstance(bubble_id, str):
            continue
        bubble = db.bubble(composer_id, bubble_id)
        if bubble is None:
            continue
        if bubble.get("type") == BUBBLE_ASSISTANT and _tool_former_taints(bubble.get("toolFormerData")):
            state.tainted = True
        elif (
            bubble.get("type") == BUBBLE_USER and state.first_prompt is None
            and bubble.get("toolFormerData") is None and isinstance(bubble.get("text"), str)
            and _typed_text(bubble["text"])[0]
        ):
            state.first_prompt = _typed_text(bubble["text"])[0]


def _ends_bubble_reply(bubble: Dict[str, Any]) -> bool:
    """A user bubble that is not a tool bubble ends the reply, typed or injected alike."""
    return bubble.get("type") == BUBBLE_USER and bubble.get("toolFormerData") is None


def _composer_running(data: Dict[str, Any]) -> bool:
    return data.get("status") == STATUS_GENERATING or bool(data.get("generatingBubbleIds"))


def _open_composer_reply(items: Sequence[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]) -> Optional[int]:
    """Index of the bubble that starts the last reply (after the last user bubble), else None."""
    start: Optional[int] = None
    for index, (_entry, bubble) in enumerate(items):
        if bubble is None:
            continue
        if _ends_bubble_reply(bubble):
            start = None
        elif start is None and bubble.get("type") == BUBBLE_ASSISTANT and bubble.get("toolFormerData") is None \
                and bubble.get("isThought") is not True and isinstance(bubble.get("text"), str) and bubble["text"].strip():
            start = index
    return start


def _read_composer(
    db: _StateDb,
    composer_id: str,
    data: Dict[str, Any],
    header: Optional[Dict[str, Any]],
    entries: List[Dict[str, Any]],
    *,
    offset: int,
    base_count: int,
    ledger: Ledger,
    skip: Set[str],
    since_unix: Optional[float],
    secrets: Sequence[str],
    limit: int,
    db_mtime: float,
) -> Tuple[List[Dict[str, Any]], int]:
    state = _ComposerState(composer_id=composer_id, turn_index=base_count)
    if isinstance(data.get("name"), str) and data["name"].strip():
        state.title = data["name"].strip()
    state.session_time = _parse_time(data.get("createdAt"))
    if header is not None:
        state.project_key = db.project_key(header.get("workspaceId"))
    if state.project_key is not None and state.project_key in skip:
        ledger.drop("skipped_folder", len(entries) - offset)
        return [], len(entries)
    if offset > 0:
        _prescan_composer(db, composer_id, entries[:offset], state)

    items: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = []
    for entry in entries[offset:]:
        bubble_id = entry.get("bubbleId") if isinstance(entry, dict) else None
        if not isinstance(bubble_id, str) or not bubble_id:
            items.append((entry, None))
            continue
        bubble = db.bubble(composer_id, bubble_id)
        if bubble is None:
            break  # header written before its bubble: a writer mid-turn; retry next run
        items.append((entry, bubble))
    hold = None
    if _composer_running(data) and time.time() - db_mtime < OPEN_REPLY_HOLD_SECONDS:
        hold = _open_composer_reply(items)
        if hold is not None:
            items = items[:hold]

    rows: List[Dict[str, Any]] = []
    reply: Optional[_Reply] = None

    def flush() -> None:
        nonlocal reply
        _flush_reply(reply, state=state, ledger=ledger, origin=ORIGIN_ASSISTANT, secrets=secrets, rows=rows)
        reply = None

    consumed = offset
    for entry, bubble in items:
        if reply is None and len(rows) >= limit:
            break
        consumed += 1
        if bubble is None:
            continue
        ledger.message_nodes += 1
        if bubble.get("type") == BUBBLE_ASSISTANT and _tool_former_taints(bubble.get("toolFormerData")):
            state.tainted = True
        if _ends_bubble_reply(bubble):
            flush()
        classified = _classify_bubble(bubble, ledger)
        if classified is None:
            continue
        role, text = classified
        created_at, timestamp_source = _bubble_time(bubble, entry, state, db_mtime)
        if since_unix is not None and created_at.timestamp() < since_unix:
            ledger.drop("before_since")
            continue
        if role == "assistant":
            if reply is not None and reply.share_excluded != state.tainted:
                flush()  # the taint instant falls between this text and the reply so far
            if reply is None:
                reply = _Reply(record_uuid=str(entry["bubbleId"]), created_at=created_at,
                               timestamp_source=timestamp_source, share_excluded=state.tainted)
            else:
                ledger.merged_blocks += 1
            reply.texts.append(text)
            continue
        record = _emit(
            state=state, ledger=ledger, record_uuid=str(entry["bubbleId"]), role=role, text=text,
            created_at=created_at, timestamp_source=timestamp_source, origin=ORIGIN_COMPOSER,
            share_excluded=False, secrets=secrets,
        )
        if record is not None:
            rows.append(record)
    flush()
    title = state.display_title()
    for row in rows:
        row["_metadata"]["conversation_title"] = title
    return rows, consumed


_NO_ACTIVITY = datetime.min.replace(tzinfo=timezone.utc)


def _activity_stamp(activity: datetime) -> float:
    """Cursor mtime for a composer: 0.0 when it carries no timestamp at all."""
    return 0.0 if activity == _NO_ACTIVITY else activity.timestamp()


def _entries_of(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries = data.get("fullConversationHeadersOnly")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _is_subagent(data: Dict[str, Any], header: Optional[Dict[str, Any]]) -> bool:
    if data.get("subagentInfo") is not None:
        return True
    if header is not None:
        flag = header.get("isSubagent")
        return flag is True or (isinstance(flag, (int, float)) and int(flag) == 1) or flag in ("1", "true")
    return False


def read_cursor_batch(
    *,
    root_path: Optional[Path] = None,
    cursors: "FileCursorStore",
    batch_size: int = 2000,
    since_unix: Optional[float] = None,
    session_path: Optional[str] = None,
    skip_folders: Sequence[str] = (),
    secrets: Sequence[str] = (),
    state_db_path: Optional[Path] = None,
) -> ReadBatch:
    """Read Cursor history: every ``agent-transcripts`` JSONL under ``root_path``
    (default ``~/.cursor``) from its byte cursor, then the composers of
    ``state_db_path`` (default Cursor's live ``state.vscdb``) that no JSONL
    serves, from their per-composer cursors, only when the DB's WAL-aware
    signature changed. Returns up to ``batch_size`` §2.2 records.

    ``session_path`` ending in ``.jsonl`` reads exactly that transcript (and
    not the DB); any other value restricts the DB read to that composer id.
    """
    root = Path(root_path) if root_path is not None else _default_root()
    db_path = Path(state_db_path) if state_db_path is not None else _default_state_db(root_path is not None, root)
    ledger = Ledger()
    rows: List[Dict[str, Any]] = []
    skip = {str(name) for name in skip_folders}
    scanned = touched = 0
    jsonl_preferred = no_rewind = 0
    now = time.time()
    db = _StateDb(db_path)
    only_jsonl = bool(session_path) and str(session_path).endswith(".jsonl")
    only_composer = None if (not session_path or only_jsonl) else str(session_path)

    try:
        # (a) append-only transcripts
        if only_jsonl:
            transcripts = [Path(session_path)] if _within(Path(session_path), root) else []
            if not transcripts:
                ledger.drop("outside_root")  # a hook's transcript_path names only the tool's own files
        else:
            transcripts = [] if only_composer else _iter_transcripts(root)
        served: Set[str] = set()
        for path in transcripts:
            if len(rows) >= batch_size:
                break
            if not path.is_file():
                continue
            scanned += 1
            composer_id = path.stem
            served.add(composer_id)
            stat = path.stat()
            if since_unix is not None and stat.st_mtime < since_unix:
                continue
            cursor = cursors.get(SOURCE_ID, str(path))
            start = base_count = 0
            if cursor is not None:
                if cursor.offset > stat.st_size:
                    logger.info("agent transcript shrank below its cursor; rereading from 0")
                else:
                    if cursor.offset >= stat.st_size and cursor.size == stat.st_size and cursor.mtime == stat.st_mtime:
                        continue
                    start, base_count = cursor.offset, cursor.record_count
            ledger.conversations_seen += 1
            file_rows, consumed = _read_transcript(
                path, composer_id=composer_id, start=start, base_count=base_count, ledger=ledger, db=db,
                skip=skip, secrets=secrets, limit=batch_size - len(rows),
                settled=(now - stat.st_mtime) >= SETTLE_SECONDS, file_mtime=stat.st_mtime,
            )
            if file_rows or consumed != start or cursor is None:
                # A transcript whose only new lines are a held reply moved nothing: the runner stops asking.
                touched += 1
            if file_rows:
                ledger.conversations_kept += 1
            rows.extend(file_rows)
            cursors.put(SOURCE_ID, str(path), size=stat.st_size, mtime=stat.st_mtime, offset=consumed,
                        record_count=base_count + len(file_rows))
            if cursors.get(SOURCE_ID, _covered_key(db_path, composer_id)) is None:
                cursors.put(SOURCE_ID, _covered_key(db_path, composer_id), size=0, mtime=0.0, offset=0,
                            record_count=0)

        # (b) the state DB
        if only_jsonl or not db_path.is_file():
            return _batch(rows, ledger, scanned, touched, jsonl_preferred, no_rewind)
        scanned += 1
        db_mtime, db_size = _db_signature(db_path)
        db_cursor = cursors.get(SOURCE_ID, str(db_path))
        if db_cursor is not None and db_cursor.mtime == db_mtime and db_cursor.size == db_size:
            return _batch(rows, ledger, scanned, touched, jsonl_preferred, no_rewind)
        if db.connection() is None:
            return _batch(rows, ledger, scanned, touched, jsonl_preferred, no_rewind)
        # The DB counts as touched only when a composer was READ below: the IDE moves the WAL's mtime for as
        # long as it is open, and the runner loops while files_touched > 0, so a signature change alone must
        # not keep an hourly sync re-reading every composer.
        db_read = False
        headers = db.headers()
        composers: List[Tuple[datetime, str, Dict[str, Any], Optional[Dict[str, Any]], List[Dict[str, Any]]]] = []
        for composer_id in db.composer_ids():
            if only_composer is not None and composer_id != only_composer:
                continue
            data = db.composer_data(composer_id)
            if data is None:
                continue
            header = headers.get(composer_id)
            entries = _entries_of(data)
            activity = _composer_activity(data, header, entries)
            composers.append((activity or _NO_ACTIVITY, composer_id, data, header, entries))
        composers.sort(key=lambda item: (item[0], item[1]))

        complete = True
        for activity, composer_id, data, header, entries in composers:
            if len(rows) >= batch_size:
                complete = False
                break
            if not entries:
                continue
            if _is_subagent(data, header):
                ledger.drop("subagent", len(entries))
                continue
            key = _composer_key(db_path, composer_id)
            cursor = cursors.get(SOURCE_ID, key)
            if composer_id in served:
                # The JSONL is primary; keep the DB cursor aligned so a vanished
                # JSONL resumes here instead of at the first bubble.
                jsonl_preferred += 1
                cursors.put(SOURCE_ID, key, size=len(entries), mtime=_activity_stamp(activity), offset=len(entries),
                            record_count=cursor.record_count if cursor else 0)
                continue
            if cursor is None and cursors.get(SOURCE_ID, _covered_key(db_path, composer_id)) is not None:
                no_rewind += 1
                cursors.put(SOURCE_ID, key, size=len(entries), mtime=_activity_stamp(activity), offset=len(entries),
                            record_count=0)
                continue
            offset = base_count = 0
            if cursor is not None and cursor.offset <= len(entries):
                offset, base_count = cursor.offset, cursor.record_count
            # Change detection (spikes A+B reconciled): the composer's newest
            # timestamp vs the cursor, OR its header count grew. Timestamps alone
            # miss composers whose stamps never move (only 89/431 carry
            # lastUpdatedAt; some bubbles carry no createdAt), so the count is a
            # signal in its own right, not a tie-breaker.
            grew = len(entries) > offset
            newer = cursor is None or _activity_stamp(activity) > cursor.mtime
            if not grew and not newer:
                continue
            if not grew:
                # Stamps moved without a new header (an in-place rewrite of
                # already-read bubbles): nothing new to emit; refresh the stamp.
                cursors.put(SOURCE_ID, key, size=len(entries), mtime=_activity_stamp(activity), offset=offset,
                            record_count=base_count)
                continue
            if since_unix is not None and offset == 0 and activity != _NO_ACTIVITY \
                    and activity.timestamp() < since_unix:
                continue
            ledger.conversations_seen += 1
            composer_rows, consumed = _read_composer(
                db, composer_id, data, header, entries, offset=offset, base_count=base_count, ledger=ledger,
                skip=skip, since_unix=since_unix, secrets=secrets, limit=batch_size - len(rows), db_mtime=db_mtime,
            )
            if composer_rows or consumed != offset:
                db_read = True  # a composer whose only new bubbles are a held reply moved nothing
            if composer_rows:
                ledger.conversations_kept += 1
            rows.extend(composer_rows)
            cursors.put(SOURCE_ID, key, size=len(entries), mtime=_activity_stamp(activity), offset=consumed,
                        record_count=base_count + len(composer_rows))
            if consumed < len(entries):
                complete = False
                if len(rows) >= batch_size:
                    break
        if complete:
            cursors.put(SOURCE_ID, str(db_path), size=db_size, mtime=db_mtime, offset=0, record_count=0)
        if db_read:
            touched += 1
        return _batch(rows, ledger, scanned, touched, jsonl_preferred, no_rewind)
    finally:
        db.close()


def _batch(rows, ledger, scanned, touched, jsonl_preferred, no_rewind) -> ReadBatch:
    return ReadBatch(
        rows=rows,
        ledger=ledger,
        scanned_files=scanned,
        files_touched=touched,
        newest_created_at=max((row["created_at"] for row in rows), default=None),
        composers_jsonl_preferred=jsonl_preferred,
        composers_no_rewind=no_rewind,
    )
