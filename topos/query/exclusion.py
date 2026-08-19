"""Enforced exclusion — the owner's "but not that" compiled into a retrieval filter.

The product ships exclusion as a headline capability: *"everything about my week,
but nothing from the therapy journal"*, *"exclude anything involving Sarah"*. Until
now there was nowhere in the pipeline where that sentence became a decision. The
only place it could plausibly have gone is the synthesis prompt — and an
instruction not to mention something is not an enforcement mechanism. The model
sees the journal entry, is asked nicely to ignore it, and the entry is one
paraphrase away from the answer. Worse, it has already entered the context packet,
the audit trail and the artifact cache by then.

So exclusion lands where the disclosure plane already is: **retrieval**. The
existing tiers, the black-hole guard and ``_strip_forbidden`` all run before a
packet is handed to the game layer, and this joins them rather than building a
second enforcement plane beside them. Excluded content is gone before synthesis,
before the disclosure filter, before the artifact is cached.

Four rules, each a test.

* **Closed vocabulary.** A fragment compiles to a ``category`` (a slug over the
  scope registry's tables / dimensions / sources), a ``tier`` (a row-level content
  class that actually exists — today that is ``nsfw``), or an ``entity`` resolved
  through the entity plane. Nothing else is an exclusion.
* **Ambiguity is a refusal to guess.** A fragment that compiles to none of those
  is recorded as *not applied* and surfaced on the packet. A false claim of
  enforcement is worse than no enforcement, so the one thing this module may never
  do is drop the fragment silently and let the answer read as though the owner's
  exclusion was honoured.
* **No raw text leaves the node.** The public exclusion block and the ledger
  entries carry closed-set slugs and integers. A named entity is reported as the
  slug ``named_entity`` — the *fact* of an entity exclusion is public, the name is
  not. Fragments and names live in ``detail``/local-only fields.
* **Cache-safe by construction.** The exclusion is parsed from ``query_text``,
  which is already part of the intent hash (:func:`topos.query.intent.compute_intent_hash`).
  The same question with and without "but nothing from the journal" hashes
  differently, so an unexcluded artifact can never be replayed for an excluded ask.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import narrowing as _N

logger = logging.getLogger(__name__)

# --- target kinds -------------------------------------------------------------------
KIND_CATEGORY = "category"
KIND_TIER = "tier"
KIND_ENTITY = "entity"

#: What a named-entity exclusion is called in public output. The owner's contact's
#: name is the owner's data; the fact that *an* entity was excluded is not.
ENTITY_PUBLIC_SLUG = "named_entity"

# --- ledger reasons (closed set) ----------------------------------------------------
REASON_CATEGORY = "exclusion_category"
REASON_TIER = "exclusion_tier"
REASON_ENTITY = "exclusion_entity"
REASON_UNCOMPILABLE = "exclusion_uncompilable"
REASON_ENFORCED = "exclusion_enforced"
#: The fragment compiled fine — there was simply nothing item-shaped to apply it
#: to. See :func:`enforce_request_exclusions` (``aggregate_only``).
REASON_AGGREGATE = "exclusion_aggregate_unfilterable"

ACTION_EXCLUDED = "excluded"
ACTION_NOT_APPLIED = "not_applied"

_REASON_BY_KIND = {
    KIND_CATEGORY: REASON_CATEGORY,
    KIND_TIER: REASON_TIER,
    KIND_ENTITY: REASON_ENTITY,
}


@dataclass(frozen=True)
class _Category:
    """One category slug and the row shapes it covers.

    Matchers are deliberately over-inclusive within a category and never across
    one: excluding ``journal`` must take every journal lane (the canonical table,
    the journal source ids, a summary whose ``retrieval_source`` says journal), and
    must take nothing else.
    """

    slug: str
    tables: Tuple[str, ...] = ()
    dimensions: Tuple[str, ...] = ()
    #: Substrings matched against source-ish fields (`source_id`, `retrieval_source`).
    source_terms: Tuple[str, ...] = ()


#: The closed category set. Every slug here is enforceable against a retrieved item;
#: adding one means adding matchers, not just a word.
CATEGORIES: Tuple[_Category, ...] = (
    _Category(
        "journal",
        tables=("journal_entries",),
        source_terms=("journal", "diary"),
    ),
    _Category(
        "messages",
        tables=("conversation_messages",),
        source_terms=("imessage", "messenger", "signal", "whatsapp", "sms"),
    ),
    _Category(
        "ai_chats",
        tables=("ai_chat_messages",),
        dimensions=("memory",),
        source_terms=("chatgpt", "claude", "ai_chat"),
    ),
    _Category(
        "calendar",
        tables=("calendar_events",),
        dimensions=("time",),
        source_terms=("calendar",),
    ),
    _Category(
        "finance",
        tables=("financial_transactions",),
        dimensions=("resources",),
        source_terms=("financial", "finance", "bank"),
    ),
    _Category("health", dimensions=("wellbeing",), source_terms=("health", "wellbeing")),
    _Category(
        "browsing",
        tables=("activity_events",),
        dimensions=("interests",),
        source_terms=("browser", "browsing"),
    ),
    _Category(
        "location",
        tables=("location_events",),
        dimensions=("places",),
        source_terms=("places", "location"),
    ),
    _Category(
        "contacts",
        tables=("contacts", "contact_identifiers"),
        dimensions=("relationships",),
        source_terms=("contacts", "address_book"),
    ),
    _Category("work", dimensions=("work",), source_terms=("work",)),
    _Category("profile", dimensions=("profile",), source_terms=("resume", "profile")),
)

_CATEGORY_BY_SLUG = {c.slug: c for c in CATEGORIES}

#: Prose → category. Longest phrase wins, so "therapy journal" is the journal and a
#: bare "therapy" is health. Keys are normalized (lower, single-spaced) fragments.
_CATEGORY_TRIGGERS: Dict[str, str] = {
    "therapy journal": "journal",
    "therapy notes": "journal",
    "private journal": "journal",
    "journal": "journal",
    "journals": "journal",
    "journalling": "journal",
    "journaling": "journal",
    "diary": "journal",
    "diaries": "journal",
    "messages": "messages",
    "message": "messages",
    "texts": "messages",
    "text messages": "messages",
    "dms": "messages",
    "imessage": "messages",
    "whatsapp": "messages",
    "ai chats": "ai_chats",
    "ai chat": "ai_chats",
    "ai conversations": "ai_chats",
    "chatgpt": "ai_chats",
    "llm chats": "ai_chats",
    "calendar": "calendar",
    "meetings": "calendar",
    "my calendar": "calendar",
    "calendar events": "calendar",
    "finances": "finance",
    "financial": "finance",
    "financials": "finance",
    "money": "finance",
    "spending": "finance",
    "transactions": "finance",
    "purchases": "finance",
    "bank": "finance",
    "health": "health",
    "health data": "health",
    "medical": "health",
    "wellbeing": "health",
    "therapy": "health",
    "browsing": "browsing",
    "browsing history": "browsing",
    "browser history": "browsing",
    "web history": "browsing",
    "location": "location",
    "locations": "location",
    "location history": "location",
    "places": "location",
    "whereabouts": "location",
    "contacts": "contacts",
    "address book": "contacts",
    "work": "work",
    "work context": "work",
    "profile": "profile",
    "resume": "profile",
    "cv": "profile",
}

#: Prose → tier. Only content classes with a real row-level signal live here; a tier
#: word with no enforceable flag must fail loud, not resolve to a no-op.
_TIER_TRIGGERS: Dict[str, str] = {
    "nsfw": "nsfw",
    "explicit": "nsfw",
    "adult": "nsfw",
    "adult content": "nsfw",
    "x rated": "nsfw",
    "not safe for work": "nsfw",
}

TIERS = frozenset(_TIER_TRIGGERS.values())

# --- parsing ------------------------------------------------------------------------

#: Negation leads. Each captures the rest of its clause; the tail is then split and
#: each fragment resolved independently. A bare "no X" is deliberately absent —
#: "any meetings with no attendees?" is a question, not an exclusion, and guessing
#: there would produce silent under-retrieval on ordinary asks.
_EXCLUSION_LEAD_RE = re.compile(
    r"(?:^|[,;.]|\s)(?:"
    r"but\s+(?:not|no|nothing|none|excluding|exclude|without|leave\s+out|omit)"
    r"|nothing\s+(?:from|about|in|involving|mentioning|related\s+to|to\s+do\s+with)"
    r"|(?:do\s*n[o']?t|don't|dont|do\s+not)\s+include"
    r"|(?:please\s+)?(?:exclude|excluding|omit|omitting|ignore|skip)"
    r"|(?:leave|leaving)\s+out"
    r"|(?:except|apart)\s+(?:for|from)"
    r"|except"
    r"|without"
    r")\s+",
    re.I,
)

#: Where an exclusion clause ends. Sentence punctuation, or a lead-in that starts a
#: new positive ask.
_CLAUSE_END_RE = re.compile(r"[,;.?!\n]|\bbut\b|\band then\b", re.I)

#: Split one exclusion tail into independent targets.
_FRAGMENT_SPLIT_RE = re.compile(r"\s+(?:and|or|nor)\s+|\s*[/&]\s*", re.I)

#: Filler stripped from a fragment before resolution. "anything involving Sarah" and
#: "the therapy journal" both have to reach a bare subject.
_FRAGMENT_FILLER = (
    "anything",
    "everything",
    "something",
    "any",
    "all",
    "stuff",
    "data",
    "content",
    "entries",
    "items",
    "records",
    "involving",
    "mentioning",
    "related to",
    "to do with",
    "about",
    "from",
    "in",
    "on",
    "with",
    "of",
    "my",
    "the",
    "a",
    "an",
    "that",
    "which",
    "is",
    "are",
)

_WS_RE = re.compile(r"\s+")


def _normalize_fragment(text: str) -> str:
    cleaned = _WS_RE.sub(" ", str(text or "").strip().lower())
    cleaned = re.sub(r"[^a-z0-9\s'&/-]+", " ", cleaned)
    return _WS_RE.sub(" ", cleaned).strip()


def _strip_filler(fragment: str) -> str:
    """Peel leading/trailing filler until a bare subject is left.

    Only the edges: a filler word in the middle ("nothing from the work journal")
    is part of the subject and removing it would change what compiles.
    """
    tokens = fragment.split()
    changed = True
    while changed and tokens:
        changed = False
        for filler in _FRAGMENT_FILLER:
            parts = filler.split()
            n = len(parts)
            if tokens[:n] == parts:
                tokens = tokens[n:]
                changed = True
                break
            if tokens[-n:] == parts:
                tokens = tokens[:-n]
                changed = True
                break
    return " ".join(tokens)


def _exclusion_fragments(query_text: str) -> List[str]:
    """Every subject the owner asked to be left out, one per returned string."""
    text = str(query_text or "")
    if not text.strip():
        return []
    out: List[str] = []
    for match in _EXCLUSION_LEAD_RE.finditer(text):
        tail = text[match.end():]
        end = _CLAUSE_END_RE.search(tail)
        clause = tail[: end.start()] if end else tail
        for raw in _FRAGMENT_SPLIT_RE.split(clause):
            fragment = _strip_filler(_normalize_fragment(raw))
            if fragment:
                out.append(fragment)
    return out


@dataclass(frozen=True)
class ExclusionTarget:
    """One compiled exclusion. ``slug`` is closed-set for categories and tiers."""

    kind: str
    slug: str
    #: Entity ids resolved by the entity plane (entity targets only).
    entity_ids: Tuple[str, ...] = ()
    #: Normalized names/aliases for the free-text scan (entity targets only). Local
    #: only — never serialized.
    terms: Tuple[str, ...] = ()

    def as_public(self) -> Dict[str, str]:
        slug = ENTITY_PUBLIC_SLUG if self.kind == KIND_ENTITY else self.slug
        return {"kind": self.kind, "slug": slug}


@dataclass(frozen=True)
class ExclusionSpec:
    """What the owner asked to be left out, and what could not be compiled."""

    targets: Tuple[ExclusionTarget, ...] = ()
    #: Fragments that resolved to nothing enforceable. LOCAL ONLY — these are the
    #: owner's own words. Their *count* is public; their content is not.
    unresolved: Tuple[str, ...] = ()

    @property
    def requested(self) -> bool:
        """True when the request carried an exclusion at all."""
        return bool(self.targets or self.unresolved)

    @property
    def enforceable(self) -> bool:
        return bool(self.targets)

    @property
    def fully_enforced(self) -> bool:
        """False when any fragment could not be compiled — the fail-loud condition."""
        return self.requested and not self.unresolved

    def as_public(self) -> Dict[str, Any]:
        return {
            "requested": self.requested,
            "enforced": self.fully_enforced,
            "applied": [t.as_public() for t in self.targets],
            "not_applied": len(self.unresolved),
        }


def parse_exclusions(query_text: str, conn: Optional[sqlite3.Connection] = None) -> ExclusionSpec:
    """Compile the request's prose exclusions into a closed, typed structure.

    ``conn`` is the entity plane. Without it, a fragment that is only resolvable as
    a named entity stays *unresolved* rather than being assumed harmless — the same
    fail-loud rule, applied to a missing dependency.
    """
    fragments = _exclusion_fragments(query_text)
    if not fragments:
        return ExclusionSpec()

    targets: List[ExclusionTarget] = []
    unresolved: List[str] = []
    seen: Set[Tuple[str, str]] = set()

    for fragment in fragments:
        target = _resolve_fragment(fragment, conn)
        if target is None:
            unresolved.append(fragment)
            continue
        key = (target.kind, target.slug)
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)

    return ExclusionSpec(targets=tuple(targets), unresolved=tuple(unresolved))


def _resolve_fragment(fragment: str, conn: Optional[sqlite3.Connection]) -> Optional[ExclusionTarget]:
    slug = _CATEGORY_TRIGGERS.get(fragment)
    if slug:
        return ExclusionTarget(kind=KIND_CATEGORY, slug=slug)
    tier = _TIER_TRIGGERS.get(fragment)
    if tier:
        return ExclusionTarget(kind=KIND_TIER, slug=tier)
    # Longest trigger contained in the fragment ("the old therapy journal").
    contained = [t for t in _CATEGORY_TRIGGERS if _phrase_in(t, fragment)]
    if contained:
        best = max(contained, key=len)
        return ExclusionTarget(kind=KIND_CATEGORY, slug=_CATEGORY_TRIGGERS[best])
    contained_tier = [t for t in _TIER_TRIGGERS if _phrase_in(t, fragment)]
    if contained_tier:
        best = max(contained_tier, key=len)
        return ExclusionTarget(kind=KIND_TIER, slug=_TIER_TRIGGERS[best])
    return _resolve_entity_fragment(fragment, conn)


def _phrase_in(phrase: str, fragment: str) -> bool:
    return f" {phrase} " in f" {fragment} "


def _resolve_entity_fragment(
    fragment: str, conn: Optional[sqlite3.Connection]
) -> Optional[ExclusionTarget]:
    """Resolve a fragment through the entity plane, or give up loudly.

    Only entities the plane already knows count. A name it cannot bind is NOT
    turned into a bare substring filter: that would silently drop rows on a common
    word and report the exclusion as enforced, which is the failure mode this whole
    module exists to prevent.
    """
    if conn is None or not fragment:
        return None
    try:
        from ..features.entities.linking import link_query_entities

        linked = link_query_entities(conn, fragment)
    except Exception as exc:  # noqa: BLE001 — a missing entity table is "unresolved"
        logger.debug("exclusion entity resolution unavailable: %s", exc)
        return None
    if not linked:
        return None
    ids: List[str] = []
    terms: List[str] = []
    for ent in linked:
        eid = str(ent.get("entity_id") or "").strip()
        if eid:
            ids.append(eid)
        for key in ("normalized_name", "canonical_name"):
            name = str(ent.get(key) or "").strip()
            if name:
                terms.append(_normalize_entity_term(name))
    terms = [t for t in dict.fromkeys(terms) if t]
    if not ids and not terms:
        return None
    return ExclusionTarget(
        kind=KIND_ENTITY,
        slug=_N._slug(fragment) or ENTITY_PUBLIC_SLUG,
        entity_ids=tuple(dict.fromkeys(ids)),
        terms=tuple(terms),
    )


def _normalize_entity_term(name: str) -> str:
    try:
        from ..features.lifecycle.blackhole import normalize_entity_name

        return normalize_entity_name(name)
    except Exception:  # noqa: BLE001
        return _normalize_fragment(name)


# --- application --------------------------------------------------------------------

#: Every list on a context packet that can carry retrieved content. Adding a lane to
#: the packet without adding it here is how content walks around this filter.
_ITEM_KEYS = ("rows", "summaries", "scores", "semantic_hits", "topic_clusters", "facts")

_TABLE_KEYS = ("_table", "table", "table_id", "canonical_table")
_SOURCE_KEYS = ("source_id", "retrieval_source", "source", "connector_id")
_DIMENSION_KEYS = ("dimension", "primary_dimension")
_RECORD_ID_KEYS = ("record_id", "message_id", "id", "canonical_record_id")
_ENTITY_ID_KEYS = ("entity_id", "subject_entity_id", "object_entity_id")
_TEXT_KEYS = (
    "content",
    "text",
    "body",
    "summary_text",
    "topic",
    "label",
    "title",
    "entity_text",
    "content_preview",
    "text_preview",
)


def _values(item: Dict[str, Any], keys: Sequence[str]) -> List[str]:
    out: List[str] = []
    for key in keys:
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            out.append(val.strip().lower())
    return out


def _matches_category(item: Dict[str, Any], category: _Category) -> bool:
    tables = _values(item, _TABLE_KEYS)
    if any(t in category.tables for t in tables):
        return True
    dims = _values(item, _DIMENSION_KEYS)
    if any(d in category.dimensions for d in dims):
        return True
    sources = _values(item, _SOURCE_KEYS)
    for src in sources:
        if any(term in src for term in category.source_terms):
            return True
    return False


def _record_ids_mentioning(
    conn: Optional[sqlite3.Connection], entity_ids: Sequence[str]
) -> Set[str]:
    """Canonical records linked to an entity via ``entity_mentions``.

    The same join :class:`BlackholeGuard` uses, for the same reason: a canonical row
    carries no entity id of its own, so the id half of the filter has to come from
    the mention table. The text scan below is the other half.
    """
    if conn is None or not entity_ids:
        return set()
    ids = sorted({str(e) for e in entity_ids if str(e or "").strip()})
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT record_id FROM entity_mentions WHERE entity_id IN ({placeholders})",
            ids,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — no mention table → text scan alone
        logger.debug("exclusion mention join unavailable: %s", exc)
        return set()
    return {str(r[0]) for r in rows if r and r[0]}


def _matches_entity(
    item: Dict[str, Any], target: ExclusionTarget, blocked_record_ids: Set[str]
) -> bool:
    for key in _ENTITY_ID_KEYS:
        val = item.get(key)
        if val is not None and str(val) in target.entity_ids:
            return True
    for key in _RECORD_ID_KEYS:
        val = item.get(key)
        if val is not None and str(val) in blocked_record_ids:
            return True
    if target.terms:
        for text in _values(item, _TEXT_KEYS):
            haystack = _normalize_entity_term(text)
            if haystack and any(term in haystack for term in target.terms):
                return True
        for ent in item.get("entities") or []:
            if isinstance(ent, dict):
                name = _normalize_entity_term(str(ent.get("name") or ent.get("canonical_name") or ""))
                if name and any(term in name for term in target.terms):
                    return True
                if str(ent.get("entity_id") or "") in target.entity_ids:
                    return True
            elif isinstance(ent, str):
                name = _normalize_entity_term(ent)
                if name and any(term in name for term in target.terms):
                    return True
    return False


def _matches_tier(item: Dict[str, Any], slug: str) -> bool:
    if slug == "nsfw":
        from ..disclosure.content_policy import is_record_nsfw

        return bool(is_record_nsfw(item))
    return False


@dataclass
class ExclusionOutcome:
    """What enforcement actually did. ``dropped_by_target`` is per closed-set slug."""

    spec: ExclusionSpec = field(default_factory=ExclusionSpec)
    dropped: int = 0
    dropped_by_target: Dict[str, int] = field(default_factory=dict)

    def as_public(self) -> Dict[str, Any]:
        out = self.spec.as_public()
        out["dropped"] = int(self.dropped)
        return out


def apply_exclusions(
    packet: Dict[str, Any],
    spec: ExclusionSpec,
    *,
    conn: Optional[sqlite3.Connection] = None,
    ledger: Optional[Any] = None,
) -> ExclusionOutcome:
    """Remove every excluded item from ``packet``, in place, and say what it cost.

    Runs inside the retrieval boundary. What this drops never reaches the disclosure
    filter, the game layer, the stored artifact or the synthesis prompt — which is
    the whole point: the model is never asked to be discreet about something it can
    see.
    """
    outcome = ExclusionOutcome(spec=spec)
    if not isinstance(packet, dict) or not spec.requested:
        return outcome

    blocked_ids_by_target: Dict[str, Set[str]] = {}
    for target in spec.targets:
        if target.kind == KIND_ENTITY:
            blocked_ids_by_target[target.slug] = _record_ids_mentioning(conn, target.entity_ids)

    for key in _ITEM_KEYS:
        seq = packet.get(key)
        if not isinstance(seq, list) or not seq:
            continue
        kept: List[Any] = []
        for item in seq:
            hit = _first_matching_target(item, spec, blocked_ids_by_target)
            if hit is None:
                kept.append(item)
                continue
            outcome.dropped += 1
            outcome.dropped_by_target[hit] = outcome.dropped_by_target.get(hit, 0) + 1
        if len(kept) != len(seq):
            packet[key] = kept

    _record(outcome, ledger)
    return outcome


def _first_matching_target(
    item: Any, spec: ExclusionSpec, blocked_ids_by_target: Dict[str, Set[str]]
) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    for target in spec.targets:
        if target.kind == KIND_CATEGORY:
            category = _CATEGORY_BY_SLUG.get(target.slug)
            if category is not None and _matches_category(item, category):
                return target.slug
        elif target.kind == KIND_TIER:
            if _matches_tier(item, target.slug):
                return target.slug
        elif target.kind == KIND_ENTITY:
            if _matches_entity(item, target, blocked_ids_by_target.get(target.slug, set())):
                return target.slug
    return None


def _record(outcome: ExclusionOutcome, ledger: Optional[Any]) -> None:
    """Ledger: one entry per target that removed something, one per fragment that could not.

    ``stage=disclosure`` because exclusion is a disclosure decision, made on the same
    plane as the tiers and the black-hole policy. ``reason`` is closed-set; the slug
    of a named entity goes in ``detail``, which never leaves the node.
    """
    if ledger is None:
        return
    try:
        for target in outcome.spec.targets:
            dropped = outcome.dropped_by_target.get(target.slug, 0)
            detail: Dict[str, Any] = {"kind": target.kind, "target": target.slug}
            ledger.record(
                _N.STAGE_DISCLOSURE,
                ACTION_EXCLUDED,
                _REASON_BY_KIND.get(target.kind, REASON_CATEGORY),
                dropped=dropped,
                detail=detail,
            )
        if outcome.spec.unresolved:
            # The loud failure. The owner asked for something this plane could not
            # compile, so the turn says so instead of reading as if it had been done.
            ledger.record(
                _N.STAGE_DISCLOSURE,
                ACTION_NOT_APPLIED,
                REASON_UNCOMPILABLE,
                dropped=0,
                detail={"fragments": list(outcome.spec.unresolved)},
            )
    except Exception as exc:  # noqa: BLE001 — telemetry may never cost a turn
        logger.debug("exclusion ledger record skipped: %s", exc)


def _record_aggregate_not_applied(spec: ExclusionSpec, ledger: Optional[Any]) -> None:
    """One ``not_applied`` entry for a packet the item filter cannot reach.

    ``detail`` carries the compiled kinds/slugs (local only, and an entity slug is
    already the owner's word for it) so a local reader can see WHAT went
    un-applied; the public entry carries only the closed-set reason and a count.
    """
    if ledger is None:
        return
    try:
        detail: Dict[str, Any] = {
            "targets": [{"kind": t.kind, "target": t.slug} for t in spec.targets],
            "packet": "cohort_aggregate",
        }
        if spec.unresolved:
            detail["fragments"] = list(spec.unresolved)
        ledger.record(
            _N.STAGE_DISCLOSURE,
            ACTION_NOT_APPLIED,
            REASON_AGGREGATE,
            dropped=0,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry may never cost a turn
        logger.debug("exclusion ledger record skipped: %s", exc)


def enforce_request_exclusions(
    packet: Dict[str, Any],
    *,
    query_text: str,
    conn: Optional[sqlite3.Connection] = None,
    ledger: Optional[Any] = None,
    aggregate_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """Parse, apply, record. Returns the public exclusion block, or ``None``.

    The single call site the retrieval adapter needs. ``None`` (no exclusion in the
    request) leaves the packet byte-identical.

    ``aggregate_only`` marks a packet whose content is a DERIVED COUNT, not a set of
    items — the cohort rollup, which reports "N people" computed over cohort
    membership before any row exists to match a category, a tier or an entity id
    against. Running the item filter over it would remove nothing and therefore
    report ``enforced=True, dropped=0`` while the count itself still includes the
    excluded members: a claim of enforcement over a number that was never filtered,
    which is the shape the module docstring forbids. So this path claims nothing.
    It reports the exclusion as REQUESTED and NOT APPLIED, counts every compiled
    target as un-applied alongside the fragments that would not compile, and says
    so in the ledger — the same fail-loud record an uncompilable fragment gets.
    """
    try:
        spec = parse_exclusions(query_text, conn)
    except Exception as exc:  # noqa: BLE001 — a parse failure must not lose the turn…
        logger.debug("exclusion parse failed: %s", exc)
        # …but it also must not be reported as an honoured exclusion. Nothing was
        # requested as far as this plane can tell, so nothing is claimed.
        return None
    if not spec.requested:
        return None
    if aggregate_only:
        _record_aggregate_not_applied(spec, ledger)
        return {
            "requested": True,
            "enforced": False,
            "applied": [],
            "not_applied": len(spec.targets) + len(spec.unresolved),
            "dropped": 0,
        }
    outcome = apply_exclusions(packet, spec, conn=conn, ledger=ledger)
    if ledger is not None and outcome.dropped and _packet_is_empty(packet):
        # An exclusion that emptied the answer is a policy decision, not an absence —
        # the same call the black-hole policy makes, for the same reason: "no data"
        # would be a lie about the owner's own instruction.
        ledger.empty(_N.CAUSE_SCOPE_DENIED, reason=REASON_ENFORCED)
    return outcome.as_public()


def _packet_is_empty(packet: Dict[str, Any]) -> bool:
    return _N.result_is_empty(packet)


def summarize_for_meta(block: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts and closed-set slugs for `retrieval_metadata`. No fragments, no names."""
    if not block:
        return {}
    return {
        "exclusion_requested": bool(block.get("requested")),
        "exclusion_enforced": bool(block.get("enforced")),
        "exclusion_dropped": int(block.get("dropped") or 0),
        "exclusion_not_applied": int(block.get("not_applied") or 0),
    }
