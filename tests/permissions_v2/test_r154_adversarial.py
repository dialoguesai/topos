"""R-N154 adversarial checks on BL-171 (the reviewer's file, kept as the M1 regression test).

`opt_out_other` is left out of the race: its setup assesses e3 after e3 was opted out, which the store refuses
(`owner_opted_out`) before the race is reached; the opt-out case is covered by `opt_out_member`."""
from __future__ import annotations

import os
import sqlite3

import pytest

from tests.permissions_v2.test_bl157_basis_keeps_serving import (  # noqa: F401
    QUERY, TEXT, _restrict, assess, journal_database, path, served, sharing, sweep,
)
from topos.permissions_v2.search_index import SearchIndexService

pytestmark = pytest.mark.public


def _authority(node):
    with node.ledger._transaction() as db:
        return node.ledger._authority(db, "grant-search", node.now[0])[0]


@pytest.mark.parametrize("change", ["member_reassessed", "member_reassessed_alike", "member_corrected",
                                    "opt_out_member"])
def test_open_then_publish_race_poisons_the_kept_verdict(sharing, monkeypatch, change):
    """An ungated check (send check / restore / sweep) whose _open read file Y while a publish put X at the path
    proves Y and keeps the verdict under X's stat identity. A later gated recheck on X then reuses it."""
    node = sharing
    p = path(node)
    assess(node, "e2")                       # e2 shareable too, so a rebuild has members
    x_bytes = p.read_bytes()                 # X: the file as built before the restriction (holds e1)
    _restrict(node, change)                  # X now fails the guard
    assert node.rebuild() == {"grant-search": "ready"}
    assess(node, "e3", domains=["hobbies"], sensitivity="special")   # digest moves past Y's basis; Y's guard holds
    x_tmp = p.parent / ".x-tmp.db"
    x_tmp.write_bytes(x_bytes)
    os.chmod(x_tmp, 0o600)
    real_open = SearchIndexService._open
    fired = []

    def racing_open(target):
        content = real_open(target)          # reads Y
        if not fired:
            fired.append(1)
            os.replace(x_tmp, target)        # a publication lands between the read and the stat
        return content
    monkeypatch.setattr(SearchIndexService, "_open", staticmethod(racing_open))
    node.index._guard_kept.clear()
    try:
        node.index.check_own("grant-search", _authority(node), now=node.now[0])
    except Exception as exc:  # noqa: BLE001
        pass
    monkeypatch.setattr(SearchIndexService, "_open", staticmethod(real_open))
    calls = []
    real_guard = SearchIndexService._reviews_current

    def counted(self, index, key, *, db=None):
        if db is None:
            calls.append(1)
        return real_guard(self, index, key, db=db)
    monkeypatch.setattr(SearchIndexService, "_reviews_current", counted)
    output, refused = node.search_request(QUERY, k=10)
    released = {r.get("content") for r in (output or {}).get("records", [])}
    assert len(calls) == 1, "X's guard was not proven at the search"
    # What BL-157 requires of X: it drops (X's own guard fails under the current reviews).
    assert output is None and refused is not None, (
        f"X served from a verdict proven on another file (e1 released: {TEXT in released})")


def test_raw_write_with_restored_mtime_still_moves_the_token(sharing):
    node = sharing
    assess(node, "e2")
    served(node)
    token = node.index._guard_token()
    rp = node.corpus.reviews.path
    states = {s: os.stat(s) for s in (rp, str(rp) + "-wal") if os.path.exists(s)}
    with sqlite3.connect(rp) as db:   # another connection, same bytes written back
        db.execute("UPDATE review_identity SET highest_generation=highest_generation")
    for s, st in states.items():
        os.utime(s, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert node.index._guard_token() != token
