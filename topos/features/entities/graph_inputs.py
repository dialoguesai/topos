"""Everything the entity-graph rebuild reads, as one fingerprint.

The refresher rebuilt the graph whenever anything marked it dirty, and the marks
do not say whether anything changed. On the owner's node (1.4.3, 2 Oct 2026) the
post-canonical pipeline marked it for every browser-visit batch (33 in the hour
before the census copy), and each batch then asked for two full rebuilds: the
inline one before signal derivation and the debounced one after it. Between
09:20 and 10:20 that was 24 rebuild children and 15 finished rebuilds of about
130 seconds each, and every report carried the same counts. Of the 250 batches
in that copy's last 40 hours, 78 carried a change the graph reads.

``graph_input_fingerprint`` digests the rows and settings the rebuild reads. The
refresher stores it with each successful rebuild and skips the next one when
nothing in it has changed (``graph_refresh.rebuild_if_inputs_changed``).

Rules for this list:

* A superset is safe and a subset is not. An input left out means a change to it
  waits for some other change before the graph shows it. An input that is not
  read costs a rebuild that writes nothing. When the rebuild learns to read a
  new table, add it here; ``tests/features/test_graph_inputs.py`` fails until
  every table the rebuild modules name is either listed here or exempted there
  with a reason.
* Rows the rebuild writes itself are left out, or each rebuild would schedule
  the next: the evidence edges (``co_occurrence``, ``communicates_with``) and
  every ``mz`` edge, the ``mz`` vertices (goals, topics, conversations,
  recordings), the columns it recomputes on ordinary entities
  (``mention_count``, ``first_seen``, ``last_seen``, ``metadata_json``), the
  dossiers and the derived community names.
* Rows a background lane rewrites often without the graph reading them are left
  out too, the same way: new canonical rows count only once a mention, a topic
  member or a goal points at them. Otherwise every browser visit would rebuild
  the graph again.
* Each table digests as its row count plus the sum of its row hashes modulo
  2**64, so no part needs an ORDER BY. Each row hashes with its rowid, because
  the rebuild meets some rows in storage order ("the first row met leads a goal
  group"): a delete and reinsert of the same values is a change.
* A part that cannot be read for any reason but a missing table makes the whole
  fingerprint ``None``, which the refresher reads as "rebuild". A missing table
  is a stable state of a node and digests as such.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("topos.features.entities.graph_inputs")

#: Bump when the rebuild starts reading something this module does not cover,
#: or when the same inputs start producing a different graph within one release.
#: The package version is part of the fingerprint too, so every release rebuilds
#: once whatever this says.
GRAPH_INPUTS_VERSION = 1

#: Edge types the rebuild recomputes from mentions and participation.
EVIDENCE_EDGE_TYPES = ("co_occurrence", "communicates_with")

_MOD = 1 << 64

# SQL fragment: true for a row the rebuild materialized (metadata_json.mz = 1),
# false for every other row. Never NULL: under NOT, a NULL would drop the row
# from the digest (a row without "mz" made json_extract NULL, and the first
# draft of this digested no ordinary entity at all). json_valid first, so one
# malformed row cannot make the whole part unreadable.
_MZ_ROW = (
    "(CASE WHEN json_valid(metadata_json) "
    "THEN COALESCE(json_extract(metadata_json, '$.mz'), 0) ELSE 0 END = 1)"
)

# Columns `record_role` and the participation load read from a canonical row.
# Whichever of them a table has are digested.
_ROLE_COLUMNS = (
    "actor_role",
    "writer_class",
    "is_from_self",
    "sender_id",
    "sender_type",
    "activity_type",
    "event_type",
    "url",
    "record_type",
    "organization",
    "entry_at",
    "mood_tag",
    "transcript_id",
    "segment_id",
    "start_sec",
)
_PARTICIPATION_COLUMNS = (
    "conversation_id",
    "chat_id",
    "thread_id",
    "sender_id",
    "is_from_self",
    "actor_role",
    "event_at",
    "sender_type",
    "writer_class",
)
# `_record_role_map` keys a canonical row by the first of these it has.
_RECORD_ID_COLUMNS = ("record_id", "message_id", "entry_id", "event_id", "id")
_MESSAGE_TABLES = ("conversation_messages", "ai_chat_messages", "conversation_message")


class _Unreadable(Exception):
    """A part could not be read; the fingerprint is unknown."""


def _safe_name(name: str) -> bool:
    return bool(name) and name.replace("_", "").isalnum()


def _columns(conn: sqlite3.Connection, table: str) -> Optional[List[str]]:
    """The table's columns, or None when it does not exist."""
    if not _safe_name(table):
        raise _Unreadable(f"unsafe table name {table!r}")
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error as exc:
        raise _Unreadable(f"{table}: {type(exc).__name__}") from exc
    return [str(r[1]) for r in rows] or None


def _digest_rows(rows: Iterable[Sequence[object]]) -> Tuple[int, int]:
    count = 0
    total = 0
    for row in rows:
        h = hashlib.blake2b(repr(tuple(row)).encode("utf-8", "surrogatepass"), digest_size=8)
        total = (total + int.from_bytes(h.digest(), "big")) % _MOD
        count += 1
    return count, total


Part = Tuple[str, str]


def _table_part(
    conn: sqlite3.Connection,
    table: str,
    wanted: Optional[Sequence[str]] = None,
    where: str = "",
    params: Sequence[object] = (),
    key: Optional[str] = None,
) -> Part:
    """(key, digest) of ``table`` over ``wanted`` columns (all when None), rowid first."""
    name = key or table
    cols = _columns(conn, table)
    if cols is None:
        return name, "absent"
    chosen = cols if wanted is None else [c for c in wanted if c in cols]
    select = ", ".join(["rowid"] + chosen)
    sql = f"SELECT {select} FROM {table}" + (f" WHERE {where}" if where else "")
    try:
        count, total = _digest_rows(conn.execute(sql, tuple(params)))
    except sqlite3.Error as exc:
        raise _Unreadable(f"{table}: {type(exc).__name__}") from exc
    return name, f"cols={','.join(chosen)};where={where};rows={count};sum={total:016x}"


def _off_limits_part(conn: sqlite3.Connection) -> Part:
    """The Off-limits list as the rebuild reads it: ``_table_part`` over the same four columns, without the entries
    the upgrade carried that the owner has not acted on (the rebuild's readers take the owner's own view of the
    list, ``features.lifecycle.off_limits_view``).

    Byte for byte what ``_table_part`` returned for this table wherever no entry has a waiting mark, so the upgrade
    moves no fingerprint by itself. A carried entry enters the digest when the owner makes it a full entry; a full
    entry that holds waiting names is digested with its mark, so it moves when those are added and again when the
    owner acts."""
    import json

    table = "entity_blackholes"
    cols = _columns(conn, table)
    if cols is None:
        return table, "absent"
    chosen = [c for c in ("entity_id", "normalized_name", "canonical_name", "aliases_json") if c in cols]
    marked = "carried_waiting_json" in cols
    select = ", ".join(["rowid"] + chosen + (["carried_waiting_json"] if marked else []))

    def rows():
        for row in conn.execute(f"SELECT {select} FROM {table}"):
            if not marked or not row[-1]:
                yield row[:len(chosen) + 1]
                continue
            try:
                mark = json.loads(row[-1])
            except (TypeError, ValueError):
                mark = None
            if isinstance(mark, dict) and mark.get("whole") is True:
                continue
            yield row

    try:
        count, total = _digest_rows(rows())
    except sqlite3.Error as exc:
        raise _Unreadable(f"{table}: {type(exc).__name__}") from exc
    return table, f"cols={','.join(chosen)};where=;rows={count};sum={total:016x}"


def _value_part(name: str, value: object) -> Part:
    h = hashlib.blake2b(repr(value).encode("utf-8", "surrogatepass"), digest_size=16)
    return name, h.hexdigest()


# --- the parts ---------------------------------------------------------------------------------


def _row_parts(conn: sqlite3.Connection) -> List[Part]:
    """Stored rows the rebuild reads. See the module docstring for what is left out."""
    parts: List[Part] = []
    add = parts.append

    # Sightings: co-occurrence, mention counts and windows, dossiers, goal and
    # topic dating, the conversation lane, topic links.
    add(_table_part(conn, "entity_mentions"))

    # Ordinary entities, by the columns other writers own (names, types,
    # identifiers, the owner flag, the contact anchor). The rebuild's own
    # vertices and its recomputed columns are left out.
    add(_table_part(
        conn,
        "entities",
        ("entity_id", "entity_type", "canonical_name", "normalized_name",
         "aliases_json", "identifiers_json", "is_self", "contact_id"),
        where=f"NOT {_MZ_ROW}",
    ))

    # Edges other writers own (affinity, declared ChatGPT edges, part_of, ...):
    # communities, dossiers and the orphan sweep read every active edge.
    placeholders = ",".join("?" for _ in EVIDENCE_EDGE_TYPES)
    add(_table_part(
        conn,
        "entity_edges",
        None,
        where=f"edge_type NOT IN ({placeholders}) AND NOT {_MZ_ROW}",
        params=EVIDENCE_EDGE_TYPES,
    ))

    # Contact seeding, the orphan sweep's live-anchor test, participation.
    add(_table_part(conn, "contacts", ("contact_id", "display_name", "known_usernames_json", "is_self")))
    add(_table_part(conn, "contact_identifiers", ("contact_id", "identifier")))
    add(_table_part(conn, "conversation_participants", ("conversation_id", "contact_id")))
    add(_table_part(conn, "conversations", ("conversation_id",)))

    # Thread co-participation reads every message with a sender and a thread.
    for table in _MESSAGE_TABLES:
        cols = _columns(conn, table)
        if cols is None:
            add((table, "absent"))
            continue
        id_col = next((c for c in ("message_id", "record_id", "id") if c in cols), None)
        wanted = ([id_col] if id_col else []) + list(_PARTICIPATION_COLUMNS)
        add(_table_part(conn, table, wanted))

    # The provenance role of every record a mention names (`_record_role_map`).
    try:
        tables = sorted({
            str(r[0]) for r in conn.execute(
                "SELECT DISTINCT canonical_table FROM entity_mentions WHERE canonical_table IS NOT NULL"
            ) if r[0]
        })
    except sqlite3.Error as exc:
        if "no such table" in str(exc):
            tables = []
        else:
            raise _Unreadable(f"entity_mentions tables: {type(exc).__name__}") from exc
    for table in tables:
        key = f"role.{table}"
        if not _safe_name(table):
            add(_value_part(key, "unsafe table name"))
            continue
        cols = _columns(conn, table)
        if cols is None:
            add((key, "absent"))
            continue
        id_col = next((c for c in _RECORD_ID_COLUMNS if c in cols), None)
        if id_col is None:
            add((key, "no id column"))
            continue
        add(_table_part(
            conn,
            table,
            [id_col] + [c for c in _ROLE_COLUMNS if c != id_col],
            where=f"{id_col} IN (SELECT record_id FROM entity_mentions WHERE canonical_table = ?)",
            params=(table,),
            key=key,
        ))

    # Facts and topic clusters the materializer projects (and the owner pick,
    # which counts facts). Other object types are not graph inputs; dossiers
    # are the rebuild's own output.
    add(_table_part(
        conn,
        "signal_objects",
        ("object_id", "object_type", "object_key", "payload_json", "valid_from", "valid_to",
         "confidence", "source_refs_json"),
        where="object_type IN ('fact', 'top_topics')",
    ))
    add(_table_part(conn, "topic_clusters", ("cluster_id", "label")))
    add(_table_part(conn, "topic_cluster_members", ("cluster_id", "record_id", "created_at")))
    # Event times and roles of the records topic members and goals point at.
    has_members = _columns(conn, "topic_cluster_members") is not None
    member_or_goal = "record_id IN (SELECT record_id FROM topic_cluster_members)" if has_members else "0"
    if _columns(conn, "user_goals") is not None:
        member_or_goal += " OR record_id IN (SELECT record_id FROM user_goals)"
    add(_table_part(
        conn, "timeline", ("record_id", "event_at", "canonical_table", "source_id"), where=member_or_goal,
    ))
    add(_table_part(
        conn,
        "signal_embeddings",
        ("record_id", "event_at"),
        where="record_id IN (SELECT record_id FROM topic_cluster_members)" if has_members else "0",
    ))

    add(_table_part(conn, "user_goals", ("goal_id", "record_id", "source_id", "goal_text", "created_at")))
    add(_table_part(conn, "location_events", ("place_name", "event_at")))

    # Owner corrections the resolver and the sweeps obey.
    add(_off_limits_part(conn))
    add(_table_part(conn, "intelligence_exclusions", ("artifact_type", "artifact_key")))
    add(_table_part(
        conn, "entity_review", ("surface_text", "candidate_entity_id"),
        where="kind = 'no_bind' AND status = 'approved'",
    ))
    # Owner renames and retirements of community names; derived names are the
    # rebuild's own history.
    add(_table_part(
        conn, "community_names", ("name_id", "name", "fingerprint_json", "source", "retired_at"),
        where="source = 'owner' OR retired_at IS NOT NULL",
    ))
    # Dossier stat lines.
    add(_table_part(conn, "signal_facts", ("fact_id", "payload_json"), where="fact_id LIKE 'stat:messages.%'"))

    # Transcripts feed the discourse lanes.
    add(_table_part(conn, "transcripts"))
    add(_table_part(conn, "transcript_segments"))
    return parts


def _journal_goal_flags() -> Tuple[bool, bool]:
    from ...permissions_v2 import journal_goal_field
    from ...permissions_v2.evidence_families import family

    return bool(journal_goal_field.enabled()), bool(family("journal_entries").enabled())


def _computed_parts(conn: sqlite3.Connection) -> List[Part]:
    """Settings and derived values the rebuild consults, digested by value."""
    from ...__version__ import __version__ as release
    from .community_naming import naming_enabled, resolve_naming_model
    from .discourse_graph import discourse_enabled_source_ids
    from .owner import fact_owner_subject, owner_entity_id, owner_entity_ids
    from .resolver import value_label_surfaces

    parts: List[Part] = []
    parts.append(_value_part("version", (str(release), GRAPH_INPUTS_VERSION)))
    parts.append(_value_part("owner", (
        fact_owner_subject(conn), owner_entity_id(conn), sorted(owner_entity_ids(conn)),
    )))
    flags = _journal_goal_flags()
    parts.append(_value_part("journal_goal_flags", flags))
    if all(flags):
        # The goal lane reads whole journal entries only while both flags are on.
        parts.append(_table_part(conn, "journal_entries"))
    parts.append(_value_part("naming", (naming_enabled(), resolve_naming_model(conn))))
    # The model the goal lane clusters goal texts with.
    from ...engine.backends.huggingface import active_embedding_model

    parts.append(_value_part("goal_embedder", active_embedding_model()))
    parts.append(_value_part("discourse_sources", sorted(discourse_enabled_source_ids(conn))))
    parts.append(_value_part("value_surfaces", sorted(value_label_surfaces(conn))))
    parts.append(_value_part("postures", _postures(conn)))
    return parts


def _postures(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """The posture `_record_role_map` resolves for each source a role is computed for."""
    from ...sources.registry import effective_posture

    sources = set()
    for sql in (
        "SELECT DISTINCT source_id FROM entity_mentions",
        "SELECT DISTINCT t.source_id FROM timeline t "
        "JOIN topic_cluster_members m ON m.record_id = t.record_id",
    ):
        try:
            sources.update(str(r[0]) for r in conn.execute(sql) if r[0])
        except sqlite3.Error as exc:
            if "no such table" not in str(exc):
                raise _Unreadable(f"posture sources: {type(exc).__name__}") from exc
    return [(sid, str(effective_posture(sid, "", conn))) for sid in sorted(sources)]


_PARTS: Tuple[Callable[[sqlite3.Connection], List[Part]], ...] = (_row_parts, _computed_parts)


def graph_input_parts(conn: sqlite3.Connection) -> Dict[str, str]:
    """Each part's digest by name: the diagnostic for "why did the graph rebuild?".

    Raises what an unreadable part raises; ``graph_input_fingerprint`` is the
    caller that turns that into "unknown".
    """
    out: Dict[str, str] = {}
    for part in _PARTS:
        for name, value in part(conn):
            if name in out:  # never two parts under one name
                raise _Unreadable(f"duplicate graph input part {name!r}")
            out[name] = value
    return out


def graph_input_fingerprint(conn: sqlite3.Connection) -> Optional[str]:
    """Digest of everything the rebuild reads, or None when any of it is unreadable.

    Reads only. About 1.5s warm on a copy of the owner's database (2 Oct 2026:
    62k active edges, 97k messages, 37k mentions).
    """
    try:
        parts = graph_input_parts(conn)
    except _Unreadable as exc:
        logger.info("graph input fingerprint unavailable: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 -- unknown means "rebuild", never "skip"
        logger.info("graph input fingerprint unavailable: %s", type(exc).__name__)
        return None
    text = "\n".join(f"{name}={value}" for name, value in parts.items())
    return hashlib.blake2b(text.encode("utf-8"), digest_size=20).hexdigest()
