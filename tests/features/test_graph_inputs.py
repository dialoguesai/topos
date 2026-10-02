"""What the graph's input fingerprint covers (1.4.4).

The refresher skips a rebuild when ``graph_input_fingerprint`` is unchanged, so a
table the rebuild reads but the fingerprint leaves out is a graph that silently
stops following that table. Two guards:

* every kind of change the rebuild must follow changes the fingerprint, and the
  changes it must ignore (its own outputs, rows nothing points at) do not;
* every table named in the rebuild's SQL is digested or exempted here, with a
  reason -- a new read that nobody added to ``graph_inputs`` fails this file.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sqlite3
import textwrap

import pytest

from topos.features.entities import graph_inputs
from topos.features.entities.graph_inputs import graph_input_fingerprint, graph_input_parts
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations

from .test_graph_refresh_no_churn import _seed, _seed_ids

NOW = "2026-07-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("TOPOS_COMMUNITY_NAMING", "off")


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "node.db"))
    ensure_all_tables(c)
    apply_all_migrations(c)
    _seed(c)
    from topos.features.entities.maintenance import rebuild_entity_graph
    from topos.features.entities import graph_enrichers

    real = graph_enrichers._default_goal_embedder
    graph_enrichers._default_goal_embedder = lambda texts: [
        [1.0 if j == i else 0.0 for j in range(len(texts))] for i in range(len(texts))
    ]
    try:
        rebuild_entity_graph(c)  # so there are materialized rows to leave alone
    finally:
        graph_enrichers._default_goal_embedder = real
    yield c
    c.close()


def _one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def _materialized_vertex(conn):
    return _one(conn, "SELECT entity_id FROM entities WHERE json_extract(metadata_json, '$.mz') = 1 LIMIT 1")


# Each changes something the rebuild reads. ``ids`` holds the seeded people.
INPUT_CHANGES = {
    "a new mention": lambda c, ids: c.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, "
        "confidence, event_at) VALUES ('m-in', ?, 'msg-1', 'imessage', 'conversation_messages', 'x', 0.9, ?)",
        (ids["bram"], NOW)),
    "a removed mention": lambda c, ids: c.execute("DELETE FROM entity_mentions WHERE mention_id='m1'"),
    "a mention moved to another entity": lambda c, ids: c.execute(
        "UPDATE entity_mentions SET entity_id=? WHERE mention_id='m1'", (ids["ada"],)),
    "a mention re-dated": lambda c, ids: c.execute("UPDATE entity_mentions SET event_at=? WHERE mention_id='m1'", (NOW,)),
    "an entity renamed": lambda c, ids: c.execute(
        "UPDATE entities SET canonical_name='Ada Q', normalized_name='ada q' WHERE entity_id=?", (ids["ada"],)),
    "an entity retyped": lambda c, ids: c.execute("UPDATE entities SET entity_type='org' WHERE entity_id=?", (ids["corin"],)),
    "an alias added": lambda c, ids: c.execute(
        "UPDATE entities SET aliases_json='[\"Cor\"]' WHERE entity_id=?", (ids["corin"],)),
    "the owner flag moved": lambda c, ids: c.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (ids["bram"],)),
    "a contact anchor cut": lambda c, ids: c.execute("UPDATE entities SET contact_id=NULL WHERE entity_id=?", (ids["ada"],)),
    "an entity deleted": lambda c, ids: c.execute("DELETE FROM entities WHERE entity_id=?", (ids["corin"],)),
    "a contact renamed": lambda c, ids: c.execute("UPDATE contacts SET display_name='A. Quill' WHERE contact_id='c-ada'"),
    "a contact removed": lambda c, ids: c.execute("DELETE FROM contacts WHERE contact_id='c-bram'"),
    "a contact identifier added": lambda c, ids: c.execute(
        "INSERT INTO contact_identifiers (contact_id, identifier, identifier_type, dataset_id, source_id) "
        "VALUES ('c-bram', 'bram.h@example.test', 'email', 'ds', 'contacts')"),
    "a participant added": lambda c, ids: c.execute(
        "INSERT INTO conversation_participants (conversation_id, dataset_id, contact_id, source_id) "
        "VALUES ('conv-2', 'ds', 'c-ada', 'imessage')"),
    "a conversation added": lambda c, ids: c.execute(
        "INSERT INTO conversations (conversation_id, dataset_id, source_id) VALUES ('conv-2', 'ds', 'imessage')"),
    "a message added to a thread": lambda c, ids: c.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, is_from_self, "
        "event_at, content, source_id) VALUES ('msg-9', 'conv-1', 'ds', 'ada@example.test', 0, ?, 'x', 'imessage')",
        (NOW,)),
    "a message's sender changed": lambda c, ids: c.execute(
        "UPDATE conversation_messages SET sender_id='bram@example.test' WHERE message_id='msg-1'"),
    "a message's role changed": lambda c, ids: c.execute(
        "UPDATE conversation_messages SET actor_role='ambient' WHERE message_id='msg-2'"),
    "a message's writer changed": lambda c, ids: c.execute(
        "UPDATE conversation_messages SET writer_class='app' WHERE message_id='msg-2'"),
    "an affinity edge": lambda c, ids: c.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type, weight, evidence_count, "
        "valid_from) VALUES ('aff-1', ?, ?, 'semantic_affinity', 0.9, 1, ?)", (ids["ada"], ids["corin"], NOW)),
    "a fact closed": lambda c, ids: c.execute("UPDATE signal_objects SET valid_to=? WHERE object_id='so-fact-a'", (NOW,)),
    "a fact added": lambda c, ids: c.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence, "
        "source_refs_json, valid_from, created_at, updated_at) VALUES ('so-fact-c', 'work', 'fact', 'fact:c', '{}', "
        "0.5, '[]', ?, ?, ?)", (NOW, NOW, NOW)),
    "a topic cluster's topics changed": lambda c, ids: c.execute(
        "UPDATE signal_objects SET payload_json=? WHERE object_id='so-topic'",
        (json.dumps({"tag": "Lantern build", "related_entities": ["Corin Vale", "Bram Holt"]}),)),
    "a topic relabelled": lambda c, ids: c.execute("UPDATE topic_clusters SET label='Lanterns' WHERE cluster_id='cl-1'"),
    "a topic member added": lambda c, ids: c.execute(
        "INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id) "
        "VALUES ('tcm-9', 'cl-1', 'msg-2', 'imessage')"),
    "a topic re-clustered (members written again, re-dated)": lambda c, ids: c.execute(
        "UPDATE topic_cluster_members SET created_at=?", (NOW,)),
    "a member's event time": lambda c, ids: c.execute("UPDATE timeline SET event_at=? WHERE record_id='msg-1'", (NOW,)),
    "a member's embedding time": lambda c, ids: c.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, signal_dimension, model, dims, "
        "vector_blob, event_at) VALUES ('emb-1', 'msg-3', 'imessage', 'memory', 'm', 1, x'00', ?)", (NOW,)),
    "a goal added": lambda c, ids: c.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
        "VALUES ('g-9', 'msg-1', 'imessage', 'Paint the hull', '{}')"),
    "a goal reworded": lambda c, ids: c.execute("UPDATE user_goals SET goal_text='Learn to sail well' WHERE goal_id='g-2'"),
    # Same rows, another storage order: "the first row met leads a goal group".
    "goals written back in another order": lambda c, ids: c.executescript(
        "CREATE TEMP TABLE keep AS SELECT * FROM user_goals; DELETE FROM user_goals; "
        "INSERT INTO user_goals SELECT * FROM keep ORDER BY goal_id DESC; DROP TABLE keep;"),
    "a visit added": lambda c, ids: c.execute(
        "INSERT INTO location_events (event_id, place_name, event_at, source_id) "
        "VALUES ('loc-9', 'Harbor Library', ?, 'grow_journal')", (NOW,)),
    "a black hole": lambda c, ids: c.execute(
        "INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name) "
        "VALUES ('bh-1', ?, 'corin vale', 'Corin Vale')", (ids["corin"],)),
    "an exclusion": lambda c, ids: c.execute(
        "INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
        "VALUES ('ex-1', 'entity', 'lantern works')"),
    "an approved unbind": lambda c, ids: c.execute(
        "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id, score, status, kind) "
        "VALUES ('rv-1', 'corin', ?, 0.9, 'approved', 'no_bind')", (ids["corin"],)),
    "an owner's community rename": lambda c, ids: c.execute(
        "INSERT INTO community_names (name_id, name, fingerprint_json, source) VALUES ('cn-o', 'Crew', '[]', 'owner')"),
    "a dossier stat line": lambda c, ids: c.execute(
        "INSERT INTO signal_facts (fact_id, dimension, payload_json) VALUES ('stat:messages.cadence:x', 'social', '{}')"),
    "a transcript segment": lambda c, ids: c.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, content, start_sec, source_id) "
        "VALUES ('yt:t1:90', 'yt:t1', 'More talk.', 90.0, 'youtube_transcripts')"),
    "a surface the NER model calls a value": lambda c, ids: c.executemany(
        "INSERT INTO message_entities (entity_id, record_id, source_id, entity_text, payload_json) "
        "VALUES (?, 'msg-1', 'imessage', 'next tuesday', ?)",
        [(f"me-{i}", json.dumps({"entity_type": "DATE"})) for i in range(3)]),
    "a role on a mentioned transcript row": lambda c, ids: c.execute(
        "UPDATE transcript_segments SET actor_role='authored' WHERE segment_id='yt:t1:0'"),
}

# Each changes something the rebuild does NOT read (or writes itself).
NOT_INPUTS = {
    "a canonical row nothing points at": lambda c, ids: (
        c.execute("INSERT INTO activity_events (event_id, source_id, activity_type, url, occurred_at) "
                  "VALUES ('visit-1', 'browser_visits', 'visit', 'https://example.test/', ?)", (NOW,)),
        c.execute("INSERT INTO timeline (event_at, record_id, source_id, canonical_table, record_type) "
                  "VALUES (?, 'visit-1', 'browser_visits', 'activity_events', 'visit')", (NOW,))),
    "a derived object the graph does not project": lambda c, ids: c.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence, "
        "source_refs_json, valid_from, created_at, updated_at) VALUES ('so-bi', 'interests', 'browsing_interest', "
        "'bi:1', '{}', 0.5, '[]', ?, ?, ?)", (NOW, NOW, NOW)),
    "a dossier rewritten": lambda c, ids: c.execute(
        "UPDATE signal_objects SET payload_json='{\"v\": 2}', updated_at=? WHERE object_type='entity_dossier'", (NOW,)),
    "the columns the rebuild recomputes": lambda c, ids: c.execute(
        "UPDATE entities SET mention_count=mention_count+1, first_seen=?, last_seen=?, updated_at=?, "
        "metadata_json=json_set(metadata_json, '$.community_id', 99) WHERE entity_id=?", (NOW, NOW, NOW, ids["ada"])),
    "a materialized vertex": lambda c, ids: c.execute(
        "UPDATE entities SET canonical_name='renamed by the rebuild', updated_at=? WHERE entity_id=?",
        (NOW, _materialized_vertex(c))),
    "an evidence edge": lambda c, ids: c.execute(
        "UPDATE entity_edges SET weight=weight+1, updated_at=? WHERE edge_type='co_occurrence'", (NOW,)),
    "a materialized edge": lambda c, ids: c.execute(
        "UPDATE entity_edges SET updated_at=?, weight=weight+1 WHERE json_extract(metadata_json, '$.mz') = 1", (NOW,)),
    "derived community names": lambda c, ids: c.execute(
        "UPDATE community_names SET times_matched=times_matched+1, last_matched_at=? WHERE source != 'owner'", (NOW,)),
    "a message's privacy stamps": lambda c, ids: c.execute(
        "UPDATE conversation_messages SET content_disclosure='owner_only', content_nsfw=0"),
    "a message stat other than the dossier lines": lambda c, ids: c.execute(
        "INSERT INTO signal_facts (fact_id, dimension, payload_json) VALUES ('stat:other.x', 'social', '{}')"),
    "the graph's own state": lambda c, ids: c.execute(
        "UPDATE graph_materialization_state SET dirty_generation=dirty_generation+5"),
    "topic members deleted and written back exactly as they were": lambda c, ids: c.executescript(
        "CREATE TEMP TABLE keep AS SELECT rowid AS rid, * FROM topic_cluster_members; "
        "DELETE FROM topic_cluster_members; "
        "INSERT INTO topic_cluster_members (rowid, member_id, cluster_id, record_id, source_id, record_type, "
        "text_preview, weight, metadata_json, created_at) SELECT rid, member_id, cluster_id, record_id, source_id, "
        "record_type, text_preview, weight, metadata_json, created_at FROM keep; DROP TABLE keep;"),
}


@pytest.mark.parametrize("change", sorted(INPUT_CHANGES))
def test_a_change_the_graph_reads_changes_the_fingerprint(conn, change):
    before = graph_input_fingerprint(conn)
    assert before is not None
    INPUT_CHANGES[change](conn, _seed_ids(conn))
    conn.commit()
    assert graph_input_fingerprint(conn) != before, f"{change} would not rebuild the graph"


@pytest.mark.parametrize("change", sorted(NOT_INPUTS))
def test_a_change_the_graph_does_not_read_leaves_the_fingerprint(conn, change):
    before = graph_input_fingerprint(conn)
    NOT_INPUTS[change](conn, _seed_ids(conn))
    conn.commit()
    assert graph_input_fingerprint(conn) == before, f"{change} would rebuild the graph for nothing"


def test_settings_the_rebuild_consults_change_the_fingerprint(conn, monkeypatch):
    import sys

    import topos.__version__  # noqa: F401 -- the module, not the package's string attribute

    before = graph_input_fingerprint(conn)
    monkeypatch.setattr(sys.modules["topos.__version__"], "__version__", "9.9.9")
    assert graph_input_fingerprint(conn) != before, "a new release must rebuild once"
    monkeypatch.undo()
    monkeypatch.setenv("TOPOS_COMMUNITY_NAMING", "on")
    assert graph_input_fingerprint(conn) != before
    monkeypatch.setenv("TOPOS_COMMUNITY_NAMING", "off")
    assert graph_input_fingerprint(conn) == before
    monkeypatch.setenv("TOPOS_EMBED_MODEL", "another/embedding-model")
    assert graph_input_fingerprint(conn) != before, "the goal lane clusters with this model"


def test_journal_entries_count_only_while_the_goal_field_flags_are_on(conn, monkeypatch):
    conn.execute("INSERT INTO journal_entries (entry_id, content, source_id) VALUES ('j-1', 'Goal: sail', 'grow_journal')")
    conn.commit()
    before = graph_input_fingerprint(conn)
    conn.execute("UPDATE journal_entries SET content='Goal: row' WHERE entry_id='j-1'")
    conn.commit()
    assert graph_input_fingerprint(conn) == before, "with the flags off the goal lane does not read entries"
    monkeypatch.setattr(graph_inputs, "_journal_goal_flags", lambda: (True, True))
    on = graph_input_fingerprint(conn)
    assert on != before
    conn.execute("UPDATE journal_entries SET content='Goal: sail' WHERE entry_id='j-1'")
    conn.commit()
    assert graph_input_fingerprint(conn) != on


def test_a_part_that_cannot_be_read_makes_the_fingerprint_unknown(conn, monkeypatch):
    def broken(rows):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(graph_inputs, "_digest_rows", lambda rows: broken(rows))
    assert graph_input_fingerprint(conn) is None


def test_a_missing_table_is_a_stable_state_not_an_unknown(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    apply_all_migrations(bare)  # no canonical conversation tables yet: a node before its first sync
    first = graph_input_fingerprint(bare)
    assert first is not None and graph_input_fingerprint(bare) == first
    assert graph_input_parts(bare)["conversation_messages"] == "absent"
    bare.close()


def test_ordinary_entities_are_digested_and_materialized_vertices_are_not(conn):
    digested = int(re.search(r"rows=(\d+)", graph_input_parts(conn)["entities"]).group(1))
    ordinary = _one(conn, "SELECT COUNT(*) FROM entities WHERE COALESCE(json_extract(metadata_json, '$.mz'), 0) != 1")
    materialized = _one(conn, "SELECT COUNT(*) FROM entities WHERE json_extract(metadata_json, '$.mz') = 1")
    assert ordinary > 3 and materialized > 3
    assert digested == ordinary


# --- the rebuild's SQL names no table this module forgot ----------------------------------------

#: Tables the rebuild's SQL names that the fingerprint deliberately leaves out.
EXEMPT = {
    "entity_context_vectors": "deleted with an entity by the orphan cascade; written, never read",
    "blackhole_notifications": "the black-hole store's own outbox; the rebuild reads entity_blackholes",
    "wiki_schema_migrations": "migration ledger, read to check a schema",
    "sqlite_master": "schema lookups",
}

#: Tables the fingerprint covers through a computed value rather than its rows.
COMPUTED = {
    "message_entities": "value_label_surfaces: the set of value surfaces",
    "source_runtime_installs": "discourse_enabled_source_ids and the source postures",
    "journal_entries": "digested whole while the goal-field flags are on",
}


def _rebuild_sql_sources():
    from topos.features.entities import (
        community_names, community_naming, discourse_graph, dossier, edges, fact_materializer,
        graph_enrichers, maintenance, owner, resolver,
    )
    from topos.features.lifecycle import blackhole, derived_scrub
    from topos.permissions_v2 import identity

    whole = [maintenance, fact_materializer, graph_enrichers, discourse_graph, dossier, community_names,
             community_naming, owner]
    parts = [
        derived_scrub._recount_entity_mentions, derived_scrub._delete_orphan_entities,
        derived_scrub._delete_entity_cascade, derived_scrub.close_dangling_facts,
        derived_scrub._ref_record_exists, derived_scrub._protected_entity_keys,
        derived_scrub.is_entity_protected, derived_scrub._is_protected,
        resolver.value_label_surfaces, resolver.EntityResolver,
        edges.update_edge, edges.top_edges, blackhole.BlackholeStore, identity.attested_self,
    ]
    for unit in whole + parts:
        yield textwrap.dedent(inspect.getsource(unit))


def _sql_literals(source: str):
    """String constants that are not docstrings, f-string text included."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value


def test_every_table_the_rebuild_names_is_digested_or_exempted(conn):
    known = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} | {"sqlite_master"}
    named = set()
    for source in _rebuild_sql_sources():
        for literal in _sql_literals(source):
            for table in re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_]*)", literal, re.I):
                if table in known:
                    named.add(table)
    digested = {key[len("role."):] if key.startswith("role.") else key for key in graph_input_parts(conn)}
    uncovered = sorted(named - digested - set(EXEMPT) - set(COMPUTED))
    assert len(named) >= 15, f"the scan found too little to mean anything: {sorted(named)}"
    assert uncovered == [], (
        f"the rebuild reads {uncovered} but graph_inputs does not digest them: "
        "add them to graph_inputs (or exempt them here, with the reason)"
    )
