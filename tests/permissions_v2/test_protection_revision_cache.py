"""The node-wide protection revision is folded once per owner mutation, not once per read.

`current_protection_revision` digests every Off-limits row and black hole, every exclusion
tombstone and the owner's consent ledger, and it runs on every permissions read, every
status and every ingest door, so the cost of one read grew with the node's whole history.
Every table it folds is watched by the clock -- each insert, update or delete advances the
generation in the same transaction -- so while the generation stands still the value is the
same. These tests pin what the cache trusts and, more importantly, what it does not: the
owner binding and the clock are verified on every call before the cache answers; any
write the triggers see, any DDL, any registry row and any other database file miss it;
and every answer, hit or miss, is the pinned formula's value.
"""
import shutil
import sqlite3
from contextlib import contextmanager

import pytest

from tests.permissions_v2.test_evidence import corpus, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_owner_identity_binding import OWNER, do_attest
from topos.features.lifecycle.exclusions import ExclusionStore
from topos.features.lifecycle.record_protection import RecordProtectionStore, protection_fingerprint
from topos.permissions_v2 import identity, protection_clock
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.exclusion_floor import exclusion_fingerprint
from topos.permissions_v2.protection_clock import (CONTRACT_VERSION, REGISTRY, TABLE, clock_state,
    current_protection_revision, identity_coverage)

# The originals, taken before any test patches the names the revision reads, so the reference
# fold below is never counted as one of the revision's own.
_identity_fingerprint, _exclusion_fingerprint, _protection_fingerprint = (
    identity.identity_fingerprint, exclusion_fingerprint, protection_fingerprint)


@contextmanager
def reading(path):
    """A read transaction the way the resolver and the protocol open one: read-only, `sqlite3.Row`."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def folded(conn):
    """The pinned definition, computed here without the cache."""
    clock_id, generation = clock_state(conn)
    return digest({"clock_id": clock_id, "generation": generation, "protection": _protection_fingerprint(conn),
                   "exclusions": _exclusion_fingerprint(conn), "identity": _identity_fingerprint(conn),
                   "identity_coverage": list(identity_coverage(conn)), "contract_version": CONTRACT_VERSION})


def revision(path):
    with reading(path) as conn:
        return current_protection_revision(conn, owner_id="owner-1")


def reference(path):
    with reading(path) as conn:
        return folded(conn)


def generation(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT generation FROM {TABLE} WHERE singleton=1").fetchone()[0]


@pytest.fixture
def folds(monkeypatch):
    """How many times the three fingerprints were computed, by patching the names the revision reads.

    The corpus fixture's own review-store enrollment already folded and remembered this
    database's revision, so the cache is emptied here: every test below starts from a miss.
    """
    counts = {"protection": 0, "exclusions": 0, "identity": 0}
    protection_clock._REVISIONS.clear()

    def counted(name, function):
        def wrapper(conn):
            counts[name] += 1
            return function(conn)
        return wrapper

    monkeypatch.setattr(protection_clock, "protection_fingerprint", counted("protection", protection_fingerprint))
    monkeypatch.setattr(protection_clock, "exclusion_fingerprint", counted("exclusions", exclusion_fingerprint))
    monkeypatch.setattr(identity, "identity_fingerprint", counted("identity", identity.identity_fingerprint))
    return counts


def total(folds):
    assert len(set(folds.values())) == 1, "the three fingerprints are always folded together"
    return folds["exclusions"]


# --- hits ---------------------------------------------------------------------------------------------


def test_a_second_read_at_the_same_generation_folds_nothing(corpus, folds):
    path = corpus[0].path
    first = revision(path)
    assert total(folds) == 1
    assert revision(path) == revision(path) == first == reference(path)
    assert total(folds) == 1


def test_a_hit_is_the_same_value_the_resolver_sees_inside_its_own_read(corpus, folds):
    """The resolver's read transaction and a protocol-style read at the same generation share one fold."""
    path = corpus[0].path
    first = revision(path)
    assert total(folds) == 1
    with owner():
        corpus[0].inspect_for_review(corpus[2])  # `_read` asks for the revision inside its own transaction
    assert total(folds) == 1, "the resolver's read was a hit"
    assert revision(path) == first == reference(path)


# --- misses: every way the value can move ----------------------------------------------------------------


@pytest.mark.parametrize("change", ["exclusion", "off_limits", "black_hole", "attestation", "self_row"])
def test_every_watched_write_moves_the_revision(corpus, folds, change):
    path = corpus[0].path
    before = revision(path)
    with sqlite3.connect(path) as conn:
        if change == "exclusion":
            ExclusionStore(conn)._tombstone("fact", "someone:likes:jazz", None)
        elif change == "off_limits":
            RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="message-1")
        elif change == "black_hole":
            conn.execute("INSERT INTO entity_blackholes(blackhole_id,normalized_name) VALUES('bh-1','a protected name')")
        elif change == "attestation":
            do_attest(conn, OWNER)
        elif change == "self_row":
            conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) VALUES('second-self','person','P','p',1)")
    assert generation(path) > 0, "the write advanced the clock"
    after = revision(path)
    assert after != before and after == reference(path)
    assert total(folds) == 2


def test_a_schema_change_refolds_without_a_clock_advance(corpus, folds):
    """A column added to a folded table changes every row's encoding and fires no trigger."""
    path = corpus[0].path
    with sqlite3.connect(path) as conn:
        ExclusionStore(conn)._tombstone("fact", "someone:likes:jazz", None)
    before, marked = revision(path), generation(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE intelligence_exclusions ADD COLUMN extra TEXT")
    assert generation(path) == marked, "DDL advances nothing"
    after = revision(path)
    assert after != before and after == reference(path)
    assert total(folds) == 2


def test_a_registry_row_written_outside_a_trigger_refolds(corpus, folds):
    """The registry has no insert trigger of its own; a stray row must still move the revision, as it did uncached."""
    path = corpus[0].path
    before, marked = revision(path), generation(path)
    with sqlite3.connect(path) as conn:
        conn.execute(f"INSERT INTO {REGISTRY}(entity_id,basis,first_generation) VALUES('stray','installed',0)")
    assert generation(path) == marked
    after = revision(path)
    assert after != before and after == reference(path)
    assert total(folds) == 2


def test_two_copies_at_the_same_generation_are_never_confused(corpus, folds, tmp_path):
    """Same clock identity, same generation, different rows: one entry per database file."""
    path = corpus[0].path
    other = tmp_path / "other" / "canonical.db"
    other.parent.mkdir()
    shutil.copy(path, other)
    with sqlite3.connect(path) as conn:
        ExclusionStore(conn)._tombstone("fact", "someone:likes:jazz", None)
    with sqlite3.connect(other) as conn:
        ExclusionStore(conn)._tombstone("fact", "someone:likes:blues", None)
    assert generation(path) == generation(other)
    assert revision(path) == reference(path)
    assert revision(other) == reference(other)
    assert revision(path) != revision(other)
    assert total(folds) == 2


def test_an_older_file_restored_in_place_is_refolded(corpus, folds, tmp_path):
    path = corpus[0].path
    snapshot = tmp_path / "snapshot.db"
    shutil.copy(path, snapshot)
    older = reference(snapshot)
    with sqlite3.connect(path) as conn:
        ExclusionStore(conn)._tombstone("fact", "someone:likes:jazz", None)
    newer = revision(path)
    assert newer != older
    shutil.copy(snapshot, path)
    assert revision(path) == older
    assert revision(path) == older


def test_a_database_without_a_file_is_never_remembered(corpus, folds):
    memory = sqlite3.connect(":memory:")
    with sqlite3.connect(corpus[0].path) as source:
        source.backup(memory)
    memory.execute("BEGIN")
    first = current_protection_revision(memory, owner_id="owner-1")
    assert current_protection_revision(memory, owner_id="owner-1") == first == folded(memory)
    assert total(folds) == 2
    assert protection_clock._database_file(memory) is None


def test_the_cache_holds_a_bounded_number_of_files(corpus, tmp_path):
    for index in range(protection_clock._REVISIONS_LIMIT + 4):
        copy = tmp_path / f"copy-{index}.db"
        shutil.copy(corpus[0].path, copy)
        revision(copy)
    assert len(protection_clock._REVISIONS) <= protection_clock._REVISIONS_LIMIT


# --- what is checked before the cache answers ------------------------------------------------------------


def test_a_lost_trigger_is_refused_before_the_cache_answers(corpus, folds):
    path = corpus[0].path
    revision(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER permissions_v2_intelligence_exclusions_insert")
    with pytest.raises(PolicyError, match="protection_clock_unavailable"):
        revision(path)
    assert total(folds) == 1


def test_a_changed_owner_binding_is_refused_before_the_cache_answers(corpus, folds):
    path = corpus[0].path
    revision(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE engine_config SET value='someone-else' WHERE key='user_id'")
    with pytest.raises(PolicyError, match="node_owner_binding"):
        revision(path)
    with pytest.raises(PolicyError, match="node_owner_binding"):
        with reading(path) as conn:
            current_protection_revision(conn, owner_id="someone-else-2")
    assert total(folds) == 1


def test_a_missing_floor_table_is_refused_before_the_cache_answers(corpus, folds):
    path = corpus[0].path
    revision(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE intelligence_exclusions")
    with pytest.raises(PolicyError, match="protection_schema_unavailable"):
        revision(path)
    assert total(folds) == 1
