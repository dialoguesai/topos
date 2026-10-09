"""BL-155 (1.5.2): a share index survives the message store's sync bookkeeping; every real change still drops it.

A p2c-v3 share over invented iMessages (`direct_search_twins.build`, `protected=True`: an ACTIVE Off-limits boundary
on an invented person no member mentions). With the boundary active every member carries its message as an entity
dependency sealed with `boundary.check(...)`'s context revision (search_index.py `entity_dependencies`), re-compared
on every sweep. Before 1.5.2 that revision digested the parent `conversations` row and the `conversation_participants`
roster whole, `updated_at` included, and the message store rewrites `updated_at` on both for every sync batch that
touches the conversation (conversations_tables.py `_upsert_conversation_row`, the participants upsert), so every
batch dropped the index as `stale (dependencies)` and the share went dark while it rebuilt.

The privacy argument the brief asks for, one test each:
  1. a timestamp-only change keeps the index                 (`..._bump_alone_keeps_the_index`, both tables)
  2. a protected contact joining drops it at once             (`test_a_protected_contact_joining_drops_at_once`)
  3. a new participant or a new sender drops it               (`..._new_participant_drops`, `..._new_sender_drops`)
  4. a new message next to a shared one drops it              (`test_a_new_neighbour_from_self_drops`)
  5. the release-time decision still reads every column       (`test_release_re_decides_the_member_on_every_column`)
and the veto itself reads the bookkeeping columns the revision leaves out
(`test_the_revision_leaves_bookkeeping_out_but_the_veto_reads_it`). An index sealed under another contract fails
once and rebuilds once (`test_an_index_sealed_under_another_contract_rebuilds_once`).
"""
from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from tests.permissions_v2 import direct_search_twins as twins
from topos.permissions_v2 import entity_boundary as eb
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.search_index import index_path

PROTECTED_NAME = "Mara Example"   # the invented Off-limits person `direct_search_twins.protect` writes

# The production statements, verbatim but for the table-name constants (conversations_tables.py `_upsert_conversation_row`
# and the participants upsert): what one sync batch writes to a conversation it touches.
CONVERSATION_UPSERT = """
    INSERT INTO conversations
    (conversation_id, dataset_id, source_id, created_at, updated_at)
    VALUES (?, ?, ?, datetime('now'), datetime('now'))
    ON CONFLICT(conversation_id, dataset_id) DO UPDATE SET
        source_id = COALESCE(NULLIF(excluded.source_id, ''), conversations.source_id),
        updated_at = excluded.updated_at
"""
PARTICIPANT_UPSERT = """
    INSERT INTO conversation_participants
    (conversation_id, dataset_id, source_id, contact_id, role, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
    ON CONFLICT(conversation_id, dataset_id, source_id, contact_id) DO UPDATE SET
        role = COALESCE(excluded.role, conversation_participants.role),
        updated_at = datetime('now')
"""


def _node(tmp_path, *, protected=True, members=6):
    return twins.build(tmp_path / "twin", members=members, hidden_facts=0, seed=3, protected=protected)


def _grant(node):
    return node.search_raw["binding"]["grant_id"]


def _path(node):
    return index_path(node.index.root, _grant(node))


def _conversations(node):
    with sqlite3.connect(node.corpus.path) as conn:
        return conn.execute("SELECT DISTINCT conversation_id, dataset_id, source_id FROM conversation_messages").fetchall()


def _bump_conversations(node):
    """What one sync batch of new messages in each member conversation writes to its parent row."""
    time.sleep(1.1)  # datetime('now') has one-second resolution
    with sqlite3.connect(node.corpus.path) as conn:
        for conversation_id, dataset_id, source_id in _conversations(node):
            conn.execute(CONVERSATION_UPSERT, (conversation_id, dataset_id, source_id))


def _upsert_participant(node, contact_id, role="participant"):
    with sqlite3.connect(node.corpus.path) as conn:
        for conversation_id, _dataset_id, source_id in _conversations(node):
            conn.execute(PARTICIPANT_UPSERT, (conversation_id, twins.DATASET, source_id, contact_id, role))


def _sweep(node, caplog):
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        node.index.sweep(now=node.now[0])
    return [record.getMessage() for record in caplog.records if "message search index stale" in record.getMessage()]


def _append_message(node, *, sender_id, is_from_self, message_id="new-after-last"):
    with sqlite3.connect(node.corpus.path) as conn:
        conversation_id, dataset_id, source_id = _conversations(node)[0]
        conn.execute("INSERT INTO conversation_messages(message_id,conversation_id,dataset_id,source_id,source_record_id,"
                     "sender_id,sender_type,is_from_self,event_at,content) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (message_id, conversation_id, dataset_id, source_id, message_id, sender_id, "human", is_from_self,
                      "2099-01-01T00:00:00Z", "an invented reply"))


def _first_member(node):
    with sqlite3.connect(node.corpus.path) as conn:
        return conn.execute("SELECT message_id, conversation_id FROM conversation_messages ORDER BY message_id LIMIT 1").fetchone()


def _check(node, message_id):
    """(row revision, the context revision `check` seals, the boundary revision) on a fresh boundary, as production."""
    from topos.permissions_v2.evidence import _row_revision
    resolver = node.corpus.resolver
    with resolver._read(gated=False) as (conn, _floor):
        identity = resolver._identity("conversation_messages", message_id, "imessage", twins.DATASET)
        row = resolver._load(conn, identity)
        boundary = eb.EntityBoundary(conn)
        assert boundary.active
        return (_row_revision(row, table="conversation_messages"),
                boundary.check(table="conversation_messages", record_id=message_id, source_id="imessage",
                               dataset_id=twins.DATASET, row=row),
                boundary.revision)


# --- control ------------------------------------------------------------------------------------------------------

def test_control_nothing_changed_the_index_stays(tmp_path, caplog):
    node = _node(tmp_path)
    assert _path(node).exists()
    assert _sweep(node, caplog) == [] and _path(node).exists()


# --- 1. a timestamp-only change keeps the index (these fail on the 1.5.1 digest) ---------------------------------

def test_a_conversation_row_bump_alone_keeps_the_index(tmp_path, caplog):
    node = _node(tmp_path)
    before = _check(node, _first_member(node)[0])
    _bump_conversations(node)          # no message, no review, no Off-limits change: only updated_at moved
    assert _check(node, _first_member(node)[0]) == before
    assert _sweep(node, caplog) == [] and _path(node).exists()


def test_a_roster_updated_at_bump_alone_keeps_the_index(tmp_path, caplog):
    node = _node(tmp_path)
    _upsert_participant(node, "contact-ordinary")   # an ordinary (invented, unprotected) participant, then a fresh build
    assert node.rebuild()[_grant(node)] == "ready"
    assert _sweep(node, caplog) == [] and _path(node).exists()
    time.sleep(1.1)
    _upsert_participant(node, "contact-ordinary")   # the next batch re-upserts the same participant: updated_at only
    assert _sweep(node, caplog) == [] and _path(node).exists()


def test_the_revision_leaves_bookkeeping_out_but_the_veto_reads_it(tmp_path):
    """The revision ignores `created_at`/`updated_at`; the Off-limits check still reads them like any column."""
    node = _node(tmp_path)
    message_id, conversation_id = _first_member(node)
    before = _check(node, message_id)
    assert eb.context_bookkeeping("conversations") == {"created_at", "updated_at"}
    assert eb.context_bookkeeping("conversation_participants") == {"created_at", "updated_at"}
    _upsert_participant(node, "contact-ordinary")
    sealed = _check(node, message_id)
    assert sealed[1] != before[1]                      # a new participant moves it (point 3) ...
    time.sleep(1.1)
    _upsert_participant(node, "contact-ordinary")
    assert _check(node, message_id) == sealed           # ... its re-upsert does not (point 1)
    with sqlite3.connect(node.corpus.path) as conn:    # a bookkeeping column holding a protected name
        conn.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?", (PROTECTED_NAME, conversation_id))
    with pytest.raises(PolicyError, match="entity_protected"):
        _check(node, message_id)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("UPDATE conversations SET updated_at=datetime('now') WHERE conversation_id=?", (conversation_id,))
        conn.execute("UPDATE conversation_participants SET created_at=? WHERE conversation_id=?", (PROTECTED_NAME, conversation_id))
    with pytest.raises(PolicyError, match="entity_protected"):
        _check(node, message_id)


# --- 2. a protected contact joining drops the index at once -------------------------------------------------------

def test_a_protected_contact_joining_drops_at_once(tmp_path, caplog):
    node = _node(tmp_path)
    _upsert_participant(node, "protected-contact")
    assert _sweep(node, caplog) == ["message search index stale (member_unavailable)"]   # the veto, not the revision
    assert not _path(node).exists()


# --- 3. a new participant or a new sender drops it ---------------------------------------------------------------

def test_an_ordinary_new_participant_drops(tmp_path, caplog):
    node = _node(tmp_path)
    _upsert_participant(node, "contact-new")
    assert _sweep(node, caplog) == ["message search index stale (dependencies)"]
    assert not _path(node).exists()


def test_a_new_message_from_a_new_sender_drops(tmp_path, caplog):
    node = _node(tmp_path)
    _append_message(node, sender_id="contact-new", is_from_self=0)
    assert _sweep(node, caplog) == ["message search index stale (dependencies)"]
    assert not _path(node).exists()


# --- 4. a new message next to a shared one drops it --------------------------------------------------------------

def test_a_new_neighbour_from_self_drops(tmp_path, caplog):
    node = _node(tmp_path)
    _append_message(node, sender_id="self", is_from_self=1)
    assert _sweep(node, caplog) == ["message search index stale (classification_context)"]
    assert not _path(node).exists()


# --- 5. the release-time decision reads every column -------------------------------------------------------------

@pytest.mark.parametrize("column", ["role", "updated_at"])
def test_release_re_decides_the_member_on_every_column(tmp_path, caplog, column):
    """A protected name written into a roster column withholds the member at release, whether the column is in the
    revision (`role`) or outside it (`updated_at`): the request path re-runs the full `entity_boundary.check` on the
    current rows before anything is ranked (search_index.py `_current`, logged `member_unavailable`; then `_floors`,
    message_evidence.py, on every candidate), so the index's revision never decides a release by itself. The
    bookkeeping variant is the one that matters: there the sealed revision is unchanged, and only this check
    stands between the member and the recipient."""
    node = _node(tmp_path, members=12)
    before, refused = node.search_request("roadmap", k=5)
    assert refused is None and before["records"]
    content = before["records"][0]["content"]
    with sqlite3.connect(node.corpus.path) as conn:
        conversation_id = conn.execute("SELECT conversation_id FROM conversation_messages WHERE content=?", (content,)).fetchone()[0]
        conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id,role) "
                     "VALUES(?,?,?,'contact-ordinary','participant')", (conversation_id, twins.DATASET, "imessage"))
    assert node.rebuild()[_grant(node)] == "ready"
    message_id = _first_member_in(node, conversation_id)
    sealed = _check(node, message_id)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute(f"UPDATE conversation_participants SET {column}=? WHERE conversation_id=? AND contact_id='contact-ordinary'",
                     (PROTECTED_NAME, conversation_id))
    with pytest.raises(PolicyError, match="entity_protected"):     # the check on the current rows: vetoed
        _check(node, message_id)
    with sqlite3.connect(node.corpus.path) as conn:                # the revision alone: moved for `role`, not for `updated_at`
        conn.execute(f"UPDATE conversation_participants SET {column}=? WHERE conversation_id=? AND contact_id='contact-ordinary'",
                     ("plain-value" if column == "role" else "2001-01-01 00:00:00", conversation_id))
    assert (_check(node, message_id)[1] == sealed[1]) == (column == "updated_at")
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute(f"UPDATE conversation_participants SET {column}=? WHERE conversation_id=? AND contact_id='contact-ordinary'",
                     (PROTECTED_NAME, conversation_id))
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        output, refused = node.search_request("roadmap", k=5)
    assert output is None and refused is not None
    assert [r.getMessage() for r in caplog.records if "message search index stale" in r.getMessage()] == [
        "message search index stale (member_unavailable)"]
    assert node.rebuild()[_grant(node)] == "ready"
    output, refused = node.search_request("roadmap", k=5)
    assert refused is None, refused
    assert content not in {item["content"] for item in output["records"]}


def _first_member_in(node, conversation_id):
    with sqlite3.connect(node.corpus.path) as conn:
        return conn.execute("SELECT message_id FROM conversation_messages WHERE conversation_id=? ORDER BY message_id LIMIT 1",
                            (conversation_id,)).fetchone()[0]


# --- the upgrade: an index sealed under another contract fails once and rebuilds once ------------------------------

def test_an_index_sealed_under_another_contract_rebuilds_once(tmp_path, caplog, monkeypatch):
    with monkeypatch.context() as patched:
        patched.setattr(eb, "CONTEXT_REVISION_CONTRACT", "context-before-1.5.2/test")
        node = _node(tmp_path)
    assert _sweep(node, caplog) == ["message search index stale (dependencies)"]
    assert not _path(node).exists()
    assert node.rebuild()[_grant(node)] == "ready"
    _bump_conversations(node)
    assert _sweep(node, caplog) == [] and _path(node).exists()


def test_without_an_active_boundary_the_same_bump_is_not_drift(tmp_path, caplog):
    node = _node(tmp_path, protected=False)
    from topos.storage.canonical.conversations_tables import ensure_conversations_table
    with sqlite3.connect(node.corpus.path) as conn:
        ensure_conversations_table(conn)
    _bump_conversations(node)
    assert _sweep(node, caplog) == [] and _path(node).exists()
