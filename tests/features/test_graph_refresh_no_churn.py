"""The graph refresh rebuilds only when its inputs change, and a rebuild that finds
nothing to change writes nothing (1.4.4).

On the owner's node (1.4.3, 2 Oct 2026) the post-canonical pipeline marked the graph
dirty for every browser-visit batch, and each mark cost two full rebuilds of about
130 seconds that rewrote 40,101 evidence edges under new ids, bumped ``updated_at``
on 19,450 materialized edges and 5,109 entities, and changed no value. A grant's
relationship members digest edge and entity rows, so every rebuild made the
recipient's index stale.

These tests pin both halves:

* a rebuild over unchanged inputs writes no row of ``entities`` or ``entity_edges``
  (counted by triggers, so a same-value rewrite counts too) and leaves every row's
  bytes as they were;
* a real change still lands, exactly where it should;
* the refresher -- the pipeline's inline fill, the debounced timer, the startup
  reconcile -- skips the rebuild when nothing the graph reads has changed, and
  rebuilds when something has.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.features.entities import graph_enrichers, graph_refresh, rebuild_subprocess
from topos.features.entities.graph_inputs import graph_input_fingerprint
from topos.features.entities.maintenance import rebuild_entity_graph
from topos.features.entities.resolver import EntityResolver
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations

GRAPH_TABLES = ("entities", "entity_edges")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No model anywhere: deterministic goal vectors, deterministic community labels."""
    monkeypatch.setenv("TOPOS_COMMUNITY_NAMING", "off")

    def one_direction_per_goal(texts):
        n = len(texts)
        return [[1.0 if j == i else 0.0 for j in range(n)] for i in range(n)]

    monkeypatch.setattr(graph_enrichers, "_default_goal_embedder", one_direction_per_goal)
    graph_refresh.reset_for_tests(rebuild_fn=lambda: None)
    yield
    graph_refresh.reset_for_tests()


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "node.db"))
    ensure_all_tables(c)  # the canonical conversation tables a node creates at first sync,
    apply_all_migrations(c)  # then the columns its migrations add to them (actor_role, writer_class)
    yield c
    c.close()


# --- a small graph that exercises every lane -----------------------------------------------------


def _mention(conn, mention_id, entity_id, record_id, table, event_at, source_id="imessage"):
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, canonical_table, "
        "surface_text, confidence, event_at, created_at) VALUES (?, ?, ?, ?, ?, 'x', 0.9, ?, ?)",
        (mention_id, entity_id, record_id, source_id, table, event_at, event_at),
    )


def _seed(conn) -> dict:
    """Owner, two contacts, people and a project; messages in one thread; two goals;
    a place visited twice; a topic both topic lanes write; two facts sharing an edge;
    a transcript for the discourse lenses."""
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (owner,))
    for cid, name, ident in (("c-ada", "Ada Quill", "ada@example.test"), ("c-bram", "Bram Holt", "bram@example.test")):
        conn.execute(
            "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self, known_usernames_json) "
            "VALUES (?, 'ds', 'contacts', ?, 0, '[]')",
            (cid, name),
        )
        conn.execute(
            "INSERT INTO contact_identifiers (contact_id, identifier, identifier_type, dataset_id, source_id) "
            "VALUES (?, ?, 'email', 'ds', 'contacts')",
            (cid, ident),
        )
    r.seed_from_contacts()
    ada = conn.execute("SELECT entity_id FROM entities WHERE contact_id='c-ada'").fetchone()[0]
    bram = conn.execute("SELECT entity_id FROM entities WHERE contact_id='c-bram'").fetchone()[0]
    corin = r._create_entity("Corin Vale", "person")
    lantern = r._create_entity("Lantern Works", "org")

    # One thread: the owner, Ada and Bram talk; the messages name people.
    conn.execute(
        "INSERT INTO conversations (conversation_id, dataset_id, source_id) VALUES ('conv-1', 'ds', 'imessage')"
    )
    for cid in ("c-ada", "c-bram"):
        conn.execute(
            "INSERT INTO conversation_participants (conversation_id, dataset_id, contact_id, source_id) "
            "VALUES ('conv-1', 'ds', ?, 'imessage')",
            (cid,),
        )
    messages = (
        ("msg-1", "ada@example.test", 0, "2026-06-01T10:00:00Z"),
        ("msg-2", "self", 1, "2026-06-02T10:00:00Z"),
        ("msg-3", "bram@example.test", 0, "2026-06-03T10:00:00Z"),
    )
    for mid, sender, from_self, at in messages:
        conn.execute(
            "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, is_from_self, "
            "actor_role, event_at, content, source_id) VALUES (?, 'conv-1', 'ds', ?, ?, ?, ?, 'synthetic', 'imessage')",
            (mid, sender, from_self, "authored" if from_self else "observed", at),
        )
        conn.execute(
            "INSERT INTO timeline (event_at, record_id, source_id, canonical_table, record_type) "
            "VALUES (?, ?, 'imessage', 'conversation_messages', 'message')",
            (at, mid),
        )
    _mention(conn, "m1", corin, "msg-1", "conversation_messages", "2026-06-01T10:00:00Z")
    _mention(conn, "m2", lantern, "msg-1", "conversation_messages", "2026-06-01T10:00:00Z")
    _mention(conn, "m3", corin, "msg-2", "conversation_messages", "2026-06-02T10:00:00Z")
    _mention(conn, "m4", ada, "msg-2", "conversation_messages", "2026-06-02T10:00:00Z")
    _mention(conn, "m5", lantern, "msg-3", "conversation_messages", "2026-06-03T10:00:00Z")
    _mention(conn, "m6", bram, "msg-3", "conversation_messages", "2026-06-03T10:00:00Z")
    _mention(conn, "m7", corin, "msg-3", "conversation_messages", "2026-06-03T10:00:00Z")

    # Two goals on records that name people.
    for gid, rec, text in (("g-1", "msg-2", "Finish the lantern prototype"), ("g-2", "msg-3", "Learn to sail")):
        conn.execute(
            "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
            "VALUES (?, ?, 'imessage', ?, '{}')",
            (gid, rec, text),
        )

    # A place, visited twice.
    for eid, at in (("loc-1", "2026-06-04T09:00:00Z"), ("loc-2", "2026-06-09T09:00:00Z")):
        conn.execute(
            "INSERT INTO location_events (event_id, place_name, event_at, source_id) "
            "VALUES (?, 'Harbor Library', ?, 'grow_journal')",
            (eid, at),
        )

    # A topic both topic lanes write: top_topics names Corin, and Corin is named in a member.
    conn.execute("INSERT INTO topic_clusters (cluster_id, label) VALUES ('cl-1', 'Lantern build')")
    for i, rec in enumerate(("msg-1", "msg-3")):
        conn.execute(
            "INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id) "
            "VALUES (?, 'cl-1', ?, 'imessage')",
            (f"tcm-{i}", rec),
        )
    now = "2026-06-10T00:00:00Z"
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
        "confidence, source_refs_json, valid_from, created_at, updated_at) "
        "VALUES ('so-topic', 'memory', 'top_topics', 'cl-1', ?, 0.8, '[]', ?, ?, ?)",
        (json.dumps({"tag": "Lantern build", "related_entities": ["Corin Vale"]}), now, now, now),
    )
    # Two owner facts that differ only in which object asserted them: one edge.
    for oid in ("so-fact-a", "so-fact-b"):
        conn.execute(
            "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
            "confidence, source_refs_json, valid_from, created_at, updated_at) "
            "VALUES (?, 'work', 'fact', ?, ?, 0.9, '[]', ?, ?, ?)",
            (oid, f"fact:{owner}:{oid}", json.dumps({
                "subject_entity_id": owner, "predicate": "works_at", "object_value": "Lantern Works",
                "asserted_by": "owner",
            }), now, now, now),
        )

    # A transcript, so the discourse lenses run (and write the topic's links last).
    conn.execute(
        "INSERT INTO transcripts (transcript_id, title, origin_kind, participation_mode, asr_quality, source_id, "
        "source_record_id) VALUES ('yt:t1', 'Talk', 'youtube', 'ambient', 'generated', 'youtube_transcripts', 'yt:t1')"
    )
    for sid, text, start in (
        ("yt:t1:0", "We argued that Lantern Works is the problem because the meeting was late.", 0.0),
        ("yt:t1:45", "Corin Vale said the program started in 2019 after the hearing.", 45.0),
    ):
        conn.execute(
            "INSERT INTO transcript_segments (segment_id, transcript_id, content, start_sec, event_at, actor_role, "
            "is_from_self, source_id, source_record_id) "
            "VALUES (?, 'yt:t1', ?, ?, '2026-06-05T10:00:00Z', 'ambient', 0, 'youtube_transcripts', ?)",
            (sid, text, start, sid),
        )
    _mention(conn, "m8", lantern, "yt:t1:0", "transcript_segments", "2026-06-05T10:00:00Z", "youtube_transcripts")
    _mention(conn, "m9", corin, "yt:t1:45", "transcript_segments", "2026-06-05T10:00:00Z", "youtube_transcripts")
    conn.commit()
    return {"owner": owner, "ada": ada, "bram": bram, "corin": corin, "lantern": lantern}


def _seed_ids(conn) -> dict:
    """The seeded people, read back by name."""
    named = dict(conn.execute(
        "SELECT canonical_name, entity_id FROM entities WHERE canonical_name IN "
        "('Ada Quill', 'Bram Holt', 'Corin Vale') AND entity_type='person'"))
    return {"ada": named["Ada Quill"], "bram": named["Bram Holt"], "corin": named["Corin Vale"]}


def _rows(conn) -> dict:
    """Every row of the graph tables, by table and rowid, as a digest of all its bytes."""
    out = {}
    for table in GRAPH_TABLES:
        for row in conn.execute(f"SELECT rowid, * FROM {table}"):
            # tuple(): the rebuild may leave a row_factory on the connection
            out[(table, row[0])] = hashlib.sha256(repr(tuple(row)).encode()).hexdigest()
    return out


class _WriteCounter:
    """Counts every INSERT, UPDATE and DELETE on the graph tables made through ``conn``,
    including an UPDATE that writes the values a row already holds."""

    def __init__(self, conn):
        self.conn = conn
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS graph_writes (tbl TEXT, op TEXT)")
        for table in GRAPH_TABLES:
            for op in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(
                    f"CREATE TEMP TRIGGER IF NOT EXISTS count_{table}_{op.lower()} AFTER {op} ON main.{table} "
                    f"BEGIN INSERT INTO graph_writes VALUES ('{table}', '{op}'); END"
                )

    def reset(self):
        self.conn.execute("DELETE FROM temp.graph_writes")

    def counts(self) -> dict:
        return {f"{t}.{o}": n for t, o, n in self.conn.execute(
            "SELECT tbl, op, COUNT(*) FROM temp.graph_writes GROUP BY tbl, op")}


def _edge_types(conn) -> dict:
    return dict(conn.execute(
        "SELECT edge_type, COUNT(*) FROM entity_edges WHERE valid_to IS NULL GROUP BY edge_type"))


# --- 1. a rebuild that finds nothing to change writes nothing -----------------------------------


def test_a_rebuild_over_unchanged_inputs_writes_no_graph_row(conn):
    _seed(conn)
    rebuild_entity_graph(conn)  # the first rebuild builds the graph
    types = _edge_types(conn)
    # Not vacuous: every lane that writes the graph wrote something to compare.
    for edge_type in ("co_occurrence", "communicates_with", "pursues", "relates_to", "located_at",
                      "discusses", "mentions", "participates_in", "works_at"):
        assert types.get(edge_type), f"the fixture built no {edge_type} edge: {types}"
    stamped = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE json_extract(metadata_json, '$.centrality') IS NOT NULL"
    ).fetchone()[0]
    assert stamped >= 5

    counter = _WriteCounter(conn)
    before = _rows(conn)
    report = rebuild_entity_graph(conn)
    after = _rows(conn)

    assert counter.counts() == {}, "a rebuild over unchanged inputs wrote graph rows"
    assert after == before
    assert len(before) > 40
    written = report["rows_written"]
    # Community-name bookkeeping (last_matched_at) is the only write allowed; it is not a graph row.
    assert {k: v for k, v in written.items() if k not in ("communities", "total")} == dict.fromkeys(
        [k for k in written if k not in ("communities", "total")], 0)
    assert report["incomplete"] == []


def test_the_same_inputs_write_nothing_on_every_later_rebuild_too(conn):
    _seed(conn)
    rebuild_entity_graph(conn)
    counter = _WriteCounter(conn)
    for _ in range(3):
        rebuild_entity_graph(conn)
    assert counter.counts() == {}


# --- 2. a real change still lands -----------------------------------------------------------------


def _edge(conn, a, b, edge_type="co_occurrence"):
    lo, hi = sorted((a, b))
    return conn.execute(
        "SELECT edge_id, weight, evidence_count, last_event_at FROM entity_edges "
        "WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=? AND valid_to IS NULL",
        (lo, hi, edge_type),
    ).fetchone()


def test_a_new_mention_adds_its_edge_and_a_removed_one_takes_it_away(conn):
    ids = _seed(conn)
    rebuild_entity_graph(conn)
    assert _edge(conn, ids["ada"], ids["bram"]) is None
    mentions = conn.execute("SELECT mention_count FROM entities WHERE entity_id=?", (ids["ada"],)).fetchone()[0]

    _mention(conn, "m-new-1", ids["ada"], "msg-new", "conversation_messages", "2026-06-20T10:00:00Z")
    _mention(conn, "m-new-2", ids["bram"], "msg-new", "conversation_messages", "2026-06-20T10:00:00Z")
    conn.commit()
    counter = _WriteCounter(conn)
    rebuild_entity_graph(conn)
    edge = _edge(conn, ids["ada"], ids["bram"])
    assert edge is not None and edge[2] == 1 and edge[3] == "2026-06-20T10:00:00Z"
    assert conn.execute("SELECT mention_count FROM entities WHERE entity_id=?",
                        (ids["ada"],)).fetchone()[0] == mentions + 1
    assert counter.counts().get("entity_edges.INSERT", 0) >= 1

    conn.execute("DELETE FROM entity_mentions WHERE mention_id IN ('m-new-1', 'm-new-2')")
    conn.commit()
    rebuild_entity_graph(conn)
    assert _edge(conn, ids["ada"], ids["bram"]) is None
    assert conn.execute("SELECT mention_count FROM entities WHERE entity_id=?",
                        (ids["ada"],)).fetchone()[0] == mentions


def test_new_evidence_updates_an_existing_edge_in_place(conn):
    ids = _seed(conn)
    rebuild_entity_graph(conn)
    edge_id, weight, count, _last = _edge(conn, ids["corin"], ids["lantern"])
    _mention(conn, "m-more-1", ids["corin"], "msg-more", "conversation_messages", "2026-06-21T10:00:00Z")
    _mention(conn, "m-more-2", ids["lantern"], "msg-more", "conversation_messages", "2026-06-21T10:00:00Z")
    conn.commit()
    rebuild_entity_graph(conn)
    edge_id2, weight2, count2, last2 = _edge(conn, ids["corin"], ids["lantern"])
    assert edge_id2 == edge_id, "a changed evidence edge keeps its id"
    assert count2 == count + 1 and weight2 != weight and last2 == "2026-06-21T10:00:00Z"


def test_a_new_goal_and_a_new_visit_reach_the_graph(conn):
    ids = _seed(conn)
    rebuild_entity_graph(conn)
    pursues = _edge_types(conn)["pursues"]
    conn.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
        "VALUES ('g-3', 'msg-1', 'imessage', 'Read every lighthouse log', '{}')"
    )
    conn.execute(
        "INSERT INTO location_events (event_id, place_name, event_at, source_id) "
        "VALUES ('loc-3', 'Harbor Library', '2026-06-15T09:00:00Z', 'grow_journal')"
    )
    conn.commit()
    rebuild_entity_graph(conn)
    assert _edge_types(conn)["pursues"] == pursues + 1
    count, last, meta = conn.execute(
        "SELECT evidence_count, last_event_at, metadata_json FROM entity_edges "
        "WHERE src_entity_id=? AND edge_type='located_at' AND valid_to IS NULL",
        (ids["owner"],),
    ).fetchone()
    assert count == 3 and last == "2026-06-15T09:00:00Z" and json.loads(meta)["visit_count"] == 3


def test_a_new_contact_identifier_reaches_the_person(conn):
    ids = _seed(conn)
    rebuild_entity_graph(conn)
    conn.execute(
        "INSERT INTO contact_identifiers (contact_id, identifier, identifier_type, dataset_id, source_id) "
        "VALUES ('c-ada', 'ada.q@example.test', 'email', 'ds', 'contacts')"
    )
    conn.commit()
    rebuild_entity_graph(conn)
    identifiers = json.loads(conn.execute(
        "SELECT identifiers_json FROM entities WHERE entity_id=?", (ids["ada"],)).fetchone()[0])
    assert "ada.q@example.test" in identifiers


# --- 3. the inline refresh is skipped when the graph is current ----------------------------------


@pytest.fixture()
def refresher(conn, monkeypatch):
    """The real refresher on this database, with the rebuild run in-process and counted."""
    calls = []

    def run_in_process(c):
        calls.append(1)
        return rebuild_entity_graph(c)

    monkeypatch.setattr(rebuild_subprocess, "run_graph_rebuild", run_in_process)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.setattr("topos.core.state.close_thread_db_connection", lambda: None)
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "3600")
    graph_refresh.reset_for_tests()  # the default rebuild function
    return calls


def _mark(conn):
    """What mark_graph_dirty persists: one more dirty generation."""
    conn.execute("UPDATE graph_materialization_state SET dirty_generation = dirty_generation + 1 WHERE id=1")
    conn.commit()


def _generations(conn):
    return tuple(conn.execute(
        "SELECT dirty_generation, materialized_generation FROM graph_materialization_state WHERE id=1"
    ).fetchone())


def test_the_inline_refresh_is_skipped_when_the_graph_is_current(conn, refresher):
    ids = _seed(conn)
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"ran": True}
    # The first rebuild minted the place and the fact's object; the vertices it
    # reads changed, so the next look rebuilds once more, and then it is settled.
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"ran": True}
    assert len(refresher) == 2

    # The pipeline marks before it asks, for every batch; this batch changed nothing.
    counter = _WriteCounter(conn)
    before = _rows(conn)
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"skipped": "inputs unchanged"}
    assert len(refresher) == 2 and counter.counts() == {} and _rows(conn) == before
    dirty, materialized = _generations(conn)
    assert dirty == materialized, "a skip covers the marks it looked at"
    assert graph_refresh.refresh_now_if_dirty() == {"skipped": "not dirty"}

    # This one names two people together: the inline fill rebuilds.
    _mention(conn, "m-x1", ids["ada"], "msg-x", "conversation_messages", "2026-06-22T10:00:00Z")
    _mention(conn, "m-x2", ids["bram"], "msg-x", "conversation_messages", "2026-06-22T10:00:00Z")
    conn.commit()
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"ran": True}
    assert len(refresher) == 3 and _edge(conn, ids["ada"], ids["bram"]) is not None
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"skipped": "inputs unchanged"}
    assert len(refresher) == 3, "a change that mints nothing settles at once"


def test_the_debounced_refresh_and_the_startup_reconcile_skip_too(conn, refresher):
    _seed(conn)
    _settle(conn)
    settled = len(refresher)
    _mark(conn)
    graph_refresh.reconcile_graph_on_startup(conn)
    graph_refresh._refresher._fire()  # the debounce timer firing
    assert len(refresher) == settled


# --- 4. a dirty mark that changes no input does not rebuild --------------------------------------


def _counting_rebuild(calls):
    def rebuild(c):
        calls.append(1)
        return rebuild_entity_graph(c)
    return rebuild


def _settle(conn, rebuild=None):
    """Look until a look skips (a rebuild that mints vertices is looked at once more)."""
    for _ in range(3):
        _mark(conn)
        out = graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild or rebuild_entity_graph)
        if out.get("skipped"):
            return
    raise AssertionError("the graph never settled")


def test_a_rebuild_that_mints_vertices_is_looked_at_once_more_then_settles(conn):
    """The stored fingerprint is the one read before the rebuild, so vertices the
    rebuild itself mints (a place, a fact's object) differ at the next look: one
    more rebuild, which mints nothing, and from then on marks skip."""
    _seed(conn)
    calls = []
    rebuild = _counting_rebuild(calls)
    assert graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild).get("ran")
    assert graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild).get("ran")
    for _ in range(3):
        _mark(conn)
        assert graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild)["skipped"] == "inputs unchanged"
    assert calls == [1, 1]


def test_a_mark_that_changes_no_input_does_not_rebuild(conn):
    _seed(conn)
    _settle(conn)
    calls = []
    rebuild = _counting_rebuild(calls)
    counter = _WriteCounter(conn)
    before = _rows(conn)
    for _ in range(3):
        _mark(conn)
        out = graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild)
        assert out["skipped"] == "inputs unchanged"
    assert calls == []
    assert counter.counts() == {} and _rows(conn) == before
    assert _generations(conn)[0] == _generations(conn)[1]


def test_rows_the_graph_does_not_read_do_not_rebuild_it(conn):
    """New canonical rows nothing points at, derived objects the graph does not project,
    and the rebuild's own outputs: none of them is a reason to rebuild."""
    ids = _seed(conn)
    _settle(conn)
    calls = []
    rebuild = _counting_rebuild(calls)
    now = "2026-06-30T00:00:00Z"
    conn.execute("INSERT INTO activity_events (event_id, source_id, activity_type, url, occurred_at) "
                 "VALUES ('visit-1', 'browser_visits', 'visit', 'https://example.test/', ?)", (now,))
    conn.execute("INSERT INTO timeline (event_at, record_id, source_id, canonical_table, record_type) "
                 "VALUES (?, 'visit-1', 'browser_visits', 'activity_events', 'visit')", (now,))
    conn.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
                 "confidence, source_refs_json, valid_from, created_at, updated_at) "
                 "VALUES ('so-bi', 'interests', 'browsing_interest', 'bi:1', '{}', 0.5, '[]', ?, ?, ?)",
                 (now, now, now))
    conn.execute("UPDATE entities SET mention_count = mention_count + 7, updated_at = ? WHERE entity_id = ?",
                 (now, ids["corin"]))
    conn.execute("UPDATE entity_edges SET weight = weight + 1, updated_at = ? "
                 "WHERE edge_type IN ('co_occurrence', 'pursues')", (now,))
    conn.execute("UPDATE community_names SET times_matched = times_matched + 1, last_matched_at = ?", (now,))
    conn.commit()
    _mark(conn)
    assert graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild)["skipped"] == "inputs unchanged"
    assert calls == []


def test_a_change_that_lands_mid_rebuild_is_rebuilt_next_time(conn):
    """The stored fingerprint is the one read BEFORE the rebuild read its inputs."""
    ids = _seed(conn)
    _settle(conn)
    _mention(conn, "m-early", ids["bram"], "msg-early", "conversation_messages", "2026-06-23T09:00:00Z")
    conn.commit()
    calls = []

    def rebuild_while_a_mention_lands(c):
        calls.append(1)
        report = rebuild_entity_graph(c)
        if len(calls) == 1:
            _mention(c, "m-late", ids["ada"], "msg-late", "conversation_messages", "2026-06-23T10:00:00Z")
            c.commit()
        return report

    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild_while_a_mention_lands)
    out = graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild_while_a_mention_lands)
    assert out.get("ran") is True and calls == [1, 1]
    assert graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild_while_a_mention_lands).get("skipped")


def test_a_mark_that_lands_mid_rebuild_stays_outstanding(conn):
    _seed(conn)

    def rebuild_while_marked(c):
        report = rebuild_entity_graph(c)
        _mark(c)
        return report

    _mark(conn)
    dirty_before = _generations(conn)[0]
    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild_while_marked)
    dirty, materialized = _generations(conn)
    assert materialized == dirty_before and dirty == dirty_before + 1


def test_an_incomplete_rebuild_is_retried_after_the_retry_window_only(conn, monkeypatch):
    _seed(conn)
    _settle(conn)
    _mention(conn, "m-inc", _seed_ids(conn)["corin"], "msg-inc", "conversation_messages", "2026-06-24T10:00:00Z")
    conn.commit()
    calls = []

    def incomplete(c):
        calls.append(1)
        report = rebuild_entity_graph(c)
        report["incomplete"] = ["goal_embeddings"]
        return report

    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=incomplete)
    stored = conn.execute("SELECT input_fingerprint FROM graph_materialization_state").fetchone()[0]
    assert stored.endswith("|incomplete")
    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=incomplete)
    assert calls == [1], "inside the retry window an unchanged graph is not rebuilt"
    # An hour ago: past the 30-minute retry window, well inside the 6-hour age limit.
    an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.execute("UPDATE graph_materialization_state SET last_run_at=?", (an_hour_ago,))
    conn.commit()
    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=_counting_rebuild(calls))
    assert calls == [1, 1]
    stored = conn.execute("SELECT input_fingerprint FROM graph_materialization_state").fetchone()[0]
    assert not stored.endswith("|incomplete")
    conn.execute("UPDATE graph_materialization_state SET last_run_at=?", (an_hour_ago,))
    conn.commit()
    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=_counting_rebuild(calls))
    assert calls == [1, 1], "a complete rebuild an hour old is inside the age limit"


def test_unchanged_inputs_rebuild_once_the_last_rebuild_is_older_than_the_age_limit(conn, monkeypatch):
    _seed(conn)
    _settle(conn)
    calls = []
    rebuild = _counting_rebuild(calls)
    conn.execute("UPDATE graph_materialization_state SET last_run_at='2026-01-01T00:00:00+00:00'")
    conn.commit()
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_MAX_AGE_S", "0")
    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild)
    assert calls == [], "0 turns the age limit off"
    monkeypatch.delenv("TOPOS_GRAPH_REFRESH_MAX_AGE_S")
    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild)
    assert calls == [1]
    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild)
    assert calls == [1], "and the rebuild it caused restarts the clock"


def test_an_unreadable_fingerprint_rebuilds(conn, monkeypatch):
    _seed(conn)
    _settle(conn)
    calls = []
    rebuild = _counting_rebuild(calls)
    monkeypatch.setattr("topos.features.entities.graph_inputs.graph_input_fingerprint", lambda c: None)
    graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild)
    assert calls == [1]
    stored = conn.execute("SELECT input_fingerprint FROM graph_materialization_state").fetchone()[0]
    assert stored is None, "an unknown fingerprint is never stored as if known"


def test_a_derivation_that_starts_while_the_inputs_are_read_defers_the_rebuild(conn, monkeypatch):
    from topos.enrichment import pipeline_activity
    from topos.features.entities import graph_inputs
    from topos.storage.db.write_gate import WriteGateDeferred

    _seed(conn)
    pipeline_activity.reset_for_tests()
    real = graph_inputs.graph_input_fingerprint
    started = pipeline_activity.derivation_in_flight()

    def fingerprint_then_derivation_starts(c):
        value = real(c)
        started.__enter__()
        return value

    monkeypatch.setattr(graph_inputs, "graph_input_fingerprint", fingerprint_then_derivation_starts)
    calls = []
    try:
        with pytest.raises(WriteGateDeferred):
            graph_refresh.rebuild_if_inputs_changed(conn, rebuild=_counting_rebuild(calls))
    finally:
        started.__exit__(None, None, None)
        pipeline_activity.reset_for_tests()
    assert calls == []


def test_the_dangling_sweep_runs_on_a_skipped_trigger(conn):
    """Closing objects whose evidence is gone keeps its old cadence: every trigger."""
    _seed(conn)
    _settle(conn)
    rebuild = _counting_rebuild([])
    now = "2026-06-30T00:00:00Z"
    conn.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
                 "confidence, source_refs_json, valid_from, created_at, updated_at) "
                 "VALUES ('so-gone', 'memory', 'message_topics', 'mt:1', '{}', 0.5, ?, ?, ?, ?)",
                 (json.dumps([{"table": "conversation_messages", "record_id": "msg-deleted"}]), now, now, now))
    conn.commit()
    out = graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild)
    assert out == {"skipped": "inputs unchanged", "dangling_closed": 1}
    assert conn.execute("SELECT valid_to FROM signal_objects WHERE object_id='so-gone'").fetchone()[0]


# --- every node: a fresh install and an upgrading one get the same gate --------------------------


def test_a_node_upgrading_from_1_4_3_gets_the_fingerprint_column_at_its_first_rebuild(tmp_path):
    from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

    c = sqlite3.connect(str(tmp_path / "old.db"))
    apply_pipeline_jobs_v1_up(c)   # the table exactly as 1.4.3 created it
    assert "input_fingerprint" not in {r[1] for r in c.execute("PRAGMA table_info(graph_materialization_state)")}
    calls = []
    graph_refresh.rebuild_if_inputs_changed(c, rebuild=lambda conn: calls.append(1) or {})
    assert calls == [1]
    assert "input_fingerprint" in {r[1] for r in c.execute("PRAGMA table_info(graph_materialization_state)")}
    graph_refresh.rebuild_if_inputs_changed(c, rebuild=lambda conn: calls.append(1) or {})
    assert calls == [1]
    # 1.4.3 reads the table by column name and keeps working with the extra column.
    c.execute("UPDATE graph_materialization_state SET dirty_generation = dirty_generation + 1 WHERE id = 1")
    assert c.execute("SELECT dirty_generation, materialized_generation FROM graph_materialization_state "
                     "WHERE id=1").fetchone()[0] == 1
    c.close()


def test_the_fingerprint_is_stable_across_connections(conn, tmp_path):
    _seed(conn)
    first = graph_input_fingerprint(conn)
    other = sqlite3.connect(str(tmp_path / "node.db"))
    try:
        assert first is not None and graph_input_fingerprint(other) == first
    finally:
        other.close()


# --- a rebuild that did not finish a part says so ------------------------------------------------


def _boom(*args, **kwargs):
    raise RuntimeError("lane failed")


@pytest.mark.parametrize("part, target", [
    ("facts", "topos.features.entities.fact_materializer.materialize_signal_objects_to_graph"),
    ("enrichments", "topos.features.entities.graph_enrichers.materialize_graph_enrichments"),
    ("discourse", "topos.features.entities.discourse_graph.materialize_discourse_lenses_to_graph"),
    ("dossiers", "topos.features.entities.dossier.refresh_dossiers"),
    ("centrality", "networkx.betweenness_centrality"),
])
def test_a_part_that_fails_marks_the_rebuild_incomplete(conn, monkeypatch, part, target):
    _seed(conn)
    monkeypatch.setattr(target, _boom)
    report = rebuild_entity_graph(conn)
    assert report["incomplete"] == [part]


def test_goals_clustered_without_the_embedder_mark_the_rebuild_incomplete(conn, monkeypatch):
    _seed(conn)
    monkeypatch.setattr(graph_enrichers, "_default_goal_embedder", lambda texts: None)
    report = rebuild_entity_graph(conn)
    assert report["goal_clustering"] == "tokens" and report["incomplete"] == ["goal_embeddings"]


def test_a_complete_rebuild_marks_nothing(conn):
    _seed(conn)
    report = rebuild_entity_graph(conn)
    assert report["goal_clustering"] == "embeddings" and report["incomplete"] == []
