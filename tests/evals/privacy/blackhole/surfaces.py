"""Reference filtered readers — one per leak surface.

Each function answers "what would this caller receive from this surface?" by
reading the corpus and applying `BlackholeGuard` the way the real serving path
must. They are the executable specification for the M1 wiring: when a read path
in the engine is hooked up, it should filter the way its twin here does, and the
battery proves the twin is sufficient.

Grouped by the mechanism they rely on, because the mechanism is the interesting
part:

* **id-joinable** — the entity id is on the row, or one join away. Exact.
* **name-string** — the artifact carries the name as prose with no id anywhere.
  Only a scan can catch these, which is why the entity's canonical name is rare
  by construction.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, List

from topos.features.lifecycle.blackhole_guard import BlackholeGuard

Reader = Callable[[sqlite3.Connection, BlackholeGuard], Any]


# ---------------------------------------------------------- id-joinable


def read_entities(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    rows = [
        {"entity_id": r[0], "canonical_name": r[1], "aliases_json": r[2]}
        for r in conn.execute("SELECT entity_id, canonical_name, aliases_json FROM entities")
    ]
    return guard.filter_rows(rows, id_keys=("entity_id",))


def read_mentions(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    rows = [
        {"entity_id": r[0], "surface_text": r[1], "record_id": r[2]}
        for r in conn.execute("SELECT entity_id, surface_text, record_id FROM entity_mentions")
    ]
    return guard.filter_rows(rows, id_keys=("entity_id",))


def read_edges(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    """Both ends filtered: an edge to a protected node leaks by adjacency even
    when the node itself is withheld.

    Covers every ``entity_edges`` type, including ``semantic_affinity`` (latent
    cosine edges) — the corpus plants one so this is not vacuously clean.
    """
    rows = [
        {
            "src_entity_id": r[0],
            "dst_entity_id": r[1],
            "edge_type": r[2],
            "metadata_json": r[3],
        }
        for r in conn.execute(
            "SELECT src_entity_id, dst_entity_id, edge_type, metadata_json "
            "FROM entity_edges"
        )
    ]
    return guard.filter_rows(rows, id_keys=("src_entity_id", "dst_entity_id"))


def read_context_vectors(
    conn: sqlite3.Connection, guard: BlackholeGuard
) -> List[Dict[str, Any]]:
    """Mention-context centroids are keyed only by entity_id."""
    try:
        rows = [
            {"entity_id": r[0], "model_name": r[1], "centroid_marker": r[2]}
            for r in conn.execute(
                "SELECT entity_id, model_name, centroid_blob FROM entity_context_vectors"
            )
        ]
    except sqlite3.OperationalError:
        return []
    # Decode the planted marker bytes so serialize() can see the token.
    for row in rows:
        blob = row.get("centroid_marker")
        if isinstance(blob, (bytes, bytearray)):
            row["centroid_marker"] = bytes(blob).decode("utf-8", errors="replace")
    return guard.filter_rows(rows, id_keys=("entity_id",))


def read_facts(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    """Subject *and* object filtered, plus a name scan on the rendered value.

    A fact whose object is protected is stored under a different subject, so
    subject-only filtering misses it; and `object_value` is a bare string, so
    even object-id filtering misses the case where the id was never resolved.
    """
    rows = []
    for (payload_json,) in conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
    ):
        rows.append(json.loads(payload_json))
    kept = guard.filter_rows(
        rows,
        id_keys=("subject_entity_id", "object_entity_id"),
        name_keys=(),
    )
    return [r for r in kept if not guard.text_mentions_blackholed(r.get("object_value"))]


def read_embedding_chunks(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    """Chunks joined to entities via embedding_entities, then text-scanned."""
    rows = [
        {"embedding_id": r[0], "text_preview": r[1], "entity_id": r[2]}
        for r in conn.execute(
            """
            SELECT e.embedding_id, e.text_preview, COALESCE(ee.entity_id, '')
            FROM signal_embeddings e
            LEFT JOIN embedding_entities ee ON ee.embedding_id = e.embedding_id
            """
        )
    ]
    kept = guard.filter_rows(rows, id_keys=("entity_id",))
    return [r for r in kept if not guard.text_mentions_blackholed(r.get("text_preview"))]


def read_raw_messages(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    """Canonical rows reached through the mention join, then text-scanned.

    Both halves matter: the join catches records the resolver bound, the scan
    catches a name it never got around to binding.
    """
    rows = [
        {"record_id": r[0], "content": r[1], "entity_id": r[2] or ""}
        for r in conn.execute(
            """
            SELECT m.record_id, m.content, COALESCE(em.entity_id, '')
            FROM conversation_messages m
            LEFT JOIN entity_mentions em ON em.record_id = m.record_id
            """
        )
    ]
    kept = guard.filter_rows(rows, id_keys=("entity_id",))
    return [r for r in kept if not guard.text_mentions_blackholed(r.get("content"))]


def read_dossiers(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    """Own dossier by id; *other* entities' dossiers by scanning their prose and
    their neighbour lists."""
    kept: List[Dict[str, Any]] = []
    for object_key, payload_json in conn.execute(
        "SELECT object_key, payload_json FROM signal_objects"
        " WHERE object_type='entity_dossier' AND valid_to IS NULL"
    ):
        entity_id = str(object_key).split(":", 1)[-1]
        # The protected entity's own dossier disappears entirely.
        if guard.blocks_entity_id(entity_id):
            continue
        payload = json.loads(payload_json)
        # An unrelated entity's dossier survives, minus anything that names the
        # protected one: dropping the whole record would punish the visible
        # entity for the association, and its absence would itself be a signal.
        payload["summary_text"] = guard.withhold_if_mentions(payload.get("summary_text"))
        for key in ("top_connections", "affinity_connections"):
            payload[key] = guard.filter_rows(
                payload.get(key) or [],
                id_keys=("entity_id",),
                name_keys=("canonical_name", "entity_name", "name", "label"),
            )
        kept.append(payload)
    return kept


# ---------------------------------------------------------- name-string


def read_briefs(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    rows = [
        {"signal_dimension": r[0], "markdown_body": r[1]}
        for r in conn.execute("SELECT signal_dimension, markdown_body FROM signal_dimension_briefs")
    ]
    return guard.filter_name_string_artifacts(rows, text_keys=("markdown_body",))


def read_top_topics(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for (payload_json,) in conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='top_topics' AND valid_to IS NULL"
    ):
        payload = json.loads(payload_json)
        payload["related_entities"] = [
            name
            for name in (payload.get("related_entities") or [])
            if not guard.text_mentions_blackholed(name)
        ]
        out.append(payload)
    return out


def read_topic_clusters(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    """A cluster row carries no entity id — the label IS the name.

    Both the label and the centroid preview are prose the labeler wrote from
    member text, so the whole row goes when either mentions a protected entity.
    Withdrawal rather than redaction: a cluster with its name cut out still says
    a cluster about that person exists, and its member_count still counts them.
    """
    rows = [
        {"cluster_id": r[0], "label": r[1], "centroid_preview": r[2]}
        for r in conn.execute(
            "SELECT cluster_id, label, centroid_preview FROM topic_clusters"
        )
    ]
    return guard.filter_name_string_artifacts(rows, text_keys=("label", "centroid_preview"))


def read_topic_cluster_members(
    conn: sqlite3.Connection, guard: BlackholeGuard
) -> List[Dict[str, Any]]:
    """Members hand back raw canonical text, so they leak without the label."""
    rows = [
        {"cluster_id": r[0], "record_id": r[1], "text_preview": r[2]}
        for r in conn.execute(
            "SELECT cluster_id, record_id, text_preview FROM topic_cluster_members"
        )
    ]
    return guard.filter_name_string_artifacts(rows, text_keys=("text_preview",))


def read_attention_digest(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    """Movers, seeds and surface titles are all bare labels — scan every one."""
    out: List[Dict[str, Any]] = []
    for (payload_json,) in conn.execute(
        "SELECT payload_json FROM signal_objects"
        " WHERE object_type='attention_summary' AND valid_to IS NULL"
    ):
        payload = json.loads(payload_json)
        payload["movers"] = [
            m for m in (payload.get("movers") or []) if not guard.text_mentions_blackholed(m)
        ]
        payload["seeds"] = [
            s
            for s in (payload.get("seeds") or [])
            if not guard.text_mentions_blackholed(s.get("bond"))
        ]
        payload["surface"] = [
            s
            for s in (payload.get("surface") or [])
            if not guard.text_mentions_blackholed(s.get("title"))
        ]
        out.append(payload)
    return out


def read_stat_insights(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    """`group_key` is inside the payload, not a column — no id to filter on, so
    the name scan is the only thing standing between this and a leak."""
    rows: List[Dict[str, Any]] = []
    for fact_id, payload_json in conn.execute("SELECT fact_id, payload_json FROM signal_facts"):
        payload = json.loads(payload_json or "{}")
        rows.append({"fact_id": fact_id, **payload})
    return guard.filter_rows(rows, id_keys=(), name_keys=("group_key",))


def read_user_goals(conn: sqlite3.Connection, guard: BlackholeGuard) -> List[Dict[str, Any]]:
    rows = [
        {"goal_id": r[0], "goal_text": r[1]}
        for r in conn.execute("SELECT goal_id, goal_text FROM user_goals")
    ]
    return [r for r in rows if not guard.text_mentions_blackholed(r.get("goal_text"))]


# Surface name → reader. The battery iterates this, so adding a surface here is
# all it takes to bring it under the gate.
SURFACES: Dict[str, Reader] = {
    "entities": read_entities,
    "mentions": read_mentions,
    "edges": read_edges,
    "context_vectors": read_context_vectors,
    "facts": read_facts,
    "dossiers": read_dossiers,
    "embeddings": read_embedding_chunks,
    "raw_messages": read_raw_messages,
    "briefs": read_briefs,
    "top_topics": read_top_topics,
    "topic_clusters": read_topic_clusters,
    "topic_cluster_members": read_topic_cluster_members,
    "attention_digest": read_attention_digest,
    "stat_insights": read_stat_insights,
    "user_goals": read_user_goals,
}


def read_all(conn: sqlite3.Connection, guard: BlackholeGuard) -> Dict[str, Any]:
    return {name: reader(conn, guard) for name, reader in SURFACES.items()}


def serialize(payload: Any) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)
