"""R-N154 L5: a build file a dead run left behind is shredded; one being written now is not.

A forced exit (BL-168's deadline) or a crash mid-build leaves `.build-*.db` beside the indexes: an unpublished index,
its members' term bags in plain text. The sweep looked at `grant-*.db` only, so nothing ever removed it.

Invented evidence only (the BL-157 journal fixture).
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_bl157_basis_keeps_serving import (  # noqa: F401 -- the fixtures
    journal_database, path, served, sharing, sweep,
)
from topos.permissions_v2 import search_index

pytestmark = pytest.mark.public


def test_the_sweep_shreds_a_leftover_build_and_spares_one_being_written(sharing):
    node = sharing
    root = node.index.root
    leftover = root / ".build-00000000deadbeef.db"
    leftover.write_bytes(b"synthetic term bag " * 64)
    writing = search_index._build_file(root)
    writing.write_bytes(b"a build in progress")
    try:
        before = served(node)
        assert sweep(node) == 0                      # no published index is counted or touched
        assert not leftover.exists(), "a leftover build file survived the sweep"
        assert writing.exists(), "the sweep shredded a build this process is writing"
        assert path(node).exists() and served(node) == before
    finally:
        search_index._built(writing)
    sweep(node)
    assert not writing.exists()


def test_a_build_unregisters_its_file_whether_it_publishes_or_fails(sharing, monkeypatch):
    node = sharing
    assert node.rebuild() == {"grant-search": "ready"}
    assert search_index._BUILDING == set()
    monkeypatch.setattr(search_index.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("synthetic")))
    try:
        node.rebuild()
    except OSError:
        pass
    assert search_index._BUILDING == set()
    assert list(node.index.root.glob(".build-*.db")) == []
