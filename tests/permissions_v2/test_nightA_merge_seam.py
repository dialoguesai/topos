"""Night review A: the merge seam itself -- the rollback floor and request consumption,
each exercised through the one recipient door that ships: knowledge search (p2c-v3).

The bookkeeping stream proved these against the floor store and the locator door it
owned; the search stream added a second recipient door beside them. The locator door
is removed (N8), and with it the cases that drove it (its capability allow-list and the
same restore read through it). What remains is the search door's half, on a v3 grant.
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_harness import recipient
from topos.permissions_v2.canonical import PolicyError

MEMBERS, SEED = 8, 515


@pytest.fixture
def node(tmp_path):
    return dst.build(tmp_path / "v3-seam", members=MEMBERS, hidden_facts=0, seed=SEED)


def a_query(node):
    """A word the first member carries, so the first search releases something."""
    return dst.texts(MEMBERS, SEED)[0].split()[0]


# ---------------------------------------------------------------- (f) the rollback floor

def _roll_back(node, tmp_path):
    """Take a copy, advance the protection log past it, then put the copy back in place.

    Same inode, lower sequence: the copy-over restore C4 describes. The search index and
    the ledger are untouched by the restore, which is the point -- they are what a restored
    node would otherwise be served from.
    """
    backup = tmp_path / "older.db"
    shutil.copyfile(node.corpus.path, backup)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) "
                     "VALUES('conversation_messages','imessage:99999')")
        conn.commit()
    return backup


def test_F1_a_copy_over_restore_is_refused_at_the_next_search(node, tmp_path):
    """C4's case, through the search door.

    The envelope is issued BEFORE the restore, as the control plane would issue it, so the
    request that arrives afterwards is a well-formed one against a rolled-back file rather
    than one that fails to be built. Search must refuse it, and must not serve the answer
    out of the index, which was built against the newer file and survives the restore.
    """
    query = a_query(node)
    assert node.search_request(query, k=5)[1] is None, "vacuous: search refused before the restore"
    backup = _roll_back(node, tmp_path)
    request_id = node.next_id("rollback")
    payload = {"query": query, "k": 5}
    envelope = node._envelope(node.search_raw["binding"]["grant_id"], "permissions.v2.search", payload, request_id)
    shutil.copyfile(backup, node.corpus.path)       # in place: same inode, lower sequence
    with recipient("actor-1", "client-2"):
        with pytest.raises(PolicyError) as caught:
            node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id)
    assert caught.value.code, "refused, but with no code"


# ------------------------------------------------- (d) the request is consumed in every branch

def test_D1_a_search_request_id_is_consumed_even_when_the_decision_refuses(node, monkeypatch):
    """Admission binds the request id before any branch, so a refusal cannot hand the
    id back. Without this a recipient could retry a refused id until a racing owner
    write made it permit, and the ledger would have no record of the first attempt."""
    from topos.permissions_v2.search_release import MessageSearchRelease
    monkeypatch.setattr(MessageSearchRelease, "_decide",
                        lambda self, *a, **k: (_ for _ in ()).throw(PolicyError("forced")))
    request_id = "search-consumed-1"
    assert node.search_request(a_query(node), k=5, request_id=request_id)[1] == "permission_denied"
    monkeypatch.undo()
    output, refused = node.search_request(a_query(node), k=5, request_id=request_id)
    assert output is None and refused == "request_replay", (refused, output)


def test_D2_a_search_request_id_is_consumed_on_a_permit(node):
    request_id = "search-consumed-2"
    output, refused = node.search_request(a_query(node), k=5, request_id=request_id)
    assert refused is None and output["records"], "vacuous: the first request did not release"
    again, reason = node.search_request(a_query(node), k=5, request_id=request_id)
    assert again is None and reason == "request_replay", (reason, again)
