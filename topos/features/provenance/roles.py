"""Record role: THE single source of truth for whose words a canonical row holds.

PLAN_PROVENANCE_SPLIT P1.2. Precedence-ordered vocabulary (plan §3.1, [L1]):

    authored > addressed > participated > observed > ambient

- ``authored``   — the owner typed/wrote it (P4 "sent by me"); the only role
                   that licenses belief/goal/self-fact attribution.
- ``addressed``  — directed at the owner (assistant replies in the owner's own
                   AI chats). Retrievable memory, never identity-eligible.
- ``participated`` — others' messages in threads the owner actively writes in.
                   v1 NOTE: the participated-vs-observed heuristic is
                   explicitly deferred (plan §Explicitly deferred); v1 returns
                   ``observed`` for all non-self conversation rows.
- ``observed``   — others' messages the owner merely reads.
- ``ambient``    — no conversational relationship at all: browser page text,
                   feeds, system prompts. The engagement modifier
                   (:func:`is_engaged`) upgrades an ambient item to evidence
                   of *interest* — never of belief.

Sender truth per family (attribution audit, Appendix A):
- ai_chat_messages: ``sender_type`` is authoritative ('human'/'user' = owner;
  'assistant' = model output; 'system' = scaffolding).
- conversation_messages: ``sender_type`` is NOT reliable (the messenger mapper
  writes 'human' for other people too); owner identity lives ONLY in
  ``is_from_self`` / ``sender_id == 'self'``.
- journal/profile tables: authored by construction.
- browser/activity rows: ambient; highlights/stars are the engagement modifier.
- transcript_segments / transcripts / transcript_speakers: ambient by table.
  The engine cannot tell if the owner spoke, sat in the room, or only
  listened. Connector ``is_self`` / ``participation_mode`` fields are dropped
  at parse; an owner tag after ingest is the only raise path.

Unknown/ambiguous rows fail toward the LESS-attributing role (observed or
ambient), never authored — belief extraction must not guess.

Pure module: stdlib only, no engine imports, no I/O.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

ROLE_AUTHORED = "authored"
ROLE_ADDRESSED = "addressed"
ROLE_PARTICIPATED = "participated"
ROLE_OBSERVED = "observed"
ROLE_AMBIENT = "ambient"

# Precedence order, most- to least-attributing.
ROLES = (
    ROLE_AUTHORED,
    ROLE_ADDRESSED,
    ROLE_PARTICIPATED,
    ROLE_OBSERVED,
    ROLE_AMBIENT,
)

# Canonical ai_chat_messages stores 'human' for the owner (chatgpt_parser maps
# role 'user' -> 'human'); 'user' survives in legacy rows and tests.
_AI_CHAT_OWNER_SENDERS = frozenset({"human", "user"})

# Tables whose rows are the owner's own writing by construction.
_AUTHORED_BY_CONSTRUCTION_TABLES = frozenset({"journal_entries", "profile_records"})

# Browser/feed activity families: exposure, not expression.
_AMBIENT_TABLES = frozenset({
    "activity_events",
    "browser_visits",
    "browser_events",
    "transcripts",
    "transcript_speakers",
    "transcript_segments",
})

# Keys that mark a transcript family row when ``_table`` is missing.
_TRANSCRIPT_MARKER_KEYS = ("segment_id", "start_sec", "asr_quality", "participation_mode")

# Keys that only conversation_messages rows carry (presence check — sqlite3.Row
# dicts keep NULL columns as None). sender_type cannot tell the message
# families apart: the messenger mapper writes 'human' for other people too.
_CONVERSATION_MARKER_KEYS = (
    "is_from_self",
    "event_type",
    "message_type",
    "reply_to_message_id",
)

# Keys that mark a row as message-shaped at all (for the fail-less fallback:
# message-shaped unknowns are someone's speech -> observed; everything else
# is content the owner was merely exposed to -> ambient).
_MESSAGE_MARKER_KEYS = ("sender_type", "sender_id", "is_from_self", "conversation_id")

# Engagement ladder (plan [L4]): Annotate/Retain-grade browser events.
_ENGAGED_ACTIVITY_TYPES = frozenset({"browser_highlight", "highlight", "star", "star_page"})
_ENGAGED_METADATA_KEYS = ("highlight", "selected_text", "selectedText")

# Posture values (mirror sources.definitions.POSTURE_VALUES; duplicated so this
# stays a pure stdlib module with no engine imports).
POSTURE_PERSONAL = "personal"
POSTURE_MIXED = "mixed"
POSTURE_AMBIENT = "ambient"

# When a source is flagged ambient posture (background-noise the owner marked as
# not-theirs), its rows can never be belief-grade: any more-attributing role is
# capped down. 'authored'/'addressed'/'participated' collapse to 'observed'
# (still recallable, never identity-eligible); 'observed'/'ambient' stay put.
_AMBIENT_POSTURE_CAP = ROLE_OBSERVED


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _row_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    meta = row.get("metadata_json")
    if meta is None:
        meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _is_self_row(row: Dict[str, Any]) -> bool:
    """conversation_messages owner check: is_from_self / sender_id=='self'."""
    if row.get("is_from_self") in (1, True, "1"):
        return True
    return _norm(row.get("sender_id")) == "self"


def _infer_table(row: Dict[str, Any]) -> str:
    """Best-effort table inference for rows without a ``_table`` stamp.

    Deliberately stricter than StatsEngine._table_for_row: that heuristic
    routes rows to fold tables; this one feeds *attribution*, so sender_type
    'human' alone must never resolve to ai_chat_messages (the messenger
    mapper writes 'human' for other people — resolving it would flip an
    observed row to authored). Unresolved rows fall to the generic
    fail-less-attributing branch of record_role.
    """
    if row.get("record_type") is not None and row.get("organization") is not None:
        return "profile_records"
    if row.get("entry_at") is not None or row.get("mood_tag") is not None:
        return "journal_entries"
    if row.get("activity_type") is not None or row.get("url") is not None:
        return "activity_events"
    if any(key in row for key in _TRANSCRIPT_MARKER_KEYS) or row.get("transcript_id") is not None:
        if row.get("segment_id") is not None or row.get("start_sec") is not None:
            return "transcript_segments"
        return "transcripts"
    if any(key in row for key in _CONVERSATION_MARKER_KEYS):
        return "conversation_messages"
    # 'assistant'/'system' only ever appear in ai_chat rows — unambiguous.
    if _norm(row.get("sender_type")) in ("assistant", "system"):
        return "ai_chat_messages"
    return ""


def is_engaged(row: Dict[str, Any]) -> bool:
    """Engagement modifier for ambient rows (highlight/star = Annotate/Retain).

    An engaged ambient item becomes evidence of *interest* (expression-grade),
    never of *belief* — the highlighted words remain the page author's.
    """
    if _norm(row.get("activity_type")) in _ENGAGED_ACTIVITY_TYPES:
        return True
    if _norm(row.get("event_type")) in _ENGAGED_ACTIVITY_TYPES:
        return True
    meta = _row_metadata(row)
    return any(str(meta.get(key) or "").strip() for key in _ENGAGED_METADATA_KEYS)


def _base_role(row: Dict[str, Any], *, table: str, posture: str) -> str:
    """Per-row role from table rules, sender fields, and the posture tiebreaker.

    This is the pre-P1.4 computation: ``posture`` here is used ONLY to break
    ties for rows the table/sender rules cannot resolve (a personal-posture
    note has no sender fields but is owner-authored by construction; an
    ambient-posture feed row is exposure). The P1.4 posture CAP is applied on
    top of this in :func:`record_role`.
    """
    table = (
        _norm(table)
        or _norm(row.get("_table") or row.get("canonical_table"))
        or _infer_table(row)
    )
    sender_type = _norm(row.get("sender_type"))

    if table == "ai_chat_messages":
        if sender_type in _AI_CHAT_OWNER_SENDERS:
            return ROLE_AUTHORED
        if sender_type == "assistant":
            return ROLE_ADDRESSED
        # 'system' and anything unrecognized: scaffolding, not speech to keep.
        return ROLE_AMBIENT

    if table == "conversation_messages":
        # participated-vs-observed heuristic explicitly deferred (plan);
        # non-self conversation rows are observed in v1.
        return ROLE_AUTHORED if _is_self_row(row) else ROLE_OBSERVED

    if table in _AUTHORED_BY_CONSTRUCTION_TABLES:
        return ROLE_AUTHORED

    if table in _AMBIENT_TABLES:
        # Engagement upgrades *interest* eligibility (is_engaged), not role.
        return ROLE_AMBIENT

    # ---- no table context: generic sender fallbacks (mirror facts/extract) --
    if sender_type == "assistant":
        return ROLE_ADDRESSED
    if sender_type == "system":
        return ROLE_AMBIENT
    if sender_type == "user":
        return ROLE_AUTHORED  # 'user' only ever meant the owner (legacy ai_chat)
    if _is_self_row(row):
        return ROLE_AUTHORED

    if posture == POSTURE_PERSONAL:
        return ROLE_AUTHORED  # journal/resume-grade source: owner by construction
    if posture == POSTURE_AMBIENT:
        return ROLE_AMBIENT

    # Unknown/ambiguous: fail toward the less-attributing role, never authored.
    if any(key in row for key in _MESSAGE_MARKER_KEYS):
        return ROLE_OBSERVED  # someone's speech, not provably the owner's
    return ROLE_AMBIENT


def _apply_posture_cap(role: str, posture: str) -> str:
    """P1.4 posture OVERRIDE: cap a computed role by the source posture.

    - ``ambient``  — the load-bearing rule: an ambient-posture source is
      background-noise the owner marked as NOT-theirs, so NO row from it may be
      belief-grade. Any role above ``observed`` (authored/addressed/
      participated) is capped to ``observed``; ``observed``/``ambient`` are
      unchanged. This is what makes a per-connector 'background-noise' toggle
      actually strip belief eligibility (goals, self-facts, sent-stats).
    - ``personal`` — keep the computed role (a personal source's owner rows
      stay authored; a stray assistant/other row keeps its lesser role).
    - ``mixed`` (and anything else) — current per-row behaviour, no cap.
    """
    if posture == POSTURE_AMBIENT:
        if role in (ROLE_AUTHORED, ROLE_ADDRESSED, ROLE_PARTICIPATED):
            return _AMBIENT_POSTURE_CAP  # observed
        return role  # observed / ambient stay put
    return role


def record_role(
    row: Dict[str, Any],
    *,
    table: str,
    posture: Optional[str] = None,
) -> str:
    """Compute the record role for a canonical row. v1 rules (plan §3.1) + the
    P1.4 posture override.

    ``table`` is the canonical table the row belongs to; pass "" when unknown
    (the row's own ``_table``/``canonical_table`` stamp, then shape inference,
    take over). ``posture`` is the EFFECTIVE source posture (personal|mixed|
    ambient) — resolve it with ``sources.registry.effective_posture`` so the
    owner's per-connector override is honoured. It plays two roles:
      1. a tiebreaker for rows the table/sender rules cannot resolve, and
      2. (P1.4) an ``ambient`` posture caps EVERY row's role at ``observed`` —
         an ambient source can never mint beliefs, even for an ``is_from_self``
         row — while ``personal``/``mixed`` keep the computed role.
    """
    posture = _norm(posture)
    role = _base_role(row, table=table, posture=posture)
    return _apply_posture_cap(role, posture)


def owner_authored(row: Dict[str, Any], *, table: str = "", posture: Optional[str] = None) -> bool:
    """Did the owner type this row? Exactly record_role(...) == 'authored'."""
    return record_role(row, table=table, posture=posture) == ROLE_AUTHORED
