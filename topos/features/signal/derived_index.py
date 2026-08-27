"""Natural-language index over derived signal objects (Layer 3 -> retrieval).

The derivation layer's product lives in ``signal_objects``; the retrieval
layer reads ``signal_embeddings``. Nothing joined the two. Measured on the
first live node (2026-08-26):

    SELECT COUNT(*) FROM signal_embeddings e
    JOIN signal_objects o ON e.record_id = o.object_id   ->  0

of 9,213 objects. So the vector/FTS lane could only ever hand back raw source
records, and a derived answer was reachable only through a hand-curated alias
regex per question (``query/facts_direct.py``). "Who's in my close circle?"
returned a place called *Hood Circle* and three messages about shops being
close by, while 216 ``RelationshipEdge`` rows naming the actual people sat one
table away.

What this module does is the missing half: render each derived object as a
short sentence a person could have written, and embed THAT. The rendering is
the whole trick — a ``RelationshipEdge`` embedded as its JSON matches nothing
anybody would type, while the same edge embedded as

    "Mom - a person in my personal circle - family or a friend, not a
     colleague; one of the people I message the most; 412 messages
     received from them"

is reachable from "who's in my close circle", "who are my parents" and "who do
I talk to most" without any of those phrasings being anticipated.

Which words appear is load-bearing in both directions. A first draft said
"not a work contact", which put "work" in all 216 edge renderings — and "what
am I working on" then answered with seven people, because the lane's own
vocabulary made every person a match for every work question. The tier
distinction has to carry; the topical noun must not (see ``_TIER_PHRASE``).

Three object types are indexed. ``AvailabilityWindow`` is deliberately NOT one
of them: 401 rows of interval bands answer through the deterministic
availability path, and embedding them would put 401 near-identical neighbors
into the ANN index for no reachable question.

Identifiers never reach the rendered text. An edge keyed by a phone number is
resolved through ``contact_identifiers`` to that contact's display name, and
one that cannot be resolved (a bare UUID, an unknown number) is SKIPPED rather
than rendered as its key — a retrieval preview is a disclosure surface, and
"+1512…" is both a leak and a useless answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("topos.features.signal.derived_index")

#: Derived ``object_type`` -> the ``signal_embeddings.record_type`` it indexes as.
#: The record_type is the join key for every downstream consumer: the retrieval
#: split that routes these into their own fusion lane, the storage breakdown,
#: and the scrub. Prefixed so a future object type is additive.
DERIVED_RECORD_TYPES: Dict[str, str] = {
    "RelationshipEdge": "derived_relationship_edge",
    "entity_dossier": "derived_entity_dossier",
    "fact": "derived_fact",
}

DERIVED_RECORD_TYPE_SET = frozenset(DERIVED_RECORD_TYPES.values())

#: ``signal_embeddings.source_id`` for every derived row. Derived objects have
#: no connector of origin — they are what the node concluded, not what a source
#: sent — and stamping one of the real source ids would make them vanish behind
#: a source filter that was never meant to exclude them.
DERIVED_SOURCE_ID = "topos_derivation"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DIGITS_RE = re.compile(r"^\d{4,}$")

#: Diarization and import placeholders that are not people. Transcript sources
#: label turns "Speaker 1"/"Unknown 0" and the edge extractor faithfully makes a
#: RelationshipEdge out of each, so without this an answer to "who do I talk to
#: most" is three anonymous speaker slots ahead of the owner's actual contacts.
_PLACEHOLDER_NAME_RE = re.compile(r"^(speaker|unknown|user|participant|person|contact)([-\s]?\d+)?$", re.I)


def is_derived_record_type(value: Any) -> bool:
    return str(value or "") in DERIVED_RECORD_TYPE_SET


def _anon_entity_key(name: str) -> str:
    """Same slug the extractors key edges by (rule_extractors._anon_entity_key)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug or "unknown"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "null")
    except (TypeError, ValueError):
        return None


def _titleize(slug: str) -> str:
    """'andrew-miller' -> 'Andrew Miller'. Leaves already-cased names alone."""
    parts = [p for p in re.split(r"[-_\s]+", str(slug or "").strip()) if p]
    return " ".join(p if p[:1].isupper() else p.capitalize() for p in parts)


@dataclass
class DerivedRendering:
    """One derived object as the sentence that will be embedded."""

    object_id: str
    object_type: str
    object_key: str
    record_type: str
    signal_dimension: str
    #: The sentence a reader sees. Stored as text_preview/search_text.
    text: str
    #: Compact kind hint prepended for the ENCODER only, the same split
    #: `embed_context.build_embed_text` makes for canonical rows: it materially
    #: helps a short sentence match an ordinary question, and it is scaffolding
    #: a person should never be shown. Leaking it into the preview put
    #: "person I know | relationship" at the head of every answer.
    header: str
    title: str
    disclosure: Optional[str] = None
    event_at: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def embed_text(self) -> str:
        return f"{self.header}\n{self.text}" if self.header else self.text


class _NameResolver:
    """Key/entity-id -> display name, over one cached pass per table.

    Every lookup here is a JOIN the renderer would otherwise do per row (216
    edges x 3 lookups). Loaded lazily so a node with no contacts pays nothing.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._identifier_names: Optional[Dict[str, str]] = None
        self._entity_names: Optional[Dict[str, Tuple[str, bool]]] = None
        self._peer_message_counts: Optional[Dict[str, int]] = None

    def _query(self, sql: str) -> List[Any]:
        try:
            return self._conn.execute(sql).fetchall()
        except sqlite3.Error as exc:  # noqa: BLE001 — a missing table is not an error
            logger.debug("derived-index lookup skipped (%s)", exc)
            return []

    @property
    def identifier_names(self) -> Dict[str, str]:
        """Slugged contact identifier (phone/email) -> contact display name."""
        if self._identifier_names is None:
            out: Dict[str, str] = {}
            for identifier, display_name in self._query(
                "SELECT ci.identifier, c.display_name "
                "FROM contact_identifiers ci JOIN contacts c ON c.contact_id = ci.contact_id "
                "WHERE c.display_name IS NOT NULL AND TRIM(c.display_name) != ''"
            ):
                key = _anon_entity_key(str(identifier or ""))
                if key and key != "unknown":
                    out.setdefault(key, str(display_name).strip())
            self._identifier_names = out
        return self._identifier_names

    @property
    def entity_names(self) -> Dict[str, Tuple[str, bool]]:
        """entity_id -> (canonical_name, is_self)."""
        if self._entity_names is None:
            self._entity_names = {
                str(entity_id): (str(name or "").strip(), bool(is_self))
                for entity_id, name, is_self in self._query(
                    "SELECT entity_id, canonical_name, is_self FROM entities"
                )
            }
        return self._entity_names

    @property
    def peer_message_counts(self) -> Dict[str, int]:
        """Slugged sender_id -> inbound message count.

        This is the only place "who do I talk to most" becomes answerable FROM
        the derived layer: a RelationshipEdge carries bands, never a volume.
        Counted over non-self messages so the owner's own traffic does not make
        every peer look equally busy.
        """
        if self._peer_message_counts is None:
            out: Dict[str, int] = {}
            for sender_id, count in self._query(
                "SELECT sender_id, COUNT(*) FROM conversation_messages "
                "WHERE COALESCE(is_from_self, 0) = 0 AND sender_id IS NOT NULL "
                "GROUP BY sender_id"
            ):
                key = _anon_entity_key(str(sender_id or ""))
                if key and key != "unknown":
                    out[key] = out.get(key, 0) + int(count or 0)
            self._peer_message_counts = out
        return self._peer_message_counts

    def display_name_for_key(self, object_key: str) -> Optional[str]:
        """A human name for an edge key, or None when it is only an identifier.

        Returning None is a decision, not a failure: a bare UUID or an
        unresolvable phone number has no answer in it, and putting the raw key
        in the index would publish the identifier while retrieving nothing.
        """
        key = str(object_key or "").strip().lower()
        if not key or key == "unknown":
            return None
        resolved = self.identifier_names.get(key)
        if resolved and not _PLACEHOLDER_NAME_RE.match(resolved.strip()):
            return resolved
        if _UUID_RE.match(key) or _DIGITS_RE.match(key):
            return None
        if _PLACEHOLDER_NAME_RE.match(key.replace("-", " ").strip()):
            return None
        return _titleize(key)

    def entity_display(self, entity_id: str) -> Tuple[Optional[str], bool]:
        name, is_self = self.entity_names.get(str(entity_id or ""), ("", False))
        return (name or None), is_self


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------

#: Tier -> the clause that actually discriminates. Both halves matter: without
#: "not a work contact" a professional edge scores as well as a personal one on
#: "who's in my close circle", which is the failure this whole module exists to
#: fix.
_TIER_PHRASE = {
    "personal": "a person in my personal circle — family or a friend, not a colleague",
    "professional": "a colleague, a professional contact — not a personal friend",
}
#: Words deliberately absent from the phrases above: "work"/"working". The first
#: draft said "not a work contact" / "I know through work", which put that token
#: in all 216 edge renderings — and "what am I working on" then came back with
#: seven people in its top 25, because the lane's own vocabulary made every
#: person a match for every work question. The tier distinction is what has to
#: carry; the topical noun must not.

_WARMTH_PHRASE = {
    "high": "we are close",
    "medium": "moderately close",
    "low": "not especially close",
}

_CADENCE_PHRASE = {
    "recent": "in touch recently",
    "lapsed": "have not been in touch lately",
    "frequent": "in touch often",
}


def _volume_band(count: int, ranked_counts: List[int]) -> Optional[str]:
    """Where this peer sits in the owner's own message volume, said in words.

    "412 messages received from them" is a fact an embedding cannot rank: the
    encoder has no arithmetic, so a question phrased "who do I talk to most"
    matches the SHAPE of every edge equally and the numeral decides nothing.
    The band is the number made searchable. It is relative to this owner's own
    distribution — a fixed threshold would call everyone frequent on a light
    corpus and no one frequent on a heavy one.
    """
    if count <= 0 or not ranked_counts:
        return None
    top = ranked_counts[: max(1, len(ranked_counts) // 10)]
    if count >= top[-1]:
        return "one of the people I message the most"
    upper = ranked_counts[: max(1, len(ranked_counts) // 3)]
    if count >= upper[-1]:
        return "someone I message often"
    return None


def render_relationship_edge(
    obj: Dict[str, Any],
    resolver: _NameResolver,
    *,
    ranked_counts: Optional[List[int]] = None,
) -> Optional[DerivedRendering]:
    payload = obj.get("payload") or {}
    object_key = str(obj.get("object_key") or payload.get("target_entity_key") or "")
    name = resolver.display_name_for_key(object_key)
    if not name:
        return None

    clauses: List[str] = []
    tier = str(payload.get("tier") or "").strip().lower()
    if tier in _TIER_PHRASE:
        clauses.append(_TIER_PHRASE[tier])

    warmth = str(payload.get("warmth_band") or "").strip().lower()
    if warmth in _WARMTH_PHRASE:
        clauses.append(_WARMTH_PHRASE[warmth])

    cadence = str(payload.get("cadence_band") or "").strip().lower()
    if cadence in _CADENCE_PHRASE:
        clauses.append(_CADENCE_PHRASE[cadence])

    messages = resolver.peer_message_counts.get(_anon_entity_key(object_key), 0)
    if messages:
        band = _volume_band(messages, ranked_counts or [])
        if band:
            clauses.append(band)
        clauses.append(f"{messages} messages received from them")

    coactivity = str(payload.get("coactivity_band") or "").strip()
    if coactivity:
        clauses.append(f"shared activity: {coactivity}")

    tags = [str(t).strip() for t in (payload.get("context_tags") or []) if str(t).strip()]
    if tags:
        clauses.append("context: " + ", ".join(tags[:4]))

    body = f"{name} — " + "; ".join(clauses) if clauses else f"{name} — someone I know"
    return DerivedRendering(
        object_id=str(obj["object_id"]),
        object_type="RelationshipEdge",
        object_key=object_key,
        record_type=DERIVED_RECORD_TYPES["RelationshipEdge"],
        signal_dimension=str(obj.get("signal_dimension") or "relationships"),
        text=f"{body}.",
        header="person I know | relationship",
        title=name,
        disclosure="owner_only",
        extra={"person_name": name, "tier": tier or None, "message_count": messages or None},
    )


_DOSSIER_KIND_PHRASE = {
    "person": "a person I know",
    "org": "an organization in my life",
    "place": "a place in my life",
    "project": "a project I work on",
    "product": "a product I use",
    "event": "an event I was part of",
    # "…or work I engaged with" put the token in every work_of_art rendering and
    # "what am I working on" answered with three songs. Same collision as
    # _TIER_PHRASE, one dictionary over.
    "work_of_art": "a book, film or piece of art I engaged with",
    "topic": "a topic I engage with",
}


def render_entity_dossier(
    obj: Dict[str, Any], resolver: _NameResolver
) -> Optional[DerivedRendering]:
    payload = obj.get("payload") or {}
    name = str(payload.get("canonical_name") or "").strip().lstrip("-–—•*").strip()
    if not name:
        return None
    entity_type = str(payload.get("entity_type") or "").strip().lower()

    parts = [f"{name} — {_DOSSIER_KIND_PHRASE.get(entity_type, entity_type or 'something I engage with')}"]
    summary = str(payload.get("summary_text") or "").strip()
    if summary:
        # The stored summary already opens with "<name> — <type>; N mentions…";
        # keep only the tail so the sentence does not say the name three times.
        tail = summary.split("—", 1)[-1].strip() if "—" in summary else summary
        # Drop the leading bare type token the stored form starts with.
        tail = re.sub(r"^%s\s*;\s*" % re.escape(entity_type), "", tail).strip()
        if tail:
            parts.append(tail.rstrip("."))

    connections = [
        str(c.get("canonical_name") or "").strip()
        for c in (payload.get("top_connections") or [])
        if isinstance(c, dict) and str(c.get("canonical_name") or "").strip()
    ]
    if connections:
        parts.append("connected to " + ", ".join(connections[:5]))

    return DerivedRendering(
        object_id=str(obj["object_id"]),
        object_type="entity_dossier",
        object_key=str(obj.get("object_key") or ""),
        record_type=DERIVED_RECORD_TYPES["entity_dossier"],
        signal_dimension=str(obj.get("signal_dimension") or "relationships"),
        text="; ".join(parts) + ".",
        header=f"{entity_type or 'entity'} profile | who and what I engage with",
        title=name,
        disclosure="owner_only",
        extra={"entity_id": payload.get("entity_id"), "entity_type": entity_type or None},
    )


#: Predicate -> the phrase a person would use for it. The fallback below
#: humanizes anything unmapped, so a new ontology pack indexes on arrival
#: instead of waiting for an entry here — the curated-alias failure mode this
#: module replaces must not reappear one level down.
_PREDICATE_PHRASE = {
    "rel.relationship": "family and relationships",
    "rel.relationship_event": "something that happened with someone I know",
    "rel.caregiving": "caregiving and looking after someone",
    "mind.self_reported_state": "how I said I was feeling",
    "work.project": "a project I work on",
    "work.career_event": "something that happened in my career",
    "work.employment_shape": "how I work",
    "health.medication": "medication I take",
}


def _humanize_predicate(predicate: str) -> str:
    tail = str(predicate or "").split(".")[-1]
    return tail.replace("_", " ").strip() or "fact"


#: `"key": "value"` pairs recovered from a JSON string that will not parse.
#: Some stored object_values are TRUNCATED mid-string (the writer clipped them),
#: so `json.loads` fails and the raw text would otherwise be embedded with its
#: braces and escapes intact — punctuation the encoder spends attention on and
#: a reader has to look past.
_JSON_PAIR_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"((?:[^"\\]|\\.)*)')


def _salvage_json_text(raw: str) -> str:
    pairs = _JSON_PAIR_RE.findall(raw)
    if not pairs:
        return raw.strip().strip("{}").strip()
    return "; ".join(
        value.replace('\\"', '"').replace("\\n", " ").strip()
        if key in ("description", "report", "summary", "text")
        else f"{key.replace('_', ' ')}: {value.strip()}"
        for key, value in pairs
        if value.strip()
    )


def _fact_detail(value: Any) -> str:
    """Flatten a fact's object_value into a readable clause."""
    parsed = _loads(value) if isinstance(value, str) else value
    if parsed is None and isinstance(value, str) and value.lstrip().startswith("{"):
        return _salvage_json_text(value)
    if isinstance(parsed, dict):
        person = str(parsed.get("person") or "").strip()
        role = str(parsed.get("role") or "").strip()
        lead = f"{person} is my {role}" if person and role else ""
        rest = []
        for key, raw in parsed.items():
            if key in ("person", "role") and lead:
                continue
            text = str(raw or "").strip()
            if not text:
                continue
            if key in ("description", "report", "summary", "text"):
                rest.append(text)
            else:
                rest.append(f"{key.replace('_', ' ')}: {text}")
        return "; ".join([p for p in ([lead] + rest) if p])
    if isinstance(parsed, list):
        return ", ".join(str(v) for v in parsed if str(v).strip())
    return str(value or "").strip()


def render_fact(obj: Dict[str, Any], resolver: _NameResolver) -> Optional[DerivedRendering]:
    payload = obj.get("payload") or {}
    predicate = str(payload.get("predicate") or "").strip()
    detail = _fact_detail(payload.get("object_value"))
    if not predicate or not detail:
        return None

    subject_name, subject_is_self = resolver.entity_display(
        str(payload.get("subject_entity_id") or "")
    )
    subject = "I" if (subject_is_self or not subject_name) else subject_name
    topic = _PREDICATE_PHRASE.get(predicate, _humanize_predicate(predicate))

    occurred = str(payload.get("occurred_at") or payload.get("event_at") or "")[:10]
    date_clause = f" ({occurred})" if occurred else ""
    body = f"{subject} — {_humanize_predicate(predicate)}: {detail.rstrip(' .;')}{date_clause}"
    return DerivedRendering(
        object_id=str(obj["object_id"]),
        object_type="fact",
        object_key=str(obj.get("object_key") or ""),
        record_type=DERIVED_RECORD_TYPES["fact"],
        signal_dimension=str(obj.get("signal_dimension") or "profile"),
        text=f"{body}.",
        header=f"{topic} | fact I stated",
        title=f"{subject} {_humanize_predicate(predicate)}",
        # Facts carry their OWN disclosure; the derivation writer decided it and
        # this index must not widen it by defaulting to something softer.
        disclosure=str(payload.get("disclosure") or "owner_only"),
        extra={"predicate": predicate, "pack": payload.get("pack")},
    )


_RENDERERS = {
    "RelationshipEdge": render_relationship_edge,
    "entity_dossier": render_entity_dossier,
    "fact": render_fact,
}


def render_object(
    obj: Dict[str, Any],
    resolver: _NameResolver,
    *,
    ranked_counts: Optional[List[int]] = None,
) -> Optional[DerivedRendering]:
    object_type = str(obj.get("object_type") or "")
    renderer = _RENDERERS.get(object_type)
    if renderer is None:
        return None
    if object_type == "RelationshipEdge":
        return renderer(obj, resolver, ranked_counts=ranked_counts)
    return renderer(obj, resolver)


# --------------------------------------------------------------------------
# Selection + indexing
# --------------------------------------------------------------------------


def load_indexable_objects(
    conn: sqlite3.Connection, *, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Active derived objects of the indexed types, newest revision first."""
    types = list(DERIVED_RECORD_TYPES)
    placeholders = ",".join("?" for _ in types)
    sql = (
        "SELECT object_id, signal_dimension, object_type, object_key, payload_json, updated_at "
        "FROM signal_objects "
        f"WHERE valid_to IS NULL AND object_type IN ({placeholders}) "
        "ORDER BY updated_at DESC"
    )
    params: List[Any] = list(types)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "object_id": row[0],
            "signal_dimension": row[1],
            "object_type": row[2],
            "object_key": row[3],
            "payload": _loads(row[4]) or {},
            "updated_at": row[5],
        }
        for row in rows
    ]


def _existing_hashes(conn: sqlite3.Connection, model: str) -> Dict[str, str]:
    """record_id -> content_hash for rows this index already wrote.

    The re-index skip is a content comparison, never a timestamp one: a
    dossier is superseded on every refresh whether or not its text changed,
    and re-embedding 169 unchanged sentences per pass is pure heat.
    """
    placeholders = ",".join("?" for _ in DERIVED_RECORD_TYPE_SET)
    try:
        rows = conn.execute(
            "SELECT record_id, content_hash FROM signal_embeddings "
            f"WHERE model = ? AND record_type IN ({placeholders})",
            (model, *sorted(DERIVED_RECORD_TYPE_SET)),
        ).fetchall()
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.debug("derived-index hash read skipped (%s)", exc)
        return {}
    return {str(r[0]): str(r[1] or "") for r in rows}


def prune_orphaned_derived_embeddings(conn: sqlite3.Connection) -> int:
    """Drop derived index rows whose object is gone or closed. SQL only.

    The full pass prunes too, but only when it next runs — and a SCRUB cannot
    wait for the next enrichment batch. Deleting a person's records closes the
    signal_objects that described them; until this runs, the index still holds
    a sentence naming them, and retrieval would still answer with it. So the
    scrub calls this directly (`lifecycle/derived_scrub.sweep_orphans`).

    Deliberately model-free and commit-free: it must be callable from inside a
    caller's write hold without loading an encoder or ending their transaction.
    """
    from ...storage.adapters.sqlite.vector_search import delete_vec_rows

    placeholders = ",".join("?" for _ in DERIVED_RECORD_TYPE_SET)
    params = sorted(DERIVED_RECORD_TYPE_SET)
    try:
        stale = [
            str(row[0])
            for row in conn.execute(
                f"""
                SELECT e.embedding_id FROM signal_embeddings e
                WHERE e.record_type IN ({placeholders})
                  AND NOT EXISTS (
                    SELECT 1 FROM signal_objects o
                     WHERE o.object_id = e.record_id AND o.valid_to IS NULL
                  )
                """,
                params,
            ).fetchall()
        ]
    except sqlite3.Error as exc:  # noqa: BLE001 — a missing table is not an error
        logger.debug("derived-index orphan scan skipped (%s)", exc)
        return 0
    if not stale:
        return 0
    for start in range(0, len(stale), 200):
        chunk = stale[start : start + 200]
        marks = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM signal_embeddings WHERE embedding_id IN ({marks})", chunk)
        delete_vec_rows(conn, chunk)
    return len(stale)


def _dedupe_renderings(renderings: List[DerivedRendering]) -> List[DerivedRendering]:
    """One row per person, not one per key that reaches them.

    The same person arrives twice — once keyed by name from a journal entry
    ("camille") and once by phone from a message thread ("15126500198" ->
    "Alpine Xray"). Two near-identical vectors for one person is a wasted ANN
    neighbor and a duplicated answer; the richer rendering wins.
    """
    best: Dict[str, DerivedRendering] = {}
    out: List[DerivedRendering] = []
    for rendering in renderings:
        if rendering.object_type != "RelationshipEdge":
            out.append(rendering)
            continue
        key = _anon_entity_key(rendering.title)
        incumbent = best.get(key)
        if incumbent is None or len(rendering.text) > len(incumbent.text):
            best[key] = rendering
    return out + list(best.values())


def index_derived_objects(
    conn: sqlite3.Connection,
    *,
    vector_index: Any = None,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    batch_size: int = 32,
    force: bool = False,
) -> Dict[str, int]:
    """Embed derived objects into ``signal_embeddings``. Idempotent.

    Incremental by content hash, so this is BOTH the backfill and the ongoing
    write path — there is no one-shot ladder step to be undone by the next
    producer run (the failure mode recorded in the withdrawal-steps finding).
    Run it again after any derivation pass and only changed objects re-embed.
    """
    from ...engine.backends.huggingface import HuggingFaceAdapter, active_embedding_model
    from ...storage.adapters.sqlite.stores import SQLiteVectorIndex
    from .vector_settings import derived_object_index_enabled

    from ...storage.db.write_gate import batched_writes

    if not derived_object_index_enabled():
        return {"considered": 0, "rendered": 0, "skipped_unrenderable": 0,
                "unchanged": 0, "written": 0, "pruned": 0, "disabled": 1}

    embed_model = model or active_embedding_model()
    index = vector_index if vector_index is not None else SQLiteVectorIndex(conn)
    resolver = _NameResolver(conn)

    counts = {
        "considered": 0,
        "rendered": 0,
        "skipped_unrenderable": 0,
        "unchanged": 0,
        "written": 0,
        "pruned": 0,
    }

    objects = load_indexable_objects(conn, limit=limit)
    counts["considered"] = len(objects)

    ranked_counts = sorted(resolver.peer_message_counts.values(), reverse=True)
    renderings: List[DerivedRendering] = []
    for obj in objects:
        rendered = render_object(obj, resolver, ranked_counts=ranked_counts)
        if rendered is None:
            counts["skipped_unrenderable"] += 1
            continue
        renderings.append(rendered)
    renderings = _dedupe_renderings(renderings)
    counts["rendered"] = len(renderings)

    indexed = _existing_hashes(conn, embed_model)

    # Prune before writing. A dossier is SUPERSEDED on every refresh — new
    # object_id, new row — and an object that stops being renderable (a name
    # that turns out to be a diarization placeholder, a scrubbed edge) leaves a
    # row behind that no producer will ever touch again. Without this the index
    # only ever grows, and it answers with people who are no longer in the data.
    # Scoped to a full pass: a `limit`ed run has not seen the whole set and
    # cannot tell an absent object from an unvisited one.
    if limit is None:
        from ...storage.db.write_gate import batched_writes

        live_ids = {r.object_id for r in renderings}
        stale = [record_id for record_id in indexed if record_id not in live_ids]
        if stale:
            with batched_writes(conn):
                for record_id in stale:
                    index.delete_by_record(record_id)
        counts["pruned"] = len(stale)

    known = {} if force else indexed
    pending = [r for r in renderings if known.get(r.object_id) != _content_hash(r.embed_text)]
    counts["unchanged"] = len(renderings) - len(pending)
    if not pending:
        return counts

    adapter = HuggingFaceAdapter()
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        result = adapter.run_inference(
            {"texts": [r.embed_text for r in chunk], "batch_size": batch_size},
            {"subtype": "embedding", "model": embed_model, "input_role": "passage"},
        )
        vectors = result.get("vectors") or []
        if len(vectors) != len(chunk):
            logger.warning(
                "derived-index embedding returned %d vectors for %d texts; batch skipped",
                len(vectors),
                len(chunk),
            )
            continue
        # One gate hold and one commit per batch, not per row. A first-time
        # backfill on a well-derived node is thousands of objects, and
        # thousands of separate transactions is thousands of chances for
        # another writer to queue behind this one.
        with batched_writes(conn):
            for rendering, vector in zip(chunk, vectors):
                index.upsert(
                    {
                        "record_id": rendering.object_id,
                        "source_id": DERIVED_SOURCE_ID,
                        "signal_dimension": rendering.signal_dimension,
                        "model": embed_model,
                        "provider": result.get("provider", "huggingface"),
                        "dims": result.get("dims"),
                        # The rendered sentence IS the content here — unlike a raw
                        # record there is no longer text behind the preview, so
                        # preview and search_text are the same string.
                        "text_preview": rendering.text[:200],
                        "search_text": rendering.text[:2000],
                        "content_hash": _content_hash(rendering.embed_text),
                        "chunk_index": 0,
                        # Deliberately undated: these are CURRENT-STATE objects. An
                        # event_at would put them under the retrieval time-window
                        # filter and the recency decay, both of which would be
                        # answering a question nobody asked of a standing fact.
                        "event_at": None,
                        "record_type": rendering.record_type,
                        "object_type": rendering.object_type,
                        "object_key": rendering.object_key,
                        "title": rendering.title,
                        "disclosure": rendering.disclosure,
                        **{k: v for k, v in rendering.extra.items() if v is not None},
                    },
                    vector=[float(x) for x in vector],
                )
                counts["written"] += 1
    return counts


def main(argv: Optional[List[str]] = None) -> int:
    """Backfill entry point: ``python -m topos.features.signal.derived_index``.

    The enrichment job of the same name keeps the index current from the next
    batch onward, and the ``derived_object_index`` rebuild target runs this
    from an upgrade manifest. This is the third door — the one that works on a
    node right now, without waiting for either. All three call the same
    idempotent pass, so running more than one of them costs a scan.
    """
    import argparse

    from ...core.state import get_db_connection

    parser = argparse.ArgumentParser(description="Backfill the derived-object index.")
    parser.add_argument("--limit", type=int, default=None,
                        help="index at most N objects (skips the orphan prune, "
                             "which needs a full pass to tell absent from unvisited)")
    parser.add_argument("--force", action="store_true",
                        help="re-embed even unchanged objects (after a rendering change)")
    args = parser.parse_args(argv)

    conn = get_db_connection()
    if conn is None:
        print("no database connection", flush=True)
        return 1
    counts = index_derived_objects(conn, limit=args.limit, force=args.force)
    print(json.dumps(counts, indent=1), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
