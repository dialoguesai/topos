"""Test doubles for the L6 readers: a stub of L4's ``ai_coding_tool.common`` and an
in-memory ``FileCursorStore``.

L4 builds the real ``topos/ingestion/parsers/ai_coding_tool/common.py`` and
``topos/ingestion/checkpoints/file_cursors.py`` in another worktree, to the
signatures in CONTRACTS.md §2.7b. The readers are coded against those exact
import paths; these doubles let their extraction guards run before L4 lands.

``scrub_text`` returns its input unchanged with no counts (the readers' guards
test extraction, never scrubbing). ``make_record`` builds the §2.2 dict and
raises ``ValueError("empty")`` on blank content. ``taints`` follows §2.7.
"""

from __future__ import annotations

import contextlib
import re
import os
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional, Sequence

from topos.ingestion.parsers.chatgpt_export import DropLedger

PACKAGE_NAME = "topos.ingestion.parsers.ai_coding_tool"
COMMON_NAME = PACKAGE_NAME + ".common"
READER_MODULES = (
    "topos.ingestion.sources.codex_reader",
    "topos.ingestion.sources.cursor_reader",
)


@dataclass
class Ledger(DropLedger):
    """§2.3: DropLedger plus ``scrubbed`` and ``tainted_turns``."""

    scrubbed_counts: Dict[str, int] = field(default_factory=dict)
    tainted_turns: int = 0

    def scrubbed(self, name: str, n: int = 1) -> None:
        self.scrubbed_counts[name] = self.scrubbed_counts.get(name, 0) + n

    def tainted(self, n: int = 1) -> None:
        self.tainted_turns += n

    def as_dict(self) -> Dict[str, Any]:
        out = super().as_dict()
        out["scrubbed"] = dict(sorted(self.scrubbed_counts.items()))
        out["tainted_turns"] = self.tainted_turns
        return out


def scrub_text(text: str, *, secrets: Sequence[str] = ()) -> tuple[str, Dict[str, int]]:
    return text, {}


_LEADING_TAG = re.compile(r"^\s*<[A-Za-z][\w.-]*(?:[\s>]|$)")


def is_harness_text(text: str) -> bool:
    return bool(_LEADING_TAG.match(text or ""))


_TAINT_NAME = re.compile(r"topos", re.IGNORECASE)
_TAINT_COMMAND_NEEDLES = (".topos", "engine.sock", "topos-node")


def taints(tool_or_server_name: Optional[str], command_text: Optional[str]) -> bool:
    name = tool_or_server_name or ""
    if _TAINT_NAME.search(name) or "remote__" in name:
        return True
    command = command_text or ""
    return any(needle in command for needle in _TAINT_COMMAND_NEEDLES)


def _iso_ms(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def make_record(
    *,
    tool: str,
    record_uuid: str,
    session_id: str,
    role: str,
    content: str,
    created_at: datetime,
    turn_index: int,
    title: str,
    origin: str,
    share_excluded: bool,
    timestamp_source: str,
    model_slug: Optional[str] = None,
) -> Dict[str, Any]:
    text = (content or "").strip()
    if not text:
        raise ValueError("empty")
    metadata: Dict[str, Any] = {
        "tool": tool,
        "session_id": session_id,
        "turn_index": int(turn_index),
        "conversation_title": title,
        "origin": origin,
        "share_excluded": bool(share_excluded),
        "timestamp_source": timestamp_source,
    }
    if model_slug:
        metadata["model_slug"] = model_slug
    return {
        "id": f"{tool}:{record_uuid}",
        "thread_id": session_id,
        "role": role,
        "content": text,
        "created_at": _iso_ms(created_at),
        "_metadata": metadata,
    }


@dataclass(frozen=True)
class FileCursor:
    size: int
    mtime: float
    offset: int
    record_count: int


class FakeCursorStore:
    """In-memory stand-in for ``FileCursorStore``: the same two methods."""

    def __init__(self) -> None:
        self.rows: Dict[tuple[str, str], FileCursor] = {}
        self.puts = 0

    def get(self, source_id: str, path: Any) -> Optional[FileCursor]:
        return self.rows.get((source_id, str(path)))

    def put(self, source_id: str, path: Any, *, size: int, mtime: float, offset: int, record_count: int) -> None:
        self.rows[(source_id, str(path))] = FileCursor(
            size=int(size), mtime=float(mtime), offset=int(offset), record_count=int(record_count)
        )
        self.puts += 1


def _build_stub_module() -> types.ModuleType:
    common = types.ModuleType(COMMON_NAME)
    common.Ledger = Ledger
    common.scrub_text = scrub_text
    common.is_harness_text = is_harness_text
    common.taints = taints
    common.make_record = make_record
    return common


@contextlib.contextmanager
def stubbed_common() -> Iterator[types.ModuleType]:
    """Install the stub package + module in ``sys.modules`` and drop any cached
    reader module, so the readers bind the stub on import. Restores on exit so a
    later test in the same session sees L4's real module when it exists."""
    if os.environ.get("TOPOS_TEST_REAL_COMMON") == "1":
        # Integration run: bind the readers to L4's real helpers instead of the stub
        # (the stub exists only so this lane's guards ran before L4's module did).
        import importlib

        importlib.import_module(COMMON_NAME)
        for name in READER_MODULES:
            sys.modules.pop(name, None)
        yield sys.modules[COMMON_NAME]
        return
    names = (PACKAGE_NAME, COMMON_NAME) + READER_MODULES
    saved = {name: sys.modules.get(name) for name in names}
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = []  # type: ignore[attr-defined]
    common = _build_stub_module()
    package.common = common  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
    sys.modules[COMMON_NAME] = common
    for name in READER_MODULES:
        sys.modules.pop(name, None)
    try:
        yield common
    finally:
        for name in names:
            previous = saved[name]
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        for name in READER_MODULES:
            sys.modules.pop(name, None)
