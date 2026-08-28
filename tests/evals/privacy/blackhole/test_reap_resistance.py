"""M1/M4: protection must survive its own housekeeping.

The black hole is keyed on an entity id, and nothing in ``derived_scrub`` used to
know the black-hole tables existed. That made the maintenance path a silent way
to switch protection off:

    mentions removed -> ``mention_count`` hits 0 -> the next
    ``rebuild_evidence_edges`` drops the entity's co-occurrence edges -> the next
    orphan sweep DELETES the entity -> ``blocked_record_ids()`` and
    ``sql_exclusion()`` both return empty -> a hard protection has degraded to a
    read-time substring name scan.

Measured on the live node 2026-08-27, one of three real black holes had already
gone through it: ``Old Harbor- Rey's Place`` carried
``rebuild_state='complete'`` while its ``entities`` row was gone, and the
protected name still sat in five columns plus the FTS index.

The first test here is the one that matters. It does not enumerate reap paths —
it asserts a PROPERTY over every scrub entry point: the protected set never
shrinks. That is what catches the next path nobody thought of, which is exactly
how this one got in.
"""

from __future__ import annotations

import sqlite3
from typing import Set, Tuple

import pytest

from topos.features.entities.maintenance import rebuild_evidence_edges
from topos.features.lifecycle.blackhole import (
    BlackholeStore,
    blackholed_entity_ids,
    blackholed_name_terms,
    normalize_entity_name,
)
from topos.features.lifecycle.blackhole_guard import guard_for
from topos.features.lifecycle.derived_scrub import (
    _delete_entity_cascade,
    _delete_orphan_entities,
    _protected_entity_keys,
    purge_derived_for_records,
    purge_derived_for_source,
    purge_junk_minted_entities,
    sweep_orphans,
)
from tests.evals.privacy.blackhole.corpus import (
    BH_CANONICAL,
    BH_ID,
    OK_ID,
    BH_THREAD_RECORD_ID,
    SOURCE_ID,
    build_blackhole_corpus,
)

pytestmark = [pytest.mark.bhlr, pytest.mark.private]


# --------------------------------------------------------------- helpers


def _protection_snapshot(conn: sqlite3.Connection) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    """Everything the guard can protect WITH, as four sets.

    An adversarial review found the first version of this was two-thirds
    decorative: ``blackholed_entity_ids`` and ``blackholed_name_terms`` read
    ``entity_blackholes`` DIRECTLY, and no scrub touches that table — so those two
    components can never narrow and asserted nothing. The live failure was
    precisely the case they miss: the flag row survives intact while the
    ``entities`` row it names is deleted, which is what empties the filter.

    The fourth set is the load-bearing one. It resolves the flag THROUGH
    ``entities``, so it shrinks exactly when a protected entity is reaped —
    whichever writer did it.
    """
    grantee = guard_for(conn, mcp_source="claude_desktop")
    flagged_ids = set(blackholed_entity_ids(conn))
    terms = set(blackholed_name_terms(conn))
    resolvable = set()
    if flagged_ids or terms:
        for entity_id, normalized in conn.execute(
            "SELECT entity_id, normalized_name FROM entities"
        ):
            if str(entity_id) in flagged_ids or str(normalized or "") in terms:
                resolvable.add(str(entity_id))
    return (set(grantee.blocked_record_ids()), flagged_ids, terms, resolvable)


def _assert_protection_did_not_narrow(
    before, after, *, what: str, deleted_records: Set[str] = frozenset()
) -> None:
    """The flag and its filters may never narrow; record blocking may, narrowly.

    A record that was DELETED legitimately leaves ``blocked_record_ids()`` — the
    thing being blocked no longer exists, and holding its id would be the bug.
    So the honest property is a subset relation, not equality: every record that
    dropped out must be one the operation was asked to remove. The entity-id and
    name-term filters have no such exemption; nothing a scrub does may shrink
    them, which is the half that failed on the live node.
    """
    b_records, b_ids, b_terms, b_live = before
    a_records, a_ids, a_terms, a_live = after
    lost_live = b_live - a_live
    assert lost_live == set(), (
        f"{what} deleted a black-holed entity — its flag row survives but the "
        f"guard can no longer resolve a filter from it: {sorted(lost_live)}"
    )
    lost_records = b_records - a_records - set(deleted_records)
    assert lost_records == set(), (
        f"{what} narrowed record blocking for records it did not delete: "
        f"{sorted(lost_records)}"
    )
    assert b_ids - a_ids == set(), (
        f"{what} narrowed the exact entity-id filter: lost {sorted(b_ids - a_ids)}"
    )
    assert b_terms - a_terms == set(), (
        f"{what} narrowed name-term protection: lost {sorted(b_terms - a_terms)}"
    )


def _strip_to_reapable(conn: sqlite3.Connection, entity_id: str) -> None:
    """Put an entity in the exact state that killed Old Harbor.

    Zero mentions, zero edges, no contact anchor, not self — every condition
    ``_delete_orphan_entities`` reaps on.
    """
    conn.execute("DELETE FROM entity_mentions WHERE entity_id=?", (entity_id,))
    conn.execute(
        "DELETE FROM entity_edges WHERE src_entity_id=? OR dst_entity_id=?",
        (entity_id, entity_id),
    )
    conn.execute(
        "UPDATE entities SET mention_count=0, contact_id=NULL, is_self=0 WHERE entity_id=?",
        (entity_id,),
    )
    conn.commit()


def _entity_exists(conn: sqlite3.Connection, entity_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()
        is not None
    )


@pytest.fixture()
def corpus(tmp_path):
    c = build_blackhole_corpus(str(tmp_path / "reap.db"))
    yield c
    c.conn.close()


# ------------------------------------------------- 1. the invariant (the point)


PURGED_RECORDS = frozenset({"rec-1", "rec-2", BH_THREAD_RECORD_ID})

#: ``name -> (operation, records it is ASKED to delete)``. Everything with an
#: empty second element is a maintenance pass that names no record, so nothing
#: at all may leave the blocked set.
SCRUB_ENTRYPOINTS = {
    "_delete_orphan_entities": (lambda conn: _delete_orphan_entities(conn), frozenset()),
    "sweep_orphans": (lambda conn: sweep_orphans(conn), frozenset()),
    "purge_junk_minted_entities": (
        lambda conn: purge_junk_minted_entities(conn),
        frozenset(),
    ),
    "rebuild_evidence_edges": (lambda conn: rebuild_evidence_edges(conn), frozenset()),
    "purge_derived_for_source": (
        lambda conn: purge_derived_for_source(conn, SOURCE_ID),
        # A whole-source scrub removes that source's records, so any of them may
        # drop out. The entity-id filter still may not.
        PURGED_RECORDS,
    ),
    "purge_derived_for_records": (
        lambda conn: purge_derived_for_records(conn, sorted(PURGED_RECORDS)),
        PURGED_RECORDS,
    ),
    # Found by adversarial review: each of these deletes entities WITHOUT going
    # through _delete_entity_cascade, so "the one door" was never one door.
    # rebuild_entity_graph is the damning one — it runs the guarded orphan sweep
    # and then the materializer's own purge two steps later, in the same pass.
    "materialize_signal_objects_to_graph": (
        lambda conn: _materialize(conn),
        frozenset(),
    ),
    "rebuild_entity_graph": (lambda conn: _rebuild_graph(conn), frozenset()),
    "exclude_entity_unprotected": (
        lambda conn: _exclude_unprotected(conn),
        frozenset(),
    ),
}


def _materialize(conn):
    from topos.features.entities.fact_materializer import materialize_signal_objects_to_graph

    return materialize_signal_objects_to_graph(conn)


def _rebuild_graph(conn):
    from topos.features.entities.maintenance import rebuild_entity_graph

    return rebuild_entity_graph(conn)


def _exclude_unprotected(conn):
    """Exclusion of an UNPROTECTED entity must still work and must not narrow."""
    from topos.features.lifecycle.exclusions import ExclusionStore

    conn.execute(
        "INSERT OR IGNORE INTO entities (entity_id, entity_type, canonical_name,"
        " normalized_name, mention_count, is_self) VALUES (?,?,?,?,0,0)",
        ("ent-excl-target", "person", "Excludable Person", "excludable person"),
    )
    conn.commit()
    return ExclusionStore(conn).exclude_entity(entity_ref="ent-excl-target")


@pytest.mark.parametrize("name", sorted(SCRUB_ENTRYPOINTS))
def test_protection_never_narrows(corpus, name):
    """THE invariant: no maintenance operation may shrink what the guard blocks.

    Parametrized over every scrub entry point rather than asserting against one
    known-bad path, because the defect was never that one function was wrong —
    it was that no function knew protection existed. A new reap path added
    tomorrow lands in this test the moment it is listed here, and a new path that
    is NOT listed is the gap this docstring exists to name.
    """
    operation, deleted = SCRUB_ENTRYPOINTS[name]
    before = _protection_snapshot(corpus.conn)
    assert before[3], "fixture must start with at least one RESOLVABLE black hole"

    operation(corpus.conn)

    after = _protection_snapshot(corpus.conn)
    _assert_protection_did_not_narrow(
        before, after, what=name, deleted_records=deleted
    )


@pytest.mark.parametrize("name", sorted(SCRUB_ENTRYPOINTS))
def test_protection_never_narrows_even_when_the_entity_is_reapable(corpus, name):
    """The same invariant from the state that actually produced the live failure.

    Stripping mentions and edges first is what makes the entity a reap
    candidate; without it most of these entry points never look at it and the
    test above passes for the wrong reason.
    """
    operation, deleted = SCRUB_ENTRYPOINTS[name]
    _strip_to_reapable(corpus.conn, BH_ID)
    before = _protection_snapshot(corpus.conn)

    operation(corpus.conn)

    after = _protection_snapshot(corpus.conn)
    _assert_protection_did_not_narrow(
        before, after, what=f"{name} (reapable state)", deleted_records=deleted
    )
    assert _entity_exists(corpus.conn, BH_ID), f"{name} deleted a black-holed entity"


# --------------------------------------------------- 2. reap resistance, direct


def test_orphan_sweep_keeps_a_black_holed_entity_with_no_mentions_and_no_edges(corpus):
    """The Old Harbor shape, as a named regression."""
    _strip_to_reapable(corpus.conn, BH_ID)

    removed = _delete_orphan_entities(corpus.conn)

    assert BH_ID not in removed
    assert _entity_exists(corpus.conn, BH_ID)
    assert BH_ID in blackholed_entity_ids(corpus.conn)


def test_orphan_sweep_still_reaps_an_unprotected_orphan(corpus):
    """Control: the guard must not have simply switched the sweep off.

    Without this, every assertion above is satisfied by a sweep that does
    nothing at all.
    """
    corpus.conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,0,0)",
        ("ent-junk-orphan", "person", "Zzz Unreferenced", "zzz unreferenced"),
    )
    corpus.conn.commit()

    removed = _delete_orphan_entities(corpus.conn)

    assert "ent-junk-orphan" in removed
    assert not _entity_exists(corpus.conn, "ent-junk-orphan")


def _footprint(conn, entity_id):
    def n(sql, *p):
        try:
            return conn.execute(sql, p).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    return (
        n("SELECT COUNT(*) FROM entity_mentions WHERE entity_id=?", entity_id),
        n(
            "SELECT COUNT(*) FROM entity_edges WHERE src_entity_id=? OR dst_entity_id=?",
            entity_id, entity_id,
        ),
        n("SELECT COUNT(*) FROM entity_context_vectors WHERE entity_id=?", entity_id),
    )


def test_cascade_refuses_a_protected_entity_at_the_door(corpus):
    """The chokepoint guarantee, asserted against the DATABASE.

    Reviewing this file found the first version trusted the returned dict, which
    a refusal that had already stripped the mentions would satisfy just as well.
    The footprint and the resolved filter are what actually matter.
    """
    before = _footprint(corpus.conn, BH_ID)
    blocked_before = _protection_snapshot(corpus.conn)

    counts = _delete_entity_cascade(corpus.conn, BH_ID)

    assert counts.get("skipped_protected") == 1
    assert _entity_exists(corpus.conn, BH_ID)
    assert _footprint(corpus.conn, BH_ID) == before, (
        "the cascade refused but still stripped part of the entity's footprint"
    )
    _assert_protection_did_not_narrow(
        blocked_before, _protection_snapshot(corpus.conn), what="_delete_entity_cascade"
    )


def test_cascade_deletes_an_unprotected_entity(corpus):
    """Control for the guard above."""
    corpus.conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,0,0)",
        ("ent-plain", "person", "Plain Person", "plain person"),
    )
    corpus.conn.commit()

    _delete_entity_cascade(corpus.conn, "ent-plain")

    assert not _entity_exists(corpus.conn, "ent-plain")


# ------------------------------------------------- 3. the name key, not just id


def test_a_remint_under_a_fresh_id_is_still_protected(corpus):
    """The half of the hazard that survives stopping the reap.

    Once an entity has been deleted, the next extraction pass mints the same name
    again under a NEW id. The stored flag's ``entity_id`` no longer matches, so id
    protection alone would let the re-mint be reaped in turn — and, worse, be
    served. Protection is therefore keyed on the normalized name as well.
    """
    reminted = "ent-bh-dana-REMINTED"
    corpus.conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,0,0)",
        (reminted, "person", BH_CANONICAL, normalize_entity_name(BH_CANONICAL)),
    )
    corpus.conn.commit()

    assert _delete_entity_cascade(corpus.conn, reminted).get("skipped_protected") == 1
    assert reminted not in _delete_orphan_entities(corpus.conn)
    assert _entity_exists(corpus.conn, reminted)


def test_protection_covers_a_name_with_no_entity_yet(tmp_path):
    """Pre-emptive protection: a name flagged before anything minted it.

    ``BlackholeStore`` supports flagging an unminted name (``bind_entity_id``
    binds it later). ``_protected_entity_keys`` must return that name so the
    entity is protected the moment it appears, not from the next flag write.
    """
    c = build_blackhole_corpus(str(tmp_path / "preempt.db"))
    try:
        c.conn.execute(
            "INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name,"
            " canonical_name) VALUES (?,?,?,?)",
            ("bh-unminted", "", normalize_entity_name("Nobody Yet"), "Nobody Yet"),
        )
        c.conn.commit()

        _ids, names = _protected_entity_keys(c.conn)
        assert normalize_entity_name("Nobody Yet") in names

        c.conn.execute(
            "INSERT INTO entities (entity_id, entity_type, canonical_name,"
            " normalized_name, mention_count, is_self) VALUES (?,?,?,?,0,0)",
            ("ent-appeared", "person", "Nobody Yet", normalize_entity_name("Nobody Yet")),
        )
        c.conn.commit()

        assert "ent-appeared" not in _delete_orphan_entities(c.conn)
        assert _entity_exists(c.conn, "ent-appeared")
    finally:
        c.conn.close()


# ------------------------------------------- 4. the C4 junk scrub, specifically


def test_junk_scrub_keeps_a_protected_entity_the_predicate_rejects(corpus):
    """``purge_junk_minted_entities`` is a second, independent route to the reap.

    The premise assertion is not ceremony. The first version of this test used
    ``"Old Harbor- Rey's Place"`` — the live compound place name — on the
    assumption that a trailing hyphen and an apostrophe would fail the C4
    predicate. ``is_valid_entity_surface`` in fact ACCEPTS it, so the entity was
    never a junk candidate and the test proved nothing while passing.

    ``"Ana"`` is genuinely rejected: the surface filter's short-name rule drops
    three-character names that are not on its allowlist, which is a real hazard
    for exactly the people an owner is most likely to protect.
    """
    from topos.features.entities.resolver import is_valid_entity_surface

    junky = "Ana"
    assert not is_valid_entity_surface(junky), (
        "premise: the C4 predicate must actually reject this surface, or the test "
        "passes for the wrong reason (it did — see the docstring)"
    )
    corpus.conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,0,0)",
        ("ent-bh-short", "person", junky, normalize_entity_name(junky)),
    )
    BlackholeStore(corpus.conn).blackhole_entity(entity_ref="ent-bh-short")
    corpus.conn.commit()

    report = purge_junk_minted_entities(corpus.conn)

    assert _entity_exists(corpus.conn, "ent-bh-short")
    assert "ent-bh-short" not in {x["entity_id"] for x in report["samples"]}


def test_junk_scrub_still_removes_an_unprotected_rejected_surface(corpus):
    """Control: the black-hole filter must not have disabled the C4 scrub."""
    from topos.features.entities.resolver import is_valid_entity_surface

    assert not is_valid_entity_surface("Zed")
    corpus.conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,0,0)",
        ("ent-junk-short", "person", "Zed", normalize_entity_name("Zed")),
    )
    corpus.conn.commit()

    purge_junk_minted_entities(corpus.conn)

    assert not _entity_exists(corpus.conn, "ent-junk-short")


# ------------------------------------------------------- 5. fail-closed reading


def test_an_unreadable_protection_list_stops_the_scrub(corpus):
    """Refuse to reap rather than assume nothing is protected.

    A missing table fails OPEN (a database with no black-hole schema has nothing
    to protect), but a table that exists and cannot be READ must not resolve to
    an empty protected set — that is indistinguishable from "reap everything".
    """
    class _BrokenBlackholeRead:
        """Delegates everything except a read of the protection list.

        ``sqlite3.Connection.execute`` is a read-only attribute, so this cannot
        be monkeypatched onto the real connection. Note ``_table_exists`` passes
        the table name as a BOUND PARAMETER, so its sqlite_master probe does not
        contain the string and still succeeds — the table looks present and then
        fails to read, which is the state under test.
        """

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **kw):
            if "entity_blackholes" in str(sql):
                raise sqlite3.DatabaseError("simulated corruption")
            return self._conn.execute(sql, *a, **kw)

        def __getattr__(self, item):
            return getattr(self._conn, item)

    with pytest.raises(sqlite3.Error):
        _protected_entity_keys(_BrokenBlackholeRead(corpus.conn))


def test_no_black_hole_schema_fails_open(tmp_path):
    """A database with no protection table has nothing to protect."""
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        conn.execute(
            "CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,"
            " canonical_name TEXT, normalized_name TEXT, mention_count INTEGER,"
            " is_self INTEGER, contact_id TEXT)"
        )
        conn.commit()

        ids, names = _protected_entity_keys(conn)

        assert ids == set() and names == set()
    finally:
        conn.close()


# --------------------------------- 6. the other doors the review found


def test_exclusion_refuses_a_black_holed_entity(corpus):
    """"Forget this" and "hide this" are different decisions.

    ``ExclusionStore.exclude_entity`` deletes the entity and every mention while
    DELIBERATELY leaving the canonical rows — "intelligence exclusion, not content
    deletion". On a protected entity that is the worst combination: the records
    that name it survive, the flag row survives, and ``blocked_record_ids()``
    collapses to empty because the join table it reads has been emptied. The
    substring name scan does not cover a record whose text never names the entity.
    """
    from topos.features.lifecycle.exclusions import ExclusionStore

    before = _protection_snapshot(corpus.conn)

    with pytest.raises(ValueError, match="black hole"):
        ExclusionStore(corpus.conn).exclude_entity(entity_ref=BH_ID)

    assert _entity_exists(corpus.conn, BH_ID)
    _assert_protection_did_not_narrow(
        before, _protection_snapshot(corpus.conn), what="exclude_entity"
    )


def test_merge_refuses_across_a_protection_boundary(corpus):
    """A merge does not move a black hole, it INVERTS it.

    ``_remap_derivation_corpus`` repoints ``entity_blackholes.entity_id`` at the
    surviving id, so absorbing a protected entity into an unprotected one hides
    the survivor's own records while leaving the protected name bound to an entity
    that was never protected — and ``bind_entity_id`` only rebinds rows with an
    empty id, so a later re-mint of the protected name can never re-attach.
    Reachable from the owner clicking "yes, same person" on a dedupe review.
    """
    from topos.features.entities.resolver import EntityResolver

    resolver = EntityResolver(corpus.conn)

    with pytest.raises(ValueError, match="black-holed"):
        resolver.merge_entities(OK_ID, BH_ID)
    with pytest.raises(ValueError, match="black-holed"):
        resolver.merge_entities(BH_ID, OK_ID)

    assert _entity_exists(corpus.conn, BH_ID)
    assert BH_ID in blackholed_entity_ids(corpus.conn)


def test_a_split_carries_protection_to_the_new_entity(corpus):
    """A split states something about identity, never about protection.

    Moving a surface off a protected entity used to move those records out of
    ``blocked_record_ids()``: the new entity is not flagged, nothing binds it, and
    the canonical rows survive untouched — so the records became servable.
    """
    from topos.features.entities.consolidation import split_surface

    # split_surface only mints a new entity when mentions carrying the surface
    # exist, so plant one rather than relying on the corpus's own surfaces.
    surface = "Dana Nickname Qx99"
    corpus.conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id,"
        " canonical_table, surface_text) VALUES (?,?,?,?,?,?)",
        ("m-split", BH_ID, "rec-split", "imessage", "conversation_messages", surface),
    )
    corpus.conn.commit()
    before = _protection_snapshot(corpus.conn)

    result = split_surface(corpus.conn, entity_id=BH_ID, surface=surface)
    corpus.conn.commit()

    new_id = result.get("new_entity_id")
    assert new_id, "premise: the split must actually mint a new entity"
    assert BlackholeStore(corpus.conn).is_blackholed(new_id), (
        "the entity a protected surface was split into must inherit the black hole"
    )
    _assert_protection_did_not_narrow(
        before, _protection_snapshot(corpus.conn), what="split_surface"
    )


# ------------------------------------- 7. no seventh door, silently


#: Every site allowed to delete from ``entities``. Each one either IS the guard
#: or calls ``is_entity_protected`` first. Adding a site here without a guard is
#: the regression this test exists to make loud.
_GUARDED_ENTITY_DELETE_SITES = {
    "topos/features/lifecycle/derived_scrub.py",   # _delete_entity_cascade — the guard
    "topos/features/lifecycle/exclusions.py",      # refuses first
    "topos/features/entities/fact_materializer.py",  # both purges check
    "topos/features/entities/resolver.py",         # merge refuses asymmetric protection
}


def test_no_unguarded_writer_deletes_from_entities():
    """``_delete_entity_cascade`` was called "the one door". It was not.

    An adversarial review found four more writers that delete entities or repoint
    mentions without passing through it, two of which reap a protected entity in
    the same pass the cascade refuses in. A hand-maintained parametrization cannot
    notice the fifth, so this walks the source instead.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[4] / "topos"
    pattern = re.compile(r"""DELETE\s+FROM\s+["']?entities\b""", re.IGNORECASE)
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root.parent).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if not pattern.search(line):
                continue
            if line.lstrip().startswith(("#", "*")) or '``' in line:
                continue  # prose in a docstring
            if rel not in _GUARDED_ENTITY_DELETE_SITES:
                offenders.append(f"{rel}:{lineno}")
    assert offenders == [], (
        "these delete from `entities` and are not on the guarded allowlist — each "
        "must call derived_scrub.is_entity_protected before deleting, then be added "
        f"to _GUARDED_ENTITY_DELETE_SITES: {offenders}"
    )


def test_a_protected_retention_is_reported_not_silent(corpus):
    """"Nothing was orphaned" and "something was protected" are different outcomes.

    Callers report ``len(orphan_ids)`` as ``entities_removed``. Without a
    retention count a protected entity is invisible in the report — the owner
    cannot tell that a scrub declined to touch something on their behalf.
    """
    _strip_to_reapable(corpus.conn, BH_ID)

    report = purge_derived_for_records(corpus.conn, ["rec-1"])

    assert report.get("entities_retained_protected", 0) >= 1


def test_the_junk_scrub_never_reports_a_protected_row_as_removed(corpus):
    """A false receipt is worse than no receipt."""
    from topos.features.lifecycle import derived_scrub as ds

    corpus.conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,0,0)",
        ("ent-late-bh", "person", "Ana", normalize_entity_name("Ana")),
    )
    corpus.conn.commit()

    # Protection applied AFTER the candidate scan: the cascade is the backstop.
    real = ds._protected_entity_keys
    calls = {"n": 0}

    def late(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            return set(), set()  # scan sees it as unprotected
        return real(conn)

    ds._protected_entity_keys = late
    try:
        BlackholeStore(corpus.conn).blackhole_entity(entity_ref="ent-late-bh")
        corpus.conn.commit()
        report = purge_junk_minted_entities(corpus.conn)
    finally:
        ds._protected_entity_keys = real

    assert _entity_exists(corpus.conn, "ent-late-bh")
    assert report.get("junk_entities_retained_protected", 0) >= 1
