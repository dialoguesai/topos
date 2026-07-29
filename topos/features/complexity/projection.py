"""Analytical projection layer.

Metrics never read the raw derived graph directly. This module builds
time-bounded, evidence-weighted views:

  evidence weight = confidence x intentionality x time-decay,
  aggregated with per-source diminishing returns (1 + ln n) so repeated
  observations from one connector do not outweigh independent evidence.

Live-DB ground truth this is built on (2026-07-15): `timeline.entity_ids_json`
and `timeline.cluster_id` are empty; the usable joins are `entity_mentions`
(entity evidence with timestamps + confidence) and
`topic_cluster_members JOIN timeline ON record_id` (topic evidence).

`EvidenceCache` loads the raw evidence tables exactly once per engine call —
a snapshot computes ~17 windows (presets + personal-baseline history) and
re-scanning timeline/conversation_messages/entity_mentions per window
dominated runtime (loop-5 review finding).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

MIN_SANE_TS = "2000-01-01"

# Deliberate activity should outweigh passive exposure.
SOURCE_INTENT: Dict[str, float] = {
    "grow_journal": 1.0,
    "grow_data_file": 1.0,
    "time_log": 0.9,
    "github_activity": 0.9,
    "voxterm_transcripts": 0.8,
    "chatgpt": 0.7,
    "chatgpt_ui_conversation": 0.7,
    "chatgpt_file_ingestion": 0.7,
    "chatgpt_ingestion": 0.7,
    "imessage": 0.5,
    "browser_visits": 0.2,
    "browser_events": 0.2,
}

TABLE_INTENT: Dict[str, float] = {
    "journal_entries": 1.0,
    "ai_chat_messages": 0.7,
    "conversation_messages": 0.5,
    "location_events": 0.3,
    "activity_events": 0.3,
}

DEFAULT_INTENT = 0.4
SENT_MESSAGE_INTENT = 0.7
RECEIVED_MESSAGE_INTENT = 0.4
# The sent/received refinement only applies to sources whose base intent is
# the generic conversation default — voxterm transcripts also live in
# conversation_messages but are self-authored speech (0.8) and must not be
# demoted to sent/received values (loop-5 review finding).
SELF_OVERRIDE_MAX_BASE_INTENT = 0.5

# Semantic strength of edge labels: a chain of generic co-occurrence edges must
# not outrank an explicit worked_on / communicates_with relation.
RELATION_STRENGTH: Dict[str, float] = {
    "worked_on": 0.95,
    "works_on": 0.95,
    "communicates_with": 0.85,
    "discusses": 0.7,
    "located_at": 0.6,
    "practices": 0.6,
    "pursues": 0.5,
    "mentions": 0.35,
    "co_occurrence": 0.3,
    "relates_to": 0.15,
}
DEFAULT_RELATION_STRENGTH = 0.3
GENERIC_RELATIONS = {"mentions", "co_occurrence", "relates_to"}


@dataclass(frozen=True)
class ProjectionConfig:
    """One analytical window. window_days=None means lifetime."""

    window_days: Optional[int] = 30
    half_life_days: Optional[float] = None
    end: Optional[datetime] = None

    def resolved_half_life(self) -> float:
        if self.half_life_days and self.half_life_days > 0:
            return float(self.half_life_days)
        if self.window_days:
            return max(3.5, self.window_days / 2.0)
        return 365.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def sane_event(dt: Optional[datetime], end: datetime) -> bool:
    if dt is None:
        return False
    if dt.strftime("%Y-%m-%d") < MIN_SANE_TS:
        return False
    return dt <= end + timedelta(days=2)


def intent_weight(
    source_id: Optional[str],
    canonical_table: Optional[str] = None,
    *,
    is_from_self: Optional[bool] = None,
) -> float:
    key = (source_id or "").strip().lower()
    table = (canonical_table or "").strip().lower()
    base = SOURCE_INTENT.get(key)
    if base is None:
        base = TABLE_INTENT.get(table, DEFAULT_INTENT)
    if is_from_self is not None and base <= SELF_OVERRIDE_MAX_BASE_INTENT:
        return SENT_MESSAGE_INTENT if is_from_self else RECEIVED_MESSAGE_INTENT
    return base


def relation_strength(edge_type: Optional[str]) -> float:
    return RELATION_STRENGTH.get((edge_type or "").strip().lower(), DEFAULT_RELATION_STRENGTH)


def damped_source_sum(contributions: Iterable[Tuple[str, float]]) -> float:
    """Aggregate (source_key, value) with per-source diminishing returns.

    Each source contributes (1 + ln n) * mean(values) so evidence volume from a
    single connector saturates while independent sources still add up.
    """
    groups: Dict[str, List[float]] = {}
    for key, value in contributions:
        if value <= 0:
            continue
        groups.setdefault(key or "unknown", []).append(value)
    total = 0.0
    for values in groups.values():
        n = len(values)
        total += (1.0 + math.log(n)) * (sum(values) / n)
    return total


@dataclass
class EvidenceRecord:
    record_id: str
    source_id: str
    canonical_table: str
    event_at: datetime
    intent: float
    decay: float

    @property
    def weight(self) -> float:
        return self.intent * self.decay


@dataclass
class Edge:
    src: str
    dst: str
    edge_type: str
    strength: float
    decay: float
    evidence_count: int
    event_at: Optional[datetime]

    @property
    def volume(self) -> float:
        """Evidence volume without semantics: decay x saturating count."""
        return self.decay * (1.0 + math.log1p(max(0, self.evidence_count)))

    @property
    def score(self) -> float:
        return self.strength * self.volume

    @property
    def is_generic(self) -> bool:
        return self.edge_type in GENERIC_RELATIONS


@dataclass
class ItemEvidence:
    """Aggregated evidence for one canonical entity or supertopic."""

    weight: float = 0.0
    record_count: int = 0
    source_ids: Set[str] = field(default_factory=set)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    contributions: List[Tuple[str, float]] = field(default_factory=list)

    def add(self, source_id: str, value: float, event_at: Optional[datetime]) -> None:
        self.contributions.append((source_id, value))
        self.record_count += 1
        self.source_ids.add(source_id or "unknown")
        if event_at is not None:
            if self.first_seen is None or event_at < self.first_seen:
                self.first_seen = event_at
            if self.last_seen is None or event_at > self.last_seen:
                self.last_seen = event_at

    def finalize(self) -> None:
        self.weight = damped_source_sum(self.contributions)


@dataclass
class _RawTimelineRecord:
    record_id: str
    event_at: datetime
    source_id: str
    canonical_table: str


@dataclass
class _RawMention:
    entity_id: str
    record_id: str
    source_id: str
    canonical_table: str
    confidence: float
    event_at: Optional[datetime]


@dataclass
class _RawEdge:
    src: str
    dst: str
    edge_type: str
    weight: float
    evidence_count: int
    event_at: Optional[datetime]


class EvidenceCache:
    """One pass over the evidence tables, shared by every projection window.

    Timestamps are parsed here and the per-record maximum is taken over PARSED
    datetimes — SQL MAX() over mixed-offset ISO strings is lexicographic and
    picks the wrong chronological max (loop-5 review finding).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.timeline_records: Dict[str, _RawTimelineRecord] = {}
        self.mentions: List[_RawMention] = []
        self.edges: List[_RawEdge] = []
        self.self_sent: Dict[str, bool] = {}

        if _table_exists(conn, "conversation_messages"):
            try:
                for row in conn.execute(
                    "SELECT message_id, is_from_self FROM conversation_messages"
                ):
                    self.self_sent[str(row["message_id"])] = bool(row["is_from_self"])
            except sqlite3.OperationalError:
                self.self_sent = {}

        if _table_exists(conn, "timeline"):
            for row in conn.execute(
                """
                SELECT record_id, event_at, source_id, canonical_table
                FROM timeline WHERE event_at >= ?
                """,
                (MIN_SANE_TS,),
            ):
                event_at = parse_ts(row["event_at"])
                if event_at is None:
                    continue
                record_id = str(row["record_id"])
                existing = self.timeline_records.get(record_id)
                if existing is None or event_at > existing.event_at:
                    self.timeline_records[record_id] = _RawTimelineRecord(
                        record_id=record_id,
                        event_at=event_at,
                        source_id=str(row["source_id"] or ""),
                        canonical_table=str(row["canonical_table"] or ""),
                    )

        if _table_exists(conn, "entity_mentions"):
            for row in conn.execute(
                """
                SELECT entity_id, record_id, source_id, canonical_table, confidence, event_at
                FROM entity_mentions
                """
            ):
                self.mentions.append(
                    _RawMention(
                        entity_id=str(row["entity_id"]),
                        record_id=str(row["record_id"] or ""),
                        source_id=str(row["source_id"] or ""),
                        canonical_table=str(row["canonical_table"] or ""),
                        confidence=float(row["confidence"] or 0.8),
                        event_at=parse_ts(row["event_at"]),
                    )
                )

        if _table_exists(conn, "entity_edges"):
            for row in conn.execute(
                """
                SELECT src_entity_id, dst_entity_id, edge_type, weight, evidence_count,
                       COALESCE(last_event_at, valid_from, created_at) AS event_at
                FROM entity_edges
                WHERE valid_to IS NULL
                """
            ):
                self.edges.append(
                    _RawEdge(
                        src=str(row["src_entity_id"]),
                        dst=str(row["dst_entity_id"]),
                        edge_type=str(row["edge_type"] or "").lower(),
                        weight=float(row["weight"] or 1.0),
                        evidence_count=int(row["evidence_count"] or 0),
                        event_at=parse_ts(row["event_at"]),
                    )
                )

    def record_intent(self, record: _RawTimelineRecord) -> float:
        is_from_self = self.self_sent.get(record.record_id)
        return intent_weight(
            record.source_id, record.canonical_table, is_from_self=is_from_self
        )


class AnalyticalProjection:
    """Windowed, decayed, intentionality-weighted view over the evidence tables.

    canonical: CanonicalIndex (canonical.py); supertopics: SuperTopicIndex
    (topic_merge.py). Both are window-independent and shared across windows,
    as is the EvidenceCache.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: ProjectionConfig,
        *,
        canonical: Any,
        supertopics: Any,
        cache: Optional[EvidenceCache] = None,
    ) -> None:
        self.conn = conn
        self.config = config
        self.canonical = canonical
        self.supertopics = supertopics
        self.cache = cache or EvidenceCache(conn)
        self.end = config.end or utc_now()
        self.start = (
            self.end - timedelta(days=config.window_days) if config.window_days else None
        )
        self._lambda = math.log(2) / config.resolved_half_life()
        self._entity_evidence: Optional[Dict[str, ItemEvidence]] = None
        self._topic_evidence: Optional[Dict[str, ItemEvidence]] = None
        self._records: Optional[Dict[str, EvidenceRecord]] = None
        self._record_entities: Optional[Dict[str, Set[str]]] = None
        self._edges: Optional[List[Edge]] = None

    # -- primitives -------------------------------------------------------

    def in_window(self, dt: Optional[datetime]) -> bool:
        if not sane_event(dt, self.end):
            return False
        if dt > self.end:
            return False
        return self.start is None or dt >= self.start

    def decay(self, dt: datetime) -> float:
        delta_days = max(0.0, (self.end - dt).total_seconds() / 86400.0)
        return math.exp(-self._lambda * delta_days)

    # -- record index ------------------------------------------------------

    def records(self) -> Dict[str, EvidenceRecord]:
        """Windowed record index from timeline (deduped per record_id)."""
        if self._records is not None:
            return self._records
        out: Dict[str, EvidenceRecord] = {}
        for raw in self.cache.timeline_records.values():
            if not self.in_window(raw.event_at):
                continue
            out[raw.record_id] = EvidenceRecord(
                record_id=raw.record_id,
                source_id=raw.source_id,
                canonical_table=raw.canonical_table,
                event_at=raw.event_at,
                intent=self.cache.record_intent(raw),
                decay=self.decay(raw.event_at),
            )
        self._records = out
        return out

    # -- entity evidence ----------------------------------------------------

    def entity_evidence(self) -> Dict[str, ItemEvidence]:
        """canonical_id -> evidence from entity_mentions (windowed, weighted)."""
        if self._entity_evidence is not None:
            return self._entity_evidence
        evidence: Dict[str, ItemEvidence] = {}
        record_entities: Dict[str, Set[str]] = {}
        records = self.records()
        for mention in self.cache.mentions:
            canonical_id = self.canonical.resolve(mention.entity_id)
            if canonical_id is None:
                continue
            event_at = mention.event_at
            rec = records.get(mention.record_id) if mention.record_id else None
            if event_at is None and rec is not None:
                event_at = rec.event_at
            if not self.in_window(event_at):
                continue
            source_id = mention.source_id or (rec.source_id if rec else "")
            intent = (
                rec.intent
                if rec is not None
                else intent_weight(source_id, mention.canonical_table)
            )
            value = mention.confidence * intent * self.decay(event_at)
            evidence.setdefault(canonical_id, ItemEvidence()).add(source_id, value, event_at)
            if mention.record_id:
                record_entities.setdefault(mention.record_id, set()).add(canonical_id)
        for item in evidence.values():
            item.finalize()
        self._entity_evidence = evidence
        self._record_entities = record_entities
        return evidence

    def record_entity_map(self) -> Dict[str, Set[str]]:
        """record_id -> canonical entity ids co-mentioned in it (windowed)."""
        if self._record_entities is None:
            self.entity_evidence()
        return self._record_entities or {}

    # -- topic evidence -----------------------------------------------------

    def topic_evidence(self) -> Dict[str, ItemEvidence]:
        """supertopic_id -> evidence over DISTINCT records (windowed, weighted)."""
        if self._topic_evidence is not None:
            return self._topic_evidence
        evidence: Dict[str, ItemEvidence] = {}
        records = self.records()
        for supertopic in self.supertopics.supertopics.values():
            item = ItemEvidence()
            for record_id in supertopic.record_ids:
                rec = records.get(record_id)
                if rec is None:
                    continue
                item.add(rec.source_id, rec.weight, rec.event_at)
            if item.record_count:
                item.finalize()
                evidence[supertopic.supertopic_id] = item
        self._topic_evidence = evidence
        return evidence

    # -- weekly activity series ----------------------------------------------

    def weekly_series(
        self, week_starts_list: List[str]
    ) -> Tuple[Dict[str, List[int]], Dict[str, List[int]]]:
        """(entity weekly mention counts, supertopic weekly record counts).

        Counts are unweighted (for MI/volatility); membership is canonical and
        restricted to sane timestamps.
        """
        bins: List[Tuple[datetime, datetime]] = []
        for start in week_starts_list:
            start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            bins.append((start_dt, start_dt + timedelta(days=7)))

        def bin_index(dt: Optional[datetime]) -> Optional[int]:
            if dt is None:
                return None
            for i, (lo, hi) in enumerate(bins):
                if lo <= dt < hi:
                    return i
            return None

        entity_series: Dict[str, List[int]] = {}
        for mention in self.cache.mentions:
            idx = bin_index(mention.event_at)
            if idx is None:
                continue
            canonical_id = self.canonical.resolve(mention.entity_id)
            if canonical_id is None:
                continue
            series = entity_series.setdefault(canonical_id, [0] * len(bins))
            series[idx] += 1

        topic_series: Dict[str, List[int]] = {}
        for supertopic in self.supertopics.supertopics.values():
            series = [0] * len(bins)
            hit = False
            for record_id in supertopic.record_ids:
                raw = self.cache.timeline_records.get(record_id)
                idx = bin_index(raw.event_at if raw else None)
                if idx is None:
                    continue
                series[idx] += 1
                hit = True
            if hit:
                topic_series[supertopic.supertopic_id] = series
        return entity_series, topic_series

    # -- edges ----------------------------------------------------------------

    def edges(self) -> List[Edge]:
        """Canonicalized, typed, decayed entity edges, restricted to the window.

        Edges whose endpoints canonicalize to the same node (duplicate-entity
        artifacts) are dropped. Out-of-window edges are excluded (a windowed
        graph must not silently contain the whole history — loop-5 review
        finding); callers that need history fall back to a lifetime
        projection. Edges without any usable timestamp are kept at a neutral
        discounted decay.
        """
        if self._edges is not None:
            return self._edges
        out: List[Edge] = []
        for raw in self.cache.edges:
            src = self.canonical.resolve(raw.src)
            dst = self.canonical.resolve(raw.dst)
            if src is None or dst is None or src == dst:
                continue
            event_at = raw.event_at
            if event_at is not None and not sane_event(event_at, self.end):
                event_at = None
            if event_at is not None:
                if not self.in_window(event_at):
                    continue
                decay = self.decay(event_at)
            else:
                decay = 0.3
            evidence_count = raw.evidence_count or int(raw.weight or 1.0)
            out.append(
                Edge(
                    src=src,
                    dst=dst,
                    edge_type=raw.edge_type,
                    strength=relation_strength(raw.edge_type),
                    decay=decay,
                    evidence_count=evidence_count,
                    event_at=event_at,
                )
            )
        self._edges = out
        return out
