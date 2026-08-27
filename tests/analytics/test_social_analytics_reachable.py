"""The social reads have to be reachable BY NAME from `get_analytics`.

They were served at `/v1/messenger-analytics/*` and as `messenger_*` websocket types from
the day they were written, and only the app's Social page could call them. An agent's
`get_analytics` knew 21 query names and not one was a relationship, a role or a person — so
`R0·BENCH`, a routine whose entire job is to produce the bench, re-inferred roles from raw
text with an LLM while the derivation built for that exact question sat one call away.

These tests pin the reachability, not the numbers.
"""

from __future__ import annotations

import json

from topos.core.handlers.messages import (
    SOCIAL_ANALYTICS_QUERIES,
    SUPPORTED_ANALYTICS_QUERIES,
    _agent_sized_person_graph,
    _PERSON_FIELDS,
)


def test_the_social_reads_are_named_queries():
    for name in ("social_bench", "social_graph", "luck_surface"):
        assert name in SOCIAL_ANALYTICS_QUERIES
        assert name in SUPPORTED_ANALYTICS_QUERIES


def test_the_supported_set_covers_the_queries_the_handler_answers():
    """The error message lists these, so a name missing here is a name nobody discovers."""
    import inspect

    from topos.core.handlers import messages as mod

    source = inspect.getsource(mod.handle_get_analytics)
    answered = set()
    for line in source.splitlines():
        line = line.strip()
        for prefix in ('if query == "', 'elif query == "'):
            if line.startswith(prefix):
                answered.add(line[len(prefix):].split('"')[0])
    missing = answered - SUPPORTED_ANALYTICS_QUERIES
    assert not missing, f"answered but undiscoverable: {sorted(missing)}"


def test_an_unknown_query_is_an_error_not_a_confident_nothing():
    """It used to return `status: ok` with an empty list. A caller who guessed a name got a
    clean nothing, indistinguishable from a real empty answer — which is exactly the shape
    the routines' own absence rule forbids: never let a lookup failure become a finding."""
    import inspect

    from topos.core.handlers import messages as mod

    source = inspect.getsource(mod.handle_get_analytics)
    tail = source[source.index("Unknown query"):]
    assert '"status": "error"' in tail, "an unknown name must fail loudly"
    assert "SUPPORTED_ANALYTICS_QUERIES" in tail, "and say what does exist"
    assert '"result": []' not in tail, "the silent empty is what made this undebuggable"


def test_the_person_graph_is_projected_never_truncated():
    """356KB of identity plumbing does not fit a context window. Dropping FIELDS keeps
    every person; dropping ROWS would let a cap be read as 'these are the people'."""
    graph = {
        "nodes": [{"node_id": "owner", "label": "You", "is_owner": True}]
        + [{"node_id": f"p{i}", "label": f"Person {i}", "band": "core",
            "closeness": i / 100, "messenger_keys": ["+1555" + str(i)],
            "evidence": {"messaged": True}, "closeness_reason": "x" * 400}
           for i in range(300)],
        "counts": {"total": 301}, "bands": {"core": 300},
    }
    out = _agent_sized_person_graph(graph)
    assert len(out["people"]) == 300, "every person, minus the owner"
    assert "300" in out["completeness"], "the count is stated, so it can be checked"
    assert set(out["people"][0]) == set(_PERSON_FIELDS), "fields are the thing reduced"
    assert out["people"][0]["closeness"] >= out["people"][-1]["closeness"], "closest first"
    assert len(json.dumps(out)) < len(json.dumps(graph)) / 3, "and it is much smaller"
