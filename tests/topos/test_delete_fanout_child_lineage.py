"""Deleting a fan-out child must not delete its parent's rows.

``journal_location_fanout`` splits one journal row into a ``journal_entries`` row
and a ``location_events`` child, and writes the PARENT's canonical id into the
child's ``source_record_id``. ``_delete_upstream_rows`` treats that column as an
upstream key and deletes ``WHERE record_id = ?`` across every upstream table — so
deleting one place event stripped the journal entry's flat source row, timeline
entries, entity mentions, triage verdict and cluster membership (1,073 rows,
measured on the live node 2026-08-27) while the journal entry itself survived,
invisible to retrieval, the graph and the timeline.

The two directions are both gated here, because they fail differently:

  * deleting the CHILD must not reach the parent (over-delete, silent data loss);
  * deleting the PARENT must still reach everything that is genuinely its own
    (guarding against a fix that simply switches the upstream sweep off).

The discrimination under test is exact, not heuristic: an ordinary canonical row
has ``source_record_id`` equal to its own id, and a fan-out child's points at a
different canonical table's primary key.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.data_explorer_row_delete import _resolve_lineage, delete_database_rows

PARENT = "tl-1"
CHILD = "tl-1-loc"
SOURCE = "grow_journal"


def _seed(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE journal_entries (
            entry_id TEXT PRIMARY KEY,
            content TEXT,
            place_name TEXT,
            source_id TEXT,
            source_record_id TEXT
        );
        CREATE TABLE location_events (
            event_id TEXT PRIMARY KEY,
            place_name TEXT,
            source_id TEXT,
            source_record_id TEXT
        );
        -- upstream: raw_ prefix and (record_id, source_id) both classify here
        CREATE TABLE raw_growjournal_ui_stream (
            source_system TEXT,
            source_record_id TEXT,
            payload_json TEXT
        );
        CREATE TABLE entity_mentions (
            mention_id TEXT PRIMARY KEY,
            entity_id TEXT,
            record_id TEXT,
            source_id TEXT
        );
        CREATE TABLE timeline (
            event_at TEXT,
            record_id TEXT,
            source_id TEXT,
            canonical_table TEXT
        );
        -- downstream
        CREATE TABLE signal_embeddings (
            embedding_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            payload_json TEXT
        );

        INSERT INTO journal_entries VALUES
            ('tl-1', 'Worked at the Convent with Ada', 'Northgate- The Foundry',
             'grow_journal', 'tl-1');
        -- the fan-out child: its source_record_id is the PARENT's canonical id
        INSERT INTO location_events VALUES
            ('tl-1-loc', 'Northgate- The Foundry', 'grow_journal', 'tl-1');

        INSERT INTO raw_growjournal_ui_stream VALUES ('grow_journal', 'tl-1', '{}');
        INSERT INTO entity_mentions VALUES ('m-parent', 'ent-ada', 'tl-1', 'grow_journal');
        INSERT INTO entity_mentions VALUES ('m-child', 'ent-place', 'tl-1-loc', 'grow_journal');
        INSERT INTO timeline VALUES ('2026-07-06', 'tl-1', 'grow_journal', 'journal_entries');
        INSERT INTO timeline VALUES ('2026-07-06', 'tl-1-loc', 'grow_journal', 'location_events');
        INSERT INTO signal_embeddings VALUES ('emb-parent', 'tl-1', 'grow_journal', '{}');
        INSERT INTO signal_embeddings VALUES ('emb-child', 'tl-1-loc', 'grow_journal', '{}');
        """
    )
    conn.commit()
    return conn


@pytest.fixture()
def conn(tmp_path):
    c = _seed(tmp_path / "fanout_delete.db")
    yield c
    c.close()


def _count(conn, table, where, params=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


# ------------------------------------------------------------ lineage resolution


def _row(conn, table, pk_column, row_id):
    cur = conn.execute(f"SELECT * FROM {table} WHERE {pk_column}=?", (row_id,))
    names = [d[0] for d in cur.description]
    return dict(zip(names, cur.fetchone()))


def test_child_does_not_adopt_its_parents_id_as_an_upstream_key(conn):
    row = _row(conn, "location_events", "event_id", CHILD)
    assert row["source_record_id"] == PARENT, "fixture must reproduce the live shape"

    anchor = _resolve_lineage(
        conn,
        table_name="location_events",
        pk_column="event_id",
        row_id=CHILD,
        row=row,
    )

    assert anchor.source_record_id == CHILD, (
        "the upstream sweep must be anchored on the child's OWN id; anchoring it "
        "on the parent's is what deleted the parent's rows"
    )
    assert anchor.parent_canonical_table == "journal_entries"
    assert anchor.parent_canonical_id == PARENT


def test_ordinary_canonical_row_keeps_its_self_referential_source_record_id(conn):
    """Control: the fix must not fire on the normal shape.

    Every ``journal_entries`` row on the live node has
    ``source_record_id = entry_id``. If the guard mis-classified that as a parent
    pointer it would silently narrow every ordinary upstream delete.
    """
    row = {
        "entry_id": PARENT,
        "content": "x",
        "source_id": SOURCE,
        "source_record_id": PARENT,
    }

    anchor = _resolve_lineage(
        conn, table_name="journal_entries", pk_column="entry_id", row_id=PARENT, row=row
    )

    assert anchor.source_record_id == PARENT
    assert anchor.parent_canonical_table is None
    assert anchor.parent_canonical_id is None


def test_an_external_source_id_that_matches_nothing_is_left_alone(conn):
    """A genuine source-system id is not a parent pointer either."""
    conn.execute(
        "INSERT INTO journal_entries VALUES (?,?,?,?,?)",
        ("tl-9", "note", None, SOURCE, "imessage-guid-ABC123"),
    )
    conn.commit()
    row = {
        "entry_id": "tl-9",
        "content": "note",
        "source_id": SOURCE,
        "source_record_id": "imessage-guid-ABC123",
    }

    anchor = _resolve_lineage(
        conn, table_name="journal_entries", pk_column="entry_id", row_id="tl-9", row=row
    )

    assert anchor.source_record_id == "imessage-guid-ABC123"
    assert anchor.parent_canonical_table is None


# ------------------------------------------------------------- the actual delete


@pytest.mark.parametrize("scope", ["with_upstream", "full_lineage"])
def test_deleting_the_child_leaves_the_parents_rows_intact(conn, scope):
    """The regression. Both scopes that reach upstream used to destroy the parent."""
    delete_database_rows(
        conn, table_name="location_events", row_ids=[CHILD], scope=scope
    )

    assert _count(conn, "journal_entries", "entry_id=?", (PARENT,)) == 1
    assert _count(conn, "raw_growjournal_ui_stream", "source_record_id=?", (PARENT,)) == 1
    assert _count(conn, "entity_mentions", "record_id=?", (PARENT,)) == 1
    assert _count(conn, "timeline", "record_id=?", (PARENT,)) == 1
    assert _count(conn, "signal_embeddings", "record_id=?", (PARENT,)) == 1

    # ...and the child's own rows are gone, so this is not a no-op delete.
    assert _count(conn, "location_events", "event_id=?", (CHILD,)) == 0


@pytest.mark.parametrize("scope", ["with_upstream", "full_lineage"])
def test_deleting_the_parent_still_reaches_its_own_upstream(conn, scope):
    """Control: the guard must not have switched the upstream sweep off."""
    delete_database_rows(
        conn, table_name="journal_entries", row_ids=[PARENT], scope=scope
    )

    assert _count(conn, "journal_entries", "entry_id=?", (PARENT,)) == 0
    assert _count(conn, "raw_growjournal_ui_stream", "source_record_id=?", (PARENT,)) == 0


def test_derived_rows_belong_to_downstream_not_upstream(conn):
    """``entity_mentions`` and ``timeline`` are derived, and used to be neither.

    An earlier version of this file asserted ``with_upstream`` removed them,
    which encoded the bug: ``_is_upstream_table`` reads ``record_id + source_id``
    as an upstream signature and both carry it, so they were swept by the scope
    that means "the source this came FROM" and skipped by the one that means
    "everything derived from it". The owner got the opposite of both promises.
    """
    delete_database_rows(
        conn, table_name="journal_entries", row_ids=[PARENT], scope="with_upstream"
    )
    assert _count(conn, "entity_mentions", "record_id=?", (PARENT,)) == 1, (
        "derived rows are not upstream"
    )


@pytest.mark.parametrize("scope", ["with_downstream", "full_lineage"])
def test_the_derived_scopes_reach_them(conn, scope):
    delete_database_rows(
        conn, table_name="journal_entries", row_ids=[PARENT], scope=scope
    )

    assert _count(conn, "entity_mentions", "record_id=?", (PARENT,)) == 0
    assert _count(conn, "timeline", "record_id=?", (PARENT,)) == 0


def test_row_only_on_the_child_touches_exactly_one_row(conn):
    """``row_only`` stays literal — the decision taken 2026-08-27.

    A provenance unit is breadth across siblings; the four existing scopes are
    depth along a derivation chain. Overloading one with the other is how someone
    tidying a stray place row loses their journal entry.
    """
    result = delete_database_rows(
        conn, table_name="location_events", row_ids=[CHILD], scope="row_only"
    )

    assert result.rows_deleted == 1
    assert _count(conn, "journal_entries", "entry_id=?", (PARENT,)) == 1
    assert _count(conn, "entity_mentions", "record_id=?", (CHILD,)) == 1
    assert _count(conn, "signal_embeddings", "record_id=?", (CHILD,)) == 1


# ------------------------------------ findings from the adversarial review


def test_a_cross_source_id_collision_is_not_a_parent(conn):
    """Ids are unique only WITHIN a source.

    Without the source_id constraint, a value that happens to match another
    connector's canonical id re-anchors the upstream sweep onto this row's own id
    — which NARROWS a legitimate delete. That failure is silent, unlike the
    over-delete, so nothing would ever surface it.
    """
    conn.execute(
        "INSERT INTO journal_entries VALUES (?,?,?,?,?)",
        ("shared-id-99", "an unrelated entry from another connector", None, "chatgpt", "tl-1"),
    )
    conn.commit()
    row = _row(conn, "journal_entries", "entry_id", "shared-id-99")

    anchor = _resolve_lineage(
        conn,
        table_name="journal_entries",
        pk_column="entry_id",
        row_id="shared-id-99",
        row=row,
    )

    assert anchor.parent_canonical_table is None, (
        "tl-1 belongs to grow_journal; a chatgpt row pointing at that string is a "
        "collision, not a parent"
    )
    assert anchor.source_record_id == "tl-1"


def test_a_same_table_fanout_child_is_still_detected(conn):
    """A declared ``fan_out`` mints children into whatever table it names.

    The ``canonical_field_map`` docstring's own GitHub example fans commits into
    ``journal_entries`` — the same table the base row lands in. Excluding the
    row's own table from the probe made that shape exempt by construction.
    """
    conn.execute(
        "INSERT INTO journal_entries VALUES (?,?,?,?,?)",
        ("tl-1:commit-abc", "commit message", None, "grow_journal", "tl-1"),
    )
    conn.commit()
    row = _row(conn, "journal_entries", "entry_id", "tl-1:commit-abc")

    anchor = _resolve_lineage(
        conn,
        table_name="journal_entries",
        pk_column="entry_id",
        row_id="tl-1:commit-abc",
        row=row,
    )

    assert anchor.parent_canonical_table == "journal_entries"
    assert anchor.parent_canonical_id == PARENT
    assert anchor.source_record_id == "tl-1:commit-abc"


def test_a_genuine_external_source_key_falls_through(conn):
    """The real github shape: 121 rows on the live node look like this.

    ``entry_id = github:{repo}:{sha}`` with
    ``source_record_id = push:{repo}:{sha}:{sha}`` — a real external key that
    resolves to no canonical row. The rule keys on "resolves to another canonical
    row", not on "differs from my own id", so these must fall through.
    """
    conn.execute(
        "INSERT INTO journal_entries VALUES (?,?,?,?,?)",
        ("github:acme/app:abc123", "commit", None, "github_activity",
         "push:acme/app:abc123:abc123"),
    )
    conn.commit()
    row = _row(conn, "journal_entries", "entry_id", "github:acme/app:abc123")

    anchor = _resolve_lineage(
        conn,
        table_name="journal_entries",
        pk_column="entry_id",
        row_id="github:acme/app:abc123",
        row=row,
    )

    assert anchor.parent_canonical_table is None
    assert anchor.source_record_id == "push:acme/app:abc123:abc123"


def test_the_retained_parent_is_reported_not_hidden(conn):
    """An under-delete that says nothing reads as a complete one."""
    result = delete_database_rows(
        conn, table_name="location_events", row_ids=[CHILD], scope="full_lineage"
    )

    assert result.parents_retained == [
        {"table": "journal_entries", "id": PARENT,
         "child_table": "location_events", "child_id": CHILD}
    ]
    payload = result.to_payload()
    assert payload["parents_retained"] == result.parents_retained
    assert any(a.action == "parent_retained" for a in result.table_actions)


def test_row_only_does_not_pay_for_the_parent_probe(conn, monkeypatch):
    """The probe only affects the upstream anchor, so row_only must skip it."""
    import topos.data_explorer_row_delete as dr

    calls = []
    real = dr._parent_canonical_row
    monkeypatch.setattr(
        dr, "_parent_canonical_row", lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    )

    delete_database_rows(
        conn, table_name="location_events", row_ids=[CHILD], scope="row_only"
    )
    assert calls == []

    delete_database_rows(
        conn, table_name="journal_entries", row_ids=[PARENT], scope="with_upstream"
    )
    assert calls, "the probe must still run where the upstream anchor is used"


def test_every_canonical_table_has_an_id_column_or_is_deliberately_absent():
    """The probe used to borrow a map covering 10 of the 14 canonical tables.

    A parent living in one of the missing four was never detected, so the
    destructive upstream delete survived for it. Anything absent from
    CANONICAL_ROW_ID_COLUMN must be absent because it has no single-column
    identity — which this pins as a deliberate list, not an oversight.
    """
    from topos.data_explorer_tables import (
        CANONICAL_ROW_ID_COLUMN,
        CANONICAL_SCHEMA_TABLES,
    )

    # No single-column row identity: composite PKs and one table with no PK.
    KNOWN_ABSENT = {"contact_identifiers", "ai_chat_participants"}

    missing = sorted(CANONICAL_SCHEMA_TABLES - set(CANONICAL_ROW_ID_COLUMN) - KNOWN_ABSENT)
    assert missing == [], f"canonical tables with no declared row id: {missing}"
    assert set(CANONICAL_ROW_ID_COLUMN) <= CANONICAL_SCHEMA_TABLES


# ------------------------------------- F: what "everything derived" reaches


def _seed_derived(conn):
    """A record plus one row in each derived table that was previously missed."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS topic_cluster_members (
            member_id TEXT PRIMARY KEY, cluster_id TEXT, record_id TEXT,
            source_id TEXT, record_type TEXT, text_preview TEXT);
        CREATE TABLE IF NOT EXISTS triage_verdicts (
            verdict_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT, verdict TEXT);
        CREATE TABLE IF NOT EXISTS cluster_candidates (
            candidate_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT);
        CREATE TABLE IF NOT EXISTS entity_review (
            review_id TEXT PRIMARY KEY, candidate_entity_id TEXT, record_id TEXT, source_id TEXT);
        INSERT INTO topic_cluster_members VALUES ('tcm-1','c1','tl-1','grow_journal','journal','x');
        INSERT INTO triage_verdicts        VALUES ('tv-1','tl-1','grow_journal','keep');
        INSERT INTO cluster_candidates     VALUES ('cc-1','tl-1','grow_journal');
        INSERT INTO entity_review          VALUES ('er-1','ent-ada','tl-1','grow_journal');
        """
    )
    conn.commit()


DERIVED_TABLES_UNDER_TEST = [
    ("timeline", "record_id"),
    ("entity_mentions", "record_id"),
    ("topic_cluster_members", "record_id"),
    ("triage_verdicts", "record_id"),
    ("cluster_candidates", "record_id"),
    ("entity_review", "record_id"),
]


@pytest.mark.parametrize("table,column", DERIVED_TABLES_UNDER_TEST)
def test_with_downstream_reaches_every_declared_derived_table(conn, table, column):
    """"Delete everything derived from this" must actually reach it.

    Membership of ``_ENRICHMENT_SIGNAL_TABLES`` decides two things at once, which
    is why these omissions were costly: the downstream sweep only visits tables
    that pass ``_is_enrichment_or_signal_table``, and ``_is_upstream_table``
    returns False for them. So a derived table missing from the declaration is
    not merely unswept by ``with_downstream`` — it is MISCLASSIFIED as upstream
    and swept by ``with_upstream`` instead, giving the owner the opposite of what
    each scope promises.

    The heuristic cannot separate them on its own: ``_is_upstream_table`` reads
    ``record_id + source_id`` as an upstream signature and every derived table
    carries both. Measured on the owner's node, the tables added here hold 38,700
    rows that this scope did not reach.
    """
    _seed_derived(conn)
    assert _count(conn, table, f"{column}=?", (PARENT,)) >= 1, "fixture must seed it"

    delete_database_rows(
        conn, table_name="journal_entries", row_ids=[PARENT], scope="with_downstream"
    )

    assert _count(conn, table, f"{column}=?", (PARENT,)) == 0, (
        f"{table} holds rows derived from the deleted record and was not swept"
    )


def test_row_only_still_touches_nothing_derived(conn):
    """The scopes must stay distinct — this is not "sweep more, always"."""
    _seed_derived(conn)

    delete_database_rows(
        conn, table_name="journal_entries", row_ids=[PARENT], scope="row_only"
    )

    for table, column in DERIVED_TABLES_UNDER_TEST:
        assert _count(conn, table, f"{column}=?", (PARENT,)) >= 1, (
            f"row_only must not reach {table}"
        )


def test_a_flat_source_table_is_never_swept_as_derived(conn):
    """The exclusions carry as much risk as the inclusions.

    ``grow_journal_sessions`` and the browser tables are the landing shape of the
    SOURCE — the owner's ingested data, upstream of canonical. They carry
    ``record_id`` and ``source_id`` and no ``raw_`` prefix, so a heuristic that
    swept on those columns alone would delete them as though they were derived.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS grow_journal_sessions (
            record_id TEXT PRIMARY KEY, goal TEXT, source_id TEXT);
        INSERT INTO grow_journal_sessions VALUES ('tl-1','Ship','grow_journal');
        """
    )
    conn.commit()

    delete_database_rows(
        conn, table_name="journal_entries", row_ids=[PARENT], scope="with_downstream"
    )

    assert _count(conn, "grow_journal_sessions", "record_id=?", (PARENT,)) == 1, (
        "a flat source table is the owner's ingested data, not a restatement of it"
    )


def test_the_declaration_and_the_upstream_heuristic_cannot_disagree(conn):
    """Structural: nothing may be both derived and upstream."""
    from topos.data_explorer_row_delete import (
        _ENRICHMENT_SIGNAL_TABLES,
        _is_enrichment_or_signal_table,
        _is_upstream_table,
    )

    both = [
        t
        for t in _ENRICHMENT_SIGNAL_TABLES
        if _is_enrichment_or_signal_table(t)
        and _is_upstream_table(t, {"record_id", "source_id"})
    ]
    assert both == [], f"classified as derived AND upstream: {both}"
