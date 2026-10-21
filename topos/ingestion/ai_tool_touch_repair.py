"""Re-judge stored coding-session replies under the read / system-touch rule (owner ruling 20 Oct 2026).

Before the ruling every Topos touch — ``topos-node status``, a health check, the setup flow — kept the
assistant's later replies for the owner only (``share_excluded``). Now only a READ of Topos does; a SYSTEM
touch is recorded and excludes nothing. Rows already stored keep the old verdict, and Claude Code keeps a
per-session "excluded from" marker in its file cursors, so new replies in such a session would stay excluded
too. This module re-scans the transcripts still on disk and, for each flagged reply:

* clears the flag only when the reply's own session has NO read before it (strictly earlier) under the new
  rule, the reply's own record said it was excluded (``_metadata.share_excluded``), and the transcript is
  still there to prove it — every other row stays as it is (fail closed);
* records every touch it finds in the owner-only touch table;
* moves Claude Code's marker to the first READ (or clears it when the session only had system touches).

A dry run (the default) changes nothing and returns counts only. ``apply`` is the owner's call: it is served
on the owner socket for the owner (never the enrolled agent). Cleared rows get their timeline projection back
and are handed to the enrichment catch-up, which derives any shareable row that has no progress marker.
Cursor sessions are not re-judged here (their rows stay as they are) and say so.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .parsers.ai_coding_tool.common import TOUCH_READ, Ledger, content_blocks

logger = logging.getLogger("topos.ingestion.ai_tool_touch_repair")

SUPPORTED_TOOLS = ("claude_code", "codex")


@dataclass
class _SessionScan:
    earliest_read: Optional[float] = None
    marker_key: Optional[str] = None  # Claude Code: the main transcript's cursor key


@dataclass
class _Scan:
    ledger: Ledger = field(default_factory=Ledger)
    sessions: Dict[str, _SessionScan] = field(default_factory=dict)

    def session(self, session_id: str) -> _SessionScan:
        return self.sessions.setdefault(session_id, _SessionScan())

    def touched(self, tool: str, session_id: str, touch: Tuple[str, str], at: Optional[float],
                anchor: Optional[str]) -> None:
        self.ledger.note_touch(tool=tool, session_id=session_id, touch=touch, at=at, anchor=anchor)
        if touch[0] == TOUCH_READ and at is not None:
            entry = self.session(session_id)
            entry.earliest_read = at if entry.earliest_read is None else min(entry.earliest_read, at)
        elif touch[0] == TOUCH_READ:
            # A read with no time: everything in the session after "the start" is suspect.
            self.session(session_id).earliest_read = float("-inf")


# ---------------------------------------------------------------------------
# Transcript scans: the same per-call judgement the readers make
# ---------------------------------------------------------------------------


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        for raw in handle:
            if b"tool_use" not in raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _scan_claude_code(root: Path) -> _Scan:
    from .sources import claude_code_reader as reader

    scan = _Scan()
    if not root.is_dir():
        return scan
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for main in sorted(p for p in project_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"):
            if reader._is_side_file(main.name):
                continue
            session_id = main.stem
            scan.session(session_id).marker_key = str(main)
            files = [(main, True)]
            subagents = project_dir / session_id / reader.SUBAGENTS_DIR
            if subagents.is_dir():
                files.extend((p, False) for p in sorted(subagents.iterdir())
                             if p.is_file() and p.suffix == ".jsonl" and not reader._is_side_file(p.name))
            for path, is_main in files:
                try:
                    fallback = float(path.stat().st_mtime)
                except OSError:
                    continue
                for record in _iter_jsonl(path):
                    if record.get("type") != "assistant":
                        continue
                    if is_main and record.get("isSidechain") is True:
                        continue  # the reader drops these before looking at their blocks
                    message = record.get("message") if isinstance(record.get("message"), dict) else {}
                    for block in content_blocks(message.get("content")):
                        if block.get("type") != "tool_use":
                            continue
                        touch = reader._tool_use_touch(block)
                        if touch is None:
                            continue
                        anchor = block.get("id") if isinstance(block.get("id"), str) and block.get("id") else None
                        scan.touched(reader.TOOL, session_id, touch, reader._record_epoch(record) or fallback, anchor)
    return scan


def _scan_codex(root: Path) -> _Scan:
    from .sources import codex_reader as reader

    scan = _Scan()
    for path in reader._iter_rollouts(root):
        try:
            fallback = float(path.stat().st_mtime)
        except OSError:
            continue
        thread_id: Optional[str] = None
        for _position, line in reader._iter_complete_lines(path, 0, settled=True):
            if not line:
                continue
            kind = line.get("type")
            payload = reader._payload_of(line)
            if kind == "session_meta" and thread_id is None:
                value = payload.get("id")
                thread_id = value if isinstance(value, str) and value else None
            if kind == "event_msg":
                touch = reader._event_touch(payload)
            elif kind == "response_item" and payload.get("type") in reader.TOOL_CALL_TYPES:
                touch = reader._call_touch(payload, reader._metadata_of(line))
            else:
                continue
            if touch is None:
                continue
            session_id = thread_id or reader._thread_id_from_name(path.name) or ""
            if not session_id:
                continue
            stamp = reader._parse_iso(line.get("timestamp"))
            scan.touched(reader.TOOL, session_id, touch, stamp.timestamp() if stamp else fallback,
                         reader._call_anchor(payload))
        session_id = thread_id or reader._thread_id_from_name(path.name)
        if session_id:
            scan.session(session_id)  # scanned, even with no touch at all
    return scan


# ---------------------------------------------------------------------------
# The verdict per stored row
# ---------------------------------------------------------------------------


def _epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _flagged_rows(conn: Any, source_id: str) -> List[Tuple[str, Optional[str], Optional[str], bool]]:
    rows = conn.execute(
        "SELECT message_id, event_at, json_extract(metadata_json, '$._metadata.session_id'), "
        "json_extract(metadata_json, '$._metadata.share_excluded') "
        "FROM ai_chat_messages WHERE source_id = ? AND share_excluded = 1",
        (source_id,),
    ).fetchall()
    return [(str(r[0]), r[1], (str(r[2]) if r[2] else None), r[3] in (1, "true", True)) for r in rows]


def _judge(rows, scan: _Scan) -> Tuple[List[str], Dict[str, int]]:
    clear: List[str] = []
    counts = {"kept_after_a_read": 0, "kept_no_transcript": 0, "kept_flagged_by_another_session": 0,
              "kept_undated": 0}
    for message_id, event_at, session_id, own_flag in rows:
        if not own_flag:
            counts["kept_flagged_by_another_session"] += 1
            continue
        entry = scan.sessions.get(session_id or "")
        if entry is None:
            counts["kept_no_transcript"] += 1
            continue
        at = _epoch(event_at)
        if at is None:
            counts["kept_undated"] += 1
            continue
        if entry.earliest_read is not None and at >= entry.earliest_read:
            counts["kept_after_a_read"] += 1
            continue
        clear.append(message_id)
    return clear, counts


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _chunks(items: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _clear_flags(conn: Any, message_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """The record's own metadata first (so no trigger can set the column again), then the column."""
    cleared: List[Dict[str, Any]] = []
    for chunk in _chunks(list(message_ids)):
        marks = ",".join("?" for _ in chunk)
        conn.execute(
            "UPDATE ai_chat_messages SET metadata_json = json_set(metadata_json, '$._metadata.share_excluded', "
            f"json('false')) WHERE message_id IN ({marks}) AND json_valid(metadata_json) "
            "AND json_type(metadata_json, '$._metadata') = 'object'",
            list(chunk),
        )
        conn.execute(
            "UPDATE ai_chat_messages SET metadata_json = json_set(metadata_json, '$.share_excluded', json('false')) "
            f"WHERE message_id IN ({marks}) AND json_valid(metadata_json) "
            "AND json_type(metadata_json, '$.share_excluded') IS NOT NULL",
            list(chunk),
        )
        conn.execute(f"UPDATE ai_chat_messages SET share_excluded = 0 WHERE message_id IN ({marks})", list(chunk))
        cursor = conn.execute(f"SELECT * FROM ai_chat_messages WHERE message_id IN ({marks}) AND share_excluded = 0",
                              list(chunk))
        keys = [c[0] for c in cursor.description]
        cleared.extend({**dict(zip(keys, row)), "_table": "ai_chat_messages"} for row in cursor.fetchall())
    return cleared


def _move_markers(conn: Any, source_id: str, scan: _Scan) -> int:
    from .checkpoints.file_cursors import FileCursorStore
    from .sources.claude_code_reader import TAINT_MARKER_SUFFIX

    store = FileCursorStore(conn)
    moved = 0
    for entry in scan.sessions.values():
        if not entry.marker_key:
            continue
        key = entry.marker_key + TAINT_MARKER_SUFFIX
        current = store.get(source_id, key)
        if current is None:
            continue
        if entry.earliest_read == float("-inf"):
            continue  # an undated read: keep the old marker
        # 0 reads as "no marker" (``_taint_marker``): the session only had system touches.
        new_mtime = float(entry.earliest_read) if entry.earliest_read is not None else 0.0
        if float(current.mtime or 0.0) != new_mtime:
            store.put(source_id, key, size=0, mtime=new_mtime, offset=0, record_count=0)
            moved += 1
    return moved


def reclassify_touches(conn: Any, source_id: str, *, apply: bool = False,
                       root: Optional[Path] = None) -> Dict[str, Any]:
    """Counts of what a re-judgement would change (``apply=False``) or did change. Never text, path or id."""
    from .ai_tool_sync import SOURCE_ID_TO_TOOL, _ensure_storage, _write_touches

    tool = SOURCE_ID_TO_TOOL.get(source_id)
    if tool is None:
        raise ValueError("unknown_source")
    flagged = _flagged_rows(conn, source_id)
    report: Dict[str, Any] = {"source_id": source_id, "applied": False, "flagged_rows": len(flagged)}
    if tool not in SUPPORTED_TOOLS:
        report.update(status="unsupported", would_clear=0)
        return report

    if tool == "claude_code":
        from .sources.claude_code_reader import DEFAULT_ROOT

        scan = _scan_claude_code(Path(root) if root else DEFAULT_ROOT)
    else:
        from .sources.codex_reader import _default_root

        scan = _scan_codex(Path(root) if root else _default_root())
    clear, kept = _judge(flagged, scan)
    touches = scan.ledger.touches
    report.update(
        status="ok",
        sessions_scanned=len(scan.sessions),
        sessions_with_reads=sum(1 for s in scan.sessions.values() if s.earliest_read is not None),
        topos_reads=sum(1 for t in touches if t["kind"] == TOUCH_READ),
        topos_system_touches=sum(1 for t in touches if t["kind"] != TOUCH_READ),
        would_clear=len(clear),
        **kept,
    )
    if not apply:
        return report

    from ..storage.db.write_gate import commit_connection, with_db_write

    _ensure_storage(conn)
    recorded = _write_touches(conn, touches)
    with with_db_write():
        cleared = _clear_flags(conn, clear)
        moved = _move_markers(conn, source_id, scan) if tool == "claude_code" else 0
        commit_connection(conn)
    projected = 0
    if cleared:
        from ..features.timeline_projection import project_timeline_rows

        projected = project_timeline_rows(conn, cleared, missing_only=True).written
    queued: List[str] = []
    if cleared:
        try:
            from .ai_tool_catchup import enqueue_catchup

            queued = enqueue_catchup(conn, source_id)
        except Exception as exc:  # noqa: BLE001 — the hourly catch-up will find them; never fail the repair
            logger.warning("touch repair: catch-up enqueue failed: %s", type(exc).__name__)
    report.update(applied=True, cleared=len(cleared), touches_recorded=recorded, markers_moved=moved,
                  timeline_rows_written=projected, catchup_jobs_queued=len(queued))
    return report
