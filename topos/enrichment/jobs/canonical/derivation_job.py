"""DerivationJob (W2.3): ontology-pack fact derivation over canonical batches.

Per (record, enabled pack): lexical prefilter -> shadow-8 prompt -> extraction
model (qwen3.5-class; recall) -> parse with A1/A2 guards -> A4 verifier
(27B-class; precision, selection-shaped) -> DerivationWriter (A3 routing,
revision ladder, event-identity keying).

Model split is MEASURED, not aesthetic (2026-08-26 battery, 32 records):
extractor alone recurs 19/23 junk classes; verifier-as-extractor loses half the
gold; extract+verify hits ~1/23 junk while keeping recall. See
derivation-packs/pilot/w1_report.md.

Verification policy: TOPOS_DERIVATION_VERIFY=required (default) — if the
verifier model is unavailable the batch DEFERS rather than writing unverified
owner-facts; the junk gate was earned WITH the verifier in the loop. "off"
exists for explicit opt-out (assertions then flagged verifier_status=skipped).

Occurrence dates: an episodic assertion with no stated date stays UNDATED —
defaulting to the record date is how one firing became six events (a July
retelling minted a phantom July firing). The writer's event-identity keying
merges undated retellings into the original.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ....core.state import get_db_connection

logger = logging.getLogger("topos.enrichment.jobs.derivation")


def derivation_enabled() -> bool:
    return os.environ.get("TOPOS_DERIVATION", "on").strip().lower() not in (
        "0", "false", "off", "no")


def _verify_mode() -> str:
    return os.environ.get("TOPOS_DERIVATION_VERIFY", "required").strip().lower()


#: The tables that carry an ``actor_role`` column (actor_role_v1). NULL there means "not
#: yet classified", not "observed": the migration backfilled once, and the legacy
#: conversation upsert never writes the column, so every iMessage synced since reads
#: NULL — 37,198 of the owner's own messages on the live node, 29 Sep 2026.
_ROLE_COLUMN_TABLES = frozenset({"conversation_messages", "ai_chat_messages"})


def _record_role_for(row: Dict[str, Any], table: str, posture_for: Callable[[Dict[str, Any]], str]) -> str:
    """The row's stored role, else the provenance role for an unclassified message.

    The fallback is `record_role` with the source's effective posture, the rule
    `maintenance._record_role_map` already applies: authored only when the row's own
    owner flag says so (`is_from_self` / `sender_id='self'`, or an AI chat's human
    turn), and never above observed for a source the owner marked ambient. Everyone
    else's message stays observed. The column is not backfilled instead: it is part
    of the native provenance link identity, so rewriting it would unlink every
    permitted message.
    """
    stored = str(row.get("actor_role") or "").strip()
    if stored:
        return stored
    if "journal" in table:
        return "authored"
    if table in _ROLE_COLUMN_TABLES:
        from ....features.provenance.roles import record_role
        return record_role(row, table=table, posture=posture_for(row))
    return "observed"


def _owner_reaction(role: str, table: str, row: Dict[str, Any], text: Any) -> bool:
    """True for a reaction ("tapback") the owner sent: a record no pack reads.

    Messages stores a reaction as a message from whoever reacted, and its text is the
    message reacted to, in quotes after "Loved", "Liked" and the rest. The sender flag
    makes it `authored`, so an owner-only pack read the other person's words as the
    owner's: measured on one node, 11 Nov 2026, 82 of the owner's 1,910 texts since
    1 July were reactions, 47 reached the model for one pack, and 2 of them became 3
    stored facts, one of them the other person's thanks filed as the owner's gratitude.

    The record is dropped, not demoted to `observed`: the message reacted to is a record
    of its own, read there with its real sender, and a second copy with no speaker label
    would only be read again by every pack that takes observed text. `row` is whatever
    carries the row's metadata (`features/facts/reactions.py`); a row with none is judged
    by its words. A contact's reaction is not touched here: it is `observed` already.
    """
    if role != "authored" or table != "conversation_messages":
        return False
    from ....features.facts.reactions import is_reaction
    return is_reaction(row, text)


def _with_stored_metadata(conn, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The batch's message rows, each with the metadata stored for it when it brought none.

    The lanes that feed a batch hand on text and sender; the reader's metadata, where the
    native reaction marker is, stays on the stored row (local sync, a reprocess reload). A
    row that already carries metadata is passed through as it is; the rest are copies, so
    the other jobs of the same batch see the list they were given. A database without the
    column, or a row not stored yet, leaves the row to be judged by its words."""
    def _bare(row):
        return (isinstance(row, dict)
                and str(row.get("_table") or row.get("canonical_table") or "") == "conversation_messages"
                and not any(row.get(key) for key in ("metadata_json", "_metadata", "metadata"))
                and row.get("message_id"))
    wanted = sorted({str(row["message_id"]) for row in rows if _bare(row)})
    stored: Dict[str, Any] = {}
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        try:
            stored.update((str(mid), meta) for mid, meta in conn.execute(
                f"SELECT message_id, metadata_json FROM conversation_messages"
                f" WHERE metadata_json IS NOT NULL AND message_id IN ({','.join('?' for _ in chunk)})",
                chunk).fetchall())
        except sqlite3.Error:
            return rows
    if not stored:
        return rows
    return [{**row, "metadata_json": stored[str(row["message_id"])]}
            if _bare(row) and str(row["message_id"]) in stored else row for row in rows]


def _row_to_record(row: Dict[str, Any], *,
                   posture_for: Callable[[Dict[str, Any]], str]) -> Optional[Dict[str, Any]]:
    text = str(row.get("content") or row.get("text") or row.get("title") or "")
    short = len(text.strip()) <= SHORT_RECORD_CHARS
    if short and not (text.strip() and _reads_short_records()):
        return None
    rid = str(row.get("record_id") or row.get("message_id") or row.get("entry_id")
              or row.get("event_id") or row.get("id") or "")
    if not rid:
        return None
    table = str(row.get("_table") or row.get("canonical_table") or "")
    role = _record_role_for(row, table, posture_for)
    if _owner_reaction(role, table, row, text):
        return None
    date = str(row.get("event_at") or row.get("entry_at") or row.get("occurred_at") or "")[:10]
    rec = {"record_id": rid, "table": table or "canonical", "text": text,
           "role": role, "date": date,
           "source_id": str(row.get("source_id") or "")}
    if short:
        if not (role == "authored" and table == "conversation_messages"):
            return None                      # only an owner's message can have a conversation's context
        rec["short"] = True
    return rec


#: A record this short ("Wow. Okay.", "Thank you!!") says too little alone. DR-62 reads one in a
#: context pack when its conversation gives it context; every other pack, and a context pack
#: without context, still never reads it.
SHORT_RECORD_CHARS = 15


def _reads_short_records() -> bool:
    """Whether any pack reads a short record at all: only while a context pack is on (DR-62)."""
    from ....features.derivation.template import context_packs
    return bool(context_packs())


#: DR-62: a context turn older than this before the owner's message belongs to another exchange.
CONTEXT_MAX_AGE_DAYS = 7
#: The messengers whose two-party conversations get context. Groups, AI chats, journals and
#: transcripts get none. DR-62 names iMessage and Signal, and Signal is left out here: its
#: reader stores no group marker, so a Signal group in which one other member has written
#: cannot be told from a two-party conversation (`_other_party` counts senders), and DR-62
#: gives a group none. Signal joins this set when a stored row can prove the conversation is
#: two-party, the way `chat_guid` does for Messages (`_sent_in_a_direct_chat`).
_CONTEXT_SOURCES = frozenset({"imessage"})
#: How many rows past the k turns one context read may look at to step over reactions.
CONTEXT_REACTION_SLACK = 12
#: How long one read of the owner's protections (the Off-limits guard, the excluded records,
#: each conversation's verdict) serves a batch. A backfill batch is model calls for minutes or
#: hours; a turn the owner excludes or protects meanwhile stops being read within this long.
CONTEXT_PROTECTIONS_MAX_AGE_S = 30.0


def _is_reaction(metadata: Any, text: Any) -> bool:
    """A reaction ("tapback") row: never a context turn, and never a message DR-62 gives context.

    Messages stores a reaction as a message from whoever reacted, and its text is the message
    reacted to, in quotes. Read as a turn it would repeat that message under the wrong speaker
    (the other person's words on an OWNER line when the owner reacted); read as the owner's
    message it would put the other person's words where the quote guard looks for the owner's.

    The detection is `features/facts/reactions.py`: the reader's marker in the row's metadata,
    and, once that module has `is_reaction`, also the fixed form Messages writes a reaction in,
    for a row stored with no metadata."""
    from ....features.facts import reactions
    row = {"metadata_json": metadata}
    by_marker_or_words = getattr(reactions, "is_reaction", None)
    if by_marker_or_words is not None:
        return bool(by_marker_or_words(row, text))
    return bool(reactions.quotes_another_message(row))


def _chat_style(metadata: Any) -> str:
    """The style of the chat a stored row names: "+" a group, "-" a direct chat, "" unknown.

    Messages names every chat `<service>;<style>;<identifier>`; the reader keeps that name as
    `chat_guid`. A row with no metadata, or none with a chat name (an older import), says
    nothing."""
    if isinstance(metadata, (str, bytes)) and metadata:
        try:
            metadata = _json.loads(metadata)
        except (TypeError, ValueError):
            return ""
    if not isinstance(metadata, dict):
        return ""
    parts = str(metadata.get("chat_guid") or "").split(";")
    return parts[1] if len(parts) >= 3 and parts[1] in ("+", "-") else ""


def _sent_in_a_group(metadata: Any) -> bool:
    """Whether a stored row's metadata says it was sent in a group chat.

    `_other_party` calls a conversation two-party when one other person has written in it, so
    a group in which only one other member has written so far would pass. One such row takes
    context away, whatever the other rows say."""
    return _chat_style(metadata) == "+"


def _sent_in_a_direct_chat(metadata: Any) -> bool:
    """Whether a stored row's metadata names a direct chat (`chat_guid` `<service>;-;<id>`).

    DR-62 gives context only on a positive sign: when none of the rows read for a message
    names a direct chat (no metadata, or no `chat_guid`), it gets none, because a group in which
    one other member has written cannot be told from it by counting senders. The sender count
    (`_other_party`) still has to agree."""
    return _chat_style(metadata) == "-"


def _aware(value: Any):
    import datetime as _dt
    try:
        parsed = _dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def _conversation_context(conn, rec: Dict[str, Any], k: int, *,
                          cache: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The k turns before an owner-written message in a two-party iMessage conversation, oldest
    first (DR-62, owner, 26 Oct 2026; Signal waits for a group marker, `_CONTEXT_SOURCES`). []
    for anything else, and on any error: no context is today's one-record reading, never a
    failed batch.

    Within CONTEXT_MAX_AGE_DAYS of the message, never a system row, never a row the owner
    excluded (`lifecycle.exclusions`), one tagged NSFW or one the Off-limits guard withholds
    from the owner's own processing (`for_own_processing`: a protected record, or text naming a
    protected entity). A conversation whose other person is Off-limits gives none. A withheld
    turn is dropped, not replaced by an older one. A reaction (`_is_reaction`), the owner's or
    the other person's, is not a turn: like a system row it is stepped over, and the k turns
    are the k messages before it. A message that is itself a reaction gets no context: it is
    not text the owner wrote. Each turn is labelled OWNER or with the
    sender's agreed contact name (`resolve_peer_identities`, "Contact" when none agrees), and
    carries its record id, which the fact keeps as a protection-only ref (`context_refs`).
    A conversation Messages itself calls a group gives none, whoever has written in it
    (`_sent_in_a_group`), and so does one in which neither the owner's message nor a row read
    for its turns names a direct chat (`_sent_in_a_direct_chat`: no metadata, no `chat_guid`).
    `cache` holds the guard, the exclusions and each conversation's
    verdict across a batch, re-read every CONTEXT_PROTECTIONS_MAX_AGE_S."""
    if k <= 0 or rec.get("role") != "authored" or rec.get("table") != "conversation_messages":
        return []
    try:
        return _read_conversation_context(conn, rec, k, cache if cache is not None else {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("derivation: no conversation context for %s (%s)", rec.get("record_id"), exc)
        return []


def _read_conversation_context(conn, rec, k, cache) -> List[Dict[str, Any]]:
    import datetime as _dt
    from ....features.derivation.template import CONTEXT_OWNER, CONTEXT_UNNAMED, is_diarisation_label
    rid = str(rec.get("record_id") or "")
    if "columns" not in cache:
        cache["columns"] = {str(r[1]) for r in conn.execute("PRAGMA table_info(conversation_messages)")}
    cols = cache["columns"]
    meta = "metadata_json" if "metadata_json" in cols else "NULL"
    found = conn.execute(f"SELECT conversation_id, event_at, source_id, {meta} FROM conversation_messages"
                         " WHERE message_id=?", (rid,)).fetchone()
    if not found or not found[0] or str(found[2] or "").lower() not in _CONTEXT_SOURCES:
        return []
    if _is_reaction(found[3], rec.get("text")) or _sent_in_a_group(found[3]):
        return []
    conv, at = str(found[0]), str(found[1] or "")
    when = _aware(at)
    if when is None:
        return []
    import time as _time
    if "guard" not in cache or _time.monotonic() - cache.get("protections_at", 0.0) > CONTEXT_PROTECTIONS_MAX_AGE_S:
        from ....features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
        from ....features.lifecycle.exclusions import excluded_record_ids
        from ....features.lifecycle.off_limits_view import for_own_processing
        cache["guard"] = BlackholeGuard(conn, caller_class=CallerClass.UNKNOWN, view=for_own_processing())
        cache["excluded"] = excluded_record_ids(conn)
        cache["conversations"] = {}
        cache["protections_at"] = _time.monotonic()
    guard = cache["guard"]
    if conv not in cache["conversations"]:
        cache["conversations"][conv] = _other_party(conn, conv, guard, is_diarisation_label)
    other = cache["conversations"][conv]
    if other is None:
        return []
    nsfw = "COALESCE(content_nsfw,0)" if "content_nsfw" in cols else "0"
    system = " AND COALESCE(message_type,'')<>'system'" if "message_type" in cols else ""
    rows = conn.execute(
        f"SELECT message_id, content, event_at, COALESCE(is_from_self,0), {nsfw}, {meta}"
        f" FROM conversation_messages"
        f" WHERE conversation_id=? AND message_id<>? AND (event_at < ? OR (event_at = ? AND message_id < ?))"
        f" AND content IS NOT NULL AND TRIM(content)<>''{system}"
        f" ORDER BY event_at DESC, message_id DESC LIMIT ?",
        (conv, rid, at, at, rid, int(k) + CONTEXT_REACTION_SLACK)).fetchall()
    if any(_sent_in_a_group(row[5]) for row in rows):
        cache["conversations"][conv] = None
        return []
    if not (_sent_in_a_direct_chat(found[3]) or any(_sent_in_a_direct_chat(row[5]) for row in rows)):
        return []                      # nothing stored names a direct chat: no positive sign
    # Newest first: the k messages before the owner's, stepping over reactions. Only then are
    # the withheld ones dropped, so a withheld turn is never replaced by an older one.
    last = [row[:5] for row in rows if not _is_reaction(row[5], row[1])][:int(k)]
    turns = []
    for mid, text, turn_at, is_self, flagged in reversed(last):
        then = _aware(turn_at)
        if then is None or when - then > _dt.timedelta(days=CONTEXT_MAX_AGE_DAYS):
            continue
        if str(mid) in cache["excluded"] or flagged:
            continue
        turns.append({"table": "conversation_messages", "record_id": str(mid), "content": str(text),
                      "owner": bool(is_self), "at": str(turn_at)[:16].replace("T", " ")})
    if guard.active:
        turns = guard.filter_canonical_rows(turns, record_id_keys=("record_id",), text_keys=("content",))
    return [{"table": t["table"], "record_id": t["record_id"], "owner": t["owner"], "at": t["at"],
             "speaker": CONTEXT_OWNER if t["owner"] else (other or CONTEXT_UNNAMED), "text": t["content"]}
            for t in turns]


def _other_party(conn, conv: str, guard, is_diarisation_label) -> Optional[str]:
    """The other person's agreed contact name ("" when none agrees) when the conversation has
    exactly one other sender who is not Off-limits; None otherwise (a group, a recording, or
    nobody else)."""
    peers = {str(s) for (s,) in conn.execute(
        "SELECT DISTINCT sender_id FROM conversation_messages WHERE conversation_id=?"
        " AND COALESCE(is_from_self,0)=0 AND sender_id IS NOT NULL AND sender_id<>'' AND sender_id<>'self'",
        (conv,)).fetchall()}
    if len(peers) != 1:
        return None
    peer = next(iter(peers))
    if is_diarisation_label(peer):
        return None
    from ....analytics.messenger_directed import resolve_peer_identities
    _cid, eid, display = resolve_peer_identities(conn, [peer]).get(peer, (None, None, None))
    name = " ".join(str(display or "").split()) if display and any(ch.isalpha() for ch in str(display)) else ""
    if guard.active and (guard.blocks_entity_id(eid) or guard.text_mentions_blackholed(peer)
                         or (name and (guard.blocks_name(name) or guard.text_mentions_blackholed(name)))):
        return None
    return name


def _context_for(conn, pack, rec: Dict[str, Any], cache: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The turns `pack` reads before `rec`: `_conversation_context` for a pack in
    `template.context_packs()`, [] for every other pack, and for a pack the caller listed in
    `cache["record_only"]` (one Topos Facts reads: the classifier is given the record alone, so
    nothing downstream of it is shown more). Read once per record and kept on it, so the prompt,
    the parser and the verifier see the same turns."""
    from ....features.derivation.template import context_packs, context_turns
    pid = str(getattr(pack, "pack", "") or "")
    if pid not in context_packs() or pid in cache.get("record_only", ()):
        return []
    if "context" not in rec:
        rec["context"] = _conversation_context(conn, rec, context_turns(), cache=cache)
    return rec["context"]


def _context_refs(context: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    """The records a fact was read with, for `writer.assert_pack_fact(context_refs=...)`."""
    return [{"table": t["table"], "record_id": t["record_id"]} for t in (context or ())]


#: DR-62: the `vreason` a ledger row of a finding read with context carries: one of these fixed
#: lines, never the verifier's own reason. The verifier read the other person's turns, so its
#: reason may repeat them; the status is in `vstatus`.
CONTEXT_LEDGER_REASON = "read with context"
CONTEXT_NOT_THE_OWNERS = "read with context, not about the owner"
CONTEXT_NOT_CHECKED = "read with context, not fact-checked"
_CONTEXT_REASONS = frozenset({CONTEXT_LEDGER_REASON, CONTEXT_NOT_THE_OWNERS, CONTEXT_NOT_CHECKED})


def _owners_own(a: Dict[str, Any], context) -> Dict[str, Any]:
    """DR-62: nothing read with someone else's words is written about anyone but the owner, not
    even to the review queue. A verifier that moves `about` off the owner rejects it instead.

    And nothing read with them is written unchecked. A verifier call that failed leaves an
    assertion `error`, which is stored flagged for a one-record reading; one read with context
    is rejected, because the ruling's reading is the one the verifier checked against the same
    turns. (With the fact check switched off there is no context to begin with:
    `template.context_packs`.)"""
    if context and a.get("verifier_status") == "rerouted":
        a = dict(a)
        a["verifier_status"] = "rejected"
        a["verifier_reason"] = CONTEXT_NOT_THE_OWNERS
    elif context and a.get("verifier_status") == "error":
        a = dict(a)
        a["verifier_status"] = "rejected"
        a["verifier_reason"] = CONTEXT_NOT_CHECKED
    return a


def _attach_dm_recipients(conn, recs: List[Dict[str, Any]]) -> None:
    """Give each owner-authored direct message the one other party it was sent to.

    "Sure, I'll send it after lunch" is a promise to the person being texted, and their
    name is nowhere in the words. Without the label the extraction model has no counterparty
    to give, and a placeholder (`<recipient>`, `<him>`) is rejected by the grounding guard,
    so the promise is lost. The label is who the message was sent to, from the record and
    the identity bridge, never anything that person wrote.

    The rule is the commitments lane's (`commitments._dm_recipients`): a conversation with
    more than one other sender is a group and gets no recipient. Observed rows, AI chats,
    journals and transcripts are untouched. A record that already carries a
    `conversation_id` keeps it; the rest read the stored one by message id.

    Attached to the record, read per pack: only `template.RECIPIENT_LABEL_PACKS` see it (a
    pack outside it, given the label, binds the person being texted to facts not about them).
    """
    import sqlite3 as _sqlite3
    dms = [r for r in recs if r.get("role") == "authored" and r.get("table") == "conversation_messages"]
    if not dms:
        return
    unknown = sorted({str(r["record_id"]) for r in dms if not r.get("conversation_id")})
    stored: Dict[str, str] = {}
    for start in range(0, len(unknown), 400):
        chunk = unknown[start:start + 400]
        try:
            stored.update((str(mid), str(conv or "")) for mid, conv in conn.execute(
                f"SELECT message_id, conversation_id FROM conversation_messages"
                f" WHERE message_id IN ({','.join('?' for _ in chunk)})", chunk).fetchall())
        except _sqlite3.Error:
            break
    for r in dms:
        if not r.get("conversation_id"):
            r["conversation_id"] = stored.get(str(r["record_id"]), "")
    try:
        from ....features.derivation.commitments import _dm_recipients
        who_by_conv = _dm_recipients(conn, sorted({r["conversation_id"] for r in dms if r["conversation_id"]}))
    except Exception as exc:  # noqa: BLE001 — no recipient means today's prompt, never a failed batch
        logger.debug("derivation: DM recipients unavailable (%s)", exc)
        return
    for r in dms:
        who = who_by_conv.get(r["conversation_id"])
        if who:
            r["recipient"], r["recipient_entity_id"], r["recipient_key"] = who


def _recipient_args(pack, rec: Dict[str, Any]) -> Dict[str, str]:
    """`build_prompt`'s recipient arguments; empty strings for a record without one, or for
    a pack that does not read the recipient (`template.RECIPIENT_LABEL_PACKS`)."""
    from ....features.derivation.template import addressing_for
    rec = addressing_for(pack, rec)
    return {"recipient": str(rec.get("recipient") or ""),
            "recipient_entity_id": str(rec.get("recipient_entity_id") or ""),
            "recipient_key": str(rec.get("recipient_key") or "")}


def _recipient_parse_args(pack, rec: Dict[str, Any]) -> Dict[str, Any]:
    """The matching `parse_output` arguments: the labels the prompt issued, and the
    recipient's name as the prompt spelled it. Not the stand-in for an unnamed partner —
    "the person you are texting" exempted from the person checks would be stored as one."""
    from ....features.derivation.commitments import UNNAMED_RECIPIENT
    from ....features.derivation.template import addressing_for
    rec = addressing_for(pack, rec)
    labels = [label for label in (
        f"id:{rec['recipient_entity_id']}" if rec.get("recipient_entity_id") else "",
        f"key:{rec['recipient_key']}" if rec.get("recipient_key") else "") if label]
    name = str(rec.get("recipient") or "").strip()
    return {"runner_labels": labels,
            "grounded_exempt": (name,) if labels and name and name != UNNAMED_RECIPIENT else ()}


def _ledger_write(conn, *, stage, pack, rec, a, model, vmodel, context, written_id=None):
    """Training-data factory: EVERY judged assertion lands here, accepted or not
    (rejects are the hard negatives; quotes+record refs recover spans).

    `context` has no default: every caller says what the finding was read with (None or []
    for a record read alone), so a new call site cannot forget it and keep model text.

    A finding read with context (DR-62) keeps its status and no model text a turn could have
    reached: no value (an accepted one is on the fact, where its `context_refs` protect it; a
    rejected one is kept nowhere), a fixed reason (`_CONTEXT_REASONS`) in place
    of the verifier's, and `about` without the name a verifier may give ("other:<name>"
    is kept as "other"). The ledger keeps no `context_refs`, so nothing here could be taken out
    when the owner excludes or deletes a turn. The quote is the owner's own message
    (`template.quote_in_record`) and the occurrence an ISO date (`parse_output`)."""
    import uuid as _uuid
    from ....features.derivation import template as _T
    value_json = _json.dumps(a.get("value"), default=str)
    about, reason = a.get("about"), a.get("verifier_reason")
    if context:
        value_json = None
        reason = reason if reason in _CONTEXT_REASONS else CONTEXT_LEDGER_REASON
        if isinstance(about, str) and about.startswith("other:"):
            about = "other"
    conn.execute(
        """INSERT INTO derivation_training_ledger
           (ledger_id, stage, pack_id, pack_version, template_version, extract_model,
            verifier_model, source_table, record_id, actor_role, predicate, value_json,
            about, occurrence, quote, confidence, vstatus, vreason, written_object_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"dtl_{_uuid.uuid4().hex[:16]}", stage, pack.pack, pack.version,
         _T.TEMPLATE_VERSION, model, vmodel, rec["table"], rec["record_id"],
         rec["role"], a.get("predicate"), value_json,
         about, a.get("occurrence"), a.get("quote"), a.get("confidence"),
         a.get("verifier_status"), reason, written_id))


def _note_deferral(conn, reason: str) -> None:
    """Record why the batch could not run where the facts config reads it (one row, counted),
    so a picked provider that never loads is visible in the app, not only in the node log."""
    from ....config.facts_llm import ENGINE_CONFIG_KEY_DERIVATION_DEFERRAL, derivation_deferral
    prior = derivation_deferral(conn) or {}
    batches = prior.get("batches") if isinstance(prior.get("batches"), int) else 0
    value = _json.dumps({"reason": reason, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "batches": batches + 1})
    try:
        conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES (?, ?)",
                     (ENGINE_CONFIG_KEY_DERIVATION_DEFERRAL, value))
        conn.commit()
    except sqlite3.Error:
        logger.debug("derivation: could not record the deferral", exc_info=True)


def _clear_deferral(conn) -> None:
    from ....config.facts_llm import ENGINE_CONFIG_KEY_DERIVATION_DEFERRAL
    try:
        cur = conn.execute("DELETE FROM engine_config WHERE key = ?", (ENGINE_CONFIG_KEY_DERIVATION_DEFERRAL,))
        if cur.rowcount:
            conn.commit()
    except sqlite3.Error:
        logger.debug("derivation: could not clear the deferral", exc_info=True)


def _bump_yield(conn, pack_id, **cols):
    import datetime as _dt
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    conn.execute("INSERT OR IGNORE INTO pack_yield (pack_id, day) VALUES (?, ?)", (pack_id, day))
    for col, inc in cols.items():
        if inc:
            conn.execute(f"UPDATE pack_yield SET {col} = {col} + ? WHERE pack_id=? AND day=?",
                         (int(inc), pack_id, day))


def _yield_window(conn, pack_id, days, col):
    row = conn.execute(
        f"SELECT COALESCE(SUM({col}),0) FROM pack_yield WHERE pack_id=? AND day >= date('now', ?)",
        (pack_id, f"-{int(days)} day")).fetchone()
    return int(row[0] or 0)


def _has_recent_offer(conn, pack_id, kind, days=30) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pack_offers WHERE pack_id=? AND kind=? AND"
        " (status='pending' OR updated_at >= datetime('now', ?)) LIMIT 1",
        (pack_id, kind, f"-{int(days)} day")).fetchone()
    return row is not None


def _mint_offer(conn, pack_id, kind, stats):
    """Store the offer and log that it was made. The log line carries the offer's numbers only:
    an enable offer's `examples` are findings (a value, the owner's quote), and a log holds
    counts and ids, never text."""
    import uuid as _uuid
    conn.execute(
        "INSERT INTO pack_offers (offer_id, pack_id, kind, stats_json) VALUES (?,?,?,?)",
        (f"ofr_{_uuid.uuid4().hex[:12]}", pack_id, kind, _json.dumps(stats, default=str)))
    counts = {k: v for k, v in stats.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    logger.info("pack offer minted: %s %s %s", kind, pack_id, counts)


#: self-gating knobs — deliberately conservative; the node OFFERS, the owner decides
TRIAL_MIN_HITS_14D = 25
TRIAL_SAMPLE_CAP = 30
TRIAL_MIN_ACCEPTED = 3
TRIAL_MIN_ACCEPT_RATE = 0.4
DRYWELL_MIN_CALLS_30D = 50


def run_derivation_batch(
    conn,
    rows: List[Dict[str, Any]],
    *,
    cancel: Optional[threading.Event] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> int:
    """Sync worker (runs in a thread). Returns facts written+routed."""
    from ....features.derivation import template as _T
    from ....features.derivation.prefilter import PackPrefilter
    from ....features.derivation.registry import (bundled_pack_dir, enabled_packs,
                                                  mark_pack_run, seed_pack_registry)
    from ....features.derivation.packs import load_packs
    from ....features.derivation.template import addressing_for, build_prompt, parse_output
    from ....features.derivation.verify import (build_verify_prompt, parse_verdict,
                                                apply_verdict, verifier_model, label_note_for, lens_note_for)
    from ....features.derivation.writer import DerivationWriter
    from ....features.facts.llm_extract import _resolved_extraction_model
    from ....config.settings import settings as _settings
    from ....engine.backends.ollama import OllamaAdapter

    st = stats if stats is not None else {}
    pack_dir = bundled_pack_dir()
    _T.set_pack_dir(pack_dir)
    seed_pack_registry(conn, pack_dir)
    all_packs = load_packs(pack_dir)
    packs = enabled_packs(conn, pack_dir)
    from ....features.entities.owner import fact_owner_subject
    from ....features.provenance.posture import make_posture_resolver
    _o = fact_owner_subject(conn)
    owner_row = (_o,) if _o else None
    if not owner_row:
        st["skipped"] = "no_owner_entity"
        return 0
    owner = owner_row[0]
    model = _resolved_extraction_model(_settings, conn)
    vmodel = verifier_model()
    adapter = OllamaAdapter()

    def settle():
        """Commit before a model call so no write transaction spans it.

        This job writes on its own connection, outside the write gate, and used to commit
        once per record. In WAL the first write takes SQLite's write lock at execute time,
        so the lock was held across every extraction and verifier call of the record (up
        to 11 packs, 9-20 s each). The pipeline's claim_next_job then took the gate and
        blocked on SQLite, and every gated writer queued behind it: measured 5-6 Nov 2026
        on an isolated node, claim_next_job held 160 s with waited=0, 383-3,017 s per
        50-record run. The lock is now held for the write bursts only (milliseconds).
        """
        if conn.in_transaction:
            conn.commit()

    def llm(m: str, prompt: str, n: int = 900, *, pack_id=None,
            phase="extraction", mode="ingest") -> str:
        settle()
        if pack_id is not None:
            from ....features.derivation.processing import measured_generate
            return measured_generate(conn, adapter, m, prompt, pack_id=pack_id,
                                     phase=phase, mode=mode, n=n)
        # The availability probe is not a pack extraction or verification call.
        out = adapter._generate(m, prompt, num_predict=n, think=False,
                                temperature=0.0, num_ctx=8192, timeout=180)
        return str(out.get("text") or "") if isinstance(out, dict) else str(out or "")

    from ....features.derivation.router import router_mode
    _router_hybrid = router_mode() == "hybrid"
    verify_mode = _verify_mode()
    if verify_mode == "required" and packs:
        try:
            probe = llm(vmodel, "Respond with exactly: ok", n=8)
            if not probe.strip():
                raise RuntimeError("empty verifier probe")
        except Exception as exc:  # noqa: BLE001
            st["deferred"] = f"verifier_unavailable: {exc}"[:120]
            logger.warning("derivation deferred: verifier %s unavailable (%s)", vmodel, exc)
            _note_deferral(conn, st["deferred"])
            return 0

    # Topos Facts (owner opt-in under Node functions → Facts extraction): the packs it
    # knows skip the LLM call; ROUTED_TO_LLM and unknown packs keep `model`. A pick that
    # cannot load defers the batch, like an unavailable verifier: nothing is marked done.
    from ....config.facts_llm import facts_classifier_version
    from ....features.derivation import topos_facts as _tf
    tf_model = None
    if facts_classifier_version(conn):
        try:
            tf_model = _tf.load()
        except Exception as exc:  # noqa: BLE001
            st["deferred"] = f"topos_facts_unavailable: {exc}"[:120]
            logger.warning("derivation deferred: Topos Facts unavailable (%s)", exc)
            _note_deferral(conn, st["deferred"])
            return 0
    else:
        _tf.release()                    # another provider picked: the encoders go
    _clear_deferral(conn)

    filters = {pid: PackPrefilter(pk) for pid, pk in all_packs.items()}
    writer = DerivationWriter(conn, model=model)
    writers = {model: writer}

    def pack_raw(pack, rec, mode, context=None):
        """(raw output, extract model) for one record and pack: Topos Facts or the LLM.
        `context` is the record's conversation (DR-62) and only the LLM reads it: a pack
        Topos Facts reads has none (`ctx_cache["record_only"]`)."""
        if tf_model is not None and _tf.routes_to_classifier(pack.pack, tf_model):
            settle()
            return _tf.raw_output(tf_model, pack.pack, rec["text"]), _tf.MODEL_ID
        raw = llm(model, build_prompt(pack, rec["text"], rec["date"], rec["role"], **_recipient_args(pack, rec),
                                      context=context, record_table=str(rec.get("table") or "")),
                  pack_id=pack.pack, mode=mode)
        _bump_yield(conn, pack.pack, llm_calls=1)
        return raw, model

    def writer_for(extract_model):
        if extract_model not in writers:
            writers[extract_model] = DerivationWriter(conn, model=extract_model)
        return writers[extract_model]
    posture_for = make_posture_resolver(conn)
    retired = retire_role_skipped_progress(conn, all_packs, posture_for)
    if retired:
        st["role_skips_retired"] = retired
    done = {r[0] for r in conn.execute("SELECT key FROM derivation_progress")}

    HARM_ROLES = {"partner", "spouse", "child", "sibling", "ex_partner"}
    HARM_EVENTS = {"loss", "conflict", "reconnected", "ended", "estranged"}

    def _is_high_harm(a):
        v = a.get("value") if isinstance(a.get("value"), dict) else {}
        if a.get("predicate") == "rel.relationship" and str(v.get("role")) in HARM_ROLES:
            return True
        if a.get("predicate") == "rel.relationship_event" and str(v.get("event")) in HARM_EVENTS:
            return True
        return False

    # Topos Facts copies every value out of the one record it is given, so a pack it reads has
    # no conversation to show the parser or the verifier either, and no short message to read.
    ctx_cache: Dict[str, Any] = {"record_only": frozenset(
        pid for pid in all_packs if _tf.routes_to_classifier(pid, tf_model))}
    ctx_packs = _T.context_packs()

    def judge(pack, rec, a, stage):
        """Verify one assertion; high-harm predicate classes get 3-vote majority
        (measured 2026-08-26: single quantized-verifier votes are variance-exposed
        exactly where errors hurt most). Everything else keeps one vote. A record read with
        its conversation is judged with the same turns (DR-62)."""
        if verify_mode == "off":
            a = dict(a); a["verifier_status"] = "skipped"
            return a
        context = _context_for(conn, pack, rec, ctx_cache) or None
        votes_needed = 3 if _is_high_harm(a) else 1
        accepts = 0
        last_verdict = None
        for _ in range(votes_needed):
            try:
                verdict = parse_verdict(llm(vmodel, build_verify_prompt(
                    rec["text"], rec["role"], rec["date"],
                    a["predicate"], a["value"], a.get("about", "owner"),
                    lens_note=lens_note_for(pack), label_note=label_note_for(addressing_for(pack, rec)),
                    context=context), n=250,
                    pack_id=pack.pack, phase="verification", mode=stage))
            except Exception:  # noqa: BLE001
                verdict = None
            last_verdict = verdict or last_verdict
            if verdict and verdict["supported"] and verdict["fields_ok"]:
                accepts += 1
        if votes_needed > 1 and accepts < 2:
            a = dict(a); a["verifier_status"] = "rejected"
            a["verifier_reason"] = f"majority {accepts}/{votes_needed} against"
            return a
        return _owners_own(apply_verdict(a, last_verdict), context)

    recs = [rec for rec in (_row_to_record(row, posture_for=posture_for)
                            for row in _with_stored_metadata(conn, rows)) if rec is not None]
    _attach_dm_recipients(conn, recs)
    written = 0
    for rec in recs:
        if cancel is not None and cancel.is_set():
            st["cancelled"] = True
            break
        _rec_vec = None
        if _router_hybrid and not rec.get("short"):
            from ....features.derivation.router import embed_record
            _rec_vec = embed_record(rec["text"])
        for pid, pack in all_packs.items():
            if rec.get("short") and pid not in ctx_packs:
                continue                     # a short message is read only with its conversation
            hit = rec["role"] in pack.allowed_roles() and filters[pid].passes(rec["text"])
            if not hit and _router_hybrid and not rec.get("short") and rec["role"] in pack.allowed_roles():
                from ....features.derivation.router import semantic_passes
                hit = semantic_passes(pack, _rec_vec)
            if hit and rec.get("short") and not _context_for(conn, pack, rec, ctx_cache):
                hit = False
            if hit:
                _bump_yield(conn, pid, prefilter_hits=1)
            if pid not in packs:
                continue                     # disabled: counted for self-gating, never run
            key = f"{pid}@{pack.version}:{rec['table']}:{rec['record_id']}"
            if key in done or not hit:
                if hit:
                    conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
                continue
            context = _context_for(conn, pack, rec, ctx_cache) or None
            try:
                raw, used = pack_raw(pack, rec, "ingest", context)
            except Exception as exc:  # noqa: BLE001
                st["llm_errors"] = st.get("llm_errors", 0) + 1
                logger.debug("derivation llm error on %s: %s", key, exc)
                continue
            valid, rejects = parse_output(raw, pack, record_text=rec["text"], **_recipient_parse_args(pack, rec),
                                          context=context, counts=st)
            st["parse_rejects"] = st.get("parse_rejects", 0) + rejects
            _bump_yield(conn, pid, assertions=len(valid))
            if context:
                st["context_reads"] = st.get("context_reads", 0) + 1
            for a in valid:
                a = judge(pack, rec, a, "ingest")
                st[f"v_{a.get('verifier_status')}"] = st.get(f"v_{a.get('verifier_status')}", 0) + 1
                if a.get("verifier_status") == "rejected":
                    _ledger_write(conn, stage="ingest", pack=pack, rec=rec, a=a,
                                  model=used, vmodel=vmodel, context=context)
                    continue
                _bump_yield(conn, pid, accepted=1)
                out = writer_for(used).assert_pack_fact(
                    pack=pack, predicate=a["predicate"], subject_entity_id=owner,
                    value=a["value"], actor_role=rec["role"],
                    source_refs=[{"table": rec["table"], "record_id": rec["record_id"],
                                  "source_id": rec["source_id"]}],
                    confidence=a["confidence"],
                    occurrence=a["occurrence"],   # NEVER defaulted to record date
                    quote=a.get("quote", ""), about=a.get("about", "owner"),
                    event_date=rec["date"] or None, context_refs=_context_refs(context))
                stored = out.get("outcome") in ("written", "corroborated", "retelling_merged",
                                                "field_update", "corrected", "superseded")
                if stored:
                    written += 1
                    _bump_yield(conn, pid, written=1)
                _ledger_write(conn, stage="ingest", pack=pack, rec=rec, a=a,
                              model=used, vmodel=vmodel, context=context,
                              written_id=out.get("object_id") if stored else None)
            conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
        conn.commit()
        _yield_to_owner()

    _drip_catchup(conn, packs=packs, filters=filters, pack_raw=pack_raw, judge=judge,
                  writer_for=writer_for, owner=owner, done=done, st=st, cancel=cancel,
                  posture_for=posture_for, ctx_cache=ctx_cache)

    _self_gate(conn, all_packs=all_packs, enabled=set(packs), filters=filters,
               llm=llm, judge=judge, model=model, vmodel=vmodel, st=st,
               cancel=cancel, posture_for=posture_for, ctx_cache=ctx_cache)

    for pid, pack in packs.items():
        mark_pack_run(conn, pid, pack.version)
    conn.commit()
    st["written"] = written
    for w in writers.values():
        for k, v in w.stats.items():
            if v:
                st[f"w_{k}"] = st.get(f"w_{k}", 0) + v
    return written


def _yield_to_owner() -> None:
    """Between records, with the record's writes committed: let a pending owner request go first.

    A batch is a model call or more per (record, pack) pair, minutes of work on the local models,
    and it ran end to end without looking up. The other long background work on the node waits
    here (`core.owner_requests.pace`), and the owner's backfill run does between its records
    (`backfill_runs`); the ingest batch and its history catch-up now do the same. It returns at
    once when no owner request is pending."""
    from ....core import owner_requests

    owner_requests.pace()


#: derivation_progress row that says the one-time retirement below has run on this node.
ROLE_SKIP_RETIREMENT_MARKER = "__provenance_role_fallback_v1__"


def retire_role_skipped_progress(conn, all_packs, posture_for) -> int:
    """Once per node: forget the progress keys a walk wrote when it skipped an owner message
    for its role.

    The history walks (drip catch-up, the owner's backfill control) mark a (pack, record)
    pair done when the record's role is outside the pack's roles, and read NULL as
    observed. So an owner message a walk reached before the provenance fallback was marked
    done for every owner-only pack and would never be read again. Retired: the key of a
    pack that refuses observed, on a message whose stored role is NULL and whose provenance
    role that pack accepts. The ingest path never marks a role-refused pair, so such a key
    is a skip, unless the record was fed with an explicit role (the commitments lane); a
    key with a training-ledger row for that pack and record was judged, and is kept. Nothing
    else is touched. Returns the number of keys retired.
    """
    import sqlite3 as _sqlite3
    try:
        if conn.execute("SELECT 1 FROM derivation_progress WHERE key=?",
                        (ROLE_SKIP_RETIREMENT_MARKER,)).fetchone():
            return 0
        rows = conn.execute(
            "SELECT message_id, is_from_self, sender_id, sender_type, source_id, dataset_id"
            " FROM conversation_messages WHERE actor_role IS NULL").fetchall()
        existing = {r[0] for r in conn.execute("SELECT key FROM derivation_progress")}
    except _sqlite3.OperationalError:
        return 0
    gated = {pid: pack for pid, pack in all_packs.items() if "observed" not in pack.allowed_roles()}
    stale = set()
    for mid, is_self, sender_id, sender_type, source_id, dataset_id in rows:
        role = _record_role_for({"is_from_self": is_self, "sender_id": sender_id, "sender_type": sender_type,
                                 "source_id": source_id, "dataset_id": dataset_id},
                                "conversation_messages", posture_for)
        for pid, pack in gated.items():
            key = f"{pid}@{pack.version}:conversation_messages:{mid}"
            if role in pack.allowed_roles() and key in existing:
                stale.add(key)
    if stale:
        try:
            judged = {f"{p}@{v}:{t}:{r}" for p, v, t, r in conn.execute(
                "SELECT pack_id, pack_version, source_table, record_id FROM derivation_training_ledger")}
        except _sqlite3.OperationalError:
            judged = set()
        stale -= judged
        conn.executemany("DELETE FROM derivation_progress WHERE key=?", [(k,) for k in sorted(stale)])
    conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (ROLE_SKIP_RETIREMENT_MARKER,))
    return len(stale)


_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: history records drip-processed per batch — the dark delta self-heals without
#: a scheduler; a fresh enablement's history closes over days, or immediately
#: via the owner's backfill control.
CATCHUP_PER_BATCH = 25


def _iter_history(conn, limit=2000, posture_for=None, since=None):
    """The newest `limit` journal entries and the newest `limit` messages, newest first.

    `since` (a `YYYY-MM-DD` date) keeps only records dated on or after it: an owner's backfill
    back to a date they chose stops there instead of walking the whole window. The stamps are ISO
    text, so the comparison is the text's own order."""
    import sqlite3 as _sqlite3
    if posture_for is None:
        from ....features.provenance.posture import make_posture_resolver
        posture_for = make_posture_resolver(conn)
    since = str(since or "").strip()
    if since and not _SINCE_RE.match(since):
        raise ValueError("since must be a YYYY-MM-DD date")
    args = (since,) if since else ()
    def _rows(sql):
        try:
            return conn.execute(sql, args).fetchall()
        except _sqlite3.OperationalError:
            return []
    out = []
    for tbl, rid, text, at, role in _rows(
            f"SELECT 'journal_entries', entry_id, content, entry_at, 'authored'"
            f" FROM journal_entries WHERE content IS NOT NULL AND LENGTH(content)>15"
            f"{' AND entry_at >= ?' if since else ''}"
            f" ORDER BY entry_at DESC LIMIT {int(limit)}"):
        out.append({"table": tbl, "record_id": rid, "text": (text or "")[:6000],
                    "date": str(at or "")[:10], "role": role or "authored", "source_id": ""})
    msg_since = " AND event_at >= ?" if since else ""
    from ....features.facts.reactions import stored_metadata_column
    msg_rows = _rows(
            f"SELECT 'conversation_messages', message_id, content, event_at, actor_role,"
            f" CASE WHEN COALESCE(is_from_self,0)=1 THEN NULL ELSE sender_id END,"
            f" is_from_self, sender_id, sender_type, source_id, dataset_id, conversation_id,"
            f" {stored_metadata_column(conn)}"
            f" FROM conversation_messages WHERE content IS NOT NULL AND LENGTH(content)>15{msg_since}"
            f" ORDER BY event_at DESC LIMIT {int(limit)}")
    if not msg_rows:
        msg_rows = [tuple(r) + (None,) * 8 for r in _rows(
            f"SELECT 'conversation_messages', message_id, content, event_at, actor_role"
            f" FROM conversation_messages WHERE content IS NOT NULL AND LENGTH(content)>15{msg_since}"
            f" ORDER BY event_at DESC LIMIT {int(limit)}")]
    # The SENDER of each observed row, from the record — the label an any_with_label pack
    # carries into the prompt so a self-statement lands on the speaker's own node.
    speakers = {}
    keys = sorted({str(r[5]) for r in msg_rows if r[5]})
    if keys:
        try:
            from ....analytics.messenger_directed import resolve_peer_identities
            speakers = resolve_peer_identities(conn, keys)
        except Exception:  # noqa: BLE001 — no speaker label means no outward fact, never a crash
            speakers = {}
    for (tbl, rid, text, at, stored, sender, is_self, sender_id, sender_type, source_id, dataset_id, conv,
         meta) in msg_rows:
        _cid, eid, display = speakers.get(str(sender or ""), (None, None, None))
        role = _record_role_for({"actor_role": stored, "is_from_self": is_self, "sender_id": sender_id,
                                 "sender_type": sender_type, "source_id": source_id,
                                 "dataset_id": dataset_id}, tbl, posture_for)
        if _owner_reaction(role, tbl, {"metadata_json": meta}, text):
            continue   # the owner's reaction: its text is the message reacted to
        out.append({"table": tbl, "record_id": rid, "text": (text or "")[:6000],
                    "date": str(at or "")[:10], "role": role, "source_id": "",
                    "speaker": str(display or "") if eid else "",
                    "speaker_entity_id": str(eid or ""),
                    "conversation_id": str(conv or "")})
    # DR-62: the owner's short messages, after everything else and marked `short`, for the
    # context packs only. A query and a loop of their own, so the walk above, and every other
    # pack's history, is exactly what it was, whatever columns that walk comes to carry.
    if _reads_short_records():
        for rid, text, at, stored, is_self, sender_id, sender_type, source_id, dataset_id, conv in _rows(
                f"SELECT message_id, content, event_at, actor_role,"
                f" is_from_self, sender_id, sender_type, source_id, dataset_id, conversation_id"
                f" FROM conversation_messages WHERE COALESCE(is_from_self,0)=1 AND content IS NOT NULL"
                f" AND LENGTH(content)<={SHORT_RECORD_CHARS} AND LENGTH(TRIM(content))>0{msg_since}"
                f" ORDER BY event_at DESC LIMIT {int(limit)}"):
            role = _record_role_for({"actor_role": stored, "is_from_self": is_self, "sender_id": sender_id,
                                     "sender_type": sender_type, "source_id": source_id,
                                     "dataset_id": dataset_id}, "conversation_messages", posture_for)
            out.append({"table": "conversation_messages", "record_id": rid, "text": text,
                        "date": str(at or "")[:10], "role": role, "source_id": "",
                        "speaker": "", "speaker_entity_id": "",
                        "conversation_id": str(conv or ""), "short": True})
    # The RECIPIENT of each owner-authored DM, for the same reason the speaker is carried:
    # who the message was addressed to comes from the record, never from the text.
    _attach_dm_recipients(conn, out)
    # Ambient captions are not taste/habit evidence. any_with_label catchup
    # must not mint owner-subject facts from overheard speech. A gated
    # overheard reader is the only path that may surface captions.
    return out


def _drip_catchup(conn, *, packs, filters, pack_raw, judge, writer_for, owner, done, st, cancel,
                  posture_for=None, ctx_cache=None):
    """Process up to CATCHUP_PER_BATCH unprocessed HISTORY (record, pack) pairs
    per batch. Newest-first: recent history answers queries soonest."""
    if not packs:
        return
    from ....features.derivation.template import context_packs, parse_output
    ctx_packs = context_packs()
    ctx_cache = ctx_cache if ctx_cache is not None else {}
    budget = CATCHUP_PER_BATCH
    for rec in _iter_history(conn, posture_for=posture_for):
        if budget <= 0 or (cancel is not None and cancel.is_set()):
            return
        for pid, pack in packs.items():
            if rec.get("short") and pid not in ctx_packs:
                continue                     # never this pack's to read: no progress key either
            key = f"{pid}@{pack.version}:{rec['table']}:{rec['record_id']}"
            if key in done:
                continue
            if (rec["role"] not in pack.allowed_roles() or not filters[pid].passes(rec["text"])
                    or (rec.get("short") and not _context_for(conn, pack, rec, ctx_cache))):
                conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
                done.add(key)
                continue
            context = _context_for(conn, pack, rec, ctx_cache) or None
            budget -= 1
            try:
                raw, used = pack_raw(pack, rec, "catchup", context)
                _bump_yield(conn, pid, prefilter_hits=1)
            except Exception:  # noqa: BLE001
                st["llm_errors"] = st.get("llm_errors", 0) + 1
                continue
            valid, _rej = parse_output(raw, pack, record_text=rec["text"], **_recipient_parse_args(pack, rec),
                                       context=context, counts=st)
            _bump_yield(conn, pid, assertions=len(valid))
            for a in valid:
                a = judge(pack, rec, a, "catchup")
                if a.get("verifier_status") == "rejected":
                    _ledger_write(conn, stage="catchup", pack=pack, rec=rec, a=a,
                                  model=used, vmodel="(job)", context=context)
                    continue
                _bump_yield(conn, pid, accepted=1)
                out = writer_for(used).assert_pack_fact(
                    pack=pack, predicate=a["predicate"], subject_entity_id=owner,
                    value=a["value"], actor_role=rec["role"],
                    source_refs=[{"table": rec["table"], "record_id": rec["record_id"]}],
                    confidence=a["confidence"], occurrence=a["occurrence"],
                    quote=a.get("quote", ""), about=a.get("about", "owner"),
                    event_date=rec["date"] or None, context_refs=_context_refs(context))
                stored = out.get("outcome") in ("written", "corroborated", "retelling_merged",
                                                "field_update", "corrected", "superseded")
                if stored:
                    _bump_yield(conn, pid, written=1)
                    st["catchup_written"] = st.get("catchup_written", 0) + 1
                _ledger_write(conn, stage="catchup", pack=pack, rec=rec, a=a,
                              model=used, vmodel="(job)", context=context,
                              written_id=out.get("object_id") if stored else None)
            conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
            done.add(key)
            if budget <= 0:
                break
        conn.commit()
        _yield_to_owner()


def _self_gate(conn, *, all_packs, enabled, filters, llm, judge, model, vmodel, st, cancel,
               posture_for=None, ctx_cache=None):
    """Per-node self-gating (owner design, 2026-08-26): the node OFFERS, never
    switches. Enable offers come from a capped shadow trial on a disabled pack
    whose prefilter keeps hitting; disable nudges come from an enabled pack
    burning calls with zero yield. Special-class packs (health, beliefs) are
    NEVER auto-trialed — extracting their content requires the owner's opt-in
    first (consent precedes computation)."""
    from ....features.derivation.template import build_prompt, context_packs, parse_output
    if posture_for is None:
        from ....features.provenance.posture import make_posture_resolver
        posture_for = make_posture_resolver(conn)
    ctx_cache = ctx_cache if ctx_cache is not None else {}

    for pid, pack in all_packs.items():
        if cancel is not None and cancel.is_set():
            return
        if pid in enabled:
            calls = _yield_window(conn, pid, 30, "llm_calls")
            wrote = _yield_window(conn, pid, 30, "written")
            if calls >= DRYWELL_MIN_CALLS_30D and wrote == 0                     and not _has_recent_offer(conn, pid, "disable_nudge"):
                _mint_offer(conn, pid, "disable_nudge",
                            {"calls_30d": calls, "written_30d": 0})
            continue
        if getattr(pack, "sensitivity_class", "personal") == "special":
            continue
        hits = _yield_window(conn, pid, 14, "prefilter_hits")
        if hits < TRIAL_MIN_HITS_14D or _has_recent_offer(conn, pid, "enable_offer"):
            continue
        # sample recent prefilter-hit records for the trial
        sample = []
        import sqlite3 as _sqlite3
        def _rows(sql):
            try:
                return conn.execute(sql).fetchall()
            except _sqlite3.OperationalError:
                return []          # canonical table absent on this node — fine
        from ....features.facts.reactions import stored_metadata_column
        msg_rows = _rows(
            f"""SELECT 'conversation_messages', message_id, content, event_at, actor_role,
                      is_from_self, sender_id, sender_type, source_id, dataset_id,
                      {stored_metadata_column(conn)}
               FROM conversation_messages WHERE content IS NOT NULL AND LENGTH(content)>15
               AND event_at >= datetime('now','-14 day') ORDER BY event_at DESC LIMIT 400""")
        if not msg_rows:
            msg_rows = [tuple(r) + (None,) * 6 for r in _rows(
                """SELECT 'conversation_messages', message_id, content, event_at, actor_role
                   FROM conversation_messages WHERE content IS NOT NULL AND LENGTH(content)>15
                   AND event_at >= datetime('now','-14 day') ORDER BY event_at DESC LIMIT 400""")]
        short_ids = set()
        if pid in context_packs():
            # DR-62: the trial reads the owner's short messages as ingest will, with their context.
            short_rows = _rows(
                f"""SELECT 'conversation_messages', message_id, content, event_at, actor_role,
                          is_from_self, sender_id, sender_type, source_id, dataset_id
                   FROM conversation_messages WHERE COALESCE(is_from_self,0)=1 AND content IS NOT NULL
                   AND LENGTH(content)<={SHORT_RECORD_CHARS} AND LENGTH(TRIM(content))>0
                   AND event_at >= datetime('now','-14 day') ORDER BY event_at DESC LIMIT 200""")
            short_ids = {str(r[1]) for r in short_rows}
            msg_rows = list(msg_rows) + list(short_rows)
        recent = []
        for tbl, rid, text, at, stored, *who in msg_rows:
            row = dict(zip(("actor_role", "is_from_self", "sender_id", "sender_type", "source_id",
                            "dataset_id", "metadata_json"), (stored, *who)))
            if _owner_reaction(_record_role_for(row, tbl, posture_for), tbl, row, text):
                continue   # a trial reads what ingest will: never the owner's reaction
            recent.append((tbl, rid, text, at, _record_role_for(row, tbl, posture_for)))
        recent += _rows(
            """SELECT 'journal_entries', entry_id, content, entry_at, 'authored'
               FROM journal_entries WHERE content IS NOT NULL AND LENGTH(content)>15
               AND entry_at >= datetime('now','-14 day') ORDER BY entry_at DESC LIMIT 100""")
        recent += _rows(
            """SELECT 'transcript_segments', segment_id, content, event_at, actor_role
               FROM transcript_segments WHERE content IS NOT NULL AND LENGTH(content)>15
               AND event_at >= datetime('now','-14 day') ORDER BY event_at DESC LIMIT 200""")
        for tbl, rid, text, at, role in recent:
            role = role or "observed"
            if role in pack.allowed_roles() and filters[pid].passes(text):
                rec = {"table": tbl, "record_id": rid, "text": (text or "")[:6000],
                       "date": str(at or "")[:10], "role": role, "source_id": ""}
                if str(rid) in short_ids:
                    rec["short"] = True
                    if not _context_for(conn, pack, rec, ctx_cache):
                        continue
                sample.append(rec)
            if len(sample) >= TRIAL_SAMPLE_CAP:
                break
        if len(sample) < 5:
            continue
        # The trial reads a record the way ingest will once the pack is on, or its accept
        # rate measures a different prompt than the one the owner is offered.
        _attach_dm_recipients(conn, sample)
        n_assert = n_acc = n_withheld = 0
        examples = []
        for rec in sample:
            if cancel is not None and cancel.is_set():
                return
            context = _context_for(conn, pack, rec, ctx_cache) or None
            try:
                raw = llm(model, build_prompt(pack, rec["text"], rec["date"], rec["role"], **_recipient_args(pack, rec),
                                              context=context, record_table=str(rec.get("table") or "")),
                          pack_id=pack.pack, mode="trial")
            except Exception:  # noqa: BLE001
                continue
            valid, _rej = parse_output(raw, pack, record_text=rec["text"], **_recipient_parse_args(pack, rec),
                                       context=context, counts=st)
            for a in valid:
                a = judge(pack, rec, a, "trial")
                n_assert += 1
                _ledger_write(conn, stage="trial", pack=pack, rec=rec, a=a,
                              model=model, vmodel=vmodel, context=context)
                if a.get("verifier_status") in ("accepted", "rerouted"):
                    n_acc += 1
                    # The bounded shadow trial must retain every accepted finding,
                    # so the owner can inspect the evidence behind the offer count.
                    # Not one read with a conversation (DR-62): its value may describe what
                    # only the other person's turn said, and an offer row keeps no
                    # `context_refs`, so nothing would take the example out when the owner
                    # excludes or deletes that turn. It is counted, not kept.
                    if context:
                        n_withheld += 1
                        continue
                    examples.append({"predicate": a["predicate"],
                                     "value": a["value"], "quote": a.get("quote", "")})
        conn.commit()
        st[f"trial_{pid}"] = {"sampled": len(sample), "assertions": n_assert, "accepted": n_acc}
        if n_acc >= TRIAL_MIN_ACCEPTED and n_assert and (n_acc / n_assert) >= TRIAL_MIN_ACCEPT_RATE:
            offer = {"sampled": len(sample), "assertions": n_assert, "accepted": n_acc, "examples": examples}
            if n_withheld:
                offer["examples_withheld"] = n_withheld
            _mint_offer(conn, pid, "enable_offer", offer)


class DerivationJob(BaseEnrichmentJob):
    def get_derived_table(self) -> str:
        return ""  # writes signal_objects via DerivationWriter

    def get_job_name(self) -> str:
        return "derivation"

    def should_run(self, canonical_messages: List[Dict[str, Any]]) -> bool:
        return bool(canonical_messages) and derivation_enabled()

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        if conn is None:
            return [{"_deferred": True, "error": "database_unavailable"}]
        cancel = threading.Event()
        stats: Dict[str, Any] = {}
        try:
            def _run_batch():
                # The worker's own handle; `conn` above belongs to this coroutine's thread.
                return run_derivation_batch(
                    get_db_connection(), canonical_messages, cancel=cancel, stats=stats)

            written = await asyncio.to_thread(_run_batch)
        except asyncio.CancelledError:
            cancel.set()
            raise
        logger.info("derivation batch: %s written, stats=%s", written, stats)
        if stats.get("deferred"):
            # The pipeline's deferral path (debt row, envelope), as for the goals job: the
            # records were not marked done, and a retry finds them.
            return [{"_deferred": True, "error": str(stats["deferred"]), "derivation_written": 0, **stats}]
        return [{"derivation_written": written, **stats}]
