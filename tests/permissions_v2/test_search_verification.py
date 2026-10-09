"""WS4 N3a: a search computes its Off-limits closure and review digest once and reuses them across its
own stages (index load, gated recheck, send check) only while nothing they were computed from has changed.

Pinned here:
- a quiet search builds one EntityBoundary, not four, and reads the review digest once, not three times;
- reuse releases exactly what recomputing every stage releases (non-empty answers, compared as bytes);
- any commit between two stages forces the full check in the next one: a protected change refuses, an
  unrelated one recomputes and answers as before; an opt-out write refuses; a replaced database refuses;
- a commit landing just after a stage's snapshot is never trusted by a later stage, at either read;
- each half of the token moves on its own: data_version on a commit with the file state frozen, the
  file state on a non-SQLite rewrite with data_version frozen, and the file identity on a replacement;
- a reused closure reads every record context again on its own stage's connection.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_entity_boundary_search import node  # noqa: F401 -- the Off-limits fixture
from tests.permissions_v2.test_message_search_refusals import PAYLOAD, Socket, relay_message, signed
from topos.permissions_v2 import search_index, search_release, search_transport
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.search_index import SearchVerification
from topos.permissions_v2.search_release import MessageSearchRelease

# Some cases here run a search under a profile a node no longer serves (N8): the suite takes the lift
# (conftest.py `retired_search_profile`) so they run as they did, for the code p2c-v3 shares with it.
pytestmark = pytest.mark.usefixtures("retired_search_profile")

ALIAS = "UPDATE entities SET aliases_json='[\"roadmap\"]' WHERE entity_id='protected-entity'"
UNRELATED = ("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
             "VALUES('unrelated-entity','person','Quinn Other','quinn other')")


STALE_BASIS = "message search index stale (basis)"  # the boundary or the digest no longer matches the build


def commit(node, sql):
    with sqlite3.connect(node.corpus.path, timeout=0.5) as conn:
        conn.execute(sql)


def wal(node):
    """A node's canonical database in WAL mode, as connection tuning runs it. Only there can a commit land
    while a read transaction is open; under the rollback journal the writer waits for every reader."""
    with sqlite3.connect(node.corpus.path) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"


def stale_basis(caplog) -> bool:
    # BL-155 isolates review dependencies from the otherwise unchanged basis.
    return any(record.getMessage() in {STALE_BASIS, 'message search index stale (member_reviews)'}
               for record in caplog.records)


def boundaries_built(monkeypatch) -> list:
    built = []
    original = EntityBoundary.__init__

    def init(self, conn):
        built.append(1)
        original(self, conn)
    monkeypatch.setattr(EntityBoundary, "__init__", init)
    return built


def verifications(monkeypatch) -> list:
    made = []
    original = MessageSearchRelease.verification

    def verification(self):
        made.append(original(self))
        return made[-1]
    monkeypatch.setattr(MessageSearchRelease, "verification", verification)
    return made


def never_reuse(monkeypatch):
    """Every stage recomputes, exactly as before N3a: no token can be read, so none matches."""
    monkeypatch.setattr(SearchVerification, "canonical_token", lambda self: None)
    monkeypatch.setattr(SearchVerification, "review_token", lambda self: None)


async def relayed(node, monkeypatch, request_id, payload=PAYLOAD) -> dict:
    message = relay_message(node, signed(node, payload=payload, request_id=request_id), payload, monkeypatch,
                            request_id=request_id)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    return frame


@pytest.fixture
def direct(tmp_path):
    node = dst.build(tmp_path / "direct", members=6, hidden_facts=0, seed=9)
    node.query = {"query": dst.queries(6, 9, 1)[0], "k": 5}
    return node


# -- reuse when nothing changed ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_quiet_search_builds_one_closure_where_it_built_four(node, monkeypatch):
    built, made = boundaries_built(monkeypatch), verifications(monkeypatch)
    frame = await relayed(node, monkeypatch, "quiet-1")
    assert frame["status"] == "ok" and frame["payload"]["output"]["records"]
    assert len(built) == 1
    [verified] = made
    # The recheck reuses it; since N5 the send check, finding nothing moved, never needs it.
    assert verified.computed["boundary"] == 1 and verified.reused["boundary"] == 1 and verified.reused["send"] == 1
    assert verified._probes == {}  # closed with the search

    never_reuse(monkeypatch)
    built.clear()
    frame = await relayed(node, monkeypatch, "quiet-2")
    assert frame["status"] == "ok"
    # Every stage recomputes: index load, the gated read (its check and its re-decisions share one, on one
    # snapshot), send. The engine before N3a built 4 here, the gated read building its own twice.
    assert len(built) == 3


@pytest.mark.asyncio
async def test_a_quiet_direct_search_reads_the_review_digest_once(direct, monkeypatch):
    made = verifications(monkeypatch)
    digests = []
    original = direct.corpus.reviews.current_authority_digest
    monkeypatch.setattr(direct.corpus.reviews, "current_authority_digest", lambda: digests.append(1) or original())
    frame = await relayed(direct, monkeypatch, "digest-1", direct.query)
    assert frame["status"] == "ok" and frame["payload"]["output"]["records"]
    [verified] = made
    assert len(digests) == 1 and verified.computed["digest"] == 1 and verified.reused["digest"] == 1  # recheck (N5)
    assert verified.computed["boundary"] == 1 and verified.reused["boundary"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["node", "direct"])
async def test_reuse_releases_exactly_what_recomputing_releases(fixture, request, monkeypatch):
    subject = request.getfixturevalue(fixture)
    payload = getattr(subject, "query", PAYLOAD)
    reused = await relayed(subject, monkeypatch, "same-1", payload)
    never_reuse(monkeypatch)
    recomputed = await relayed(subject, monkeypatch, "same-2", payload)
    assert reused["status"] == recomputed["status"] == "ok"
    assert reused["payload"]["output"]["records"]  # a real comparison, not empty against empty
    assert json.dumps(reused["payload"]["output"], sort_keys=True) == json.dumps(recomputed["payload"]["output"], sort_keys=True)


# -- a change between stages forces the full check --------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unrelated", "alias"])
@pytest.mark.parametrize("between", ["load_and_recheck", "checkpoint_and_send"])
async def test_a_commit_between_stages_forces_the_full_check(node, monkeypatch, caplog, between, change):
    baseline = await relayed(node, monkeypatch, "between-0")
    assert baseline["status"] == "ok" and baseline["payload"]["output"]["records"]
    made = verifications(monkeypatch)
    sql = UNRELATED if change == "unrelated" else ALIAS
    if between == "load_and_recheck":
        original = search_release.rank

        def rank(*args, **kwargs):
            commit(node, sql)
            return original(*args, **kwargs)
        monkeypatch.setattr(search_release, "rank", rank)
    else:
        original = node.search.dispatch

        def dispatch(**kwargs):
            result = original(**kwargs)
            commit(node, sql)
            return result
        monkeypatch.setattr(node.search, "dispatch", dispatch)
    frame = await relayed(node, monkeypatch, "between-1")
    [verified] = made
    if change == "alias":  # an alias on the protected entity: the closure moves, the signed clock does not
        assert frame["status"] == "error" and stale_basis(caplog)
        return
    assert frame["status"] == "ok"
    assert frame["payload"]["output"] == baseline["payload"]["output"]
    # Recomputed in the stage after the commit. Before the recheck: the recheck recomputes, and the send check,
    # finding nothing moved since, skips (N5). Before the send: the recheck reuses, the send check recomputes.
    reused = 0 if between == "load_and_recheck" else 1
    assert verified.computed["boundary"] == 2 and verified.reused["boundary"] == reused


@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["index_load", "recheck"])
async def test_a_commit_just_after_a_snapshot_is_never_trusted_later(node, monkeypatch, caplog, read):
    """The token kept with a closure is read BEFORE its snapshot. Read after it, a commit landing between the
    two would be counted in the token and missing from the closure, and a later stage would reuse it.
    WAL only: under the rollback journal the commit cannot land while the snapshot's read is open."""
    wal(node)
    if read == "index_load":
        original, calls = search_index.clock_state, []

        def clock_state(conn):  # check_own's snapshot is established here
            value = original(conn)
            if not calls:
                calls.append(1)
                commit(node, ALIAS)
            return value
        monkeypatch.setattr(search_index, "clock_state", clock_state)
    else:
        reviews, calls = node.search.reviews, []
        original = reviews._observe_clock

        def observe(conn):  # the gated read's snapshot was established just before this
            if not calls:
                calls.append(1)
                commit(node, ALIAS)
            return original(conn)
        monkeypatch.setattr(reviews, "_observe_clock", observe)
    frame = await relayed(node, monkeypatch, "race-1")
    assert calls == [1]
    # Refused because a later stage rebuilt the closure and saw it moved, not because the commit failed.
    assert frame["status"] == "error" and stale_basis(caplog)
    with sqlite3.connect(node.corpus.path) as conn:
        assert "roadmap" in conn.execute("SELECT aliases_json FROM entities WHERE entity_id='protected-entity'").fetchone()[0]


@pytest.mark.asyncio
async def test_a_review_write_between_checkpoint_and_send_refuses(direct, monkeypatch, caplog):
    made = verifications(monkeypatch)
    original = direct.search.dispatch

    def dispatch(**kwargs):
        result = original(**kwargs)
        with owner():
            direct.corpus.reviews.opt_out("fact-outside-the-grant", now=direct.now[0])
        return result
    monkeypatch.setattr(direct.search, "dispatch", dispatch)
    frame = await relayed(direct, monkeypatch, "review-1", direct.query)
    assert frame["status"] == "error" and stale_basis(caplog)
    [verified] = made
    assert verified.computed["digest"] == 2  # the send check read the store again


@pytest.mark.asyncio
async def test_a_review_write_just_after_the_first_digest_is_never_trusted_later(direct, monkeypatch, caplog):
    """The digest is kept only when the store's token read before it equals the one read after it: a write
    landing between the digest and the second read is in the token and not in the value."""
    reviews, calls = direct.corpus.reviews, []
    original = reviews.current_authority_digest

    def digest_then_write():
        value = original()
        if not calls:
            calls.append(1)
            with owner():
                reviews.opt_out("fact-outside-the-grant", now=direct.now[0])
        return value
    monkeypatch.setattr(reviews, "current_authority_digest", digest_then_write)
    frame = await relayed(direct, monkeypatch, "review-race-1", direct.query)
    assert calls == [1]
    assert frame["status"] == "error" and stale_basis(caplog)


@pytest.mark.asyncio
async def test_a_database_replaced_between_checkpoint_and_send_refuses(direct, monkeypatch):
    """A byte copy put in place has the same rows and clock: only the file identity tells it apart.
    The reused digest still runs the store's own file checks, which see the canonical file replaced."""
    original = direct.search.dispatch
    path = direct.corpus.path

    def dispatch(**kwargs):
        result = original(**kwargs)
        copy = path.with_name(path.name + ".copy")
        shutil.copy2(path, copy)
        os.replace(copy, path)
        return result
    monkeypatch.setattr(direct.search, "dispatch", dispatch)
    frame = await relayed(direct, monkeypatch, "replaced-1", direct.query)
    assert frame["status"] == "error"


# -- the token, one half at a time ------------------------------------------------------------------

def scratch_database(tmp_path, journal):
    path = tmp_path / f"canonical-{journal}.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA journal_mode={journal}")
        conn.execute("CREATE TABLE entities(entity_id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO entities VALUES('a','Alpha')")
    return path


def standalone(path):
    return SearchVerification(SimpleNamespace(path=path, _incarnation=lambda: None), None)


@pytest.mark.parametrize("journal", ["WAL", "DELETE"])
def test_reads_leave_the_token_and_a_commit_moves_it_with_the_file_state_frozen(tmp_path, monkeypatch, journal):
    path = scratch_database(tmp_path, journal)
    with standalone(path) as verified:
        first = verified.canonical_token()
        with sqlite3.connect(path) as reader:
            reader.execute("SELECT * FROM entities").fetchall()
        assert first is not None and verified.canonical_token() == first
        monkeypatch.setattr(search_index, "_file_state", lambda _path: ("frozen",))
        frozen = verified.canonical_token()
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE entities SET name='Beta'")
        assert verified.canonical_token() != frozen


@pytest.mark.parametrize("journal", ["WAL", "DELETE"])
def test_a_rewrite_outside_sqlite_moves_the_token_with_data_version_frozen(tmp_path, monkeypatch, journal):
    path = scratch_database(tmp_path, journal)
    with standalone(path) as verified:
        monkeypatch.setattr(SearchVerification, "_data_version", lambda self, _path: 0)
        first = verified.canonical_token()
        time.sleep(0.02)  # the file state is only as fine as the filesystem's timestamps
        data = path.read_bytes()
        with open(path, "r+b") as handle:
            handle.write(data[:100])  # the same bytes: no content change, but a write
        assert verified.canonical_token() != first


def test_a_replaced_file_moves_the_token_and_a_closed_verification_matches_nothing(tmp_path):
    path = scratch_database(tmp_path, "DELETE")
    verified = standalone(path)
    first = verified.canonical_token()
    copy = tmp_path / "copy.db"
    shutil.copy2(path, copy)
    os.replace(copy, path)
    assert verified.canonical_token() != first
    verified.close()
    assert verified.canonical_token() is None and verified._probes == {}


def test_a_rebound_closure_reads_contexts_on_its_own_connection(node):
    unit = node.corpus.units[3]
    with sqlite3.connect(node.corpus.path) as seed:
        row = dict(zip([c[0] for c in seed.execute("SELECT * FROM conversation_messages WHERE message_id=?",
                   (unit.message_id,)).description], seed.execute(
                   "SELECT * FROM conversation_messages WHERE message_id=?", (unit.message_id,)).fetchone()))
    first = sqlite3.connect(f"file:{node.corpus.path}?mode=ro", uri=True)
    boundary = EntityBoundary(first)
    assert boundary.active
    identity = dict(table="conversation_messages", record_id=unit.message_id, source_id=row["source_id"],
                    dataset_id=row["dataset_id"], row=row)
    revision = boundary.check(**identity)
    first.close()
    second = sqlite3.connect(f"file:{node.corpus.path}?mode=ro", uri=True)
    statements = []
    second.set_trace_callback(statements.append)
    rebound = boundary.rebind(second)
    assert rebound.check(**identity) == revision
    assert any("conversation_participants" in statement for statement in statements)
    assert rebound.revision == boundary.revision and boundary._context_cache  # the original keeps its own
    second.close()
