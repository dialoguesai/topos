"""Tool-RAG substrate (PLAN_AGENT_QUALITY WS-3/M2): index + retrieve over a
dedicated tool_index table, with an injected embedder — no model downloads."""

from __future__ import annotations

import sqlite3
from typing import Dict, List

import pytest

from topos.features.signal.tool_index import (
    CORE_TOOL_NAMES,
    connector_of,
    embed_text_for_tool,
    index_status,
    index_tools,
    retrieve_tools,
)

# Deterministic 4-dim unit vectors per keyword theme. "commit"-themed texts sit
# on axis 0, "issue" on axis 1, "profile" on axis 2, everything else on axis 3 —
# cosine ranking then follows themes exactly.
_AXES: Dict[str, int] = {"commit": 0, "issue": 1, "profile": 2}


def _fake_vector(text: str) -> List[float]:
    vec = [0.0, 0.0, 0.0, 0.0]
    lowered = text.lower()
    for token, axis in _AXES.items():
        if token in lowered:
            vec[axis] = 1.0
    if not any(vec):
        vec[3] = 1.0
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]


def fake_embed(texts: List[str], input_role: str) -> List[List[float]]:
    assert input_role in ("passage", "query")
    return [_fake_vector(t) for t in texts]


TOOLS = [
    {"name": "remote__topos-github__list_commits", "description": "List commits of a branch in a repository"},
    {"name": "remote__topos-github__get_commit", "description": "Get details for a single commit"},
    {"name": "remote__topos-github__list_issues", "description": "List issues in a repository"},
    {"name": "remote__topos-github__get_me", "description": "Get my GitHub user profile"},
    {"name": "remote__topos-notion__search", "description": "Search pages in the workspace"},
    {"name": "query_scope", "description": "Query synced personal data by scope"},
]


@pytest.fixture()
def conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def test_connector_of_and_embed_text():
    assert connector_of("remote__topos-github__list_commits") == "topos-github"
    assert connector_of("query_scope") == ""
    assert connector_of("query_scope", explicit="custom") == "custom"
    text = embed_text_for_tool(
        "remote__topos-github__list_commits", "List commits of a branch", "topos-github"
    )
    assert text == "topos-github list_commits: List commits of a branch"


def test_index_upserts_then_reindex_is_noop(conn):
    first = index_tools(conn, TOOLS, embed_fn=fake_embed)
    assert first["indexed"] == len(TOOLS)
    assert first["unchanged"] == 0
    assert first["total"] == len(TOOLS)

    again = index_tools(conn, TOOLS, embed_fn=fake_embed)
    assert again["indexed"] == 0
    assert again["unchanged"] == len(TOOLS)
    assert again["total"] == len(TOOLS)


def test_index_prune_missing_removes_stale_rows(conn):
    index_tools(conn, TOOLS, embed_fn=fake_embed)
    kept = TOOLS[:3]
    result = index_tools(conn, kept, embed_fn=fake_embed, prune_missing=True)
    assert result["pruned"] == len(TOOLS) - len(kept)
    assert result["total"] == len(kept)


def test_retrieve_ranks_commit_tools_for_commit_query(conn):
    index_tools(conn, TOOLS, embed_fn=fake_embed)
    out = retrieve_tools(conn, "what were my most recent commits", k=3, embed_fn=fake_embed)
    assert out["backend"] == "cosine"
    names = [t["name"] for t in out["tools"]]
    # Both commit-themed tools outrank everything else.
    assert set(names[:2]) == {
        "remote__topos-github__list_commits",
        "remote__topos-github__get_commit",
    }
    assert out["tools"][0]["similarity"] > out["tools"][-1]["similarity"] or len(names) <= 2


def test_retrieve_scopes_to_connector(conn):
    index_tools(conn, TOOLS, embed_fn=fake_embed)
    out = retrieve_tools(
        conn, "search my notion pages", connector_scope="topos-notion", k=5, embed_fn=fake_embed
    )
    assert [t["name"] for t in out["tools"]] == ["remote__topos-notion__search"]
    assert out["indexed_count"] == 1  # scope filter applied before ranking


def test_core_set_always_present(conn):
    index_tools(conn, TOOLS, embed_fn=fake_embed)
    out = retrieve_tools(conn, "anything at all", k=1, embed_fn=fake_embed)
    assert out["core"] == list(CORE_TOOL_NAMES)
    empty_conn = sqlite3.connect(":memory:")
    out_empty = retrieve_tools(empty_conn, "anything", embed_fn=fake_embed)
    assert out_empty["tools"] == []
    assert out_empty["core"] == list(CORE_TOOL_NAMES)


def test_keyword_fallback_when_query_embedding_unavailable(conn):
    index_tools(conn, TOOLS, embed_fn=fake_embed)

    def broken_query_embed(texts: List[str], input_role: str) -> List[List[float]]:
        if input_role == "query":
            return []
        return fake_embed(texts, input_role)

    out = retrieve_tools(
        conn, "list commits from the repository", k=3, embed_fn=broken_query_embed
    )
    assert out["backend"] == "keyword_fallback"
    assert out["tools"][0]["name"] == "remote__topos-github__list_commits"
    assert out["tools"][0]["similarity"] > 0


def test_stale_dims_rows_fall_back_to_keyword(conn):
    # Index with 4-dim vectors, then query with a 3-dim embedder (model swap):
    # every stored row is skipped on dims mismatch → keyword fallback, not empty.
    index_tools(conn, TOOLS, embed_fn=fake_embed)

    def three_dim_embed(texts: List[str], input_role: str) -> List[List[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    out = retrieve_tools(conn, "list commits in repository", k=3, embed_fn=three_dim_embed)
    assert out["backend"] == "keyword_fallback"
    assert out["tools"][0]["name"] == "remote__topos-github__list_commits"


def test_index_status_reports_counts(conn):
    index_tools(conn, TOOLS, embed_fn=fake_embed)
    status = index_status(conn)
    assert status["total"] == len(TOOLS)
    assert status["by_connector"]["topos-github"] == 4
    assert status["by_connector"]["topos-notion"] == 1
    assert status["by_connector"][""] == 1  # query_scope (base tool)
    assert status["updated_at"]
