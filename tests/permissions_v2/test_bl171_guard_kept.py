"""BL-171: the review guard is proven once per state, not three times per search.

1.5.3 (BL-157) keeps a share served while the node-wide review digest moves, by re-proving every member's sealed
review bindings. It did so at every stage of every search (index load, gated recheck, send check) and every sweep:
each time the write gate, the review store's whole authority digest, and an unseal, review parse and digest per
member. On the owner's 550-member share, under a recipient's concurrent batch searches, index_load went from 0.46 s
to 1.3 s (p50) and the send check's p95 from 72 ms to 13.6 s.

Now the index load leaves the guard to the gated recheck (it is per member, like the member loop N5 left there), and
a True verdict is reused for the same file and key while the review store's token has not moved at all. These tests
pin that the reuse never hides a change: every construction BL-157 drops for still drops, with the verdict warm.

Invented evidence only (the BL-157 journal fixture); no model, network or personal data.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_bl157_basis_keeps_serving import (  # noqa: F401 -- the fixtures
    QUERY, _restrict, assess, journal_database, path, served, sharing, sweep,
)
from topos.permissions_v2.search_index import SearchIndexService

pytestmark = pytest.mark.public


@pytest.fixture
def guard_calls(monkeypatch):
    calls = []
    real = SearchIndexService._reviews_current

    def counted(self, index, key, *, db=None):
        if db is None:
            calls.append(1)
        return real(self, index, key, db=db)
    monkeypatch.setattr(SearchIndexService, "_reviews_current", counted)
    return calls


def _warm(node):
    """A review outside the index moves the digest; the first search proves the guard, the second reuses it."""
    before = served(node)
    assess(node, "e2")
    assert served(node) == before
    return before


def test_the_guard_is_proven_once_while_nothing_moves(sharing, guard_calls):
    node = sharing
    before = _warm(node)
    assert len(guard_calls) == 1, "the first search after the move proves the guard once (its gated recheck)"
    for _ in range(3):
        assert served(node) == before                            # byte for byte, as BL-157 requires
    assert sweep(node) == 0
    assert len(guard_calls) == 1, guard_calls


def test_the_index_load_leaves_the_guard_to_the_gated_recheck(sharing, guard_calls, monkeypatch):
    node = sharing
    assess(node, "e2")
    seen = []
    real = SearchIndexService._current

    def spy(self, *a, **kw):
        seen.append((kw.get("members", True), kw.get("review_guard", True)))
        return real(self, *a, **kw)
    monkeypatch.setattr(SearchIndexService, "_current", spy)
    served(node)
    assert (False, False) in seen, "the index load still ran the guard"
    assert (True, True) in seen, "the gated recheck did not run the guard"


@pytest.mark.parametrize("change", ["member_reassessed", "member_reassessed_alike", "member_corrected",
                                    "opt_out_member", "opt_out_other", "off_limits", "content", "owner_only"])
def test_a_kept_verdict_never_hides_a_change_that_can_touch_a_member(sharing, change):
    node = sharing
    _warm(node)
    served(node)                                                 # the verdict is kept now
    _restrict(node, change)
    output, refused = node.search_request(QUERY, k=10)
    assert output is None and refused is not None
    sweep(node)
    assert not path(node).exists()


def test_any_review_write_forces_a_new_proof(sharing, guard_calls):
    """Even one that touches no member: the token says only 'the store moved', so the guard is proven again."""
    node = sharing
    before = _warm(node)
    assess(node, "e3", domains=["hobbies"], sensitivity="special")
    assert served(node) == before
    assert len(guard_calls) == 2


def test_a_new_index_file_is_proven_for_itself(sharing, guard_calls):
    node = sharing
    _warm(node)
    assert node.rebuild() == {"grant-search": "ready"}            # a new file (the owed build), the same key
    assess(node, "e3", domains=["hobbies"], sensitivity="special")   # move the digest past the new basis
    count = len(guard_calls)
    served(node)
    assert len(guard_calls) == count + 1


def test_an_unreadable_token_never_reuses(sharing, guard_calls, monkeypatch):
    node = sharing
    _warm(node)
    monkeypatch.setattr(SearchIndexService, "_guard_token", lambda self: None)
    served(node)
    served(node)
    assert len(guard_calls) == 3


def test_a_failed_proof_is_never_kept(sharing, monkeypatch):
    node = sharing
    _warm(node)
    node.index._guard_kept.clear()
    monkeypatch.setattr(SearchIndexService, "_reviews_current", lambda self, index, key, *, db=None: False)
    output, refused = node.search_request(QUERY, k=10)
    assert output is None and refused is not None
    assert node.index._guard_kept == {}


def test_a_verdict_belongs_to_the_file_it_proved(sharing, guard_calls):
    """The same path with another stat identity (a file written in place, or replaced) is proven again."""
    import os
    node = sharing
    _warm(node)
    stat = path(node).stat()
    os.utime(path(node), ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    served(node)
    assert len(guard_calls) == 2
