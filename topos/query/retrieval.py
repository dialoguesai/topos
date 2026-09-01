"""Mode-aware signal retrieval (PRD §8.5–8.7)."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ..storage.adapters.factory import AdapterBundle
from . import narrowing as _N
from .entity_window import (
    REASON_DERIVED,
    REFUSAL_UNRESOLVED,
    DerivedWindow,
    derive_window_from_days,
    entity_anchor_intent,
)
from .exclusion import enforce_request_exclusions as _enforce_request_exclusions
from .exclusion import summarize_for_meta as _exclusion_meta
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



def _facts_lane_weight() -> float:
    """BP1 (W1.2): the facts lane's fusion weight, config-overridable so the
    sweep calibrates by data instead of a hardcoded literal. Default 1.5 is the
    shipped behavior; the 2026-08-26 sweep readout lives in
    derivation-packs/pilot/w1_report.md."""
    import os
    try:
        return float(os.environ.get("TOPOS_FACTS_LANE_WEIGHT", "1.5"))
    except ValueError:
        return 1.5


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
    # The extra terms are the goals vocabulary — goal, objective, project,
    # roadmap, working on. They name the goals surface, and stripping them is
    # right when that surface is what the ask should route to.
    #
    # They were applied to every table, and no canonical table owns them, so on
    # a table without a goals surface they could only ever delete content and
    # never authorise the browse that compensates for deleting it. "What have I
    # been working on?" against a chat corpus lost every content token, matched
    # nothing, and returned silence — with 106 rows sitting in the FTS index.
    # In a chat message "project" and "working on" are simply words.
    owns_extra_surface = tables is None or any(
        set(_EXTRA_SURFACE_TERMS) & set(_SURFACE_INTENT_TERMS.get(tbl, ()))
        for tbl in tables
    )
    if owns_extra_surface:
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


def _needle_token_groups(
    needle_text: str, needle_parts: Optional[List[str]] = None
) -> List[List[str]]:
    """Residual content tokens PER PART of the request.

    A multi-part request ("1) what did I ship 2) how did I sleep") has one needle set
    per part, not one flattened set. The flattened set is what killed the gate in
    production: `_rrf_fuse_summary_lists` vetoes when ANY token is unevidenced, so a
    specific ask in part A empties the lane part B was asking about — the gate fires on
    everything or nothing, and never on the one section it was meant to protect.

    No parts (every caller today) → exactly one group from `needle_text`, which is the
    pre-existing single-needle behaviour token-for-token.
    """
    parts = [str(p or "").strip() for p in (needle_parts or [])]
    parts = [p for p in parts if p]
    if not parts:
        parts = [needle_text] if str(needle_text or "").strip() else []
    return [_residual_content_tokens(_query_tokens(part)) for part in parts]


def _rare_token_groups(conn, groups: List[List[str]]) -> List[Dict[str, int]]:
    """`_rare_tokens` per group, over ONE df pass on the union.

    Per-part gating must not cost a df lookup per part: the frequency of a token does
    not depend on which part asked for it, so the union is looked up once and split.
    """
    union = list(dict.fromkeys(token for group in groups for token in group))
    rare = _rare_tokens(conn, union)
    return [{t: rare[t] for t in group if t in rare} for group in groups]


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


def _label_time_windows(
    items: List[Dict[str, Any]],
    windows: Optional[List[Tuple[str, str]]],
) -> int:
    """Stamp each item with WHICH of a differenced ask's two windows it fell in.

    Returns the number of items labelled. Without this, "what changed between last week
    and this week" retrieves the union span and hands synthesis one undifferentiated
    pile — the model then has to infer the split from per-item timestamps, which is the
    same inference that produced "nothing from 2026-07-16" claims about indexed data.

    The labels are the closed set `baseline` / `current` (earlier / later), never the
    owner's phrasing, so they are safe on anything that leaves the node. Items dated
    into the gap between the windows, and undated items, are left unlabelled: they
    evidence neither side.
    """
    if not windows or len(windows) < 2 or not items:
        return 0
    from .planner import WINDOW_BASELINE, WINDOW_CURRENT

    bounds = []
    for label, (raw_start, raw_end) in zip((WINDOW_BASELINE, WINDOW_CURRENT), windows):
        start, end = _parse_instant(raw_start), _parse_instant(raw_end)
        if start is None or end is None:
            return 0
        bounds.append((label, start, end))
    labelled = 0
    for item in items:
        ts = _parse_row_timestamp(item)
        if ts is None:
            continue
        for label, start, end in bounds:
            if start <= ts <= end:
                item["time_window_label"] = label
                labelled += 1
                break
    return labelled


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
    goal_intent: bool = False,
) -> List[Dict[str, Any]]:
    """Q1: `goal_intent` lets a caller assert the ask is ABOUT goals when the words do
    not say so. "What did I say I'd do last week, and did I actually do it?" contains no
    member of `_EXTRA_SURFACE_TERMS`, so on the token gate below it loaded no goals at
    all — the commitment question could not see the commitments. The flag is passed, not
    added to the surface lexicon, because the lexicon also drives routing and "said i'd"
    is not a surface.
    """
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
        goal_intent = goal_intent or any(term in query_lower for term in _EXTRA_SURFACE_TERMS)
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


def _canonical_table_absent(adapters: AdapterBundle, table: str) -> bool:
    """Has this node never created the canonical table a manifest declares?

    A canonical table is created by the writer that first lands a row in it, so on a
    first-run install a scope can declare a table that does not exist yet: a standard
    init produces 77 tables and ``conversation_messages`` is not among them. Reading it
    anyway raised ``no such table`` out of ``stores.py`` and 500'd the owner's first
    question. A declared-but-uncreated store is an EMPTY store, not a fault, and the
    empty-cause taxonomy already has the words for that.

    Deliberately narrow in both directions. ``True`` only when ``sqlite_master`` was read
    and the table was positively not in it — this is not a ``try/except`` wrapped around
    the read, so a disk error, a locked database or a malformed row still reaches the
    caller as the failure it is. ``False`` whenever existence cannot be established (a
    non-SQLite adapter, an unreadable catalog), because an unknown must not silently
    disable a lane — the mirror of the rule ``_scope_supply_state`` states for diagnoses.
    """
    conn = getattr(adapters.canonical, "_conn", None)
    if conn is None or not _SAFE_TABLE_RE.match(str(table or "")):
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 — a catalog we cannot read is not an absence
        logger.debug("canonical table existence probe skipped for %s: %s", table, exc)
        return False
    return row is None


#: Rows returned per canonical table on a raw read. Fetched as CAP + 1 so that
#: truncation is a FACT rather than an inference: getting CAP + 1 back proves more
#: rows exist, where getting exactly CAP is ambiguous — a table with exactly that
#: many rows is indistinguishable from one that was cut off.
#:
#: Measured 2026-08-25: a scheduled report stated that something had not happened,
#: when the evidence that it had was one row past the cap. The query returned
#: exactly CAP rows and the model read that as a fact about the world. Nothing in
#: the response said the result had been cut off, so no consumer — model, ledger or
#: human — could tell an absence from a truncation. That is what
#: `public_result.truncated` now says.
CANONICAL_ROW_CAP = 100


def _list_canonical_rows(
    adapters: AdapterBundle,
    table: str,
    *,
    source_ids: List[str],
    limit: int = 100,
    disclosure_tier: str = "owner_raw",
    contains: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    # A table this node has never created holds no rows — the one funnel every
    # canonical lane goes through (scope routes, the entity-thread lane, the
    # employer heuristic) so no future lane can reach around the probe.
    if _canonical_table_absent(adapters, table):
        return []
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
        # `event_at` is the column this table actually has, and it is populated
        # on every row. Reading `occurred_at`/`starts_at` — neither of which
        # exists here — meant every location item reached synthesis undated,
        # silently, because a missing key is just an empty part.
        parts = [
            str(row.get(field) or "")
            for field in ("place_name", "city", "event_type")
            if row.get(field)
        ]
        human_date = _human_date_from_iso(
            str(row.get("event_at") or row.get("occurred_at") or row.get("starts_at") or "")
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
    rare_query_token_groups: Optional[List[Dict[str, int]]] = None,
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
    # Per part of a multi-part request: a term ABSENT from the corpus means only that
    # part's specific thing isn't there. Blocking the browse for the whole table on it
    # would let one part's unanswerable ask silently empty every other part's lane —
    # the same flattening that disabled the fusion gate. Blocked only when every part
    # is blocked; a part carrying no rare tokens is never blocked, so it keeps browse
    # alive on its own.
    if rare_query_token_groups is not None:
        groups = [dict(g) for g in rare_query_token_groups]
    else:
        groups = [
            dict(rare_query_tokens)
            if isinstance(rare_query_tokens, dict)
            else {t: 0 for t in (rare_query_tokens or [])}
        ]

    def _blocks(rare_dfs: Dict[str, int]) -> bool:
        return any(df <= 2 and t in residual for t, df in rare_dfs.items())

    if groups and all(_blocks(group) for group in groups):
        return []
    work_profile = manifest.scope_id == "work_context:read" and table == "profile_records"
    if _surface_intent(table, query_lower) or work_profile or browse_fallback:
        return _list_canonical_rows(
            adapters, table, source_ids=source_ids, limit=limit,
            disclosure_tier=disclosure_tier,
        )
    return []


def _canonical_row_to_item(
    table: str,
    row: Dict[str, Any],
    *,
    manifest: ScopeResolutionManifest,
    query_text: str,
    conn: Optional[Any],
    first_person: bool,
    belief_intent: bool,
    exposure_visible: bool,
    role_cache: Dict[str, Optional[bool]],
    display_cache: Dict[str, str],
    highlight_cache: Dict[str, str],
    retrieval_source: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """One disclosed canonical row → one summary item, or ``None`` if it may not be shown.

    Extracted from :func:`_load_canonical_summary_items` so that every lane which
    turns a canonical row into an item shares ONE copy of the row-level guards —
    the belief/identity owner-authorship filter, the scope redaction, the
    exposure-profile rule for un-engaged browse rows, and the speaker labelling.
    A second lane with its own transcription of these is a privacy regression
    waiting for the two copies to drift; there is now nothing to drift.

    `retrieval_source` names the contributing lane (default `canonical:<table>`).
    It changes provenance only — never what survives.
    """
    owner: Optional[bool] = None
    if first_person and table in _MESSAGE_TABLES:
        owner = _message_row_owner(table, row, conn, role_cache)
        if belief_intent and owner is not True:
            return None  # belief/identity composition: owner-authored only
    clean = _redact_row_for_scope(manifest.scope_id, table, row)
    text = _row_summary_text(table, clean, scope_id=manifest.scope_id)
    # IMB9 / P2.1: an engaged browser row (highlight/star) carries the
    # owner's Annotate-grade span in metadata_json.highlight, which the
    # canonical list adapter never selects — pull it in so the needle
    # ("copper still method") is retrievable. page_excerpt (the page
    # author's words) is deliberately never surfaced by the lookup.
    # A place hit alone answers nothing. `location_events` is a fan-out CHILD of
    # a journal entry, and its whole document is the place name — so "who did I
    # eat with at X?" could only ever come back with the string "X". The parent
    # carries the narrative, `source_record_id` has always pointed at it, and no
    # reader had ever followed it.
    #
    # Gated on the parent's table being in the manifest, and that gate is the
    # whole design. Pulling journal prose into a LOCATION-scoped grant is the
    # exact inverse of the leak this workstream started from — a journal-only
    # grant admitting location evidence — and it would be worse, because the
    # journal is the richer surface.
    if table == "location_events" and "journal_entries" in (manifest.canonical_tables or []):
        parent_id = str(row.get("source_record_id") or "").strip()
        if parent_id:
            parent = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?", (parent_id,)
            ).fetchone()
            if parent is not None:
                parent_row = dict(parent)
                parent_clean = _redact_row_for_scope(
                    manifest.scope_id, "journal_entries", parent_row
                )
                parent_text = _row_summary_text(
                    "journal_entries", parent_clean, scope_id=manifest.scope_id
                )
                if parent_text:
                    text = f"{text} — {parent_text}".strip(" —") if text else parent_text

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
            return None
    if not text:
        return None
    if belief_intent and owner is True and table in _MESSAGE_TABLES and _belief_about_other(text):
        return None  # owner-authored but about a third party — not the owner's belief
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
        "retrieval_source": retrieval_source or f"canonical:{table}",
    }
    # The fan-out link, carried so result assembly can collapse a child against
    # its own parent. Written by ingest since the beginning and read by nothing
    # until now — the same join this workstream opened with.
    if clean.get("source_record_id"):
        item["source_record_id"] = clean.get("source_record_id")
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
    return item


def _collapse_fanout_children(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop a fan-out child when its own parent is in the same result set.

    A child and its parent describe one moment. Once a place hit carries the
    parent's narrative, a result holding both shows that narrative twice and
    spends two slots saying one thing — measured before the narrative was
    attached, a child's parent was already present in 55 of 55 sessions that
    surfaced one, so this is the common case rather than the corner.

    Collapsing is preferred to excluding children from retrieval outright: a
    child that surfaces WITHOUT its parent is the only way some asks reach the
    moment at all (the child is what carries the place name into the index), and
    it now brings the parent's text with it.

    Keyed on ``source_record_id``, the link the fan-out has always written and
    nothing had read.
    """
    parent_ids = {
        str(item.get("record_id") or "")
        for item in items
        if str(item.get("record_id") or "")
    }
    kept: List[Dict[str, Any]] = []
    for item in items:
        parent = str(item.get("source_record_id") or "").strip()
        if parent and parent != str(item.get("record_id") or "") and parent in parent_ids:
            continue
        kept.append(item)
    return kept


def _load_canonical_summary_items(
    *,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    query_text: str,
    source_ids: List[str],
    disclosure_tier: str = "owner_raw",
    rare_query_tokens: Optional[List[str]] = None,
    rare_query_token_groups: Optional[List[Dict[str, int]]] = None,
    browse_fallback: bool = False,
    plan=None,
    conn: Optional[Any] = None,
    exposure_visible: bool = True,
    ledger: Optional[Any] = None,
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
        if _canonical_table_absent(adapters, table):
            # `connected_never_delivered` of the three supply states, and the choice is
            # the point. `delivered_then_emptied` is excluded by the evidence itself:
            # the writer that creates this table has never run, so nothing was ever
            # delivered and then removed. `no_source_connected` is a claim about the
            # INSTALL SET, not about this store, and it is the more actionable of the
            # two remaining — it sends the owner to add a connector. Saying it to an
            # owner who has connected one and is simply pre-first-sync is the same
            # false-absence this taxonomy exists to end (`_scope_supply_state` refuses
            # the symmetric guess for the symmetric reason). What the absent table does
            # evidence, first-hand, is that this store has never received a delivery.
            if ledger is not None:
                ledger.empty(
                    _N.CAUSE_STORE_EMPTY,
                    stage=_N.STAGE_RETRIEVAL,
                    reason=SUPPLY_NEVER_DELIVERED,
                    # Which store, for the owner's own logs. `detail` is dropped by
                    # `as_public`, so the diagnostic does not ride the wire.
                    detail={"table": table},
                )
            continue
        rows = _route_canonical_rows(
            adapters,
            table,
            manifest=manifest,
            query_text=query_text,
            source_ids=source_ids,
            limit=50,
            disclosure_tier=disclosure_tier,
            rare_query_tokens=rare_query_tokens,
            rare_query_token_groups=rare_query_token_groups,
            browse_fallback=browse_fallback,
        )
        for row in rows:
            item = _canonical_row_to_item(
                table,
                row,
                manifest=manifest,
                query_text=query_text,
                conn=conn,
                first_person=first_person,
                belief_intent=belief_intent,
                exposure_visible=exposure_visible,
                role_cache=role_cache,
                display_cache=display_cache,
                highlight_cache=highlight_cache,
            )
            if item is not None:
                items.append(item)
    items = _collapse_fanout_children(items)
    items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
    if first_person:
        # Strong downweight, not a drop: owner-authored rows first, exempt
        # (non-message) rows keep relative order, other-authored rows last.
        items.sort(key=lambda item: _owner_rank(item.get("owner_authored")))
    return items[:_SUMMARY_ITEM_CAP]


# --- entity-keyed retrieval (the entity-thread lane) ----------------------------------
#
# Everything above routes by SCOPE and filters by TIME. A question about a PERSON or a
# RECORD ("what happened with the Anthropic thread") is answered by whether the query's
# own words happen to appear in a row — keyword luck — while the entity graph that knows
# exactly which rows belong to that subject sits one join away and unreachable from the
# query path. This lane closes that: an entity already resolved from the request
# contributes ITS records as one more candidate source beside the scope routes.
#
# It is a contributor, not an access mode. Every plane still applies, and the code below
# is arranged so that it cannot be otherwise:
#
#   * disclosure tier — rows are only ever obtained from `_list_canonical_rows`, i.e.
#     `adapters.canonical.list(..., disclosure_tier=...)`, the same disclosed path the
#     canonical lane uses. `CanonicalStore.get()` would fetch a row BY ID with no tier
#     applied at all; this lane deliberately never calls it. The mention table supplies
#     an id SET which selects from disclosed rows — it never fetches around them.
#   * scope ceiling — only `manifest.canonical_tables` are scanned, so the lane can
#     reach nothing the scope does not already authorize.
#   * row-level guards — items come from `_canonical_row_to_item`, the one shared copy.
#   * black hole / exclusions / rare gate / time window — items join the same packet at
#     the same point as every other lane and are subject to all of them; the fields the
#     exclusion filter matches on (`canonical_table`, `entity_id`, `record_id`) are set
#     explicitly so a "but not X" cannot be walked around by arriving through this lane.

#: Rows pulled per canonical table per prefilter page. The prefilter is a superset of the
#: mention join (see `_load_entity_thread_items`); this bounds work, not membership.
_ENTITY_THREAD_SCAN_LIMIT = 200
#: Distinct surfaces that build that prefilter. `Dialogues` carries 722 mention rows on
#: this node and the surfaces repeat, so the first handful is the whole vocabulary.
_ENTITY_THREAD_SURFACE_CAP = 12
#: Mention rows read per request across all resolved entities, newest first.
_ENTITY_THREAD_MENTION_LIMIT = 600
#: Items the lane may contribute. A thread is evidence beside the scope routes, never a
#: replacement for them: an entity with hundreds of linked rows must not own the packet.
_ENTITY_THREAD_CAP = 8


def _entity_thread_entities(
    conn: Optional[Any],
    linked: List[Dict[str, Any]],
    *,
    manifest: ScopeResolutionManifest,
) -> Tuple[List[str], Dict[str, int]]:
    """Which resolved entities may contribute a thread — and a tally of who may not.

    Two entities are refused, both because of artefacts this node has actually produced:

    ``is_self`` — the owner's own entity links on ordinary first-person phrasing, and its
    "thread" is not a subject, it is the corpus. Contributing it would turn every question
    that happens to name the owner into an undirected dump. On this node the self row
    carries zero mentions, so the guard costs nothing today; it is here for the day the
    resolver starts attaching them.

    An entity outside an ACTIVE selector allow-list. The pipeline already suppresses a
    grantee query that names an unauthorized *person* before retrieval runs, but that
    check is person-shaped and this lane is not: under an active policy, an org or place
    thread would otherwise pull rows the requester's own words never matched. When a
    selector policy is in force this lane contributes only for explicitly accessible
    entities, and nothing at all if the read of `is_self` fails — an unverifiable
    resolution is not a resolution.
    """
    skipped: Dict[str, int] = {}
    ids = [str(e.get("entity_id") or "").strip() for e in linked or []]
    ids = [i for i in ids if i]
    if not ids or conn is None:
        return [], skipped

    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"SELECT entity_id, COALESCE(is_self, 0) FROM entities "
            f"WHERE entity_id IN ({placeholders})",
            ids,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — fail closed: no lane rather than a blind one
        logger.debug("entity thread self-check unavailable: %s", exc)
        skipped["self_check_unavailable"] = len(ids)
        return [], skipped
    self_flags = {str(r[0]): bool(r[1]) for r in rows}

    policy_active = bool(getattr(manifest, "entity_selector_policy_active", False))
    accessible = {
        str(x).strip()
        for x in (getattr(manifest, "accessible_entity_ids", None) or [])
    }

    kept: List[str] = []
    for entity_id in ids:
        if entity_id not in self_flags:
            skipped["unresolved"] = skipped.get("unresolved", 0) + 1
            continue
        if self_flags[entity_id]:
            skipped["is_self"] = skipped.get("is_self", 0) + 1
            continue
        if policy_active and entity_id not in accessible:
            skipped["selector_not_accessible"] = skipped.get("selector_not_accessible", 0) + 1
            continue
        kept.append(entity_id)
    return kept, skipped


def _entity_thread_mentions(
    conn: Optional[Any],
    entity_ids: List[str],
    *,
    tables: List[str],
) -> Tuple[Dict[str, Set[str]], Set[str], List[str], Dict[str, str]]:
    """The mention join: which records belong to these entities, and where they live.

    Returns ``(by_table, untabled, surfaces, entity_by_record)``.

    ``canonical_table`` is NULL on a real share of this node's mention rows (619 of
    4313). Those records are not dropped — they are carried in ``untabled`` and offered
    to every scanned table, where the record-id match decides. A mention that cannot say
    which table it came from is still a mention.

    ``surfaces`` are the observed mention strings, used ONLY to build a SQL prefilter.
    They never decide membership; the record-id sets above do.
    """
    by_table: Dict[str, Set[str]] = {}
    untabled: Set[str] = set()
    surfaces: List[str] = []
    entity_by_record: Dict[str, str] = {}
    if conn is None or not entity_ids:
        return by_table, untabled, surfaces, entity_by_record

    allowed = {str(t) for t in (tables or [])}
    placeholders = ",".join("?" for _ in entity_ids)
    try:
        rows = conn.execute(
            f"""
            SELECT entity_id, record_id, canonical_table, surface_text
            FROM entity_mentions
            WHERE entity_id IN ({placeholders})
            ORDER BY COALESCE(event_at, created_at) DESC
            LIMIT ?
            """,
            [*entity_ids, _ENTITY_THREAD_MENTION_LIMIT],
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — no mention table → no thread lane
        logger.debug("entity thread mention join unavailable: %s", exc)
        return by_table, untabled, surfaces, entity_by_record

    seen_surface: Set[str] = set()
    for entity_id, record_id, canonical_table, surface_text in rows:
        rid = str(record_id or "").strip()
        if not rid:
            continue
        entity_by_record.setdefault(rid, str(entity_id))
        table = str(canonical_table or "").strip()
        if not table:
            untabled.add(rid)
        elif table in allowed:
            by_table.setdefault(table, set()).add(rid)
        surface = str(surface_text or "").strip().lower()
        if (
            surface
            and len(surface) >= 3
            and surface not in seen_surface
            and len(surfaces) < _ENTITY_THREAD_SURFACE_CAP
        ):
            seen_surface.add(surface)
            surfaces.append(surface)
    return by_table, untabled, surfaces, entity_by_record


def _blackhole_filter_thread_mentions(
    by_table: Dict[str, Set[str]],
    untabled: Set[str],
    *,
    conn: Optional[Any],
    owner_view: bool,
) -> Tuple[Dict[str, Set[str]], Set[str]]:
    """THE BLACK HOLE, AT SOURCE — wire A of two.

    Every other plane on this lane withholds CONTENT; this one withholds
    EXISTENCE, so it cannot be applied to the rows on the way out — the protected
    records must never be read at all. For a lane that reaches rows through
    ``entity_mentions``, the honest place to stop is the mention set itself, and
    that is precisely the question ``BlackholeGuard.blocked_record_ids()``
    answers, from the same join, so it is asked rather than re-derived.

    Filtering RECORDS rather than entities is deliberate and is the whole filter:
    a record linked to both a protected entity and a visible one arrives under
    the visible entity's id, so dropping protected entities from ``entity_ids``
    would sail straight past it. The record set covers that case and the plain
    one together.

    Deliberately NOT recorded in the ledger, unlike every other refusal on this
    lane. ``as_public()`` leaves the node, and a narrowing line reading "an entity
    was withheld" is itself the confirmation of existence D5 exists to deny — the
    same reason the guard returns empty rows rather than raising, and the same
    reason the exit policy's own receipt is suppressed for non-owners in
    ``_build_summary_items``.

    **Why this is not a redundant copy of the exit filter.** Wire B
    (``_blackhole_policy_for_summary``) catches the same rows on the way out, so
    severing this one leaves the packet clean and every leak assertion green.
    What it does not leave alone is the RECEIPT: with this wire cut, the lane
    loads the protected records, and then records
    ``stage=retrieval, action=contributed, reason=entity_thread_lane`` in the
    PUBLIC ledger before wire B empties the answer. A grantee handed nothing plus
    a line saying the entity's thread lane contributed has been told, in a
    closed-set slug whose meaning the protocol guarantees, that the entity exists
    and has records. That converts hiding-by-absence into hiding-by-denial. It is
    factored out here so a test can sever exactly this wire and watch the ledger
    property go red — which is the only way the two wires are distinguishable.
    """
    if owner_view or conn is None:
        return by_table, untabled
    blocked_records = _blackhole_blocked_record_ids(conn)
    if not blocked_records:
        return by_table, untabled
    filtered = {
        table: ids
        for table, ids in (
            (table, ids - blocked_records) for table, ids in by_table.items()
        )
        if ids
    }
    return filtered, untabled - blocked_records


def _load_entity_thread_items(
    *,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    conn: Optional[Any],
    linked: List[Dict[str, Any]],
    query_text: str,
    source_ids: List[str],
    disclosure_tier: str,
    first_person: bool,
    belief_intent: bool,
    exposure_visible: bool,
    plan=None,
    ledger: Optional[Any] = None,
    thread_sink: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """The entity's own records, contributed beside the scope routes.

    Two disclosed pages are read per scanned table and unioned:

    1. a ``contains`` prefilter on the entity's observed surfaces — a SQL scan of the
       WHOLE table, which is what lets an old thread record be reached at all; and
    2. a plain recency page, because the prefilter is only a superset of the join while
       the surface actually lives in a filtered column. Where it does not, page 1 is
       silently a subset — page 2 is the floor that stops that being invisible.

    Both go through `_list_canonical_rows`, so both are already disclosed to
    `disclosure_tier`. The record-id intersection then decides membership. Rows the
    entity does not own are dropped here and never enter the packet.

    `thread_sink` is the Q7 tap. It is filled from INSIDE this loop, with the row and
    the item both in hand, so a thread entry cannot exist for a record this lane did
    not itself produce: same entity resolution, same mention join, same black hole at
    source, same `_list_canonical_rows` disclosure, same `_canonical_row_to_item`
    guards. It carries ids, timestamps and closed-set labels — never row text — and
    it is only ever a CANDIDATE set: `_attach_topic_thread` intersects it with the
    packet's own surviving summaries, so fusion caps, the exit black hole and the
    request exclusions all subtract from the thread without this function knowing
    they exist.
    """
    tables = [str(t) for t in (manifest.canonical_tables or [])]
    if thread_sink is not None:
        thread_sink["message_tables"] = [t for t in tables if t in _MESSAGE_TABLES]
        thread_sink["non_message_tables"] = [t for t in tables if t not in _MESSAGE_TABLES]
    if not tables or not linked or conn is None:
        return []

    entity_ids, skipped = _entity_thread_entities(conn, linked, manifest=manifest)
    owner_view = str(disclosure_tier or "") == "owner_raw"
    if ledger is not None:
        for reason, count in sorted(skipped.items()):
            ledger.record(
                _N.STAGE_RETRIEVAL,
                "dropped_items",
                f"entity_thread_{reason}",
                dropped=count,
            )
    if not entity_ids:
        return []
    if thread_sink is not None:
        thread_sink["entities"] = list(entity_ids)

    by_table, untabled, surfaces, entity_by_record = _entity_thread_mentions(
        conn, entity_ids, tables=tables
    )
    if not by_table and not untabled:
        return []

    by_table, untabled = _blackhole_filter_thread_mentions(
        by_table, untabled, conn=conn, owner_view=owner_view
    )
    if not by_table and not untabled:
        return []

    items: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    role_cache: Dict[str, Optional[bool]] = {}
    display_cache: Dict[str, str] = {}
    highlight_cache: Dict[str, str] = {}
    sender_entity_cache: Dict[str, Tuple[Optional[str], bool]] = {}
    wanted_total = 0
    for table in tables:
        wanted = set(by_table.get(table) or set()) | untabled
        if not wanted:
            continue
        wanted_total += len(by_table.get(table) or set())
        rows: List[Dict[str, Any]] = []
        if surfaces:
            rows += _list_canonical_rows(
                adapters,
                table,
                source_ids=source_ids,
                limit=_ENTITY_THREAD_SCAN_LIMIT,
                disclosure_tier=disclosure_tier,
                contains=surfaces,
            )
        rows += _list_canonical_rows(
            adapters,
            table,
            source_ids=source_ids,
            limit=_ENTITY_THREAD_SCAN_LIMIT,
            disclosure_tier=disclosure_tier,
        )
        for row in rows:
            rid = str(row.get("record_id") or row.get("message_id") or "").strip()
            if not rid or rid not in wanted:
                continue
            key = f"{table}|{rid}"
            if key in seen:
                continue
            seen.add(key)
            item = _canonical_row_to_item(
                table,
                row,
                manifest=manifest,
                query_text=query_text,
                conn=conn,
                first_person=first_person,
                belief_intent=belief_intent,
                exposure_visible=exposure_visible,
                role_cache=role_cache,
                display_cache=display_cache,
                highlight_cache=highlight_cache,
                retrieval_source=f"entity_thread:{table}",
            )
            if item is None:
                continue
            # The keys the exclusion filter matches on. An "everything about the
            # install rewrite but not Anthropic" ask has to be able to reach a row
            # that arrived because of an entity, by the same entity.
            item["canonical_table"] = table
            entity_id = entity_by_record.get(rid)
            if entity_id:
                item["entity_id"] = entity_id
            items.append(item)
            if thread_sink is not None and table in _MESSAGE_TABLES:
                # The Q7 tap. Speaker and decision marker are read HERE, off the
                # disclosed row, because neither survives into the item: the item
                # only carries `speaker_label` on a first-person plan, and a thread
                # must be able to say who spoke on any phrasing of the question.
                thread_sink.setdefault("candidates", {})[key] = {
                    "record_id": rid,
                    "canonical_table": table,
                    "entity_id": entity_id,
                    "event_at": item.get("event_at"),
                    "speaker": _thread_speaker(
                        conn,
                        table,
                        row,
                        role_cache=role_cache,
                        display_cache=display_cache,
                        entity_cache=sender_entity_cache,
                    ),
                    "decision": _decision_marker(item.get("summary_text") or ""),
                }

    items = _prefer_time_window(items, getattr(plan, "time_range", None) if plan else None)
    items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
    kept = items[:_ENTITY_THREAD_CAP]
    if ledger is not None:
        # The lane's own coverage line: how many of the thread's records the scan
        # actually reached. `matched` short of `linked` is the honest shape of a
        # truncated scan — the alternative is a thread that quietly answers with part
        # of itself and says nothing about the rest.
        ledger.record(
            _N.STAGE_RETRIEVAL,
            "contributed",
            "entity_thread_lane",
            dropped=max(0, len(items) - len(kept)),
            detail={
                "entities": len(entity_ids),
                "linked_records": wanted_total,
                "matched": len(items),
                "contributed": len(kept),
                "tables": sorted(by_table),
            },
        )
    return kept


# --- Q3: the entity-anchored window ---------------------------------------------------
#
# The lane above answers "which rows belong to this subject". This one answers a
# question no lane could: *when*. "What did I miss while I was heads-down on the
# classifier?" names no dates, and every window this pipeline can build is parsed out
# of the words — an explicit range, a relative phrase, a differenced pair. The period
# the owner means is in their own activity, so it is read from the subject's mention
# density (`entity_window.py` holds the method and the refusals; nothing there touches
# a database).
#
# THE SURFACE THE DENSITY MAY SEE. A date is content: "you were busy with X between
# the 4th and the 11th" is a claim about the owner's life, derived from records. So
# the mentions that vote are bounded to the record surface THIS GRANT authorizes,
# proved two ways and never assumed:
#
#   * `manifest.canonical_tables` — a mention naming a table the grant does not name
#     does not vote. A mention with a NULL `canonical_table` cannot prove which table
#     it came from, so it does not vote either; the thread lane can carry those rows
#     because a disclosed row later confirms them, and there is no such confirmation
#     here.
#   * `triage_verdicts.record_id`, and only for a scope whose own content IS the
#     triage analytics (`attention_summary` in `signal_objects`). `attention:read`
#     names zero canonical tables — its content is digests computed over exactly this
#     record surface — so under the table bound alone the mode would be structurally
#     unreachable on the one scope that owns the triage it exists to window. The
#     record ids are the ones the grant's own answers already summarize; the join
#     reads their mention DATES and nothing else.
#
# A scope that authorizes neither surface derives nothing (`entity_window_no_mentions`),
# which is the fail-closed direction.
#
# Then the same planes as any other row, before the arithmetic rather than after it:
# the entity admission gate (`_entity_thread_entities`: is_self, selector allow-list,
# fail-closed on an unreadable self flag), the source bound, the black hole at source,
# and the Q5 request exclusions. Order matters — a window shaped by records the caller
# may not see is a leak whatever the packet ends up holding, because the DATES leave
# the node in `packet["time_window"]`.

#: Mention rows read for the density, newest first. Past this node's largest entity
#: (`Dialogues`, 722 rows), so the cap bounds work and not membership. Where it does
#: bite it truncates the OLD end of the span, which raises the mean daily rate and
#: therefore the hot-day threshold: truncation makes the method more reluctant to
#: name a period, never more confident about one.
_ENTITY_WINDOW_MENTION_LIMIT = 2000


def _entity_window_day_counts(
    conn: Optional[Any],
    entity_ids: List[str],
    *,
    tables: List[str],
    source_ids: List[str],
    analytics_surface: bool,
    blocked_records: Set[str],
    excluded_records: Set[str],
) -> Dict[str, int]:
    """Mentions per calendar day, over the record surface the grant authorizes.

    The two membership proofs are OR'd in SQL rather than unioned in Python so the
    ``LIMIT`` applies to ADMITTED rows: a limit applied before the bound would let an
    unreachable table crowd out the reachable ones and silently thin the density.

    Nothing is returned about what the guards removed. The black-hole drop count is
    withheld for the same reason ``_blackhole_filter_thread_mentions`` records no
    ledger line: "n mentions were withheld" is a confirmation of existence, and a
    count is as good a confirmation as a name.
    """
    if conn is None or not entity_ids:
        return {}
    allowed_tables = [str(t) for t in (tables or []) if str(t or "").strip()]
    if not allowed_tables and not analytics_surface:
        return {}

    params: List[Any] = list(entity_ids)
    entity_ph = ",".join("?" for _ in entity_ids)
    surface_clauses: List[str] = []
    if allowed_tables:
        table_ph = ",".join("?" for _ in allowed_tables)
        surface_clauses.append(f"canonical_table IN ({table_ph})")
        params.extend(allowed_tables)
    if analytics_surface:
        surface_clauses.append("record_id IN (SELECT record_id FROM triage_verdicts)")
    where = [f"entity_id IN ({entity_ph})", "(" + " OR ".join(surface_clauses) + ")"]
    if source_ids:
        # An empty `source_ids` is "the grant named no source", not "any source".
        # A non-empty one is a bound, and a mention that cannot say which source it
        # came from cannot prove it is inside that bound.
        source_ph = ",".join("?" for _ in source_ids)
        where.append(f"source_id IN ({source_ph})")
        params.extend(str(s) for s in source_ids)
    params.append(_ENTITY_WINDOW_MENTION_LIMIT)

    try:
        rows = conn.execute(
            f"""
            SELECT record_id, COALESCE(event_at, created_at)
            FROM entity_mentions
            WHERE {" AND ".join(where)}
            ORDER BY COALESCE(event_at, created_at) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — no mention table → no derived window
        logger.debug("entity window density unavailable: %s", exc)
        return {}

    counts: Dict[str, int] = {}
    for record_id, stamp in rows:
        rid = str(record_id or "").strip()
        if not rid or rid in blocked_records or rid in excluded_records:
            continue
        day = str(stamp or "")[:10]
        if len(day) != 10:
            continue
        counts[day] = counts.get(day, 0) + 1
    return counts


def _derive_entity_anchored_window(
    *,
    manifest: ScopeResolutionManifest,
    conn: Optional[Any],
    query_text: str,
    source_ids: List[str],
    disclosure_tier: str,
    ledger: Optional[Any] = None,
) -> Optional[DerivedWindow]:
    """Resolve "while I was heads-down on X" to a date range, or refuse to.

    ``None`` means *no derivation was attempted* and the turn proceeds exactly as it
    does today. A :class:`DerivedWindow` carrying a ``refusal`` means the attempt was
    made and the data would not support a period — which is an answer, and is carried
    to the caller as one.

    The subject is resolved by ``link_query_entities`` — the one resolver, not a second
    one — against the connection the density will be READ from, which is deliberately
    not the planner's. ``build_query_plan`` runs on the global db because the clock and
    the eval's pinned `now` live there, so on a bundle pointed at another database
    ``plan.entities`` holds ids from a corpus this window would then count mentions in.
    That is the cross-db class ``_bundle_is_global_db`` exists to name, and it is why
    ``_build_summary_items`` re-links the same way for the lanes.
    """
    linked: List[Dict[str, Any]] = []
    if conn is not None and query_text:
        try:
            from ..features.entities.linking import link_query_entities

            linked = list(link_query_entities(conn, query_text) or [])
        except Exception as exc:  # noqa: BLE001 — no resolver → no window
            logger.debug("entity window linking unavailable: %s", exc)
    entity_ids, _skipped = _entity_thread_entities(conn, linked, manifest=manifest)
    if not entity_ids:
        # The admission gate refused every candidate (unresolved, is_self, or outside
        # an active selector allow-list). No detail: which of the three it was is the
        # gate's business, and on the grantee path it is the answer to a question the
        # grantee is not allowed to ask.
        if ledger is not None:
            ledger.record(_N.STAGE_PLANNER, "not_applied", REFUSAL_UNRESOLVED)
        return DerivedWindow(refusal=REFUSAL_UNRESOLVED)

    excluded_records: Set[str] = set()
    if query_text:
        from .exclusion import KIND_ENTITY, _record_ids_mentioning, parse_exclusions

        spec = parse_exclusions(query_text, conn)
        if spec.requested:
            if not spec.fully_enforced or any(t.kind != KIND_ENTITY for t in spec.targets):
                # A category or tier exclusion ("nothing from work", "no private
                # rows") subtracts records this join cannot identify, and an
                # unresolved fragment subtracts records nobody can. Either way the
                # density would be computed over a corpus the owner asked to shrink,
                # and a window derived from it would be shaped by the very rows they
                # excluded. Derive nothing rather than derive it wrong.
                if ledger is not None:
                    ledger.record(_N.STAGE_PLANNER, "not_applied", "exclusion_enforced")
                return None
            for target in spec.targets:
                excluded_records |= _record_ids_mentioning(conn, target.entity_ids)

    owner_view = str(disclosure_tier or "") == "owner_raw"
    blocked_records: Set[str] = set()
    if not owner_view:
        blocked_records = _blackhole_blocked_record_ids(conn)

    day_counts = _entity_window_day_counts(
        conn,
        entity_ids,
        tables=[str(t) for t in (manifest.canonical_tables or [])],
        source_ids=[str(s) for s in (source_ids or [])],
        analytics_surface="attention_summary" in (manifest.signal_objects or []),
        blocked_records=blocked_records,
        excluded_records=excluded_records,
    )
    window = derive_window_from_days(day_counts, entity_ids=entity_ids)

    if ledger is not None:
        # The public line is three slugs; the arithmetic and the dates ride `detail`,
        # which never leaves the node. A derived range IS content — it says when the
        # owner was busy — and the caller reads it off `packet["time_window"]`, where
        # a parsed window has always been published and where it can be disputed.
        detail: Dict[str, Any] = dict(window.as_ledger_public())
        if window.resolved:
            detail["time_range"] = [window.start, window.end]
        ledger.record(
            _N.STAGE_PLANNER,
            "windowed" if window.resolved else "not_applied",
            REASON_DERIVED if window.resolved else (window.refusal or REFUSAL_UNRESOLVED),
            detail=detail,
        )
    return window


def _attention_items_in_window(
    items: List[Dict[str, Any]],
    window: Optional[DerivedWindow],
) -> Tuple[List[Dict[str, Any]], int]:
    """Keep the triage digests that fall inside a derived window.

    The triage itself is NOT reimplemented — `triage_verdicts` already holds the
    analytics and `_load_attention_summary_items` already shapes the digests computed
    from them. All that happens here is that a window selects among days that were
    going to be served anyway, which is the whole of "run the existing triage inside
    the derived window".

    A digest's day is read off its `record_id`, which is the `signal_objects` key and
    ends in the date the loader already sorts on. An item whose key carries no
    readable date is KEPT: a window that silently deleted evidence on a formatting
    accident would be indistinguishable from a quiet week.
    """
    if window is None or not window.resolved or not items:
        return items, 0
    kept: List[Dict[str, Any]] = []
    for item in items:
        tail = str(item.get("record_id") or "").rsplit(":", 1)[-1]
        dated = len(tail) == 10 and tail[4] == "-" and tail[7] == "-"
        if dated and not (window.start <= tail <= window.end):
            continue
        kept.append(item)
    return kept, len(items) - len(kept)


# --- Q7: the topic thread ------------------------------------------------------------
#
# The entity-thread lane above answers "which rows belong to this subject". It does not
# answer the question the owner actually asks about a subject: *who did I talk to about
# the classifier, and what did we decide?* Three things are missing from a ranked list
# and none of them can be recovered downstream:
#
#   * ORDER. A thread is a sequence. Ranked by relevance, the reply that settled the
#     argument can sit above the question that started it, and no consumer can put them
#     back — `event_at` is on the items, but "these items are one conversation, in this
#     order" is a claim only retrieval is in a position to make.
#   * PARTICIPANTS. "Who" is a first-class answer, not something to infer by reading
#     speaker labels off prose — which are only attached at all on a first-person plan.
#   * DECISIONS. Where the thread resolved, if it did. "A thread, no identifiable
#     decision" is a real answer and has to be sayable.
#
# THE THREAD IS A PROJECTION, NEVER A SOURCE. Every entry names a record that is already
# in `packet["summaries"]`: candidates are tapped from inside the entity-thread lane's
# own loop and then intersected with the packet at the very end of `retrieve()`, after
# the fusion cap, after the exit black hole, after the request exclusions. There is no
# code path by which the thread can carry a row the answer does not already carry, which
# is why it needs no plane of its own — it inherits all of them, and severing any one of
# them empties the thread with it.
#
# The one disclosure the thread makes that the items do not is the PARTICIPANT ROSTER: a
# person's name derived from `contacts`. That gets its own rule (`_thread_participants`)
# — named for the owner, counted for everyone else.

#: Thread entries returned. A thread is an outline of the conversation, not a transcript;
#: the rows themselves are already in `summaries` and are not duplicated here.
_TOPIC_THREAD_ITEM_CAP = 24
#: Distinct participants named. Past this the honest shape is a count.
_TOPIC_THREAD_PARTICIPANT_CAP = 12
#: Decision points reported. A "thread" with twenty decisions is a lexical false positive
#: farm, not a decision list.
_TOPIC_THREAD_DECISION_CAP = 6

#: Conservative, closed, and lexical ON PURPOSE. A decision point is a strong claim about
#: the owner's own history, and the failure that matters is a false one — telling someone
#: they decided something they did not. Every phrase here is an explicit performative of
#: settling; nothing inferential ("maybe we should", "I think we'll") is in the list, and
#: the marker emitted is a slug from a fixed set, never the matched words.
_DECISION_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("we decided", "decided"),
    ("we've decided", "decided"),
    ("we have decided", "decided"),
    ("i decided", "decided"),
    ("i've decided", "decided"),
    ("decided to", "decided"),
    ("the decision is", "decided"),
    ("we agreed", "agreed"),
    ("agreed to", "agreed"),
    ("agreed on", "agreed"),
    ("we settled on", "settled_on"),
    ("settled on", "settled_on"),
    ("let's go with", "chose"),
    ("lets go with", "chose"),
    ("we'll go with", "chose"),
    ("going with", "chose"),
    ("we chose", "chose"),
    ("we picked", "chose"),
    ("signed off", "signed_off"),
    ("locked in", "locked_in"),
    ("final call", "final"),
)
#: The slugs `_decision_marker` may return. Closed set: a decision point that leaves the
#: node carries one of these and nothing else.
DECISION_MARKERS = frozenset(marker for _, marker in _DECISION_MARKERS)

#: The word immediately before a performative can un-say it, and a substring match
#: cannot see that. "have we decided anything yet?" contains "we decided" and is the
#: OPPOSITE of a decision — it is the question that gets asked when nothing was settled.
#: Interrogative auxiliaries, conditionals and negations are the whole of the closed
#: list; apostrophes are stripped before the comparison so "haven't" lands on "havent".
_DECISION_DISQUALIFIERS = frozenset(
    {
        "before",
        "did",
        "didnt",
        "do",
        "dont",
        "had",
        "hadnt",
        "has",
        "hasnt",
        "have",
        "havent",
        "how",
        "if",
        "never",
        "not",
        "unless",
        "until",
        "what",
        "when",
        "whether",
        "why",
    }
)


def _decision_marker(text: str) -> Optional[str]:
    """Which closed-set decision marker this row's text carries, if any.

    Every occurrence is checked, not just the first: one hedged mention of a phrase
    must not suppress a real settling later in the same row.
    """
    blob = " ".join(str(text or "").lower().split())
    if not blob:
        return None
    for phrase, marker in _DECISION_MARKERS:
        start = 0
        while True:
            idx = blob.find(phrase, start)
            if idx < 0:
                break
            preceding = blob[:idx].replace("'", "").replace("\u2019", "").split()
            if not preceding or preceding[-1] not in _DECISION_DISQUALIFIERS:
                return marker
            start = idx + 1
    return None


def _sender_entity(
    conn: Optional[Any],
    sender_id: str,
    cache: Dict[str, Tuple[Optional[str], bool]],
) -> Tuple[Optional[str], bool]:
    """``sender_id`` → ``(entity_id, is_self)`` through the contact the identifier names.

    This is NOT a second resolver. The topic is resolved by `link_query_entities` and
    nothing here touches that. This is the entity plane's own `is_self` and alias
    handling applied to a SPEAKER: the graph already links a contact row to an entity
    (`entities.contact_id`), and the same join tells us whether the speaker is the
    owner — which is the difference between "you and Sam" and "you, you and Sam".
    """
    key = str(sender_id or "").strip()
    if not key:
        return None, False
    if key in cache:
        return cache[key]
    result: Tuple[Optional[str], bool] = (None, False)
    if conn is not None:
        try:
            row = conn.execute(
                """
                SELECT e.entity_id, COALESCE(e.is_self, 0)
                FROM contact_identifiers ci
                JOIN entities e ON e.contact_id = ci.contact_id
                WHERE ci.identifier = ?
                LIMIT 1
                """,
                (key,),
            ).fetchone()
            if row:
                result = (str(row[0]), bool(row[1]))
        except Exception as exc:  # noqa: BLE001 — no contact graph → an unnamed speaker
            logger.debug("thread speaker entity lookup unavailable: %s", exc)
    cache[key] = result
    return result


def _thread_speaker(
    conn: Optional[Any],
    table: str,
    row: Dict[str, Any],
    *,
    role_cache: Dict[str, Optional[bool]],
    display_cache: Dict[str, str],
    entity_cache: Dict[str, Tuple[Optional[str], bool]],
) -> Dict[str, Any]:
    """Who spoke this row: ``owner``, ``assistant``, a ``person``, or ``unknown``.

    Ownership is decided by `_message_row_owner` — the same reader the canonical lane
    uses — and then corroborated by the speaker's own entity: a `sender_id` that
    resolves to an `is_self` entity is the owner however the row's flags are shaped.
    The owner is never a named participant of their own thread.

    A model turn is typed `assistant` and is deliberately not a person. Counting one as
    a participant would answer "who did I talk to about the classifier" with a chatbot.
    """
    owner = _message_row_owner(table, row, conn, role_cache)
    if table == "ai_chat_messages":
        if owner is True:
            return {"kind": "owner"}
        label = str(row.get("sender_type") or "assistant").strip() or "assistant"
        return {"kind": "assistant", "label": label}
    sender_id = str(row.get("sender_id") or "").strip()
    entity_id, is_self = _sender_entity(conn, sender_id, entity_cache)
    if owner is True or is_self or sender_id.lower() == "self":
        return {"kind": "owner"}
    if not sender_id:
        return {"kind": "unknown"}
    # `label` IS A NAME OR IT IS EMPTY. `_sender_display` ends in
    # `name or sender_id`, which is right for owner prose (a quoted message still
    # needs an attribution when the contact is unnamed) and WRONG for a roster
    # entry, where the fallback puts a raw phone number or email address under the
    # one key the disclosure plane treats as a display name. The identifier is
    # carried separately so `_thread_participants` decides, per tier, whether the
    # reader is entitled to it.
    display = _sender_display(conn, sender_id, display_cache)
    return {
        "kind": "person",
        "label": display if display and display != sender_id else "",
        "entity_id": entity_id,
        "sender_id": sender_id,
    }


def _thread_participants(
    speakers: List[Dict[str, Any]],
    *,
    conn: Optional[Any],
    disclosure_tier: str,
    manifest: ScopeResolutionManifest,
) -> Tuple[List[Dict[str, Any]], bool, int]:
    """The participant set — ``(roster, owner_participated, withheld)``.

    Three planes decide what a roster entry may say, in this order:

    1. **The black hole.** A protected person is removed with no trace: not named, not
       counted, not ledgered. A roster of two that says "and one withheld" has confirmed
       the existence of the third, which is the single thing D5 rules out. Applied by id
       AND by name, because a speaker reaches this function through `contacts` and may
       never have been bound to an entity id at all.
    2. **The disclosure tier.** Names are the owner's. At `owner_raw` the roster is
       named; below it, a person is counted and not named — the grantee learns that the
       thread had three counterparties, not who they were. This is strictly narrower
       than `speaker_label`, which the canonical lane already attaches to prose on a
       first-person plan.
    3. **The selector policy.** Where a grant names accessible entities, those entities
       may be named below `owner_raw` too — the grant is the naming. It licenses a
       NAME and nothing else: an entity the grant names but `contacts` does not is
       still not disclosed, because the only string available for it is a raw
       identifier and an identifier is not a name.

    Two fields the roster does NOT carry below `owner_raw`, whatever the tier says
    about naming: `entity_id`, which is a stable pseudonymous join key and therefore
    owner-only on exactly the rule Q1 applies to it (`_attach_commitment_report`), and
    `sender_id`, which is the raw phone number or email itself.

    `owner_participated` is a boolean and never an entry: the owner does not need to be
    told their own name, and a roster that carries it is one field away from being a
    self-identifier in a grantee payload.
    """
    owner_view = str(disclosure_tier or "") == "owner_raw"
    policy_active = bool(getattr(manifest, "entity_selector_policy_active", False))
    accessible = {
        str(x).strip() for x in (getattr(manifest, "accessible_entity_ids", None) or [])
    }

    blocked_ids: Set[str] = set()
    blocked_terms: Set[str] = set()
    normalize = None
    try:
        from ..features.lifecycle.blackhole import (
            blackholed_entity_ids,
            blackholed_name_terms,
            normalize_entity_name,
        )

        blocked_ids = set(blackholed_entity_ids(conn) or set())
        blocked_terms = set(blackholed_name_terms(conn) or set())
        normalize = normalize_entity_name
    except Exception as exc:  # noqa: BLE001 — no black-hole store → nothing protected
        logger.debug("thread participant blackhole read skipped: %s", exc)

    owner_participated = False
    withheld = 0
    seen: Set[str] = set()
    roster: List[Dict[str, Any]] = []
    for speaker in speakers:
        kind = str(speaker.get("kind") or "unknown")
        if kind == "owner":
            owner_participated = True
            continue
        if kind == "unknown":
            continue
        if kind == "assistant":
            key = "assistant"
            if key in seen:
                continue
            seen.add(key)
            roster.append({"kind": "assistant", "label": speaker.get("label") or "assistant"})
            continue

        entity_id = str(speaker.get("entity_id") or "") or None
        label = str(speaker.get("label") or "").strip()
        identifier = str(speaker.get("sender_id") or "").strip()
        if entity_id and entity_id in blocked_ids:
            continue
        if blocked_terms and normalize is not None:
            # Both surfaces, because `label` is now a NAME or nothing: a protected
            # person with no display name would otherwise reach the roster through
            # their bare identifier with nothing for the term match to read.
            if any(
                blob and any(term in blob for term in blocked_terms)
                for blob in (normalize(label), normalize(identifier))
            ):
                continue
        key = entity_id or f"sender:{identifier}"
        if key in seen:
            continue
        seen.add(key)
        nameable = owner_view or (policy_active and entity_id and entity_id in accessible)
        # THE IDENTIFIER IS OWNER-ONLY, on the same rule as the name it stands in for.
        # An unnamed roster entry carrying `entity_id` is a stable pseudonymous JOIN
        # KEY: a grantee can count distinct counterparties, watch who recurs across
        # sessions, and — the moment the same id appears in an `accessible_entity_ids`
        # list on any other grant they hold — resolve the pseudonym and retroactively
        # de-anonymize every roster that carried it. Q1 gates the identical field the
        # same way (`_attach_commitment_report`: `if owner_view and c.get("entity_id")`);
        # this is that rule, not a second one. `sender_id` never enters the roster at
        # all — it is a raw identifier with no tier that makes it a name.
        display = label or (identifier if owner_view else "")
        entry: Dict[str, Any] = {"kind": "person"}
        if owner_view and entity_id:
            entry["entity_id"] = entity_id
        if nameable and display:
            entry["label"] = display
        else:
            withheld += 1
        roster.append(entry)

    if len(roster) > _TOPIC_THREAD_PARTICIPANT_CAP:
        roster = roster[:_TOPIC_THREAD_PARTICIPANT_CAP]
    return roster, owner_participated, withheld


def _attach_topic_thread(
    packet: Dict[str, Any],
    sink: Optional[Dict[str, Any]],
    *,
    conn: Optional[Any],
    disclosure_tier: str,
    manifest: ScopeResolutionManifest,
    ledger: Optional[Any] = None,
) -> None:
    """Assemble the thread over the rows the packet actually kept, and attach it.

    Called at the END of `retrieve()` — after the fusion cap, after
    `_blackhole_policy_for_summary`, after `_enforce_request_exclusions` — precisely so
    that every one of those subtractions has already happened to the set this reads. The
    intersection below is the whole privacy argument: a candidate that is not in
    `packet["summaries"]` is not in the thread, whatever removed it and whether or not
    this function knows the remover exists.
    """
    if not isinstance(sink, dict) or not sink:
        return
    summaries = packet.get("summaries")
    if not isinstance(summaries, list):
        return
    entities = [str(e) for e in (sink.get("entities") or [])]
    if not entities:
        # The topic never resolved to an entity. `_entity_thread_entities` has already
        # said why on the ledger (unresolved / is_self / selector); a second line here
        # would be the same refusal counted twice.
        return

    message_tables = [str(t) for t in (sink.get("message_tables") or [])]
    if not message_tables:
        if ledger is not None:
            ledger.record(
                _N.STAGE_RETRIEVAL,
                "not_applied",
                "topic_thread_not_message_scope",
                detail={"tables": [str(t) for t in (sink.get("non_message_tables") or [])]},
            )
        return

    surviving: Dict[str, Dict[str, Any]] = {}
    for item in summaries:
        rid = str(item.get("record_id") or "").strip()
        if rid and rid not in surviving:
            surviving[rid] = item

    candidates = dict(sink.get("candidates") or {})
    kept: List[Dict[str, Any]] = []
    for candidate in candidates.values():
        rid = str(candidate.get("record_id") or "")
        item = surviving.get(rid)
        if item is None:
            continue
        table = str(item.get("canonical_table") or "")
        if table and table != candidate.get("canonical_table"):
            continue
        kept.append({**candidate, "blackhole_protected": bool(item.get("blackhole_protected"))})

    if not kept:
        # ONLY reportable when there were candidates to lose. A line saying "the thread
        # emptied, dropped=0" is emitted on exactly the path where the wire-A black hole
        # removed the subject's mentions at source, and it tells a grantee — in a slug
        # whose meaning the protocol guarantees — that the entity they named resolved
        # and has a thread. That converts hiding-by-absence into hiding-by-denial, the
        # one thing D5 forbids. With no candidates the entity-thread lane has already
        # said whatever is sayable; a second line here can only add the leak.
        if ledger is not None and candidates:
            ledger.record(
                _N.STAGE_RETRIEVAL,
                "emptied",
                "topic_thread_no_message_rows",
                dropped=len(candidates),
                detail={"entities": len(entities), "tables": sorted(message_tables)},
            )
        return

    # ORDERING IS THE ANSWER. Ascending, oldest first, because a thread is read
    # forwards: the question, then the argument, then the decision. A row with no
    # timestamp cannot be placed in a sequence and is not given a false position — it is
    # excluded from the ordering and counted, so "three of the eleven rows carry no time"
    # is visible instead of silently sorting to one end.
    dated = [entry for entry in kept if str(entry.get("event_at") or "").strip()]
    undated = len(kept) - len(dated)
    dated.sort(key=lambda entry: str(entry.get("event_at")))
    ordered = dated[:_TOPIC_THREAD_ITEM_CAP]

    if not ordered:
        if ledger is not None:
            ledger.record(
                _N.STAGE_RETRIEVAL,
                "emptied",
                "topic_thread_no_message_rows",
                dropped=len(kept),
                detail={"undated": undated, "entities": len(entities)},
            )
        return

    roster, owner_participated, withheld = _thread_participants(
        [entry.get("speaker") or {} for entry in ordered],
        conn=conn,
        disclosure_tier=disclosure_tier,
        manifest=manifest,
    )

    thread_items: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    stores: Set[str] = set()
    for ordinal, entry in enumerate(ordered):
        table = str(entry.get("canonical_table") or "")
        stores.add(table)
        speaker = entry.get("speaker") or {}
        node: Dict[str, Any] = {
            "ordinal": ordinal,
            "record_id": entry.get("record_id"),
            "canonical_table": table,
            "event_at": entry.get("event_at"),
            "speaker_kind": str(speaker.get("kind") or "unknown"),
        }
        if entry.get("entity_id"):
            node["entity_id"] = entry["entity_id"]
        if entry.get("blackhole_protected"):
            node["blackhole_protected"] = True
        marker = entry.get("decision")
        if marker:
            node["decision"] = marker
            if len(decisions) < _TOPIC_THREAD_DECISION_CAP:
                decisions.append(
                    {
                        "ordinal": ordinal,
                        "record_id": entry.get("record_id"),
                        "marker": marker,
                        "event_at": entry.get("event_at"),
                    }
                )
        thread_items.append(node)

    thread: Dict[str, Any] = {
        "entity_ids": sorted(set(entities)),
        "stores": sorted(stores),
        "cross_source": len(stores) > 1,
        "items": thread_items,
        "item_count": len(thread_items),
        "undated_items": undated,
        "participants": roster,
        "participant_count": len(roster),
        "owner_participated": owner_participated,
        # An empty list is the answer "a thread, no identifiable decision" — it is the
        # shipped shape, not a missing field, so a consumer cannot read absence as
        # "not looked for".
        "decision_points": decisions,
    }
    packet["topic_thread"] = thread

    if ledger is None:
        return
    ledger.record(
        _N.STAGE_RETRIEVAL,
        "contributed",
        "topic_thread_lane",
        dropped=max(0, len(candidates) - len(ordered)),
        detail={
            "entities": len(entities),
            "candidates": len(candidates),
            "in_thread": len(thread_items),
            "undated": undated,
            "stores": sorted(stores),
            "participants": len(roster),
            "decisions": len(decisions),
        },
    )
    if len(stores) < 2:
        # THE HONEST SHAPE OF A ONE-STORE THREAD. `messages:read` names
        # `conversation_messages` and `ai_conversations:read` names `ai_chat_messages`;
        # no scope in the registry names both, so on today's grants this line fires on
        # every thread. That is the finding, not a defect in the assembly: the thread
        # spans exactly the stores the grant reaches, and it says which.
        ledger.record(
            _N.STAGE_RETRIEVAL,
            "scoped",
            "topic_thread_single_store",
            detail={
                "store": sorted(stores)[0] if stores else None,
                "scope_message_tables": sorted(message_tables),
            },
        )
    if not decisions:
        ledger.record(
            _N.STAGE_RETRIEVAL,
            "scoped",
            "topic_thread_no_decision",
            detail={"scanned": len(thread_items)},
        )
    if withheld:
        # Counted, not named. Safe to ledger — unlike the black hole, the existence of a
        # counterparty is not itself the protected fact here; the grantee already knows
        # the thread has participants because `participant_count` says so.
        ledger.record(
            _N.STAGE_DISCLOSURE,
            "dropped_items",
            "topic_thread_participants_withheld",
            dropped=withheld,
        )


# --- Q1: commitment tracking ---------------------------------------------------------
#
# "What did I say I'd do last week, and did I actually do it?"
#
# The engine answered that with two lists that have nothing to do with each other: goals
# out of `user_goals`, journal and message rows out of the scope routes, no join between
# them. Synthesis was then left to decide which rows were about which goal, and the only
# tool it has for that is wording — so it matched a goal to a row that sounded like it
# and reported progress nobody could check. That is the worst failure in the catalog. An
# empty answer costs the owner a question; a CONFIDENT WRONG one costs them the belief
# that they did something they did not.
#
# THE JOIN IS PER GOAL, NOT ONE BLENDED QUERY. Each stated goal is an ITEM: it has its own
# `record_id` (the message the owner stated it in) and, through `entity_mentions` on that
# record, its own entity ids. Evidence is retrieved against THOSE ids, so an evidence row
# is attached to a goal because the entity graph links them, never because the words
# rhyme. Every evidence entry names the record it rests on, which is what makes a progress
# claim checkable: the owner can open the row.
#
# "NO EVIDENCE FOUND" IS THE FEATURE, NOT THE FALLBACK. A goal with nothing behind it is
# reported with a `no_evidence` status and an `empty_cause` drawn from the same five-cause
# taxonomy the request-level ledger uses, so the answer says WHICH kind of nothing:
#
#   not_queried  — no evidence lane ran for this goal at all. Its statement resolved no
#                  entity (`commitment_goal_unresolved`), it carries no timestamp to
#                  order evidence against (`commitment_goal_undated`), or the grant names
#                  no store to look in (`commitment_scope_no_evidence_store`). This is
#                  emphatically NOT "we looked and found nothing", and the two must never
#                  collapse — one is a gap in the graph, the other is a fact about the
#                  owner's week.
#   store_empty  — the stores that would hold the evidence hold nothing at all, with the
#                  supply-state sub-cause (`no_source_connected` and friends) saying which
#                  kind of empty. "Connect a calendar", not "you did nothing".
#   no_match     — the lane ran against real stores and this goal's ids reached no row.
#                  The honest "you said this and I can find nothing".
#   scope_denied — candidates existed and a plane removed them. A goal whose evidence lane
#                  was VETOED is not a goal with no evidence, and this is the label that
#                  keeps them apart. Owner-only; see `_attach_commitment_report`.
#
# THE REPORT IS A PROJECTION OF `packet["summaries"]`, exactly as Q7's thread is. The lane
# below contributes its evidence rows to the packet like any other lane, and the report is
# assembled at the end of `retrieve()` by INTERSECTING the per-goal candidate sets with
# the summaries that survived the fusion cap, the black-hole exit policy and the request
# exclusions. A goal that did not itself survive gets no entry; an evidence row that did
# not survive is not evidence. So the join adds no plane of its own — it inherits every
# one, and severing any of them subtracts from the report without the report knowing the
# plane exists.
#
# The lane's own planes, which are the entity-thread lane's planes because they are the
# same code: `manifest.canonical_tables` bounds what may be scanned, `_list_canonical_rows`
# supplies rows already disclosed to the request's tier (never `CanonicalStore.get()`),
# `_blackhole_filter_thread_mentions` removes protected records from the mention set AT
# SOURCE, and `_entity_thread_entities` applies the is-self and selector rules.

#: Goals the join runs for. Past this the answer is a list, not an answer, and each goal
#: costs a mention join.
_COMMITMENT_GOAL_CAP = 6
#: Evidence rows retrieved per goal. Evidence is a citation, not a transcript — the point
#: is that the claim is checkable, and four records is enough to check.
_COMMITMENT_EVIDENCE_PER_GOAL_CAP = 4
#: Evidence rows the lane may contribute in total. Deliberately small: commitment evidence
#: is a joined minority lane beside the scope routes, never a replacement for them.
_COMMITMENT_EVIDENCE_CAP = 12
#: Distinct surfaces across all goals used to build the SQL prefilter. As on the entity
#: thread, surfaces bound work; the record-id sets decide membership.
_COMMITMENT_SURFACE_CAP = 16

#: The owner PLACING a commitment. Past tense and first person on purpose: "I'll ship it"
#: said now is not a thing to check progress against. Both auxiliaries are listed because
#: the catalog's own phrasing is "what did I SAY I'd do" — the past tense is carried by
#: "did", so requiring "said" here missed the exact question the mode is for.
_COMMITMENT_STATED_TERMS: Tuple[str, ...] = (
    "say i'd",
    "say i would",
    "say id ",
    "said i'd",
    "said i would",
    "said id ",
    "planned to",
    "promised",
    "committed to",
    "commitment",
    "meant to",
    "supposed to",
    "intended to",
    "told myself",
    "set out to",
)
#: The owner CHECKING it. Both halves are required (`_commitment_intent`), which is what
#: keeps "what are my goals" — a browse — out of this lane entirely.
#:
#: RETROSPECTIVE OR INTERROGATIVE ONLY. The bare infinitives of completion ("finish",
#: "complete", "get done", "stick to") were here first and were wrong: they are the words
#: a commitment is MADE of, so "remind me I said I'd finish the rewrite" satisfied both
#: halves and armed a progress lane on a request that was placing a goal, not auditing
#: one. Only the past tense and the question forms mean "and then what happened".
_COMMITMENT_FOLLOWTHROUGH_TERMS: Tuple[str, ...] = (
    "did i",
    "have i",
    "was i",
    "actually",
    "followed through",
    "got done",
    "finished",
    "completed",
    "progress",
    "made good on",
    "stuck to",
)


def _commitment_intent(query_text: str) -> bool:
    """Is this the commitment question, rather than a browse of the goal list?

    TWO markers are required, one from each half, because the cost of a false positive is
    paid in ranking: the lane contributes items to fusion, so every request it fires on
    that was not this question is a request whose answer got a little different for no
    reason. "What am I working on" must take the byte-identical path it took before, and
    with only one half required it would not — `_EXTRA_SURFACE_TERMS` already routes it to
    the goal lane and "working" would have been enough on its own.
    """
    blob = f" {str(query_text or '').lower()} "
    if not any(term in blob for term in _COMMITMENT_STATED_TERMS):
        return False
    return any(term in blob for term in _COMMITMENT_FOLLOWTHROUGH_TERMS)


def _goal_entity_ids(
    conn: Optional[Any],
    record_id: str,
    *,
    manifest: ScopeResolutionManifest,
) -> Tuple[List[str], Optional[str]]:
    """The entities the owner named IN the sentence that stated this goal.

    This is the goal's identity for the purposes of the join, and it is read off the
    EXISTING entity graph — `entity_mentions` keyed by the goal's own `record_id`, the
    same join the entity-thread lane makes. There is no second resolver here and there
    must never be one: a goal resolved by different rules than a query would attach
    evidence the rest of the pipeline cannot see, which is how a join starts lying.

    The ids are then put through `_entity_thread_entities`, so the is-self guard and the
    selector allow-list apply unchanged. Returns ``(entity_ids, refusal_slug)`` — a goal
    that resolves nothing says which kind of nothing rather than silently contributing.
    """
    if conn is None or not record_id:
        return [], "commitment_goal_unresolved"
    try:
        rows = conn.execute(
            "SELECT DISTINCT entity_id FROM entity_mentions WHERE record_id = ?",
            (record_id,),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — no mention table → no join, not a failure
        logger.debug("commitment goal entity lookup unavailable: %s", exc)
        return [], "commitment_goal_unresolved"
    linked = [{"entity_id": str(r[0])} for r in rows if str(r[0] or "").strip()]
    if not linked:
        return [], "commitment_goal_unresolved"
    kept, _skipped = _entity_thread_entities(conn, linked, manifest=manifest)
    if not kept:
        return [], "commitment_goal_unresolved"
    return kept, None


def _load_commitment_evidence_items(
    *,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    conn: Optional[Any],
    goal_items: List[Dict[str, Any]],
    query_text: str,
    source_ids: List[str],
    disclosure_tier: str,
    first_person: bool,
    belief_intent: bool,
    exposure_visible: bool,
    plan=None,
    ledger: Optional[Any] = None,
    sink: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Retrieve evidence for each stated goal, against that goal's own ids.

    One pass per scanned table over rows `_list_canonical_rows` has already disclosed,
    with the per-goal record-id sets deciding which goal (if any) each row is evidence
    for. A row may be evidence for more than one goal; it enters the packet once.

    EVIDENCE FOLLOWS THE COMMITMENT. A row dated before the goal was stated is not
    evidence that the owner did it, so the window is `[stated_at, ∞)` per goal and a goal
    with no timestamp gets no evidence lane at all rather than an unordered one. This is
    the single rule that stops the join re-creating, with ids instead of wording, the
    "sounds related, must be progress" error it exists to fix.
    """
    tables = [str(t) for t in (manifest.canonical_tables or [])]
    goals = [g for g in (goal_items or []) if str(g.get("goal_id") or "").strip()]
    goals = goals[:_COMMITMENT_GOAL_CAP]
    if sink is not None:
        sink["active"] = True
        sink["tables"] = list(tables)
        sink["goals"] = {}
    if not goals or conn is None:
        return []

    owner_view = str(disclosure_tier or "") == "owner_raw"

    # 1. Resolve every goal to its own ids first, so the per-goal refusals are recorded
    #    even on a grant that names no store to search.
    resolved: List[Dict[str, Any]] = []
    for goal in goals:
        goal_id = str(goal.get("goal_id"))
        record_id = str(goal.get("record_id") or "").strip()
        stated_at = str(goal.get("event_at") or "").strip()
        state: Dict[str, Any] = {
            "goal_id": goal_id,
            "record_id": record_id,
            "source_id": goal.get("source_id"),
            "stated_at": stated_at,
            "entity_ids": [],
            "unresolved_reason": None,
            "candidates": {},
            "reached": 0,
        }
        if sink is not None:
            sink["goals"][goal_id] = state
        if not tables:
            state["unresolved_reason"] = "commitment_scope_no_evidence_store"
            continue
        if not stated_at:
            # Undated: nothing to order evidence against. Reported, never guessed at.
            state["unresolved_reason"] = "commitment_goal_undated"
            continue
        entity_ids, refusal = _goal_entity_ids(conn, record_id, manifest=manifest)
        if refusal:
            state["unresolved_reason"] = refusal
            continue
        state["entity_ids"] = entity_ids
        resolved.append(state)

    if not resolved:
        return []

    # 2. The mention join, per goal, through the shipped helper — so the black hole runs
    #    at SOURCE on every goal's set independently and a protected record is never read.
    surfaces: List[str] = []
    seen_surface: Set[str] = set()
    wanted_by_table: Dict[str, Set[str]] = {}
    for state in resolved:
        by_table, untabled, goal_surfaces, entity_by_record = _entity_thread_mentions(
            conn, state["entity_ids"], tables=tables
        )
        by_table, untabled = _blackhole_filter_thread_mentions(
            by_table, untabled, conn=conn, owner_view=owner_view
        )
        # The goal's own record is not evidence of itself.
        own = str(state["record_id"] or "")
        state["_by_table"] = {t: {r for r in ids if r != own} for t, ids in by_table.items()}
        state["_untabled"] = {r for r in untabled if r != own}
        state["_entity_by_record"] = entity_by_record
        for table in tables:
            reach = set(state["_by_table"].get(table) or set()) | state["_untabled"]
            if reach:
                wanted_by_table.setdefault(table, set()).update(reach)
        for surface in goal_surfaces:
            if surface not in seen_surface and len(surfaces) < _COMMITMENT_SURFACE_CAP:
                seen_surface.add(surface)
                surfaces.append(surface)

    if not wanted_by_table:
        return []

    # 3. One disclosed scan per table, attributed back to whichever goals wanted the row.
    items: List[Dict[str, Any]] = []
    emitted: Set[str] = set()
    role_cache: Dict[str, Optional[bool]] = {}
    display_cache: Dict[str, str] = {}
    highlight_cache: Dict[str, str] = {}
    for table in tables:
        wanted = wanted_by_table.get(table) or set()
        if not wanted:
            continue
        rows: List[Dict[str, Any]] = []
        if surfaces:
            rows += _list_canonical_rows(
                adapters,
                table,
                source_ids=source_ids,
                limit=_ENTITY_THREAD_SCAN_LIMIT,
                disclosure_tier=disclosure_tier,
                contains=surfaces,
            )
        rows += _list_canonical_rows(
            adapters,
            table,
            source_ids=source_ids,
            limit=_ENTITY_THREAD_SCAN_LIMIT,
            disclosure_tier=disclosure_tier,
        )
        seen_row: Set[str] = set()
        for row in rows:
            rid = str(row.get("record_id") or row.get("message_id") or "").strip()
            if not rid or rid not in wanted or rid in seen_row:
                continue
            seen_row.add(rid)
            event_at = str(row.get("event_at") or row.get("starts_at") or row.get("entry_at") or "")
            # Which goals is this row evidence FOR? Both halves must hold: the goal's own
            # ids must reach the record, and the record must not predate the statement.
            for_goals = [
                state
                for state in resolved
                if rid in (set(state["_by_table"].get(table) or set()) | state["_untabled"])
                and event_at
                and event_at >= str(state["stated_at"])
                and len(state["candidates"]) < _COMMITMENT_EVIDENCE_PER_GOAL_CAP
            ]
            if not for_goals:
                continue
            item = _canonical_row_to_item(
                table,
                row,
                manifest=manifest,
                query_text=query_text,
                conn=conn,
                first_person=first_person,
                belief_intent=belief_intent,
                exposure_visible=exposure_visible,
                role_cache=role_cache,
                display_cache=display_cache,
                highlight_cache=highlight_cache,
                retrieval_source=f"commitment_evidence:{table}",
            )
            if item is None:
                continue
            # The keys the exclusion filter matches on, set for the same reason the
            # entity-thread lane sets them: "…but nothing about X" has to be able to
            # reach a row that arrived because of X, however it arrived.
            item["canonical_table"] = table
            entity_id = None
            for state in for_goals:
                entity_id = entity_id or state["_entity_by_record"].get(rid)
                state["candidates"][rid] = {
                    "record_id": rid,
                    "canonical_table": table,
                    "event_at": event_at,
                    "entity_id": state["_entity_by_record"].get(rid),
                }
                state["reached"] += 1
            if entity_id:
                item["entity_id"] = entity_id
            if rid not in emitted and len(items) < _COMMITMENT_EVIDENCE_CAP:
                emitted.add(rid)
                items.append(item)

    if ledger is not None:
        ledger.record(
            _N.STAGE_RETRIEVAL,
            "contributed",
            "commitment_evidence_lane",
            dropped=max(0, sum(s["reached"] for s in resolved) - len(items)),
            detail={
                "goals": len(goals),
                "goals_joined": len(resolved),
                "contributed": len(items),
                "tables": sorted(wanted_by_table),
            },
        )
    return items


def _attach_commitment_report(
    packet: Dict[str, Any],
    sink: Optional[Dict[str, Any]],
    *,
    conn: Optional[Any],
    adapters: AdapterBundle,
    manifest: ScopeResolutionManifest,
    disclosure_tier: str,
    installed_source_ids: Optional[List[str]] = None,
    ledger: Optional[Any] = None,
) -> None:
    """Answer PER GOAL over the rows the packet actually kept, and attach it.

    Called at the END of `retrieve()`, beside `_attach_topic_thread` and for the same
    reason: every subtraction the packet is going to suffer has already happened to the
    set this reads. A goal whose own row did not survive gets no entry at all; an evidence
    record that did not survive is not evidence. That intersection IS the privacy
    argument — the report cannot name a record the answer does not already carry.

    ENTITY IDS ARE OWNER-ONLY, everywhere in this block. The entity lane has already
    leaked existence once by handing a grantee the `entity_id`, `record_id`, table and
    timestamp of a black-holed subject, and a per-goal report is a denser version of
    exactly that shape. Records and timestamps here are safe because they are already in
    `summaries`; the entity ids are not, because nothing else in the packet published
    them. Grantees get `entity_count`, on the same "named for the owner, counted for
    everyone else" rule the thread's participant roster runs on.
    """
    if not isinstance(sink, dict) or not sink.get("active"):
        return
    states = list((sink.get("goals") or {}).values())
    if not states:
        return
    summaries = packet.get("summaries")
    if not isinstance(summaries, list):
        return

    owner_view = str(disclosure_tier or "") == "owner_raw"
    surviving: Set[str] = {
        str(item.get("record_id")).strip()
        for item in summaries
        if str(item.get("record_id") or "").strip()
    }
    # Was an exclusion actually enforced on this packet? It is the one removal a goal may
    # be told about by name: the requester wrote it, so reporting it discloses nothing
    # they did not already say. Every other removal is reported as absence.
    excluded = bool((packet.get("exclusion") or {}).get("enforced"))
    stores_empty: Optional[bool] = None

    goals_out: List[Dict[str, Any]] = []
    with_evidence = 0
    unresolved = 0
    withheld = 0
    for state in states:
        if str(state.get("record_id") or "").strip() not in surviving:
            # The goal itself did not survive to the answer. Reporting on it would be the
            # report reaching around the plane that removed it.
            continue
        candidates: Dict[str, Any] = dict(state.get("candidates") or {})
        evidence = [c for rid, c in candidates.items() if rid in surviving]
        evidence.sort(key=lambda c: str(c.get("event_at") or ""))

        entry: Dict[str, Any] = {
            "goal_id": state.get("goal_id"),
            "record_id": state.get("record_id"),
            "stated_at": state.get("stated_at") or None,
            "entity_count": len(state.get("entity_ids") or []),
        }
        if owner_view and state.get("entity_ids"):
            entry["entity_ids"] = sorted(set(state["entity_ids"]))

        if evidence:
            with_evidence += 1
            entry["status"] = "evidence_found"
            # `evidence_records`, NOT `evidence`. The bare key is in
            # FORBIDDEN_ARTIFACT_KEYS and `validate_public_result` walks the payload
            # RECURSIVELY, so naming it `evidence` killed every turn that kept a goal
            # — the whole turn, at pipeline.py's unguarded validate call. The ban is
            # aimed at internal evidence BLOBS (raw rows, prompts, vectors); this list
            # is pointers — record ids, tables and timestamps already carried in
            # `summaries` beside it. Keep it pointer-shaped and keep the name distinct.
            entry["evidence_records"] = [
                {
                    "record_id": c["record_id"],
                    "canonical_table": c["canonical_table"],
                    "event_at": c["event_at"] or None,
                    **({"entity_id": c["entity_id"]} if owner_view and c.get("entity_id") else {}),
                }
                for c in evidence
            ]
            entry["evidence_count"] = len(evidence)
            goals_out.append(entry)
            continue

        # --- no evidence. Say which kind of nothing. -------------------------------
        entry["status"] = "no_evidence"
        entry["evidence_records"] = []
        entry["evidence_count"] = 0
        refusal = state.get("unresolved_reason")
        if refusal:
            unresolved += 1
            entry["empty_cause"] = _N.CAUSE_NOT_QUERIED
            entry["empty_reason"] = refusal
        elif candidates and excluded and owner_view:
            # THE VETOED LANE. Candidates were reached and a plane took them away. This is
            # not the same statement as "I looked and there is nothing", and collapsing
            # the two would tell the owner their week was empty when their own exclusion
            # emptied it. Owner-only: for a grantee the same line is a receipt that the
            # goal has evidence they are not being given.
            withheld += 1
            entry["empty_cause"] = _N.CAUSE_SCOPE_DENIED
            entry["empty_reason"] = "commitment_evidence_withheld"
        elif candidates:
            # Reached, then lost to a cap or a filter this block cannot name. Honest
            # about the difference between "there is nothing" and "nothing of it is in
            # this answer" without claiming a denial it cannot evidence.
            entry["empty_cause"] = _N.CAUSE_NO_MATCH
            entry["empty_reason"] = "commitment_evidence_not_in_answer"
        else:
            if stores_empty is None:
                stores_empty = _scope_stores_are_empty(conn, manifest)
            if stores_empty is True:
                entry["empty_cause"] = _N.CAUSE_STORE_EMPTY
                entry["empty_reason"] = (
                    _scope_supply_state(conn, manifest, installed_source_ids)
                    or "scope_stores_hold_no_rows"
                )
            else:
                entry["empty_cause"] = _N.CAUSE_NO_MATCH
                entry["empty_reason"] = "commitment_no_evidence_matched"
        goals_out.append(entry)

    if not goals_out:
        return

    packet["commitment_report"] = {
        "goals": goals_out,
        "goal_count": len(goals_out),
        "goals_with_evidence": with_evidence,
        "goals_without_evidence": len(goals_out) - with_evidence,
        "stores": sorted(str(t) for t in (sink.get("tables") or [])),
    }

    if ledger is None:
        return
    if not (sink.get("tables") or []):
        ledger.record(
            _N.STAGE_RETRIEVAL,
            "not_applied",
            "commitment_scope_no_evidence_store",
            detail={"goals": len(goals_out)},
        )
    if unresolved:
        # A COUNT OF GAPS IN THE GRAPH, not a statement about any subject. It says N of
        # the owner's own goals could not be joined at all — which is the finding this
        # mode is for, and is the same sentence on a node with no entity graph.
        ledger.record(
            _N.STAGE_RETRIEVAL,
            "scoped",
            "commitment_goal_unresolved",
            dropped=unresolved,
        )
    without = len(goals_out) - with_evidence
    if without:
        ledger.record(
            _N.STAGE_RETRIEVAL,
            "emptied",
            "commitment_no_evidence_matched",
            dropped=without,
            detail={"goals": len(goals_out), "with_evidence": with_evidence},
        )
    if withheld:
        # Owner-only by construction (`owner_view` above). For a grantee this line is the
        # receipt that discloses the evidence exists, which is the leak the entity lane
        # has already had once.
        ledger.record(
            _N.STAGE_DISCLOSURE,
            "dropped_items",
            "commitment_evidence_withheld",
            dropped=withheld,
        )


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


#: One `attention_summary` and one `interest_profile` per day, so a window's day
#: count doubles into the number of objects that answer it.
_ATTENTION_OBJECTS_PER_DAY = 2

#: Days a windowed fetch will reach back for. A window is a request for its days,
#: but an unbounded one would put a year of digests into a single summary payload;
#: past this the newest days inside the window answer.
_ATTENTION_WINDOW_FETCH_DAYS_CAP = 31

#: `substr(object_key, -10)` on a dated key. The loader already SORTS on this
#: expression, so windowing on it cannot disagree with the order it serves in.
_ATTENTION_DAY_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"


def _attention_window_fetch_limit(window: Optional[DerivedWindow], default_limit: int) -> int:
    """Row budget wide enough to hold every day a resolved window asks for.

    The default of ten objects is five days — right for "what did I miss yesterday",
    and silently wrong for a week's report, which is the shape that found this.
    """
    if window is None or not window.resolved:
        return default_limit
    try:
        start = datetime.strptime(str(window.start)[:10], "%Y-%m-%d").date()
        end = datetime.strptime(str(window.end)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default_limit
    days = (end - start).days + 1
    if days < 1:
        return default_limit
    return max(default_limit, min(days, _ATTENTION_WINDOW_FETCH_DAYS_CAP) * _ATTENTION_OBJECTS_PER_DAY)


def _count_attention_summary_items(conn: Optional[Any]) -> int:
    """How many triage digests this node holds, ignoring any window.

    Only ever asked when a windowed fetch came back empty, and only to tell
    "the triage has nothing in your window" apart from "this node runs no triage".
    """
    if conn is None:
        return 0
    try:
        row = conn.execute(
            "SELECT count(*) FROM signal_objects "
            "WHERE signal_dimension='interests' AND valid_to IS NULL "
            "AND object_type IN ('attention_summary','interest_profile')"
        ).fetchone()
    except Exception:
        return 0
    return int(row[0]) if row else 0


def _load_attention_summary_items(
    conn: Optional[Any],
    limit: int = 10,
    *,
    window: Optional[DerivedWindow] = None,
) -> List[Dict[str, Any]]:
    """Attention-triage objects (daily digests + interest profiles) as summary items —
    the attention:read scope's primary content (PLAN_ATTENTION_TRIAGE.md M2). The
    attention_summary payload already enforces the silence invariant (no discard
    references), so shaping is safe.

    A resolved ``window`` selects the days IN SQL. It used to be applied afterwards, by
    `_attention_items_in_window`, to whatever the newest-ten happened to be: a report
    for Aug 11-16 asked on Aug 21 fetched Aug 17-21, dropped all of it, and said the
    window held no triage — while the node held a digest for every day of it. Filtering
    a fixed page is not the same as asking for the days, and the difference is invisible
    downstream: the empty and the ledger line both look exactly like a quiet week.

    Undated keys are selected whatever the window, matching the rule
    `_attention_items_in_window` documents — evidence must not vanish on a formatting
    accident.
    """
    if conn is None:
        return []
    sql = (
        "SELECT object_type, object_key, payload_json FROM signal_objects "
        "WHERE signal_dimension='interests' AND valid_to IS NULL "
        "AND object_type IN ('attention_summary','interest_profile')"
    )
    params: List[Any] = []
    if window is not None and window.resolved:
        sql += (
            " AND (substr(object_key, -10) BETWEEN ? AND ?"
            f" OR substr(object_key, -10) NOT GLOB '{_ATTENTION_DAY_GLOB}')"
        )
        params.extend([str(window.start)[:10], str(window.end)[:10]])
    sql += " ORDER BY substr(object_key, -10) DESC, object_type ASC LIMIT ?"
    params.append(_attention_window_fetch_limit(window, limit))
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
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
                    # Derived signal objects share this index with raw records
                    # and are split out of it by record_type below. Dropping
                    # the type here is what would make the derived lane
                    # unreachable AFTER it was written — the exact
                    # written-but-never-read failure this lane was added to end.
                    "record_type": item.get("record_type"),
                    "object_type": item.get("object_type"),
                    "object_key": item.get("object_key"),
                    "title": item.get("title"),
                    "disclosure": item.get("disclosure"),
                    "person_name": item.get("person_name"),
                    "entity_id": item.get("entity_id"),
                    "predicate": item.get("predicate"),
                    "message_count": item.get("message_count"),
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
    # A mention pointer is the entity plane's own artefact, shipped beside the
    # dossier line, so it rides the same grant. Without an entry here it would
    # fall to the default (`stat_insights`) and an unrelated grant would unlock it.
    "entity_mention": "entity_dossiers",
    "fact": "owner_facts",
    # The derived relationship graph rides the grant the relationship scope
    # already declares. Without an entry it would fall to the default
    # (`stat_insights`) and a spend-pattern grant would unlock who the owner is
    # close to — the same mis-binding the entity_mention note above records.
    "RelationshipEdge": "relationship_edges",
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
    include_packet_fields: bool = False,
) -> List[Dict[str, Any]]:
    """Atomic facts: subject-first for linked entities, then token search.

    include_packet_fields — packet-resolution 'facts'/'facts_all' turns only: items
    additionally carry value/dates/altitude/pack/sensitivity so the inference packet
    can emit a structured facts block. OFF for the summary path: grantee-facing
    surfaces keep exactly their pre-feature shape.

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
        item = {
            "topic": text[:120],
            "summary_text": text,
            "record_id": fact["object_id"],
            "predicate": payload.get("predicate"),
            "retrieval_source": "fact",
            "relevance_score": round(min(1.0, 0.6 + 0.1 * overlap), 4),
            "_overlap": overlap,
        }
        if include_packet_fields:
            item.update({
                "value": payload.get("object_value"),
                "valid_from": fact.get("valid_from"),
                "valid_to": fact.get("valid_to"),
                "altitude": payload.get("altitude") or fact.get("altitude"),
                "pack": payload.get("pack") or fact.get("ontology_id"),
                "sensitivity": payload.get("sensitivity"),
                "confidence": payload.get("confidence"),
            })
        items.append(item)
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
    # "Echo Foxtrot". Keep identifier rows distinct from the contact + each other.
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
    # entity_mention rows are POINTERS, not content: `entity_context_items` emits
    # "<surface> in <table>" for a record it never reads. Collapsing them under
    # rec:{id} lets the pointer win the payload — `best_item` keeps the FIRST lane
    # to claim a key and `entities` fuses ahead of `canonical` — so whenever a
    # mention happens to point at a record, the owner's actual sentence is replaced
    # by "2026-03-13 — Anthropic". That was already true of the canonical lane
    # before the entity-thread lane existed; both are shadowed identically. Third
    # instance of the two cases above, and the same remedy.
    if record_id and retrieval == "entity_mention":
        return f"rec:{record_id}|mention:{str(item.get('entity_id') or '')}"
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
    # `derived_objects` belongs here for the same reason `facts_store` does:
    # a RelationshipEdge or a dossier is a statement about how things ARE, not
    # an event that happened at a time. Decaying it would rank the owner's
    # mother below last night's chatter.
    {"stat_insights", "facts_store", "entities", "briefs", "goals", "derived_objects"}
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
    rare_token_groups: Optional[List[Dict[str, int]]] = None,
    min_per_source: Optional[Dict[str, int]] = None,
    ledger: Optional[Any] = None,
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

    `rare_token_groups` runs that gate ONCE PER PART of a multi-part request and vetoes
    only when EVERY part is vetoed. The flat `rare_tokens` form is the one-part case and
    is evaluated identically — but a multi-part request handed the flat form is a gate
    that cannot work: one unevidenced token anywhere empties every part's lane, so a
    report's specific section takes down its ordinary sections and the sections most in
    need of the gate are exactly the ones it can never fire for.

    Three of those returns are empty and they are NOT the same empty: nothing was
    ever a candidate, or candidates existed and the rare gate vetoed them. Told
    apart they are "connect a source" and "you asked about something your data does
    not mention"; conflated they are both "no data", which is how a report came to
    tell the owner their journal "may not be synced" while it sat indexed. `ledger`
    (optional, never load-bearing) is where that distinction is written down.
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
        if ledger is not None:
            ledger.empty(
                _N.CAUSE_STORE_EMPTY,
                stage=_N.STAGE_RETRIEVAL,
                reason="no_evidence_lane_returned_rows",
            )
        return []
    groups: List[Dict[str, int]] = []
    if rare_token_groups is not None:
        groups = [dict(g) for g in rare_token_groups]
    elif rare_tokens:
        groups = [
            dict(rare_tokens) if isinstance(rare_tokens, dict)
            else {t: 1 for t in rare_tokens}
        ]
    if groups:
        blobs = [_item_text_blob(item) for item in evidence_items]

        def _evidenced(token: str) -> bool:
            # Variant-aware: items saying 'journal' evidence a 'journaling' ask.
            return any(v in blob for v in _token_variants(token) for blob in blobs)

        def _veto_for(rare_dfs: Dict[str, int]) -> Optional[Tuple[str, List[str]]]:
            """(reason_slug, offending_tokens) if THIS part's ask is unanswerable."""
            if not rare_dfs:
                # No needles in this part — it never carried a specific ask, so it has
                # nothing to be unanswerable about (a pure date-scoped "my week" is
                # narrowed by its window alone).
                return None
            # Every effectively-absent token (df ≤ 2: zero, or a porter-stem
            # collision like 'falconer'→'falcon' df 1) must be evidenced by the
            # returned items themselves, or the ask is about something that does
            # not exist. Answer-shape vocabulary ('cadence', 'frequency') is
            # excluded upstream by the token stoplist — it describes the aggregate
            # wanted, not row content.
            unevidenced = [t for t, df in rare_dfs.items() if df <= 2 and not _evidenced(t)]
            if unevidenced:
                return "rare_token_unevidenced", unevidenced
            # A query with SEVERAL rare tokens is a specific ask even when stem
            # collisions keep each df nonzero ('years as a competitive falconer':
            # falconer→'falcon' df 1, competitive df 26): if NONE of them is
            # evidenced, nothing retrieved is about the ask. A single weakly-rare
            # token ('journaling' df 5, 'cadence' df 1) never vetoes alone — the
            # derived layers may answer it without containing the word.
            if len(rare_dfs) >= 2 and not any(_evidenced(t) for t in rare_dfs):
                return "no_rare_token_evidenced", sorted(rare_dfs)
            return None

        vetoes = [_veto_for(group) for group in groups]
        if all(veto is not None for veto in vetoes):
            # Every part is unanswerable — the same honest empty as before. The
            # reported reason is the first part's, which for a one-part request is
            # byte-identical to the pre-multi-needle ledger entry.
            reason, tokens = vetoes[0]  # type: ignore[misc]
            if ledger is not None:
                ledger.empty(
                    _N.CAUSE_GATE_VETOED,
                    stage=_N.STAGE_RARE_GATE,
                    reason=reason,
                    dropped=len(evidence_items),
                    # Local only — the tokens are the owner's words.
                    detail={
                        "tokens": tokens,
                        "n_rare": len(groups[0]),
                        "parts_vetoed": len(groups),
                        "n_parts": len(groups),
                    },
                )
            return []
        if ledger is not None and any(veto is not None for veto in vetoes):
            # SOME part was unanswerable and the rest were not. Before per-part
            # needles this was an empty answer for all of them; now it is a narrowing
            # worth naming, so a section that genuinely has nothing can still say so.
            ledger.record(
                _N.STAGE_RARE_GATE,
                "scoped",
                "rare_gate_partial_veto",
                detail={
                    "parts_vetoed": [i for i, v in enumerate(vetoes) if v is not None],
                    "n_parts": len(groups),
                },
            )

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
    if ledger is not None and len(ranked) > cap:
        ledger.record(
            _N.STAGE_RETRIEVAL,
            "capped",
            "summary_item_cap",
            dropped=len(ranked) - cap,
        )

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


#: Where a summary item keeps the id of the canonical row behind it. The same tuple
#: the exclusion filter matches on, for the same reason: one row shape, many lanes.
_BLACKHOLE_RECORD_ID_KEYS = ("record_id", "message_id", "id", "canonical_record_id")
#: Where it keeps the entity it arrived by. The entity lane sets this explicitly.
_BLACKHOLE_ENTITY_ID_KEYS = ("entity_id", "subject_entity_id", "object_entity_id")


def _blackhole_blocked_record_ids(conn: Optional[Any]) -> Set[str]:
    """Canonical records linked to a protected entity, by id rather than by name.

    The name scan is a floor: it can only catch a row that SAYS the name. The
    entity lane's entire purpose is reaching rows that do not — it finds them
    through `entity_mentions` — so on that lane the scan is structurally the
    wrong instrument, and a black-holed entity's thread record sailed through it
    with the body correctly withheld and `record_id`, `entity_id`,
    `canonical_table` and `event_at` intact. That is the disclosure tier holding
    while the black hole fails: the caller learned the entity exists, which is
    the one thing D5 says must be impossible.

    `BlackholeGuard` already answers the id half exactly, from the same mention
    join the lane used to find the row. It is asked here rather than re-derived.

    The guard is built as a GRANTEE deliberately: this returns the FULL blocked
    set and the owner/non-owner split is the caller's, because the owner needs
    the same set to stamp with and a guard that `sees_everything` returns none.
    """
    if conn is None:
        return set()
    from ..features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass

    return BlackholeGuard(conn, caller_class=CallerClass.GRANTEE).blocked_record_ids()


def _blackhole_policy_for_summary(
    items: List[Dict[str, Any]],
    *,
    conn: Optional[Any],
    disclosure_tier: str,
) -> List[Dict[str, Any]]:
    """Apply the entity black hole to assembled summary items.

    This is the grantee-facing pipeline, so the rule splits on who is asking:

    * **Not `owner_raw`** — a grantee. Items touching a protected entity are
      dropped outright. `resolve_disclosure_tier` never elevates a grantee to
      `owner_raw`, so this tier check is a sound proxy for "not the owner".
    * **`owner_raw`** — the owner or something running as them (a routine).
      Items are kept, because the owner is entitled to them, but each protected
      item is stamped `blackhole_protected`. That stamp is the taint feed the
      control plane needs: it cannot detect protected content itself, and
      without it Gate C on the BYOK route can never fire.

    "Touching" is decided by two instruments, and the id half runs first because
    it is the exact one:

    1. **id** — the row's record id is in `blocked_record_ids()`, or it carries a
       protected `entity_id`. This catches a row that never says the name, which
       is precisely what the entity lane retrieves.
    2. **name** — a scan of the item's prose. This catches a name the resolver
       never bound to an id, which no join can see.

    Neither is sufficient alone, which is why both run. This is the wrapper every
    build path exits through, so it is also the backstop for the lanes that do
    not filter at source.
    """
    if conn is None or not items:
        return items
    try:
        from ..features.lifecycle.blackhole import (
            blackholed_entity_ids,
            blackholed_name_terms,
            normalize_entity_name,
        )
    except Exception:  # noqa: BLE001
        return items
    try:
        terms = blackholed_name_terms(conn)
        blocked_ids = blackholed_entity_ids(conn)
        blocked_records = _blackhole_blocked_record_ids(conn) if blocked_ids else set()
    except Exception:  # noqa: BLE001
        # A store that cannot answer must not silently serve protected content
        # to a grantee; the owner's own path is unaffected.
        if str(disclosure_tier or "") == "owner_raw":
            return items
        raise
    if not terms and not blocked_ids:
        return items

    owner_view = str(disclosure_tier or "") == "owner_raw"
    kept: List[Dict[str, Any]] = []
    for item in items:
        hit = _blackhole_id_hit(item, blocked_records, blocked_ids)
        if not hit:
            blob = normalize_entity_name(_item_text_blob(item))
            hit = bool(blob) and any(term in blob for term in terms)
        if not hit:
            kept.append(item)
            continue
        if owner_view:
            kept.append({**item, "blackhole_protected": True})
    return kept


def _blackhole_id_hit(
    item: Dict[str, Any], blocked_records: Set[str], blocked_entities: Set[str]
) -> bool:
    """Does this item point at a protected entity by an id it carries?"""
    for key in _BLACKHOLE_RECORD_ID_KEYS:
        value = item.get(key)
        if value is not None and str(value) in blocked_records:
            return True
    for key in _BLACKHOLE_ENTITY_ID_KEYS:
        value = item.get(key)
        if value is not None and str(value) in blocked_entities:
            return True
    return False


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
    needle_text: str = "",
    needle_parts: Optional[List[str]] = None,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    query_text: str,
    semantic_hits: List[Dict[str, Any]],
    ranked_clusters: List[Dict[str, Any]],
    derived_hits: Optional[List[Dict[str, Any]]] = None,
    installed_source_ids: Optional[List[str]] = None,
    disclosure_tier: str = "owner_raw",
    plan=None,
    now: Optional[datetime] = None,
    ledger: Optional[Any] = None,
    thread_sink: Optional[Dict[str, Any]] = None,
    commitment_sink: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Assemble summary items, then apply the black-hole policy to every exit.

    The policy is applied here rather than inside the builder because the
    builder has two return paths (fused and unfused); wrapping is the only way
    to be sure a future third path cannot bypass it.
    """
    items = _build_summary_items_unfiltered(
        needle_text=needle_text,
        needle_parts=needle_parts,
        manifest=manifest,
        adapters=adapters,
        query_text=query_text,
        semantic_hits=semantic_hits,
        ranked_clusters=ranked_clusters,
        derived_hits=derived_hits,
        installed_source_ids=installed_source_ids,
        disclosure_tier=disclosure_tier,
        plan=plan,
        now=now,
        ledger=ledger,
        thread_sink=thread_sink,
        commitment_sink=commitment_sink,
    )
    kept = _blackhole_policy_for_summary(
        items,
        conn=getattr(adapters.signal, "_conn", None),
        disclosure_tier=disclosure_tier,
    )
    dropped = len(items) - len(kept)
    # THE RECEIPT IS THE LEAK. Every other stage here owes the caller an account of
    # why their answer got smaller, and this one owes them the opposite. `as_public()`
    # leaves the node, so a line reading `stage=disclosure, action=dropped_items,
    # reason=blackhole_policy, dropped=1` tells a grantee — in a closed-set slug whose
    # meaning the protocol guarantees — that the entity they asked about exists, has
    # records, and is being kept from them. `empty(scope_denied)` says it louder: not
    # "nothing matched" but "you were refused". That converts hiding-by-absence into
    # hiding-by-denial, which is the single thing D5 rules out, and it is the reason
    # `BlackholeGuard` returns empty rows rather than raising.
    #
    # It is only ever emitted in the leaking direction: the owner keeps every row and
    # gets it stamped instead, so `dropped` is 0 on their side and there is nothing to
    # report anyway. Non-owners get the debug line, which stays on the node.
    if dropped > 0:
        if str(disclosure_tier or "") != "owner_raw":
            logger.debug("blackhole policy withheld %d summary item(s)", dropped)
        elif ledger is not None:
            ledger.record(
                _N.STAGE_DISCLOSURE, "dropped_items", "blackhole_policy", dropped=dropped
            )
    return kept


def _build_summary_items_unfiltered(
    *,
    needle_text: str = "",
    needle_parts: Optional[List[str]] = None,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    query_text: str,
    semantic_hits: List[Dict[str, Any]],
    ranked_clusters: List[Dict[str, Any]],
    derived_hits: Optional[List[Dict[str, Any]]] = None,
    installed_source_ids: Optional[List[str]] = None,
    disclosure_tier: str = "owner_raw",
    plan=None,
    now: Optional[datetime] = None,
    ledger: Optional[Any] = None,
    thread_sink: Optional[Dict[str, Any]] = None,
    commitment_sink: Optional[Dict[str, Any]] = None,
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
    query_tokens = _query_tokens(needle_text or query_text)
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
    # The same needles, split per part, for the gates below. One df pass over the
    # union — the flat set above stays exactly what it was, so a single-part request
    # produces one group holding the identical tokens.
    rare_groups = _rare_token_groups(
        bundle_conn, _needle_token_groups(needle_text or query_text, needle_parts)
    )
    if first_person and rare_groups:
        rare_groups = [
            {t: df for t, df in group.items() if t not in _FIRST_PERSON_SHAPE_TOKENS}
            for group in rare_groups
        ]

    # Q1: "what did I say I'd do … and did I actually do it". Detected once, here, and
    # threaded — it widens the goal gate below and switches the per-goal evidence join on.
    commitment_intent = bool(query_text) and _commitment_intent(query_text)

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
            goal_intent=commitment_intent,
        )

    canonical_items = _load_canonical_summary_items(
        manifest=manifest,
        adapters=adapters,
        query_text=query_text,
        source_ids=source_ids,
        disclosure_tier=disclosure_tier,
        rare_query_tokens=rare_query_tokens,
        rare_query_token_groups=rare_groups,
        plan=plan,
        conn=bundle_conn,
        exposure_visible=exposure_visible,
        ledger=ledger,
    )
    _canonical_before_window = len(canonical_items)
    canonical_items = _prefer_time_window(
        canonical_items, getattr(plan, "time_range", None) if plan else None
    )
    if ledger is not None and len(canonical_items) < _canonical_before_window:
        # The soft window kept only the in-range rows. Nothing was lost that the
        # owner asked for, but the search DID get smaller and the count says by how much.
        ledger.record(
            _N.STAGE_PLANNER,
            "windowed",
            "prefer_time_window",
            dropped=_canonical_before_window - len(canonical_items),
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
                    "SELECT entity_id FROM entities WHERE is_self=1"
                    " ORDER BY (SELECT COUNT(*) FROM signal_objects o"
                    "   WHERE o.object_type='fact' AND o.object_key LIKE"
                    "   'fact:' || entities.entity_id || ':%') DESC, entity_id ASC LIMIT 1"
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
            # The tier is NOT optional here. `_list_canonical_rows` defaults to
            # `owner_raw`, and this was the one call site out of nine that took
            # the default — so a grantee's work-context ask reached this lane's
            # rows at the owner's tier, past the NSFW exclusion and the
            # disclosure-column swap the other eight get for free.
            for row in _list_canonical_rows(
                adapters,
                "profile_records",
                source_ids=source_ids,
                limit=50,
                disclosure_tier=disclosure_tier,
            ):
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
        # `text_preview` is a 200-char label; `search_text` is the indexed body
        # and sits in the same dict, written by the same insert. Handing the
        # preview to synthesis delivered every long entry cut off mid-sentence
        # while the full text was already in hand — no extra query, no extra
        # cost. The label stays short (it is what the UI shows as the topic);
        # the SUMMARY, which synthesis actually reads, gets the body.
        #
        # The lexical check reads the body too. Matching a content token only
        # within the first 200 characters made "is this hit about the ask?"
        # depend on where in the entry the word happened to fall.
        preview = str(hit.get("text_preview") or "")
        body = str(hit.get("search_text") or "") or preview
        preview_lower = body.lower()
        similarity = float(hit.get("similarity") or 0.0)
        item = {
            "topic": preview,
            "summary_text": body,
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

    # The derived layer's own lane. These items are already sentences — the
    # index stores the rendered text, not the object's JSON — so the whole
    # contribution is a re-key, not a second rendering pass. `topic` carries the
    # subject's NAME rather than the sentence: it is what the rare-token gate
    # reads (`_item_text_blob`) and what synthesis shows, and a lane about
    # people that never surfaces a name answers nothing.
    derived_items: List[Dict[str, Any]] = []
    for hit in derived_hits or []:
        text = str(hit.get("text_preview") or "").strip()
        if not text:
            continue
        object_type = str(hit.get("object_type") or "")
        derived_items.append(
            {
                "topic": str(hit.get("title") or text.splitlines()[-1])[:160],
                "summary_text": text,
                "record_id": hit.get("record_id"),
                "object_type": object_type,
                "object_key": hit.get("object_key"),
                "entity_id": hit.get("entity_id"),
                "disclosure": hit.get("disclosure"),
                "signal_dimension": hit.get("signal_dimension"),
                "relevance_score": round(float(hit.get("similarity") or 0.0), 4),
                "retrieval_source": f"derived:{object_type}" if object_type else "derived_object",
            }
        )

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
    entity_thread_items: List[Dict[str, Any]] = []
    fact_store_items: List[Dict[str, Any]] = []
    if query_text:
        try:
            from ..core.state import get_db_connection
            from ..features.entities.linking import entity_context_items, link_query_entities

            conn = raw_conn if raw_conn is not None else get_db_connection()
            if conn is not None:
                linked = link_query_entities(conn, query_text)
                temporal_shift = getattr(plan, "temporal_shift", None) if plan else None
                # T7 pass-through (B2.2): past-tense asks widen the edge read to
                # closed revisions ("no longer current" marker). `manifest` bounds
                # the mention lane to the grant's tables — the pointer it emits
                # names a table and a record id, which is a disclosure whether or
                # not the record is ever read.
                #
                # The `except TypeError` that used to wrap this call is gone. It
                # guarded a pre-M1 `linking` that cannot exist in this tree, and
                # it would have swallowed exactly the signature error a missing
                # `manifest=` raises — turning a required bound into a silently
                # unbounded lane, which is the defect it sits on top of.
                raw_entity_items = entity_context_items(
                    conn, linked, manifest=manifest, temporal_shift=temporal_shift
                )
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
                # P4: the same resolution, one join further. `entity_context_items`
                # above contributes what the graph SAYS about the entity (a dossier
                # line, four mention surfaces); this contributes the entity's actual
                # RECORDS — the thread — which no lane could otherwise reach without
                # the query happening to contain the right word.
                entity_thread_items = _load_entity_thread_items(
                    manifest=manifest,
                    adapters=adapters,
                    conn=conn,
                    linked=linked,
                    query_text=query_text,
                    source_ids=source_ids,
                    disclosure_tier=disclosure_tier,
                    first_person=first_person,
                    belief_intent=belief_intent,
                    exposure_visible=exposure_visible,
                    plan=plan,
                    ledger=ledger,
                    thread_sink=thread_sink,
                )
        except Exception as exc:
            logger.debug("entity linking skipped: %s", exc)

    # Q1. The per-goal evidence join. Gated on BOTH the commitment question and goals
    # actually being in play, so every other request in the corpus takes the byte-identical
    # path it took before this lane existed — the ranking floor is measured, not assumed.
    commitment_items: List[Dict[str, Any]] = []
    if commitment_intent and goal_items:
        try:
            commitment_items = _load_commitment_evidence_items(
                manifest=manifest,
                adapters=adapters,
                conn=bundle_conn,
                goal_items=goal_items,
                query_text=query_text,
                source_ids=source_ids,
                disclosure_tier=disclosure_tier,
                first_person=first_person,
                belief_intent=belief_intent,
                exposure_visible=exposure_visible,
                plan=plan,
                ledger=ledger,
                sink=commitment_sink,
            )
        except Exception as exc:  # noqa: BLE001 — a join failure must not lose the turn
            logger.debug("commitment evidence lane skipped: %s", exc)

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
                ("facts_store", _facts_lane_weight(), fact_store_items),
                ("entities", 1.5, entity_items),
                # Above the raw vector lane and beside `entities`, because that
                # is what these items ARE: the entity plane's conclusions,
                # reached semantically instead of by the query happening to
                # contain the subject's name. Below `facts_store`, which is the
                # same content addressed exactly rather than by similarity.
                ("derived_objects", 1.5, derived_items),
                # Beside the scope routes, never above them: a thread record is
                # ordinary canonical evidence that arrived by a different key, so
                # it fuses at the canonical lane's own weight.
                ("entity_thread", 1.0, entity_thread_items),
                # Q1. Same argument, same weight: evidence for a stated goal is
                # ordinary canonical evidence that arrived keyed to that goal's ids.
                # It is a joined minority lane beside the scope routes, never above
                # them — the mode earns its answer by ATTRIBUTION, not by outranking.
                ("commitment_evidence", 1.0, commitment_items),
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
            rare_token_groups=rare_groups,
            now=now,
            min_per_source=min_per,
            ledger=ledger,
        )

    # Legacy path (TOPOS_FUSION_RRF=off): incomparable absolute scores.
    for item in entity_items + fact_store_items + stat_items:
        item.setdefault("relevance_score", 0.9)
    items = (
        stat_items
        + fact_store_items
        + entity_items
        + derived_items
        + entity_thread_items
        + commitment_items
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


_SAFE_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _scope_stores_are_empty(conn: Optional[Any], manifest: ScopeResolutionManifest) -> Optional[bool]:
    """Do the canonical tables backing this scope hold ANY row at all?

    The difference between "you had a quiet week" and "nothing has ever synced here"
    — on this node `calendar_events` and `financial_transactions` both hold zero rows,
    and their empty answer was worded identically to a genuine absence of activity.
    One `SELECT 1 … LIMIT 1` per table, run ONLY when a result already came back
    empty and only when a ledger asked, so no existing caller pays for it.

    `None` when it cannot be determined (no connection, no tables, a failed probe) —
    an unknown must not masquerade as either answer.
    """
    tables = [t for t in (manifest.canonical_tables or []) if _SAFE_TABLE_RE.match(str(t or ""))]
    if conn is None or not tables:
        return None
    seen_any = False
    for table in tables:
        try:
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        except Exception as exc:  # noqa: BLE001 — a missing table is not an error here
            logger.debug("store-empty probe skipped for %s: %s", table, exc)
            continue
        seen_any = True
        if row is not None:
            return False
    return False if not seen_any else True


#: Why a scope holds nothing. `store_empty` already separates "nothing has ever been
#: stored" from "you had a quiet week"; these separate the three ways the first happens,
#: because they have three different remedies and only one of them is the owner's to act on.
SUPPLY_NO_SOURCE = "no_source_connected"      # nothing installed feeds this scope
SUPPLY_NEVER_DELIVERED = "connected_never_delivered"  # a feed exists, it has never landed
SUPPLY_DELIVERED_THEN_EMPTIED = "delivered_then_emptied"  # it landed once; the tables are bare now


def _scope_supply_state(
    conn: Optional[Any],
    manifest: ScopeResolutionManifest,
    installed_source_ids: Optional[List[str]] = None,
) -> Optional[str]:
    """WHY does this scope hold nothing? Only meaningful once emptiness is established.

    "Your calendar is empty" is true and useless. The owner needs to know whether to
    connect Google Calendar, wait for a first sync, or go looking for what deleted the
    rows — three different actions behind one identical answer today (doc gap G4, Q6).

    Read from `scope_source_generation`, which records per (scope, source) that data
    actually landed, cross-checked against what this node has installed. `None` when it
    cannot be told — an unknown must not be dressed up as a diagnosis.
    """
    if conn is None:
        return None
    scope_id = str(getattr(manifest, "scope_id", "") or "").strip()
    if not scope_id:
        return None
    try:
        delivered = conn.execute(
            "SELECT 1 FROM scope_source_generation WHERE scope_id = ? AND generation > 0 LIMIT 1",
            (scope_id,),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 — an older DB without the table is not an error
        logger.debug("supply-state probe skipped for %s: %s", scope_id, exc)
        return None
    if delivered is not None:
        # Rows arrived at some point and the canonical tables are bare now. Never
        # silently call this "not connected" — that sends the owner to re-add a
        # connector that is working.
        return SUPPLY_DELIVERED_THEN_EMPTIED
    # `manifest.default_source_ids` is NOT the feeding list — it is a narrower default,
    # and using it said "no source connected" for a scope with a calendar connector
    # installed. `get_sources_by_scope` is the authority: static registry plus active
    # runtime installs, the same mapping the catalog reports to clients.
    try:
        from ..sources.registry import get_sources_by_scope

        feeds = {str(s).strip() for s in get_sources_by_scope(scope_id) if str(s or "").strip()}
    except Exception as exc:  # noqa: BLE001 — a registry read failure is not a diagnosis
        logger.debug("supply-state feed lookup skipped for %s: %s", scope_id, exc)
        return None
    if not feeds:
        return SUPPLY_NO_SOURCE
    installed = {str(s).strip() for s in (installed_source_ids or []) if str(s or "").strip()}
    if not installed:
        # We know what could feed this scope but not what this node installed. Claiming
        # "nothing connected" from that is a guess, and the owner acts on it.
        return None
    return SUPPLY_NEVER_DELIVERED if (feeds & installed) else SUPPLY_NO_SOURCE


def routing_supply_states(
    installed_source_ids: Optional[List[str]],
    scope_ids: Optional[List[str]] = None,
) -> Dict[str, str]:
    """The pre-query half of the supply state: which scopes cannot return a row at all.

    `_scope_supply_state` explains an emptiness that already happened. A router has to
    decide BEFORE it spends one of its four route slots, so it needs the same question
    answered without a query and without a data read — from the registry and the
    installed set alone.

    Deliberately narrower than `_scope_supply_state` in two ways, because this one is
    allowed to make a request retrieve LESS:

    - Only `no_source_connected` is ever reported. `connected_never_delivered` and
      `delivered_then_emptied` describe connected feeds whose store is empty — a real
      empty answer that must still be queried. A router-facing map carrying them would
      invite exactly the mistake the three sub-causes exist to prevent.
    - A scope with no feeds at all in the registry is left OUT of the map rather than
      called unsupplied. The derived scopes (`attention:read`, `complexity:read`) are
      computed on-node and register no feeding source, so "no feeds" here means "not
      knowable from the registry", and an unknown must never cost a route.

    `installed_source_ids` empty or None returns `{}` — the same guard
    `_scope_supply_state` uses, for the same reason: not being told what is installed
    is not evidence that nothing is.
    """
    installed = {str(s).strip() for s in (installed_source_ids or []) if str(s or "").strip()}
    if not installed:
        return {}
    try:
        from ..sources.registry import get_sources_by_scope

        if scope_ids is None:
            from .scope_registry_loader import list_scopes

            wanted = [str(entry.get("scope_id") or "").strip() for entry in list_scopes()]
        else:
            wanted = [str(s).strip() for s in scope_ids]
    except Exception as exc:  # noqa: BLE001 — a registry read failure is not a diagnosis
        logger.debug("routing supply-state lookup skipped: %s", exc)
        return {}
    out: Dict[str, str] = {}
    for scope_id in wanted:
        if not scope_id:
            continue
        try:
            feeds = {str(s).strip() for s in get_sources_by_scope(scope_id) if str(s or "").strip()}
        except Exception as exc:  # noqa: BLE001
            logger.debug("routing supply-state feeds skipped for %s: %s", scope_id, exc)
            continue
        if not feeds:
            continue
        if not (feeds & installed):
            out[scope_id] = SUPPLY_NO_SOURCE
    return out


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

        # This bundle returns from `retrieve` BEFORE the exclusion plane at the foot
        # of the method, so "…but nothing from X" used to leave no trace at all here:
        # not enforced, and not reported as un-enforced either. That silence is the
        # exact false-claim-of-enforcement shape `exclusion.py` exists to prevent —
        # the caller cannot tell an honoured exclusion from a skipped one.
        #
        # It is not routed through the item filter, because there is no item. The
        # packet holds ONE derived count over cohort membership, computed above
        # before any row existed; the filter would match nothing, report
        # `enforced=True, dropped=0`, and leave `person_count` still counting the
        # excluded members. So the plane is told this is aggregate-only and records
        # `not_applied` honestly instead.
        exclusion_block = _enforce_request_exclusions(
            packet,
            query_text=str(request.query_text or "").strip(),
            conn=db_conn,
            ledger=getattr(request, "ledger", None),
            aggregate_only=True,
        )
        if exclusion_block:
            packet["exclusion"] = exclusion_block
            retrieval_meta.update(_exclusion_meta(exclusion_block))

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
        # What the rare gate treats as discriminative needles. Defaults to the owner's
        # words; a caller that can phrase the SUBJECT better than the request does (home
        # chat, which holds the raw prompt AND the distilled version) may narrow it. The
        # planner, the embeddings and the classifier below deliberately keep query_text.
        needle_text = str(request.needle_text or "").strip() or query_text
        # The same needles PER PART. Empty (every caller that does not opt in) →
        # `_needle_token_groups` yields the single whole-request group, so the gates
        # below behave exactly as they did before multi-needle.
        needle_parts = [
            str(p or "").strip()
            for p in (getattr(request, "needle_parts", None) or [])
            if str(p or "").strip()
        ]
        needle_groups = _needle_token_groups(needle_text, needle_parts)
        #: Optional narrowing ledger — mutated in place by the stages below. `None`
        #: (every caller that does not opt in) leaves this method byte-identical.
        ledger = request.ledger
        if request.skip_retrieval:
            self._last_stores = []
            if ledger is not None:
                ledger.empty(
                    _N.CAUSE_NOT_QUERIED,
                    stage=_N.STAGE_RETRIEVAL,
                    reason="skip_retrieval_requested",
                )
            return RetrievalBundle(context_packet={}, stores_touched=[], record_counts={})

        if not _mode_allowed(request.access_mode, manifest.access_mode_ceiling):
            if ledger is not None:
                ledger.empty(
                    _N.CAUSE_SCOPE_DENIED,
                    stage=_N.STAGE_GRANT,
                    reason="mode_ceiling_exceeded",
                )
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
            if ledger is not None:
                # The empty is indistinguishable from absence BY DESIGN (CQE). The
                # cause is recorded for the node's own audit trail, and the public
                # ledger carries the same closed-set enum a genuine denial does.
                ledger.empty(
                    _N.CAUSE_SCOPE_DENIED,
                    stage=_N.STAGE_GRANT,
                    reason="selector_suppressed",
                )
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

        # Q7: the entity-thread lane's tap. Filled during retrieval, drained after the
        # exclusion filter below — never before, so the thread can only ever describe
        # rows that survived every plane. Declared at function scope because the drain
        # is common to all three modes even though only `summary` fills it.
        thread_sink: Dict[str, Any] = {}

        # Q1: the commitment lane's tap, on the same contract and for the same reason.
        # It holds one state per stated goal — the goal's own ids, the evidence rows the
        # per-goal join REACHED, and any refusal that stopped the join before it ran.
        # Reached is not answered: the drain below intersects it with the surviving
        # summaries, so a row the exclusion filter or the black hole removed cannot be
        # cited as progress.
        commitment_sink: Dict[str, Any] = {}

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
                    if ledger is not None and plan.time_range:
                        # The one stage that already reported its narrowing. It now
                        # reports it in the same shape as the other seven.
                        ledger.record(
                            _N.STAGE_PLANNER,
                            "scoped",
                            "time_range_parsed",
                            detail={"time_range": list(plan.time_range)},
                        )
            except Exception as exc:
                logger.debug("query planner skipped: %s", exc)

        # Q3: the window the sentence did not name. Only when the planner found no
        # anchor of its own — an explicit range, a relative phrase, a differenced pair
        # and a point-in-time "in July" all already say which period the owner means,
        # and this mode may not overrule a period they stated. It writes into
        # `plan.time_range`, so every existing consumer (the lanes,
        # `_prefer_time_window`, R2's labelling, the packet's own `time_window` block)
        # sees it through the path it already uses rather than through a second window
        # that only some of them know about.
        derived_window: Optional[DerivedWindow] = None
        if (
            plan is not None
            and not plan.time_range
            and not plan.as_of
            and entity_anchor_intent(query_text)
        ):
            try:
                derived_window = _derive_entity_anchored_window(
                    manifest=manifest,
                    conn=getattr(self._adapters.signal, "_conn", None),
                    query_text=query_text,
                    source_ids=source_ids,
                    disclosure_tier=request.disclosure_tier,
                    ledger=ledger,
                )
            except Exception as exc:  # noqa: BLE001 — a derived window is an extra
                logger.debug("entity-anchored window skipped: %s", exc)
            if derived_window is not None:
                retrieval_meta["entity_window"] = derived_window.as_packet()
                if derived_window.resolved:
                    plan.time_range = derived_window.time_range()
                    retrieval_meta["query_plan"] = plan.to_meta()

        time_range = plan.time_range if plan else None
        # The fourth text. The planner strips TIME framing ("this week") and leaves
        # instructional framing alone, so a structured request embeds its own
        # instructions: measured 2026-08-18, the weekly-report prompt embedded all 315
        # characters of "generate a personal work report … summarize achievements …
        # with any adjustments made", which is a vector query for the shape of a
        # request rather than its subject.
        #
        # `needle_text` is only ever sent when distillation actually removed something,
        # so its presence is the caller saying "this request carried bulk". Absent — a
        # plain question — nothing changes, and the sentence still reaches the encoder,
        # which is what 2026-08-16 measured that it needs.
        semantic_query = query_text
        if needle_text and needle_text != query_text:
            semantic_query = needle_text
            if ledger is not None:
                ledger.record(
                    stage=_N.STAGE_RETRIEVAL,
                    action="rewrote",
                    reason="embedded_subject_not_instruction",
                )
        elif plan and plan.semantic_residual and len(plan.semantic_residual) >= 6:
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

        # The derived layer's own lane, searched SEPARATELY rather than filtered
        # out of the shared result set. Both live in `signal_embeddings`, so one
        # search would make them compete for one top-N — and there are ~350
        # derived rows against ~9,000 raw ones, so the lane that exists to
        # answer "who is close to me" would be starved by whichever messages
        # happened to contain the word "close". Their own `source_id` gives them
        # their own budget; the query embedding is already cached, so the second
        # call costs a filtered scan and no inference.
        derived_hits: List[Dict[str, Any]] = []
        if query_text and global_layers_apply and request.access_mode in ("summary", "inference"):
            from ..features.signal.derived_index import (
                DERIVED_SOURCE_ID,
                is_derived_record_type,
            )
            from ..features.signal.vector_settings import derived_object_index_enabled

            # Raw is raw: `packet["semantic_hits"]` promises dated source rows a
            # consumer can follow back to a connector, and a derived summary is
            # not one. Nothing should reach here (the derived rows carry their
            # own source_id) — this holds if a future writer forgets that.
            semantic_hits = [
                h for h in semantic_hits if not is_derived_record_type(h.get("record_type"))
            ]
            # NOT `semantic_query`. That is the residual — the query minus the
            # spans the entity and time planes already claimed — and the derived
            # lane is the one lane those spans are the CONTENT of. Measured on
            # this corpus: "who are my parents" links `parents` to a junk
            # 0-mention dossier ("Parents kinfolk"), leaving the residual "who
            # are my", which retrieves generic acquaintances while
            # "dad is my parent" sits at rank 0 of the full-text search.
            # `needle_text` still applies — stripping a bulk instruction is a
            # different operation from stripping the subject.
            derived_query = (
                needle_text if (needle_text and needle_text != query_text) else query_text
            )
            derived_error = None
            if derived_object_index_enabled():
                derived_hits, derived_error = _semantic_hits(
                    derived_query, source_id=DERIVED_SOURCE_ID
                )
            derived_hits = [h for h in derived_hits if is_derived_record_type(h.get("record_type"))]
            if derived_hits and str(request.disclosure_tier or "") != "owner_raw":
                # OWNER TIER ONLY, and not because `_fact_disclosure_allowed`
                # says so — measured 2026-08-26, it says the opposite. A
                # RelationshipEdge maps to the `relationship_edges` grant, which
                # `relationship_context:read` declares, so a grantee at
                # `default_disclosure` asking "who's in my close circle" got the
                # owner's mother, grandmother and closest friends BY NAME. The
                # grantee scrub does not save it: `_redact_pii` removes emails
                # and phone numbers, never names.
                #
                # That declaration was written when nothing emitted names from
                # that store — it granted bands and counts, and this lane is the
                # first thing that would have turned it into a roster. Widening
                # a grant as a side effect of adding a lane is not a decision
                # this change gets to make. The rest of the node already holds
                # this line (`_build_cohort_aggregate_summary`: "Individual
                # people are not disclosed in this rollup").
                #
                # A grantee-facing derived lane is a real thing to want. It
                # needs an aggregate rendering (bands, counts, no names) and its
                # own review, not this filter relaxed.
                #
                # Debug, not ledger: on the leaking direction a stamped receipt
                # is itself the disclosure (see the note in
                # `_build_summary_items`), and the owner never loses a row here.
                logger.debug(
                    "derived-object lane withheld from tier %s (%d item(s))",
                    request.disclosure_tier,
                    len(derived_hits),
                )
                derived_hits = []
            elif derived_hits:
                # Owner tier still honours each object type's own declared
                # grant, so a future relaxation of the line above cannot let a
                # stat-insights grant unlock the relationship graph.
                derived_hits = [
                    h
                    for h in derived_hits
                    if _fact_disclosure_allowed(h, request.disclosure_tier, manifest)
                ]
            if derived_hits:
                touched.append("derived_objects")
                retrieval_meta["derived_objects_returned"] = len(derived_hits)
                retrieval_meta["retrieval_strategy"] = "query_aware"
            elif derived_error:
                logger.debug("derived-object search unavailable: %s", derived_error)

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
            raw_rare_groups: Optional[List[Dict[str, int]]] = None
            if query_text:
                try:
                    raw_conn_for_df = getattr(self._adapters.signal, "_conn", None)
                    raw_rare_tokens = _rare_tokens(
                        raw_conn_for_df, _residual_content_tokens(_query_tokens(needle_text))
                    )
                    raw_rare_groups = _rare_token_groups(raw_conn_for_df, needle_groups)
                except Exception:
                    raw_rare_tokens = []
                    raw_rare_groups = None
            truncated_tables: List[str] = []
            for table in manifest.canonical_tables:
                table_rows = _route_canonical_rows(
                    self._adapters,
                    table,
                    manifest=manifest,
                    query_text=query_text,
                    source_ids=source_ids,
                    limit=CANONICAL_ROW_CAP + 1,
                    disclosure_tier=request.disclosure_tier,
                    rare_query_tokens=raw_rare_tokens,
                    rare_query_token_groups=raw_rare_groups,
                )
                # Truncation decided BEFORE any downstream filtering: the cap applies
                # to what the store handed back, and a later filter narrowing 101 rows
                # to 3 does not make the underlying read complete.
                if len(table_rows) > CANONICAL_ROW_CAP:
                    truncated_tables.append(table)
                    table_rows = table_rows[:CANONICAL_ROW_CAP]
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
                    if len(table_rows) > max_rows and table not in truncated_tables:
                        truncated_tables.append(table)
                    table_rows = table_rows[:max_rows]
                counts[table] = len(table_rows)
                for row in table_rows:
                    rows.append({"_table": table, **row})
            packet["rows"] = _strip_forbidden(rows, manifest.must_not_retrieve)
            if truncated_tables:
                # Rides inside public_result, the pattern the field contract already
                # documents for empty_cause: a nested field survives every seam
                # without widening required_return.
                packet["truncated"] = {
                    "row_cap": CANONICAL_ROW_CAP,
                    "tables": sorted(truncated_tables),
                    "note": (
                        "More rows exist than were returned. An absence in this result "
                        "is NOT evidence the thing does not exist."
                    ),
                }
                if ledger is not None:
                    ledger.record(_N.STAGE_RETRIEVAL, "capped", "row_cap_reached")
        elif mode == "summary":
            # Query-aware building only applies to actual queries. Ranked
            # clusters load even for browse requests (no query_text), and
            # rerouting those through the query builder starved the dimension
            # summaries lane entirely (p3 regression: summaries=[] and the
            # signal store never touched on browse-mode summary reads).
            if query_text:
                summaries = _build_summary_items(
                    needle_text=needle_text,
                    needle_parts=needle_parts,
                    manifest=manifest,
                    adapters=self._adapters,
                    query_text=query_text,
                    semantic_hits=semantic_hits,
                    ranked_clusters=ranked_clusters,
                    derived_hits=derived_hits,
                    installed_source_ids=request.installed_source_ids,
                    disclosure_tier=request.disclosure_tier,
                    plan=plan,
                    now=plan_now,
                    ledger=ledger,
                    thread_sink=thread_sink,
                    commitment_sink=commitment_sink,
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
                    if derived_window is not None and derived_window.resolved:
                        # Q3: say the dates came from the DATA and show the arithmetic
                        # that produced them. A derived window the owner cannot see is
                        # a window they cannot correct, and a wrong one would then
                        # silently shape every lane in the turn — "I think you meant
                        # Aug 4-11" is only a possible sentence if Aug 4-11 is shown.
                        window.update(derived_window.as_packet())
                    # R2: a differenced ask names both sides, and each item says which
                    # side it evidences. Synthesis can then difference them instead of
                    # inferring the split from timestamps it was never told the meaning
                    # of. `from`/`to` above stay the union span, so a consumer that
                    # ignores `windows` sees the period that was actually searched.
                    planned_windows = list(getattr(plan, "time_windows", None) or [])
                    if len(planned_windows) >= 2:
                        from .planner import WINDOW_BASELINE, WINDOW_CURRENT

                        labelled = _label_time_windows(summaries, planned_windows)
                        window["comparison"] = True
                        window["windows"] = [
                            {"label": label, "from": bounds[0], "to": bounds[1]}
                            for label, bounds in zip(
                                (WINDOW_BASELINE, WINDOW_CURRENT), planned_windows
                            )
                        ]
                        retrieval_meta["comparison_windows"] = len(window["windows"])
                        retrieval_meta["comparison_items_labelled"] = labelled
                        if ledger is not None:
                            ledger.record(
                                _N.STAGE_PLANNER,
                                "scoped",
                                "multi_window_comparison",
                                detail={"windows": [list(w) for w in planned_windows]},
                            )
                    packet["time_window"] = window
                elif derived_window is not None:
                    # Q3's refusal, published in the same place the window would have
                    # been. An entity with three scattered mentions has no heads-down
                    # period, and the honest answer is that no window could be derived
                    # — which the caller has to be TOLD, or the turn reads as an
                    # ordinary unwindowed search and the refusal becomes invisible.
                    packet["time_window"] = derived_window.as_packet()
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
                # Q3: the existing triage, run inside the derived window. The digests
                # are the ones this scope always serves, computed by
                # `features/triage/daily.py` off `triage_verdicts`; the window only
                # decides which days of them answer. Nothing about the triage is
                # recomputed here, which is the point — a second scoring path would
                # be a second set of verdicts to disagree with the first.
                #
                # The window goes to the LOADER, not to a filter over the newest page:
                # asking for five days and keeping the ones that happen to fall inside
                # a six-day window answers a different question than asking for the six.
                attention_conn = getattr(self._adapters.signal, "_conn", None)
                attention_items = _load_attention_summary_items(
                    attention_conn, window=derived_window)
                # Still run, and still the authority on what counts as in-window: the
                # loader keeps undated keys deliberately, and this is what decides them.
                # `out_of_window` is expected to be 0 now that the days are selected
                # in SQL; a non-zero value means the predicate and the filter disagreed,
                # and `withheld` below counts it either way (it is held minus served).
                attention_items, out_of_window = _attention_items_in_window(
                    attention_items, derived_window
                )
                if attention_items:
                    summaries = attention_items + list(summaries)
                    touched.append("signal")
                if ledger is not None and derived_window is not None and derived_window.resolved:
                    # What the window actually cost, counted against every digest the
                    # node holds rather than against one page of them. Reporting the
                    # post-filter drop instead would now report ~0 on every windowed
                    # turn — the narrowing did not stop happening when it moved into
                    # the query, it stopped being visible from where it used to be read.
                    withheld = max(
                        0,
                        _count_attention_summary_items(attention_conn) - len(attention_items),
                    )
                    if attention_items and withheld:
                        ledger.record(
                            _N.STAGE_RETRIEVAL,
                            "windowed",
                            "entity_window_triage_lane",
                            dropped=withheld,
                        )
                    elif not attention_items and withheld:
                        # The window was derived and the triage has nothing in it. That is
                        # a different empty from "this node runs no triage", and only this
                        # line can tell them apart.
                        ledger.empty(
                            _N.CAUSE_NO_MATCH,
                            stage=_N.STAGE_RETRIEVAL,
                            reason="entity_window_no_triage_in_window",
                            dropped=withheld,
                        )
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
            #
            # A scope's declared `must_not_retrieve` bound one mode out of three.
            # `raw` applied it to its rows and `inference` to the whole packet;
            # summary — the mode most scopes actually answer in — never applied it
            # at all. `availability:read` is the live case: it declares
            # `calendar_events.title`, `conversation_messages.content` and
            # `content`, its ceiling is `inference`, and `summary` outranks that
            # ceiling, so the reachable mode was the unenforced one. Applied to
            # the whole packet, exactly as inference does, so the restriction
            # covers the lanes that grow later as well as the ones here today.
            packet = _strip_forbidden(packet, manifest.must_not_retrieve)
        elif mode == "inference":
            scores: List[Dict[str, Any]] = []
            inference_rare: List[str] = []
            inference_rare_groups: Optional[List[Dict[str, int]]] = None
            if query_text:
                try:
                    inference_rare = _rare_tokens(
                        getattr(self._adapters.signal, "_conn", None),
                        _residual_content_tokens(_query_tokens(needle_text)),
                    )
                    inference_rare_groups = _rare_token_groups(
                        getattr(self._adapters.signal, "_conn", None), needle_groups
                    )
                except Exception:
                    inference_rare = []
                    inference_rare_groups = None
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
                rare_query_token_groups=inference_rare_groups,
                browse_fallback=True,
                plan=plan,
                conn=getattr(self._adapters.signal, "_conn", None),
                exposure_visible=_exp_visible,
                ledger=ledger,
            )
            for item in canon_items:
                scores.append({k: v for k, v in item.items() if k not in _INFERENCE_CANONICAL_EXCLUDED_KEYS})
            # Packet resolution (owner opt-in, floors already applied by the pipeline):
            # at 'facts'/'facts_all' the inference packet carries fact CONTENT. Facts are
            # DERIVED items — the raw-text exclusions above are about canonical rows and
            # do not apply; what gates here is the owner's setting and the sensitivity
            # class. At 'scores_only' this block is skipped and the packet is
            # byte-compatible with the pre-feature behavior.
            if request.packet_resolution in ("facts", "facts_all"):
                try:
                    from ..features.entities.linking import link_query_entities

                    _fact_conn = getattr(self._adapters.signal, "_conn", None)
                    _linked = link_query_entities(_fact_conn, query_text or "") if _fact_conn else []
                    fact_items = _load_fact_store_items(
                        _fact_conn,
                        query_text or "",
                        _linked,
                        disclosure_tier=request.disclosure_tier,
                        manifest=manifest,
                        temporal_shift=getattr(plan, "temporal_shift", None) if plan else None,
                        as_of=getattr(plan, "as_of", None) if plan else None,
                        include_packet_fields=True,
                    )
                    for item in fact_items:
                        if request.packet_resolution != "facts_all" and str(
                            item.get("sensitivity") or ""
                        ) == "special":
                            continue  # special-class needs the explicit facts_all step
                        scores.append(dict(item))
                    if fact_items:
                        touched.append("facts_store")
                except Exception:  # noqa: BLE001 — the facts lane must never break a turn
                    pass
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
                # Same window as the summary path. An inference packet built from the
                # newest five days while the plan's time range says otherwise is a
                # score for a week the owner did not ask about.
                for item in _load_attention_summary_items(
                        getattr(self._adapters.signal, "_conn", None),
                        window=derived_window):
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

        # ENFORCED EXCLUSION. "…but nothing from the therapy journal" is compiled to
        # category / tier / entity filters and applied HERE, inside the retrieval
        # boundary — not as a line in the synthesis prompt. A model asked nicely not
        # to mention something has already been shown it; this drops the content
        # before the disclosure filter, the game layer, the stored artifact and the
        # prompt ever see it, on the same plane the tiers and the black-hole policy
        # already run. An exclusion that cannot be compiled is reported as NOT
        # applied rather than passed through as though it had been honoured.
        exclusion_block = _enforce_request_exclusions(
            packet,
            query_text=query_text,
            conn=getattr(self._adapters.signal, "_conn", None),
            ledger=ledger,
        )
        if exclusion_block:
            packet["exclusion"] = exclusion_block
            retrieval_meta.update(_exclusion_meta(exclusion_block))

        # Q7. LAST, deliberately. The thread is a projection of `packet["summaries"]` and
        # is assembled only once nothing further can be removed from them — so the fusion
        # cap, the exit black hole and the exclusions above all subtract from the thread
        # without it having to know they ran. `_strip_forbidden` has already applied the
        # scope's `must_not_retrieve` to the summaries this reads, and the thread carries
        # ids, timestamps and closed-set labels rather than row content, so there is no
        # forbidden column for it to reintroduce.
        _attach_topic_thread(
            packet,
            thread_sink,
            conn=getattr(self._adapters.signal, "_conn", None),
            disclosure_tier=request.disclosure_tier,
            manifest=manifest,
            ledger=ledger,
        )
        if packet.get("topic_thread"):
            retrieval_meta["topic_thread_items"] = len(packet["topic_thread"]["items"])
            retrieval_meta["topic_thread_cross_source"] = bool(
                packet["topic_thread"]["cross_source"]
            )

        # Q1. Beside Q7 and after it, for the same reason and one more. The per-goal
        # report cites record ids as PROOF OF PROGRESS, so every id in it must be an id
        # that survived — the fusion cap, the exit black hole and the exclusions above
        # all have to have finished subtracting before a single citation is written.
        # Draining any earlier would let the mode assert, with an id attached, that the
        # owner did something on evidence the owner's own request removed.
        _attach_commitment_report(
            packet,
            commitment_sink,
            conn=getattr(self._adapters.signal, "_conn", None),
            adapters=self._adapters,
            manifest=manifest,
            disclosure_tier=request.disclosure_tier,
            installed_source_ids=request.installed_source_ids,
            ledger=ledger,
        )
        if packet.get("commitment_report"):
            report = packet["commitment_report"]
            retrieval_meta["commitment_goals"] = int(report.get("goal_count") or 0)
            retrieval_meta["commitment_goals_with_evidence"] = int(
                report.get("goals_with_evidence") or 0
            )

        # Why is this lane empty? Four causes had one message, and two of six
        # sections in a real report chose the wrong one out loud. Anything a stage
        # above already attributed (a gate veto, a denial) wins — this is the
        # fallback for "we looked and there was nothing", which still splits two
        # ways: nothing has ever been stored here, or nothing matched.
        if ledger is not None and _N.result_is_empty(packet):
            if ledger.empty_cause is None:
                stores_empty = _scope_stores_are_empty(
                    getattr(self._adapters.signal, "_conn", None), manifest
                )
                if stores_empty is True:
                    # The remedy differs per case, so the cause carries which one.
                    ledger.empty(
                        _N.CAUSE_STORE_EMPTY,
                        stage=_N.STAGE_RETRIEVAL,
                        reason=_scope_supply_state(
                            getattr(self._adapters.signal, "_conn", None),
                            manifest,
                            request.installed_source_ids,
                        )
                        or "scope_stores_hold_no_rows",
                    )
                else:
                    ledger.empty(
                        _N.CAUSE_NO_MATCH,
                        stage=_N.STAGE_RETRIEVAL,
                        reason="no_row_matched_the_request",
                    )
            retrieval_meta["empty_cause"] = ledger.empty_cause

        self._last_stores = sorted(set(touched))
        return RetrievalBundle(
            context_packet=packet,
            stores_touched=self._last_stores,
            record_counts=counts,
            retrieval_metadata=retrieval_meta,
        )


# Protocol alias for imports
SignalRetrievalAdapter = DefaultSignalRetrievalAdapter
