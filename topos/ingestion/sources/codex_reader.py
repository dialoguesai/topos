"""Codex reader: ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`` to §2.2 records.

Shape (wf1-verdicts-full.txt [transcripts.V4]; DIGEST F2): one JSON object per
line, ``{timestamp, ordinal, type, payload[, metadata]}``, with ``type`` one of
``session_meta``, ``response_item``, ``event_msg``, ``turn_context``,
``compacted``, ``token_usage_record``, ``world_state``,
``inter_agent_communication_metadata``. The first line is a ``session_meta``
whose ``payload.id`` is the thread id (it equals the file-name uuid); further
``session_meta`` lines mid-file are normal (resume, fork) and never start a new
conversation. The file name is ``rollout-<ts>-<threadId>.jsonl`` or
``rollout-<ts>-<threadId>_<rolloutId>.jsonl``.

Forks replay the parent's items with the same ``payload.id`` (the replayed user
prompt also carries ``metadata.inherited_user_message``), so items dedupe by id
across files in path order: first writer wins, the copy is counted
``fork_duplicate``. The canonical writer dedupes the same way across runs
(§2.1), so a replay that lands in a later run is a no-op there.

Spike A (P0.12, measured 2026-10-15) fixed the classification as the data
tables below. Keyboard-typed text is a ``response_item`` message with
``role == "user"`` in a thread whose first ``session_meta.source`` is a string
in ``CODEX_KEYBOARD_SOURCES`` (a dict source ``{"subagent": ...}`` marks a
subagent thread whose "user" messages are parent-injected), joined from the
``input_text`` blocks that are not harness blocks. Harness text is spliced in
as SEPARATE blocks, so ``CODEX_HARNESS_BLOCK_TAGS`` is matched per block, never
inside a block. ``auto-compact-*`` turns are the compaction summary, not typed.

Assistant rows are per reply, not per message: a turn writes a ``message``
item between each round of tool calls, so consecutive assistant messages of
one turn (``turn_id``, from the item's
``internal_chat_message_metadata_passthrough``) and one ``phase`` are joined
into one row with the first message's id and stamp. Commentary and the final
answer stay separate rows. Any other message (user, developer), a
``task_started``/``task_complete``/``turn_aborted`` event, ``compacted``,
``session_meta`` or the taint instant ends the reply. A turn still running at
the end of a rollout younger than ``OPEN_REPLY_HOLD_SECONDS`` (its last item
is commentary, a tool call or a tool output) is left for the next run.

Follows ``imessage_reader.py``'s shape: a frozen batch dataclass, one public
``read_codex_batch``, defaults resolved only when no root is given. The reader
never writes under the root; ``state_5.sqlite`` is opened ``mode=ro``. Nothing
from ``cwd`` reaches the record: the project facet is the folder's basename on
the row, outside ``_metadata`` (§2.6), for the runner to move to
``ai_chat_message_facets``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union

from topos.ingestion.parsers.ai_coding_tool.common import Ledger, make_record, project_key_from_cwd, scrub_text, taints

if TYPE_CHECKING:  # L4 owns the store; the reader only needs its two methods.
    from topos.ingestion.checkpoints.file_cursors import FileCursorStore

logger = logging.getLogger("topos.ingestion.sources.codex_reader")

TOOL = "codex"
SOURCE_ID = "codex_sessions"
SESSIONS_DIRNAME = "sessions"
STATE_DB_NAME = "state_5.sqlite"
IMPORTS_FILENAME = "external_agent_session_imports.json"

# --- Spike A (P0.12) data tables -------------------------------------------------
# A thread is keyboard-driven when its first session_meta.source is one of these
# STRINGS. Spike A measured "vscode" (Codex Desktop) on this machine; "cli" is
# the upstream value for the terminal client (codex-rs SessionSource::Cli) and
# is kept so CLI users' own prompts are not dropped. "exec" (non-interactive
# `codex exec`), "mcp" and "unknown" are not keyboard; a dict source is a
# subagent. Every excluded thread is counted `non_keyboard_origin:<tag>`.
CODEX_KEYBOARD_SOURCES = frozenset({"vscode", "cli"})
# A user-message block that starts with "<" + one of these is a harness block.
CODEX_HARNESS_BLOCK_TAGS = (
    "environment_context",
    "guardian_tool_descriptions",
    "in-app-browser-context",
    "external_codex_apps_open_page",
    "heartbeat",
    "image",
    "send_user_message_question_reply",
    "recommended_plugins",
    "turn_aborted",
)
_HARNESS_BLOCK_RE = re.compile(
    r"^\s*<(?:" + "|".join(re.escape(tag) for tag in CODEX_HARNESS_BLOCK_TAGS) + r")(?=[\s>/]|$)"
)
# "<no open files>"-style placeholders are harness too.
_NO_PLACEHOLDER_RE = re.compile(r"^\s*<no [a-z _-]+>\s*$")
AUTO_COMPACT_TURN_PREFIX = "auto-compact-"
# ---------------------------------------------------------------------------------

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
ROLLOUT_NAME_RE = re.compile(rf"^rollout-(?P<stamp>.+?)-(?P<thread>{_UUID})(?:_(?P<rollout>{_UUID}))?\.jsonl$")
_UUID_ANYWHERE_RE = re.compile(_UUID)

ORIGIN_ASSISTANT = "assistant"
ORIGIN_COMMENTARY = "commentary"
ORIGIN_FINAL_ANSWER = "final_answer"

TOOL_CALL_TYPES = frozenset({
    "function_call", "custom_tool_call", "local_shell_call", "web_search_call", "computer_call",
})
TOOL_OUTPUT_TYPES = frozenset({
    "function_call_output", "custom_tool_call_output", "local_shell_call_output", "computer_call_output",
})
ENVELOPE_KEYS = frozenset({"timestamp", "ordinal", "type", "payload", "metadata"})

# A last line with no trailing newline on a file this young is a writer mid-append:
# leave it for the next run. Older files are taken as complete.
SETTLE_SECONDS = 30.0
# A turn still running at the end of a rollout this young is held for the next run (see the docstring).
OPEN_REPLY_HOLD_SECONDS = 3600.0
TURN_EDGE_EVENTS = frozenset({"task_started", "task_complete", "turn_aborted"})
TURN_EDGE_LINES = frozenset({"compacted", "session_meta"})
TITLE_MAX = 120


@dataclass(frozen=True)
class ReadBatch:
    """One batch of §2.2 records. ``scanned_files`` counts rollouts looked at,
    ``files_touched`` the ones actually read (changed since their cursor).
    ``imported_sessions`` counts threads read that Codex Desktop lists in
    ``external_agent_session_imports.json``; their records carry
    ``_metadata.cross_tool_import`` (cross-tool dedupe is phase 2)."""

    rows: List[Dict[str, Any]]
    ledger: Ledger
    scanned_files: int
    files_touched: int
    newest_created_at: Optional[str]
    imported_sessions: int = 0


@dataclass
class _ThreadState:
    thread_id: str
    source_tag: str = "unknown"
    is_subagent: bool = False
    project_key: Optional[str] = None
    session_time: Optional[datetime] = None
    model: Optional[str] = None
    title: Optional[str] = None
    first_prompt: Optional[str] = None
    tainted: bool = False
    cross_tool_import: bool = False
    turn_index: int = 0
    header_seen: bool = False

    def apply_session_meta(self, payload: Dict[str, Any]) -> None:
        """The first session_meta is the header; later ones only fill gaps."""
        if not self.header_seen:
            self.header_seen = True
            thread_id = payload.get("id")
            if isinstance(thread_id, str) and thread_id:
                self.thread_id = thread_id
            self.source_tag, self.is_subagent = _classify_source(payload.get("source"))
        if self.project_key is None:
            self.project_key = _project_key(payload.get("cwd"))
        if self.session_time is None:
            self.session_time = _parse_iso(payload.get("timestamp"))
        if self.model is None and isinstance(payload.get("model"), str):
            self.model = payload["model"]

    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.first_prompt:
            return _title_from_prompt(self.first_prompt)
        return f"Codex session {self.thread_id[:8]}"


def _default_root() -> Path:
    custom = os.environ.get("CODEX_HOME", "").strip()
    return Path(custom) if custom else Path.home() / ".codex"


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
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


def _classify_source(value: Any) -> Tuple[str, bool]:
    """``session_meta.source``: a string names the client; a dict with
    ``subagent`` marks a subagent thread."""
    if isinstance(value, str):
        tag = value.strip().lower() or "unknown"
        return (tag, tag == "subagent")
    if isinstance(value, dict):
        if "subagent" in value:
            return ("subagent", True)
        if len(value) == 1:
            return (str(next(iter(value))).lower(), False)
    return ("unknown", False)


def _project_key(cwd: Any) -> Optional[str]:
    """The §2.6 facet, by the rule every reader shares: the folder's basename, None for the home folder itself."""
    return project_key_from_cwd(cwd)


def _title_from_prompt(text: str) -> str:
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if len(first_line) > TITLE_MAX:
        first_line = first_line[: TITLE_MAX - 1].rstrip() + "…"
    return first_line


def _thread_id_from_name(name: str) -> Optional[str]:
    match = ROLLOUT_NAME_RE.match(name)
    return match.group("thread") if match else None


def _within(path: Path, root: Path) -> bool:
    """Whether ``path`` lies under ``root`` once both are resolved (security re-check N4): a hook's
    ``transcript_path`` may name only the tool's own files, never another file on the disk."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _iter_rollouts(root: Path) -> List[Path]:
    sessions = root / SESSIONS_DIRNAME
    if not sessions.is_dir():
        return []
    return sorted(p for p in sessions.rglob("rollout-*.jsonl") if p.is_file() and ROLLOUT_NAME_RE.match(p.name))


def _load_titles(root: Path) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """``threads`` rows of ``state_5.sqlite`` keyed by thread id and by rollout
    file name: ``(title, cwd)``. Read-only; any failure means no titles."""
    db = root / STATE_DB_NAME
    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    if not db.is_file():
        return out
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("state_5.sqlite not readable: %s", exc)
        return out
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "id" not in columns:
            return out
        select = ["id"] + [name if name in columns else "NULL" for name in ("title", "cwd", "rollout_path")]
        for thread_id, title, cwd, rollout_path in conn.execute(f"SELECT {', '.join(select)} FROM threads"):
            entry = (str(title).strip() or None if title else None, str(cwd) if cwd else None)
            if thread_id:
                out[str(thread_id)] = entry
            if rollout_path:
                out[Path(str(rollout_path)).name] = entry
    except sqlite3.Error as exc:
        logger.debug("state_5.sqlite threads not readable: %s", exc)
    finally:
        conn.close()
    return out


def _load_imported_thread_ids(root: Path) -> Set[str]:
    """Thread ids Codex Desktop imported from other tools. The file's shape is
    not pinned (0 records on the measured machine), so every uuid-shaped string
    in it counts; foreign ids never match a local thread."""
    path = root / IMPORTS_FILENAME
    if not path.is_file():
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {match.group(0).lower() for match in _UUID_ANYWHERE_RE.finditer(text)}


def _iter_complete_lines(path: Path, start: int, *, settled: bool) -> Iterator[Tuple[int, Optional[Dict[str, Any]]]]:
    """Yield ``(end_offset, parsed)`` for each complete line from ``start``;
    ``parsed`` is None for a line that is not a JSON object."""
    with path.open("rb") as handle:
        handle.seek(start)
        position = start
        for raw in handle:
            position += len(raw)
            if not raw.endswith(b"\n") and not settled:
                break
            stripped = raw.strip()
            if not stripped:
                yield position, {}
                continue
            try:
                parsed = json.loads(stripped)
            except ValueError:
                yield position, None
                continue
            yield position, parsed if isinstance(parsed, dict) else None


def _read_header(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            raw = handle.readline()
    except OSError:
        return None
    try:
        line = json.loads(raw.strip() or b"{}")
    except ValueError:
        return None
    if isinstance(line, dict) and line.get("type") == "session_meta":
        payload = _payload_of(line)
        return payload
    return None


def _payload_of(line: Dict[str, Any]) -> Dict[str, Any]:
    payload = line.get("payload")
    if isinstance(payload, dict):
        return payload
    # Upstream RolloutLine "intentionally does not implement Deserialize":
    # tolerate a flattened envelope.
    return {k: v for k, v in line.items() if k not in ENVELOPE_KEYS}


def _metadata_of(line: Dict[str, Any]) -> Dict[str, Any]:
    metadata = line.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _turn_id(line: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
    """The item's turn id: real rollouts carry it only in the message's passthrough metadata."""
    passthrough = payload.get("internal_chat_message_metadata_passthrough")
    for source in (passthrough if isinstance(passthrough, dict) else {}, _metadata_of(line), payload):
        value = source.get("turn_id")
        if isinstance(value, str) and value:
            return value
    return None


def _ends_reply(line: Dict[str, Any]) -> bool:
    """A line that closes the assistant reply being assembled (see the module docstring)."""
    kind = line.get("type")
    if kind in TURN_EDGE_LINES:
        return True
    payload = _payload_of(line)
    if kind == "event_msg":
        return payload.get("type") in TURN_EDGE_EVENTS
    return kind == "response_item" and payload.get("type") == "message" and payload.get("role") != "assistant"


def _open_reply_start(entries: Sequence[Tuple[int, Optional[Dict[str, Any]]]]) -> Optional[int]:
    """Index of the first assistant message of a turn still running at the end of ``entries``, else None."""
    start: Optional[int] = None
    turn: Optional[str] = None
    awaiting = False
    for index, (_end, line) in enumerate(entries):
        if not line:
            continue
        if _ends_reply(line):
            start, awaiting = None, False
            continue
        if line.get("type") != "response_item":
            continue
        payload = _payload_of(line)
        if payload.get("type") == "message":
            item_turn = _turn_id(line, payload)
            if start is not None and item_turn != turn:
                start = None
            if start is None:
                start, turn = index, item_turn
            awaiting = payload.get("phase") == ORIGIN_COMMENTARY
        else:
            # A tool call, a tool output or reasoning: the model has another step to take.
            awaiting = True
    return start if awaiting else None


@dataclass
class _Piece:
    """One assistant message's text, before it is folded into a reply."""

    key: str
    text: str
    created_at: datetime
    timestamp_source: str
    origin: str
    turn_id: Optional[str]
    share_excluded: bool


@dataclass
class _Reply:
    first: _Piece
    texts: List[str] = field(default_factory=list)

    def takes(self, piece: _Piece) -> bool:
        return (piece.origin, piece.turn_id, piece.share_excluded) == (
            self.first.origin, self.first.turn_id, self.first.share_excluded,
        )


def _block_is_harness(text: str) -> bool:
    return bool(_HARNESS_BLOCK_RE.match(text) or _NO_PLACEHOLDER_RE.match(text))


def _user_text(content: Any) -> Tuple[str, int]:
    """Join the ``input_text`` blocks that are not harness blocks. Returns the
    text and how many harness blocks were removed."""
    if isinstance(content, str):
        return ("" if _block_is_harness(content) else content, 1 if _block_is_harness(content) else 0)
    parts: List[str] = []
    removed = 0
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "input_text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        if _block_is_harness(text):
            removed += 1
            continue
        parts.append(text)
    return "\n".join(parts).strip(), removed


def _assistant_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    parts = [
        block["text"]
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, dict) and block.get("type") == "output_text" and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts).strip()


def _content_key(line: Dict[str, Any], payload: Dict[str, Any]) -> str:
    """Stable id for an item without ``payload.id``: a replay keeps the line's
    timestamp, role and content, so it keys the same."""
    digest = hashlib.sha256()
    for part in (line.get("timestamp"), payload.get("role"), payload.get("content")):
        digest.update(json.dumps(part, sort_keys=True, default=str).encode("utf-8"))
        digest.update(b"\x00")
    return "sha256:" + digest.hexdigest()[:32]


def _attribution_text(metadata: Dict[str, Any]) -> Optional[str]:
    """``metadata.mcp_attribution`` names the MCP server/tool a call went to;
    its keys are not pinned, so every string value is offered to ``taints``."""
    attribution = metadata.get("mcp_attribution")
    if isinstance(attribution, dict):
        values = [str(v) for v in attribution.values() if isinstance(v, (str, int))]
        return " ".join(values) if values else None
    if isinstance(attribution, str):
        return attribution
    return None


def _call_taints(payload: Dict[str, Any], metadata: Dict[str, Any]) -> bool:
    name = payload.get("name")
    if not isinstance(name, str):
        name = None
    command: Optional[str] = None
    for key in ("arguments", "input"):
        value = payload.get(key)
        if isinstance(value, str):
            command = value
            break
        if value is not None:
            command = json.dumps(value, default=str)
            break
    action = payload.get("action")
    if command is None and isinstance(action, dict):
        parts = action.get("command")
        command = " ".join(str(p) for p in parts) if isinstance(parts, list) else json.dumps(action, default=str)
    attribution = _attribution_text(metadata)
    if attribution and taints(attribution, None):
        return True
    return taints(name, command)


def _event_taints(payload: Dict[str, Any]) -> bool:
    kind = payload.get("type")
    if kind == "mcp_tool_call_begin":
        invocation = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else payload
        server = invocation.get("server")
        tool = invocation.get("tool")
        name = "/".join(str(v) for v in (server, tool) if isinstance(v, str))
        arguments = invocation.get("arguments")
        return taints(name or None, json.dumps(arguments, default=str) if arguments is not None else None)
    if kind == "exec_command_begin":
        parts = payload.get("command")
        command = " ".join(str(p) for p in parts) if isinstance(parts, list) else (
            parts if isinstance(parts, str) else None
        )
        return taints(None, command)
    return False


def _line_time(line: Dict[str, Any], state: _ThreadState, file_mtime: float) -> Tuple[datetime, str]:
    stamp = _parse_iso(line.get("timestamp"))
    if stamp is not None:
        return stamp, "record"
    if state.session_time is not None:
        return state.session_time, "session"
    return datetime.fromtimestamp(file_mtime, tz=timezone.utc), "file_mtime"


def _prescan(path: Path, end: int, state: _ThreadState, seen: Set[str]) -> None:
    """Replay the lines before a resume offset for the state they carry: taint,
    model, the first prompt (title fallback) and the item ids already emitted."""
    for position, line in _iter_complete_lines(path, 0, settled=True):
        if position > end:
            break
        if not line:
            continue
        kind = line.get("type")
        payload = _payload_of(line)
        if kind == "session_meta":
            state.apply_session_meta(payload)
        elif kind == "turn_context":
            if isinstance(payload.get("model"), str):
                state.model = payload["model"]
        elif kind == "event_msg":
            if _event_taints(payload):
                state.tainted = True
        elif kind == "response_item":
            item_type = payload.get("type")
            if item_type in TOOL_CALL_TYPES:
                if _call_taints(payload, _metadata_of(line)):
                    state.tainted = True
            elif item_type == "message":
                item_id = payload.get("id")
                seen.add(item_id if isinstance(item_id, str) and item_id else _content_key(line, payload))
                if state.first_prompt is None and payload.get("role") == "user":
                    text, _removed = _user_text(payload.get("content"))
                    if text:
                        state.first_prompt = text


def _process_line(
    line: Dict[str, Any],
    state: _ThreadState,
    *,
    ledger: Ledger,
    seen: Set[str],
    skip: Set[str],
    since_unix: Optional[float],
    secrets: Sequence[str],
    file_mtime: float,
) -> Union[Dict[str, Any], _Piece, None]:
    """A user record, an assistant :class:`_Piece` for the caller to fold into a reply, or None."""
    kind = line.get("type")
    payload = _payload_of(line)
    if kind == "session_meta":
        state.apply_session_meta(payload)
        return None
    if kind == "turn_context":
        if isinstance(payload.get("model"), str):
            state.model = payload["model"]
        return None
    if kind == "event_msg":
        if _event_taints(payload):
            state.tainted = True
        return None
    if kind != "response_item":
        return None

    ledger.message_nodes += 1
    item_type = payload.get("type")
    if item_type in TOOL_CALL_TYPES:
        if _call_taints(payload, _metadata_of(line)):
            state.tainted = True
        ledger.drop("tool_use")
        return None
    if item_type in TOOL_OUTPUT_TYPES:
        ledger.drop("tool_result")
        return None
    if item_type == "reasoning":
        ledger.drop("thinking")
        return None
    if item_type != "message":
        ledger.drop(f"other_item:{item_type}")
        return None

    if state.is_subagent:
        ledger.drop("subagent")
        return None
    if state.source_tag not in CODEX_KEYBOARD_SOURCES:
        ledger.drop(f"non_keyboard_origin:{state.source_tag}")
        return None
    if state.project_key is not None and state.project_key in skip:
        ledger.drop("skipped_folder")
        return None

    role = payload.get("role")
    if role not in ("user", "assistant"):
        ledger.drop("system")  # developer/system prompts are injected, never typed
        return None
    metadata = _metadata_of(line)
    turn_id = _turn_id(line, payload)
    if isinstance(turn_id, str) and turn_id.startswith(AUTO_COMPACT_TURN_PREFIX):
        ledger.drop("harness_injected")
        return None

    item_id = payload.get("id")
    item_key = item_id if isinstance(item_id, str) and item_id else _content_key(line, payload)
    if metadata.get("inherited_user_message") is True or item_key in seen:
        ledger.drop("fork_duplicate")
        return None
    seen.add(item_key)

    if role == "user":
        text, removed = _user_text(payload.get("content"))
        if not text:
            ledger.drop("harness_injected" if removed else "empty")
            return None
        origin = state.source_tag
        share_excluded = False
    else:
        text = _assistant_text(payload.get("content"))
        if not text:
            ledger.drop("empty")
            return None
        phase = payload.get("phase")
        origin = (
            ORIGIN_COMMENTARY if phase == "commentary"
            else ORIGIN_FINAL_ANSWER if phase == "final_answer"
            else ORIGIN_ASSISTANT
        )
        share_excluded = state.tainted

    created_at, timestamp_source = _line_time(line, state, file_mtime)
    if since_unix is not None and created_at.timestamp() < since_unix:
        ledger.drop("before_since")
        return None

    text, counts = scrub_text(text, secrets=secrets)
    for name, count in counts.items():
        ledger.scrubbed(name, count)
    if role == "assistant":
        return _Piece(key=item_key, text=text, created_at=created_at, timestamp_source=timestamp_source,
                      origin=origin, turn_id=turn_id, share_excluded=share_excluded)
    if state.first_prompt is None:
        state.first_prompt = text
    return _emit(state, ledger, key=item_key, role=role, text=text, created_at=created_at,
                 timestamp_source=timestamp_source, origin=origin, share_excluded=share_excluded)


def _emit(
    state: _ThreadState,
    ledger: Ledger,
    *,
    key: str,
    role: str,
    text: str,
    created_at: datetime,
    timestamp_source: str,
    origin: str,
    share_excluded: bool,
) -> Optional[Dict[str, Any]]:
    try:
        record = make_record(
            tool=TOOL,
            record_uuid=key,
            session_id=state.thread_id,
            role=role,
            content=text,
            created_at=created_at,
            turn_index=state.turn_index,
            title=state.display_title(),
            origin=origin,
            share_excluded=share_excluded,
            timestamp_source=timestamp_source,
            model_slug=state.model if role == "assistant" else None,
        )
    except ValueError:
        ledger.drop("empty")
        return None
    if share_excluded:
        ledger.tainted()
    if state.cross_tool_import:
        record["_metadata"]["cross_tool_import"] = True
    record["project_key"] = state.project_key
    state.turn_index += 1
    ledger.turns_emitted += 1
    return record


def _read_rollout(
    path: Path,
    *,
    start: int,
    base_count: int,
    ledger: Ledger,
    seen: Set[str],
    titles: Dict[str, Tuple[Optional[str], Optional[str]]],
    imported: Set[str],
    skip: Set[str],
    since_unix: Optional[float],
    secrets: Sequence[str],
    limit: int,
    settled: bool,
    file_mtime: float,
) -> Tuple[List[Dict[str, Any]], int, _ThreadState]:
    state = _ThreadState(thread_id=_thread_id_from_name(path.name) or path.stem, turn_index=base_count)
    header = _read_header(path)
    if header is not None:
        state.apply_session_meta(header)
    hint = titles.get(state.thread_id) or titles.get(path.name)
    if hint is not None:
        state.title = hint[0]
        if state.project_key is None:
            state.project_key = _project_key(hint[1])
    state.cross_tool_import = state.thread_id.lower() in imported
    if start > 0:
        _prescan(path, start, state, seen)

    rows: List[Dict[str, Any]] = []
    consumed = start
    entries = list(_iter_complete_lines(path, start, settled=settled))
    if time.time() - file_mtime < OPEN_REPLY_HOLD_SECONDS:
        hold = _open_reply_start(entries)
        if hold is not None:
            entries = entries[:hold]
    reply: Optional[_Reply] = None

    def flush() -> None:
        nonlocal reply
        if reply is None:
            return
        done, reply = reply, None
        first = done.first
        record = _emit(state, ledger, key=first.key, role="assistant", text="\n\n".join(done.texts),
                       created_at=first.created_at, timestamp_source=first.timestamp_source,
                       origin=first.origin, share_excluded=first.share_excluded)
        if record is not None:
            rows.append(record)

    for end, line in entries:
        if line is None:
            ledger.drop("unparseable")
        elif line:
            if _ends_reply(line):
                flush()
            out = _process_line(
                line, state, ledger=ledger, seen=seen, skip=skip,
                since_unix=since_unix, secrets=secrets, file_mtime=file_mtime,
            )
            if isinstance(out, _Piece):
                if reply is not None and not reply.takes(out):
                    flush()
                if reply is None:
                    reply = _Reply(first=out)
                else:
                    ledger.merged_blocks += 1
                reply.texts.append(out.text)
            elif out is not None:
                rows.append(out)
        consumed = end
        # A batch ends between replies only, so the size cap never cuts one in two.
        if reply is None and len(rows) >= limit:
            break
    flush()
    title = state.display_title()
    for row in rows:
        row["_metadata"]["conversation_title"] = title
    return rows, consumed, state


def read_codex_batch(
    *,
    root_path: Optional[Path] = None,
    cursors: "FileCursorStore",
    batch_size: int = 2000,
    since_unix: Optional[float] = None,
    session_path: Optional[str] = None,
    skip_folders: Sequence[str] = (),
    secrets: Sequence[str] = (),
) -> ReadBatch:
    """Scan rollouts under ``root_path`` (default ``$CODEX_HOME`` or ``~/.codex``)
    from each file's byte cursor and return up to ``batch_size`` §2.2 records.

    ``session_path`` reads exactly that rollout (a hook's ``transcript_path``).
    ``since_unix`` skips files older than it and records before it.
    ``skip_folders`` are project basenames the owner excluded (§2.6 facet).
    A file whose size and mtime match its cursor and is fully consumed is not
    reopened; a file shorter than its cursor's offset is read again from 0.
    """
    root = Path(root_path) if root_path is not None else _default_root()
    ledger = Ledger()
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    titles = _load_titles(root)
    imported = _load_imported_thread_ids(root)
    imported_touched: Set[str] = set()
    skip = {str(name) for name in skip_folders}
    if session_path:
        files = [Path(session_path)] if _within(Path(session_path), root) else []
        if not files:
            ledger.drop("outside_root")  # a hook's transcript_path names only the tool's own files
    else:
        files = _iter_rollouts(root)
    scanned = touched = 0
    now = time.time()

    for path in files:
        if len(rows) >= batch_size:
            break
        if not path.is_file():
            continue
        scanned += 1
        stat = path.stat()
        if since_unix is not None and stat.st_mtime < since_unix:
            continue
        cursor = cursors.get(SOURCE_ID, str(path))
        start = 0
        base_count = 0
        if cursor is not None:
            if cursor.offset > stat.st_size:
                logger.info("rollout shrank below its cursor; rereading from 0: %s", path.name)
            else:
                if cursor.offset >= stat.st_size and cursor.size == stat.st_size and cursor.mtime == stat.st_mtime:
                    continue
                start = cursor.offset
                base_count = cursor.record_count
        ledger.conversations_seen += 1
        file_rows, consumed, state = _read_rollout(
            path,
            start=start,
            base_count=base_count,
            ledger=ledger,
            seen=seen,
            titles=titles,
            imported=imported,
            skip=skip,
            since_unix=since_unix,
            secrets=secrets,
            limit=batch_size - len(rows),
            settled=(now - stat.st_mtime) >= SETTLE_SECONDS,
            file_mtime=stat.st_mtime,
        )
        if file_rows or consumed != start or cursor is None:
            # A rollout whose only new lines are a held turn moved nothing: the runner stops asking for it.
            touched += 1
        if file_rows:
            ledger.conversations_kept += 1
        if state.cross_tool_import:
            imported_touched.add(state.thread_id)
        rows.extend(file_rows)
        cursors.put(
            SOURCE_ID, str(path),
            size=stat.st_size, mtime=stat.st_mtime, offset=consumed,
            record_count=base_count + len(file_rows),
        )

    newest = max((row["created_at"] for row in rows), default=None)
    return ReadBatch(
        rows=rows,
        ledger=ledger,
        scanned_files=scanned,
        files_touched=touched,
        newest_created_at=newest,
        imported_sessions=len(imported_touched),
    )
