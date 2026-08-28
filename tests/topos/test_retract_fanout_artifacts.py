"""The fan-out retraction is a per-site migration, not a schema one.

Three populations exist on an affected node and each needs a different recovery
rule, which is why this is a script with a plan/apply split rather than a
migration:

  a. **fabricated goals** — 154 rows over 63 records on the live node (77 real
     plus 77 untyped twins from the double write), 76 distinct texts, which minted
     37 ``goal`` entities carrying 54 edges. Those entities are exempt from orphan
     pruning by an explicit ``goal_%`` keep rule, so nothing else removes them.
  b. **the retired GitHub per-commit fan-out** — the code path went on
     2026-08-14, the data did not: 121 ``journal_entries`` rows feeding 485 stale
     facts and 121 embeddings that duplicate an ``activity_events`` embedding.
  c. **one unrecoverable orphan** — a timeline row with no child and no parent.

What the tests are for. A retraction is the one operation whose bugs are
unrecoverable, so the properties that matter are not "did it delete" but:

  * dry run writes NOTHING;
  * it removes only its own population, never the owner's real records;
  * it is idempotent, so a re-run after a partial failure is safe;
  * a black-holed entity outranks the retraction;
  * the embedding lane goes through the vector index, so ANN and FTS stay in sync.
"""

from __future__ import annotations

import sqlite3

import pytest

from scripts.retract_fanout_artifacts import (
    apply_fabricated_goals,
    plan_fabricated_goals,
    plan_retired_github_fanout,
    plan_unlinkable_orphans,
    main,
)


@pytest.fixture()
def db(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    path = tmp_path / "retract.db"
    conn = sqlite3.connect(str(path))
    apply_all_migrations(conn)

    # --- (a) a fan-out child and the fabricated goal it produced
    conn.execute(
        "INSERT INTO location_events (event_id, place_name, source_id, source_record_id)"
        " VALUES (?,?,?,?)",
        ("tl-1-loc", "Northgate- The Foundry", "grow_journal", "tl-1"),
    )
    conn.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json)"
        " VALUES (?,?,?,?,?)",
        ("g-fab", "tl-1-loc", "grow_journal", "Watch Northgate- The Foundry", "{}"),
    )
    conn.execute(  # the untyped twin the double write produced
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json)"
        " VALUES (?,?,?,?,?)",
        ("g-fab-twin", "tl-1-loc", "grow_journal", "", "{}"),
    )
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,0,0)",
        ("goal_fab", "goal", "Watch Northgate- The Foundry", "watch brooklyn the convent"),
    )
    # --- the owner's REAL goal, from their own writing
    conn.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json)"
        " VALUES (?,?,?,?,?)",
        ("g-real", "tl-1", "grow_journal", "Ship the eval set", "{}"),
    )
    conn.execute(
        "INSERT INTO journal_entries (entry_id, content, place_name, source_id, source_record_id)"
        " VALUES (?,?,?,?,?)",
        ("tl-1", "Deep work on the eval set.", "Northgate- The Foundry", "grow_journal", "tl-1"),
    )

    # --- (b) the retired github fan-out, plus its retained activity sibling
    conn.execute(
        "INSERT INTO journal_entries (entry_id, content, source_id, source_record_id)"
        " VALUES (?,?,?,?)",
        ("github:acme/app:abc", "commit message", "github_activity", "push:acme/app:abc:abc"),
    )
    conn.execute(
        "INSERT INTO signal_facts (fact_id, dimension, source_id, record_id, payload_json)"
        " VALUES (?,?,?,?,?)",
        ("f-gh", "relationships", "github_activity", "github:acme/app:abc", "{}"),
    )
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, text_preview,"
        " search_text) VALUES (?,?,?,?,?)",
        ("emb-gh", "github:acme/app:abc", "github_activity", "commit message", "commit message"),
    )
    conn.execute(
        "INSERT INTO timeline (event_at, record_id, source_id, canonical_table)"
        " VALUES (?,?,?,?)",
        ("2026-08-01", "github:acme/app:abc", "github_activity", "journal_entries"),
    )

    # --- (c) the unlinkable orphan
    conn.execute(
        "INSERT INTO timeline (event_at, record_id, source_id, canonical_table)"
        " VALUES (?,?,?,?)",
        ("2026-07-01", "tl-job-time-log-1-loc", "time_log", "journal_entries"),
    )
    conn.commit()
    yield path, conn
    conn.close()


def _count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


# --------------------------------------------------------------------- planning


def test_the_plan_finds_each_population(db):
    _path, conn = db

    goals = plan_fabricated_goals(conn)
    github = plan_retired_github_fanout(conn)
    orphans = plan_unlinkable_orphans(conn)

    assert set(goals["user_goals"]) == {"g-fab", "g-fab-twin"}
    assert goals["goal_entities"] == ["goal_fab"]
    assert github["journal_entries"] == ["github:acme/app:abc"]
    assert github["derived"]["signal_facts"] == 1
    assert orphans["timeline_rows"] == ["tl-job-time-log-1-loc"]


def test_the_plan_never_names_the_owners_own_rows(db):
    """The retraction's blast radius is the whole risk."""
    _path, conn = db

    goals = plan_fabricated_goals(conn)
    github = plan_retired_github_fanout(conn)

    assert "g-real" not in goals["user_goals"]
    assert "tl-1" not in github["journal_entries"]


# ------------------------------------------------------------------- dry run


def test_dry_run_writes_nothing(db):
    path, conn = db
    before = (
        _count(conn, "SELECT COUNT(*) FROM user_goals"),
        _count(conn, "SELECT COUNT(*) FROM journal_entries"),
        _count(conn, "SELECT COUNT(*) FROM timeline"),
        _count(conn, "SELECT COUNT(*) FROM entities"),
    )

    assert main(["--database", str(path)]) == 0

    verify = sqlite3.connect(str(path))
    try:
        after = (
            _count(verify, "SELECT COUNT(*) FROM user_goals"),
            _count(verify, "SELECT COUNT(*) FROM journal_entries"),
            _count(verify, "SELECT COUNT(*) FROM timeline"),
            _count(verify, "SELECT COUNT(*) FROM entities"),
        )
    finally:
        verify.close()
    assert after == before


def test_a_missing_database_is_an_error_not_a_default(db):
    """There is deliberately no fallback to ~/.topos/database.db."""
    assert main(["--database", "/nonexistent/nope.db"]) == 2


# --------------------------------------------------------------------- apply


def test_apply_removes_the_populations_and_nothing_else(db):
    path, conn = db
    conn.close()

    assert main(["--database", str(path), "--apply"]) == 0

    verify = sqlite3.connect(str(path))
    try:
        assert _count(verify, "SELECT COUNT(*) FROM user_goals WHERE record_id LIKE '%-loc'") == 0
        assert _count(verify, "SELECT COUNT(*) FROM entities WHERE entity_id='goal_fab'") == 0
        assert (
            _count(verify, "SELECT COUNT(*) FROM journal_entries WHERE source_id='github_activity'")
            == 0
        )
        assert (
            _count(verify, "SELECT COUNT(*) FROM timeline WHERE record_id='tl-job-time-log-1-loc'")
            == 0
        )
        # the owner's own rows survive
        assert _count(verify, "SELECT COUNT(*) FROM user_goals WHERE goal_id='g-real'") == 1
        assert _count(verify, "SELECT COUNT(*) FROM journal_entries WHERE entry_id='tl-1'") == 1
    finally:
        verify.close()


def test_apply_is_idempotent(db):
    path, conn = db
    conn.close()

    assert main(["--database", str(path), "--apply"]) == 0
    assert main(["--database", str(path), "--apply"]) == 0

    verify = sqlite3.connect(str(path))
    try:
        assert _count(verify, "SELECT COUNT(*) FROM user_goals WHERE goal_id='g-real'") == 1
    finally:
        verify.close()


def test_one_population_can_be_retracted_alone(db):
    path, conn = db
    conn.close()

    assert main(["--database", str(path), "--population", "orphans", "--apply"]) == 0

    verify = sqlite3.connect(str(path))
    try:
        assert (
            _count(verify, "SELECT COUNT(*) FROM timeline WHERE record_id='tl-job-time-log-1-loc'")
            == 0
        )
        # goals untouched, because they were not selected
        assert _count(verify, "SELECT COUNT(*) FROM user_goals WHERE goal_id='g-fab'") == 1
    finally:
        verify.close()


# ------------------------------------------------------- protection outranks it


def test_a_black_holed_goal_entity_is_kept(db):
    """Protection outranks retraction, even for a fabricated vertex.

    A black hole says "this must not be reachable", not "this must be gone", and
    the owner may have protected the name precisely because it is an address.
    Removing the entity would empty the guard's exact filter — the failure this
    whole workstream started from.
    """
    _path, conn = db
    from topos.features.lifecycle.blackhole import BlackholeStore

    BlackholeStore(conn).blackhole_entity(entity_ref="goal_fab")
    conn.commit()

    plan = plan_fabricated_goals(conn)
    assert plan["goal_entities"] == []
    assert plan["protected_kept"] == ["goal_fab"]

    apply_fabricated_goals(conn, plan)
    conn.commit()

    assert _count(conn, "SELECT COUNT(*) FROM entities WHERE entity_id='goal_fab'") == 1
    # ...while the fabricated goal ROWS still go: they are not the protected thing.
    assert _count(conn, "SELECT COUNT(*) FROM user_goals WHERE record_id LIKE '%-loc'") == 0


# ------------------------------------------------------------- index coherence


def test_the_embedding_lane_stays_in_sync(db):
    """FTS is external-content with a delete trigger; ANN is a separate table.

    A raw DELETE on signal_embeddings would leave the ANN companion behind. This
    asserts the counts agree afterwards, which is the observable form of that.
    """
    path, conn = db
    conn.close()

    assert main(["--database", str(path), "--population", "github", "--apply"]) == 0

    verify = sqlite3.connect(str(path))
    try:
        base = _count(verify, "SELECT COUNT(*) FROM signal_embeddings")
        try:
            fts = _count(verify, "SELECT COUNT(*) FROM signal_embeddings_fts")
        except sqlite3.OperationalError:
            pytest.skip("no FTS table in this build")
        assert fts == base, f"FTS index drifted from the base table: {fts} vs {base}"
        assert (
            _count(
                verify,
                "SELECT COUNT(*) FROM signal_embeddings WHERE record_id='github:acme/app:abc'",
            )
            == 0
        )
    finally:
        verify.close()


def test_no_ann_companion_is_left_without_its_embedding(db):
    """An orphaned vector is still a nearest neighbour.

    Caught by running this script against a real 513MB snapshot, not by reading
    the code: the base table went 9,583 -> 9,462 while
    ``signal_embeddings_vec_rowids`` stayed at 9,583 — 121 orphans against a
    baseline of zero. ``signal_embeddings_vec`` is a vec0 VIRTUAL table, so
    without the sqlite-vec extension loaded every statement against it raises;
    ``delete_vec_rows`` swallows that, and ``_sqlite_vec_ready`` checks only that
    the table exists in ``sqlite_master``, not that the module is loaded. A plain
    ``sqlite3.connect`` therefore deleted the documents and left the vectors
    searchable.

    The fixture writes a REAL vec0 row rather than a bare shadow row: the shadow
    tables are maintained by the virtual table, so a hand-inserted
    ``_vec_rowids`` entry has nothing behind it and no correct delete would ever
    clean it up — the test would fail for a reason that is not the bug.

    The count assertion is the point. The earlier version of this file checked
    FTS/base agreement only, which stayed green through the whole failure.
    """
    from topos.storage.db.connection_tuning import load_sqlite_vec

    path, conn = db
    if not load_sqlite_vec(conn):
        pytest.skip("sqlite-vec unavailable in this environment")
    dims = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='signal_embeddings_vec'"
    ).fetchone()
    if not dims:
        pytest.skip("no vec0 table in this build")
    import json as _json

    n = int(str(dims[0]).split("float[")[1].split("]")[0])
    conn.execute(
        "INSERT INTO signal_embeddings_vec (embedding_id, embedding) VALUES (?, ?)",
        ("emb-gh", _json.dumps([0.0] * n)),
    )
    conn.commit()
    assert (
        _count(conn, "SELECT COUNT(*) FROM signal_embeddings_vec_rowids WHERE id='emb-gh'") == 1
    ), "fixture must put a real vector in the index"
    conn.close()

    assert main(["--database", str(path), "--population", "github", "--apply"]) == 0

    verify = sqlite3.connect(str(path))
    try:
        orphans = _count(
            verify,
            "SELECT COUNT(*) FROM signal_embeddings_vec_rowids r WHERE NOT EXISTS ("
            " SELECT 1 FROM signal_embeddings e WHERE e.embedding_id = r.id)",
        )
    finally:
        verify.close()
    assert orphans == 0, (
        f"{orphans} ANN companion rows survive their embedding — the retracted "
        "vectors are still reachable by similarity search"
    )


def test_it_exits_nonzero_if_it_leaves_ann_orphans(db, monkeypatch):
    """A retraction that cannot finish must not report success.

    If the extension is unavailable the vectors cannot be removed, and silently
    reporting a clean run is how the incomplete state ships.
    """
    path, conn = db
    conn.execute(
        "INSERT INTO signal_embeddings_vec_rowids (id, chunk_id, chunk_offset)"
        " VALUES (?,?,?)",
        ("emb-gh", 1, 0),
    )
    conn.commit()
    conn.close()

    import scripts.retract_fanout_artifacts as mod
    from topos.storage.adapters.sqlite import stores as store_mod

    class _Inert:
        def __init__(self, _conn):
            pass

        def delete_embeddings(self, _ids):
            return 0  # the silent no-op the missing extension produces

    monkeypatch.setattr(store_mod, "SQLiteVectorIndex", _Inert)

    assert mod.main(["--database", str(path), "--population", "github", "--apply"]) == 3
