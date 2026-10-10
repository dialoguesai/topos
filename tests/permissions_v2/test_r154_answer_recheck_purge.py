"""R-N154 L1: the answer fetch's re-check removes an index it finds failed, as the search paths do.

Before, `retrieve_for_answer` refused `search_index_stale` and left the failed file in place until the next sweep
(about 10 s), where a search's gated recheck purges it at once. The fetch still holds the body (`pending`) or refuses
exactly as before; only the file's removal moves earlier.

Invented evidence only (the BL-159 journal fixture).
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_bl159_answer_delivery import (  # noqa: F401 -- the fixtures
    Refusals, _restrict, answers_node, fetched, first_day, journal_database, service, written,
)
from topos.permissions_v2.search_index import index_path

pytestmark = pytest.mark.public


@pytest.mark.parametrize("restriction", ["assessment", "opt_out", "content"])
def test_a_failed_answer_recheck_removes_the_index_at_once(answers_node, service, monkeypatch, restriction):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    index = index_path(node.index.root, "grant-search")
    assert index.exists()
    _restrict(node, restriction)
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is not None or body.get("state") == "pending", f"delivered after {restriction}"
    assert not index.exists(), "the failed index stayed until a sweep"
