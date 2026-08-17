"""Mode-aware signal retrieval (PRD §8.5–8.7)."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..storage.adapters.factory import AdapterBundle
from .manifest import ScopeResolutionManifest
from .types import (
    MODE_RANK,
    AccessMode,
    RetrievalBundle,
    RetrievalError,
    RetrievalRequest,
)

logger = logging.getLogger(__name__)

_INFERENCE_EXCLUDED_KEYS = frozenset({"content", "text", "body"})
# Canonical rows carry the raw record text in topic/summary_text (see _load_canonical_summary_items).
# Inference must expose only the existence/relevance signal — never the raw text — so canonical
# score items are stripped of these too. Derived items (briefs, facts, clusters) keep topic/
# summary_text because those are computed labels, not raw content.
_INFERENCE_CANONICAL_EXCLUDED_KEYS = _INFERENCE_EXCLUDED_KEYS | frozenset({"topic", "summary_text"})
# Semantic hits carry raw chunk previews; inference keeps only the similarity/id signal.
_INFERENCE_SEMANTIC_EXCLUDED_KEYS = frozenset(
    {"content", "text", "body", "content_preview", "text_preview", "title"}
)
_SUMMARY_ITEM_CAP = 25
# Work "working on lately" asks: keep authored goals visible without letting a
# dense user_goals corpus monopolize the summary cap (D3 diversity floor).
_WORK_GOAL_FUSION_CAP = 8
_SEMANTIC_HIT_LIMIT = 20
_CLUSTER_LIMIT = 5
_GOAL_SUMMARY_BOOST = 0.88
_VECTOR_WORK_SCOPE_DAMPEN = 0.55

# --- Query routing vocabulary ---------------------------------------------------------
# A query that names a surface ("what's on my calendar", "show my messages") makes that
# surface topically relevant per se: with no further content tokens it is a BROWSE of
# that surface (recent rows). Content tokens beyond the surface words are the actual
# ask and must match rows — an unmatched specific ask contributes nothing (absence
# honesty; the negative-control lane).
_SURFACE_INTENT_TERMS: Dict[str, Tuple[str, ...]] = {
    "conversation_messages": (
        "message", "texted", "text message", "chat", "conversation", "sms", "imessage",
    ),
    "ai_chat_messages": (
        "ai conversation", "ai chat", "assistant", "chatgpt", "prompt", "ai message",
    ),
    "calendar_events": (
        "calendar", "meeting", "schedule", "event", "appointment", "standup",
        "agenda", "busy", "free", "availability",
    ),
    "journal_entries": ("journal", "diary", "mood", "wrote", "writing"),
    "location_events": (
        "place", "where", "location", "visit", "went", "travel", "city", "cities",
    ),
    # NB: content words like 'github' are deliberately NOT surface terms — they
    # must stay in the residual so rows are matched by them (C21).
    "activity_events": (
        "brows", "website", "site", "url", "reading", "read", "looked", "looking",
        "visit", "online", "activity", "watched",
    ),
    "financial_transactions": (
        "spend", "spent", "transaction", "purchase", "bought", "money", "cost",
        "paid", "finance", "financial", "expense",
    ),
    "contacts": ("contact", "phone", "number", "email", "reach", "person", "people"),
    "contact_identifiers": ("contact", "phone", "number", "email", "identifier"),
    "profile_records": (
        "profile", "bio", "experience", "job", "work history", "resume", "employer",
        "certification", "education",
    ),
}
# Non-canonical surfaces that participate in the same routing decision.
# Keep this ≥ the work_context router lexicon for paraphrases that never say
# "goal" ("working toward", "projects", "roadmap") — otherwise goals load but
# fail the keep/floor gates and drown under vector/recent lanes.
_EXTRA_SURFACE_TERMS: Tuple[str, ...] = (
    "goal",
    "objective",
    "priorit",
    "working on",
    "working toward",
    "working towards",
    "working",
    "project",
    "roadmap",
)
_RECENCY_TERMS = frozenset(
    {"recent", "recently", "latest", "newest", "last", "today", "yesterday",
     "now", "current", "currently",
     # Aspect/temporal function words: "what have I BEEN working on LATELY"
     # frames recency — these must never act as discriminative content needles
     # (rare-token veto zeroed that everyday phrasing; 1.2.0 release battery).
     "been", "being", "lately", "nowadays", "these", "days"}
)

#: ABSOLUTE date framing. `_RECENCY_TERMS` covered relative time ("yesterday",
#: "lately") but nothing absolute, so "my work goals Aug 11 through Aug 16 2026" kept
#: `aug`, `2026`, `11`, `16` as discriminative content needles — tokens the retrieved
#: rows had to literally contain. No goal text contains "aug", so the rare gate
#: concluded "specific ask, nothing matches" and returned an honest empty lane.
#:
#: Live 2026-08-17: work_context:read answered a dateless "what am I working on" with
#: 25 items including the exact goals the owner wanted ("Keep running install tests for
#: the new multi Topos installer…", event_at 2026-08-16), and answered the SAME question
#: scoped to Aug 11–16 with nothing at all. Naming the window destroyed the lane it was
#: meant to narrow.
#:
#: Dates belong to the time-window machinery (`plan.time_range`,
#: `_prefer_time_window`), which already filters and annotates `in_time_window`. They
#: must not double as content.
_MONTH_TOKENS = frozenset(
    {"january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december",
     "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}
)
#: Range connectors, stripped ONLY when the query is date-scoped — "see it through" is
#: content, "Aug 11 through Aug 16" is framing, and only the surrounding date tells them
#: apart.
_DATE_RANGE_TERMS = frozenset({"through", "thru", "until", "till", "between", "during"})


def _is_year_token(token: str) -> bool:
    return len(token) == 4 and token.isdigit() and 1900 <= int(token) <= 2099


def _is_day_number_token(token: str) -> bool:
    return 1 <= len(token) <= 2 and token.isdigit() and 1 <= int(token) <= 31


def _surface_intent(table: str, query_lower: str) -> bool:
    return any(term in query_lower for term in _SURFACE_INTENT_TERMS.get(table, ()))


def _residual_content_tokens(tokens: List[str], tables: Optional[List[str]] = None) -> List[str]:
    """Query tokens that are the *content* of the ask — not surface names, not
    recency framing. These are what retrieved rows must actually match."""
    blobs = [
        " ".join(terms)
        for tbl, terms in _SURFACE_INTENT_TERMS.items()
        if tables is None or tbl in tables
    ]
    blobs.append(" ".join(_EXTRA_SURFACE_TERMS))
    surface_blob = " ".join(blobs)
    # Date-scoped asks: a month name or a year means the bare small integers are day
    # numbers the date parser already consumed, and the range words are framing. Judged
    # over the whole token list so "11" is content in "top 11 sites" and framing in
    # "Aug 11-16" — the surrounding date is the only thing that distinguishes them.
    date_scoped = any(
        token in _MONTH_TOKENS or _is_year_token(token) for token in tokens
    )
    out: List[str] = []
    for token in tokens:
        if token in _RECENCY_TERMS:
            continue
        if token in _MONTH_TOKENS or _is_year_token(token):
            continue
        if date_scoped and (_is_day_number_token(token) or token in _DATE_RANGE_TERMS):
            continue
        # Plural-insensitive: "goals"/"meetings" name the same surface as
        # "goal"/"meeting".
        if token in surface_blob or token.rstrip("s") in surface_blob:
            continue
        out.append(token)
    return out


def _token_variants(token: str) -> List[str]:
    """Light suffix-stripped variants for df lookup and evidence matching.

    'journaling' must inherit the corpus frequency of 'journal' — an -ing/-ed/-s
    ask about an abundant concept is not a fabricated topic. Deliberately NOT a
    full stemmer: fabricated words produce fabricated stems (zorblatt → df 0
    either way), so the NH abstention guarantee is unaffected.
    """
    clean = re.sub(r"[^a-z0-9]", "", str(token).lower())
    variants = [clean]
    if len(clean) > 5 and clean.endswith("ing"):
        variants.append(clean[:-3])          # journaling → journal
        variants.append(clean[:-3] + "e")    # messaging → message
    if len(clean) > 4 and clean.endswith("ed"):
        variants.append(clean[:-2])
        variants.append(clean[:-1])          # committed-ish → keep simple
    if len(clean) > 3 and clean.endswith("es"):
        variants.append(clean[:-2])
        variants.append(clean[:-1])
    elif len(clean) > 3 and clean.endswith("s"):
        variants.append(clean[:-1])
    if len(clean) >= 5 and clean.endswith("e"):
        variants.append(clean[:-1])          # active → activ (matches 'activity')
    return [v for v in dict.fromkeys(variants) if len(v) >= 3]


def _rare_tokens(conn, tokens: List[str]) -> Dict[str, int]:
    """Tokens with low document frequency in the FTS index, mapped to their df —
    the discriminative part of a specific ask. df==0 means the term appears
    nowhere in the indexed corpus (fabricated topics). Porter stemming on both
    sides makes 'committed' meet 'commitment'. Iteration yields the tokens, so
    callers that only need membership can treat the result like a list."""
    if conn is None or not tokens:
        return {}
    from ..features.signal.vector_settings import rare_token_df_max

    df_max = rare_token_df_max()
    try:
        total_row = conn.execute("SELECT count(*) FROM signal_embeddings_fts").fetchone()
        if not total_row or int(total_row[0]) < df_max * 10:
            # An empty/tiny FTS index carries no frequency signal — treating
            # every token as rare would abstain on everything (fresh DBs,
            # seeded test corpora).
            return {}
    except Exception:
        return {}
    rare: Dict[str, int] = {}
    for token in tokens:
        variants = _token_variants(token)
        if not variants:
            continue
        # df = max over light morphological variants: 'journaling' inherits
        # the frequency of 'journal' (the docstring long promised stemming;
        # the implementation matched the exact token only).
        df = 0
        for clean in variants:
            try:
                row = conn.execute(
                    "SELECT count(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH ?",
                    (f'"{clean}"',),
                ).fetchone()
            except Exception:
                return {}  # no FTS index (fresh DB) — treat nothing as rare
            df = max(df, int(row[0]) if row else 0)
        if df < df_max:
            rare[token] = df
    return rare


def _item_text_blob(item: Dict[str, Any]) -> str:
    # `tag` carries a stat insight's entire content; omitting it meant NO stat
    # item could ever evidence a rare token, so aggregate asks with a low-df
    # content word were vetoed wholesale (C11, 1.2.0 release battery).
    return " ".join(
        str(item.get(f) or "")
        for f in ("topic", "summary_text", "content", "text_preview", "content_preview", "tag", "label")
    ).lower()


def _resolve_plan_now(request: RetrievalRequest) -> Optional[datetime]:
    """Reference instant for temporal planning: request.now, then the
    TOPOS_QUERY_NOW env (eval harness injection), else None → wall clock."""
    for raw in (getattr(request, "now", None), os.environ.get("TOPOS_QUERY_NOW")):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.debug("unparseable query now=%r ignored", raw)
    return None


# --- Record roles (P3.3 / PLAN_PROVENANCE_SPLIT) ---------------------------------------
# Query-side authorship checks mirror contract 1 (features/provenance/roles):
# conversation_messages — is_from_self / sender_id=='self'; ai_chat_messages —
# sender_type in ('human','user'). The roles module is preferred when
# importable; the local rules keep this half working standalone.
_MESSAGE_TABLES = frozenset({"conversation_messages", "ai_chat_messages"})

# Head-nouns / framing verbs of a first-person ask ("my hobbies", "who do I
# talk to") describe the KIND of answer wanted, never row content — like the
# answer-shape words in the token stoplist, they must not trip the rare-token
# abstention gate when first_person_intent is set. They still participate in
# matching (fact search, canonical contains).
_FIRST_PERSON_SHAPE_TOKENS = frozenset(
    {
        "opinion", "opinions", "belief", "beliefs", "stance", "stances",
        "view", "views", "interest", "interests", "interested",
        "hobby", "hobbies", "preference", "preferences", "style",
        "goal", "goals", "think", "feel", "believe",
        "people", "talk", "talked", "talking", "interact", "interacted",
        "interaction", "interactions",
        # Work-context answer shape ("what am I working toward / which projects")
        "project", "projects", "working", "toward", "towards",
        "focused", "focus", "roadmap", "priority", "priorities",
        # Mood / emotion ask shape (D1.8 — message_emotions contributor)
        "mood", "moods", "emotion", "emotions", "feeling", "feelings",
        "emotional", "wellbeing", "well-being",
    }
)

# Lexical cues that should load the role-filtered message_emotions aggregate.
_MOOD_EMOTION_TERMS: Tuple[str, ...] = (
    "mood",
    "moods",
    "emotion",
    "emotions",
    "feeling",
    "feelings",
    "felt",
    "emotional",
    "anxious",
    "anxiety",
    "stress",
    "stressed",
    "wellbeing",
    "well-being",
)


def _mood_emotion_intent(query_text: str) -> bool:
    q = (query_text or "").lower()
    return any(term in q for term in _MOOD_EMOTION_TERMS)


def _roles_owner_authored(table: str, row: Dict[str, Any]) -> Optional[bool]:
    try:
        from ..features.provenance.roles import owner_authored

        return bool(owner_authored(row, table=table))
    except Exception:
        return None


# Subject attribution for belief/interest asks: an owner-AUTHORED message can be
# ABOUT a third party ("Her interests are…", "my friend is vegan"). record_role
# captures WHO WROTE it (P4), not WHO IT'S ABOUT (P2); a first-person "what do I
# like/think" must not answer from the owner's description of someone else.
_BELIEF_STRONG_SELF = re.compile(r"\b(i|i'm|im|i've|ive|i'd|id|i'll|ill|me|myself|mine)\b")
_BELIEF_OTHER_PRONOUN = re.compile(r"\b(she|he|they|her|hers|his|him|them|their|theirs)\b")
_BELIEF_MY_RELATION = re.compile(
    r"\bmy (friend|friends|sister|brother|mom|mother|dad|father|wife|husband|"
    r"partner|spouse|colleague|colleagues|coworker|coworkers|boss|roommate|"
    r"neighbor|neighbour|cousin|aunt|uncle|kid|kids|son|daughter|buddy|pal|"
    r"mate|girlfriend|boyfriend|gf|bf|team|manager|client|teacher|professor)\b"
)
_BELIEF_MY = re.compile(r"\bmy\b")


def _belief_about_other(text: str) -> bool:
    """True when an owner-authored message is ABOUT a third party (so it must not
    answer a first-person belief/interest ask). Conservative: only True when a
    third-party subject is present AND there is NO first-person self-reference —
    'my sister and I both love hiking' and 'talked to her about my climbing' stay
    (they carry self-reference); 'Her interests are…' and 'my friend is vegan' go."""
    t = (text or "").lower()
    if not t.strip():
        return False
    my_relations = len(_BELIEF_MY_RELATION.findall(t))
    my_total = len(_BELIEF_MY.findall(t))
    has_my_topic = my_total > my_relations  # a 'my <topic>' not consumed by a relation
    self_ref = bool(_BELIEF_STRONG_SELF.search(t)) or has_my_topic
    other_ref = bool(_BELIEF_OTHER_PRONOUN.search(t)) or my_relations > 0
    return other_ref and not self_ref


def _message_row_owner(
    table: str,
    row: Dict[str, Any],
    conn: Optional[Any] = None,
    cache: Optional[Dict[str, Optional[bool]]] = None,
) -> Optional[bool]:
    """True/False for message-table rows, None for non-message rows.

    Canonical list specs alias away is_from_self, so conversation rows whose
    sender_id is a raw identifier (not 'self') fall back to a record_id lookup
    before failing toward NOT-authored (contract 1: never guess authored)."""
    if table == "ai_chat_messages":
        sender_type = str(row.get("sender_type") or "").strip().lower()
        if sender_type in ("human", "user"):
            return True
        if sender_type:
            return False
        verdict = _roles_owner_authored(table, row)
        return verdict if verdict is not None else None
    if table == "conversation_messages":
        if row.get("is_from_self") is not None:
            return row.get("is_from_self") in (1, True, "1")
        sender_id = str(row.get("sender_id") or "").strip().lower()
        if sender_id == "self":
            return True
        record_id = str(row.get("record_id") or row.get("message_id") or "")
        if conn is not None and record_id:
            looked_up = _record_owner_authored(conn, record_id, cache)
            if looked_up is not None:
                return looked_up
        verdict = _roles_owner_authored(table, row)
        if verdict is not None:
            return verdict
        return False if sender_id else None
    return None


def _record_owner_authored(
    conn: Any,
    record_id: str,
    cache: Optional[Dict[str, Optional[bool]]] = None,
) -> Optional[bool]:
    """Authorship by record_id for message-backed items (vector/recent lanes).
    None = not a message row (stat/fact/dossier/journal items are exempt)."""
    if not record_id or conn is None:
        return None
    if cache is not None and record_id in cache:
        return cache[record_id]
    verdict: Optional[bool] = None
    try:
        row = conn.execute(
            "SELECT is_from_self, sender_id FROM conversation_messages WHERE message_id = ?",
            (record_id,),
        ).fetchone()
        if row is not None:
            verdict = row[0] in (1, True, "1") or str(row[1] or "").lower() == "self"
        else:
            row = conn.execute(
                "SELECT sender_type FROM ai_chat_messages WHERE message_id = ?",
                (record_id,),
            ).fetchone()
            if row is not None:
                verdict = str(row[0] or "").lower() in ("human", "user")
    except Exception:
        verdict = None
    if cache is not None:
        cache[record_id] = verdict
    return verdict


def _activity_highlight_text(
    conn: Any,
    event_id: str,
    row: Dict[str, Any],
    cache: Optional[Dict[str, str]] = None,
) -> str:
    """The engaged (Annotate-grade) highlight span for an activity_events row.

    IMB9 / P2.1: a browser_highlight row's needle lives in
    ``metadata_json.highlight`` (mirrored onto the ``content`` column by the
    mapper), but the canonical list adapter selects only title/url — so the row
    handed to retrieval has neither. Look them up by event_id and, ONLY when the
    row is engaged (roles.is_engaged), return the span. The page-author text
    (``page_excerpt``) is deliberately never read here — that is exposure poison
    ("cast-iron cookware is strictly superior"), not the owner's takeaway.

    Returns "" for non-engaged rows, missing rows, or rows with no span."""
    if not event_id or conn is None:
        return ""
    if cache is not None and event_id in cache:
        return cache[event_id]
    span = ""
    try:
        db_row = conn.execute(
            "SELECT activity_type, content, metadata_json FROM activity_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    except Exception:
        db_row = None
    if db_row is not None:
        activity_type, content_col, metadata_json = db_row[0], db_row[1], db_row[2]
        probe = {
            "activity_type": activity_type,
            "metadata_json": metadata_json,
        }
        try:
            from ..features.provenance.roles import is_engaged

            engaged = is_engaged(probe)
        except Exception:
            engaged = str(activity_type or "").strip().lower() in (
                "browser_highlight",
                "highlight",
                "star",
                "star_page",
            )
        if engaged:
            meta = metadata_json
            if isinstance(meta, str):
                try:
                    import json as _json

                    meta = _json.loads(meta)
                except (ValueError, TypeError):
                    meta = {}
            if isinstance(meta, dict):
                span = str(meta.get("highlight") or "").strip()
            if not span:
                span = str(content_col or "").strip()
    if cache is not None:
        cache[event_id] = span
    return span


def _owner_rank(verdict: Optional[bool]) -> int:
    """Stable-sort key for owner-first re-ranking: authored → exempt → other."""
    if verdict is True:
        return 0
    if verdict is None:
        return 1
    return 2


def _sender_display(
    conn: Optional[Any],
    sender_id: str,
    cache: Dict[str, str],
) -> str:
    """Display name for a conversation sender_id via contact_identifiers, or
    the sender_id itself when unresolvable (contract: '[Bram Holloway] …' or
    sender_id fallback)."""
    if sender_id in cache:
        return cache[sender_id]
    name = ""
    if conn is not None:
        try:
            row = conn.execute(
                """SELECT c.display_name FROM contact_identifiers ci
                   JOIN contacts c ON c.contact_id = ci.contact_id
                   WHERE ci.identifier = ? AND c.display_name IS NOT NULL LIMIT 1""",
                (sender_id,),
            ).fetchone()
            name = str(row[0]).strip() if row and row[0] else ""
        except Exception:
            name = ""
    cache[sender_id] = name or sender_id
    return cache[sender_id]


def _plural_token_variants(tokens: List[str]) -> List[str]:
    """Tokens plus naive singular variants ('hobbies'→'hobby', 'interests'→
    'interest') so terse fact predicates meet plural query nouns."""
    out = list(tokens)
    for token in tokens:
        if token.endswith("ies") and len(token) > 4:
            variant = token[:-3] + "y"
        elif token.endswith("s") and not token.endswith("ss") and len(token) > 3:
            variant = token[:-1]
        else:
            continue
        if variant not in out:
            out.append(variant)
    return out


def _stat_like(entry: Dict[str, Any]) -> bool:
    return (
        str(entry.get("object_type") or "") == "stat_insight"
        or str(entry.get("fact_id") or "").startswith("stat:")
    )


def _entry_ledger(entry: Dict[str, Any]) -> str:
    """The stat entry's ledger tag. The exposure marker rides on two channels: a
    top-level payload key ({"ledger": "exposure"}) and — for facts minted by the
    live StatsEngine.promote_insights path, where the definition payload is
    merged into the insight summary (stats/insights.py _summary_with_payload) —
    nested under stat_summary. Both must be read."""
    ledger = entry.get("ledger")
    if not ledger:
        summary = entry.get("stat_summary")
        if isinstance(summary, dict):
            ledger = summary.get("ledger")
    return str(ledger or "").strip().lower()


def _suppress_exposure_ledger_entries(
    entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """P1.5: drop exposure-ledger stats (ledger=='exposure', e.g.
    'activity.visits.by_title') from ANY query when the owner has turned the
    exposure profile off (exposure_profile_visible=False). Unlike the
    first-person preference, this is intent-independent — the owner opted out of
    the whole exposure surface. Non-stat entries pass through untouched."""
    return [
        entry
        for entry in entries
        if not (_stat_like(entry) and _entry_ledger(entry) == "exposure")
    ]


def _apply_first_person_stat_preference(
    entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Contract 5 stat selection for first-person asks ("how many messages have
    I sent"): drop exposure-ledger stats ({"ledger": "exposure"}) and drop a
    volume family shadowed by its authored '.sent' twin for the same group
    ('messages.volume.by_thread' loses to 'messages.volume.sent.by_thread').
    Non-stat entries pass through untouched."""

    shadowed = set()
    for entry in entries:
        family = str(entry.get("record_id") or "")
        if _stat_like(entry) and ".sent" in family:
            shadowed.add((family.replace(".sent", ""), str(entry.get("group_key") or "")))
    out: List[Dict[str, Any]] = []
    for entry in entries:
        if _stat_like(entry):
            if _entry_ledger(entry) == "exposure":
                continue  # exposure ledger never answers a first-person "I" ask
            family = str(entry.get("record_id") or "")
            if ".sent" not in family and (
                family,
                str(entry.get("group_key") or ""),
            ) in shadowed:
                continue
        out.append(entry)
    return out


def _sqlite_main_path(conn) -> str:
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main":
                import os as _os

                return _os.path.realpath(str(row[2] or ""))
    except Exception:
        pass
    return ""


def _bundle_is_global_db(adapters: AdapterBundle) -> bool:
    """True when this query's adapter bundle targets the same database as the
    global singleton — i.e. the global-connection layers (vector search, topic
    clusters) actually describe THIS query's data. On mismatch (seeded eval
    corpora, multi-db verification) those layers must not contribute: they
    would silently serve another database's content."""
    bundle_conn = getattr(adapters.signal, "_conn", None)
    if bundle_conn is None:
        return True  # non-sqlite/fake bundles: nothing to compare
    try:
        from ..core.state import get_db_connection

        global_conn = get_db_connection()
    except Exception:
        return True
    if global_conn is None:
        return True
    bundle_path = _sqlite_main_path(bundle_conn)
    global_path = _sqlite_main_path(global_conn)
    if not bundle_path or not global_path:
        return True
    return bundle_path == global_path


def resolve_retrieval_source_ids(
    manifest: ScopeResolutionManifest,
    installed_source_ids: Optional[List[str]] = None,
) -> List[str]:
    from ..sources.definitions import CANONICAL_ADDRESS_BOOK_SOURCE_ID

    ids = [str(s).strip() for s in (manifest.default_source_ids or []) if str(s).strip()]
    if not ids and manifest.default_source_id:
        ids = [str(manifest.default_source_id)]
    if not installed_source_ids:
        return ids
    installed = {str(s).strip() for s in installed_source_ids if str(s).strip()}
    if not installed:
        return ids
    filtered = [sid for sid in ids if sid in installed]
    # Derived address-book rows are not runtime-installed connectors. When the
    # scope's manifest includes them, keep them even if only a demo/connector
    # contact source is installed — otherwise live contacts:resolve returns
    # empty while contact_identifiers still hold the needles (C7/C14).
    if (
        CANONICAL_ADDRESS_BOOK_SOURCE_ID in ids
        and CANONICAL_ADDRESS_BOOK_SOURCE_ID not in filtered
    ):
        filtered.append(CANONICAL_ADDRESS_BOOK_SOURCE_ID)
    if filtered:
        return filtered
    logger.debug(
        "No installed sources intersect manifest for scope=%s; using manifest defaults",
        manifest.scope_id,
    )
    return ids


def _resolve_source_ids(
    manifest: ScopeResolutionManifest,
    installed_source_ids: Optional[List[str]] = None,
) -> List[str]:
    return resolve_retrieval_source_ids(manifest, installed_source_ids)


def _parse_instant(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _parse_row_timestamp(row: Dict[str, Any]) -> Optional[datetime]:
    for field in ("event_at", "ts", "occurred_at", "created_at"):
        ts = _parse_instant(row.get(field))
        if ts is not None:
            return ts
    return None


def _prefer_time_window(
    items: List[Dict[str, Any]],
    time_range: Optional[Tuple[str, str]],
) -> List[Dict[str, Any]]:
    """Keep the items inside the plan's time window when any exist.

    Soft fallback, mirroring the vector lane's unscoped retry: a time-scoped
    ask over a lane with nothing in-window degrades to dated-but-out-of-window
    evidence rather than an empty lane — but every returned item is annotated
    with `in_time_window` so synthesis can say "nothing from yesterday; most
    recent instead" rather than passing off stale items as in-range. Undated
    items count as out-of-window: they cannot evidence a date-scoped claim.
    """
    if not time_range or not items:
        return items
    start = _parse_instant(time_range[0])
    end = _parse_instant(time_range[1])
    if start is None or end is None:
        return items
    in_window: List[Dict[str, Any]] = []
    for item in items:
        ts = _parse_row_timestamp(item)
        if ts is not None and start <= ts <= end:
            in_window.append(item)
    if in_window:
        for item in in_window:
            item["in_time_window"] = True
        return in_window
    for item in items:
        item["in_time_window"] = False
    return items


def _apply_filter_manifest_rows(
    rows: List[Dict[str, Any]],
    filter_manifest: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not filter_manifest:
        return rows
    window = filter_manifest.get("rolling_window") or {}
    days = int(window.get("days") or 0)
    if days <= 0:
        return rows
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept: List[Dict[str, Any]] = []
    for row in rows:
        ts = _parse_row_timestamp(row)
        if ts is None or ts >= cutoff:
            kept.append(row)
    return kept


def _goal_relevance(goal_text: str, query_text: str) -> float:
    text = str(goal_text or "").strip()
    if not text:
        return 0.0
    tokens = _query_tokens(query_text)
    if not tokens:
        return _GOAL_SUMMARY_BOOST
    blob = text.lower()
    overlap = sum(1 for token in tokens if token in blob)
    if overlap == 0:
        return 0.72
    return min(1.0, 0.75 + overlap / len(tokens) * 0.25)


def _load_user_goal_summaries(
    query_text: str,
    *,
    source_ids: Optional[List[str]] = None,
    limit: int = _SUMMARY_ITEM_CAP,
    conn: Optional[Any] = None,
    time_range: Optional[Tuple[str, str]] = None,
) -> List[Dict[str, Any]]:
    try:
        # Prefer the query's own connection (multi-db verification, seeded evals);
        # the global singleton may point at a different database.
        if conn is None:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        if conn is None:
            return []
        params: List[Any] = []
        # event_at is the source message's time from the unified timeline
        # (user_goals.record_id = message_id); created_at is only ingest time.
        # Without a date field goals can never answer "yesterday's goals" and
        # dodge recency decay entirely.
        select = (
            "SELECT g.goal_id, g.record_id, g.source_id, g.goal_text, g.created_at,"
            " (SELECT MAX(t.event_at) FROM timeline t WHERE t.record_id = g.record_id)"
            " FROM user_goals g"
        )
        where = ""
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            where = f" WHERE g.source_id IN ({placeholders})"
            params.extend(source_ids)
        tail = " ORDER BY g.created_at DESC LIMIT ?"
        params.append(max(limit * 3, 50))
        try:
            rows = conn.execute(select + where + tail, tuple(params)).fetchall()
        except Exception:
            # Databases predating the timeline projection: dated by ingest only.
            rows = [
                (*row, None)
                for row in conn.execute(
                    "SELECT goal_id, record_id, source_id, goal_text, created_at"
                    " FROM user_goals" + where.replace("g.", "") + tail.replace("g.", ""),
                    tuple(params),
                ).fetchall()
            ]
        items: List[Dict[str, Any]] = []
        tokens = _query_tokens(query_text)
        query_lower = (query_text or "").lower()
        goal_intent = any(term in query_lower for term in _EXTRA_SURFACE_TERMS)
        seen_texts: set = set()
        for goal_id, record_id, source_id, goal_text, created_at, event_at in rows:
            text = str(goal_text or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen_texts:
                continue
            token_match = bool(tokens) and any(token in key for token in tokens)
            # A goal rides on token overlap OR on explicit goal intent ("what are
            # my goals") — never as unconditional filler.
            if tokens and not token_match and not goal_intent:
                continue
            seen_texts.add(key)
            items.append(
                {
                    "topic": text,
                    "summary_text": text,
                    "goal_id": goal_id,
                    "record_id": record_id,
                    "source_id": source_id,
                    "dimension": "work",
                    "event_at": event_at or created_at,
                    "created_at": created_at,
                    "relevance_score": round(_goal_relevance(text, query_text), 4),
                    "retrieval_source": "user_goal",
                }
            )
        items = _prefer_time_window(items, time_range)
        items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
        return items[:limit]
    except Exception as exc:
        logger.debug("user_goals load skipped: %s", exc)
        return []


def _list_canonical_rows(
    adapters: AdapterBundle,
    table: str,
    *,
    source_ids: List[str],
    limit: int = 100,
    disclosure_tier: str = "owner_raw",
    contains: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    # canonical.list() already applies the disclosure tier (SQL adapters via the
    # per-table _disclosure spec; in-memory fake via apply_disclosure_tier_to_rows), so the
    # rows returned here are ALREADY disclosed to `disclosure_tier`. Re-applying the swap
    # would over-redact: SQL-disclosed rows arrive as redacted text with no disclosure
    # column, which the swap mistakes for pending raw and overwrites with the placeholder.
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    candidates = source_ids or [None]
    for source_id in candidates:
        try:
            page = adapters.canonical.list(
                table,
                limit=limit,
                offset=0,
                source_id=source_id,
                disclosure_tier=disclosure_tier,
                contains=contains,
            )
        except TypeError:
            # Adapter predates the contains filter — fall back to a plain page.
            page = adapters.canonical.list(
                table,
                limit=limit,
                offset=0,
                source_id=source_id,
                disclosure_tier=disclosure_tier,
            )
        for row in page.items:
            record_id = str(row.get("record_id") or row.get("message_id") or "")
            # contact_identifiers rows share record_id (= contact_id) across
            # distinct identifiers — the identifier is part of the row identity.
            key = (record_id + "|" + str(row.get("identifier") or "")) if record_id else str(row)
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item.setdefault("_table", table)
            rows.append(item)
    # Global recency: each source's page is already newest-first, but sources
    # are concatenated in manifest order — without a merge sort the first
    # source's (often demo-fixture) rows outrank every real source's newest.
    def _ts(row: Dict[str, Any]) -> str:
        return str(row.get("event_at") or row.get("starts_at") or row.get("entry_at") or "")

    if any(_ts(r) for r in rows):
        rows.sort(key=_ts, reverse=True)
    return rows[:limit]


def _row_summary_text(table: str, row: Dict[str, Any], *, scope_id: str = "") -> str:
    import json as _json

    if table == "profile_records":
        parts = [row.get("record_type"), row.get("title"), row.get("organization"), row.get("description")]
        return " — ".join(str(p).strip() for p in parts if p)
    if table == "calendar_events":
        if scope_id == "availability:read":
            # Read the real is_busy column (B6) — not metadata_json, which the
            # Google Calendar lane doesn't even populate with is_busy.
            is_busy = row.get("is_busy")
            busy = True if is_busy is None else bool(is_busy)
            label = "open window" if not busy else "busy block"
            return f"{label} {row.get('starts_at')} — {row.get('ends_at')}"
        title = str(row.get("title") or "").strip()
        human_date = _human_date_from_iso(str(row.get("starts_at") or ""))
        parts = [title]
        if human_date:
            parts.append(human_date)
        parts.append(f"{row.get('starts_at')} — {row.get('ends_at')}")
        return " ".join(p for p in parts if p).strip()
    if table == "contacts":
        return str(row.get("display_name") or "")
    if table == "contact_identifiers":
        return f"{row.get('identifier_type')}: {row.get('identifier')}"
    if table == "financial_transactions":
        return " — ".join(
            str(row.get(field) or "")
            for field in ("description", "category", "amount", "account")
            if row.get(field)
        )
    if table == "location_events":
        parts = [
            str(row.get(field) or "")
            for field in ("place_name", "city", "event_type", "occurred_at")
            if row.get(field)
        ]
        human_date = _human_date_from_iso(
            str(row.get("occurred_at") or row.get("starts_at") or "")
        )
        if human_date:
            parts.append(human_date)
        return " — ".join(parts)
    if table == "journal_entries":
        parts = [str(row.get(field) or "") for field in ("content", "mood_tag", "category", "people", "place_name") if row.get(field)]
        human_date = _human_date_from_iso(str(row.get("entry_at") or ""))
        if human_date:
            parts.append(human_date)
        meta = row.get("metadata_json")
        if isinstance(meta, str):
            try:
                import json as _json

                meta = _json.loads(meta)
            except _json.JSONDecodeError:
                meta = {}
        if isinstance(meta, dict):
            ends_at = str(row.get("ends_at") or meta.get("ends_at") or "").strip()
            if ends_at and row.get("entry_at"):
                parts.insert(0, f"{row.get('entry_at')} — {ends_at}")
            duration = meta.get("duration_minutes")
            if duration:
                parts.append(f"{duration} min")
        return " — ".join(parts)
    return " ".join(
        str(row.get(field) or "")
        for field in ("title", "description", "content", "place_name", "display_name")
        if row.get(field)
    ).strip()


def _redact_row_for_scope(scope_id: str, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
    import json as _json

    if scope_id != "availability:read":
        return row
    out = dict(row)
    out.pop("title", None)
    meta = out.get("metadata_json")
    if isinstance(meta, dict):
        meta = {k: v for k, v in meta.items() if k not in ("attendees",)}
    elif isinstance(meta, str):
        try:
            parsed = _json.loads(meta)
            parsed.pop("attendees", None)
            meta = parsed
        except _json.JSONDecodeError:
            pass
    out["metadata_json"] = meta
    out.pop("content", None)
    return out


def _canonical_relevance(text: str, query_text: str) -> float:
    tokens = _query_tokens(query_text)
    blob = text.lower()
    if not tokens:
        return 0.55
    overlap = sum(1 for token in tokens if token in blob)
    if overlap == 0:
        return 0.35
    return min(1.0, 0.6 + overlap / len(tokens) * 0.4)


def _route_canonical_rows(
    adapters: AdapterBundle,
    table: str,
    *,
    manifest: ScopeResolutionManifest,
    query_text: str,
    source_ids: List[str],
    limit: int,
    disclosure_tier: str,
    rare_query_tokens: Optional[List[str]] = None,
    browse_fallback: bool = False,
) -> List[Dict[str, Any]]:
    """Router: content tokens must MATCH rows (full-table SQL filter); a query
    that only names the surface (or has no tokens) BROWSES recent rows; a
    specific ask that matches nothing contributes nothing — no unfiltered
    fallback page. Fabricated topics must come back empty.

    browse_fallback=True (inference mode) keeps the recency browse even without
    surface intent: the inference packet is derived existence signal, not
    content, and the answerer needs candidates — but the rare-token honesty
    check still applies, so unanswerable specifics stay empty."""
    query_lower = (query_text or "").lower()
    tokens = _query_tokens(query_text)
    residual = _residual_content_tokens(tokens, tables=[table])
    date_hints = _iso_date_hints(query_text)

    if not tokens and not date_hints:
        # No query content at all — recency browse.
        return _list_canonical_rows(
            adapters, table, source_ids=source_ids, limit=limit,
            disclosure_tier=disclosure_tier,
        )

    matched: List[Dict[str, Any]] = []
    if residual or date_hints:
        matched = _list_canonical_rows(
            adapters, table, source_ids=source_ids, limit=limit,
            disclosure_tier=disclosure_tier,
            contains=[*residual, *date_hints],
        )
    if matched:
        if table == "calendar_events":
            all_rows = _list_canonical_rows(
                adapters, table, source_ids=source_ids, limit=max(limit, 100),
                disclosure_tier=disclosure_tier,
            )
            matched = _expand_calendar_week_context(matched, all_rows, query_text)
        return matched

    # Identifiers join their contact: "find the contact record for Jessica"
    # must surface Jessica's phone/email rows even though those rows don't
    # contain her name — match contacts by name, then identifiers by contact id.
    if table == "contact_identifiers" and residual:
        contact_rows = _list_canonical_rows(
            adapters, "contacts", source_ids=source_ids, limit=20,
            disclosure_tier=disclosure_tier, contains=residual,
        )
        ids = [str(r.get("record_id") or "") for r in contact_rows if r.get("record_id")]
        if ids:
            # No source filter here: identifier rows carry the provenance of the
            # channel that observed them (imessage/signal/'*'), not the contact's
            # source — the contact itself is already scope-authorized.
            ident_rows = _list_canonical_rows(
                adapters, table, source_ids=[], limit=limit,
                disclosure_tier=disclosure_tier, contains=ids,
            )
            if ident_rows:
                return ident_rows

    # Nothing matched the content tokens. Browse is honest only when the surface
    # itself was asked for AND the ask carried no effectively-absent token
    # (df ≤ 2 — zero, or a porter-stem collision) — a term the corpus does not
    # contain means the specific thing isn't there. Weakly-rare df>2 framing
    # ('spend') and answer-shape words (stoplisted upstream) must not block.
    rare_dfs = (
        dict(rare_query_tokens)
        if isinstance(rare_query_tokens, dict)
        else {t: 0 for t in (rare_query_tokens or [])}
    )
    if any(df <= 2 and t in residual for t, df in rare_dfs.items()):
        return []
    work_profile = manifest.scope_id == "work_context:read" and table == "profile_records"
    if _surface_intent(table, query_lower) or work_profile or browse_fallback:
        return _list_canonical_rows(
            adapters, table, source_ids=source_ids, limit=limit,
            disclosure_tier=disclosure_tier,
        )
    return []


def _load_canonical_summary_items(
    *,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    query_text: str,
    source_ids: List[str],
    disclosure_tier: str = "owner_raw",
    rare_query_tokens: Optional[List[str]] = None,
    browse_fallback: bool = False,
    plan=None,
    conn: Optional[Any] = None,
    exposure_visible: bool = True,
) -> List[Dict[str, Any]]:
    # First-person asks (P3.3): message rows get owner-authorship treatment —
    # belief/identity asks hard-filter to owner-authored rows (another person's
    # stance must not enter the answer at all); the broader first-person flag
    # re-ranks owner rows first and prefixes non-owner rows with their speaker
    # so nothing they say can read as the owner's words. General recall
    # queries (plan absent / flags off) are byte-identical to before.
    first_person = bool(getattr(plan, "first_person_intent", False)) if plan else False
    belief_intent = bool(getattr(plan, "first_person_belief", False)) if plan else False
    role_cache: Dict[str, Optional[bool]] = {}
    display_cache: Dict[str, str] = {}
    highlight_cache: Dict[str, str] = {}

    items: List[Dict[str, Any]] = []
    for table in manifest.canonical_tables or []:
        rows = _route_canonical_rows(
            adapters,
            table,
            manifest=manifest,
            query_text=query_text,
            source_ids=source_ids,
            limit=50,
            disclosure_tier=disclosure_tier,
            rare_query_tokens=rare_query_tokens,
            browse_fallback=browse_fallback,
        )
        for row in rows:
            owner: Optional[bool] = None
            if first_person and table in _MESSAGE_TABLES:
                owner = _message_row_owner(table, row, conn, role_cache)
                if belief_intent and owner is not True:
                    continue  # belief/identity composition: owner-authored only
            clean = _redact_row_for_scope(manifest.scope_id, table, row)
            text = _row_summary_text(table, clean, scope_id=manifest.scope_id)
            # IMB9 / P2.1: an engaged browser row (highlight/star) carries the
            # owner's Annotate-grade span in metadata_json.highlight, which the
            # canonical list adapter never selects — pull it in so the needle
            # ("copper still method") is retrievable. page_excerpt (the page
            # author's words) is deliberately never surfaced by the lookup.
            if table == "activity_events":
                event_id = str(clean.get("record_id") or clean.get("event_id") or "")
                span = _activity_highlight_text(conn, event_id, clean, highlight_cache)
                if span:
                    text = f"{span} — {text}".strip(" —") if text else span
                elif not exposure_visible:
                    # P1.5: exposure profile off — a non-engaged browse row is
                    # exposure-only (never the owner's expression); it must not
                    # answer an interest/identity ask. Engaged rows (span set)
                    # are Annotate-grade expression and always survive.
                    continue
            if not text:
                continue
            if belief_intent and owner is True and table in _MESSAGE_TABLES and _belief_about_other(text):
                continue  # owner-authored but about a third party — not the owner's belief
            speaker = ""
            if first_person and owner is False:
                if table == "ai_chat_messages":
                    speaker = str(row.get("sender_type") or "assistant").strip() or "assistant"
                else:
                    speaker = _sender_display(
                        conn, str(row.get("sender_id") or ""), display_cache
                    )
                if speaker:
                    text = f"[{speaker}] {text}"
            item = {
                "topic": text[:120],
                "summary_text": text,
                "record_id": clean.get("record_id")
                or clean.get("event_id")
                or clean.get("contact_id")
                or clean.get("message_id"),
                "source_id": clean.get("source_id"),
                "relevance_score": round(_canonical_relevance(text, query_text), 4),
                "retrieval_source": f"canonical:{table}",
            }
            # B8 / GEN-judged IMB: speaker_label + owner_authored survive inference
            # stripping of topic/summary_text so the generative answer can attribute
            # non-owner evidence without a raw-content side channel.
            if speaker:
                item["speaker_label"] = speaker
            # Per-table event-time column (same keys the recency sort in
            # _list_canonical_rows uses); without it canonical rows can neither
            # decay in fusion nor answer a date-scoped ask.
            for ts_key in ("event_at", "starts_at", "occurred_at", "entry_at"):
                ts_val = clean.get(ts_key) or row.get(ts_key)
                if ts_val:
                    item["event_at"] = ts_val
                    break
            if owner is not None:
                item["owner_authored"] = owner
            items.append(item)
    items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
    if first_person:
        # Strong downweight, not a drop: owner-authored rows first, exempt
        # (non-message) rows keep relative order, other-authored rows last.
        items.sort(key=lambda item: _owner_rank(item.get("owner_authored")))
    return items[:_SUMMARY_ITEM_CAP]


def _load_emotion_summary_items(
    conn: Optional[Any],
    *,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """Role-filtered emotion aggregate for mood/emotion asks (D1.8 wire).

    Same authorship filter as MCP message joins: keep ``authored`` /
    ``addressed`` / legacy NULL; drop ``observed`` (other people's affect).
    Returns at most one summary item plus optional per-label detail rows.
    """
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT emotion_label, COUNT(*) AS n, AVG(confidence) AS avg_conf
            FROM message_emotions
            WHERE emotion_label IS NOT NULL
              AND TRIM(emotion_label) != ''
              AND (role IS NULL OR role IN ('authored', 'addressed'))
            GROUP BY emotion_label
            ORDER BY n DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    except Exception as exc:
        logger.debug("message_emotions load skipped: %s", exc)
        return []
    if not rows:
        return []
    parts = [
        f"{str(label)} ({int(n)})"
        for label, n, _avg in rows
        if label is not None
    ]
    if not parts:
        return []
    body = "Owner authored/addressed emotion signals: " + ", ".join(parts) + "."
    top_label = str(rows[0][0])
    top_n = int(rows[0][1] or 0)
    items: List[Dict[str, Any]] = [
        {
            "topic": "owner emotion signals",
            "summary_text": body,
            "emotion_label": top_label,
            "emotion_count": top_n,
            "relevance_score": 0.9,
            "retrieval_source": "message_emotions",
            "dimension": "wellbeing",
        }
    ]
    for label, n, avg_conf in rows[:3]:
        items.append(
            {
                "topic": f"emotion:{label}",
                "summary_text": f"Emotion signal {label}: {int(n)} messages"
                + (
                    f" (avg confidence {float(avg_conf):.2f})"
                    if isinstance(avg_conf, (int, float))
                    else ""
                ),
                "emotion_label": str(label),
                "emotion_count": int(n or 0),
                "relevance_score": 0.82,
                "retrieval_source": "message_emotions",
                "dimension": "wellbeing",
            }
        )
    return items


def _load_complexity_summary_items(conn: Optional[Any]) -> List[Dict[str, Any]]:
    """Latest cached complexity snapshot as summary items — the
    complexity:read scope's content (PLAN_COMPLEXITY_DATA_PAGE.md M3).
    Reads the derived complexity_snapshots cache only; never recomputes on
    the query path."""
    if conn is None:
        return []
    try:
        from ..features.complexity.store import load_latest_summary

        summary = load_latest_summary(conn)
    except Exception:
        return []
    if not isinstance(summary, dict) or not summary:
        return []
    items: List[Dict[str, Any]] = []
    day = str(summary.get("computed_at") or "")[:10]
    readings = summary.get("readings") or {}

    def _score(key: str) -> Optional[float]:
        block = readings.get(key) or {}
        value = block.get("score")
        return float(value) if isinstance(value, (int, float)) else None

    focus = readings.get("current_focus") or summary.get("focus_index") or {}
    parts = [f"Structure readings {day}".strip()]
    scores = []
    for key, label in (
        ("current_focus", "focus"),
        ("structural_clarity", "structural clarity"),
        ("information_breadth", "information breadth"),
        ("pipeline_confidence", "pipeline confidence"),
    ):
        value = _score(key)
        if value is not None:
            scores.append(f"{label} {value:.0f}/100")
    if scores:
        parts.append(", ".join(scores))
    baseline = (focus.get("baseline") or {}) if isinstance(focus, dict) else {}
    if baseline.get("status") == "ok" and isinstance(baseline.get("percentile"), (int, float)):
        parts.append(f"focus at p{int(baseline['percentile'] * 100)} of the trailing 12 weeks")
    interpretation = str(focus.get("interpretation") or "").strip()
    if interpretation:
        parts.append(interpretation)
    text = ". ".join(part for part in parts if part)
    items.append({
        "topic": f"Structure readings {day}".strip(),
        "summary_text": text,
        "record_id": "complexity:summary_latest",
        "retrieval_source": "complexity_summary",
    })

    threads = summary.get("influence_threads") or []
    lines = []
    for thread in threads[:5]:
        if not isinstance(thread, dict):
            continue
        status = str(thread.get("epistemic_status") or "").replace("_", " ")
        lines.append(
            f"{thread.get('source_label')} → {thread.get('target_label')}"
            + (f" ({status})" if status else "")
        )
    if lines:
        items.append({
            "topic": "Influence threads",
            "summary_text": "Influence threads: " + "; ".join(lines),
            "record_id": "complexity:influence_latest",
            "retrieval_source": "complexity_influence",
        })
    return items


def _load_attention_summary_items(conn: Optional[Any], limit: int = 10) -> List[Dict[str, Any]]:
    """Latest attention-triage objects (daily digests + interest profiles) as
    summary items — the attention:read scope's primary content
    (PLAN_ATTENTION_TRIAGE.md M2). The attention_summary payload already
    enforces the silence invariant (no discard references), so shaping is safe."""
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT object_type, object_key, payload_json FROM signal_objects "
            "WHERE signal_dimension='interests' AND valid_to IS NULL "
            "AND object_type IN ('attention_summary','interest_profile') "
            "ORDER BY substr(object_key, -10) DESC, object_type ASC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:
        return []
    items: List[Dict[str, Any]] = []
    for otype, okey, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            continue
        day = payload.get("day") or payload.get("asof") or str(okey).split(":")[-1]
        if otype == "attention_summary":
            valid = payload.get("movers_valid")
            if valid:
                movers = ", ".join(str(v) for v in valid[:3])
            else:
                movers = ", ".join(str(v) for v, _c in (payload.get("movers") or [])[:3])
            surface = "; ".join(
                str(s.get("title") or "") for s in (payload.get("surface") or [])[:3])
            distraction = "; ".join(
                f"{d.get('group')} x{d.get('count')}"
                for d in (payload.get("distraction_patterns") or [])[:3])
            pctl = payload.get("day_kl_percentile")
            surprise = (f"surprise p{int(pctl * 100)}" if isinstance(pctl, (int, float))
                        else f"KL={payload.get('day_kl')}")
            parts = [f"Attention digest {day} ({surprise})"]
            if movers:
                # A surprise is an event, not a state: narrators were re-telling
                # a days-old spike in the present tense because the movers list
                # carried no date of its own. Age is computed at query time so
                # the qualifier stays true however long the object is served.
                age = None
                try:
                    age = (datetime.now(timezone.utc).date()
                           - datetime.strptime(str(day)[:10], "%Y-%m-%d").date()).days
                except ValueError:
                    pass
                when = (f"on {day}" if age is None
                        else "today" if age <= 0
                        else "yesterday" if age == 1
                        else f"on {day}, {age} days ago — not current")
                parts.append(f"surprise movers ({when}): {movers}")
            if surface:
                parts.append(f"missed-but-matters: {surface}")
            if distraction:
                parts.append(f"distraction patterns: {distraction}")
            text = ". ".join(parts)
        else:
            top = ", ".join(str(v) for v, _c in (payload.get("top_vocab") or [])[:6])
            text = f"Interest profile {day}: top interests {top}"
        items.append({
            "topic": text.split(" (KL", 1)[0].split(": top", 1)[0],
            "summary_text": text,
            "record_id": okey,
            "retrieval_source": otype,
        })
    return items


# Intent → time-objects routing (minimal-disclosure pass): a grantee's summary
# answer carries only the layers their question is about. No keyword match ⇒
# the compact availability digest alone — never the full bundle by default.
_TIME_ASPECT_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    # "meet"/"meeting" is deliberately absent: it appears in load and
    # commitment questions constantly and would drag the digest into every
    # answer, defeating intent-proportionality.
    "availability_summary": (
        "free", "open", "window", "availab", "slot", "space", "spot",
        "minute", "session", "call",
    ),
    "flex_windows": (
        "negotiab", "flex", "mov", "shift", "reschedul", "conditional",
        "give", "immovable", "soft", "earlier", "run past",
    ),
    "meeting_load_band": (
        "load", "busy hours", "bandwidth", "capacity", "heavy", "light",
        "how busy", "hours",
    ),
    "routine_confidence": (
        "rhythm", "active", "responsive", "routine", "predictab", "habit",
        "pattern", "morning", "evening", "night", "usually",
    ),
    "Commitment": (
        "commitment", "recurring", "standing", "weekly", "collide",
    ),
}


def _time_aspects_for_query(query_text: str) -> set:
    lowered = str(query_text or "").lower()
    wanted = {
        otype
        for otype, keywords in _TIME_ASPECT_KEYWORDS.items()
        if any(k in lowered for k in keywords)
    }
    return wanted or {"availability_summary"}


def _load_time_summary_items(
    conn: Optional[Any], query_text: str = "", limit: int = 8
) -> List[Dict[str, Any]]:
    """Derived time-dimension objects as summary items — the availability:read
    scope's negotiability layer (PLAN_TIME_SIGNAL_UPGRADE M3). Payloads are
    title/attendee-free by construction; raw movability scores stay owner-side,
    only bands are phrased here. Items are filtered to the ASPECT the query is
    about (minimal disclosure): a load question never also reveals commitments."""
    if conn is None:
        return []
    wanted = _time_aspects_for_query(query_text)
    object_types = sorted(wanted - {"Commitment"})
    if not object_types and "Commitment" not in wanted:
        return []
    rows = []
    if object_types:
        placeholders = ",".join("?" for _ in object_types)
        try:
            rows = conn.execute(
                "SELECT object_type, object_key, payload_json FROM signal_objects "
                "WHERE signal_dimension='time' AND valid_to IS NULL "
                f"AND object_type IN ({placeholders}) "
                "ORDER BY object_type ASC LIMIT ?",
                (*object_types, limit),
            ).fetchall()
        except Exception:
            return []
    items: List[Dict[str, Any]] = []
    for otype, okey, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            continue
        if otype == "availability_summary":
            text = str(payload.get("summary_text") or "")
        elif otype == "meeting_load_band":
            text = (
                f"Meeting load ({payload.get('band')}): "
                f"{payload.get('busy_hours_7d')} busy hours in the last 7 days; "
                f"{payload.get('soft_count')} of "
                f"{int(payload.get('hard_count') or 0) + int(payload.get('soft_count') or 0)} "
                "blocks negotiable"
            )
        elif otype == "flex_windows":
            windows = payload.get("windows") or []
            spans = "; ".join(
                f"{w.get('start')} — {w.get('end')} ({w.get('negotiability')}; "
                f"soft shoulders {(w.get('flex_before') or {}).get('start')} → "
                f"{(w.get('flex_after') or {}).get('end')})"
                for w in windows[:4]
                if isinstance(w, dict)
            )
            text = f"Conditionally available (negotiable busy time): {spans}"
        else:
            bands = "; ".join(
                f"{b.get('day_of_week')} {b.get('time_band')} ({b.get('dominant_kind')})"
                for b in (payload.get("top_bands") or [])[:3]
                if isinstance(b, dict)
            )
            text = (
                f"Activity rhythm (confidence {payload.get('confidence')}): "
                f"typically active {bands}" if bands else ""
            )
        if not text:
            continue
        items.append({
            "topic": text.split(":", 1)[0],
            "summary_text": text,
            "record_id": f"{otype}:{okey}",
            "retrieval_source": otype,
        })
    commitment_rows = []
    if "Commitment" in wanted:
        try:
            commitment_rows = conn.execute(
                "SELECT object_key, payload_json FROM signal_objects "
                "WHERE signal_dimension='time' AND valid_to IS NULL "
                "AND object_type='Commitment' ORDER BY object_key LIMIT 6",
            ).fetchall()
        except Exception:
            commitment_rows = []
    if commitment_rows:
        parts = []
        for okey, payload_json in commitment_rows:
            try:
                payload = json.loads(payload_json or "{}")
            except (TypeError, ValueError):
                continue
            parts.append(
                f"{payload.get('day_of_week')} {payload.get('start_clock')} "
                f"({payload.get('kind')}, {payload.get('movability_band') or 'unknown'}, "
                f"~{payload.get('load_weight')}h/wk)"
            )
        if parts:
            text = "Recurring commitments: " + "; ".join(parts)
            items.append({
                "topic": "Recurring commitments",
                "summary_text": text,
                "record_id": "Commitment:rollup",
                "retrieval_source": "Commitment",
            })
    return items


def _availability_band(conn: Optional[Any], query_text: str) -> Optional[Dict[str, Any]]:
    """Minimum-disclosure availability verdict for inference mode: one band,
    one confidence — target window in, band out (PLAN_TIME_SIGNAL_UPGRADE §
    minimal-disclosure pass). Bands: overlap_found | negotiable_overlap |
    no_overlap | unknown. Nothing else crosses."""
    if conn is None:
        return None
    try:
        from ..features.fit.evaluator import evaluate_opportunity

        hints = _iso_date_hints(query_text or "")
        context = {"target_window_start": hints[0]} if hints else {}
        result = evaluate_opportunity(conn, "schedule_meeting", context=context)
    except Exception:
        return None
    timing = next(
        (f for f in result.get("facet_results") or [] if f.get("facet_id") == "timing_feasibility"),
        None,
    )
    if not timing:
        return None
    return {
        "band": str(timing.get("public_band") or "unknown"),
        "confidence": float(timing.get("confidence") or 0.0),
    }


def _load_brief_summary_items(
    dimensions: List[str], *, conn: Optional[Any] = None
) -> List[Dict[str, Any]]:
    try:
        if conn is None:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        if conn is None:
            return []
        items: List[Dict[str, Any]] = []
        for dim in dimensions:
            dim_key = str(dim or "").strip().lower()
            if not dim_key:
                continue
            row = conn.execute(
                "SELECT markdown_body FROM signal_dimension_briefs WHERE signal_dimension=?",
                (dim_key,),
            ).fetchone()
            if not row or not row[0]:
                continue
            text = str(row[0])[:2500]
            items.append(
                {
                    "topic": f"{dim_key} brief",
                    "summary_text": text,
                    "dimension": dim_key,
                    "relevance_score": 0.82,
                    "retrieval_source": "dimension_brief",
                }
            )
        return items
    except Exception as exc:
        logger.debug("dimension brief load skipped: %s", exc)
        return []


def _mode_allowed(requested: AccessMode, ceiling: str) -> bool:
    req_rank = MODE_RANK.get(str(requested))
    if req_rank is None:
        return False
    return req_rank <= MODE_RANK.get(str(ceiling), MODE_RANK["inference"])


def _strip_forbidden(data: Any, forbidden: List[str]) -> Any:
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in forbidden or any(f in k for f in forbidden):
                continue
            out[k] = _strip_forbidden(v, forbidden)
        return out
    if isinstance(data, list):
        return [_strip_forbidden(item, forbidden) for item in data]
    return data


def _query_tokens(query_text: str) -> List[str]:
    stop = frozenset(
        {
            "the",
            "for",
            "what",
            "who",
            "whom",
            "how",
            "any",
            "are",
            "was",
            "did",
            "has",
            "have",
            "this",
            "that",
            "with",
            "from",
            "about",
            "when",
            "where",
            "give",
            "name",
            "one",
            "line",
            "mid",
            "day",
            "free",
            "busy",
            "can",
            "you",
            "their",
            "them",
            "there",
            "and",
            "not",
            "based",
            "into",
            "without",
            # query framing — never content
            "which",
            "does",
            "show",
            "find",
            "tell",
            "know",
            # framing adverbs ("what do I actually think") — emphasis, never
            # content; as rare tokens they vetoed answerable first-person asks
            "actually",
            "really",
            "truly",
            "most",
            "often",
            "usually",
            "typically",
            "involving",
            "list",
            "whats",
            "record",
            "records",
            "long",
            "much",
            "many",
            "time",
            "say",
            "says",
            "everything",
            "anything",
            "something",
            "stuff",
            "year",
            "years",
            # answer-shape words: they describe the KIND of aggregate wanted,
            # never row content — derived layers answer them without containing
            # them ('cadence' df 1 in a corpus whose stat tags say "every 5.8 h")
            "cadence",
            "frequency",
            "frequently",
            "rhythm",
            "pattern",
            "patterns",
            "habit",
            "habits",
            "routine",
            "routines",
            "trend",
            "trends",
            "average",
            "typical",
            # "what did I TAKE AWAY / my TAKEAWAY from my READING" — the framing
            # of a recall ask, never row content. A highlight span ("copper still
            # method") never contains these words, so as rare tokens (df ≤ 2)
            # they vetoed an answerable browser-highlight ask (IMB9). Stoplisting
            # them here removes them from residual/rare entirely; the real
            # content tokens ("fermentation", "methods") still match rows.
            "take",
            "takes",
            "took",
            "taken",
            "taking",
            "away",
            "takeaway",
            "takeaways",
            "reading",
            "read",
            "reads",
            # Work-phrasing leftovers after surface strip ("working toward",
            # "projects I'm focused on") — answer shape, never goal text.
            "toward",
            "towards",
            "focused",
            "focus",
        }
    )
    return [
        token
        for token in dict.fromkeys(re.findall(r"[a-z0-9]{3,}", (query_text or "").lower()))
        if token not in stop
    ] + _calendar_day_tokens(query_text)


def _calendar_day_tokens(query_text: str) -> List[str]:
    """Include short day numbers when a month name is present (e.g. March 13)."""
    text = query_text or ""
    if not re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b",
        text,
        re.I,
    ):
        return []
    return re.findall(r"\b(\d{1,2})\b", text)


def _filter_rows_by_query(rows: List[Dict[str, Any]], query_text: str) -> List[Dict[str, Any]]:
    tokens = _query_tokens(query_text)
    if not tokens:
        return rows
    matched: List[Dict[str, Any]] = []
    for row in rows:
        haystack = " ".join(
            str(row.get(field) or "")
            for field in (
                "content",
                "content_preview",
                "title",
                "text",
                "body",
                "description",
                "organization",
                "record_type",
                "display_name",
                "starts_at",
                "ends_at",
                "entry_at",
                "occurred_at",
                "place_name",
                "category",
                "amount",
                "mood_tag",
            )
        ).lower()
        if any(token in haystack for token in tokens):
            matched.append(row)
    if matched:
        return matched
    date_hints = _iso_date_hints(query_text)
    if date_hints:
        dated = [
            row
            for row in rows
            if any(
                hint in str(row.get("starts_at") or row.get("entry_at") or row.get("occurred_at") or "")
                for hint in date_hints
            )
        ]
        if dated:
            return dated
    return matched


_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

#: Abbreviation → full name, so range parsing accepts either spelling. "may" has no
#: abbreviated form here because it has none in the wild; the spelled-out form is guarded
#: instead by `_may_is_month`, since "may" is also a common verb.
_MONTH_ALIASES = {name: name for name in _MONTHS}
_MONTH_ALIASES.update({
    "jan": "january", "feb": "february", "mar": "march", "apr": "april",
    "jun": "june", "jul": "july", "aug": "august", "sep": "september",
    "sept": "september", "oct": "october", "nov": "november", "dec": "december",
})


def _iso_date_hint(query_text: str) -> Optional[str]:
    hints = _iso_date_hints(query_text)
    return hints[0] if hints else None


#: A day range inside ONE month: "Aug 11–16", "August 11-16", "Aug 11 to 16".
#: Without this, only the first endpoint is found and `_explicit_time_range` — which
#: takes min/max of the hints — collapses to a single day while still returning a
#: perfectly valid-looking window. Observed 2026-08-17: "a work report for Aug 11–16,
#: 2026" searched Aug 11 alone and reported the rest of the week as unsynced. The
#: repeated-month ("Aug 11 to Aug 16") and cross-month ("Aug 28 - Sep 3") forms already
#: worked, which is what made the gap easy to miss: the failure needs the *compact*
#: spelling, which is also the one people reach for first.
_MONTH_NAME_RE = (
    r"january|february|march|april|may|june|july|august|september|october|november|"
    r"december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DAY_RANGE_RE = re.compile(
    rf"\b({_MONTH_NAME_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    r"\s*(?:-|--|–|—|to|through|thru|until|til|till)\s*"
    r"(\d{1,2})(?:st|nd|rd|th)?\b",
    re.I,
)

#: The day part following a month word: "11", "11th", "11-16", "11th to 16th".
#: Used only to skip past the day(s) so `_may_is_month` can look at what comes AFTER.
_DAY_PART_RE = re.compile(
    r"\.?\s+\d{1,2}(?P<ord1>st|nd|rd|th)?"
    r"(?:\s*(?:-|--|–|—|to|through|thru|until|til|till)\s*\d{1,2}(?P<ord2>st|nd|rd|th)?)?"
)
#: What makes a lowercase "may 11" a date rather than a modal verb plus a quantity:
#: a date-like comma ("may 11, 2026") or an adjacent year ("may 11 2026").
_MAY_DATE_CONTEXT_RE = re.compile(r"\s*,|\s*(?:of\s+)?20\d{2}\b", re.I)


def _may_is_month(token: str, text: str, token_end: int) -> bool:
    """Is this "may" the month, or the modal verb?

    "may" is the one month name that is also an everyday verb, so — unlike "march" or
    "august", which are rare enough as words that a following number settles it — a
    number is not evidence: "I may 11 times reconsider" is not a date. A false hint here
    is quiet, not loud: `_explicit_time_range` takes min/max of the hints, so the query
    still returns a well-formed window and simply looks like thin data (the same failure
    shape as the `Aug 11-16` range collapse). The *abbreviation* pattern in
    `_iso_date_hints` has always omitted "may" for this reason; this is that guard, made
    consistent.

    Anchored on the "may" token rather than on any one pattern's match, so `_DAY_RANGE_RE`
    and both single-date patterns necessarily agree: a range and its first endpoint can
    never disagree about whether "may" was a month.
    """
    if token.lower() != "may":
        return True
    if token[0] == "M":  # "May 11" — the capital is the writer disambiguating for us
        return True
    day_part = _DAY_PART_RE.match(text, token_end)
    if day_part is None:
        return False
    if day_part.group("ord1") or day_part.group("ord2"):  # "may 11th" is never a verb
        return True
    return _MAY_DATE_CONTEXT_RE.match(text, day_part.end()) is not None


def _iso_date_hints(query_text: str) -> List[str]:
    text = query_text or ""
    year_match = re.search(r"\b(20\d{2})\b", text)
    year = int(year_match.group(1)) if year_match else datetime.now(timezone.utc).year
    hints: List[str] = []

    def _add(month: int, day: int) -> None:
        try:
            datetime(year, month, day)  # "Feb 30" is not a date; drop it silently
        except ValueError:
            return
        iso = f"{year}-{month:02d}-{day:02d}"
        if iso not in hints:
            hints.append(iso)

    # Ranges first: both endpoints, so a same-month range spans instead of collapsing.
    for range_match in _DAY_RANGE_RE.finditer(text):
        if not _may_is_month(range_match.group(1), text, range_match.end(1)):
            continue
        month = _MONTHS[_MONTH_ALIASES[range_match.group(1).lower()]]
        _add(month, int(range_match.group(2)))
        _add(month, int(range_match.group(3)))

    for month_match in re.finditer(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?\b",
        text,
        re.I,
    ):
        if not _may_is_month(month_match.group(1), text, month_match.end(1)):
            continue
        _add(_MONTHS[month_match.group(1).lower()], int(month_match.group(2)))
    # Abbreviated month + day ("Mar 13", "jan 5"). "may" has no abbreviation to list, so
    # this pattern needs no guard — see `_may_is_month`, which covers the spellings above.
    for abbrev_match in re.finditer(
        r"\b(jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
        text,
        re.I,
    ):
        month = _MONTHS[_MONTH_ALIASES[abbrev_match.group(1).lower()]]
        _add(month, int(abbrev_match.group(2)))
    for iso_match in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
        iso = iso_match.group(0)
        if iso not in hints:
            hints.append(iso)
    # A bare day number without any month is ambiguous — return nothing rather
    # than guessing a month (the old behavior defaulted to March).
    # Document order, not sorted: `_iso_date_hint` (singular) hands hints[0] to the
    # meeting planner as a window start, so first-mentioned must stay first-mentioned.
    # `_explicit_time_range` sorts for itself when it needs min/max.
    return hints


def _human_date_from_iso(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso_ts)
    if not match:
        return ""
    year, month_num, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    month_names = {v: k for k, v in _MONTHS.items()}
    month_name = month_names.get(month_num, "")
    if not month_name:
        return ""
    return f"{month_name} {day}, {year}"


def _expand_calendar_week_context(
    rows: List[Dict[str, Any]], all_rows: List[Dict[str, Any]], query_text: str
) -> List[Dict[str, Any]]:
    lowered = (query_text or "").lower()
    if not rows or not any(token in lowered for token in ("week", "busy", "density", "compare", "consistent")):
        return rows
    seed_dates = {
        str(row.get("starts_at") or "")[:10]
        for row in rows
        if row.get("starts_at")
    }
    date_hints = set(_iso_date_hints(query_text))
    if any(token in lowered for token in ("compare", "versus", " vs ", "density")) and date_hints:
        seed_dates |= date_hints
    if not seed_dates:
        return rows
    expanded = list(rows)
    seen = {str(row.get("event_id") or row.get("record_id") or id(row)) for row in rows}
    for row in all_rows:
        row_key = str(row.get("event_id") or row.get("record_id") or id(row))
        if row_key in seen:
            continue
        starts = str(row.get("starts_at") or "")
        if starts[:10] in seed_dates:
            expanded.append(row)
            seen.add(row_key)
    return expanded


def _filter_calendar_rows(
    rows: List[Dict[str, Any]], query_text: str
) -> List[Dict[str, Any]]:
    """Date-aware calendar filter — handles compare/density queries with short day tokens."""
    date_hints = _iso_date_hints(query_text)
    if date_hints:
        dated = [
            row
            for row in rows
            if any(hint in str(row.get("starts_at") or "") for hint in date_hints)
        ]
        if dated:
            return _expand_calendar_week_context(dated, rows, query_text)
    filtered = _filter_rows_by_query(rows, query_text)
    if filtered:
        return _expand_calendar_week_context(filtered, rows, query_text)
    if date_hints:
        dated = [
            row
            for row in rows
            if any(hint in str(row.get("starts_at") or "") for hint in date_hints)
        ]
        if dated:
            return dated
    return filtered


def _semantic_hits(
    query_text: str,
    *,
    source_id: Optional[str] = None,
    limit: int = _SEMANTIC_HIT_LIMIT,
    time_range: Optional[Tuple[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    q = str(query_text or "").strip()
    if not q:
        return [], None
    try:
        from ..features.signal.service import get_signal_service

        result = get_signal_service().search_vectors(
            query=q,
            limit=limit,
            source_id=source_id,
            event_after=time_range[0] if time_range else None,
            event_before=time_range[1] if time_range else None,
        )
        hits: List[Dict[str, Any]] = []
        for item in result.get("items") or []:
            hits.append(
                {
                    "record_id": item.get("record_id"),
                    "text_preview": item.get("text_preview"),
                    "similarity": item.get("similarity"),
                    "source_id": item.get("source_id"),
                    "signal_dimension": item.get("signal_dimension"),
                    # Without the event time, vector items are exempt from
                    # recency decay by accident and undated at synthesis.
                    "event_at": item.get("event_at"),
                }
            )
        return hits, result.get("error")
    except Exception as exc:
        logger.debug("semantic vector search skipped: %s", exc)
        return [], str(exc)


def _strip_vector_keys(item: Dict[str, Any]) -> Dict[str, Any]:
    """Drop embedding-shaped keys before an item can enter a context packet.

    Even a null centroid_vector is a contract violation on the cross-user
    surface — the no-raw-vectors gate scans for vector-shaped keys, not just
    populated arrays.

    ``centroid_blob`` and any ``*_centroid`` are covered because entity
    mention-context centroids (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §3.6) are
    vector-shaped under names that matched none of the older patterns.
    """
    return {
        k: v
        for k, v in item.items()
        if not (
            k.endswith("_vector")
            or k.endswith("_centroid")
            or k in ("embedding", "vector", "embedding_blob", "centroid", "centroid_blob")
        )
    }


def _load_ranked_clusters(
    query_text: str,
    *,
    limit: int = _CLUSTER_LIMIT,
    primary_dimensions: Optional[List[str]] = None,
    disclosure_tier: str = "owner_raw",
) -> List[Dict[str, Any]]:
    """Ranked clusters with the black-hole policy applied to every exit.

    Wrapped rather than filtered inline for the same reason
    `_build_summary_items` is: the loader has four return paths (two early
    empties, query-ranked, size-ranked), and the packet attaches the result at
    two call sites. One choke point here is what makes a future fifth path
    impossible to leak through.
    """
    return _blackhole_policy_for_clusters(
        _load_ranked_clusters_unfiltered(
            query_text, limit=limit, primary_dimensions=primary_dimensions
        ),
        conn=_cluster_policy_connection(),
        disclosure_tier=disclosure_tier,
    )


def _cluster_policy_connection():
    try:
        from ..core.state import get_db_connection

        return get_db_connection()
    except Exception:  # noqa: BLE001
        return None


def _load_ranked_clusters_unfiltered(
    query_text: str,
    *,
    limit: int = _CLUSTER_LIMIT,
    primary_dimensions: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    try:
        from ..core.state import get_db_connection
        from ..features.signal.topic_clustering import (
            filter_clusters_by_dimensions,
            load_topic_clusters_for_query,
            rank_topic_clusters_for_query,
        )

        conn = get_db_connection()
        if conn is None:
            return []
        clusters = load_topic_clusters_for_query(conn, limit=50)
        clusters = filter_clusters_by_dimensions(clusters, primary_dimensions)
        if not clusters:
            return []
        query_vector = None
        if str(query_text or "").strip():
            from ..features.signal.topic_clustering import embed_query_text_for_ranking

            query_vector = embed_query_text_for_ranking(query_text)
            ranked_by_query = rank_topic_clusters_for_query(
                clusters,
                query_text,
                limit=limit,
                query_vector=query_vector,
            )
            return [_strip_vector_keys(c) for c in ranked_by_query]
        ranked = sorted(clusters, key=lambda c: int(c.get("member_count") or 0), reverse=True)
        return [_strip_vector_keys({**c, "relevance_score": 0.0}) for c in ranked[:limit]]
    except Exception as exc:
        logger.debug("topic cluster load skipped: %s", exc)
        return []


# Owner-only artifact classes and the manifest grant that unlocks each for
# non-owner tiers. Dense rollups (stats, dossiers) are computed unconditionally
# but packaged only where a scope explicitly asks for them.
_OWNER_ONLY_GRANTS = {
    "stat_insight": "stat_insights",
    "entity_dossier": "entity_dossiers",
    "fact": "owner_facts",
}


def _fact_disclosure_allowed(
    fact: Dict[str, Any],
    disclosure_tier: str,
    manifest: ScopeResolutionManifest,
) -> bool:
    """Owner-only facts never leave the owner tier without an explicit grant."""
    if str(fact.get("disclosure") or "") != "owner_only":
        return True
    if disclosure_tier == "owner_raw":
        return True
    grant = _OWNER_ONLY_GRANTS.get(str(fact.get("object_type") or ""), "stat_insights")
    return grant in (manifest.signal_objects or [])


def _fact_valid_at(fact: Dict[str, Any], as_of: str) -> bool:
    """Did this fact's belief-validity window cover `as_of` (ISO date)?

    String comparison over ISO timestamps — same convention as
    FactStore.facts_for_subject(as_of=...). A bare date sorts before any
    same-day timestamp; the seeded chains keep a one-day buffer, so boundary
    days never decide a case."""
    valid_from = str(fact.get("valid_from") or "")
    valid_to = fact.get("valid_to")
    if valid_from and valid_from > as_of:
        return False
    return not valid_to or str(valid_to) > as_of


def _load_fact_store_items(
    conn,
    query_text: str,
    linked_entities: List[Dict[str, Any]],
    *,
    disclosure_tier: str,
    manifest: ScopeResolutionManifest,
    temporal_shift: Optional[str] = None,
    as_of: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Atomic facts: subject-first for linked entities, then token search.

    temporal_shift='past' (the planner's before/prior/used-to signal) widens the
    read to superseded revisions — the bi-temporal store keeps closed facts and
    a past-tense question is exactly what they answer. Superseded facts are
    rendered with an explicit no-longer-current marker so they can never read
    as present-tense truth.

    as_of (the planner's "in <Month>" point-in-time anchor, B1.1/T4) switches
    both reads to point-in-time: only facts whose validity covers as_of answer,
    and they answer WITHOUT the stale marker — they were current at the asked
    instant, marking them stale would misdescribe the point-in-time truth."""
    try:
        from ..features.facts.store import FactStore
    except Exception:
        return []
    store = FactStore(conn)
    include_closed = temporal_shift == "past" or bool(as_of)
    facts: List[Dict[str, Any]] = []
    subject_linked: set = set()
    seen_ids: set = set()
    # Plural folding ("hobbies"→"hobby") so terse fact predicates meet plural
    # query nouns; used for both the search and the overlap grade below.
    search_tokens = _plural_token_variants(_query_tokens(query_text))
    try:
        # Subject-first ONLY for entities the query actually names. The old
        # behavior also dumped every self-entity fact into every query — the
        # "owner lives in San Francisco" padding on all results.
        for entity in linked_entities:
            for fact in store.facts_for_subject(
                entity["entity_id"], as_of=as_of, include_closed=include_closed
            ):
                if fact["object_id"] not in seen_ids:
                    facts.append(fact)
                    seen_ids.add(fact["object_id"])
                    subject_linked.add(fact["object_id"])
        for fact in store.search(search_tokens, include_closed=include_closed):
            if as_of and not _fact_valid_at(fact, as_of):
                continue  # point-in-time read: only facts valid AT as_of answer
            if fact["object_id"] not in seen_ids:
                facts.append(fact)
                seen_ids.add(fact["object_id"])
    except Exception as exc:
        logger.debug("fact store load skipped: %s", exc)
        return []

    tokens = set(search_tokens)
    items: List[Dict[str, Any]] = []
    for fact in facts:
        payload = fact.get("payload") or {}
        gate_item = {"object_type": "fact", "disclosure": payload.get("disclosure")}
        if not _fact_disclosure_allowed(gate_item, disclosure_tier, manifest):
            continue
        text = FactStore.render(fact)
        valid_to = fact.get("valid_to")
        if valid_to and not as_of:
            text += f" (no longer current — superseded {str(valid_to)[:10]})"
        # Overlap graded on the fact's own content — the rendered "owner …"
        # subject prefix guaranteed fake overlap on owner-phrased queries.
        content_blob = " ".join(
            [
                str(payload.get("predicate") or "").replace("_", " "),
                str(payload.get("object_value") or ""),
            ]
        ).lower()
        overlap = sum(1 for t in tokens if t in content_blob)
        if overlap == 0 and fact["object_id"] not in subject_linked and tokens:
            continue
        items.append(
            {
                "topic": text[:120],
                "summary_text": text,
                "record_id": fact["object_id"],
                "predicate": payload.get("predicate"),
                "retrieval_source": "fact",
                "relevance_score": round(min(1.0, 0.6 + 0.1 * overlap), 4),
                "_overlap": overlap,
            }
        )
    items.sort(key=lambda i: i.pop("_overlap"), reverse=True)
    return items[:10]


_ISO_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _stat_artifact_date(fact: Dict[str, Any], created_at: Any) -> str:
    """The artifact's OWN date (ISO YYYY-MM-DD): payload period_end, then
    period_start, then the row's created_at. Staleness honesty (B1.5/T8): a
    served stat must carry when its numbers are from, not just the value."""
    for candidate in (fact.get("period_end"), fact.get("period_start"), created_at):
        date_iso = str(candidate or "")[:10]
        if _ISO_DATE_PREFIX_RE.match(date_iso):
            return date_iso
    return ""


def _load_stat_insight_items(
    conn,
    query_text: str,
    *,
    dimensions: Optional[List[str]] = None,
    disclosure_tier: str = "owner_raw",
    manifest: Optional[ScopeResolutionManifest] = None,
    limit: int = 8,
    first_person: bool = False,
    exposure_visible: bool = True,
) -> List[Dict[str, Any]]:
    """Aggregate-intent queries answer best from stat insights, not chunks."""
    import json as _json

    if conn is None:
        return []
    try:
        # No recency window: stats are keyed artifacts, not a stream — a
        # LIMIT-by-created_at made older stat families permanently unreachable
        # (calendar.commitment sat at rank 558/558). The safety cap is generous.
        rows = conn.execute(
            "SELECT payload_json, created_at FROM signal_facts WHERE fact_id LIKE 'stat:%' ORDER BY created_at DESC LIMIT 5000"
        ).fetchall()
    except Exception:
        return []
    tokens = set(_query_tokens(query_text))
    wanted_dims = {d.lower() for d in (dimensions or [])}
    candidates: List[Tuple[Dict[str, Any], Any]] = []
    for payload_json, created_at in rows:
        try:
            fact = _json.loads(payload_json)
        except _json.JSONDecodeError:
            continue
        if manifest is not None and not _fact_disclosure_allowed(fact, disclosure_tier, manifest):
            continue
        text = str(fact.get("tag") or fact.get("summary_text") or "").strip()
        if not text:
            continue
        blob = f"{text} {fact.get('group_key') or ''} {fact.get('record_id') or ''}".lower()
        # Prefix matching bridges morphology ("committed"→"commitment",
        # "journaling"→"journal") — stat tags are terse, exact-token overlap
        # missed them.
        overlap = sum(1 for t in tokens if (t[:5] if len(t) >= 5 else t) in blob)
        # Token evidence is required; the dimension bonus only reorders. A
        # dimension match alone must not qualify stats for a query about a
        # topic that does not exist.
        if overlap <= 0:
            continue
        candidates.append((fact, created_at))
    if not exposure_visible:
        # P1.5: exposure profile off — the exposure ledger
        # ("activity.visits.by_title") is suppressed for every stat query, not
        # just first-person ones.
        kept = _suppress_exposure_ledger_entries([fact for fact, _ in candidates])
        kept_ids = {id(fact) for fact in kept}
        candidates = [(fact, ca) for fact, ca in candidates if id(fact) in kept_ids]
    if first_person:
        # Contract 5: a first-person ask reads the authored ledger — the
        # exposure ledger and a '.sent'-shadowed volume twin must not answer
        # "how many messages have I sent" with total thread volume (IMB6).
        kept = _apply_first_person_stat_preference([fact for fact, _ in candidates])
        kept_ids = {id(fact) for fact in kept}
        candidates = [(fact, ca) for fact, ca in candidates if id(fact) in kept_ids]

    scored: List[Tuple[Tuple[float, float], Dict[str, Any]]] = []
    for fact, created_at in candidates:
        text = str(fact.get("tag") or fact.get("summary_text") or "").strip()
        blob = f"{text} {fact.get('group_key') or ''} {fact.get('record_id') or ''}".lower()
        overlap = sum(1 for t in tokens if (t[:5] if len(t) >= 5 else t) in blob)
        dim_bonus = 1.0 if str(fact.get("dimension") or "").lower() in wanted_dims else 0.0
        score = overlap + dim_bonus
        if first_person and ".sent" in str(fact.get("record_id") or ""):
            score += 0.5  # authored-ledger preference (contract 5)
        # Tie-break equal lexical scores by the stat's own sample size: for
        # per-group count families ("Most visited: …") hundreds of groups
        # match the same query words, and the high-n groups ARE the answer.
        try:
            n_weight = float((fact.get("stat_summary") or {}).get("n") or 0.0)
        except (TypeError, ValueError):
            n_weight = 0.0
        # Staleness honesty (B1.5/T8): render the artifact's own date next to
        # the value. Substring needles over tags stay intact (suffix-only).
        date_iso = _stat_artifact_date(fact, created_at)
        summary_text = f"{text} (as of {date_iso})" if date_iso else text
        scored.append(
            (
                (score, n_weight),
                {
                    "topic": text[:120],
                    "summary_text": summary_text,
                    "record_id": fact.get("fact_id"),
                    "dimension": fact.get("dimension"),
                    "retrieval_source": "stat_insight",
                    "relevance_score": round(min(1.0, 0.5 + 0.15 * score), 4),
                },
            )
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


_RECENT_WINDOW_DAYS = 14
_RECENT_ITEM_LIMIT = 10


def _default_conn():
    try:
        from ..core.state import get_db_connection

        return get_db_connection()
    except Exception:
        return None


def _load_recent_summary_items(
    conn,
    *,
    source_ids: Optional[List[str]] = None,
    days: int = _RECENT_WINDOW_DAYS,
    limit: int = _RECENT_ITEM_LIMIT,
) -> List[Dict[str, Any]]:
    """Freshest records as an ordered fusion contributor.

    Guarantees the last two weeks are always *representable* in the summary
    regardless of semantic similarity — recency is a first-class relevance
    signal, not a tiebreaker.
    """
    if conn is None:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    params: List[Any] = [cutoff]
    source_sql = ""
    ids = [str(s) for s in (source_ids or []) if str(s).strip()]
    if ids:
        source_sql = f" AND source_id IN ({','.join('?' for _ in ids)})"
        params.extend(ids)
    params.append(max(1, limit))
    try:
        rows = conn.execute(
            f"""
            SELECT record_id, source_id, signal_dimension, text_preview, event_at
            FROM signal_embeddings
            WHERE chunk_index = 0 AND event_at IS NOT NULL AND event_at >= ?{source_sql}
            ORDER BY event_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as exc:
        logger.debug("recent summary items skipped: %s", exc)
        return []
    items: List[Dict[str, Any]] = []
    for record_id, source_id, dimension, preview, event_at in rows:
        text = str(preview or "").strip()
        if not text:
            continue
        items.append(
            {
                "topic": text[:120],
                "summary_text": text,
                "record_id": record_id,
                "source_id": source_id,
                "signal_dimension": dimension,
                "event_at": event_at,
                "retrieval_source": "recent",
            }
        )
    return items


def _fusion_item_key(item: Dict[str, Any]) -> str:
    record_id = str(item.get("record_id") or "")
    retrieval = str(item.get("retrieval_source") or "")
    # contact_identifiers alias contact_id as record_id (same as the contacts
    # row). Collapsing them under one fusion key lets a token-heavier email/phone
    # summary replace the display-name row — C15 "contact John" then misses
    # "John Ludlow". Keep identifier rows distinct from the contact + each other.
    if record_id and retrieval.startswith("canonical:contact_identifiers"):
        ident = str(item.get("summary_text") or item.get("topic") or "")[:80]
        return f"rec:{record_id}|ident:{ident}"
    # user_goals.record_id is the source ai_chat / message id. Collapsing the
    # extracted goal with the full chat row under rec:{id} lets the shorter
    # goal_text win (goals lane is fused first) and drops needle fragments that
    # only live in the message — C26 "goals extraction personal" then misses
    # "coverage, and pursue edtech". Keep each goal distinct from the message
    # and from sibling goals on the same source record.
    if record_id and retrieval == "user_goal":
        goal_id = str(item.get("goal_id") or item.get("summary_text") or "")[:80]
        return f"rec:{record_id}|goal:{goal_id}"
    if record_id:
        return f"rec:{record_id}"
    cluster_id = str(item.get("cluster_id") or "")
    if cluster_id:
        return f"cluster:{cluster_id}"
    return f"txt:{retrieval}:{str(item.get('topic') or '')[:80]}"


# Contributors whose items describe *current state* rather than events in
# time: facts carry their own validity intervals, stats fold their own
# windows, briefs and dossiers are maintained snapshots. Decaying these by
# created_at would punish exactly the artifacts built to stay current.
_NO_DECAY_FUSION_SOURCES = frozenset(
    {"stat_insights", "facts_store", "entities", "briefs", "goals"}
)


def _recency_decay_factor(
    item: Dict[str, Any],
    *,
    now: datetime,
    half_life_days: float,
    floor: float,
) -> float:
    ts = _parse_row_timestamp(item)
    if ts is None:
        return 1.0
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return max(floor, 0.5 ** (age_days / half_life_days))


def _rrf_fuse_summary_lists(
    lists: List[Tuple[str, float, List[Dict[str, Any]]]],
    *,
    k: int = 60,
    cap: int = _SUMMARY_ITEM_CAP,
    now: Optional[datetime] = None,
    context_sources: frozenset = frozenset(),
    rare_tokens: Optional[List[str]] = None,
    min_per_source: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Fuse ordered contributor lists with weighted reciprocal rank fusion.

    Each entry is (source_name, weight, ordered_items). Scores from different
    contributors are never compared directly — only ranks are, which is the
    whole point: cosine similarities, keyword overlaps, and fixed brief scores
    live on incomparable scales.

    Time-stamped event items (vector hits, canonical rows) additionally decay
    by 2^(-age/half_life) toward a floor, so "relevant" drifts with the
    present instead of treating a message from last year and last night as
    interchangeable. Current-state contributors are exempt (see
    _NO_DECAY_FUSION_SOURCES).

    Abstention: `context_sources` name contributors that only add color around
    real findings (briefs, recent window, dimension filler) — they can never
    justify a non-empty result by themselves. And when the query carried
    `rare_tokens` (a specific ask), at least one evidence item must actually
    contain one of them, or the honest answer is nothing.
    """
    from ..features.signal.vector_settings import (
        fusion_recency_enabled,
        fusion_recency_floor,
        fusion_recency_half_life_days,
    )

    evidence_items = [
        item for source_name, _, ordered in lists
        if source_name not in context_sources
        for item in ordered
    ]
    if not evidence_items:
        return []
    if rare_tokens:
        rare_dfs: Dict[str, int] = (
            dict(rare_tokens) if isinstance(rare_tokens, dict)
            else {t: 1 for t in rare_tokens}
        )
        blobs = [_item_text_blob(item) for item in evidence_items]

        def _evidenced(token: str) -> bool:
            # Variant-aware: items saying 'journal' evidence a 'journaling' ask.
            return any(v in blob for v in _token_variants(token) for blob in blobs)

        # Every effectively-absent token (df ≤ 2: zero, or a porter-stem
        # collision like 'falconer'→'falcon' df 1) must be evidenced by the
        # returned items themselves, or the ask is about something that does
        # not exist. Answer-shape vocabulary ('cadence', 'frequency') is
        # excluded upstream by the token stoplist — it describes the aggregate
        # wanted, not row content.
        if any(df <= 2 and not _evidenced(t) for t, df in rare_dfs.items()):
            return []
        # A query with SEVERAL rare tokens is a specific ask even when stem
        # collisions keep each df nonzero ('years as a competitive falconer':
        # falconer→'falcon' df 1, competitive df 26): if NONE of them is
        # evidenced, nothing retrieved is about the ask. A single weakly-rare
        # token ('journaling' df 5, 'cadence' df 1) never vetoes alone — the
        # derived layers may answer it without containing the word.
        if len(rare_dfs) >= 2 and not any(_evidenced(t) for t in rare_dfs):
            return []

    decay_on = fusion_recency_enabled()
    half_life = fusion_recency_half_life_days()
    floor = fusion_recency_floor()
    now = now or datetime.now(timezone.utc)

    scores: Dict[str, float] = {}
    best_item: Dict[str, Dict[str, Any]] = {}
    contributors: Dict[str, List[str]] = {}
    for source_name, weight, ordered in lists:
        apply_decay = decay_on and source_name not in _NO_DECAY_FUSION_SOURCES
        for rank, item in enumerate(ordered):
            key = _fusion_item_key(item)
            decay = (
                _recency_decay_factor(item, now=now, half_life_days=half_life, floor=floor)
                if apply_decay
                else 1.0
            )
            scores[key] = scores.get(key, 0.0) + weight * decay / (k + rank + 1)
            contributors.setdefault(key, []).append(source_name)
            if key not in best_item:
                best_item[key] = dict(item)
                if apply_decay and decay < 1.0:
                    best_item[key]["recency_factor"] = round(decay, 4)

    if not scores:
        return []
    max_score = max(scores.values()) or 1.0
    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    selected = [key for key, _ in ranked[:cap]]

    # Per-lane diversity floor: weighted RRF buries a lane whose evidence is
    # genuinely relevant but appears in ONLY that lane (authored goals for "what
    # have I been working on") under high-volume overlapping lanes (vector +
    # recent). For the named lanes, guarantee a minimum representation by
    # promoting their top-scored items over the lowest-scored non-pinned items.
    # Gated by the caller (only goal-intent queries pin "goals"), so ordinary
    # queries fuse exactly as before.
    if min_per_source:
        pinned = set(min_per_source)
        selected_set = set(selected)
        for lane, minimum in min_per_source.items():
            have = sum(1 for key in selected if lane in contributors.get(key, []))
            if have >= minimum:
                continue
            candidates = [
                key for key, _ in ranked
                if lane in contributors.get(key, []) and key not in selected_set
            ]
            evictable = [
                key for key in reversed(selected)
                if not (set(contributors.get(key, [])) & pinned)
            ]
            for promote in candidates[: minimum - have]:
                if not evictable:
                    break
                drop = evictable.pop(0)
                selected_set.discard(drop)
                selected_set.add(promote)
        selected = [key for key, _ in ranked if key in selected_set][:cap]

    fused: List[Dict[str, Any]] = []
    for key in selected:
        item = best_item[key]
        item["relevance_score"] = round(scores[key] / max_score, 4)
        item["fusion_sources"] = sorted(set(contributors.get(key) or []))
        fused.append(item)
    return fused


def _blackhole_policy_for_summary(
    items: List[Dict[str, Any]],
    *,
    conn: Optional[Any],
    disclosure_tier: str,
) -> List[Dict[str, Any]]:
    """Apply the entity black hole to assembled summary items.

    This is the grantee-facing pipeline, so the rule splits on who is asking:

    * **Not `owner_raw`** — a grantee. Items mentioning a protected entity are
      dropped outright. `resolve_disclosure_tier` never elevates a grantee to
      `owner_raw`, so this tier check is a sound proxy for "not the owner".
    * **`owner_raw`** — the owner or something running as them (a routine).
      Items are kept, because the owner is entitled to them, but each protected
      item is stamped `blackhole_protected`. That stamp is the taint feed the
      control plane needs: it cannot detect protected content itself, and
      without it Gate C on the BYOK route can never fire.

    Items carry names as prose with no entity id, so this is a name scan — the
    same floor described in `blackhole_llm`, not a ceiling.
    """
    if conn is None or not items:
        return items
    try:
        from ..features.lifecycle.blackhole import (
            blackholed_name_terms,
            normalize_entity_name,
        )
    except Exception:  # noqa: BLE001
        return items
    try:
        terms = blackholed_name_terms(conn)
    except Exception:  # noqa: BLE001
        # A store that cannot answer must not silently serve protected content
        # to a grantee; the owner's own path is unaffected.
        if str(disclosure_tier or "") == "owner_raw":
            return items
        raise
    if not terms:
        return items

    owner_view = str(disclosure_tier or "") == "owner_raw"
    kept: List[Dict[str, Any]] = []
    for item in items:
        blob = normalize_entity_name(_item_text_blob(item))
        hit = bool(blob) and any(term in blob for term in terms)
        if not hit:
            kept.append(item)
            continue
        if owner_view:
            kept.append({**item, "blackhole_protected": True})
    return kept


_CLUSTER_TEXT_KEYS = ("label", "centroid_preview", "term_label")


def _cluster_text_blob(cluster: Dict[str, Any]) -> str:
    """Every string a cluster hands a caller.

    A cluster row carries no entity id — the label IS the name — so the scan
    has to cover the prose the labeler wrote (`label`, `centroid_preview`), the
    deterministic label kept beside it, and `label_terms`, which are lifted
    verbatim from member text and would otherwise carry the name past a filter
    that only read the label.
    """
    parts = [str(cluster.get(key) or "") for key in _CLUSTER_TEXT_KEYS]
    metadata = cluster.get("metadata")
    if isinstance(metadata, dict):
        parts.append(str(metadata.get("term_label") or ""))
    parts.extend(str(term) for term in (cluster.get("label_terms") or []))
    parts.extend(str(alias) for alias in (cluster.get("query_aliases") or []))
    return " ".join(parts).lower()


def _blackhole_policy_for_clusters(
    clusters: List[Dict[str, Any]],
    *,
    conn: Optional[Any],
    disclosure_tier: str,
) -> List[Dict[str, Any]]:
    """Apply the entity black hole to topic clusters in the retrieval packet.

    Same split as `_blackhole_policy_for_summary`, applied to a surface the
    summary policy never saw: `packet["topic_clusters"]` is assembled from
    `load_topic_clusters_for_query` and attached directly, so a cluster named
    after a protected entity reached every grantee and MCP caller no matter
    what the summary items did.

    The labeler refuses to mint a protected name and the rebuild withdraws the
    ones written before protection, so on a healthy node this filter has
    nothing to catch. It is here because those are producer-side guarantees:
    they hold for labels this build wrote, not for a row that predates the
    rebuild, arrives by restore, or is written by an older node against the
    same database.
    """
    if conn is None or not clusters:
        return clusters
    try:
        from ..features.lifecycle.blackhole import (
            blackholed_name_terms,
            normalize_entity_name,
        )
    except Exception:  # noqa: BLE001
        return clusters
    try:
        terms = blackholed_name_terms(conn)
    except Exception:  # noqa: BLE001
        # Same fail-closed rule as the summary policy: a store that cannot
        # answer must not serve protected content to a grantee.
        if str(disclosure_tier or "") == "owner_raw":
            return clusters
        raise
    if not terms:
        return clusters

    owner_view = str(disclosure_tier or "") == "owner_raw"
    kept: List[Dict[str, Any]] = []
    for cluster in clusters:
        blob = normalize_entity_name(_cluster_text_blob(cluster))
        hit = bool(blob) and any(term in blob for term in terms)
        if not hit:
            kept.append(cluster)
            continue
        if owner_view:
            kept.append({**cluster, "blackhole_protected": True})
    return kept


def _build_summary_items(
    *,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    query_text: str,
    semantic_hits: List[Dict[str, Any]],
    ranked_clusters: List[Dict[str, Any]],
    installed_source_ids: Optional[List[str]] = None,
    disclosure_tier: str = "owner_raw",
    plan=None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Assemble summary items, then apply the black-hole policy to every exit.

    The policy is applied here rather than inside the builder because the
    builder has two return paths (fused and unfused); wrapping is the only way
    to be sure a future third path cannot bypass it.
    """
    items = _build_summary_items_unfiltered(
        manifest=manifest,
        adapters=adapters,
        query_text=query_text,
        semantic_hits=semantic_hits,
        ranked_clusters=ranked_clusters,
        installed_source_ids=installed_source_ids,
        disclosure_tier=disclosure_tier,
        plan=plan,
        now=now,
    )
    return _blackhole_policy_for_summary(
        items,
        conn=getattr(adapters.signal, "_conn", None),
        disclosure_tier=disclosure_tier,
    )


def _build_summary_items_unfiltered(
    *,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    query_text: str,
    semantic_hits: List[Dict[str, Any]],
    ranked_clusters: List[Dict[str, Any]],
    installed_source_ids: Optional[List[str]] = None,
    disclosure_tier: str = "owner_raw",
    plan=None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    from ..features.signal.vector_settings import fusion_rrf_enabled, vector_evidence_min

    hit_record_ids = {str(h.get("record_id")) for h in semantic_hits if h.get("record_id")}
    prefer_goals = "user_goals" in (manifest.signal_objects or [])
    work_scope = manifest.scope_id == "work_context:read"
    ai_scope = manifest.scope_id == "ai_conversations:read"
    source_ids = _resolve_source_ids(manifest, installed_source_ids)

    first_person = bool(getattr(plan, "first_person_intent", False)) if plan else False
    belief_intent = bool(getattr(plan, "first_person_belief", False)) if plan else False
    interaction_browse = bool(getattr(plan, "interaction_browse", False)) if plan else False
    owner_cache: Dict[str, Optional[bool]] = {}

    # The query's own connection — goal/brief contributors must read the same
    # database the query targets (the global singleton may point elsewhere in
    # multi-db verification runs and seeded evals).
    bundle_conn = getattr(adapters.signal, "_conn", None)

    # P1.5: the owner's exposure-profile visibility toggle (per-node
    # engine_config, default ON). When OFF, exposure-ledger stats
    # ("activity.visits.by_title") and exposure-only interest items are
    # suppressed for EVERY query (the owner opted out of the surface), not just
    # labeled. Read against the query's own connection so verification runs hit
    # the right node.
    try:
        from ..config.settings import exposure_profile_visible as _exposure_visible

        exposure_visible = _exposure_visible(bundle_conn)
    except Exception as exc:  # noqa: BLE001 — a read failure must not break retrieval
        logger.debug("exposure-profile visibility read skipped: %s", exc)
        exposure_visible = True

    # Specific-ask detection: the discriminative (rare-in-corpus) content tokens
    # of the query. If the query carries these and nothing matches them, the
    # honest result is empty — see the rare gate in _rrf_fuse_summary_lists.
    query_tokens = _query_tokens(query_text)
    residual_tokens = _residual_content_tokens(query_tokens)
    rare_query_tokens = _rare_tokens(bundle_conn, residual_tokens)
    if first_person and rare_query_tokens:
        # The head-nouns of a first-person ask ("hobbies", "opinion", "people
        # I talk to") are answer-shape, not row content — they must not veto
        # an answer the derived layers hold without containing the word.
        rare_query_tokens = {
            t: df
            for t, df in rare_query_tokens.items()
            if t not in _FIRST_PERSON_SHAPE_TOKENS
        }

    goal_items: List[Dict[str, Any]] = []
    if prefer_goals or work_scope:
        # Goals are owner-authored artifacts; the scope's authorization list
        # (default_source_ids) is the right boundary, NOT the runtime-install
        # subset. A bundled source can hold authored goals (chatgpt_file_
        # ingestion) while only a sibling variant (chatgpt_ui_conversation) has
        # an install row — install-gating would then hide the owner's own goals.
        goal_source_ids = [
            str(s).strip() for s in (manifest.default_source_ids or []) if str(s).strip()
        ] or (source_ids or None)
        goal_items = _load_user_goal_summaries(
            query_text,
            source_ids=goal_source_ids or None,
            conn=bundle_conn,
            time_range=getattr(plan, "time_range", None) if plan else None,
        )

    canonical_items = _load_canonical_summary_items(
        manifest=manifest,
        adapters=adapters,
        query_text=query_text,
        source_ids=source_ids,
        disclosure_tier=disclosure_tier,
        rare_query_tokens=rare_query_tokens,
        plan=plan,
        conn=bundle_conn,
        exposure_visible=exposure_visible,
    )
    canonical_items = _prefer_time_window(
        canonical_items, getattr(plan, "time_range", None) if plan else None
    )

    # Interaction browse ("who do I talk to"): contacts + co-participation
    # edges. Contacts alone greened IMB7 as a workaround (mention-only names
    # have no contact row). P3.2 adds communicates_with neighbors of self so
    # talked-to answers survive without the contacts lane and still exclude
    # mention-only third parties (Odile).
    interaction_items: List[Dict[str, Any]] = []
    if first_person and interaction_browse:
        seen_names: set[str] = set()
        try:
            contact_rows = _list_canonical_rows(
                adapters, "contacts", source_ids=source_ids, limit=30,
                disclosure_tier=disclosure_tier,
            )
        except Exception as exc:
            logger.debug("interaction contact browse skipped: %s", exc)
            contact_rows = []
        for row in contact_rows:
            if row.get("is_self") in (1, True, "1"):
                continue
            name = str(row.get("display_name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            interaction_items.append(
                {
                    "topic": name,
                    "summary_text": f"Contact: {name}",
                    "record_id": row.get("record_id"),
                    "source_id": row.get("source_id"),
                    "relevance_score": 0.75,
                    "retrieval_source": "canonical:contacts",
                }
            )
        if bundle_conn is not None:
            try:
                from ..features.entities.edges import EDGE_COMMUNICATES, top_edges

                self_row = bundle_conn.execute(
                    "SELECT entity_id FROM entities WHERE is_self=1 LIMIT 1"
                ).fetchone()
                if self_row:
                    for edge in top_edges(
                        bundle_conn,
                        str(self_row[0]),
                        edge_type=EDGE_COMMUNICATES,
                        limit=20,
                    ):
                        name = str(edge.get("entity_name") or "").strip()
                        if not name:
                            continue
                        key = name.lower()
                        if key in seen_names:
                            continue
                        seen_names.add(key)
                        interaction_items.append(
                            {
                                "topic": name,
                                "summary_text": f"Talked with: {name}",
                                "record_id": edge.get("entity_id"),
                                "relevance_score": min(
                                    0.85, 0.55 + float(edge.get("weight") or 0.0) * 0.05
                                ),
                                "retrieval_source": "entity_edge:communicates_with",
                            }
                        )
            except Exception as exc:
                logger.debug("interaction edge browse skipped: %s", exc)
        interaction_items = interaction_items[:15]

    brief_dims = list(manifest.primary_dimensions)
    if manifest.scope_id == "activity:read":
        brief_dims.append("Profile")
    brief_items = _load_brief_summary_items(brief_dims, conn=bundle_conn)

    # D1.8: role-filtered message_emotions for mood/emotion asks. Declared on
    # messages:read signal_objects; also answers health:read mood questions
    # (journals alone cannot probe the emotions table — see PRV-E1 note).
    emotion_items: List[Dict[str, Any]] = []
    mood_ask = _mood_emotion_intent(query_text)
    emotions_in_scope = (
        "message_emotions" in (manifest.signal_objects or [])
        or manifest.scope_id in ("messages:read", "health:read")
    )
    if mood_ask and emotions_in_scope and bundle_conn is not None:
        emotion_items = _load_emotion_summary_items(bundle_conn)

    # Legacy work-scope employer heuristic (scheduled for deletion once the
    # query planner covers it); contributes ordered items, not fake scores.
    if manifest.scope_id == "work_context:read" and query_text:
        lowered = query_text.lower()
        if any(token in lowered for token in ("employer", "company", "prior", "before", "previous")):
            for row in _list_canonical_rows(adapters, "profile_records", source_ids=source_ids, limit=50):
                if str(row.get("record_type") or "").lower() != "experience":
                    continue
                text = _row_summary_text("profile_records", row, scope_id=manifest.scope_id)
                if not text:
                    continue
                canonical_items.insert(
                    0,
                    {
                        "topic": text[:120],
                        "summary_text": text,
                        "record_id": row.get("record_id"),
                        "relevance_score": 0.94,
                        "retrieval_source": "canonical:profile_records",
                    },
                )

    # Zero-scored clusters are unranked filler, not findings.
    cluster_items = [
        {
            "topic": cluster.get("label"),
            "summary_text": cluster.get("label"),
            "dimension": cluster.get("dimension"),
            "cluster_id": cluster.get("cluster_id"),
            "member_count": cluster.get("member_count"),
            "relevance_score": float(cluster.get("relevance_score") or 0.0),
            "retrieval_source": "cluster",
        }
        for cluster in ranked_clusters
        if float(cluster.get("relevance_score") or 0.0) > 0.0
    ]

    # Vector hits split by strength: a strong hit (cosine ≥ evidence floor) or a
    # lexical match on the query's content tokens is evidence; a weak FTS-OR or
    # low-cosine hit only rides along when real evidence exists.
    evidence_floor = vector_evidence_min()
    vector_items: List[Dict[str, Any]] = []
    vector_context_items: List[Dict[str, Any]] = []
    for hit in semantic_hits:
        preview_lower = str(hit.get("text_preview") or "").lower()
        similarity = float(hit.get("similarity") or 0.0)
        item = {
            "topic": hit.get("text_preview"),
            "summary_text": hit.get("text_preview"),
            "record_id": hit.get("record_id"),
            "source_id": hit.get("source_id"),
            "signal_dimension": hit.get("signal_dimension"),
            "event_at": hit.get("event_at"),
            "relevance_score": round(similarity, 4),
            "retrieval_source": "vector",
        }
        lexical = any(t in preview_lower for t in residual_tokens)
        if similarity >= evidence_floor or lexical:
            vector_items.append(item)
        else:
            vector_context_items.append(item)

    if first_person and bundle_conn is not None:
        # Owner-authored preference in the vector lane (P3.3): belief/identity
        # asks drop other people's message-backed hits (their words must not
        # become the owner's beliefs); the broader flag re-ranks owner rows
        # first. Non-message-backed hits (journal, activity, …) are exempt.
        def _vec_owner(item: Dict[str, Any]) -> Optional[bool]:
            return _record_owner_authored(
                bundle_conn, str(item.get("record_id") or ""), owner_cache
            )

        if belief_intent:
            # Drop other people's words (author) AND the owner's descriptions of
            # someone else (subject) — both misattribute a belief/interest.
            def _vec_belief_ok(i: Dict[str, Any]) -> bool:
                if _vec_owner(i) is False:
                    return False
                blob = f"{i.get('topic') or ''} {i.get('summary_text') or ''}"
                return not _belief_about_other(blob)

            vector_items = [i for i in vector_items if _vec_belief_ok(i)]
            vector_context_items = [i for i in vector_context_items if _vec_belief_ok(i)]
        vector_items.sort(key=lambda i: _owner_rank(_vec_owner(i)))
        vector_context_items.sort(key=lambda i: _owner_rank(_vec_owner(i)))

    # Minimal-disclosure gate: owner-only facts (e.g. stat_insight aggregates —
    # work rhythms, spend patterns, contact cadence) are dense fingerprints of
    # the person. They are computed unconditionally but only *packaged* for the
    # owner tier, unless the scope manifest explicitly grants "stat_insights".
    fact_items: List[Dict[str, Any]] = []
    for dim in manifest.primary_dimensions:
        dim_key = dim.lower()
        page = adapters.signal.get_by_dimension(dim_key, limit=50, offset=0)
        for fact in page.items:
            if not _fact_disclosure_allowed(fact, disclosure_tier, manifest):
                continue
            label = fact.get("goal_text") or fact.get("summary_text") or fact.get("topic")
            if not label and not fact.get("dimension"):
                continue
            record_id = str(fact.get("record_id") or fact.get("fact_id") or "")
            if hit_record_ids and record_id and record_id not in hit_record_ids and not fact.get("goal_text"):
                continue
            if fact.get("goal_text"):
                # Same rule as the goals contributor: a goal rides on token
                # overlap or explicit goal intent — never as filler. Undated
                # goal texts dodge recency decay and were outranking on-topic
                # evidence on niche queries (C8's top-5).
                goal_lower = str(fact.get("goal_text")).lower()
                tokens_q = _query_tokens(query_text)
                goal_intent = any(term in (query_text or "").lower() for term in _EXTRA_SURFACE_TERMS)
                if tokens_q and not goal_intent and not any(t in goal_lower for t in tokens_q):
                    continue
            score = (
                _goal_relevance(str(fact.get("goal_text")), query_text)
                if fact.get("goal_text")
                else (0.35 if hit_record_ids else 0.1)
            )
            fact_items.append(
                {
                    **{k: v for k, v in fact.items() if k != "content"},
                    "topic": label,
                    "summary_text": label,
                    "relevance_score": round(score, 4),
                    "retrieval_source": "signal_fact",
                }
            )
    # Same time-window preference as the goal lane: dimension facts now carry
    # created_at (and sometimes event_at) — a "yesterday" ask keeps in-window
    # facts when any exist, annotated fallback otherwise.
    fact_items = _prefer_time_window(
        fact_items, getattr(plan, "time_range", None) if plan else None
    )
    fact_items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
    if first_person:
        # The dimension-dump lane carries raw stat payloads too — the same
        # authored-ledger preference must hold or the exposure/volume twin
        # leaks around the stat lane's selection (IMB6).
        fact_items = _apply_first_person_stat_preference(fact_items)
    if not exposure_visible:
        # P1.5: exposure profile off — drop exposure-ledger stats from the
        # dimension-dump lane too (intent-independent), never just from the
        # first-person path.
        fact_items = _suppress_exposure_ledger_entries(fact_items)

    # Entity spine: link query entities, contribute dossier/mention items.
    # Entity/fact/stat contributors need a raw sqlite handle; reuse the bundle
    # connection resolved above for the goal/brief contributors.
    raw_conn = bundle_conn

    entity_items: List[Dict[str, Any]] = []
    fact_store_items: List[Dict[str, Any]] = []
    if query_text:
        try:
            from ..core.state import get_db_connection
            from ..features.entities.linking import entity_context_items, link_query_entities

            conn = raw_conn if raw_conn is not None else get_db_connection()
            if conn is not None:
                linked = link_query_entities(conn, query_text)
                temporal_shift = getattr(plan, "temporal_shift", None) if plan else None
                try:
                    # T7 pass-through (B2.2): past-tense asks widen the edge
                    # read to closed revisions ("no longer current" marker).
                    raw_entity_items = entity_context_items(
                        conn, linked, temporal_shift=temporal_shift
                    )
                except TypeError:
                    # Pre-M1 linking without the kwarg — current-edges only.
                    raw_entity_items = entity_context_items(conn, linked)
                entity_items = [
                    item
                    for item in raw_entity_items
                    if _fact_disclosure_allowed(item, disclosure_tier, manifest)
                ]
                fact_store_items = _load_fact_store_items(
                    conn, query_text, linked, disclosure_tier=disclosure_tier,
                    manifest=manifest,
                    temporal_shift=temporal_shift,
                    as_of=getattr(plan, "as_of", None) if plan else None,
                )
        except Exception as exc:
            logger.debug("entity linking skipped: %s", exc)

    # The statistics layer is a first-class surface, not an intent special
    # case: frequency questions ("what cities…", "which moods…") often carry
    # no aggregate keyword yet answer best from stat insights, and the loader
    # self-qualifies on token overlap (the rare gate protects negatives). The
    # planner's aggregate flag remains a dimension hint only.
    stat_items: List[Dict[str, Any]] = []
    if query_text:
        try:
            from ..core.state import get_db_connection

            stat_items = _load_stat_insight_items(
                raw_conn if raw_conn is not None else get_db_connection(),
                query_text,
                dimensions=getattr(plan, "dimensions", None) if plan else None,
                disclosure_tier=disclosure_tier,
                manifest=manifest,
                first_person=first_person,
                exposure_visible=exposure_visible,
            )
        except Exception as exc:
            logger.debug("stat insight load skipped: %s", exc)

    if fusion_rrf_enabled():
        recent_items = _load_recent_summary_items(
            raw_conn if raw_conn is not None else _default_conn(),
            source_ids=source_ids or None,
        )
        if belief_intent and bundle_conn is not None:
            # Recency filler must not smuggle other people's message rows into
            # a belief/identity answer either.
            recent_items = [
                i
                for i in recent_items
                if _record_owner_authored(
                    bundle_conn, str(i.get("record_id") or ""), owner_cache
                )
                is not False
            ]
        recency_intent = any(t in (query_text or "").lower() for t in _RECENCY_TERMS)
        # ai_conversations:read's primary evidence is the chat row itself; lift it
        # like work_context so goal/stat lanes (also on that manifest) cannot bury
        # known-item needles when the ask literally contains "goal".
        canonical_weight = 2.0 if (work_scope or ai_scope) else 1.0
        vector_weight = 0.6 if work_scope else 1.0
        # Interest/identity asks ("what are my interests/values/beliefs") want the
        # lanes that SUMMARIZE the owner — dimension briefs, topic clusters, facts —
        # to outrank raw recent chatter: a fresh logistics message ("Williamsburg!
        # Brooklyn") is recency, not identity signal. Down-weight recency + lift the
        # summarizing lanes, but ONLY for a belief/identity ask with no explicit
        # recency intent ("lately" keeps recency strong; general recall is untouched).
        identity_ask = belief_intent and not recency_intent
        recent_weight = 0.4 if identity_ask else 1.0
        brief_weight = 1.6 if identity_ask else 0.8
        cluster_weight = 1.2 if identity_ask else 0.8
        goal_intent = any(
            t in (query_text or "").lower() for t in _EXTRA_SURFACE_TERMS
        )
        # Work / goal-intent asks need authored goals above ambient lanes;
        # modest lift (floor still pins ≥2) for dense "working on" quality.
        goals_weight = 1.4 if goal_items and (work_scope or goal_intent) else 1.0
        emotions_weight = 1.8 if emotion_items and mood_ask else 1.0
        # Cap goals on work "working on" asks so a dense authored-goals corpus
        # cannot monopolize the summary cap (D3: need ≥3 retrieval_sources).
        # Floor below still guarantees ≥2 user_goal items.
        goals_for_fuse = goal_items
        if work_scope and goal_intent and len(goal_items) > _WORK_GOAL_FUSION_CAP:
            goals_for_fuse = goal_items[:_WORK_GOAL_FUSION_CAP]
        # Diversity floors: goals stay pinned on goal-intent; ai_conversations
        # also pins the chat lane so extracted user_goals / Work-dimension stats
        # cannot occupy every slot when the ask is for conversations (C26).
        min_per: Optional[Dict[str, int]] = None
        if goal_items and goal_intent:
            min_per = {"goals": 2}
        if (
            work_scope
            and goal_intent
            and recency_intent
            and recent_items
        ):
            # "lately" / "recently" work asks: keep a recency spine beside goals.
            min_per = dict(min_per or {})
            min_per["recent"] = max(int(min_per.get("recent") or 0), 2)
        if emotion_items and mood_ask:
            min_per = dict(min_per or {})
            min_per["emotions"] = max(int(min_per.get("emotions") or 0), 1)
        if ai_scope and canonical_items:
            min_per = dict(min_per or {})
            min_per["canonical"] = max(int(min_per.get("canonical") or 0), 3)
        # NOTE: cosine similarity must NOT waive the zero-df gate — a strong hit
        # on the generic half of a query ("compiler rewrite") cannot evidence a
        # name the corpus does not contain ("Threnody-7"). The N-lane found
        # exactly this leak when a strong-vector waiver existed here.
        return _rrf_fuse_summary_lists(
            [
                ("stat_insights", 2.0, stat_items),
                ("facts_store", 1.5, fact_store_items),
                ("entities", 1.5, entity_items),
                ("goals", goals_weight, goals_for_fuse),
                ("emotions", emotions_weight, emotion_items),
                ("canonical", canonical_weight, canonical_items),
                ("contacts", 1.2, interaction_items),
                ("briefs", brief_weight, brief_items),
                ("clusters", cluster_weight, cluster_items),
                ("vector", vector_weight, vector_items),
                ("vector_context", vector_weight * 0.8, vector_context_items),
                ("signal_facts", 1.0, fact_items),
                ("recent", recent_weight, recent_items),
            ],
            context_sources=frozenset(
                {"briefs", "signal_facts", "vector_context"}
                | (set() if recency_intent else {"recent"})
            ),
            rare_tokens=rare_query_tokens,
            now=now,
            min_per_source=min_per,
        )

    # Legacy path (TOPOS_FUSION_RRF=off): incomparable absolute scores.
    for item in entity_items + fact_store_items + stat_items:
        item.setdefault("relevance_score", 0.9)
    items = (
        stat_items
        + fact_store_items
        + entity_items
        + goal_items
        + emotion_items
        + canonical_items
        + interaction_items
        + brief_items
        + cluster_items
        + vector_items
        + vector_context_items
        + fact_items
    )
    if work_scope:
        for item in items:
            if str(item.get("retrieval_source") or "").startswith("canonical:profile_records"):
                item["relevance_score"] = max(float(item.get("relevance_score") or 0.0), 0.96)
            if str(item.get("retrieval_source") or "") == "vector":
                item["relevance_score"] = round(
                    float(item.get("relevance_score") or 0.0) * _VECTOR_WORK_SCOPE_DAMPEN, 4
                )
    items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
    return items[:_SUMMARY_ITEM_CAP]


def _count_non_self_persons(db_conn) -> Optional[int]:
    """Thin A8 stub: distinct non-self person count, never names. None if unavailable."""
    if db_conn is None:
        return None
    try:
        row = db_conn.execute(
            "SELECT COUNT(*) FROM entities "
            "WHERE lower(entity_type)='person' AND COALESCE(is_self, 0)=0"
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return None


def _build_cohort_aggregate_summary(
    *,
    person_count: Optional[int],
    scope_id: str,
    cohort_labels: Optional[List[str]] = None,
    peer_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Non-entity-specific aggregate text — no person names, no per-entity rows."""
    labels = [str(x).strip().lower() for x in (cohort_labels or []) if str(x).strip()]
    label_hint = ", ".join(labels[:3]) if labels else "granted cohort"
    if person_count is None and peer_count is None:
        body = (
            "Cohort aggregate (non-entity-specific): messaging and contact activity "
            f"can be summarized across the {label_hint} without naming individuals. "
            "Individual people are not listed in this rollup."
        )
    else:
        parts = []
        if person_count is not None:
            parts.append(f"about {person_count} people in the granted cohort membership")
        if peer_count is not None and peer_count != person_count:
            parts.append(f"about {peer_count} active message peers")
        detail = "; ".join(parts) if parts else "cohort activity"
        body = (
            f"Cohort aggregate (non-entity-specific): {detail} "
            f"({label_hint}). Individual people are not disclosed in this rollup."
        )
    return {
        "summary_text": body,
        "topic": "cohort_aggregate",
        "retrieval_source": "cohort_aggregate",
        "scope_id": scope_id,
        "relevance_score": 1.0,
        # Explicit: this rollup must never carry entity selectors.
        "entity_ids": [],
        "aggregate_only": True,
    }


class DefaultSignalRetrievalAdapter:
    """Retrieve minimum necessary data per access mode and manifest."""

    def __init__(self, adapters: AdapterBundle) -> None:
        self._adapters = adapters
        self._last_stores: List[str] = []
        self.retrieve_call_count = 0

    def reset_retrieve_call_count(self) -> None:
        self.retrieve_call_count = 0

    def stores_touched(self) -> List[str]:
        return list(self._last_stores)

    def _cohort_aggregate_bundle(
        self,
        request: RetrievalRequest,
        packet: Dict[str, Any],
        retrieval_meta: Dict[str, Any],
    ) -> RetrievalBundle:
        """A8/C1: mode-appropriate non-entity-specific aggregate (no named-person data)."""
        from ..core.state import get_db_connection
        from .cohort_resolvers import resolve_accessible_entity_cohorts

        try:
            db_conn = get_db_connection()
        except Exception:
            db_conn = None
        cohorts = list(getattr(request.manifest, "accessible_entity_cohorts", None) or [])
        # Prefer resolved membership size (C1) over whole-graph person count.
        membership = resolve_accessible_entity_cohorts(cohorts, db_conn) if db_conn else []
        person_count: Optional[int]
        if membership:
            person_count = len(membership)
        else:
            person_count = _count_non_self_persons(db_conn)
        peer_count: Optional[int] = None
        if db_conn is not None and any(
            str(c).strip().lower() == "message_peers" for c in cohorts
        ):
            peers = resolve_accessible_entity_cohorts(["message_peers"], db_conn)
            peer_count = len(peers) if peers else None
        summary = _build_cohort_aggregate_summary(
            person_count=person_count,
            scope_id=str(getattr(request.manifest, "scope_id", "") or ""),
            cohort_labels=cohorts,
            peer_count=peer_count,
        )
        mode = request.access_mode
        if mode == "raw":
            # Raw still must not expose entity rows — same aggregate fact as a row-shaped shell.
            packet["rows"] = [
                {
                    "record_id": "cohort_aggregate",
                    "summary_text": summary["summary_text"],
                    "retrieval_source": "cohort_aggregate",
                    "aggregate_only": True,
                }
            ]
        elif mode == "inference":
            packet["scores"] = [
                {
                    "label": "cohort_aggregate",
                    "score": 1.0,
                    "summary_text": summary["summary_text"],
                    "retrieval_source": "cohort_aggregate",
                    "aggregate_only": True,
                }
            ]
        else:
            packet["answer_type"] = "summary"
            packet["summaries"] = [summary]
        retrieval_meta["retrieval_strategy"] = "cohort_aggregate"
        retrieval_meta["aggregate_only"] = True
        if person_count is not None:
            retrieval_meta["cohort_person_count"] = person_count
        if peer_count is not None:
            retrieval_meta["cohort_peer_count"] = peer_count
        self._last_stores = ["entities"] if person_count is not None else []
        return RetrievalBundle(
            context_packet=packet,
            stores_touched=list(self._last_stores),
            record_counts={"cohort_aggregate": 1},
            retrieval_metadata=retrieval_meta,
        )

    def retrieve(self, request: RetrievalRequest) -> RetrievalBundle:
        self.retrieve_call_count += 1
        manifest: ScopeResolutionManifest = request.manifest
        query_text = str(request.query_text or "").strip()
        if request.skip_retrieval:
            self._last_stores = []
            return RetrievalBundle(context_packet={}, stores_touched=[], record_counts={})

        if not _mode_allowed(request.access_mode, manifest.access_mode_ceiling):
            raise RetrievalError("mode_ceiling_exceeded", f"{request.access_mode} exceeds ceiling {manifest.access_mode_ceiling}")

        touched: List[str] = []
        counts: Dict[str, int] = {}
        retrieval_meta: Dict[str, Any] = {
            "retrieval_strategy": "dimension_dump",
            "disclosure_tier": request.disclosure_tier,
        }
        packet: Dict[str, Any] = {"scope_id": manifest.scope_id, "access_mode": request.access_mode}

        # Selector-aware suppression (plan A2): the query names a third-party entity this
        # grantee may not select. Produce an empty, mode-appropriate result WITHOUT touching
        # the entity's data — access-advantage=0 (PermLLM) — and shaped identically to a query
        # about a nonexistent entity (CQE indistinguishability: same keys, empty). Mode
        # ceiling is still enforced above so an over-broad access_mode still denies first.
        if request.suppress_selectors:
            mode = request.access_mode
            if mode == "raw":
                packet["rows"] = []
            elif mode == "inference":
                packet["scores"] = []
            else:
                packet["answer_type"] = "summary"
                packet["summaries"] = []
            retrieval_meta["retrieval_strategy"] = "selector_suppressed"
            self._last_stores = []
            return RetrievalBundle(
                context_packet=packet, stores_touched=[], record_counts={},
                retrieval_metadata=retrieval_meta,
            )

        # A2.3 / A8 refuse-vs-aggregate: aggregate-only ask under active selector / cohort
        # grant → non-entity-specific rollup. No named-person rows; no full retrieve.
        # C1 membership resolvers widen named allow-list separately; this path stays nameless.
        if request.cohort_aggregate:
            return self._cohort_aggregate_bundle(request, packet, retrieval_meta)

        source_filter = manifest.default_source_id
        source_ids = _resolve_source_ids(manifest, request.installed_source_ids)

        # One structured parse ahead of retrieval (entities/time/aggregate).
        # The reference instant is threaded, not read from the wall clock here:
        # request.now / TOPOS_QUERY_NOW (eval injection) → None → wall clock
        # inside the planner. Month arithmetic (as-of, "last week") must be
        # reproducible under a pinned now.
        plan = None
        plan_now = _resolve_plan_now(request)
        if query_text:
            try:
                from ..core.state import get_db_connection
                from .planner import build_query_plan, query_planner_enabled

                if query_planner_enabled():
                    plan = build_query_plan(get_db_connection(), query_text, now=plan_now)
                    retrieval_meta["query_plan"] = plan.to_meta()
            except Exception as exc:
                logger.debug("query planner skipped: %s", exc)

        time_range = plan.time_range if plan else None
        semantic_query = query_text
        if plan and plan.semantic_residual and len(plan.semantic_residual) >= 6:
            semantic_query = plan.semantic_residual

        # The vector/cluster services read the GLOBAL db connection. When this
        # query's adapter bundle targets a different database (seeded eval
        # corpora, multi-db verification), those layers would silently serve
        # another database's content — the cross-db leak class of bce067a.
        global_layers_apply = _bundle_is_global_db(self._adapters)

        semantic_hits: List[Dict[str, Any]] = []
        vector_error: Optional[str] = None
        if query_text and global_layers_apply and request.access_mode in ("summary", "inference"):
            semantic_hits, vector_error = _semantic_hits(
                semantic_query, source_id=source_filter, time_range=time_range
            )
            if not semantic_hits and time_range:
                # Time scope can starve results (sparse corpora); retry unscoped.
                semantic_hits, vector_error = _semantic_hits(semantic_query, source_id=source_filter)
            if not semantic_hits and source_ids:
                for sid in source_ids:
                    if sid == source_filter:
                        continue
                    semantic_hits, vector_error = _semantic_hits(semantic_query, source_id=sid)
                    if semantic_hits:
                        break
            if semantic_hits:
                touched.append("vector")
                retrieval_meta["retrieval_strategy"] = "query_aware"
            elif vector_error:
                logger.debug("vector search unavailable: %s", vector_error)

        ranked_clusters: List[Dict[str, Any]] = []
        if global_layers_apply and request.access_mode in ("summary", "inference"):
            ranked_clusters = _load_ranked_clusters(
                query_text,
                primary_dimensions=manifest.primary_dimensions,
                disclosure_tier=request.disclosure_tier,
            )
            if ranked_clusters:
                touched.append("topic_clusters")
                if query_text:
                    retrieval_meta["retrieval_strategy"] = "query_aware"
                retrieval_meta["clusters_returned"] = [
                    {
                        "cluster_id": cluster.get("cluster_id"),
                        "relevance_score": cluster.get("relevance_score"),
                        "primary_dimension": cluster.get("primary_dimension") or cluster.get("dimension"),
                    }
                    for cluster in ranked_clusters
                ]
                retrieval_meta["cluster_rank_strategy"] = ranked_clusters[0].get(
                    "cluster_rank_strategy", "term_entity"
                )
                if manifest.primary_dimensions:
                    retrieval_meta["facet_filter_applied"] = list(manifest.primary_dimensions)

        mode = request.access_mode
        if mode == "raw":
            rows: List[Dict[str, Any]] = []
            raw_rare_tokens: List[str] = []
            if query_text:
                try:
                    raw_conn_for_df = getattr(self._adapters.signal, "_conn", None)
                    raw_rare_tokens = _rare_tokens(
                        raw_conn_for_df, _residual_content_tokens(_query_tokens(query_text))
                    )
                except Exception:
                    raw_rare_tokens = []
            for table in manifest.canonical_tables:
                table_rows = _route_canonical_rows(
                    self._adapters,
                    table,
                    manifest=manifest,
                    query_text=query_text,
                    source_ids=source_ids,
                    limit=100,
                    disclosure_tier=request.disclosure_tier,
                    rare_query_tokens=raw_rare_tokens,
                )
                table_rows = [_redact_row_for_scope(manifest.scope_id, table, row) for row in table_rows]
                touched.append("canonical")
                if table == "profile_records" and "certification" in (query_text or "").lower():
                    typed = [
                        row
                        for row in table_rows
                        if str(row.get("record_type") or "").lower() == "certification"
                    ]
                    if typed:
                        table_rows = typed
                if (
                    table == "profile_records"
                    and manifest.scope_id == "work_context:read"
                    and query_text
                    and any(w in query_text.lower() for w in ("prior", "before", "previous", "employer"))
                ):
                    typed = [
                        row
                        for row in table_rows
                        if str(row.get("record_type") or "").lower() == "experience"
                    ]
                    if "topos" in query_text.lower() and "before" in query_text.lower():
                        typed = [
                            row
                            for row in typed
                            if str(row.get("organization") or "").strip().lower() != "topos"
                        ]
                    if typed:
                        table_rows = typed
                # Query-token filtering (incl. calendar date awareness) happened
                # in _route_canonical_rows — SQL-side, over the full table, with
                # no unfiltered-fallback page.
                if query_text and table_rows:
                    retrieval_meta["retrieval_strategy"] = "raw_query_filter"
                table_rows = _apply_filter_manifest_rows(table_rows, request.filter_manifest)
                max_rows = int((request.filter_manifest or {}).get("max_rows") or 0)
                if max_rows > 0:
                    table_rows = table_rows[:max_rows]
                counts[table] = len(table_rows)
                for row in table_rows:
                    rows.append({"_table": table, **row})
            packet["rows"] = _strip_forbidden(rows, manifest.must_not_retrieve)
        elif mode == "summary":
            # Query-aware building only applies to actual queries. Ranked
            # clusters load even for browse requests (no query_text), and
            # rerouting those through the query builder starved the dimension
            # summaries lane entirely (p3 regression: summaries=[] and the
            # signal store never touched on browse-mode summary reads).
            if query_text:
                summaries = _build_summary_items(
                    manifest=manifest,
                    adapters=self._adapters,
                    query_text=query_text,
                    semantic_hits=semantic_hits,
                    ranked_clusters=ranked_clusters,
                    installed_source_ids=request.installed_source_ids,
                    disclosure_tier=request.disclosure_tier,
                    plan=plan,
                    now=plan_now,
                )
                if summaries:
                    touched.append("signal")
                # Surface the planner's parsed window so synthesis can state
                # which dates were searched ("nothing from 2026-07-16") instead
                # of inferring it from per-item timestamps. Summary mode only:
                # its lanes honor the window; raw/inference do not yet
                # (PLAN_TEMPORAL_COHERENCE.md M5/M6) and must not claim to.
                if plan is not None and (plan.time_range or plan.as_of):
                    window: Dict[str, Any] = {"source": "query_planner"}
                    if plan.time_range:
                        window["from"], window["to"] = plan.time_range
                    if plan.as_of:
                        window["as_of"] = plan.as_of
                    packet["time_window"] = window
            else:
                summaries = []
                for dim in manifest.primary_dimensions:
                    dim_key = dim.lower()
                    page = self._adapters.signal.get_by_dimension(dim_key, limit=50, offset=0)
                    touched.append("signal")
                    for item in page.items:
                        if not _fact_disclosure_allowed(item, request.disclosure_tier, manifest):
                            continue
                        if item.get("summary_text") or item.get("topic") or item.get("dimension"):
                            summaries.append({k: v for k, v in item.items() if k != "content"})
            if manifest.scope_id == "attention:read":
                attention_items = _load_attention_summary_items(
                    getattr(self._adapters.signal, "_conn", None))
                if attention_items:
                    summaries = attention_items + list(summaries)
                    touched.append("signal")
            if manifest.scope_id == "availability:read":
                time_items = _load_time_summary_items(
                    getattr(self._adapters.signal, "_conn", None), query_text)
                if time_items:
                    summaries = time_items + list(summaries)
                    touched.append("signal")
            if manifest.scope_id == "complexity:read":
                complexity_items = _load_complexity_summary_items(
                    getattr(self._adapters.signal, "_conn", None))
                if complexity_items:
                    summaries = complexity_items + list(summaries)
                    touched.append("signal")
            packet["summaries"] = summaries
            counts["summaries"] = len(summaries)
            # Abstention is a complete answer: when a query found nothing, do not
            # dress the empty result with semantic/cluster/graph furniture that
            # reads as confident content about a topic that does not exist.
            abstained = bool(query_text) and not summaries
            if semantic_hits and not abstained:
                packet["semantic_hits"] = semantic_hits
            if ranked_clusters and not abstained:
                packet["topic_clusters"] = ranked_clusters
            # D1.8: do not attach legacy graph_nodes/graph_edges furniture.
            # Product graph answers use entity_edges; dual-graph store is GC-deprecated.
        elif mode == "inference":
            scores: List[Dict[str, Any]] = []
            inference_rare: List[str] = []
            if query_text:
                try:
                    inference_rare = _rare_tokens(
                        getattr(self._adapters.signal, "_conn", None),
                        _residual_content_tokens(_query_tokens(query_text)),
                    )
                except Exception:
                    inference_rare = []
            try:
                from ..config.settings import exposure_profile_visible as _exposure_visible

                _exp_visible = _exposure_visible(getattr(self._adapters.signal, "_conn", None))
            except Exception:
                _exp_visible = True
            # B8: pass plan so first_person / belief filters and owner_authored +
            # speaker_label metadata land on inference scores (summary path already
            # threaded plan; inference previously dropped it → attribution-blind).
            canon_items = _load_canonical_summary_items(
                manifest=manifest,
                adapters=self._adapters,
                query_text=query_text,
                source_ids=source_ids,
                disclosure_tier=request.disclosure_tier,
                rare_query_tokens=inference_rare,
                browse_fallback=True,
                plan=plan,
                conn=getattr(self._adapters.signal, "_conn", None),
                exposure_visible=_exp_visible,
            )
            for item in canon_items:
                scores.append({k: v for k, v in item.items() if k not in _INFERENCE_CANONICAL_EXCLUDED_KEYS})
            if manifest.scope_id == "activity:read":
                for item in _load_brief_summary_items(["Profile"]):
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            elif manifest.scope_id == "health:read":
                for item in _load_brief_summary_items(["Wellbeing"]):
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            elif manifest.scope_id == "schedule:read":
                for item in _load_brief_summary_items(["Time"]):
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            elif manifest.scope_id == "availability:read":
                # Minimal disclosure: inference answers with ONE band, not the
                # summary bundle. The band is computed here (store access) and
                # rendered by the game layer; no time items enter the packet.
                band = _availability_band(
                    getattr(self._adapters.signal, "_conn", None), query_text
                )
                if band:
                    packet["availability_band"] = band
            elif manifest.scope_id == "attention:read":
                for item in _load_attention_summary_items(
                        getattr(self._adapters.signal, "_conn", None)):
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            for dim in manifest.primary_dimensions:
                page = self._adapters.signal.get_by_dimension(dim.lower(), limit=50, offset=0)
                touched.append("signal")
                for item in page.items:
                    if not _fact_disclosure_allowed(item, request.disclosure_tier, manifest):
                        continue
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            if ranked_clusters:
                packet["topic_clusters"] = ranked_clusters
                counts["topic_clusters"] = len(ranked_clusters)
            if semantic_hits:
                # Inference exposes only the similarity/id signal from semantic hits, never
                # the raw chunk preview text.
                packet["semantic_hits"] = [
                    {k: v for k, v in hit.items() if k not in _INFERENCE_SEMANTIC_EXCLUDED_KEYS}
                    for hit in semantic_hits
                ]
                counts["semantic_hits"] = len(semantic_hits)
            # D1.8: legacy graph_nodes/graph_edges furniture removed (GC-deprecated).
            meta = self._adapters.vector.list_metadata(limit=20, offset=0)
            if meta.total:
                touched.append("vector")
            packet["scores"] = _strip_forbidden(scores, manifest.must_not_retrieve)
            counts["scores"] = len(scores)
            packet = _strip_forbidden(packet, manifest.must_not_retrieve)

        retrieval_meta["vector_hits"] = len(semantic_hits)
        retrieval_meta["clusters_returned"] = len(ranked_clusters)

        self._last_stores = sorted(set(touched))
        return RetrievalBundle(
            context_packet=packet,
            stores_touched=self._last_stores,
            record_counts=counts,
            retrieval_metadata=retrieval_meta,
        )


# Protocol alias for imports
SignalRetrievalAdapter = DefaultSignalRetrievalAdapter
