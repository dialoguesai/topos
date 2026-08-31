"""Group chat messages into units worth embedding.

A message is the wrong unit for retrieval and the right unit for citation.

"sure, do that" carries no meaning on its own — its meaning is the question it
answers. Embedding it produces a vector near every other agreeable noise in the
corpus, which is worse than useless: it takes up a retrieval slot a real answer
could have had. But when someone asks *where did I say that*, the message is
exactly what they want pointed at.

So this does not replace per-message embeddings. It adds a coarser tier beside
them:

* **message** — pinpoint. Good for "find the message where…".
* **window**  — thematic. Good for "what have I been working on", which is a
  question about a stretch of conversation and cannot be answered by any single
  line in it.

The two chat shapes need different windows, because their turn structure is
different:

* **AI chat** has an obvious semantic unit — a person asks, the assistant
  answers. An *exchange* is one user turn plus the replies to it. Splitting that
  pair loses the question the answer belongs to.
* **Human chat** has no such structure. People send five messages in a row, then
  nothing for six hours. The unit is a *burst*: whatever was said in one sitting,
  bounded by a gap in time.

Both are additionally bounded by a character budget, because an unbounded window
drifts across topics and embeds to their average, which resembles none of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Chat kinds this module knows how to window.
KIND_AI_CHAT = "ai_chat"
KIND_HUMAN_CHAT = "human_chat"

# A window past this many characters is drifting across topics; embedding it
# averages them into something that matches none of them well. Roughly a couple
# of thousand tokens, matching the `search_text` column's own 2000-char cap.
MAX_WINDOW_CHARS = 6000

# A person's messages inside one sitting belong together. Past this, they are
# answering something else — most likely something that happened off-screen.
HUMAN_BURST_GAP_SECONDS = 30 * 60

# An exchange with only a bare acknowledgement in it is not worth its own
# vector; it is the case that motivated windowing in the first place.
MIN_WINDOW_CHARS = 80

# Roles that count as "the person asking". Sources disagree on the label.
_USER_ROLES = frozenset({"user", "human", "owner", "me"})


@dataclass
class ConversationWindow:
    """One embeddable stretch of a conversation."""

    conversation_id: str
    kind: str
    text: str
    message_ids: List[str] = field(default_factory=list)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    turn_count: int = 0

    @property
    def window_id(self) -> str:
        """Stable within a conversation, so a re-run overwrites rather than duplicates."""
        first = self.message_ids[0] if self.message_ids else "empty"
        return f"{self.conversation_id}:w:{first}"


def _role(msg: Dict[str, Any]) -> str:
    for key in ("sender_type", "role", "author_role", "speaker"):
        val = str(msg.get(key) or "").strip().lower()
        if val:
            return val
    return ""


def _is_user_turn(msg: Dict[str, Any]) -> bool:
    return _role(msg) in _USER_ROLES


def _content(msg: Dict[str, Any]) -> str:
    for key in ("content", "text", "body", "message"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _timestamp(msg: Dict[str, Any]) -> Optional[datetime]:
    for key in ("event_at", "created_at", "timestamp", "sent_at"):
        raw = msg.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, (int, float)):
            try:
                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _speaker_label(msg: Dict[str, Any], kind: str) -> str:
    role = _role(msg)
    if kind == KIND_AI_CHAT:
        return "Me" if _is_user_turn(msg) else "Assistant"
    if _is_user_turn(msg):
        return "Me"
    name = str(msg.get("sender_name") or msg.get("author") or "").strip()
    return name or "Them"


def _render(messages: Sequence[Dict[str, Any]], kind: str, title: Optional[str]) -> str:
    """The text actually embedded: who said what, under what heading.

    The title is included because it is the cheapest topic label the corpus
    carries, and because a window that says only "yes, that works" is
    unrecoverable without it.
    """
    lines: List[str] = []
    if title:
        lines.append(f"Conversation: {title}")
    for msg in messages:
        body = _content(msg)
        if not body:
            continue
        lines.append(f"{_speaker_label(msg, kind)}: {body}")
    return "\n".join(lines)


def _finish(
    batch: List[Dict[str, Any]],
    *,
    conversation_id: str,
    kind: str,
    title: Optional[str],
) -> Optional[ConversationWindow]:
    text = _render(batch, kind, title)
    if len(text) < MIN_WINDOW_CHARS:
        return None
    stamps = [t for t in (_timestamp(m) for m in batch) if t is not None]
    return ConversationWindow(
        conversation_id=conversation_id,
        kind=kind,
        text=text[:MAX_WINDOW_CHARS],
        message_ids=[str(m.get("message_id") or m.get("record_id") or "") for m in batch],
        started_at=min(stamps).isoformat() if stamps else None,
        ended_at=max(stamps).isoformat() if stamps else None,
        turn_count=len(batch),
    )


def _ai_chat_windows(
    messages: Sequence[Dict[str, Any]], conversation_id: str, title: Optional[str]
) -> List[ConversationWindow]:
    """One window per exchange: a user turn and the replies to it.

    A new window opens on a user turn, because that is where a new question
    starts. Assistant turns join the question they answer rather than starting
    anything — splitting them apart is what leaves an answer with no question.
    """
    windows: List[ConversationWindow] = []
    batch: List[Dict[str, Any]] = []

    def flush() -> None:
        if not batch:
            return
        win = _finish(batch, conversation_id=conversation_id, kind=KIND_AI_CHAT, title=title)
        if win:
            windows.append(win)
        batch.clear()

    for msg in messages:
        if not _content(msg):
            continue
        starts_new = _is_user_turn(msg) and batch
        too_long = sum(len(_content(m)) for m in batch) > MAX_WINDOW_CHARS
        if starts_new or too_long:
            flush()
        batch.append(msg)
    flush()
    return windows


def _human_chat_windows(
    messages: Sequence[Dict[str, Any]], conversation_id: str, title: Optional[str]
) -> List[ConversationWindow]:
    """One window per burst: whatever was said in a sitting.

    Human chat has no question/answer shape to lean on — people send five lines
    in a row and then nothing until tomorrow. Time is the only reliable seam.
    """
    windows: List[ConversationWindow] = []
    batch: List[Dict[str, Any]] = []
    previous: Optional[datetime] = None

    def flush() -> None:
        if not batch:
            return
        win = _finish(batch, conversation_id=conversation_id, kind=KIND_HUMAN_CHAT, title=title)
        if win:
            windows.append(win)
        batch.clear()

    for msg in messages:
        if not _content(msg):
            continue
        stamp = _timestamp(msg)
        gap_broken = (
            previous is not None
            and stamp is not None
            and (stamp - previous).total_seconds() > HUMAN_BURST_GAP_SECONDS
        )
        too_long = sum(len(_content(m)) for m in batch) > MAX_WINDOW_CHARS
        if batch and (gap_broken or too_long):
            flush()
        batch.append(msg)
        if stamp is not None:
            previous = stamp
    flush()
    return windows


def build_windows(
    messages: Iterable[Dict[str, Any]],
    *,
    kind: str,
    conversation_id: Optional[str] = None,
    title: Optional[str] = None,
) -> List[ConversationWindow]:
    """Group one conversation's messages into embeddable windows.

    Messages are taken in the order given; callers order by time. An unknown
    ``kind`` falls back to burst windowing, which needs no turn structure and so
    cannot be wrong about one that is not there.
    """
    ordered = [m for m in messages if isinstance(m, dict)]
    if not ordered:
        return []
    conv = str(
        conversation_id
        or ordered[0].get("conversation_id")
        or ordered[0].get("thread_id")
        or "unknown"
    )
    if kind == KIND_AI_CHAT:
        return _ai_chat_windows(ordered, conv, title)
    return _human_chat_windows(ordered, conv, title)
