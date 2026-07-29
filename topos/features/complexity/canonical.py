"""Read-time entity canonicalization (non-destructive).

The live entity table contains extraction artifacts: the same concept split
across types ("Topos" as org/product/project/work_of_art), the same person
extracted multiple times (x3 is common), and person/place names that also
exist as topic entities. Metrics computed on the raw table split one node's
evidence across duplicates. This module builds an analytical mapping
entity_id -> canonical_id without touching the evidence tables; original
mentions stay resolvable through `members`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# Same-name entities of these types are one concept observed through different
# extraction lenses.
CONCEPT_TYPES = {"org", "product", "project", "work_of_art", "topic", "event"}

# Canonical type when a merge group mixes types (most specific/identifying wins).
TYPE_PRIORITY = [
    "person",
    "org",
    "project",
    "product",
    "event",
    "place",
    "work_of_art",
    "topic",
    "goal",
    "conversation",
]

# Entity types excluded from attention metrics (kept for graph/influence).
ATTENTION_EXCLUDED_TYPES = {"conversation", "goal"}

# Types that never auto-merge on name alone.
NO_NAME_MERGE_TYPES = {"goal", "conversation"}


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (name or "").lower())).strip()


@dataclass
class CanonicalEntity:
    canonical_id: str
    canonical_name: str
    canonical_type: str
    members: List[str] = field(default_factory=list)
    member_types: Set[str] = field(default_factory=set)
    merge_confidence: float = 1.0
    is_self: bool = False
    total_mentions: int = 0

    @property
    def merged(self) -> bool:
        return len(self.members) > 1

    @property
    def attention_eligible(self) -> bool:
        return not self.is_self and self.canonical_type not in ATTENTION_EXCLUDED_TYPES


@dataclass
class CanonicalIndex:
    entities: Dict[str, CanonicalEntity] = field(default_factory=dict)
    to_canonical: Dict[str, str] = field(default_factory=dict)

    def resolve(self, entity_id: str) -> Optional[str]:
        return self.to_canonical.get(entity_id)

    def get(self, canonical_id: str) -> Optional[CanonicalEntity]:
        return self.entities.get(canonical_id)

    def name_of(self, canonical_id: str) -> str:
        entity = self.entities.get(canonical_id)
        return entity.canonical_name if entity else canonical_id

    def type_of(self, canonical_id: str) -> str:
        entity = self.entities.get(canonical_id)
        return entity.canonical_type if entity else "unknown"

    @property
    def merge_rate(self) -> float:
        raw = len(self.to_canonical)
        if raw == 0:
            return 0.0
        return 1.0 - (len(self.entities) / raw)

    def merged_groups(self, limit: int = 20) -> List[Dict[str, object]]:
        groups = [e for e in self.entities.values() if e.merged]
        groups.sort(key=lambda e: len(e.members), reverse=True)
        return [
            {
                "canonical_id": e.canonical_id,
                "canonical_name": e.canonical_name,
                "canonical_type": e.canonical_type,
                "member_count": len(e.members),
                "member_types": sorted(e.member_types),
                "merge_confidence": e.merge_confidence,
            }
            for e in groups[:limit]
        ]


class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class _RawEntity:
    entity_id: str
    entity_type: str
    canonical_name: str
    normalized_name: str
    aliases: List[str]
    is_self: bool
    mention_count: int


def _mergeable(a: _RawEntity, b: _RawEntity) -> Optional[float]:
    """Merge confidence for two same-name entities, or None if incompatible.

    Pairs involving a topic entity are NOT merged here: union-find closure is
    transitive, so a same-name topic bridging e.g. a person and an org would
    collapse a pair this function explicitly forbids (live examples: "Lindy"
    person+org+topic, "nyc" place+org+topic). Topic absorption is handled
    separately by attaching each topic entity to exactly ONE concrete entity.
    """
    if a.entity_type in NO_NAME_MERGE_TYPES or b.entity_type in NO_NAME_MERGE_TYPES:
        return None
    if a.entity_type == b.entity_type:
        return 0.95
    types = {a.entity_type, b.entity_type}
    if "topic" in types:
        return None
    if types <= CONCEPT_TYPES:
        return 0.9
    # person/place vs org etc.: too risky on name alone
    return None


# Preference order for which concrete entity absorbs a same-name topic entity.
_TOPIC_ABSORB_PRIORITY = ["person", "org", "project", "product", "event", "place", "work_of_art"]


def _topic_absorb_target(topic: _RawEntity, group: List[_RawEntity]) -> Optional[_RawEntity]:
    """Pick the single concrete same-name entity that absorbs a topic entity."""
    concrete = [
        e
        for e in group
        if e.entity_id != topic.entity_id
        and e.entity_type != "topic"
        and e.entity_type not in NO_NAME_MERGE_TYPES
    ]
    if not concrete:
        return None
    concrete.sort(
        key=lambda e: (
            _TOPIC_ABSORB_PRIORITY.index(e.entity_type)
            if e.entity_type in _TOPIC_ABSORB_PRIORITY
            else len(_TOPIC_ABSORB_PRIORITY),
            -e.mention_count,
            e.entity_id,
        )
    )
    return concrete[0]


def build_canonical_index(conn: sqlite3.Connection) -> CanonicalIndex:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entities' LIMIT 1"
    ).fetchone()
    if row is None:
        return CanonicalIndex()

    raw: Dict[str, _RawEntity] = {}
    for r in conn.execute(
        """
        SELECT entity_id, entity_type, canonical_name, normalized_name,
               aliases_json, is_self, mention_count
        FROM entities
        """
    ).fetchall():
        try:
            aliases = [str(a) for a in json.loads(str(r["aliases_json"] or "[]"))]
        except (json.JSONDecodeError, TypeError):
            aliases = []
        entity = _RawEntity(
            entity_id=str(r["entity_id"]),
            entity_type=str(r["entity_type"] or "unknown").lower(),
            canonical_name=str(r["canonical_name"] or r["entity_id"]),
            normalized_name=_normalize(str(r["normalized_name"] or r["canonical_name"] or "")),
            aliases=aliases,
            is_self=bool(r["is_self"]),
            mention_count=int(r["mention_count"] or 0),
        )
        raw[entity.entity_id] = entity

    uf = _UnionFind()
    pair_confidence: Dict[frozenset, float] = {}

    by_name: Dict[str, List[_RawEntity]] = {}
    for entity in raw.values():
        if entity.normalized_name:
            by_name.setdefault(entity.normalized_name, []).append(entity)

    for group in by_name.values():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                confidence = _mergeable(a, b)
                if confidence is None:
                    continue
                uf.union(a.entity_id, b.entity_id)
                pair_confidence[frozenset((a.entity_id, b.entity_id))] = confidence
        # Topic absorption: each topic entity joins exactly one concrete
        # same-name entity, so it can never bridge incompatible types.
        for topic in group:
            if topic.entity_type != "topic":
                continue
            target = _topic_absorb_target(topic, group)
            if target is None:
                continue
            uf.union(target.entity_id, topic.entity_id)
            pair_confidence[frozenset((topic.entity_id, target.entity_id))] = 0.9

    # Alias pass: entity B whose normalized name matches one of A's aliases.
    # Topic-typed pairs are excluded here too — alias merges are already the
    # lowest-confidence rule and a topic bridge would defeat the type checks.
    alias_owners: Dict[str, List[_RawEntity]] = {}
    for entity in raw.values():
        for alias in entity.aliases:
            normalized = _normalize(alias)
            if normalized and normalized != entity.normalized_name:
                alias_owners.setdefault(normalized, []).append(entity)
    for alias_name, owners in alias_owners.items():
        for candidate in by_name.get(alias_name, []):
            for owner in owners:
                if owner.entity_id == candidate.entity_id:
                    continue
                if "topic" in {owner.entity_type, candidate.entity_type}:
                    continue
                confidence = _mergeable(owner, candidate)
                if confidence is None:
                    continue
                uf.union(owner.entity_id, candidate.entity_id)
                key = frozenset((owner.entity_id, candidate.entity_id))
                pair_confidence[key] = min(0.85, confidence)

    groups: Dict[str, List[_RawEntity]] = {}
    for entity in raw.values():
        groups.setdefault(uf.find(entity.entity_id), []).append(entity)

    index = CanonicalIndex()
    for members in groups.values():
        members.sort(key=lambda e: (-e.mention_count, e.entity_id))
        rep = members[0]
        types = {m.entity_type for m in members}
        canonical_type = next((t for t in TYPE_PRIORITY if t in types), rep.entity_type)
        named = [m for m in members if m.entity_type == canonical_type]
        canonical_name = (named[0] if named else rep).canonical_name
        confidence = 1.0
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                pc = pair_confidence.get(frozenset((a.entity_id, b.entity_id)))
                if pc is not None:
                    confidence = min(confidence, pc)
        canonical = CanonicalEntity(
            canonical_id=rep.entity_id,
            canonical_name=canonical_name,
            canonical_type=canonical_type,
            members=[m.entity_id for m in members],
            member_types=types,
            merge_confidence=confidence if len(members) > 1 else 1.0,
            is_self=any(m.is_self for m in members),
            total_mentions=sum(m.mention_count for m in members),
        )
        index.entities[canonical.canonical_id] = canonical
        for m in members:
            index.to_canonical[m.entity_id] = canonical.canonical_id
    return index
