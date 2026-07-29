"""Canary corpus for the entity black-hole battery (EVAL_ENTITY_BLACKHOLE.md §1).

Every leak surface enumerated in the feasibility report gets its own planted,
unguessable token. That is the point of building the corpus this way: coverage
is not a checklist someone maintains by hand, it is a property of the fixture.
If a surface has no token, the owner-recall assertion fails — an unprobed
surface shows up as a *test failure*, not as silent absence.

Two token kinds, because the two enforcement mechanisms differ:

* the **entity's own name** is deliberately rare (`Dana Qx71reyes`), and every
  prose surface embeds it verbatim, because prose artifacts carry names as
  strings and can only be caught by scanning for them;
* each surface additionally carries a **distinct surface token**, so when a
  probe leaks, the scorecard says *which* surface leaked rather than just that
  something did.

One entity is protected and one is not. The control entity is what makes the
battery non-vacuous: a filter that hides everything scores a perfect zero leak
rate and is worthless, so every class also asserts the control survives.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Dict, List

from topos.features.lifecycle.blackhole import BlackholeStore
from topos.storage.db.migrations import apply_all_migrations

# Bump when the corpus changes — resets comparability of scorecards across runs,
# same discipline as CORPUS_VERSION in the privacy corpus.
CORPUS_VERSION = "blackhole-corpus-1"

# Rare enough that a substring scan cannot false-positive on ordinary prose,
# name-shaped enough that it exercises the real normalizer.
BH_ID = "ent-bh-dana"
BH_CANONICAL = "Dana Qx71reyes"
BH_ALIAS_A = "Dqx72nickname"
BH_ALIAS_B = "D. Qx71reyes"

# Protected but never mentioned anywhere: proves "no data" and "protected" are
# indistinguishable (D5) — a probe for either must look the same.
BH2_ID = "ent-bh-quiet"
BH2_CANONICAL = "Quiet Qx73person"

# The control: visible under a normal grant. Must survive every filter.
OK_ID = "ent-ok-sam"
OK_CANONICAL = "Sam Ok91okoye"

# One token per leak surface, keyed by the surface it proves.
#
# `entity_name` is the canonical name itself rather than a separate marker: the
# name IS the thing being protected, and it must appear verbatim in every prose
# surface for the name-scan to have anything to match. Appending a marker to the
# stored name instead would change what the normalizer produces, and the scan
# would silently never fire — which is exactly the bug this battery caught when
# the corpus was first written.
TOKENS: Dict[str, str] = {
    "entity_name": BH_CANONICAL,
    "alias": BH_ALIAS_A,
    "mention_surface": "BHMENTIONqx03",
    "fact_subject": "BHSUBJqx04",
    "fact_object_value": "BHOBJqx05",
    "dossier": "BHDOSSIERqx06",
    "dossier_neighbour": "BHNEIGHqx07",
    "brief": "BHBRIEFqx08",
    "digest_mover": "BHMOVERqx09",
    "stat_group_key": "BHSTATqx10",
    "embedding_chunk": "BHCHUNKqx11",
    "raw_canonical": "BHRAWqx12",
    "goal_text": "BHGOALqx13",
    "edge_statement": "BHEDGEqx14",
    "top_topics_related": "BHTOPICqx15",
}

# Control tokens — these must SURVIVE for every non-owner caller.
CONTROL_TOKENS: Dict[str, str] = {
    "entity_name": OK_CANONICAL,
    "mention_surface": "OKMENTIONqx92",
    "brief": "OKBRIEFqx93",
}


@dataclass
class BlackholeCorpus:
    conn: sqlite3.Connection
    bh_entity_id: str = BH_ID
    bh_name: str = BH_CANONICAL
    quiet_entity_id: str = BH2_ID
    control_entity_id: str = OK_ID
    control_name: str = OK_CANONICAL
    tokens: Dict[str, str] = field(default_factory=lambda: dict(TOKENS))
    control_tokens: Dict[str, str] = field(default_factory=lambda: dict(CONTROL_TOKENS))

    @property
    def all_bh_tokens(self) -> List[str]:
        return list(self.tokens.values())

    @property
    def all_control_tokens(self) -> List[str]:
        return list(self.control_tokens.values())


def _obj(
    conn: sqlite3.Connection,
    *,
    dimension: str,
    object_type: str,
    object_key: str,
    payload: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO signal_objects
            (object_id, signal_dimension, object_type, object_key, payload_json,
             valid_from, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))
        """,
        (
            f"obj_{uuid.uuid4().hex[:12]}",
            dimension,
            object_type,
            object_key,
            json.dumps(payload),
        ),
    )


def _aux_tables(conn: sqlite3.Connection) -> None:
    """Surfaces whose real DDL lives outside the wiki migrations.

    Created here so the corpus can plant in them without dragging the whole
    ingest pipeline into a unit-speed battery.
    """
    # conversation_messages is created by the legacy _ensure_tables path rather
    # than by a migration, so the battery stands it up itself. Every other
    # surface uses the real migrated schema.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_messages (
            record_id TEXT PRIMARY KEY,
            source_id TEXT,
            content TEXT,
            event_at TEXT
        );
        """
    )


def build_blackhole_corpus(db_path: str, *, rebuild_complete: bool = True) -> BlackholeCorpus:
    """Seed a topos where one entity is protected and one is not.

    `rebuild_complete=False` leaves the black hole in its pending-rebuild window,
    which is the state the I6 probes exercise.
    """
    conn = sqlite3.connect(db_path)
    apply_all_migrations(conn)
    _aux_tables(conn)

    t = TOKENS
    c = CONTROL_TOKENS

    # ---------------------------------------------------------- entities
    for entity_id, canonical, aliases in (
        (BH_ID, BH_CANONICAL, [BH_ALIAS_A, BH_ALIAS_B]),
        (BH2_ID, BH2_CANONICAL, []),
        (OK_ID, OK_CANONICAL, []),
    ):
        conn.execute(
            """
            INSERT INTO entities
                (entity_id, entity_type, canonical_name, normalized_name, aliases_json)
            VALUES (?, 'person', ?, ?, ?)
            """,
            (entity_id, canonical, canonical.lower(), json.dumps(aliases)),
        )
    # Canonical names are left exactly as stored: they are themselves the
    # name canaries, and the scan matches on what the normalizer produces.

    # -------------------------------------------------------- mentions
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, event_at)
        VALUES (?, ?, 'rec-1', 'src-a', 'conversation_messages', ?, '2026-07-01T10:00:00Z')
        """,
        (f"m_{uuid.uuid4().hex[:8]}", BH_ID, f"{BH_CANONICAL} {t['mention_surface']}"),
    )
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, event_at)
        VALUES (?, ?, 'rec-2', 'src-a', 'conversation_messages', ?, '2026-07-01T11:00:00Z')
        """,
        (f"m_{uuid.uuid4().hex[:8]}", OK_ID, f"{OK_CANONICAL} {c['mention_surface']}"),
    )

    # ----------------------------------------------------------- edges
    # Links the protected entity to the visible one: how a graph snapshot leaks
    # by adjacency even when the protected node is itself withheld.
    conn.execute(
        """
        INSERT INTO entity_edges
            (edge_id, src_entity_id, dst_entity_id, edge_type, weight, metadata_json)
        VALUES (?, ?, ?, 'co_mention', 1.0, ?)
        """,
        (
            f"e_{uuid.uuid4().hex[:8]}",
            OK_ID,
            BH_ID,
            json.dumps({"statement": f"works with {BH_CANONICAL} {t['edge_statement']}"}),
        ),
    )

    # ------------------------------------------ facts: subject and object
    _obj(
        conn,
        dimension="identity",
        object_type="fact",
        object_key=f"fact:{BH_ID}:works_at",
        payload={
            "subject_entity_id": BH_ID,
            "predicate": "works_at",
            "object_value": f"{BH_CANONICAL} {t['fact_subject']}",
            "disclosure": "owner_only",
        },
    )
    # The hard one: keyed under the *visible* entity, protected entity as value.
    # Filtering facts by subject alone would sail straight past this.
    _obj(
        conn,
        dimension="identity",
        object_type="fact",
        object_key=f"fact:{OK_ID}:collaborates_with",
        payload={
            "subject_entity_id": OK_ID,
            "predicate": "collaborates_with",
            "object_entity_id": BH_ID,
            "object_value": f"{BH_CANONICAL} {t['fact_object_value']}",
            "disclosure": "scoped",
        },
    )

    # --------------------------------------------------------- dossiers
    _obj(
        conn,
        dimension="identity",
        object_type="entity_dossier",
        object_key=f"dossier:{BH_ID}",
        payload={
            "summary_text": f"{BH_CANONICAL} {t['dossier']}",
            "disclosure": "owner_only",
        },
    )
    # A dossier for the *visible* entity that names the protected one as a neighbour.
    _obj(
        conn,
        dimension="identity",
        object_type="entity_dossier",
        object_key=f"dossier:{OK_ID}",
        payload={
            "summary_text": f"{OK_CANONICAL} works closely with {BH_CANONICAL}"
            f" {t['dossier_neighbour']}",
            "top_connections": [{"entity_id": BH_ID, "canonical_name": BH_CANONICAL}],
        },
    )

    # ------------------------------------------------------- top_topics
    _obj(
        conn,
        dimension="interests",
        object_type="top_topics",
        object_key="top_topics:current",
        payload={
            "topics": ["scheduling"],
            "related_entities": [f"{BH_CANONICAL} {t['top_topics_related']}"],
        },
    )

    # ------------------------------------------------- attention digest
    _obj(
        conn,
        dimension="attention",
        object_type="attention_summary",
        object_key="attention:2026-07-28",
        payload={
            "movers": [f"{BH_CANONICAL} {t['digest_mover']}"],
            "seeds": [{"bond": BH_CANONICAL}],
            "surface": [{"title": f"catch up with {BH_CANONICAL}"}],
        },
    )

    # ----------------------------------------------------------- briefs
    for dimension, body in (
        ("relationships", f"A quiet week, mostly with {BH_CANONICAL} {t['brief']}."),
        ("work", f"Shipping alongside {OK_CANONICAL} {c['brief']}."),
    ):
        conn.execute(
            """
            INSERT INTO signal_dimension_briefs
                (brief_id, signal_dimension, head_revision_id, structured_json, markdown_body)
            VALUES (?, ?, 'rev-1', '{}', ?)
            """,
            (f"b_{uuid.uuid4().hex[:8]}", dimension, body),
        )

    # ----------------------------------------------------- stat insight
    # group_key lives inside the payload, which is exactly why a name scan is
    # needed here: there is no column to filter on.
    conn.execute(
        "INSERT INTO signal_facts (fact_id, dimension, payload_json) VALUES (?,?,?)",
        (
            "stat:contact_freq:bh",
            "relationships",
            json.dumps(
                {
                    "group_key": BH_CANONICAL,
                    "body": f"12 exchanges this month {t['stat_group_key']}",
                }
            ),
        ),
    )

    # ------------------------------------------------- embedding chunk
    emb_id = f"emb_{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, text_preview) VALUES (?,?,?)",
        (emb_id, "rec-1", f"dinner plans with {BH_CANONICAL} {t['embedding_chunk']}"),
    )
    conn.execute(
        "INSERT INTO embedding_entities (embedding_id, entity_id) VALUES (?,?)",
        (emb_id, BH_ID),
    )

    # -------------------------------------------------- raw canonical
    conn.execute(
        "INSERT INTO conversation_messages (record_id, source_id, content, event_at)"
        " VALUES (?,?,?,?)",
        (
            "rec-1",
            "src-a",
            f"see you thursday {BH_CANONICAL} {t['raw_canonical']}",
            "2026-07-01T10:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO conversation_messages (record_id, source_id, content, event_at)"
        " VALUES (?,?,?,?)",
        ("rec-2", "src-a", f"standup notes {OK_CANONICAL}", "2026-07-01T11:00:00Z"),
    )

    # ------------------------------------------------------- user goal
    conn.execute(
        "INSERT INTO user_goals (goal_id, goal_text, payload_json) VALUES (?,?,'{}')",
        ("g-1", f"spend more time with {BH_CANONICAL} {t['goal_text']}"),
    )

    conn.commit()

    # ------------------------------------------------ flip the black holes
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=BH_ID)
    store.blackhole_entity(entity_ref=BH2_ID)
    if rebuild_complete:
        store.mark_rebuild_complete(BH_ID)
        store.mark_rebuild_complete(BH2_ID)

    return BlackholeCorpus(conn=conn)
