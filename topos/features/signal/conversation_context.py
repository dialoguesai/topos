"""Auto-categorize conversations as work/personal with the local model.

Runs inside the enrichment pass (piggybacked on dimension_summary, same
pattern as the badge award pass): for each conversation that has no tag and no
owner decision, read the first few ingested messages and ask the configured
local model for one word — work or personal. Writes ``context_tag`` with
``context_tag_source='auto:<model>'``.

Invariants:
- The owner always wins: rows whose ``context_tag_source`` is ``'owner'`` are
  never touched, including owner-cleared (tag NULL, source 'owner').
- Fail-silent: no model, an unparseable answer, or an engine error leaves the
  conversation untagged — never a guessed default.
- Message content goes to the LOCAL model only; nothing leaves the node.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("topos.features.signal.conversation_context")

_MAX_CONVERSATIONS_PER_PASS = 20
_MESSAGES_PER_CONVERSATION = 8
_SNIPPET_CHARS = 240

_PROMPT = (
    "You label chat conversations for the person who owns this archive. "
    "Based on the message excerpts, is this conversation about their work "
    "(colleagues, projects, business) or their personal life (family, friends, "
    "hobbies, logistics)? Answer with exactly one word: work or personal. "
    "If genuinely unclear, answer: unclear."
)


def _untagged_conversations(conn: sqlite3.Connection, limit: int) -> List[Tuple[str, str]]:
    rows = conn.execute(
        "SELECT conversation_id, dataset_id FROM conversations "
        "WHERE context_tag IS NULL AND (context_tag_source IS NULL OR context_tag_source='') "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _first_messages(conn: sqlite3.Connection, conversation_id: str) -> List[str]:
    rows = conn.execute(
        "SELECT content FROM conversation_messages "
        "WHERE conversation_id=? AND content IS NOT NULL AND content != '' "
        "ORDER BY event_at ASC LIMIT ?",
        (conversation_id, _MESSAGES_PER_CONVERSATION),
    ).fetchall()
    return [str(r[0])[:_SNIPPET_CHARS] for r in rows]


def _parse_label(answer: Any) -> Optional[str]:
    text = str(answer or "").strip().lower()
    # Exact-word scan; "unclear" (or anything else) → no tag.
    has_work = "work" in text.split() or text == "work"
    has_personal = "personal" in text.split() or text == "personal"
    if has_work and not has_personal:
        return "work"
    if has_personal and not has_work:
        return "personal"
    return None


def _classify_with_model(model: str, excerpts: List[str]) -> Optional[str]:
    from ...config.settings import settings  # noqa: F401 — engine client env
    from ...engine.client import get_engine_client_or_local
    from ...engine.tasks import ModelRequest, ProcessingTask, RequestedBy

    task = ProcessingTask(
        id="conversation_context",
        type="query_inference",
        subtype="query_inference",
        source_id="conversation_context",
        record_ids=[],
        input={
            "query": _PROMPT,
            "context": "\n---\n".join(excerpts),
        },
        model_request=ModelRequest(provider="ollama", model=model),
        # Piggybacked on dimension_summary / enrichment — not an interactive UI ask.
        requested_by=RequestedBy(origin="ingestion_pipeline"),
    )
    try:
        result = get_engine_client_or_local(None).run(task)
    except Exception as exc:  # noqa: BLE001 — classification is best-effort
        logger.debug("conversation-context model call failed: %s", exc)
        return None
    if getattr(result, "status", "") != "completed":
        return None
    out = result.output or {}
    return _parse_label(out.get("answer") or out.get("output"))


def classify_untagged_conversations(
    conn: sqlite3.Connection,
    *,
    limit: int = _MAX_CONVERSATIONS_PER_PASS,
    classify=None,
) -> Dict[str, int]:
    """Tag untagged conversations; returns {examined, tagged}.

    ``classify`` (excerpts -> 'work'|'personal'|None) is injectable for tests;
    default resolves the configured local model and calls the engine.
    """
    from ..signal.dimension_registry import is_signal_dimension  # noqa: F401 — import guard parity

    examined = tagged = 0
    candidates = _untagged_conversations(conn, limit)
    if not candidates:
        return {"examined": 0, "tagged": 0}

    model = ""
    if classify is None:
        from ...config.conversation_context_llm import resolve_context_llm_model
        from ...config.settings import settings

        model = resolve_context_llm_model(settings, conn)
        if not model:
            return {"examined": 0, "tagged": 0}

        def classify(excerpts: List[str]) -> Optional[str]:  # noqa: F811
            return _classify_with_model(model, excerpts)

    for conversation_id, dataset_id in candidates:
        excerpts = _first_messages(conn, conversation_id)
        if not excerpts:
            continue
        examined += 1
        label = classify(excerpts)
        if label not in ("work", "personal"):
            continue
        conn.execute(
            "UPDATE conversations SET context_tag=?, context_tag_source=?, "
            "updated_at=datetime('now') "
            "WHERE conversation_id=? AND dataset_id=? "
            "AND context_tag IS NULL AND (context_tag_source IS NULL OR context_tag_source='')",
            (label, f"auto:{model or 'injected'}", conversation_id, dataset_id),
        )
        tagged += 1
    conn.commit()
    return {"examined": examined, "tagged": tagged}
