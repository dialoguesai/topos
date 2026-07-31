"""Guards for the handled-message-types protocol snapshot.

The snapshot (topos/protocol/handled_message_types.json) is the machine-readable
list of control-plane message types this engine dispatches. Two invariants:

1. Freshness — the committed snapshot equals the runtime HANDLERS registry.
   Adding or removing an @handles registration requires regenerating it:
   uv run python scripts/dump_handled_types.py > topos/protocol/handled_message_types.json

2. Static parity — the set of string literals in @handles(...) decorators equals
   the runtime registry, so no handler is registered dynamically in a way that
   static consumers of the snapshot (or this repo's source) would miss.

The snapshot's second key, ``owner_only_message_types``, carries the subset
registered with ``@handles(..., owner_only=True)``. Those are types the engine
answers for the node's owner but which must never be reachable from an agent or
cross-user surface; the tests below are the leak gates that enforce it.

What "not reachable" is measured against
----------------------------------------
The control plane's ``/mcp`` gateway (``control_plane/mcp_gateway.py``) is the
only place the CP forwards an engine message on behalf of an agent or a UMA
grantee, so it is the forwarding set these gates assert against: both the tool
names it publishes and the message types those tools forward.

``control_plane/engine_protocol.py::SENT_MESSAGE_TYPES`` is deliberately *not*
the gate. It declares what the owner's own authenticated dashboard routes send
to the owner's own node — ``delete_database_table`` is in it precisely because
the owner's data explorer exists. Owner-only means owner-only, not unreachable.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO_ROOT / "topos" / "protocol" / "handled_message_types.json"

MCP_GATEWAY_RELPATH = Path("control_plane") / "mcp_gateway.py"


def _runtime_handled() -> set[str]:
    from topos.core.handlers import HANDLERS

    return set(HANDLERS)


def _runtime_owner_only() -> set[str]:
    from topos.core.handlers import OWNER_ONLY_MESSAGE_TYPES

    return set(OWNER_ONLY_MESSAGE_TYPES)


def _handles_calls() -> list[ast.Call]:
    calls: list[ast.Call] = []
    for py in sorted((REPO_ROOT / "topos").rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "handles":
                calls.append(node)
    return calls


def _literal_str_args(call: ast.Call) -> set[str]:
    return {
        arg.value
        for arg in call.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }


def _static_handled() -> set[str]:
    found: set[str] = set()
    for call in _handles_calls():
        found |= _literal_str_args(call)
    return found


def _static_owner_only() -> set[str]:
    found: set[str] = set()
    for call in _handles_calls():
        marked = any(
            kw.arg == "owner_only"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in call.keywords
        )
        if marked:
            found |= _literal_str_args(call)
    return found


def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def _mcp_gateway_tree() -> ast.AST:
    """The CP's /mcp gateway module, or skip if the CP repo isn't checked out.

    Mirrors the CP's own engine lookup in its tests/contract/test_engine_protocol.py.
    """
    env_path = os.environ.get("CONTROL_PLANE_REPO_PATH", "").strip()
    roots = [Path(env_path)] if env_path else [REPO_ROOT.parent / "topos-control-plane"]
    for root in roots:
        gateway = root / MCP_GATEWAY_RELPATH
        if gateway.is_file():
            return ast.parse(gateway.read_text(encoding="utf-8"))
    pytest.skip(
        "control-plane repo not found; set CONTROL_PLANE_REPO_PATH to run the "
        "owner-only /mcp leak gates (CI must provide it)"
    )


def _mcp_tool_names(tree: ast.AST) -> set[str]:
    """Names published as /mcp tools: functions decorated with @fastmcp.tool()."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(func, ast.Attribute) and func.attr == "tool":
                names.add(node.name)
    return names


def _mcp_forwarded_message_types(tree: ast.AST) -> set[str]:
    """Engine message types the /mcp gateway forwards (_forward/_forward_owner_scoped)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if not name.startswith("_forward"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.add(first.value)
    return found


def _mcp_string_literals(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_snapshot_matches_runtime_registry():
    snapshot = set(_snapshot()["handled_message_types"])
    runtime = _runtime_handled()
    missing = sorted(runtime - snapshot)
    stale = sorted(snapshot - runtime)
    assert not missing and not stale, (
        "topos/protocol/handled_message_types.json is stale "
        f"(missing={missing}, stale={stale}). Regenerate with: "
        "uv run python scripts/dump_handled_types.py > topos/protocol/handled_message_types.json"
    )


def test_static_handles_literals_match_runtime_registry():
    runtime = _runtime_handled()
    static = _static_handled()
    dynamic_only = sorted(runtime - static)
    unregistered = sorted(static - runtime)
    assert not dynamic_only, (
        "Handlers registered without a literal @handles(...) decorator; static "
        f"consumers of the registry cannot see them: {dynamic_only}"
    )
    assert not unregistered, (
        f"@handles(...) literals that never reach the runtime registry: {unregistered}"
    )


def test_owner_only_snapshot_matches_runtime_registry():
    snapshot = set(_snapshot()["owner_only_message_types"])
    runtime = _runtime_owner_only()
    missing = sorted(runtime - snapshot)
    stale = sorted(snapshot - runtime)
    assert not missing and not stale, (
        "owner_only_message_types in topos/protocol/handled_message_types.json is "
        f"stale (missing={missing}, stale={stale}). Regenerate with: "
        "uv run python scripts/dump_handled_types.py > topos/protocol/handled_message_types.json"
    )


def test_owner_only_is_a_subset_of_handled():
    snapshot = _snapshot()
    orphans = sorted(
        set(snapshot["owner_only_message_types"]) - set(snapshot["handled_message_types"])
    )
    assert not orphans, f"owner_only types with no registered handler: {orphans}"


def test_static_owner_only_markers_match_runtime_registry():
    runtime = _runtime_owner_only()
    static = _static_owner_only()
    dynamic_only = sorted(runtime - static)
    unregistered = sorted(static - runtime)
    assert not dynamic_only, (
        "Types marked owner-only at runtime with no literal "
        f"@handles(..., owner_only=True) decorator: {dynamic_only}"
    )
    assert not unregistered, (
        "@handles(..., owner_only=True) literals that never reach the runtime "
        f"registry: {unregistered}"
    )


def test_destructive_database_explorer_types_are_owner_only():
    """These predate the marker and are why it exists; they may not be unmarked."""
    runtime = _runtime_owner_only()
    expected = {
        "get_database_explorer_summary",
        "delete_database_rows",
        "delete_database_table",
    }
    assert expected <= runtime, f"no longer marked owner-only: {sorted(expected - runtime)}"


def test_graph_cypher_query_is_owner_only_and_off_every_agent_surface():
    """The §4.1 gate, hard 0.

    An arbitrary traversal punches straight through row-level redaction:
    ``MATCH (a)-[*1..3]->(b)`` does not respect a scope unless the subgraph was
    scoped BEFORE the query ran, and nothing scopes it today. Until a
    scoped-subgraph materialisation story exists, openCypher is owner-only.
    """
    assert "graph_cypher_query" in _runtime_owner_only(), (
        "graph_cypher_query must be registered @handles(..., owner_only=True)"
    )
    assert "graph_cypher_query" in set(_snapshot()["owner_only_message_types"])

    tree = _mcp_gateway_tree()
    assert "graph_cypher_query" not in _mcp_forwarded_message_types(tree), (
        "graph_cypher_query is forwarded by the control plane's /mcp gateway"
    )
    assert "graph_cypher_query" not in _mcp_tool_names(tree), (
        "graph_cypher_query is published as a /mcp tool"
    )
    assert "graph_cypher_query" not in _mcp_string_literals(tree), (
        "graph_cypher_query is named in control_plane/mcp_gateway.py"
    )


def test_owner_only_types_are_absent_from_the_local_mcp_tool_list():
    """The engine's own stdio MCP proxy is an agent surface too.

    ``_mcp_gateway_tree`` skips when the control-plane repo is not checked out;
    this list ships in this repo, so this half of the gate can never be skipped.
    """
    owner_only = _runtime_owner_only()
    assert owner_only, "owner-only marker registry is empty; the gate would be vacuous"

    proxy = REPO_ROOT / "topos" / "mcp_stdio_proxy.py"
    tree = ast.parse(proxy.read_text(encoding="utf-8"))

    published = sorted(owner_only & _mcp_tool_names(tree))
    assert not published, f"owner-only types published as local /mcp tools: {published}"

    mentioned = sorted(owner_only & _mcp_string_literals(tree))
    assert not mentioned, f"owner-only types named in {proxy.name}: {mentioned}"


def test_owner_only_types_are_absent_from_the_mcp_surface():
    """Hard 0: no owner-only type may be reachable through the /mcp gateway."""
    owner_only = _runtime_owner_only()
    assert owner_only, "owner-only marker registry is empty; the gate would be vacuous"

    tree = _mcp_gateway_tree()

    forwarded = sorted(owner_only & _mcp_forwarded_message_types(tree))
    assert not forwarded, f"owner-only message types forwarded by the /mcp gateway: {forwarded}"

    published = sorted(owner_only & _mcp_tool_names(tree))
    assert not published, f"owner-only message types published as /mcp tool names: {published}"

    # Belt and braces: the forward helpers can be renamed or wrapped, so also fail
    # on an owner-only type name appearing anywhere in the gateway module at all.
    mentioned = sorted(owner_only & _mcp_string_literals(tree))
    assert not mentioned, (
        f"owner-only message types named in control_plane/mcp_gateway.py: {mentioned}"
    )
