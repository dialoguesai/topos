"""NR189 (D-17): a row the owner keeps from every share (`ai_chat_messages.share_excluded = 1`) reaches no share path.

The on-ramp (topos#126) added the column: an assistant turn of an AI coding session that touched a Topos surface. The
owner keeps the row and nobody else gets it (on-ramp CONTRACTS.md §2.6, ruling R8). The enrichment jobs and the
grantee store path already skip it. These tests cover the sharing reads in `permissions_v2`: each site gets one
flagged row and one plain row and must see only the plain one, and on a database without the column every site reads
both rows exactly as before. The node is one whose only data is one imported AI-chat export
(`test_fresh_node_message_tables.ai_chat_only`); it has the column, and the "no_column" cases drop it (with its index
and triggers) to stand for a database from before migration 85.

Most sites are tested with the user's own prompt flagged, not only an assistant turn: the AI-coding sync flags
assistant turns only (§2.7), and those are already refused as "not the owner's words", which would hide whether the
filter itself works. The review pass is also tested with the assistant turn flagged, which is the real shape.

DR-31 (restrictions apply at once): a row flagged after its share's index was built is never served again. The request
path's member re-check reads no row for it (`_live_rows`), so the index is dropped, and the release re-check refuses it
(`_load`). This holds whether or not `share_excluded` is part of the reviewed surface (`REVIEW_SURFACE_EXCLUSIONS`).

The upgrade (WS0 ruling, 16 Oct 2026): migration 85 adds the column as NOT NULL DEFAULT 0, so on a node that already
holds AI-chat rows every row gains a value. Inside the reviewed surface that moved every AI-chat row's revision and
context revision: every review went stale and every AI-chat share index dropped. The column is now left out of the
surface, and so out of the context revision, which reads the same list (`entity_boundary.context_bookkeeping`). With it
out, a later 0 -> 1 flip no longer moves a revision either: the two filters above are what make the flip apply at
once, so the exclusion ships only with them. The DR-31 tests run with the column out of the surface (today) and in it
(before the ruling).

Every person, word and id here is invented.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from tests.ingestion.test_ai_chat_writer_class import captured_jobs  # noqa: F401 (fixture)
from tests.permissions_v2.test_fresh_node_message_tables import (  # noqa: F401 (fixture)
    AI_CHAT, EXPORT, LINE, NOW, OWNER, REPLY, _bound, _line, _own_pass, _share, _share_policy, ai_chat_only)
from tests.permissions_v2.test_ingest_provenance import owner
from topos.permissions_v2.canonical import PolicyError

LINE_ID, REPLY_ID, PLAIN_ID = "m-line", "m-reply", "m-plain"
PLAIN_TEXT = "An invented second question about rock pools for the same open day."
DAY = 86_400


# --- helpers -------------------------------------------------------------------------------------------------------

def _columns(store) -> set:
    return {row[1] for row in store.db.execute("PRAGMA table_info(ai_chat_messages)")}


def _add_column(store) -> None:
    """What the node's next start does (migration 85 is always-run): the column, its index and its two triggers."""
    from topos.storage.db.migrations.ai_coding_sessions_v1 import ensure_share_excluded_column
    ensure_share_excluded_column(store.db)
    store.db.commit()


def _flag(store, message_id: str) -> None:
    store.db.execute("UPDATE ai_chat_messages SET share_excluded = 1 WHERE message_id = ?", (message_id,))
    store.db.commit()


def _drop_column(store) -> None:
    """A database from before migration 85: no column, and none of its index and triggers."""
    from topos.storage.db.migrations.ai_coding_sessions_v1 import TRIGGER_ON_INSERT, TRIGGER_ON_METADATA
    if "share_excluded" in _columns(store):
        for trigger in (TRIGGER_ON_INSERT, TRIGGER_ON_METADATA):
            store.db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        store.db.execute("DROP INDEX IF EXISTS idx_ai_chat_messages_share_excluded")
        store.db.execute("ALTER TABLE ai_chat_messages DROP COLUMN share_excluded")
        store.db.commit()
    assert "share_excluded" not in _columns(store)


def _prepare(store, column: bool, flagged: str = LINE_ID) -> None:
    """With `column`, the column is there and `flagged` is set; without, the database predates the column."""
    if column:
        _add_column(store)
        _flag(store, flagged)
    else:
        _drop_column(store)


def _second_prompt(store) -> None:
    """A second user prompt in the same chat, a copy of the first with its own id and words."""
    row = dict(store.db.execute("SELECT * FROM ai_chat_messages WHERE message_id = ?", (LINE_ID,)).fetchone())
    row.update(message_id=PLAIN_ID, content=PLAIN_TEXT, event_at="2026-09-01T10:00:02Z")
    for name in ("content_rendered", "content_disclosure"):
        if row.get(name) is not None:
            row[name] = PLAIN_TEXT
    if row.get("source_record_id") is not None:
        row["source_record_id"] = PLAIN_ID
    if "share_excluded" in row:
        row["share_excluded"] = 0
    names = sorted(row)
    store.db.execute(f"INSERT INTO ai_chat_messages ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                     [row[name] for name in names])
    store.db.commit()


# --- 1. an evidence load: the build, an answer's records and every re-qualification read rows here -----------------

@pytest.mark.parametrize("column", [True, False], ids=["flagged", "no_column"])
def test_an_evidence_load_refuses_a_flagged_row(ai_chat_only, column):
    _prepare(ai_chat_only, column)
    resolver, _reviews = _bound(ai_chat_only)
    with resolver._read(gated=False) as (conn, _floor):
        assert resolver._load(conn, _line(resolver, REPLY_ID))["content"] == REPLY
        if column:
            with pytest.raises(PolicyError) as refused:
                resolver._load(conn, _line(resolver, LINE_ID))
            assert refused.value.code == "evidence_missing"
        else:
            assert resolver._load(conn, _line(resolver, LINE_ID))["content"] == LINE


# --- 2. the member re-check (and the build's sealing) ---------------------------------------------------------------

@pytest.mark.parametrize("column", [True, False], ids=["flagged", "no_column"])
def test_the_member_recheck_reads_no_row_for_a_flagged_member(ai_chat_only, column):
    from topos.permissions_v2.search_index import _live_rows, _member_fingerprint
    _prepare(ai_chat_only, column)
    conn = sqlite3.connect(ai_chat_only.path)
    try:
        def member(record_id):
            return {"table": AI_CHAT, "record_id": record_id, "source_id": EXPORT, "facts": []}
        plain_rows, plain_facts = _live_rows(conn, member(REPLY_ID))
        assert len(plain_rows) == 1 and _member_fingerprint(plain_rows, plain_facts, table=AI_CHAT) is not None
        rows, facts = _live_rows(conn, member(LINE_ID))
        if column:
            # No row: the build skips the member (its fingerprint is None) and the re-check's `len(rows) != 1` drops
            # the index, as it does for a deleted row.
            assert rows == [] and _member_fingerprint(rows, facts, table=AI_CHAT) is None
        else:
            assert [row["content"] for row in rows] == [LINE]
    finally:
        conn.close()


# --- 3. the owner's review queue (its candidates are the way into an owner review) ---------------------------------

@pytest.mark.parametrize("column", [True, False], ids=["flagged", "no_column"])
def test_the_review_queue_lists_no_flagged_prompt(ai_chat_only, column):
    from topos.permissions_v2.message_evidence import _page_rows
    _second_prompt(ai_chat_only)
    _prepare(ai_chat_only, column)
    conn = sqlite3.connect(ai_chat_only.path)
    try:
        listed = sorted(row[0] for row in _page_rows(conn, AI_CHAT, NOW - DAY, NOW))
    finally:
        conn.close()
    assert listed == ([PLAIN_ID] if column else [LINE_ID, PLAIN_ID])


# --- 4. the automatic review pass (its candidates become machine reviews, which index builds start from) ------------

def test_the_review_pass_assesses_no_flagged_turn(ai_chat_only):
    """The real shape: the AI-coding sync flags the assistant's reply, and the pass no longer even reads it."""
    _prepare(ai_chat_only, True, flagged=REPLY_ID)
    resolver, reviews = _bound(ai_chat_only)
    assert _own_pass(resolver, reviews) == {"scanned": 1, "assessed": 1, "withheld": 0}


@pytest.mark.parametrize("column", [True, False], ids=["flagged", "no_column"])
def test_the_review_pass_page_holds_no_flagged_row(ai_chat_only, column):
    from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
    _prepare(ai_chat_only, column)
    resolver, _reviews = _bound(ai_chat_only)
    page = AutomaticReviewWorker(resolver, None)._page(AI_CHAT, "", SimpleNamespace(after=NOW - DAY, before=NOW))
    assert sorted(row[0] for row in page) == ([REPLY_ID] if column else [LINE_ID, REPLY_ID])


# --- 5. the share page's counts -----------------------------------------------------------------------------------

@pytest.mark.parametrize("column", [True, False], ids=["flagged", "no_column"])
def test_the_share_counts_count_no_flagged_row(ai_chat_only, column):
    """A flagged row is not counted at all, neither as shareable nor as held back: no share can ever hold it."""
    from topos.permissions_v2.registry import parse_policy
    from topos.permissions_v2.share_counts import count, forget
    _prepare(ai_chat_only, column)
    resolver, reviews = _bound(ai_chat_only)
    _own_pass(resolver, reviews)
    forget()
    try:
        with owner(actor=OWNER):
            counted = count(resolver, reviews, parse_policy(_share_policy()), now=NOW)
    finally:
        forget()
    ai_chats = counted["kinds"]["ai_chats"]
    held = sum(ai_chats["held_back"].values())
    # Without the column the prompt can be shared (the assistant's reply is never shareable, so it is in no count).
    # With the prompt flagged it is in no count either: not shareable, and not "held back" for any reason a share has.
    assert (ai_chats["can_share"], held) == ((0, 0) if column else (1, 0))


# --- 6. the share editor's source catalog -------------------------------------------------------------------------

@pytest.mark.parametrize("column", [True, False], ids=["flagged", "no_column"])
def test_the_catalog_counts_no_flagged_row(ai_chat_only, column):
    from topos.permissions_v2.share_catalog import catalog
    _prepare(ai_chat_only, column)
    reply = catalog(ai_chat_only.path, [], now=NOW, env={})
    assert [(s["source_id"], s["table"], s["rows"]) for s in reply["sources"]] == [(EXPORT, AI_CHAT, 1 if column else 2)]


def test_the_catalog_keeps_a_source_whose_rows_are_all_flagged_only_when_installed(ai_chat_only):
    """Every row flagged: the source is no longer listed from its rows (an install would still list it, at 0)."""
    from topos.permissions_v2.share_catalog import catalog
    _add_column(ai_chat_only)
    _flag(ai_chat_only, LINE_ID)
    _flag(ai_chat_only, REPLY_ID)
    assert catalog(ai_chat_only.path, [], now=NOW, env={})["sources"] == []


# --- 7. the owner-attested snapshot's fact extraction (its facts are evidence a share can cite) ---------------------

def _snapshot_db(tmp_path, *, column: bool) -> sqlite3.Connection:
    from topos.permissions_v2.ingest_protocol import CHATGPT_SOURCE_ID
    conn = sqlite3.connect(tmp_path / "snapshot.db")
    flag = ", share_excluded INTEGER NOT NULL DEFAULT 0" if column else ""
    conn.execute("CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, conversation_id TEXT, sender_type TEXT,"
                 f" event_at TEXT, content TEXT, metadata_json TEXT, source_id TEXT{flag})")
    conn.execute("CREATE TABLE ingest_provenance_records (message_id TEXT, enrollment_id TEXT,"
                 " enrollment_revision INTEGER, job_id TEXT)")
    for message_id, excluded in (("snap-plain", 0), ("snap-flagged", 1)):
        values = [message_id, "snap-chat", "user", "2026-09-01T10:00:00Z", "An invented line.", "{}", CHATGPT_SOURCE_ID]
        conn.execute(f"INSERT INTO ai_chat_messages VALUES ({','.join('?' * (len(values) + column))})",
                     values + ([excluded] if column else []))
        conn.execute("INSERT INTO ingest_provenance_records VALUES (?, 'enrol-1', 1, 'job-1')", (message_id,))
    conn.commit()
    return conn


@pytest.mark.parametrize("column", [True, False], ids=["flagged", "no_column"])
def test_the_snapshot_fact_extraction_reads_no_flagged_row(tmp_path, monkeypatch, column):
    from topos.permissions_v2 import ingest_snapshot_facts
    from topos.permissions_v2.ingest_protocol import CHATGPT_SOURCE_ID
    conn = _snapshot_db(tmp_path, column=column)
    seen = []
    context = SimpleNamespace(table=AI_CHAT, source_id=CHATGPT_SOURCE_ID, enrollment_id="enrol-1",
                              enrollment_revision=1, job_id="job-1", require_batch=lambda _conn: None,
                              existing_record=lambda _conn, message_id: seen.append(message_id) or True)
    # No attested self: the job stops after counting its rows, before any fact is written.
    monkeypatch.setattr(ingest_snapshot_facts, "_attested_self", lambda _conn: None)
    stats = ingest_snapshot_facts.extract_snapshot_facts(conn, None, context)
    conn.close()
    assert sorted(seen) == (["snap-plain"] if column else ["snap-flagged", "snap-plain"])
    assert stats["rows_linked"] == stats["owner_subject_unattested"] == len(seen)


# --- 8. DR-31: a row flagged after the build is never served again, at once ---------------------------------------

@pytest.fixture(params=["column_outside_surface", "column_inside_surface"])
def surface(request, monkeypatch):
    """`share_excluded` left out of the reviewed surface (today, WS0 ruling), or in it (before the ruling): the flag
    is refused at once either way, so the filters, not the revision, are what make it apply."""
    from topos.permissions_v2 import evidence
    exclusions = dict(evidence.REVIEW_SURFACE_EXCLUSIONS)
    if request.param == "column_outside_surface":
        exclusions[AI_CHAT] = exclusions[AI_CHAT] | {"share_excluded"}
    else:
        exclusions[AI_CHAT] = exclusions[AI_CHAT] - {"share_excluded"}
    monkeypatch.setattr(evidence, "REVIEW_SURFACE_EXCLUSIONS", exclusions)
    return request.param


def _served(node) -> list:
    output, _refused = node.search_request("tide pools talk", k=10)
    return [] if output is None else [record["content"] for record in output["records"]]


def _built(store):
    _add_column(store)
    resolver, reviews = _bound(store)
    assert _own_pass(resolver, reviews) == {"scanned": 2, "assessed": 1, "withheld": 1}
    node = _share(store, resolver, reviews)
    with owner(actor=OWNER):
        assert node.index.rebuild("grant-search", now=NOW) == {"state": "ready", "member_count": 1}
    assert _served(node) == [LINE]
    return node


def test_a_member_flagged_after_the_build_is_refused_on_the_next_request(ai_chat_only, surface):
    node = _built(ai_chat_only)
    _flag(ai_chat_only, LINE_ID)
    assert _served(node) == []
    assert _served(node) == []     # and on every request after it


def test_a_member_flagged_after_the_build_takes_the_index_down_at_the_next_sweep(ai_chat_only, surface):
    node = _built(ai_chat_only)
    _flag(ai_chat_only, LINE_ID)
    with owner(actor=OWNER):
        assert node.index.sweep(now=NOW) == 1
    assert _served(node) == []


def test_a_rebuild_after_the_flag_never_brings_the_member_back(ai_chat_only, surface):
    node = _built(ai_chat_only)
    _flag(ai_chat_only, LINE_ID)
    with owner(actor=OWNER):
        assert node.index.rebuild("grant-search", now=NOW)["member_count"] == 0
    assert _served(node) == []


# --- 9. the upgrade: the column appearing with 0 moves nothing -----------------------------------------------------

OFF_LIMITS = "Quillmarsh"   # an invented Off-limits entry the line does not name: it makes the boundary active, so the
                            # index seals each member's entity dependencies and context revision


def _pass(resolver, reviews) -> dict:
    """The node's own assessment pass, as `_own_pass`, with the count of reviews it found still current."""
    import asyncio
    from tests.permissions_v2.test_automatic_message_review import answer
    from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
    from topos.permissions_v2.message_review_contract import AutomaticReviewRequest

    async def classify(prepared):
        return answer(prepared)
    worker = AutomaticReviewWorker(resolver, reviews, classifier=classify)
    with owner(actor=OWNER):
        asyncio.run(worker._process(AutomaticReviewRequest(after=NOW - DAY, before=NOW)))
        status = worker.status()
    return {name: getattr(status, name) for name in ("scanned", "assessed", "current", "withheld")}


def _revisions(resolver, reviews) -> dict:
    """What a review and an index pin for the line: its row revision, its Off-limits context revision, its
    classification context revision, and the revision the stored machine review holds."""
    from topos.permissions_v2.automatic_message_review import context_for, machine_key
    from topos.permissions_v2.evidence import _row_revision
    identity = _line(resolver, LINE_ID)
    with resolver._read(gated=False) as (conn, _floor):
        row = resolver._load(conn, identity)
        boundary = resolver.entity_boundary(conn)
        assert boundary.active
        found = {"row": _row_revision(row, table=AI_CHAT),
                 "context": boundary.check(table=AI_CHAT, record_id=LINE_ID, source_id=EXPORT, dataset_id=None, row=row),
                 "classification_context": context_for(conn, identity, row, boundary=boundary)[0]}
    with owner(actor=OWNER), reviews._db() as db:
        found["machine_review"] = reviews._current_in(db, machine_key(identity)).snapshot.message.revision
    return found


def test_the_column_appearing_with_0_moves_no_revision_and_keeps_the_index(ai_chat_only):
    """Migration 85 on a node that already holds a reviewed, shared AI-chat line: nothing it pinned moves."""
    _drop_column(ai_chat_only)                     # the node before the upgrade
    ai_chat_only.db.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,"
                            "rebuild_state) VALUES('bh-nr189','',?,?,'complete')", (OFF_LIMITS, OFF_LIMITS.lower()))
    ai_chat_only.db.commit()
    resolver, reviews = _bound(ai_chat_only)
    assert _pass(resolver, reviews) == {"scanned": 2, "assessed": 1, "current": 0, "withheld": 1}
    node = _share(ai_chat_only, resolver, reviews)
    with owner(actor=OWNER):
        assert node.index.rebuild("grant-search", now=NOW) == {"state": "ready", "member_count": 1}
    assert _served(node) == [LINE]
    before = _revisions(resolver, reviews)
    assert before["row"] == before["machine_review"]

    _add_column(ai_chat_only)                      # the upgrade: every stored row now reads share_excluded = 0
    assert {row[0] for row in ai_chat_only.db.execute("SELECT share_excluded FROM ai_chat_messages")} == {0}
    assert _revisions(resolver, reviews) == before
    assert _served(node) == [LINE]                 # the index built before the column still answers
    with owner(actor=OWNER):
        assert node.index.sweep(now=NOW) == 0      # and the deep sweep keeps it
    assert _served(node) == [LINE]
    # The node's next pass finds the line's review current: nothing to assess again.
    assert _pass(resolver, reviews) == {"scanned": 2, "assessed": 0, "current": 1, "withheld": 1}


def test_the_column_is_outside_the_reviewed_surface_and_the_context_revision():
    """A pin, with the reason: out of both lists, a flip moves no revision, so only the load and the member re-check
    (the tests above) make it apply at once. Neither list may drop the column without those two filters."""
    from topos.permissions_v2 import entity_boundary, evidence
    assert "share_excluded" in evidence.REVIEW_SURFACE_EXCLUSIONS[AI_CHAT]
    assert "share_excluded" in entity_boundary.context_bookkeeping(AI_CHAT)
    row = {"message_id": LINE_ID, "conversation_id": "c-1", "sender_type": "user", "event_at": "2026-09-01T10:00:00Z",
           "content": LINE, "source_id": EXPORT}
    revisions = {evidence._row_revision({**row, **extra}, table=AI_CHAT)
                 for extra in ({}, {"share_excluded": 0}, {"share_excluded": 1})}
    assert len(revisions) == 1


# --- 10. the census names the reason (WS0, 16 Oct 2026) -------------------------------------------------------------

def _census(store, resolver, reviews, node):
    """The grant census (scripts/permissions_v2/grant_census.py) over this node's own stores, at the run instant."""
    from pathlib import Path
    from tests.permissions_v2.test_grant_census import gc
    from topos.permissions_v2.search_index import root_for
    index_root = root_for(resolver.path)
    return gc, gc.run(canonical=Path(resolver.path), reviews=Path(reviews.path), ledger=node.ledger.path,
                      index_root=index_root, keys=index_root / "keys.db", binding=resolver.binding,
                      live_canonical=None, now=NOW)


@pytest.mark.parametrize("flagged", [True, False], ids=["flagged", "plain"])
def test_the_census_tallies_a_flagged_row_as_its_own_policy_reason(ai_chat_only, flagged):
    """Not the engineering `evidence_missing` the node's load raises for it: the owner's rule, which no fix lifts.
    Its exposure stage is unchanged (refused before any proof). Product code and the census pins are untouched."""
    _add_column(ai_chat_only)
    if flagged:
        _flag(ai_chat_only, LINE_ID)
    resolver, reviews = _bound(ai_chat_only)
    _own_pass(resolver, reviews)
    node = _share(ai_chat_only, resolver, reviews)
    with owner(actor=OWNER):
        node.index.rebuild("grant-search", now=NOW)
    gc, census = _census(ai_chat_only, resolver, reviews, node)
    outcomes = {o.record_id: o for o in census.outcomes}
    line, reply = outcomes[LINE_ID], outcomes[REPLY_ID]
    if flagged:
        assert (line.stage, line.reason, line.veto) == ("qualify", "share_excluded", "share_excluded")
        assert gc.reason_class(line.reason) == "policy" and line.reason in gc.UNPROVEN
        assert not line.permitted
    else:
        assert line.permitted and line.reason != "share_excluded" and line.veto != "share_excluded"
    assert "share_excluded" not in (reply.reason, reply.veto)
    assert "share_excluded" in gc.POLICY and "share_excluded" not in gc.ENGINEERING
    assert gc.mirrored_sources() == gc.PINNED


def _twin(store) -> None:
    """A second AI-chat row with exactly the line's words, under its own id."""
    row = dict(store.db.execute("SELECT * FROM ai_chat_messages WHERE message_id = ?", (LINE_ID,)).fetchone())
    row.update(message_id="m-twin", event_at="2026-09-01T10:00:03Z")
    if row.get("source_record_id") is not None:
        row["source_record_id"] = "m-twin"
    names = sorted(row)
    store.db.execute(f"INSERT INTO ai_chat_messages ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                     [row[name] for name in names])
    store.db.commit()


@pytest.mark.parametrize("twin", [False, True], ids=["alone", "with_twin"])
def test_the_census_runs_on_a_node_that_never_made_the_messages_table(ai_chat_only, twin):
    """BL-108's shape: a node whose only data is an AI-chat import has no `conversation_messages` table. The census's
    policy veto counted copies with the bare statement over both message tables, so an owner line it had to veto (here
    unassessed) ended the whole run with `evidence_storage_unavailable`. It now counts as the node does
    (`evidence._copy_count`): a table never made holds no copy, and a real twin is still found."""
    if twin:
        _twin(ai_chat_only)
    resolver, reviews = _bound(ai_chat_only)            # no review pass: the owner's line is unassessed
    node = _share(ai_chat_only, resolver, reviews)
    with owner(actor=OWNER):
        node.index.rebuild("grant-search", now=NOW)
    gc, census = _census(ai_chat_only, resolver, reviews, node)
    line = {o.record_id: o for o in census.outcomes}[LINE_ID]
    assert not line.permitted
    if twin:   # the node refuses the copy first; the census's own veto counts the twin too
        assert (line.reason, line.veto) == ("independent_copy_lineage", "independent_copy_lineage")
    else:      # unassessed, and nothing the owner's policy says withholds it
        assert line.reason in gc.UNASSESSED and line.veto is None
    tables = {row[0] for row in ai_chat_only.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_messages" not in tables          # the census made nothing on the way
    assert gc.mirrored_sources() == gc.PINNED


# --- 11. cost: a per-member read never probes the schema -------------------------------------------------------------

def test_the_per_member_reads_run_no_schema_probe(ai_chat_only):
    """`_load` and `_live_rows` run once per member (builds, re-checks, sweeps): they read the flag off the row they
    already select, so neither issues a `PRAGMA table_info`."""
    from topos.permissions_v2.search_index import _live_rows
    _prepare(ai_chat_only, True)
    resolver, _reviews = _bound(ai_chat_only)
    statements = []
    with resolver._read(gated=False) as (conn, _floor):
        conn.set_trace_callback(statements.append)
        resolver._load(conn, _line(resolver, REPLY_ID))
        with pytest.raises(PolicyError):
            resolver._load(conn, _line(resolver, LINE_ID))
        _live_rows(conn, {"table": AI_CHAT, "record_id": LINE_ID, "source_id": EXPORT, "facts": []})
        conn.set_trace_callback(None)
    assert statements and not [s for s in statements if "table_info(ai_chat_messages)" in s.replace(" ", "")]


# --- 12. the helpers --------------------------------------------------------------------------------------------------

def test_the_clause_takes_a_probe_already_made():
    from topos.storage.share_excluded import row_share_excluded, share_excluded_clause, share_excluded_predicate

    class NoQueries:
        def execute(self, *_args):
            raise AssertionError("probed although the caller passed what it knew")

    assert share_excluded_clause(NoQueries(), alias="m", present=True) == " AND m.share_excluded = 0"
    assert share_excluded_clause(NoQueries(), alias="m", present=False) == ""
    assert share_excluded_predicate(NoQueries(), present=True) == "share_excluded = 0"
    assert share_excluded_predicate(NoQueries(), present=False) == ""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ai_chat_messages (message_id TEXT, share_excluded INTEGER NOT NULL DEFAULT 0)")
    assert share_excluded_clause(conn, alias="m") == " AND m.share_excluded = 0"     # no probe given: it probes
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO ai_chat_messages VALUES ('a', 0), ('b', 1)")
    stored = {row["message_id"]: row for row in conn.execute("SELECT * FROM ai_chat_messages")}
    assert not row_share_excluded(stored["a"]) and row_share_excluded(stored["b"])
    assert row_share_excluded({"share_excluded": 1}) and not row_share_excluded({"share_excluded": 0})
    assert not row_share_excluded({"message_id": "c"})            # a row without the column never is
    assert row_share_excluded({"share_excluded": "1"})            # anything but 0 is: fail closed
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    bare.execute("CREATE TABLE ai_chat_messages (message_id TEXT)")
    bare.execute("INSERT INTO ai_chat_messages VALUES ('d')")
    assert not row_share_excluded(bare.execute("SELECT * FROM ai_chat_messages").fetchone())
    conn.close()
    bare.close()
