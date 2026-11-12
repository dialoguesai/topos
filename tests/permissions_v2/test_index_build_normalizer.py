"""A share-index build keeps the matcher's short normalised strings for itself (entity_boundary.build_normalizer).

What these stop: a build that keeps a string the scope should not (a whole message), a scope that outlives the build or
leaks into the next one, and a build whose published members differ with the memory on and off.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus
from topos.permissions_v2 import entity_boundary, search_index
from topos.permissions_v2.search_index import index_path

pytestmark = pytest.mark.usefixtures("retired_search_profile")

SHORT = "Ｍａｒａ Example"
LONG = "a whole message that is longer than the build keeps " * 3


def test_the_build_scope_keeps_only_short_strings_and_normalises_as_without_it():
    expected = {value: (entity_boundary.normalized(value), entity_boundary.skeleton(value)) for value in (SHORT, LONG)}
    assert entity_boundary._PASS_NORMALIZER.get() is None
    with entity_boundary.build_normalizer():
        memory = entity_boundary._PASS_NORMALIZER.get()
        assert memory.keep_max == entity_boundary.BUILD_KEEP_MAX < len(LONG)
        for value in (SHORT, LONG, SHORT, LONG):
            assert (entity_boundary.normalized(value), entity_boundary.skeleton(value)) == expected[value]
        assert set(memory.normalized) == {SHORT} and set(memory.skeleton) == {SHORT}
    assert entity_boundary._PASS_NORMALIZER.get() is None
    assert not memory.open and not memory.normalized and not memory.skeleton and memory.chars == 0


def test_the_build_scope_ends_on_every_path_and_an_open_one_is_used_as_it_is():
    with pytest.raises(RuntimeError):
        with entity_boundary.build_normalizer():
            outer = entity_boundary._PASS_NORMALIZER.get()
            with entity_boundary.build_normalizer():
                assert entity_boundary._PASS_NORMALIZER.get() is outer
                entity_boundary.normalized(SHORT)
            assert outer.open and SHORT in outer.normalized    # the inner scope did not end the outer one
            raise RuntimeError
    assert entity_boundary._PASS_NORMALIZER.get() is None and not outer.open and not outer.normalized


def test_with_pass_memory_off_the_build_keeps_nothing(monkeypatch):
    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", False)
    with entity_boundary.build_normalizer():
        assert entity_boundary._PASS_NORMALIZER.get() is None


def published(node) -> str:
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    conn = sqlite3.connect(str(path))
    try:
        digest = hashlib.sha256()
        for row in conn.execute("SELECT opaque_id, event_at_us, doc_len, terms_json FROM members ORDER BY opaque_id"):
            digest.update(repr(tuple(row)).encode())
        for row in conn.execute("SELECT opaque_id, chunk_index, vector FROM vectors ORDER BY opaque_id, chunk_index"):
            digest.update(repr(tuple(row)).encode())
        return digest.hexdigest()
    finally:
        conn.close()


def test_a_rebuild_runs_inside_the_scope_and_publishes_what_it_publishes_without_it(tmp_path, monkeypatch):
    corpus = mc.build(tmp_path / "corpus", seed=5, counts={name: 2 for name in mc.KINDS})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    seen = []
    build_once = search_index.SearchIndexService._rebuild_once

    def spy(self, *args, **kwargs):
        seen.append(entity_boundary._PASS_NORMALIZER.get())
        return build_once(self, *args, **kwargs)
    monkeypatch.setattr(search_index.SearchIndexService, "_rebuild_once", spy)

    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", False)
    assert node.rebuild() == {"grant-search": "ready"}
    without = published(node)
    assert seen == [None]

    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", True)
    assert node.rebuild() == {"grant-search": "ready"}
    assert published(node) == without
    assert seen[1] is not None and seen[1].keep_max == entity_boundary.BUILD_KEEP_MAX
    assert not seen[1].open and not seen[1].normalized                # ended with the build
    assert entity_boundary._PASS_NORMALIZER.get() is None


def test_the_builds_yield_points_run_inside_the_scope_and_leave_it_open(tmp_path, monkeypatch) -> None:
    """``owner_requests.pace`` only sleeps on the build's own thread, so a yield keeps the memory and hands it to no one."""
    corpus = mc.build(tmp_path / "corpus", seed=5, counts={name: 2 for name in mc.KINDS})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    at_yield = []
    monkeypatch.setattr(search_index.owner_requests, "pace",
                        lambda **_: at_yield.append((entity_boundary._PASS_NORMALIZER.get(),
                                                     getattr(entity_boundary._PASS_NORMALIZER.get(), "open", False))) or 0.0)
    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", True)

    assert node.rebuild() == {"grant-search": "ready"}
    assert at_yield, "the build reached no yield point"
    memory = at_yield[0][0]
    assert memory is not None and all(seen is memory and was_open for seen, was_open in at_yield)
    assert not memory.open and entity_boundary._PASS_NORMALIZER.get() is None   # still ends with the build
