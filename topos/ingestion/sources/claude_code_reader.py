"""Claude Code transcript reader: ``~/.claude/projects/*/<session>.jsonl`` → §2.2 records.

What a session file is (DIGEST F1, spike A): one JSON record per line, typed
``user``/``assistant`` plus title, summary and system records; one assistant
record per content block, the blocks of one reply sharing ``message.id``; tool
results arriving inside ``role=user``; harness text spliced into user records;
fork and resume copies of earlier records (same ``uuid``) in new files;
``.orphaned-*``/``.superseded-*`` side files left by compaction; sub-agent
transcripts under ``<session>/subagents/``; files deleted after 30 days.

What this reader does with it (CONTRACTS §2.3, §2.4, §2.7):

* emits a ``user`` row only for text the person typed (the keyboard-origin
  table in ``common``), an ``assistant`` row per ``message.id`` with its text
  blocks joined in order, and counts every other record in the ledger by reason;
* scrubs secrets, home paths and hostnames before a row leaves here;
* taints a session from the first Topos touch in it or in any of its sub-agent
  files, and marks every later assistant row ``share_excluded``;
* reads each file from its byte cursor, so an hourly run costs the new bytes.

It never reads anything but the files under the root it is given, and the
default root is only a default: tests point it at fixture trees.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..parsers.ai_coding_tool.common import (
    DROP_EMPTY,
    DROP_FILE_GONE,
    DROP_FORK_DUPLICATE,
    DROP_HARNESS_INJECTED,
    DROP_OUTSIDE_WINDOW,
    DROP_SIDE_FILE,
    DROP_SKIPPED_FOLDER,
    DROP_SUBAGENT,
    DROP_SYSTEM,
    DROP_THINKING,
    DROP_TOOL_RESULT,
    DROP_TOOL_USE,
    Ledger,
    classify_user_text,
    content_blocks,
    machine_hostnames,
    machine_usernames,
    make_record,
    non_keyboard_reason,
    origin_violation,
    scrub_text,
    taints,
)

logger = logging.getLogger("topos.ingestion.sources.claude_code_reader")

SOURCE_ID = "claude_code_sessions"
TOOL = "claude_code"
DEFAULT_ROOT = Path.home() / ".claude" / "projects"

SIDE_FILE_MARKERS = (".orphaned-", ".superseded-")
SUBAGENTS_DIR = "subagents"
TITLE_FILE = "custom-title.json"
TITLE_MAX_CHARS = 80
# Title-bearing record types, best first, with the keys each may carry the title in.
TITLE_RECORD_FIELDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("custom-title", ("customTitle", "custom_title", "title")),
    ("ai-title", ("aiTitle", "ai_title", "title")),
    ("summary", ("summary",)),
)
TITLE_FILE_KEYS = ("title", "customTitle", "custom_title", "name")
# A session's taint instant persists as a cursor row under this key so a later
# run — which reads only new bytes — still knows the session is tainted.
TAINT_MARKER_SUFFIX = "#taint"
# Bytes read from the end of a never-seen file to find its newest record stamp.
TAIL_PROBE_BYTES = 64 * 1024
# Lines read from the top of a file to find the first typed prompt for a title.
TITLE_PROBE_LINES = 400
DROP_UNPARSEABLE = "unparseable"
DROP_OTHER_BLOCK = "other_block"
DROP_OUTSIDE_ROOT = "outside_root"

_TITLE_LINE_RE = re.compile(rb'"type"\s*:\s*"(?:custom-title|ai-title|summary)"')
_TOOL_USE_HINT = b"tool_use"


@dataclass
class ReadBatch:
    rows: List[Dict[str, Any]]
    ledger: Ledger
    scanned_files: int
    files_touched: int
    newest_created_at: Optional[str]


@dataclass
class _Session:
    session_id: str
    title: str
    turn_index: int
    taint_at: Optional[float]
    file_mtime: float
    # First record stamp seen in the file: the fallback for records without one.
    session_epoch: Optional[float] = None


@dataclass
class _Pending:
    """One assistant reply being assembled across the records that share its message.id."""

    message_id: str
    uuid: str
    epoch: float
    stamp: datetime
    ts_source: str
    model: Optional[str]
    texts: List[str] = field(default_factory=list)


def read_claude_code_batch(
    *,
    root_path: Optional[Path] = None,
    cursors: Any,
    batch_size: int = 2000,
    since_unix: Optional[float] = None,
    session_path: Optional[str] = None,
    skip_folders: Sequence[str] = (),
    secrets: Sequence[str] = (),
    hostnames: Optional[Sequence[str]] = None,
    usernames: Optional[Sequence[str]] = None,
) -> ReadBatch:
    """Read new turns from every session under ``root_path`` (or one ``session_path``).

    ``cursors`` is a :class:`FileCursorStore` (or anything with its ``get``/``put``).
    ``batch_size`` is a soft cap checked between files, so a file's cursor only
    ever moves past whole records. ``since_unix`` applies to files not yet
    cursored, by their newest RECORD stamp (mtime is not a proxy: spike A).
    ``skip_folders`` are project slugs (as listed, or in their scrubbed ``-~-``
    form). ``secrets`` are the node's exact secrets (§2.4 step 1); ``hostnames``
    and ``usernames`` default to this machine's (§2.4 step 3).
    """
    ledger = Ledger()
    skip = _skip_set(skip_folders)
    # Resolved once per batch, not once per turn: scrub_text would otherwise ask the OS for the hostname and
    # the login name for every record it scrubs.
    if hostnames is None:
        hostnames = machine_hostnames()
    if usernames is None:
        usernames = machine_usernames()
    root = Path(root_path) if root_path is not None else DEFAULT_ROOT
    if session_path is not None:
        candidates = _session_path_candidates(Path(session_path), root, skip, ledger)
    else:
        candidates = _walk_root(root, skip, ledger)

    rows: List[Dict[str, Any]] = []
    seen_uuids: Set[str] = set()
    files_touched = 0
    for path in candidates:
        if batch_size and len(rows) >= batch_size:
            break
        file_rows, touched = _read_session_file(
            path,
            cursors=cursors,
            ledger=ledger,
            since_unix=since_unix,
            seen_uuids=seen_uuids,
            secrets=secrets,
            hostnames=hostnames,
            usernames=usernames,
        )
        if touched:
            files_touched += 1
        rows.extend(file_rows)
    newest = max((row["created_at"] for row in rows), default=None)
    return ReadBatch(
        rows=rows,
        ledger=ledger,
        scanned_files=len(candidates),
        files_touched=files_touched,
        newest_created_at=newest,
    )


# ---------------------------------------------------------------------------
# Walking
# ---------------------------------------------------------------------------


def _skip_set(skip_folders: Sequence[str]) -> Set[str]:
    return {str(name).strip() for name in skip_folders if str(name).strip()}


def _folder_skipped(slug: str, skip: Set[str]) -> bool:
    if not skip:
        return False
    if slug in skip:
        return True
    scrubbed, _ = scrub_text(slug, hostnames=(), usernames=())
    return scrubbed in skip


def _within(path: Path, root: Path) -> bool:
    """Whether ``path`` lies under ``root`` once both are resolved (security re-check N4): a hook's
    ``transcript_path`` may name only the tool's own files, never another file on the disk."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _is_side_file(name: str) -> bool:
    return any(marker in name for marker in SIDE_FILE_MARKERS)


def _session_path_candidates(path: Path, root: Path, skip: Set[str], ledger: Ledger) -> List[Path]:
    """The hook's ``transcript_path``: one file under the tool's own root, still subject to the folder and
    side-file rules. A path outside the root is dropped: the sync door takes it from any socket caller."""
    if not _within(path, root):
        ledger.drop(DROP_OUTSIDE_ROOT)
        return []
    if _folder_skipped(path.parent.name, skip):
        ledger.drop(DROP_SKIPPED_FOLDER)
        return []
    if _is_side_file(path.name):
        ledger.drop(DROP_SIDE_FILE)
        return []
    return [path]


def _walk_root(root: Path, skip: Set[str], ledger: Ledger) -> List[Path]:
    """Session files under ``root/<slug>/``, oldest first; side files and skipped folders counted."""
    if not root.is_dir():
        logger.info("Claude Code root %s is not a directory; nothing to read", root)
        return []
    candidates: List[Path] = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        if _folder_skipped(project_dir.name, skip):
            ledger.drop(DROP_SKIPPED_FOLDER)
            continue
        files: List[Tuple[float, str, Path]] = []
        for entry in project_dir.iterdir():
            if not entry.is_file():
                continue
            if _is_side_file(entry.name):
                ledger.drop(DROP_SIDE_FILE)
                continue
            if entry.suffix != ".jsonl":
                continue
            try:
                mtime = entry.stat().st_mtime
            except FileNotFoundError:
                continue
            files.append((mtime, entry.name, entry))
        # Oldest first: of a fork pair the original is usually older, so it is
        # the copy that keeps its uuids and the fork that counts as duplicate.
        candidates.extend(path for _mtime, _name, path in sorted(files, key=lambda item: (item[0], item[1])))
    return candidates


# ---------------------------------------------------------------------------
# One session file
# ---------------------------------------------------------------------------


def _read_session_file(
    path: Path,
    *,
    cursors: Any,
    ledger: Ledger,
    since_unix: Optional[float],
    seen_uuids: Set[str],
    secrets: Sequence[str],
    hostnames: Optional[Sequence[str]],
    usernames: Optional[Sequence[str]],
) -> Tuple[List[Dict[str, Any]], bool]:
    key = str(path)
    cursor = cursors.get(SOURCE_ID, key)
    try:
        stat = path.stat()
    except FileNotFoundError:
        # Claude Code deletes transcripts after cleanupPeriodDays. The cursor
        # stays: nothing here is read again, and nothing rewinds.
        ledger.drop(DROP_FILE_GONE)
        return [], False

    if cursor is None and since_unix is not None:
        newest = _newest_record_epoch(path)
        if newest is not None and newest < since_unix:
            ledger.drop(DROP_OUTSIDE_WINDOW)
            return [], False

    offset = cursor.offset if cursor is not None else 0
    if offset > stat.st_size:
        # Re-created shorter than the cursor: start over. The canonical
        # first-writer-wins rule makes the repeated rows a no-op.
        offset = 0
    if cursor is not None and offset == stat.st_size:
        return [], False

    try:
        data = _read_from(path, offset)
    except FileNotFoundError:
        ledger.drop(DROP_FILE_GONE)
        return [], False
    lines, consumed = _complete_lines(data)

    ledger.conversations_seen += 1
    session_id = path.stem
    session_dir = path.parent / path.stem
    marker = _taint_marker(cursors, key)
    taint_at = _min_epoch(marker, _scan_subagents(session_dir, cursors, ledger))
    state = _Session(
        session_id=session_id,
        title=_title_for_session(path, session_id, secrets, hostnames, usernames),
        turn_index=cursor.record_count if cursor is not None else 0,
        taint_at=taint_at,
        file_mtime=float(stat.st_mtime),
    )
    rows = _classify(lines, state, ledger, seen_uuids, secrets, hostnames, usernames)

    if state.taint_at is not None and (marker is None or state.taint_at < marker):
        _put_taint_marker(cursors, key, state.taint_at)
    cursors.put(
        SOURCE_ID,
        key,
        size=int(stat.st_size),
        mtime=float(stat.st_mtime),
        offset=offset + consumed,
        record_count=state.turn_index,
    )
    if rows:
        ledger.conversations_kept += 1
    return rows, True


def _read_from(path: Path, offset: int) -> bytes:
    with path.open("rb") as handle:
        if offset:
            handle.seek(offset)
        return handle.read()


def _complete_lines(data: bytes) -> Tuple[List[bytes], int]:
    """Whole lines and the byte count they cover; a trailing partial line waits for the next run."""
    if not data:
        return [], 0
    parts = data.split(b"\n")
    if data.endswith(b"\n"):
        return [line for line in parts[:-1]], len(data)
    partial = parts[-1]
    return [line for line in parts[:-1]], len(data) - len(partial)


def _parse_line(line: bytes) -> Optional[Dict[str, Any]]:
    if not line.strip():
        return None
    try:
        record = json.loads(line.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def _record_datetime(record: Dict[str, Any]) -> Optional[datetime]:
    """The record's own stamp (ISO with ms and a Z, or epoch s/ms), as an aware UTC datetime."""
    value = record.get("timestamp")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _record_epoch(record: Dict[str, Any]) -> Optional[float]:
    stamp = _record_datetime(record)
    return stamp.timestamp() if stamp is not None else None


def _stamp(record: Dict[str, Any], state: _Session) -> Tuple[datetime, float, str]:
    """``(datetime, epoch, timestamp_source)`` for a record, with the §2.2 fallbacks."""
    own = _record_datetime(record)
    if own is not None:
        epoch = own.timestamp()
        if state.session_epoch is None:
            state.session_epoch = epoch
        return own, epoch, "record"
    if state.session_epoch is not None:
        return datetime.fromtimestamp(state.session_epoch, tz=timezone.utc), state.session_epoch, "session"
    return datetime.fromtimestamp(state.file_mtime, tz=timezone.utc), state.file_mtime, "file_mtime"


def _min_epoch(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _newest_record_epoch(path: Path) -> Optional[float]:
    """The newest stamped record near the end of the file, from one tail read."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > TAIL_PROBE_BYTES:
                handle.seek(size - TAIL_PROBE_BYTES)
            tail = handle.read()
    except OSError:
        return None
    for line in reversed(tail.split(b"\n")):
        record = _parse_line(line)
        if record is None:
            continue
        epoch = _record_epoch(record)
        if epoch is not None:
            return epoch
    return None


# ---------------------------------------------------------------------------
# Taint (§2.7)
# ---------------------------------------------------------------------------


def _taint_marker(cursors: Any, key: str) -> Optional[float]:
    marker = cursors.get(SOURCE_ID, key + TAINT_MARKER_SUFFIX)
    if marker is None:
        return None
    return float(marker.mtime) if marker.mtime else None


def _put_taint_marker(cursors: Any, key: str, epoch: float) -> None:
    cursors.put(SOURCE_ID, key + TAINT_MARKER_SUFFIX, size=0, mtime=float(epoch), offset=0, record_count=0)


def _tool_use_taint(block: Dict[str, Any]) -> bool:
    """A tool call touches a Topos surface by its name or by ANY of its inputs — the shell command, but also the
    path a Read/Grep/Edit names (security review F6: ``~/.topos/database.db`` read through a file tool)."""
    inputs = block.get("input")
    text: Optional[str] = None
    if isinstance(inputs, str):
        text = inputs
    elif inputs is not None:
        try:
            text = json.dumps(inputs, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(inputs)
    return taints(block.get("name"), text)


def _scan_subagents(session_dir: Path, cursors: Any, ledger: Ledger) -> Optional[float]:
    """Earliest Topos touch in the NEW bytes of the session's sub-agent files; rows never come from here."""
    folder = session_dir / SUBAGENTS_DIR
    if not folder.is_dir():
        return None
    earliest: Optional[float] = None
    for entry in sorted(folder.iterdir()):
        if not entry.is_file() or entry.suffix != ".jsonl" or _is_side_file(entry.name):
            continue
        key = str(entry)
        cursor = cursors.get(SOURCE_ID, key)
        try:
            stat = entry.stat()
        except FileNotFoundError:
            continue
        offset = cursor.offset if cursor is not None else 0
        if offset > stat.st_size:
            offset = 0
        if cursor is not None and offset == stat.st_size:
            continue
        try:
            data = _read_from(entry, offset)
        except FileNotFoundError:
            continue
        lines, consumed = _complete_lines(data)
        ledger.subagent_files_scanned += 1
        for line in lines:
            if _TOOL_USE_HINT not in line:
                continue
            record = _parse_line(line)
            if record is None or record.get("type") != "assistant":
                continue
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            for block in content_blocks(message.get("content")):
                if block.get("type") == "tool_use" and _tool_use_taint(block):
                    earliest = _min_epoch(earliest, _record_epoch(record) or float(stat.st_mtime))
        cursors.put(SOURCE_ID, key, size=int(stat.st_size), mtime=float(stat.st_mtime),
                    offset=offset + consumed, record_count=0)
    return earliest


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------


def _title_for_session(
    path: Path,
    session_id: str,
    secrets: Sequence[str],
    hostnames: Optional[Sequence[str]],
    usernames: Optional[Sequence[str]],
) -> str:
    """custom-title.json, else the best title record, else the first typed prompt — scrubbed, then cut to 80."""
    raw = (
        _title_from_file(path.parent / path.stem / TITLE_FILE)
        or _title_from_records(path)
        or _title_from_first_prompt(path)
    )
    if raw:
        # Scrub before truncating: a cut can leave a fragment no rule recognises.
        scrubbed, _ = scrub_text(" ".join(raw.split()), secrets=secrets, hostnames=hostnames, usernames=usernames)
        scrubbed = scrubbed.strip()[:TITLE_MAX_CHARS].strip()
        if scrubbed:
            return scrubbed
    return f"Claude Code session {session_id[:8]}"


def _title_from_file(title_file: Path) -> Optional[str]:
    try:
        payload = json.loads(title_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, dict):
        for key in TITLE_FILE_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _title_from_records(path: Path) -> Optional[str]:
    """Title records anywhere in the file; the last of each kind wins (a rename replaces)."""
    found: Dict[str, str] = {}
    try:
        with path.open("rb") as handle:
            for line in handle:
                if not _TITLE_LINE_RE.search(line):
                    continue
                record = _parse_line(line)
                if record is None:
                    continue
                for record_type, keys in TITLE_RECORD_FIELDS:
                    if record.get("type") != record_type:
                        continue
                    for key in keys:
                        value = record.get(key)
                        if isinstance(value, str) and value.strip():
                            found[record_type] = value.strip()
                            break
    except OSError:
        return None
    for record_type, _keys in TITLE_RECORD_FIELDS:
        if record_type in found:
            return found[record_type]
    return None


def _title_from_first_prompt(path: Path) -> Optional[str]:
    try:
        with path.open("rb") as handle:
            for index, line in enumerate(handle):
                if index >= TITLE_PROBE_LINES:
                    break
                record = _parse_line(line)
                if record is None or record.get("type") != "user":
                    continue
                if origin_violation(record) is not None:
                    continue
                text, tag = _user_text(record)
                if text is None or tag is not None:
                    continue
                kept, harness, _ = classify_user_text(text)
                if harness is None and kept:
                    return kept
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Classification (§2.3)
# ---------------------------------------------------------------------------


def _user_text(record: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(joined text, drop reason)`` for a user record's content: tool results and odd blocks are reasons."""
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    blocks = list(content_blocks(message.get("content")))
    if any(block.get("type") == "tool_result" for block in blocks):
        return None, DROP_TOOL_RESULT
    if any(block.get("type") not in ("text", "image") for block in blocks):
        return None, non_keyboard_reason("content_blocks")
    return "\n\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text"), None


def _classify(
    lines: Iterable[bytes],
    state: _Session,
    ledger: Ledger,
    seen_uuids: Set[str],
    secrets: Sequence[str],
    hostnames: Optional[Sequence[str]],
    usernames: Optional[Sequence[str]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    pending: Optional[_Pending] = None

    def emit(role: str, *, uuid: str, content: str, stamp: datetime, ts_source: str,
             origin: str, share_excluded: bool, model: Optional[str]) -> None:
        row = make_record(
            tool=TOOL,
            record_uuid=uuid,
            session_id=state.session_id,
            role=role,
            content=content,
            created_at=stamp,
            turn_index=state.turn_index,
            title=state.title,
            origin=origin,
            share_excluded=share_excluded,
            timestamp_source=ts_source,
            model_slug=model,
        )
        state.turn_index += 1
        ledger.turns_emitted += 1
        if share_excluded:
            ledger.tainted(1)
        rows.append(row)

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        reply, pending = pending, None
        if not reply.texts:
            return
        content = "\n\n".join(text.strip() for text in reply.texts if text and text.strip())
        content, counts = scrub_text(content, secrets=secrets, hostnames=hostnames, usernames=usernames)
        ledger.record_scrubs(counts, session_id=state.session_id)
        if not content.strip():
            ledger.drop(DROP_EMPTY)
            return
        excluded = state.taint_at is not None and reply.epoch >= state.taint_at
        emit("assistant", uuid=reply.uuid, content=content, stamp=reply.stamp, ts_source=reply.ts_source,
             origin="assistant", share_excluded=excluded, model=reply.model)

    def assistant(record: Dict[str, Any]) -> None:
        nonlocal pending
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        message_id = str(message.get("id") or record.get("uuid") or "")
        if pending is not None and pending.message_id != message_id:
            flush()
        if record.get("isSidechain") is True:
            flush()
            ledger.drop(DROP_SUBAGENT)
            return
        uuid = record.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            ledger.drop(DROP_UNPARSEABLE)
            return
        if uuid in seen_uuids:
            ledger.drop(DROP_FORK_DUPLICATE)
            return
        seen_uuids.add(uuid)
        stamp, epoch, ts_source = _stamp(record, state)
        blocks = list(content_blocks(message.get("content")))
        if not blocks:
            ledger.drop(DROP_EMPTY)
            return
        first_text_here = True
        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                if pending is None or pending.message_id != message_id:
                    pending = _Pending(message_id, uuid, epoch, stamp, ts_source,
                                       model=message.get("model") if isinstance(message.get("model"), str) else None)
                elif first_text_here:
                    ledger.merged_blocks += 1
                first_text_here = False
                pending.texts.append(str(block.get("text") or ""))
            elif block_type == "tool_use":
                ledger.drop(DROP_TOOL_USE)
                if _tool_use_taint(block):
                    state.taint_at = _min_epoch(state.taint_at, epoch)
            elif block_type in ("thinking", "redacted_thinking"):
                ledger.drop(DROP_THINKING)
            else:
                ledger.drop(f"{DROP_OTHER_BLOCK}:{block_type}")

    def user(record: Dict[str, Any]) -> None:
        uuid = record.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            ledger.drop(DROP_UNPARSEABLE)
            return
        if uuid in seen_uuids:
            ledger.drop(DROP_FORK_DUPLICATE)
            return
        seen_uuids.add(uuid)
        tag = origin_violation(record)
        if tag is not None:
            ledger.drop(non_keyboard_reason(tag), origin=tag)
            return
        text, reason = _user_text(record)
        if reason is not None:
            ledger.drop(reason, origin=reason.split(":", 1)[1] if ":" in reason else None)
            return
        kept, harness, stripped = classify_user_text(text or "")
        ledger.harness_spans_stripped += stripped
        if harness is not None:
            ledger.drop(DROP_HARNESS_INJECTED, origin=harness)
            return
        if not kept:
            ledger.drop(DROP_EMPTY)
            return
        kept, counts = scrub_text(kept, secrets=secrets, hostnames=hostnames, usernames=usernames)
        ledger.record_scrubs(counts, session_id=state.session_id)
        if not kept.strip():
            ledger.drop(DROP_EMPTY)
            return
        stamp, _epoch, ts_source = _stamp(record, state)
        emit("user", uuid=uuid, content=kept, stamp=stamp, ts_source=ts_source,
             origin="keyboard", share_excluded=False, model=None)

    for line in lines:
        record = _parse_line(line)
        if record is None:
            if line.strip():
                ledger.drop(DROP_UNPARSEABLE)
            continue
        ledger.message_nodes += 1
        record_type = record.get("type")
        if record_type == "assistant":
            assistant(record)
        elif record_type == "user":
            flush()
            user(record)
        elif record_type == "system":
            flush()
            ledger.drop(DROP_SYSTEM)
        else:
            # Titles, summaries, snapshots, progress: not turns, not drops.
            flush()
            ledger.meta_records += 1
    flush()
    return rows
