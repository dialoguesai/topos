"""The census's per-share mode (any-to-any T3): one node holds a share for each of two people, and the census of
each is the census of that share alone. Before, the census refused any node with two active knowledge-search grants
(``exactly_one_active_knowledge_search_grant_required``), which is every owner node in a three-owner rig."""
from __future__ import annotations

import copy
import os
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_grant_census import built, cs, gc
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.permissions_v2.search_index import root_for


def _second_share(node, grant="grant-search-b", actor="actor-2"):
    raw = copy.deepcopy(node.search_raw)
    raw["binding"].update(grant_id=grant, assignment_id=f"assignment-{grant}", actor_id=actor)
    raw["policy_version_id"] = f"policy-{grant}"
    node.activate(raw)
    with owner():
        assert node.index.rebuild(grant, now=node.now[0])["state"] == "ready"
    return grant


def _census(node, grant_id=None):
    resolver = node.index.resolver
    durable = root_for(resolver.path).parent
    return gc.run(canonical=resolver.path, reviews=durable / os.path.basename(node.index.reviews.path),
                  ledger=node.ledger.path, index_root=durable / "message-search",
                  keys=durable / "message-search" / "keys.db", binding=resolver.binding, live_canonical=None,
                  now=node.now[0], grant_id=grant_id)


def test_two_active_shares_need_a_named_one_and_each_census_is_its_own(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    second = _second_share(node)
    with pytest.raises(cs.CensusRefused) as refused:
        _census(node)
    assert str(refused.value) == "exactly_one_active_knowledge_search_grant_required"
    first = _census(node, "grant-search")
    other = _census(node, second)
    assert (first.grant_id, other.grant_id) == ("grant-search", second)
    for census, grant in ((first, "grant-search"), (other, second)):
        comparison = gc.compare_index(census)
        assert comparison["sets_equal"] and comparison["census_members"] == comparison["live_members"] == 1, grant
    # The same message under two shares has two opaque ids: each census keys its members by its own grant.
    assert set(first.members) != set(other.members)
    with pytest.raises(cs.CensusRefused) as refused:
        _census(node, "no-such-grant")
    assert str(refused.value) == "named_grant_not_active_knowledge_search"


def test_the_grant_is_named_by_flag_or_private_file_and_never_two_different_ways(tmp_path):
    named = tmp_path / "grant-id"
    named.write_text("grant-search\n")
    os.chmod(named, 0o600)
    args = SimpleNamespace(grant=None, grant_id_file=None)
    assert gc.named_grant(args) is None
    assert gc.named_grant(SimpleNamespace(grant="grant-x", grant_id_file=None)) == "grant-x"
    assert gc.named_grant(SimpleNamespace(grant=None, grant_id_file=named)) == "grant-search"
    assert gc.named_grant(SimpleNamespace(grant="grant-search", grant_id_file=named)) == "grant-search"
    with pytest.raises(cs.CensusRefused) as refused:
        gc.named_grant(SimpleNamespace(grant="grant-x", grant_id_file=named))
    assert str(refused.value) == "two_grants_named"
    os.chmod(named, 0o644)
    with pytest.raises(cs.CensusRefused) as refused:
        gc.named_grant(SimpleNamespace(grant=None, grant_id_file=named))
    assert str(refused.value) == "grant_id_file_must_be_private"


def test_the_flag_reaches_the_census_from_the_command_line(legacy, tmp_path, monkeypatch, capsys):
    import json

    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    second = _second_share(node)
    index_root = root_for(node.index.resolver.path)
    for grant in ("grant-search", second):
        aggregate = gc.aggregate(_census(node, grant), run_at="t")
        assert gc.main(["--index-revision", "--source-root", str(index_root.parent.parent), "--grant", grant]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["index_revision"] == aggregate["index_revision"] is not None
        assert printed["live_index_members"] == 1 and grant not in json.dumps(printed)
