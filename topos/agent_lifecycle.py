"""Agent disconnect, in one place for the CLI and the relay handler.

``topos-node agents disconnect NAME`` and the web app's Connected-apps
"Disconnect" (relay type ``mcp_client_revoke`` with ``client_id="agent-<name>"``)
must do the same three things (CONTRACTS §4, rev-1 security finding 12):

1. tombstone the enrolment row (``revoke_agent``; checked on every request,
   nothing cached, so the next call from that agent is refused);
2. pause THAT tool's own transcript source through the §2.8 owner door, so the
   hourly schedule and the plugin's hooks stop ingesting it — and only it: an
   owner with Claude Code and Cursor both connected who disconnects Cursor
   keeps Claude Code syncing (Gemini has no bundled source);
3. remove the label file, so the stdio server says "set up Topos" from now on.

The pause goes over the owner socket in both callers: from the CLI that is the
only door, and from inside the node it keeps the one code path (and the one
test fixture). A node whose socket is off reports the sources it could not
pause in ``problems`` rather than pretending.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .mcp_server import TRANSCRIPT_SOURCE_IDS, NodeDown, NodeRefused, NodeSocket, label_path

PAUSE_TIMEOUT_S = 30.0

#: Each client's own transcript source (CONTRACTS §2.1); Gemini has none in v1.
TRANSCRIPT_SOURCE_BY_CLIENT: dict[str, Optional[str]] = {
    "claude-code": "claude_code_sessions",
    "codex": "codex_sessions",
    "cursor": "cursor_sessions",
    "gemini": None,
}


def transcript_sources_for(client: str) -> tuple[str, ...]:
    """The source ids a disconnect of ``client`` pauses: its own, or none."""
    source_id = TRANSCRIPT_SOURCE_BY_CLIENT.get(client)
    return (source_id,) if source_id else ()


@dataclass
class DisconnectResult:
    client: str
    revoked: bool
    label_file_removed: bool
    sources_paused: list[str] = field(default_factory=list)
    sources_skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def was_connected(self) -> bool:
        return self.revoked or self.label_file_removed

    def as_dict(self) -> dict:
        return asdict(self)


def pause_transcript_sources(
    sock: Optional[NodeSocket] = None, *, source_ids: tuple[str, ...] = TRANSCRIPT_SOURCE_IDS
) -> tuple[list[str], list[str], list[str]]:
    """``(paused, skipped, problems)`` after ``POST …/sources/{id}/pause`` for each id.

    404/409 means nothing to pause (unknown, not enabled, not paired). A dead
    socket stops the loop with one problem naming the manual command.
    """
    sock = sock or NodeSocket(timeout_s=PAUSE_TIMEOUT_S)
    paused: list[str] = []
    skipped: list[str] = []
    problems: list[str] = []
    for source_id in source_ids:
        try:
            sock.request_sync("POST", f"/api/local/owner/sources/{source_id}/pause", json_body={})
            paused.append(source_id)
        except NodeDown:
            problems.append(
                "Topos isn't running, so its transcript source was not paused. Start Topos, then run "
                "`topos-node sources pause " + " / ".join(source_ids) + "`."
            )
            break
        except NodeRefused as exc:
            if exc.status in (404, 409):
                skipped.append(source_id)
            else:
                problems.append(f"{source_id}: pause refused ({exc.code}).")
    return paused, skipped, problems


def disconnect_agent(
    conn: Optional[sqlite3.Connection],
    client: str,
    *,
    sock: Optional[NodeSocket] = None,
    agents_dir: Optional[Path] = None,
) -> DisconnectResult:
    """Revoke + pause + remove. ``conn`` may be None when the node has no database yet."""
    from . import mcp_clients

    revoked = False
    revoker = getattr(mcp_clients, "revoke_agent", None)
    if conn is not None and revoker is not None:
        revoked = bool(revoker(conn, client=client))
    token = label_path(client, agents_dir=agents_dir)
    had_file = token.exists()
    if had_file:
        token.unlink()
    result = DisconnectResult(client=client, revoked=revoked, label_file_removed=had_file)
    if not result.was_connected:
        return result
    own = transcript_sources_for(client)
    if own:
        result.sources_paused, result.sources_skipped, result.problems = pause_transcript_sources(
            sock, source_ids=own
        )
    return result
