"""Server-derived routine actors pass exactly the normal node grant rules."""
from copy import deepcopy

import pytest

from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.message_search_harness import owner

ACTOR = "routine-" + "a" * 64
GRANT = "routine-grant-test"


def prepare(legacy, tmp_path, monkeypatch, **changes):
    node, _ = node_for(legacy, tmp_path, monkeypatch, **changes)
    raw = deepcopy(node.search_raw)
    raw["binding"].update(actor_id=ACTOR, grant_id=GRANT, assignment_id="routine-assignment-test")
    raw["policy_version_id"] = "routine-policy-test"
    raw["read_budget_per_day"] = 1
    node.activate(raw)
    with owner():
        node.index.rebuild(GRANT, now=node.now[0])
    return node


def test_routine_actor_gets_only_grant_eligible_records_and_obeys_daily_limit(legacy, tmp_path, monkeypatch):
    node = prepare(legacy, tmp_path, monkeypatch)
    output, refusal = node.search_request("Synthetic message", k=10, grant_id=GRANT, actor=ACTOR)
    assert refusal is None
    assert len(output["records"]) == 1
    assert output["records"][0]["content"] == "I am working on Synthetic message at work."
    _, refusal = node.search_request("Synthetic message", k=10, grant_id=GRANT, actor=ACTOR)
    assert refusal is not None


@pytest.mark.parametrize("labels", [{"domains": ["work", "health"]}, {"sensitivity": "special"}, {"protected_content": "unknown"}])
def test_routine_actor_never_bypasses_topic_sensitivity_or_protection_checks(legacy, tmp_path, monkeypatch, labels):
    node = prepare(legacy, tmp_path, monkeypatch, labels=labels)
    output, refusal = node.search_request("Synthetic message", k=10, grant_id=GRANT, actor=ACTOR)
    assert refusal is not None or output["records"] == []


def test_subscriber_or_owner_cannot_query_with_routine_identity(legacy, tmp_path, monkeypatch):
    node = prepare(legacy, tmp_path, monkeypatch)
    for actor in ("owner-1", "subscriber", "actor-1"):
        _, refusal = node.search_request("Synthetic message", k=10, grant_id=GRANT, actor=actor)
        assert refusal is not None
