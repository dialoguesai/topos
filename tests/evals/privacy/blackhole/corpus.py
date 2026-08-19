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
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

# Bump when the corpus changes — resets comparability of scorecards across runs,
# same discipline as CORPUS_VERSION in the privacy corpus.
CORPUS_VERSION = "blackhole-corpus-4"

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

# A REAL connector id, not a fixture-shaped one. The storage readers in
# `surfaces.py` do not care, but the query path does: `retrieve()` throws the
# caller's `installed_source_ids` at `resolve_retrieval_source_ids`, which
# intersects them with the scope manifest's own defaults and falls back to those
# defaults when the intersection is empty. A corpus seeded under `src-a` is
# therefore invisible to every canonical lane the query path runs — the P4 lane
# resolved the entity, asked for its records, and got nothing back, so the query
# reader scored clean without ever reaching the code it exists to guard.
SOURCE_ID = "imessage"

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
    "affinity_edge": "BHAFFINqx16",
    "context_vector": "BHCTXqx17",
    # A topic cluster's own row. The label used to be term soup ("https / good
    # / here") and named nobody; the contrastive labeler names the subject, so
    # a cluster about a protected person is now called after them. The label is
    # stored once and served to every caller, and top_topics facts are minted
    # from it — so it is a name-string surface in its own right.
    "cluster_label": "BHCLUSTERqx18",
    "cluster_preview": "BHPREVIEWqx19",
    # The entity-lane hazard, and the only token here whose row does NOT name the
    # protected entity. Every other canonical probe carries the name, so a filter
    # that is a pure name scan passes all of them — which is how the query path
    # served a black-holed entity's record id, table and timestamp to a grantee
    # while scoring BHLR = 0. The link lives only in `entity_mentions`, so the
    # only instrument that can reach this row is the id join.
    "thread_record": "BHTHREADqx20",
    # The record ID, planted as a token in its own right. This is the shape the
    # leak actually took: the body WAS correctly withheld by the disclosure tier
    # and the identifiers were not, so a probe that scans only for content tokens
    # scores the packet clean while the caller learns the entity exists, which
    # record it owns, in which table, and when. An identifier is a leak.
    "thread_record_id": "rec-BHRECIDqx21",
}

#: The record id of that row — the same string as the token above, named because
#: the corpus plants it in two places (the canonical row and the mention join).
BH_THREAD_RECORD_ID = TOKENS["thread_record_id"]

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


def _seed_messages(conn: sqlite3.Connection, rows: List[Dict[str, str]]) -> None:
    """Canonical messages, through the real ingest manager.

    This used to be a four-column hand-rolled table, and that shortcut was load
    bearing in the wrong direction: the query pipeline's canonical adapter reads
    the *real* schema, so a corpus that faked it could not be handed to
    `retrieve()` at all — which is why the battery had no reader on the query
    path, and why a live existence leak there sat under a green BHLR.
    """
    mgr = ConversationsTablesManager(conn)
    for row in rows:
        mgr.upsert_message_batch(
            [
                {
                    "message_id": row["record_id"],
                    "thread_id": "thread-bh",
                    "content": row["content"],
                    "ts": row["event_at"],
                    "sender_type": "contact",
                }
            ],
            dataset_id="user:default:device",
            source_id=row["source_id"],
            sync_batch_id=f"batch-{row['record_id']}",
        )


def build_blackhole_corpus(db_path: str, *, rebuild_complete: bool = True) -> BlackholeCorpus:
    """Seed a topos where one entity is protected and one is not.

    `rebuild_complete=False` leaves the black hole in its pending-rebuild window,
    which is the state the I6 probes exercise.
    """
    conn = sqlite3.connect(db_path)
    apply_all_migrations(conn)

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
        VALUES (?, ?, 'rec-1', ?, 'conversation_messages', ?, '2026-07-01T10:00:00Z')
        """,
        (
            f"m_{uuid.uuid4().hex[:8]}",
            BH_ID,
            SOURCE_ID,
            f"{BH_CANONICAL} {t['mention_surface']}",
        ),
    )
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, event_at)
        VALUES (?, ?, 'rec-2', ?, 'conversation_messages', ?, '2026-07-01T11:00:00Z')
        """,
        (
            f"m_{uuid.uuid4().hex[:8]}",
            OK_ID,
            SOURCE_ID,
            f"{OK_CANONICAL} {c['mention_surface']}",
        ),
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
    # Latent affinity is a separate edge type with a cosine weight — the generic
    # edges reader must cover it too, not only co_mention / co_occurrence.
    conn.execute(
        """
        INSERT INTO entity_edges
            (edge_id, src_entity_id, dst_entity_id, edge_type, weight, metadata_json)
        VALUES (?, ?, ?, 'semantic_affinity', 0.82, ?)
        """,
        (
            f"e_{uuid.uuid4().hex[:8]}",
            OK_ID,
            BH_ID,
            json.dumps(
                {
                    "kind": "affinity",
                    "statement": f"{BH_CANONICAL} {t['affinity_edge']}",
                }
            ),
        ),
    )

    # Mention-context centroid for the protected person. Nothing serves the
    # blob today, but the surface must stay under the gate so a future reader
    # cannot regress silently.
    conn.execute(
        """
        INSERT INTO entity_context_vectors
            (entity_id, centroid_blob, mention_sample, source_sample,
             model_name, computed_at)
        VALUES (?, ?, 5, 3, ?, datetime('now'))
        """,
        (BH_ID, t["context_vector"].encode("utf-8"), "test-centroid"),
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
            "affinity_connections": [
                {
                    "entity_id": BH_ID,
                    "canonical_name": BH_CANONICAL,
                    "edge_type": "semantic_affinity",
                    "weight": 0.82,
                }
            ],
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
    _seed_messages(
        conn,
        [
            {
                "record_id": "rec-1",
                "source_id": SOURCE_ID,
                "content": f"see you thursday {BH_CANONICAL} {t['raw_canonical']}",
                "event_at": "2026-07-01T10:00:00Z",
            },
            {
                "record_id": "rec-2",
                "source_id": SOURCE_ID,
                "content": f"standup notes {OK_CANONICAL}",
                "event_at": "2026-07-01T11:00:00Z",
            },
            # The entity-lane row: linked to the protected entity by mention only,
            # its text naming nobody. A name scan cannot see it; the id join can.
            {
                "record_id": BH_THREAD_RECORD_ID,
                "source_id": SOURCE_ID,
                "content": f"shipping the compiler rewrite on friday {t['thread_record']}",
                "event_at": "2026-07-01T12:00:00Z",
            },
        ],
    )
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, event_at)
        VALUES (?, ?, ?, ?, 'conversation_messages', ?, '2026-07-01T12:00:00Z')
        """,
        (
            f"m_{uuid.uuid4().hex[:8]}",
            BH_ID,
            BH_THREAD_RECORD_ID,
            SOURCE_ID,
            BH_CANONICAL,
        ),
    )

    # ------------------------------------------------------- user goal
    conn.execute(
        "INSERT INTO user_goals (goal_id, goal_text, payload_json) VALUES (?,?,'{}')",
        ("g-1", f"spend more time with {BH_CANONICAL} {t['goal_text']}"),
    )

    # ------------------------------------------------- topic cluster rows
    # The label is the surface: stored once, served to every caller, and the
    # source `write_top_topics_signal_facts` mints top_topics facts from. The
    # control cluster is what keeps the probe non-vacuous.
    for cluster_id, label, preview, member_text in (
        (
            "tc-bh",
            f"{BH_CANONICAL} {t['cluster_label']}",
            f"catching up with {BH_CANONICAL} {t['cluster_preview']}",
            f"dinner with {BH_CANONICAL}",
        ),
        (
            "tc-ok",
            f"{OK_CANONICAL} standup",
            "sprint planning notes",
            f"standup with {OK_CANONICAL}",
        ),
    ):
        conn.execute(
            """
            INSERT INTO topic_clusters
                (cluster_id, label, dimension, member_count, source_mix_json,
                 label_terms_json, centroid_preview, metadata_json)
            VALUES (?, ?, 'relationships', 4, '{}', ?, ?, ?)
            """,
            (
                cluster_id,
                label,
                json.dumps([label.split()[0].lower()]),
                preview,
                json.dumps({"term_label": f"{label.split()[0].lower()} / notes"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO topic_cluster_members
                (member_id, cluster_id, record_id, source_id, record_type, text_preview)
            VALUES (?, ?, ?, ?, 'conversation_message', ?)
            """,
            (f"m-{cluster_id}", cluster_id, f"rec-{cluster_id}", SOURCE_ID, member_text),
        )

    # ------------------------------------------- make the entities linkable
    # `link_query_entities` reads `FROM entities WHERE mention_count > 0 OR
    # contact_id IS NOT NULL`, so an entity left at the column default of 0 is
    # invisible to the linker no matter how exactly the query names it. That is
    # not a detail: the query-path reader below resolves the entity by name, and
    # with nothing linked the P4 entity lane never runs, so `read_query_retrieval`
    # exercised the retrieval boundary while structurally missing the one lane it
    # was added to guard. The battery scored BHLR = 0 against a leak it could not
    # reach — the same shape of blindness one layer down.
    #
    # Derived from the mention rows rather than hardcoded, so a surface added
    # later cannot leave the count stale. The quiet entity has no mentions and so
    # truthfully stays at 0, which is what makes it the D5 control.
    conn.execute(
        """
        UPDATE entities SET mention_count = (
            SELECT COUNT(*) FROM entity_mentions em
            WHERE em.entity_id = entities.entity_id
        )
        """
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
