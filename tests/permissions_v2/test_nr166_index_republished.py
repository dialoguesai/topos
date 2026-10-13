"""NR166 D2: a request in flight when a share's index is rebuilt is answered on the new file, not refused.

A request loads and ranks the share's index file, then decides on it under the write gate. A rebuild publishes a
new file by replacing the old one. Until now a request that had loaded the old file was refused as
`search_index_stale` when its decision found another file at the path: once per rebuild, for nothing the asker did.

It now runs once more, from nothing, on the file that is there. It is never decided on a file it did not load: the
second run loads, checks and ranks the new file itself. A second publish during that run is the uniform refusal;
a file that is gone is `search_index_stale` as before, with no second run.

Pinned here on all three sites (the search door, the batch door, the answers retrieval): the same bytes as a
quiet request; exactly two loads; the new file's own state decides (a member the owner took back between the
two files refuses the request, as it would any request signed before it); twice is refused and there is no third; removed is not retried.

Invented evidence only (the batch suite's p2c-v3 node). The write gate is held as it always was: the switch that
decides outside it is off throughout.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_message_search_batch import QUERIES, node  # noqa: F401
from tests.permissions_v2.test_nr166_decide_outside_gate import (  # noqa: F401 -- the autouse fixture unsets the switch
    PAYLOAD, asked, elsewhere, owner_only_mark, reasons, switch_unset,
)
from tests.permissions_v2.test_nr166_provenance_walk import GRANT, answer, batch
from tests.permissions_v2.test_search_verification import wal
from topos.permissions_v2 import answer_release
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.search_index import index_path, purge
from topos.permissions_v2.search_release import REPUBLISHED, MessageSearchRelease

pytestmark = pytest.mark.public
DOORS = ["search", "batch", "answers"]


def ask(node, door: str, tag: str):
    """What the door releases, as the R1 tests read it; a refusal raises or returns its code."""
    if door == "search":
        records, refused = asked(node, f"{tag}-search")
        if refused is not None:
            raise PolicyError(refused)
        return records
    return batch(node, QUERIES[:3], f"{tag}-batch") if door == "batch" else answer(node, PAYLOAD["query"])


class Loads(list):
    """The loads that finished, in order; `.begun` counts every load that was started, finished or not."""
    begun = 0


def after_each_load(monkeypatch, change, *, loads=(1,)) -> Loads:
    """Run `change` on another thread right after the loads named: the request has its file, and has not decided."""
    seen = Loads()
    original = MessageSearchRelease._load_index

    def load(self, *args, **kwargs):
        seen.begun += 1          # a run that begins is a run, even when its load finds no file and refuses
        loaded = original(self, *args, **kwargs)
        seen.append(len(seen) + 1)
        if seen[-1] in loads:
            elsewhere(change)
        return loaded
    monkeypatch.setattr(MessageSearchRelease, "_load_index", load)
    return seen


def taken_back_then_rebuilt(node) -> None:
    """The owner makes a member theirs alone; the node takes note of it and builds the share's index again."""
    owner_only_mark(node)
    with node.ledger._transaction() as conn:
        node.protocol._sync_protection(conn)
    assert node.rebuild() == {GRANT: "ready"}


@pytest.mark.parametrize("door", DOORS)
def test_a_request_in_flight_when_a_build_publishes_is_answered_on_the_new_file(node, monkeypatch, door):
    wal(node)
    quiet = ask(node, door, "quiet")
    path = index_path(node.index.root, GRANT)
    before = path.stat().st_ino
    loads = after_each_load(monkeypatch, lambda: node.rebuild())
    raced = ask(node, door, "raced")
    assert path.stat().st_ino != before             # the build did publish another file
    assert raced == quiet and loads == [1, 2] and loads.begun == 2   # the same bytes, one more run and no more


def test_a_build_that_follows_the_owner_taking_a_member_back_does_not_let_the_request_through(node, monkeypatch):
    """Between the two files the owner makes a member theirs alone. The request was signed before that change, and
    its decision checks that before it looks at the file at all: refused in its first run, as it always was.
    The new file being there earns it nothing."""
    wal(node)
    seen = reasons(monkeypatch)
    loads = after_each_load(monkeypatch, lambda: taken_back_then_rebuilt(node))
    raced, refused = asked(node, "raced")
    assert raced is None and refused == "permission_denied"
    assert loads == [1] and seen == ["authority_stale"]        # no second run: this is not a republish to retry


@pytest.mark.parametrize("door", DOORS)
def test_a_build_that_publishes_during_both_runs_is_refused_and_there_is_no_third(node, monkeypatch, door):
    wal(node)
    seen = reasons(monkeypatch)
    loads = after_each_load(monkeypatch, lambda: node.rebuild(), loads=(1, 2, 3, 4))
    with pytest.raises(PolicyError) as refused:
        ask(node, door, "twice")
    assert loads == [1, 2] and loads.begun == 2
    if door == "answers":
        assert refused.value.code == REPUBLISHED and REPUBLISHED in answer_release.NOT_SERVED_NOW
    else:
        assert refused.value.code == "permission_denied"          # the wire's one word
        if door == "search":
            assert seen == [REPUBLISHED]                          # the node's own word for it


@pytest.mark.parametrize("door", DOORS)
def test_an_index_removed_in_flight_is_stale_and_not_tried_again(node, monkeypatch, door):
    wal(node)
    loads = after_each_load(monkeypatch, lambda: purge(node.index.root, GRANT))
    with pytest.raises(PolicyError) as refused:
        ask(node, door, "removed")
    assert loads == [1] and loads.begun == 1      # no second run was even begun
    assert refused.value.code == ("search_index_stale" if door == "answers" else "permission_denied")


def test_a_quiet_request_loads_once(node, monkeypatch):
    wal(node)
    loads = after_each_load(monkeypatch, lambda: None, loads=())
    assert ask(node, "search", "quiet") is not None and loads == [1]
