"""OD-38 in the grant census: `levers:*entailment*` is the node's own rule with the flag on."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from topos.permissions_v2 import entailment_grounding as eg
from tests.permissions_v2.test_entailment_grounding import (  # noqa: F401 (fixtures)
    FACT_MESSAGE, StubJudge, fact_node, ingest_fixture, owner, paraphrase, run_pass)



def census(node):
    from tests.permissions_v2.test_grant_census import census_of
    return census_of(node)


@pytest.mark.parametrize('paraphrase', [FACT_MESSAGE], indirect=True)
def test_census_counts_the_entailment_lever_with_the_nodes_own_rule(paraphrase, tmp_path, monkeypatch):  # noqa: F811 (fixture)
    gc = importlib.import_module("grant_census")
    node = fact_node(paraphrase, tmp_path, monkeypatch)
    node.rebuild()
    off = census(node).rd11
    # Flag off: the lever is the verbatim upper bound, and the node's own rule releases nothing.
    assert off["entailment_rule"] == "verbatim_upper_bound"
    assert off["facts"]["levers:none"] == 0 and off["facts"]["levers:entailment"] == 1
    monkeypatch.setenv(eg.FLAG, "true")
    node.rebuild()
    pending = census(node).rd11
    # Flag on, no verdict: the lever is the node's rule, so it releases nothing either.
    assert pending["entailment_rule"] == "od38_guards_and_verdicts"
    assert pending["facts"]["levers:entailment"] == 0 and pending["entailment_verdicts"] == {"verdict:absent": 1}
    assert pending["entailment_guard_codes"] == {"fact_verbatim:pass": 1}
    run_pass(node, StubJudge())
    on = census(node)
    assert gc.compare_index(on)["sets_equal"]
    # `levers:none` is fullmatch alone; with the flag on the node's own rule is the `entailment` column,
    # and the funnel's grounded gate is the node's rule as it runs.
    assert on.rd11["facts"]["levers:none"] == 0 and on.rd11["facts"]["levers:entailment"] == 1
    assert on.rd11["facts"]["funnel_grounded"] == 1 and on.rd11["entailment_verdicts"] == {"verdict:entailed": 1}
    assert sorted(o.family for o in on.members.values()) == ["fact", "message"]


@pytest.mark.parametrize('paraphrase', ["My sister has been working at Northwind since the spring."], indirect=True)
def test_census_reports_why_od38_withholds_without_asking_anyone(paraphrase, tmp_path, monkeypatch):  # noqa: F811 (fixture)
    node = fact_node(paraphrase, tmp_path, monkeypatch)
    monkeypatch.setenv(eg.FLAG, "true")
    node.rebuild()
    rd11 = census(node).rd11
    assert rd11["entailment_guard_codes"] == {"fact_verbatim:entailment_third_party": 1}
    assert rd11["entailment_verdicts"] == {} and rd11["facts"]["levers:provenance+entailment"] == 0


@pytest.mark.parametrize('paraphrase', [FACT_MESSAGE], indirect=True)
def test_census_judge_answers_stay_in_memory(paraphrase, tmp_path, monkeypatch):  # noqa: F811 (fixture)
    gc = importlib.import_module("grant_census")
    node = fact_node(paraphrase, tmp_path, monkeypatch)
    monkeypatch.setenv(eg.FLAG, "true")
    node.rebuild()
    judge = StubJudge()
    monkeypatch.setattr(eg, "LocalEntailmentJudge", lambda: judge)
    resolver = node.index.resolver
    result = gc.run(canonical=Path(resolver.path), reviews=Path(node.index.reviews.path), ledger=node.ledger.path,
                    index_root=Path(resolver.path).parent / "permissions-v2" / "message-search",
                    keys=Path(resolver.path).parent / "permissions-v2" / "message-search" / "keys.db",
                    binding=resolver.binding, live_canonical=None, now=node.now[0], entailment_judge=True)
    assert result.rd11["entailment_verdicts"] == {"verdict:entailed": 1} and judge.asked == 1
    assert result.rd11["facts"]["levers:entailment"] == 1 and result.rd11["entailment_judge"] == "verified"
    assert not eg.store_path_for(resolver).exists()
