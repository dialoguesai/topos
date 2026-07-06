"""Guards for the handled-message-types protocol snapshot.

The snapshot (topos/protocol/handled_message_types.json) is the machine-readable
list of control-plane message types this engine dispatches. Two invariants:

1. Freshness — the committed snapshot equals the runtime HANDLERS registry.
   Adding or removing an @handles registration requires regenerating it:
   uv run python scripts/dump_handled_types.py > topos/protocol/handled_message_types.json

2. Static parity — the set of string literals in @handles(...) decorators equals
   the runtime registry, so no handler is registered dynamically in a way that
   static consumers of the snapshot (or this repo's source) would miss.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO_ROOT / "topos" / "protocol" / "handled_message_types.json"


def _runtime_handled() -> set[str]:
    from topos.core.handlers import HANDLERS

    return set(HANDLERS)


def _static_handled() -> set[str]:
    found: set[str] = set()
    for py in sorted((REPO_ROOT / "topos").rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "handles":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
    return found


def test_snapshot_matches_runtime_registry():
    snapshot = set(json.loads(SNAPSHOT.read_text(encoding="utf-8"))["handled_message_types"])
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
