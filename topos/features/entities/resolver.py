"""Entity resolution: surface mention -> canonical entity_id.

Tiers (first hit wins):
  1. identifier  — exact email/phone/handle match (seeded from contacts)
  2. alias       — normalized exact match on canonical_name or aliases
  3. fuzzy       — token-set similarity >= AUTO_MERGE_SCORE (same entity_type)
  4. create      — new entity

Fuzzy scores in [REVIEW_SCORE, AUTO_MERGE_SCORE) are never merged silently:
they create a new entity AND an entity_review row for owner confirmation.
Person merges are the one irreversible failure mode of the spine — mentions
keep full provenance so any merge can be undone.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from ...storage.db.write_gate import batched_writes, commit_connection, with_db_write

logger = logging.getLogger("topos.features.entities.resolver")

AUTO_MERGE_SCORE = 0.92
REVIEW_SCORE = 0.80

# Sightings an entity needs before the owner is asked to merge anything into it.
# One bar for both proposal paths — the consolidation sweep imports it as
# _MIN_MENTIONS_FOR_SWEEP. Lowering it to 1 admits one-sighting evidence for a
# near-irreversible merge; on the first live node it would have taken the open
# queue from 24 questions to 41.
MIN_MENTIONS_FOR_MERGE = 2

# Model labels → spine entity_type. Covers CoNLL-2003 (PER/ORG/LOC/MISC) and
# OntoNotes 5 (18 types): identity-bearing labels get first-class or folded
# types; everything else falls through to "topic".
_NER_TYPE_MAP = {
    "PER": "person",
    "PERSON": "person",
    "ORG": "org",
    "LOC": "place",
    "GPE": "place",
    "FAC": "place",  # buildings, airports, venues
    "NORP": "org",  # nationality/religious/political groups
    "MISC": "topic",
    "WORK_OF_ART": "work_of_art",
    "EVENT": "event",
    "PRODUCT": "product",
    "LAW": "topic",
    "LANGUAGE": "topic",
}

# Value labels are quantities, not identities — dropping them here (instead of
# letting the unknown-label fallback bucket them into "topic") is what keeps an
# OntoNotes model from flooding the spine with dates and dollar amounts.
_NER_DROP_LABELS = frozenset(
    {"DATE", "TIME", "PERCENT", "MONEY", "QUANTITY", "ORDINAL", "CARDINAL"}
)

_HONORIFICS = ("dr", "mr", "mrs", "ms", "prof", "dr.")


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Possessives, before punctuation becomes whitespace. "Altman's" would
    # otherwise normalize to "altman s" and mint a second entity beside
    # "Altman" — 26 such twins in the first live graph ("Alpha Hotel's",
    # "Tango Victor's", "School's"). Requiring the apostrophe keeps names that
    # merely end in s ("James", "Suggs") untouched.
    text = re.sub(r"['’]s\b", "", text.lower())
    text = re.sub(r"[^\w\s@.+-]", " ", text)
    tokens = [t for t in text.split() if t]
    if tokens and tokens[0].rstrip(".") in _HONORIFICS:
        tokens = tokens[1:] or tokens
    return " ".join(tokens)


def token_set_similarity(a: str, b: str) -> float:
    """Order-insensitive similarity in [0, 1] (rapidfuzz-style token_set_ratio)."""
    ta, tb = set(normalize_name(a).split()), set(normalize_name(b).split())
    if not ta or not tb:
        return 0.0
    if ta <= tb or tb <= ta:
        return 1.0
    inter = " ".join(sorted(ta & tb))
    sa = " ".join(sorted(ta))
    sb = " ".join(sorted(tb))
    scores = [
        SequenceMatcher(None, sa, sb).ratio(),
    ]
    if inter:
        scores.append(SequenceMatcher(None, inter, sa).ratio())
        scores.append(SequenceMatcher(None, inter, sb).ratio())
    return max(scores)


def map_ner_type(ner_label: Optional[str]) -> Optional[str]:
    """Spine entity_type for a model label; None = drop the mention entirely."""
    label = str(ner_label or "").upper()
    if label in _NER_DROP_LABELS:
        return None
    return _NER_TYPE_MAP.get(label, "topic")


def _remap_derivation_corpus(conn: sqlite3.Connection, *, keep_id: str, absorb_id: str) -> dict:
    """Repoint everything keyed on the absorbed entity that the edge/mention fold misses.

    Each of these tables stores the subject as an opaque id, so a merge that skips them
    leaves rows describing a person who no longer exists. `signal_objects` needs both the
    key and the payload rewritten, because the subject appears in both and a reader that
    trusts one over the other would see two different answers.
    """
    counts = {}
    stmts = [
        ("signal_objects_key",
         "UPDATE signal_objects SET object_key = REPLACE(object_key, ?, ?)"
         " WHERE object_key LIKE '%' || ? || '%'", (absorb_id, keep_id, absorb_id)),
        ("signal_objects_payload",
         "UPDATE signal_objects SET payload_json = REPLACE(payload_json, ?, ?)"
         " WHERE payload_json LIKE '%' || ? || '%'", (absorb_id, keep_id, absorb_id)),
        ("fact_conflicts",
         "UPDATE fact_conflicts SET subject_entity_id=? WHERE subject_entity_id=?",
         (keep_id, absorb_id)),
        ("intelligence_exclusions",
         "UPDATE intelligence_exclusions SET entity_id=? WHERE entity_id=?",
         (keep_id, absorb_id)),
        ("entity_blackholes",
         "UPDATE entity_blackholes SET entity_id=? WHERE entity_id=?", (keep_id, absorb_id)),
        ("net_subject_policy",
         "UPDATE OR IGNORE net_subject_policy SET subject_entity_id=? WHERE subject_entity_id=?",
         (keep_id, absorb_id)),
    ]
    for name, sql, args in stmts:
        try:
            counts[name] = conn.execute(sql, args).rowcount
        except sqlite3.Error:
            # A table this node does not have is not a failure — net_subject_policy ships
            # ahead of its migration by design. Recording a zero keeps the summary honest.
            counts[name] = 0
    return counts


def _write_merge_tombstone(conn: sqlite3.Connection, *, keep_id: str, absorb_id: str,
                           name: str, aliases: list, identifiers: list) -> None:
    """Record what was absorbed, so an incorrect merge is reviewable rather than only
    re-ingestable. Created on demand: this is feature-owned state, and a registry migration
    would bump user_version past an installed engine."""
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS entity_merge_tombstones (
                   absorbed_entity_id TEXT PRIMARY KEY,
                   merged_into TEXT NOT NULL,
                   canonical_name TEXT,
                   aliases_json TEXT,
                   identifiers_json TEXT,
                   merged_at TEXT NOT NULL DEFAULT (datetime('now'))
               )""")
        conn.execute(
            "INSERT OR REPLACE INTO entity_merge_tombstones"
            " (absorbed_entity_id, merged_into, canonical_name, aliases_json, identifiers_json)"
            " VALUES (?,?,?,?,?)",
            (absorb_id, keep_id, name, json.dumps(aliases), json.dumps(identifiers)))
        # a chain of merges must resolve to the surviving row, not to an intermediate
        conn.execute("UPDATE entity_merge_tombstones SET merged_into=? WHERE merged_into=?",
                     (keep_id, absorb_id))
    except sqlite3.Error:
        pass


def value_label_surfaces(conn: sqlite3.Connection) -> frozenset:
    """Normalized surfaces the NER model judged to be VALUES, not identities.

    ``map_ner_type`` drops DATE/TIME/CARDINAL/… on the extraction lane, but it
    needs a label — and the graph's other minting lane (topic-cluster
    ``related_entities`` and string-valued fact objects in
    ``fact_materializer``) only ever sees a bare surface string. With no label
    the drop list cannot fire, so "an hour", "this week", "four" and "Mon-Wed"
    were minted as first-class ``topic`` nodes and rendered in the graph.

    Rather than guess from the string — an endless denylist that would also
    swallow real names — this reuses the judgment the model already made when
    it extracted the mention. A surface is a value only when value-labelled
    mentions OUTNUMBER identity-labelled ones, so a genuine entity that was
    mislabelled once ("Phoenix" as a DATE) still survives.
    """
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(json_extract(payload_json, '$.entity_text'), entity_text) AS surface,
                   UPPER(COALESCE(json_extract(payload_json, '$.entity_type'), '')) AS label,
                   COUNT(*) AS n
            FROM message_entities
            GROUP BY surface, label
            """
        ).fetchall()
    except sqlite3.Error:
        return frozenset()
    value_n: Dict[str, int] = {}
    total_n: Dict[str, int] = {}
    for surface, label, n in rows:
        key = normalize_name(str(surface or ""))
        if not key:
            continue
        count = int(n or 0)
        total_n[key] = total_n.get(key, 0) + count
        if str(label or "") in _NER_DROP_LABELS:
            value_n[key] = value_n.get(key, 0) + count
    return frozenset(k for k, v in value_n.items() if v * 2 > total_n.get(k, 0))


# Function/common words that NER routinely mislabels as MISC/topic entities.
# A surface made ENTIRELY of these is junk ('IS', 'Go', 'and', 'of', 'The One',
# 'Place') — 634 such entities polluted the live spine, dossiers, and mentions.
# The query-time linking guard already refuses to LINK them; this stops them
# being MINTED at all (plan C4). A real name with one stopword token ('The
# Weeknd', 'Hotel Juliett') survives because not *every* token is a stopword.
_JUNK_SURFACE_WORDS = frozenset(
    {
        "a", "an", "and", "or", "of", "at", "am", "is", "are", "was", "were",
        "be", "been", "in", "on", "to", "the", "it", "its", "as", "by", "for",
        "with", "from", "this", "that", "these", "those", "he", "she", "they",
        "we", "you", "i", "me", "my", "do", "did", "does", "go", "went", "get",
        "got", "has", "have", "had", "not", "no", "yes", "so", "if", "but",
        "one", "all", "any", "can", "just", "now", "out",
        "up", "down", "here", "there", "place", "thing", "things", "some",
        "more", "most", "other", "into", "about", "over", "under", "our",
        "time", "times", "long", "today",
        "tomorrow", "yesterday", "what", "who", "how", "when", "where", "why",
        "much", "many", "then", "than", "also", "very",
        # NB: deliberately NOT denylisting will/good/back/new/old/day/well —
        # each is a plausible given name or surname; bare function words only.
    }
)

# Plan C4: reject ≤3-char mint surfaces by default, but keep known short orgs /
# places / nicknames that are real entities (and alphanumeric codes like C3).
_SHORT_SURFACE_ALLOWLIST = frozenset(
    {
        "al", "bo", "ed", "jo", "li", "ty", "aj", "jd", "jp", "tj",
        "aws", "ibm", "ups", "att", "bbc", "cnn", "nba", "nfl", "nhl",
        "nyc", "la", "sf", "uk", "us", "eu", "un", "mit", "cmu",
        "max", "sam", "ben", "amy", "zoe", "mia", "leo", "eva", "ava",
    }
)


def clean_entity_surface(text: str) -> str:
    """Trim the punctuation a mention drags in from the sentence around it.

    Extraction hands over spans as they were cut, so a list item or a range
    keeps its dash: "Williamsburg-", "NYC-", "- Hood Circle". Left alone the
    stray character is part of the identity, and the entity never matches the
    clean spelling of the same place.
    """
    return str(text or "").strip().strip("-–—,;:").strip()


def is_valid_entity_surface(text: str) -> bool:
    """Reject NER artifacts before they become entities.

    BERT-style NER emits sub-word fragments ('##dy', '##ccelerator') when an
    entity spans wordpieces; those, digit/punctuation-only surfaces, ≤3-char
    junk (plan C4), and all-stopword surfaces ('IS', 'Go', 'The One') must never
    enter the registry.

    Redaction placeholders are rejected too. Text bound for a model that must
    not see names comes back with "[NAME]", "[EMAIL]" and friends standing in
    for them (see sanitization.privacy_filter.ENTITY_PLACEHOLDERS); anything
    minted from that text names the redaction, not a thing in the world.
    """
    surface = clean_entity_surface(text)
    if not surface or "##" in surface:
        return False
    if re.search(r"\[[A-Z][A-Z_]*\]", surface):
        return False
    normalized = normalize_name(surface)
    if not normalized:
        return False
    if not any(c.isalpha() for c in normalized):
        return False
    # C4: ≤3-char surfaces are almost always NER crumbs ("dy", "is", "the").
    # Allow short allowlisted names/orgs and alphanumeric codes (C3, H2).
    if len(normalized) <= 3:
        compact = normalized.replace(" ", "")
        has_digit = any(c.isdigit() for c in compact)
        if compact not in _SHORT_SURFACE_ALLOWLIST and not (
            has_digit and any(c.isalpha() for c in compact)
        ):
            return False
    # All-stopword surface → junk. Keep names where at least one token is a
    # real word ('Hotel Juliett', 'The Weeknd', 'LA Fitness').
    alpha_tokens = [t for t in normalized.split() if any(c.isalpha() for c in t)]
    if alpha_tokens and all(t in _JUNK_SURFACE_WORDS for t in alpha_tokens):
        return False
    return True


def _placeholder_name_from_identifiers(identifiers: List[str]) -> str:
    """A display name for a contact that has none, taken from its identifiers.

    Preference order is legibility, not availability: an email or a handle says
    something to a human reading a graph, a phone number says almost nothing.
    Whatever is chosen is a placeholder — the contact_id is what identity is
    anchored on, so a later pass that learns the real name updates the same row
    rather than creating a second person.
    """
    if not identifiers:
        return ""
    emails = [i for i in identifiers if "@" in i and "." in i.rsplit("@", 1)[-1]]
    if emails:
        return sorted(emails)[0]
    # A handle: has letters, is not a bare phone number.
    handles = [
        i
        for i in identifiers
        if any(c.isalpha() for c in i) and not i.lstrip("+").replace("-", "").isdigit()
    ]
    if handles:
        return sorted(handles)[0]
    phones = [i for i in identifiers if any(c.isdigit() for c in i)]
    return sorted(phones)[0] if phones else ""


class EntityResolver:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------ seeding

    def seed_from_contacts(self) -> int:
        """One person entity per contact, identifiers attached."""
        created = 0
        try:
            contacts = self._conn.execute(
                "SELECT contact_id, display_name, known_usernames_json, is_self FROM contacts"
            ).fetchall()
        except sqlite3.OperationalError:
            return 0
        # Per-contact lookups are quick indexed reads and the contact list is
        # personal-scale, so one gated batch covers the whole seed.
        with batched_writes(self._conn):
            for contact_id, display_name, usernames_json, is_self in contacts:
                existing = self._conn.execute(
                    "SELECT entity_id FROM entities WHERE contact_id=?",
                    (contact_id,),
                ).fetchone()
                identifiers: List[str] = []
                try:
                    rows = self._conn.execute(
                        "SELECT identifier FROM contact_identifiers WHERE contact_id=?",
                        (contact_id,),
                    ).fetchall()
                    identifiers = [str(r[0]).strip().lower() for r in rows if r[0]]
                except sqlite3.OperationalError:
                    pass
                try:
                    for username in json.loads(usernames_json or "[]"):
                        if str(username).strip():
                            identifiers.append(str(username).strip().lower())
                except (json.JSONDecodeError, TypeError):
                    pass

                # A contact with no display name still needs a person entity.
                #
                # This used to be `if not name: continue`, placed before the
                # identifiers were even read — and every contact derived from a
                # messenger sync has an empty display_name, because a phone
                # number is all iMessage gives you. Measured on a live node
                # 2026-08-25: 822 of 1,423 contacts nameless, and of 167
                # distinct non-self message senders, all 167 resolved to a
                # contact while only 10 resolved to a person ENTITY.
                #
                # That gap is load-bearing downstream: fact_materializer's SPO
                # lane refuses any fact whose subject fails `_entity_exists`,
                # so facts about the people you actually text had nowhere to
                # attach. Falling back to the contact's best identifier gives
                # the graph a subject to hang evidence on; a later pass that
                # learns the real name updates the same row, because the
                # contact_id anchor is what identity is keyed on here, not the
                # name.
                name = str(display_name or "").strip()
                if not name:
                    name = _placeholder_name_from_identifiers(identifiers)
                if not name:
                    # Nothing to key on at all — no name and no identifier.
                    # 653 of 1,423 contacts have zero identifier rows; minting
                    # entities for them would add people who can never be
                    # resolved from a message.
                    continue
                if existing:
                    self._conn.execute(
                        "UPDATE entities SET identifiers_json=?, updated_at=datetime('now') WHERE entity_id=?",
                        (json.dumps(sorted(set(identifiers))), existing[0]),
                    )
                    continue
                self._create_entity(
                    name,
                    "person",
                    identifiers=sorted(set(identifiers)),
                    contact_id=str(contact_id),
                    is_self=bool(is_self),
                )
                created += 1
        return created

    # ---------------------------------------------------------- resolution

    def resolve(
        self,
        surface_text: str,
        *,
        entity_type: Optional[str] = None,
        record_id: Optional[str] = None,
        queue_review: bool = True,
    ) -> Tuple[str, str]:
        """Resolve a mention; returns (entity_id, tier).

        ``queue_review=False`` for callers minting a graph vertex rather than
        recording a sighting — ``fact_materializer`` needs a node to hang an
        edge on, ``graph_enrichers`` the same for place names. They cite no
        record, so a question raised here shows the owner nothing to judge by,
        and the entity minted for it is deleted again by orphan cleanup (no
        mentions, no contact anchor) before the next derivation run — which then
        re-mints it and asks again. That loop, not the ingest path, produced
        97% of the review rows on the first live node: 2,886 rows carrying 18
        decisions, against 88 rows carrying 76 real ones. Nothing is lost by
        staying quiet, because the record that fed derivation was itself
        ingested, and the ingest path already asked about it with provenance.
        """
        surface = clean_entity_surface(surface_text)
        if not surface:
            raise ValueError("empty surface text")
        if not is_valid_entity_surface(surface):
            raise ValueError(f"invalid entity surface: {surface_text!r}")
        etype = entity_type or "topic"
        normalized = normalize_name(surface)
        if not normalized:
            raise ValueError(f"unresolvable surface text: {surface_text!r}")
        if self._is_excluded_name(normalized):
            raise ValueError(f"entity excluded by owner: {surface_text!r}")

        # Owner unbinds: entities this surface must NEVER resolve to again
        # (split_surface guards). "Claire" is a token-subset of "Claire
        # Duncombe" (similarity 1.0), so without this every tier below would
        # happily re-merge an owner-corrected mislink.
        blocked = self._no_bind_targets(normalized)

        # Tier 1: identifier (emails/handles/phones)
        if "@" in surface or surface.startswith("+") or "." in normalized.replace(" ", ""):
            hit = self._match_identifier(normalized.replace(" ", ""))
            if hit and hit not in blocked:
                return hit, "identifier"

        # Tier 1.5: contact-seeded people outrank NER typing. NER labels
        # "Austin" a place even when the owner's contacts contain exactly one
        # Austin — the contact registry is ground truth about people the owner
        # knows, so a unique contact match wins regardless of the NER label.
        contact_hit = self._match_contact_person(normalized)
        if contact_hit and contact_hit not in blocked:
            return contact_hit, "contact"

        # Tier 2: exact normalized alias
        hit = self._match_alias(normalized, etype)
        if hit and hit not in blocked:
            return hit, "alias"

        # Tier 3: fuzzy within type. Ambiguity (two candidates at threshold)
        # must never auto-merge — a wrong person-merge is the spine's one
        # near-irreversible failure, so ties go to review instead.
        best_id, best_score, at_threshold = self._best_fuzzy(normalized, etype)
        if best_id in blocked:
            best_id, best_score = None, 0.0
        if best_id and best_score >= AUTO_MERGE_SCORE and at_threshold == 1:
            self._add_alias(best_id, surface)
            return best_id, "fuzzy"
        entity_id = self._create_entity(surface, etype)
        if queue_review and best_id and best_score >= REVIEW_SCORE:
            self._queue_review(surface, best_id, best_score, record_id)
        return entity_id, "created"

    def _match_identifier(self, needle: str) -> Optional[str]:
        rows = self._conn.execute(
            "SELECT entity_id, identifiers_json FROM entities WHERE identifiers_json != '[]'"
        ).fetchall()
        for entity_id, identifiers_json in rows:
            try:
                identifiers = json.loads(identifiers_json or "[]")
            except json.JSONDecodeError:
                continue
            if needle in (str(i).lower() for i in identifiers):
                return str(entity_id)
        return None

    def _no_bind_targets(self, normalized: str) -> frozenset:
        """Entities the owner UNBOUND from this surface (split_surface guards)."""
        try:
            rows = self._conn.execute(
                "SELECT candidate_entity_id FROM entity_review "
                "WHERE kind='no_bind' AND status='approved' AND surface_text=?",
                (normalized,),
            ).fetchall()
        except sqlite3.OperationalError:
            return frozenset()
        return frozenset(str(r[0]) for r in rows if r[0])

    def _is_excluded_name(self, normalized: str) -> bool:
        """Owner tombstone: never track this entity again (see lifecycle.exclusions)."""
        try:
            row = self._conn.execute(
                "SELECT 1 FROM intelligence_exclusions WHERE artifact_type='entity' AND artifact_key=?",
                (normalized,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def _match_contact_person(self, normalized: str) -> Optional[str]:
        """Unique match against contact-seeded person entities (any NER type).

        Single-token ambiguity is checked against *all* person entities, not
        just contacts — if any other person shares the token, never guess.
        """
        row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE normalized_name=? AND entity_type='person'"
            " AND contact_id IS NOT NULL",
            (normalized,),
        ).fetchone()
        if row:
            return str(row[0])
        tokens = normalized.split()
        if len(tokens) != 1:
            return None
        contact_hit: Optional[str] = None
        matches = 0
        for entity_id, name, contact_id in self._conn.execute(
            "SELECT entity_id, normalized_name, contact_id FROM entities WHERE entity_type='person'"
        ).fetchall():
            if normalized in str(name).split():
                matches += 1
                if contact_id:
                    contact_hit = str(entity_id)
        return contact_hit if matches == 1 else None

    def _match_alias(self, normalized: str, etype: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE normalized_name=? AND entity_type=?",
            (normalized, etype),
        ).fetchone()
        if row:
            return str(row[0])
        rows = self._conn.execute(
            "SELECT entity_id, aliases_json FROM entities WHERE entity_type=?",
            (etype,),
        ).fetchall()
        for entity_id, aliases_json in rows:
            try:
                aliases = json.loads(aliases_json or "[]")
            except json.JSONDecodeError:
                continue
            if normalized in (normalize_name(a) for a in aliases):
                return str(entity_id)
        # People: a single-token mention ("Maya") matches a unique person whose
        # name contains that token; ambiguity falls through to fuzzy/create.
        if etype == "person" and " " not in normalized:
            candidates = [
                str(entity_id)
                for entity_id, name in self._conn.execute(
                    "SELECT entity_id, normalized_name FROM entities WHERE entity_type='person'"
                ).fetchall()
                if normalized in str(name).split()
            ]
            if len(candidates) == 1:
                return candidates[0]
        return None

    def _best_fuzzy(self, normalized: str, etype: str) -> Tuple[Optional[str], float, int]:
        best_id, best_score, at_threshold = None, 0.0, 0
        rows = self._conn.execute(
            "SELECT entity_id, normalized_name FROM entities WHERE entity_type=?",
            (etype,),
        ).fetchall()
        for entity_id, name in rows:
            score = token_set_similarity(normalized, str(name))
            if score >= AUTO_MERGE_SCORE:
                at_threshold += 1
            if score > best_score:
                best_id, best_score = str(entity_id), score
        return best_id, best_score, at_threshold

    # ------------------------------------------------------------- writes

    def _create_entity(
        self,
        name: str,
        etype: str,
        *,
        identifiers: Optional[List[str]] = None,
        contact_id: Optional[str] = None,
        is_self: bool = False,
    ) -> str:
        entity_id = f"ent_{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO entities (
                entity_id, entity_type, canonical_name, normalized_name,
                aliases_json, identifiers_json, contact_id, is_self,
                first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_id,
                etype,
                str(name).strip(),
                normalize_name(name),
                "[]",
                json.dumps(identifiers or []),
                contact_id,
                1 if is_self else 0,
                now,
                now,
            ),
        )
        if etype == "org":
            self._link_org_hierarchy(entity_id, normalize_name(name))
        return entity_id

    def _link_org_hierarchy(self, entity_id: str, normalized: str) -> None:
        """Connect fragmented org entities ("Google Docs" vs "Google").

        Products/sub-units stay distinct entities — "Google Docs" is not an
        alias of "Google" — but get a directed part_of edge so query linking
        and dossiers can aggregate the family. Parent = org whose token set is
        a strict subset (e.g. {google} ⊂ {google, docs}); the reverse case
        (parent created after its products) links existing children upward.
        """
        from .edges import EDGE_PART_OF, update_edge

        tokens = set(normalized.split())
        if not tokens:
            return
        for other_id, other_name in self._conn.execute(
            "SELECT entity_id, normalized_name FROM entities WHERE entity_type='org' AND entity_id != ?",
            (entity_id,),
        ).fetchall():
            other_tokens = set(str(other_name).split())
            if not other_tokens or other_tokens == tokens:
                continue
            if other_tokens < tokens:
                update_edge(
                    self._conn,
                    src_entity_id=entity_id,
                    dst_entity_id=str(other_id),
                    edge_type=EDGE_PART_OF,
                )
            elif tokens < other_tokens:
                update_edge(
                    self._conn,
                    src_entity_id=str(other_id),
                    dst_entity_id=entity_id,
                    edge_type=EDGE_PART_OF,
                )

    def _add_alias(self, entity_id: str, surface: str) -> None:
        row = self._conn.execute(
            "SELECT aliases_json, normalized_name FROM entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if not row:
            return
        try:
            aliases = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            aliases = []
        normalized_existing = {normalize_name(a) for a in aliases} | {str(row[1])}
        if normalize_name(surface) not in normalized_existing:
            aliases.append(str(surface).strip())
            self._conn.execute(
                "UPDATE entities SET aliases_json=?, updated_at=datetime('now') WHERE entity_id=?",
                (json.dumps(aliases), entity_id),
            )

    def _is_mergeable_candidate(self, candidate_id: str) -> bool:
        """Has this entity been seen enough to be worth merging something into?

        The bar is the consolidation sweep's, and it applies to contacts too.
        An address book is not a list of important people: most imported
        contacts are someone met in passing, often a decade ago, who will never
        come up again. A contact row says the owner once had a phone number, not
        that a name in today's data is that person — so a question that exists
        only because a surface fuzzy-matches an address-book entry is noise.
        The live queue showed exactly that: 35 of its 39 contact questions
        proposed a never-mentioned contact, among them "Delta Whiskey" (the Palantir
        CEO) offered as the owner's contact "Alex", "Alexis" as "Alex", "Alice"
        as "Allie", "Dan" as "Delta Sierra" — first names colliding, not
        identities matching.

        Once that contact IS named in ingested content it earns mentions like
        anything else, and the question comes back with evidence behind it.
        """
        row = self._conn.execute(
            "SELECT COALESCE(mention_count, 0) FROM entities WHERE entity_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return False
        return int(row[0] or 0) >= MIN_MENTIONS_FOR_MERGE

    def _queue_review(
        self, surface: str, candidate_id: str, score: float, record_id: Optional[str]
    ) -> None:
        """Ask the owner "is this surface that entity?" — once, ever, and only
        when both sides of the question are real.

        Three gates, for three different failure modes.

        ``record_id`` is provenance on the *surface* side.  ``resolve()``'s
        ``queue_review`` flag lets a caller declare it is minting a graph vertex
        rather than recording a sighting; this enforces the same thing from the
        row's own contents, so a future derivation lane that forgets the flag
        still cannot fill the queue with strings the owner never said.

        ``_is_mergeable_candidate`` is evidence on the *candidate* side.

        The last gate is repetition. This runs per *mention*, so a surface the
        owner uses every week would otherwise stack up hundreds of identical
        rows — and an answer already given is an answer: a decision they
        approved or dismissed must never come back, which is what the sweep has
        always done for its own merge proposals (see
        consolidation._existing_pairs).

        No gate for entity types: ``_best_fuzzy`` only scores within one
        ``entity_type`` and the entity minted for the surface carries that same
        type, so a proposal is same-type by construction. Approval is where that
        invariant had to be re-stated (consolidation.resolve_review).
        """
        if not str(record_id or "").strip():
            return
        if not self._is_mergeable_candidate(candidate_id):
            return
        try:
            already_asked = self._conn.execute(
                """
                SELECT 1 FROM entity_review
                WHERE kind = 'resolution'
                  AND candidate_entity_id = ?
                  AND lower(trim(surface_text)) = lower(trim(?))
                  AND status IN ('pending', 'approved', 'dismissed')
                LIMIT 1
                """,
                (candidate_id, surface),
            ).fetchone()
        except sqlite3.OperationalError:
            already_asked = None  # pre-`kind` schema; queue as before
        if already_asked:
            return
        self._conn.execute(
            """
            INSERT INTO entity_review (review_id, surface_text, candidate_entity_id, score, record_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (f"rev_{uuid.uuid4().hex[:12]}", surface, candidate_id, round(score, 4), record_id),
        )

    def record_mention(
        self,
        entity_id: str,
        *,
        record_id: str,
        surface_text: str,
        source_id: Optional[str] = None,
        canonical_table: Optional[str] = None,
        confidence: Optional[float] = None,
        event_at: Optional[str] = None,
        authored_by_owner: Optional[int] = None,
    ) -> None:
        mention_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(entity_mentions)").fetchall()
        }
        has_authored_col = "authored_by_owner" in mention_cols
        # P3.1: denormalized owner-authorship for IMB / misattribution expansion.
        # Explicit arg wins; else resolve from the parent canonical row.
        if has_authored_col and authored_by_owner is None:
            try:
                from topos.storage.db.migrations.entity_mentions_authored_v1 import (
                    lookup_authored_by_owner,
                )

                authored_by_owner = lookup_authored_by_owner(
                    self._conn, record_id, canonical_table=canonical_table
                )
            except Exception:  # noqa: BLE001 — never block mention mint on lookup
                authored_by_owner = None
        if has_authored_col:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO entity_mentions (
                    mention_id, entity_id, record_id, source_id, canonical_table,
                    surface_text, confidence, event_at, authored_by_owner
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"men_{uuid.uuid4().hex[:16]}",
                    entity_id,
                    record_id,
                    source_id,
                    canonical_table,
                    surface_text,
                    confidence,
                    event_at,
                    authored_by_owner,
                ),
            )
        else:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO entity_mentions (
                    mention_id, entity_id, record_id, source_id, canonical_table,
                    surface_text, confidence, event_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"men_{uuid.uuid4().hex[:16]}",
                    entity_id,
                    record_id,
                    source_id,
                    canonical_table,
                    surface_text,
                    confidence,
                    event_at,
                ),
            )
        if not cursor.rowcount:
            return  # duplicate mention (batch replay) — don't inflate counts
        self._conn.execute(
            """
            UPDATE entities SET
                mention_count = mention_count + 1,
                last_seen = COALESCE(MAX(COALESCE(last_seen, ''), COALESCE(?, '')), last_seen),
                updated_at = datetime('now')
            WHERE entity_id = ?
            """,
            (event_at, entity_id),
        )

    # ------------------------------------------------------- owner tooling

    def merge_entities(self, keep_id: str, absorb_id: str) -> None:
        """Reversible merge: absorb aliases/identifiers/mentions into keep_id."""
        keep = self._conn.execute(
            "SELECT aliases_json, identifiers_json, entity_type FROM entities WHERE entity_id=?",
            (keep_id,),
        ).fetchone()
        gone = self._conn.execute(
            "SELECT canonical_name, aliases_json, identifiers_json, entity_type"
            " FROM entities WHERE entity_id=?",
            (absorb_id,),
        ).fetchone()
        if not keep or not gone:
            raise ValueError("both entities must exist")
        # Every path that proposes a merge pairs like with like, so a cross-type
        # merge arriving here is a malformed proposal, not a real duplicate —
        # and this is the write that cannot be undone. Fail before it lands.
        if str(keep[2]) != str(gone[3]):
            raise ValueError(
                f"cannot merge across entity types (keep={keep[2]!r}, absorb={gone[3]!r})"
            )
        aliases = list(
            dict.fromkeys(
                json.loads(keep[0] or "[]") + [gone[0]] + json.loads(gone[1] or "[]")
            )
        )
        identifiers = sorted(
            set(json.loads(keep[1] or "[]")) | set(json.loads(gone[2] or "[]"))
        )
        from .edges import merge_entity_edges

        with with_db_write():
            self._conn.execute(
                "UPDATE entities SET aliases_json=?, identifiers_json=?, updated_at=datetime('now') WHERE entity_id=?",
                (json.dumps(aliases), json.dumps(identifiers), keep_id),
            )
            self._conn.execute(
                "UPDATE entity_mentions SET entity_id=? WHERE entity_id=?", (keep_id, absorb_id)
            )
            # Fold-and-rewrite edges: a blanket UPDATE of src/dst would violate
            # the active-row partial unique index when both entities hold an
            # active edge of the same type to the same third entity
            # (edges.merge_entity_edges folds those collisions into the
            # surviving row).
            merge_entity_edges(self._conn, keep_id=keep_id, absorb_id=absorb_id)
            self._conn.execute(
                "UPDATE entities SET mention_count = (SELECT COUNT(*) FROM entity_mentions WHERE entity_id=?) WHERE entity_id=?",
                (keep_id, keep_id),
            )
            # --- the derivation corpus ---
            #
            # Folding aliases, mentions and edges is not the whole merge. The absorbed
            # entity is also the SUBJECT of stored facts, of quarantined conflicts and of
            # the owner's exclusions, and none of those live in the three tables above.
            # Measured on the live node before this was written: collapsing the owner's nine
            # entities would have stranded 335 signal_objects (178 facts + 157 dossier rows),
            # 13 fact_conflicts and 3 intelligence_exclusions — pointing at a row that no
            # longer exists, which is precisely the dangling-subject shape that made two
            # promoted facts unreachable earlier the same day.
            _remap_derivation_corpus(self._conn, keep_id=keep_id, absorb_id=absorb_id)
            # --- the tombstone ---
            #
            # The docstring says "reversible merge". It was not: this ended in a DELETE with
            # nothing recorded, so an incorrect merge could be re-split only by re-ingesting.
            # The tombstone makes the claim true enough to act on — what was absorbed, into
            # what, and when.
            _write_merge_tombstone(self._conn, keep_id=keep_id, absorb_id=absorb_id,
                                   name=str(gone[0] or ""), aliases=json.loads(gone[1] or "[]"),
                                   identifiers=json.loads(gone[2] or "[]"))
            self._conn.execute("DELETE FROM entities WHERE entity_id=?", (absorb_id,))
            commit_connection(self._conn)
