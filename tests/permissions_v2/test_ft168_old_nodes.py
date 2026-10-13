"""FT168 item 5: what a node from before topos#111 does with a share that names an ontology switched off.

Main at 6a9b8a9e is the wheel the owner's node runs today (labelled 1.5.8). Its fact decision
(`knowledge_projections.fact_projection`) is loaded from that commit and put in place of the current one on a
fixture node, so the build, the release, the answer writer's items and the owner's count all decide a fact as that
node does. Everything else is this tree's, which for a records-and-facts share differs from main only in what these
tests are about (the five product files topos#111 changed).

What it documents, with the node's facts switch on:
  - an old node never reads the owner's ontology switches. A share that signs Work and career releases its facts
    whether the ontology is on, off, or the registry is gone. The Lenses toggle does not stop fact sharing there;
  - the owner's count and preview on such a node call the fact shareable, so they agree with what is released;
  - a switch moves no digest there, so nothing is rebuilt for it (nothing needs to be);
  - with the node's facts switch off (the default on every node today) no fact is released by old or new.

Invented data only.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.permissions_v2 import ft168_support as ft
from tests.permissions_v2.test_ft168_exposure import (CONTROL, STATEMENT, count_and_preview, released_everywhere,
                                                      serving)
from tests.permissions_v2.test_ft168_switch_loops import fact_members
from topos.permissions_v2 import fact_preview, knowledge_projections, refresh_loop, selective_facts

MAIN = "6a9b8a9e"


def test_main_reads_no_ontology_switch_anywhere_in_the_fact_decision():
    then_projections = ft.module_at(MAIN, "knowledge_projections")
    then_selective = ft.module_at(MAIN, "selective_facts")
    then_loop = ft.module_at(MAIN, "refresh_loop")
    for module in (then_projections, then_selective, then_loop):
        assert "pack_registry" not in module.source_text and "fact_ontology_off" not in module.source_text
    assert not hasattr(then_selective, "enabled_ontologies") and not hasattr(then_selective, "enabled_revision")
    assert hasattr(selective_facts, "enabled_ontologies") and "pack_registry" in open(selective_facts.__file__).read()
    # The share's side is the same on both: the policy an old node installs is the one a new node installs.
    assert then_selective.CATALOG_DIGEST == selective_facts.CATALOG_DIGEST
    assert then_selective.SEMANTICS == selective_facts.SEMANTICS


@pytest.fixture
def as_main(monkeypatch):
    """The fact decision of main, in place of the current one wherever this tree asks for it."""
    then = ft.module_at(MAIN, "knowledge_projections")
    monkeypatch.setattr(knowledge_projections, "fact_projection", then.fact_projection)
    monkeypatch.setattr(fact_preview, "fact_projection", then.fact_projection)
    return then


def _registry(node, sql, *args):
    conn = sqlite3.connect(str(node.corpus.resolver.path), timeout=30)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("registry", ["switched_off", "table_missing"])
def test_an_old_node_releases_the_facts_of_an_ontology_the_owner_switched_off(tmp_path, monkeypatch, as_main,
                                                                              registry, capsys):
    """The share was made while the ontology was on; the owner then switches it off on the Lenses page of an old
    node (or the node never had a registry). Its index is rebuilt, as any later build would. Both facts go on being
    released by every door, and the owner's count says two can be shared."""
    node = serving(tmp_path / "ft168", monkeypatch)
    try:
        if registry == "switched_off":
            assert ft.switch(node, ft.WORK, False)
        else:
            _registry(node, "DROP TABLE pack_registry")
        assert ft.WORK not in ft.switched_on(node)
        assert set(node.rebuild().values()) == {"ready"} and fact_members(node) == 2
        out = released_everywhere(node)
        for door in ("search", "batch", "facts_only"):
            assert out[door]["refused"] is None
            assert sorted(set(out[door]["facts"])) == sorted([CONTROL, STATEMENT]), door
        assert CONTROL in out["answer"]["given"] and STATEMENT in out["answer"]["given"]
        counted, preview = count_and_preview(node)
        assert counted["can_share"] == 2 and all(item["eligible"] for item in preview.values())
        with capsys.disabled():
            print(f"\nFT168 item 5, a node on main, registry {registry}: facts released by search "
                  f"{len(out['search']['facts'])}, by a facts-only share {len(out['facts_only']['facts'])}; given "
                  f"to the answer writer: yes; owner's count can share {counted['can_share']}")
        # The same stores, decided by this tree: nothing.
        monkeypatch.undo()
        monkeypatch.setenv(ft.FACTS_SWITCH, "true")
        now_out = released_everywhere(node)
        for door in ("search", "batch", "facts_only"):
            assert now_out[door]["refused"] is None and now_out[door]["facts"] == [], door
        assert count_and_preview(node)[0]["can_share"] == 0
    finally:
        ft.close(node)


def test_with_the_nodes_facts_switch_off_an_old_node_releases_no_fact(tmp_path, monkeypatch, as_main):
    """The one protection an old node has, and the state of every node today: the facts switch is off."""
    node = serving(tmp_path / "ft168", monkeypatch)
    try:
        assert fact_members(node) == 2
        monkeypatch.setenv(ft.FACTS_SWITCH, "false")
        facts, _others, refused = ft.ask(node, "Northwind work project")
        assert facts == []                                   # refused whole or served without: never a fact
        states = node.rebuild()
        assert set(states.values()) == {"ready"} and fact_members(node) == 0
        facts, others, refused = ft.ask(node, "Northwind work project")
        assert refused is None and facts == [] and others
    finally:
        ft.close(node)


def test_on_an_old_node_a_switch_moves_no_digest_so_nothing_is_built_for_it(tmp_path, monkeypatch):
    then = ft.module_at(MAIN, "refresh_loop")
    node = serving(tmp_path / "ft168", monkeypatch)
    try:
        def digests():
            conn = sqlite3.connect(str(node.corpus.resolver.path))
            try:
                return then.fact_digest(conn), refresh_loop.fact_digest(conn)
            finally:
                conn.close()
        old_on, new_on = digests()
        assert old_on == new_on, "with every admitted ontology on the two digests are one (an upgrade queues nothing)"
        assert ft.switch(node, ft.WORK, False)
        old_off, new_off = digests()
        assert old_off == old_on and new_off != new_on
    finally:
        ft.close(node)
