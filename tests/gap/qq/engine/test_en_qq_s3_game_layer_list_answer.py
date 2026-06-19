"""GT-EN-QQ-S3-05: Game layer list answer type for who/people queries."""

import pytest

from topos.query.game_layer import DefaultGameLayer

pytestmark = pytest.mark.gap


def test_relationship_who_query_returns_list_answer_type() -> None:
    layer = DefaultGameLayer()
    result = layer.apply(
        context_packet={
            "graph": {"nodes": [{"node_id": "n1", "label": "Alice"}, {"node_id": "n2", "label": "Bob"}]},
            "scores": [],
        },
        access_mode="inference",
        scope_id="relationship_context:read",
        query_text="who do I collaborate with",
    )
    payload = result.to_dict()
    assert payload.get("answer_type") == "list"
    assert "Alice" in payload.get("items", [])
    assert "content" not in payload
