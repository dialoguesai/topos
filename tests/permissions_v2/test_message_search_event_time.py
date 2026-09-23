"""Event time in search results is a declared part of the owner's consent (design sign-off, 18 Sep).

The locator view releases no time at all, so by default search releases none either:
the policy's search block carries `release_event_time` in {none, day, second}; absent
means none. Under none the field is absent from every record; under day it is truncated
to the UTC day; only under second is it the full value. The request window still filters
on full precision inside the node. Ranking ties never break on more time precision than
the grant releases, so the order of two same-day records says nothing a day value hides.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, pin_record_key
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.registry import parse_policy
from topos.permissions_v2.search_contract import MessageSearchResult


def node_with(tmp_path, precision, *, counts=None, name="n"):
    corpus = mc.build(tmp_path / name / "corpus", seed=71, counts=counts or {"clean_positive_C": 6})
    embed_corpus(corpus)
    raw = mc.search_policy()
    if precision is not None:
        raw["search"]["release_event_time"] = precision
    node = Node(corpus, tmp_path / name, search_raw=raw)
    pin_record_key(node)
    node.rebuild()
    return node


def test_a_grant_without_the_declaration_parses_and_releases_no_time(tmp_path):
    raw = mc.search_policy()
    assert "release_event_time" not in raw["search"]
    assert parse_policy(raw).search.release_event_time == "none"
    node = node_with(tmp_path, None)
    output, refused = node.search_request("roadmap deploy review", k=25)
    assert refused is None and output["records"]
    assert all(set(record) == {"record_id", "source_id", "canonical_table", "content"} for record in output["records"])
    assert "event_at" not in json.dumps(output)


def test_none_declared_explicitly_is_the_same_as_absent(tmp_path):
    left = node_with(tmp_path, None, name="a").search_request("roadmap deploy", k=25)[0]
    right = node_with(tmp_path, "none", name="b").search_request("roadmap deploy", k=25)[0]
    assert left == right


def test_day_truncates_to_the_utc_day(tmp_path):
    node = node_with(tmp_path, "day")
    output, _ = node.search_request("roadmap deploy review", k=25)
    assert output["records"]
    for record in output["records"]:
        assert record["event_at"] % 86_400 == 0


def test_second_releases_the_full_value(tmp_path):
    node = node_with(tmp_path, "second")
    output, _ = node.search_request("roadmap deploy review", k=25)
    assert output["records"] and any(record["event_at"] % 86_400 for record in output["records"])


@pytest.mark.parametrize("value", ["hour", "minute", "", 1, None, "SECOND"])
def test_other_precisions_refuse_at_parse(value):
    raw = mc.search_policy()
    raw["search"]["release_event_time"] = value
    with pytest.raises(PolicyError):
        parse_policy(raw)


def test_the_request_window_still_filters_on_full_precision(tmp_path):
    node = node_with(tmp_path, None)
    everything = node.search_request("roadmap deploy review sprint", k=25)[0]["records"]
    assert everything
    # Half-open window that ends one second after the oldest positive: only it can remain,
    # although no time is released in the answer.
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    oldest = min((unit for unit in node.corpus.units if unit.search_release), key=lambda unit: unit.event_at)
    t = canonical_utc_microseconds(oldest.event_at) // 1_000_000
    output, refused = node.search_request("roadmap deploy review sprint", k=25, window={"after": t, "before": t + 1})
    assert refused is None
    assert [record["content"] for record in output["records"]] in ([oldest.text], [])


def tie_twin(tmp_path, name, hours, precision):
    """Two same-text-shape records on one UTC day; `hours` says which record gets which hour."""
    from tests.permissions_v2.message_search_harness import owner
    from topos.permissions_v2.evidence import ReviewedClassification
    corpus = mc.build(tmp_path / name / "corpus", seed=73, counts={"clean_positive_C": 2})
    units = [unit for unit in corpus.units if unit.search_release]
    with sqlite3.connect(corpus.path) as conn:
        for unit, hour in zip(units, hours):
            conn.execute("UPDATE conversation_messages SET content=?, event_at=? WHERE message_id=?",
                         (f"quasartie roadmap {unit.message_id.replace(':', '')}", f"2027-01-10T{hour}:00:00Z",
                          unit.message_id))
    with owner():
        for index, unit in enumerate(units):
            snapshot = corpus.resolver.inspect_for_review(unit.fact_id)
            corpus.reviews.record_review(resolver=corpus.resolver, review_id=f"tie-{index}", expected_snapshot=snapshot,
                classifications=[ReviewedClassification(evidence=version, domains=["work"], sensitivity="none",
                    subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
                    independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves],
                reviewed_at=mc.NOW - 1)
    embed_corpus(corpus)
    raw = mc.search_policy()
    if precision:
        raw["search"]["release_event_time"] = precision
    node = Node(corpus, tmp_path / name, search_raw=raw)
    pin_record_key(node)
    node.rebuild()
    return node


@pytest.mark.parametrize("precision", [None, "day"])
def test_same_day_ties_never_break_on_hidden_time_of_day(tmp_path, precision):
    """Twins differing only in which record was sent in the morning: identical answers, ties by opaque id."""
    orders = []
    for name, hours in (("am-first", ("08", "20")), ("pm-first", ("20", "08"))):
        output, refused = tie_twin(tmp_path, name, hours, precision).search_request("quasartie", k=25)
        assert refused is None and len(output["records"]) == 2
        orders.append(json.dumps(output, sort_keys=True))
        ids = [record["record_id"] for record in output["records"]]
        assert ids == sorted(ids)
    assert orders[0] == orders[1]


def test_under_second_the_time_of_day_may_order_ties(tmp_path):
    """Control: with second precision released, the later record legitimately ranks first."""
    outputs = [tie_twin(tmp_path, name, hours, "second").search_request("quasartie", k=25)[0]
               for name, hours in (("am-first", ("08", "20")), ("pm-first", ("20", "08")))]
    assert [r["content"] for r in outputs[0]["records"]] != [r["content"] for r in outputs[1]["records"]]


def test_the_view_refuses_mixed_record_shapes():
    base = {"record_id": "r." + "a" * 64, "source_id": "imessage", "canonical_table": "conversation_messages",
            "content": "x"}
    view = {"family": "canonical_record", "operation": "search", "view_id": "canonical.message_search.v1"}
    MessageSearchResult.parse({**view, "records": [base, {**base, "record_id": "r." + "b" * 64}]})
    MessageSearchResult.parse({**view, "records": [{**base, "event_at": 1}]})
    with pytest.raises(PolicyError):
        MessageSearchResult.parse({**view, "records": [base, {**base, "record_id": "r." + "b" * 64, "event_at": 1}]})
