"""The read-path indexes from the scaling assessment: outside every contract, inside every plan.

Three b-trees on the canonical database and two on the clock's event log turn the scans the
assessment measured into seeks: the merge trigger's per-fact probe of the event log, the
owner's identity lookups, the self-row and merge-tombstone reads, and the exact-copy count
over both message tables. None of them changes a stored value, a pinned digest or an answer.
These tests pin that the indexes exist where they should, that the clock neither requires
nor notices them, that the upgrade lanes carry them, that the planner actually uses them,
and that every answer is the one the scan gave.
"""
import shutil
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import attest, corpus, decision, edit, owner  # noqa: F401 (fixtures)
from tests.permissions_v2.test_exclusion_floor import make_legacy, upgrade_to_current
from tests.permissions_v2.test_owner_identity_binding import OWNER, do_attest
from topos.permissions_v2 import evidence as evidence_module
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceResolver, LEAF_TABLES
from topos.permissions_v2.identity import (composition_revision, entries, identity_event_count,
    last_identity_event, legacy_owner_subjects, rekeyed_facts, self_entity_ids)
from topos.permissions_v2.protection_clock import (EVENTS, EVENT_INDEXES, TABLE, clock_state,
    closure_protection_revision, current_protection_revision, ensure_protection_clock, identity_coverage,
    identity_event_key, rekey_event_key, resync_identity_coverage)
from topos.storage.db.migrations.permissions_read_path_indexes_v1 import (CONTENT_KEY, INDEXES,
    apply_permissions_read_path_indexes_v1_up)

ARTIFACT, SOURCE = "permissions_v2_protection_events_artifact", "permissions_v2_protection_events_source"


def indexes_on(conn, table):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,))}


def plan(conn, sql, args=()):
    return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]


def uses(conn, index, sql, args=()):
    steps = plan(conn, sql, args)
    assert any(index in step for step in steps), steps
    assert not any(step.startswith(f"SCAN {EVENTS}") or step.startswith("SCAN entities") or step.startswith("SCAN entity_merge_tombstones")
                   for step in steps), steps
    return steps


def drop_event_indexes(conn):
    for name in EVENT_INDEXES:
        conn.execute(f"DROP INDEX {name}")


# --- the event log: R5/W4 ------------------------------------------------------------------------------


def test_the_event_log_indexes_are_installed_with_the_clock(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        assert set(EVENT_INDEXES) <= indexes_on(conn, EVENTS)


def test_the_clock_neither_requires_nor_notices_its_indexes(corpus):
    """`clock_state` compares the state row, the trigger text and the table declarations: never an index."""
    path = corpus[0].path
    with sqlite3.connect(path) as conn:
        do_attest(conn, OWNER)
    with sqlite3.connect(path) as conn:
        with_indexes, revision = clock_state(conn), current_protection_revision(conn, owner_id="owner-1")
        drop_event_indexes(conn)
        assert clock_state(conn) == with_indexes
        assert current_protection_revision(conn, owner_id="owner-1") == revision
        conn.execute(f"CREATE INDEX stray ON {EVENTS}(sequence, source)")
        assert clock_state(conn) == with_indexes
        conn.execute(f"CREATE TRIGGER permissions_v2_stray AFTER INSERT ON {EVENTS} BEGIN SELECT 1; END")
        with pytest.raises(PolicyError, match="protection_clock_unavailable"):
            clock_state(conn)


def test_an_existing_clock_gains_the_indexes_at_the_next_start(corpus):
    path = corpus[0].path
    with sqlite3.connect(path) as conn:
        drop_event_indexes(conn)
        marked = clock_state(conn)
    ensure_protection_clock(path, owner_id="owner-1", allow_install=False)
    with sqlite3.connect(path) as conn:
        assert set(EVENT_INDEXES) <= indexes_on(conn, EVENTS)
        assert clock_state(conn) == marked, "an index is not a mutation: the generation did not move"
    ensure_protection_clock(path, owner_id="owner-1", allow_install=False)
    with sqlite3.connect(path) as conn:
        assert clock_state(conn) == marked


def test_a_clock_the_node_refuses_is_left_without_them(corpus):
    """The indexes are created after the clock verifies, inside the same transaction, so a refusal leaves no DDL behind."""
    path = corpus[0].path
    with sqlite3.connect(path) as conn:
        drop_event_indexes(conn)
        conn.execute("DROP TRIGGER permissions_v2_intelligence_exclusions_insert")
    with pytest.raises(PolicyError, match="protection_clock_unavailable"):
        ensure_protection_clock(path, owner_id="owner-1", allow_install=False)
    with sqlite3.connect(path) as conn:
        assert not set(EVENT_INDEXES) & indexes_on(conn, EVENTS)


def test_the_v4_upgrade_rebuilds_the_event_log_with_its_indexes(corpus):
    old = make_legacy(corpus)
    upgrade_to_current(corpus[0].path, old)
    with sqlite3.connect(corpus[0].path) as conn:
        assert set(EVENT_INDEXES) <= indexes_on(conn, EVENTS)
        clock_state(conn)


def test_a_coverage_resync_keeps_them(corpus):
    path = corpus[0].path
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE entity_mentions")
        clock_id, generation = conn.execute(f"SELECT clock_id,generation FROM {TABLE}").fetchone()
    resync_identity_coverage(path, owner_id="owner-1", expected_clock_id=clock_id, expected_generation=generation)
    with sqlite3.connect(path) as conn:
        assert set(EVENT_INDEXES) <= indexes_on(conn, EVENTS)
        clock_state(conn)


def test_every_event_log_lookup_is_a_seek(corpus):
    """The trigger probes, the identity lookups and both closure filters use the two indexes, never a scan."""
    with sqlite3.connect(corpus[0].path) as conn:
        uses(conn, ARTIFACT, f"SELECT max(generation) FROM {EVENTS} WHERE artifact_key=?", ("identity|x",))
        uses(conn, ARTIFACT, f"SELECT count(*) FROM {EVENTS} WHERE artifact_key=?", ("identity|x",))
        uses(conn, ARTIFACT, f"SELECT 1 FROM {EVENTS} WHERE artifact_key=? LIMIT 1", ("fact_rekeyed|x",))
        # the fact re-key trigger's probe, and the once-per-generation mention probe
        uses(conn, ARTIFACT, f"SELECT 1 FROM {TABLE} WHERE singleton=1 AND NOT EXISTS(SELECT 1 FROM {EVENTS} e "
                             f"WHERE e.artifact_key='fact_rekeyed|'||?)", ("x",))
        uses(conn, ARTIFACT, f"SELECT 1 FROM {TABLE} WHERE singleton=1 AND NOT EXISTS(SELECT 1 FROM {EVENTS} e "
                             f"WHERE e.artifact_key='identity|'||? AND e.generation=(SELECT generation FROM {TABLE} WHERE singleton=1))", ("x",))
        # the closure revision's own two filters: its record terms constrain source AND key, and are
        # exact covering seeks on the source index, which carries the key; the prefix and entity
        # terms range over one source's events
        steps = uses(conn, SOURCE, f"SELECT max(generation) FROM {EVENTS} WHERE (source='owner_only_records' AND artifact_key=?) "
                                   "OR (source='intelligence_exclusions' AND artifact_key=?) "
                                   "OR (source='intelligence_exclusions' AND lower(artifact_key) LIKE ? ESCAPE '\\')",
                     ("conversation_messages|message-1", "record|message-1", "fact|self:prefers%"))
        assert steps.count(f"SEARCH {EVENTS} USING COVERING INDEX {SOURCE} (source=? AND artifact_key=?)") == 2, steps
        assert steps.count(f"SEARCH {EVENTS} USING COVERING INDEX {SOURCE} (source=?)") == 1, steps
        steps = uses(conn, SOURCE, f"SELECT max(generation) FROM {EVENTS} WHERE source='entity_blackholes' "
                                   "OR (source='intelligence_exclusions' AND artifact_key LIKE 'entity|%')")
        assert steps.count(f"SEARCH {EVENTS} USING COVERING INDEX {SOURCE} (source=?)") == 2, steps


def test_the_indexes_change_no_event_no_generation_and_no_revision(corpus, tmp_path):
    """The same owner and merge writes, with and without the indexes, leave identical logs, clocks and revisions."""
    path = corpus[0].path
    bare = tmp_path / "bare.db"
    shutil.copy(path, bare)
    with sqlite3.connect(bare) as conn:
        drop_event_indexes(conn)

    def churn(conn):
        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) VALUES('gone','person','G','g',0)")
        conn.executemany("INSERT INTO entity_mentions(mention_id,entity_id,record_id,surface_text) VALUES(?,?,?,?)",
                         [(f"m-{index}", "gone", f"rec-{index}", "Alias") for index in range(40)])
        conn.executemany("INSERT INTO signal_objects(object_id,signal_dimension,object_type,object_key,payload_json,valid_from,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         [(f"fact-{index}", "facts", "fact", f"gone:pred{index}:v", "{}", "2026", "2026", "2026") for index in range(30)])
        do_attest(conn, OWNER)
        conn.execute("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into) VALUES('gone',?)", (OWNER,))
        conn.execute("UPDATE entity_mentions SET entity_id=? WHERE entity_id='gone'", (OWNER,))
        for index in range(30):
            conn.execute("UPDATE signal_objects SET object_key=? WHERE object_id=?", (f"{OWNER}:pred{index}:v", f"fact-{index}"))
            conn.execute("UPDATE signal_objects SET object_key=? WHERE object_id=?", (f"{OWNER}:pred{index}:v2", f"fact-{index}"))
        conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('conversation_messages','message-1')")
        conn.execute("DELETE FROM owner_only_records WHERE record_id='message-1'")

    def observed(where):
        with sqlite3.connect(where) as conn:
            churn(conn)
        with sqlite3.connect(where) as conn:
            log = conn.execute(f"SELECT sequence,generation,source,artifact_key FROM {EVENTS} ORDER BY sequence").fetchall()
            return (log, clock_state(conn), current_protection_revision(conn, owner_id="owner-1"),
                    closure_protection_revision(conn, owner_id="owner-1", records=[("conversation_messages", "message-1")],
                                                fact_prefixes=[f"{OWNER}:pred1"]),
                    last_identity_event(conn, OWNER), identity_event_count(conn, OWNER),
                    rekeyed_facts(conn, [f"fact-{index}" for index in range(30)]),
                    {key: value.state for key, value in entries(conn).items()})

    assert observed(path) == observed(bare)
    with sqlite3.connect(path) as conn:
        assert len([row for row in conn.execute(f"SELECT artifact_key FROM {EVENTS}") if row[0].startswith("fact_rekeyed|")]) == 30, \
            "one re-key event per fact, however many times it was re-keyed"


# --- entities and merge tombstones: R9 --------------------------------------------------------------------


@pytest.fixture
def migrated(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        apply_permissions_read_path_indexes_v1_up(conn)
    return corpus


def test_the_self_row_and_merge_lookups_use_their_indexes(migrated):
    with sqlite3.connect(migrated[0].path) as conn:
        uses(conn, "idx_entities_is_self", "SELECT entity_id FROM entities WHERE is_self=1")
        uses(conn, "idx_entity_merge_tombstones_merged_into",
             "SELECT absorbed_entity_id FROM entity_merge_tombstones WHERE merged_into=?", (OWNER,))
        assert "idx_entities_is_self" not in " ".join(plan(conn, "SELECT entity_id FROM entities WHERE is_self=0")), \
            "a partial index answers only the rows it holds"


def test_the_identity_answers_are_unchanged_by_the_indexes(corpus, tmp_path):
    path = corpus[0].path
    bare = tmp_path / "bare.db"
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) VALUES('second-self','person','P','p',1)")
        conn.execute("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into) VALUES('gone',?)", (OWNER,))
        conn.execute("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into) VALUES(?,'elsewhere')", (OWNER,))
    shutil.copy(path, bare)
    with sqlite3.connect(path) as conn:
        apply_permissions_read_path_indexes_v1_up(conn)

    def answers(where):
        with sqlite3.connect(where) as conn:
            legacy = None
            try:
                legacy = legacy_owner_subjects(conn)
            except PolicyError as exc:
                legacy = exc.code
            return (self_entity_ids(conn), legacy, composition_revision(conn, OWNER), composition_revision(conn, "gone"),
                    {key: value.state for key, value in entries(conn).items()})

    assert answers(path) == answers(bare)
    assert answers(path)[0] == {OWNER, "second-self"} and answers(path)[1] == "owner_subject_ambiguous"


# --- the exact-copy count: R1 --------------------------------------------------------------------------------


def test_the_copy_count_spelling_is_the_index_key(corpus):
    """The predicate is spelled from the migration's tuple, so the two cannot drift apart unnoticed."""
    for expression in CONTENT_KEY:
        assert expression in evidence_module._COPY_COUNT
    assert evidence_module._COPY_COUNT.endswith("AND content=?1")
    assert [name for _table, _columns, name, _sql in INDEXES if name.endswith("_content_key")] == \
        [f"idx_{table}_content_key" for table in LEAF_TABLES]


def test_the_copy_count_is_answered_from_the_content_key(migrated):
    with sqlite3.connect(migrated[0].path) as conn:
        for table in LEAF_TABLES:
            steps = plan(conn, evidence_module._COPY_COUNT.format(table=table), ("I enjoy reading history books.",))
            assert steps == [f"SEARCH {table} USING INDEX idx_{table}_content_key (<expr>=? AND <expr>=?)"], steps


def known_copies(conn, content):
    return EvidenceResolver._known_copies(conn, None, {"content": content})


def test_the_statement_the_resolver_runs_is_the_indexed_one(migrated):
    """Not the constant: the SQL `_known_copies` actually hands SQLite, one statement per leaf table."""
    executed = []
    with sqlite3.connect(migrated[0].path) as conn:
        conn.set_trace_callback(executed.append)
        assert known_copies(conn, "I enjoy reading history books.") is False
    assert executed == [evidence_module._COPY_COUNT.format(table=table).replace("?1", "'I enjoy reading history books.'")
                        for table in LEAF_TABLES], executed


@pytest.mark.parametrize("indexed", [False, True], ids=["scan", "index"])
def test_the_copy_answers_are_the_ones_the_scan_gave(corpus, indexed):
    path = corpus[0].path
    if indexed:
        with sqlite3.connect(path) as conn:
            apply_permissions_read_path_indexes_v1_up(conn)
    long = "a" * 64
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO conversation_messages(message_id,content) VALUES(?,?)", [
            ("twin-1", "twins"), ("twin-2", "twins"),
            ("tail-1", long + "x"), ("tail-2", long + "y"),  # same length, same first 64 characters, different text
            ("nul-1", "ab\x00c"), ("nul-2", "ab\x00c"),      # SQLite's length() stops at the NUL; the key must too
            ("case-1", "Twins"),
        ])
        conn.execute("INSERT INTO ai_chat_messages(message_id,content) VALUES('cross','I enjoy reading history books.')")
    with sqlite3.connect(path) as conn:
        assert known_copies(conn, "twins") is True
        assert known_copies(conn, "Twins") is False, "equality is exact, as before"
        assert known_copies(conn, long + "x") is False, "the full text decides, not the key"
        assert known_copies(conn, "ab\x00c") is True
        assert known_copies(conn, "ab\x00d") is False
        assert known_copies(conn, "I enjoy reading history books.") is True, "one copy in each table"
        assert known_copies(conn, "I attend my reading group.") is False
        assert known_copies(conn, "never written") is False
        with pytest.raises(PolicyError, match="evidence_content_unknown"):
            known_copies(conn, "   ")


@pytest.mark.parametrize("indexed", [False, True], ids=["scan", "index"])
def test_an_independent_copy_still_withholds(corpus, indexed):
    if indexed:
        with sqlite3.connect(corpus[0].path) as conn:
            apply_permissions_read_path_indexes_v1_up(conn)
    edit(corpus, "UPDATE ai_chat_messages SET content='I enjoy reading history books.'")
    attest(corpus)
    assert decision(corpus).reason_code == "independent_copy_lineage"


def test_a_database_without_the_content_key_still_scans_and_still_answers(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        for table in LEAF_TABLES:
            assert plan(conn, evidence_module._COPY_COUNT.format(table=table), ("x",)) == [f"SCAN {table}"]
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
