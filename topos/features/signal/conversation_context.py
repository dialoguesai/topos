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
- Asking twice costs twice: a conversation the model declines to label is
  *retired* with an ``unclear:n<k>`` marker recording how many excerpts were
  judged. Without it this pass re-asked the same unlabelable conversations on
  every enrichment batch forever — ~20 model calls a batch for zero progress.
  A retired row returns only once it has grown new excerpts to judge, and
  since ``_first_messages`` caps at ``_MESSAGES_PER_CONVERSATION`` a
  conversation at that cap never returns at all.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ...storage.db.write_gate import batched_writes

logger = logging.getLogger("topos.features.signal.conversation_context")

_MAX_CONVERSATIONS_PER_PASS = 20
_MESSAGES_PER_CONVERSATION = 8
_SNIPPET_CHARS = 240

# "Asked, no verdict" marker. Deliberately NOT `auto:`-prefixed: the UI reads
# that prefix as "the classifier assigned this tag" and renders an `auto` badge
# with the model name. Nothing is assigned here — `context_tag` stays NULL.
_UNCLEAR_PREFIX = "unclear:n"

# The model never rendered a verdict — no engine, a failed call, or a
# non-completed result. Distinct from None, which means the model answered and
# the answer was not work/personal. Only the latter retires a conversation:
# retiring on an engine outage would bury rows that were never really judged.
_ENGINE_UNAVAILABLE = object()

_PROMPT = (
    "You label chat conversations for the person who owns this archive. "
    "Based on the message excerpts, is this conversation about their work "
    "(colleagues, projects, business) or their personal life (family, friends, "
    "hobbies, logistics)? Answer with exactly one word: work or personal. "
    "If genuinely unclear, answer: unclear."
)


def _unclear_marker(model: str, seen: int) -> str:
    """Marker for 'asked, no verdict', recording how many excerpts were judged."""
    return f"{_UNCLEAR_PREFIX}{seen}:{model or 'injected'}"


def _prior_unclear_count(source: Optional[str]) -> Optional[int]:
    """Excerpt count from a prior unclear verdict; None if never asked."""
    text = str(source or "")
    if not text.startswith(_UNCLEAR_PREFIX):
        return None
    digits = text[len(_UNCLEAR_PREFIX) :].split(":", 1)[0]
    try:
        return int(digits)
    except ValueError:
        # Malformed marker — treat as "nothing judged yet" so it retries once
        # and gets rewritten with a well-formed count.
        return 0


# (conversation_id, dataset_id, excerpts judged by a prior pass or None)
_Candidate = Tuple[str, str, Optional[int]]


def _untagged_conversations(conn: sqlite3.Connection, limit: int) -> List[_Candidate]:
    """Never-asked conversations, newest first."""
    rows = conn.execute(
        "SELECT conversation_id, dataset_id FROM conversations "
        "WHERE context_tag IS NULL AND (context_tag_source IS NULL OR context_tag_source='') "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [(str(r[0]), str(r[1]), None) for r in rows]


def _unclear_conversations(conn: sqlite3.Connection, limit: int) -> List[_Candidate]:
    """Previously-unclear conversations, newest first.

    Only ever fills budget left over by never-asked rows, so a backlog of
    unlabelable conversations can never crowd out genuinely new work.
    """
    rows = conn.execute(
        "SELECT conversation_id, dataset_id, context_tag_source FROM conversations "
        "WHERE context_tag IS NULL AND context_tag_source LIKE ? "
        "ORDER BY created_at DESC LIMIT ?",
        (f"{_UNCLEAR_PREFIX}%", limit),
    ).fetchall()
    return [(str(r[0]), str(r[1]), _prior_unclear_count(r[2])) for r in rows]


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


def _classify_with_model(
    model: str,
    excerpts: List[str],
    conn: Optional[sqlite3.Connection] = None,
    *,
    provider: str = "ollama",
) -> Any:
    """'work' | 'personal' | None (answered, unclear) | ``_ENGINE_UNAVAILABLE``."""
    from ...config.conversation_context_llm import resolve_context_llm_params
    from ...config.node_function_providers import engine_provider_for
    from ...config.settings import settings  # noqa: F401 — engine client env
    from ...engine.client import get_engine_client_or_local
    from ...engine.tasks import ModelRequest, ProcessingTask, RequestedBy

    # The Ollama adapter hard-codes think=False for `query_inference`, so a pack
    # that turned thinking ON for `classify` was honoured by facts extraction
    # and silently dropped here — one model running at two different settings.
    binding = resolve_context_llm_params(conn, model)
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
        model_request=ModelRequest(
            provider=engine_provider_for(provider),
            model=model,
            thinking=binding.thinking if binding else None,
            context=binding.context if binding else None,
            max_tokens=binding.max_tokens if binding else None,
        ),
        # Piggybacked on dimension_summary / enrichment — not an interactive UI ask.
        requested_by=RequestedBy(origin="ingestion_pipeline"),
    )
    try:
        result = get_engine_client_or_local(None).run(task)
    except Exception as exc:  # noqa: BLE001 — classification is best-effort
        logger.debug("conversation-context model call failed: %s", exc)
        return _ENGINE_UNAVAILABLE
    if getattr(result, "status", "") != "completed":
        return _ENGINE_UNAVAILABLE
    out = result.output or {}
    return _parse_label(out.get("answer") or out.get("output"))


def classify_untagged_conversations(
    conn: sqlite3.Connection,
    *,
    limit: int = _MAX_CONVERSATIONS_PER_PASS,
    classify=None,
) -> Dict[str, int]:
    """Tag untagged conversations; returns per-pass counters.

    ``classify`` (excerpts -> 'work'|'personal'|None) is injectable for tests;
    default resolves the configured local model and calls the engine.

    Counters: ``examined`` conversations the model rendered a verdict on,
    ``tagged`` newly labelled, ``retired`` marked unclear so they stop being
    re-asked, ``unchanged`` skipped without a model call because a prior pass
    already declined the same excerpts.
    """
    from ..signal.dimension_registry import is_signal_dimension  # noqa: F401 — import guard parity

    examined = tagged = retired = unchanged = 0
    candidates = _untagged_conversations(conn, limit)
    if len(candidates) < limit:
        candidates += _unclear_conversations(conn, limit - len(candidates))
    if not candidates:
        return {"examined": 0, "tagged": 0, "retired": 0, "unchanged": 0}

    model = ""
    if classify is None:
        from ...config.conversation_context_llm import resolve_context_llm_request
        from ...config.settings import settings

        provider, model = resolve_context_llm_request(settings, conn)
        if not model:
            return {"examined": 0, "tagged": 0, "retired": 0, "unchanged": 0}

        def classify(excerpts: List[str]) -> Any:  # noqa: F811
            return _classify_with_model(model, excerpts, conn, provider=provider)

    # Classification is a model call per conversation — collect the verdicts
    # first, then land them in one short gated write pass.
    labeled: List[Tuple[str, str, str]] = []
    unclear: List[Tuple[str, str, int]] = []
    for conversation_id, dataset_id, prior in candidates:
        excerpts = _first_messages(conn, conversation_id)
        if not excerpts:
            continue
        if prior is not None and len(excerpts) <= prior:
            # The same excerpts a prior pass already declined to label. Asking
            # again spends a model call to reach the identical answer.
            unchanged += 1
            continue
        label = classify(excerpts)
        if label is _ENGINE_UNAVAILABLE:
            # No verdict is not a verdict of "unclear" — never retire on an
            # engine problem. Abandon the pass; the rest would fail the same
            # way, and the next batch retries from a clean slate.
            logger.debug("conversation-context pass stopped: engine unavailable")
            break
        examined += 1
        if label in ("work", "personal"):
            labeled.append((str(label), conversation_id, dataset_id))
        else:
            unclear.append((conversation_id, dataset_id, len(excerpts)))
    if not labeled and not unclear:
        return {
            "examined": examined,
            "tagged": 0,
            "retired": 0,
            "unchanged": unchanged,
        }
    # Both guards below admit a prior unclear marker so a conversation that has
    # since grown can be labelled — and still exclude 'owner', which always wins.
    with batched_writes(conn):
        for label, conversation_id, dataset_id in labeled:
            cur = conn.execute(
                "UPDATE conversations SET context_tag=?, context_tag_source=?, "
                "updated_at=datetime('now') "
                "WHERE conversation_id=? AND dataset_id=? AND context_tag IS NULL "
                "AND (context_tag_source IS NULL OR context_tag_source='' "
                "     OR context_tag_source LIKE ?)",
                (
                    label,
                    f"auto:{model or 'injected'}",
                    conversation_id,
                    dataset_id,
                    f"{_UNCLEAR_PREFIX}%",
                ),
            )
            tagged += cur.rowcount
        for conversation_id, dataset_id, seen in unclear:
            cur = conn.execute(
                "UPDATE conversations SET context_tag_source=?, "
                "updated_at=datetime('now') "
                "WHERE conversation_id=? AND dataset_id=? AND context_tag IS NULL "
                "AND (context_tag_source IS NULL OR context_tag_source='' "
                "     OR context_tag_source LIKE ?)",
                (
                    _unclear_marker(model, seen),
                    conversation_id,
                    dataset_id,
                    f"{_UNCLEAR_PREFIX}%",
                ),
            )
            retired += cur.rowcount
    return {
        "examined": examined,
        "tagged": tagged,
        "retired": retired,
        "unchanged": unchanged,
    }
