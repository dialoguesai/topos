"""N8 guard: the older ways of reading a Topos stay out of the node.

1.5.0 leaves one recipient read door, knowledge search (p2c-v3) and the answers built on it. Removed with N8: the
three UMA read messages and their HTTP routes, the locator door and the fact door (adapters, transports, handlers,
switches), the shadow audit and the experiments package, the negotiation / minimiser / cohort lane. Nothing here
tests behaviour; it fails when one of those names, files, message types or switches comes back under ``topos/``.

The two door adapters survive as test drivers only (``tests/permissions_v2/retired_doors.py``, which says why).
The last test keeps the product from ever importing them.
"""
from __future__ import annotations

import ast
import io
import json
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "topos"

#: Files and folders that were the removed lanes.
REMOVED_PATHS = (
    "permissions_v2/release_transport.py", "permissions_v2/fact_release_transport.py",
    "permissions_v2/fact_release.py", "permissions_v2/shadow_index.py", "permissions_v2/shadow_rescore.py",
    "permissions_v2/shadow_labelers.py", "permissions_v2/experiments", "core/handlers/shadow_rescore.py",
    "core/handlers/uma.py", "uma_rpt.py", "uma_authority.py", "uma_resource_id.py", "scope_resolution.py",
    "query/minimizer.py", "query/negotiation.py", "query/cohort_resolvers.py", "query/grant_cache.py",
)
#: Message types the node no longer handles.
RETIRED_MESSAGE_TYPES = frozenset({
    "uma_get_messages", "uma_get_rows", "uma_get_oplog",
    "permissions_v2_source_read", "permissions_v2_fact_read", "permissions_v2_shadow_rescore",
})
#: Classes and functions that were the removed doors and their transports.
RETIRED_IDENTIFIERS = frozenset({
    "SourceMessageRelease", "FactProjectionRelease", "SourceMessageIntent", "parse_source_envelope",
    "dispatch_source_message", "dispatch_fact_message", "RETIRED_SOURCE_CAPABILITIES",
})
#: Module names of the removed lanes: no import may name one (a parameter may still be called `uma_resource_id`).
RETIRED_MODULES = frozenset({
    "release_transport", "fact_release_transport", "fact_release", "shadow_index", "shadow_rescore",
    "shadow_labelers", "experiments", "uma_rpt", "uma_authority", "uma_resource_id", "scope_resolution",
    "minimizer", "negotiation", "cohort_resolvers", "grant_cache",
})
#: Switches that only gated the removed doors and the shadow audit.
RETIRED_SWITCHES = frozenset({
    "TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", "TOPOS_PERMISSIONS_V2_FACT_RELEASE_ENABLED",
    "TOPOS_PERMISSIONS_V2_SHADOW_INDEX_ENABLED", "TOPOS_PERMISSIONS_V2_SHADOW_LABELER",
})


def product_files() -> list[Path]:
    files = sorted(path for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts)
    assert len(files) > 300, "the scan did not find the package"
    return files


def code_tokens(path: Path):
    """(names, string literals) of one file: comments and docstring prose never count, code does."""
    names, strings = set(), set()
    for token in tokenize.generate_tokens(io.StringIO(path.read_text("utf-8")).readline):
        if token.type == tokenize.NAME:
            names.add(token.string)
        elif token.type == tokenize.STRING:
            try:
                value = ast.literal_eval(token.string)
            except (SyntaxError, ValueError):
                continue
            if isinstance(value, str):
                strings.add(value)
    return names, strings


def imported_modules(path: Path) -> set[str]:
    """Every dotted part of every module an import statement in this file names, and each name it imports."""
    parts = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            parts.update((node.module or "").split("."))
            parts.update(alias.name for alias in node.names)
    return parts


@pytest.mark.parametrize("relative", REMOVED_PATHS)
def test_a_removed_file_stays_removed(relative):
    path = PACKAGE / relative
    # A folder left behind with nothing but a bytecode cache is not source; any source file in it is.
    assert not path.is_file() and not (path.is_dir() and any(path.rglob("*.py"))), relative


def test_no_retired_message_type_is_handled_or_listed():
    from topos.core.handlers.registry import HANDLERS
    listed = set(json.loads((PACKAGE / "protocol" / "handled_message_types.json").read_text("utf-8"))
                 ["handled_message_types"])
    assert not RETIRED_MESSAGE_TYPES & listed
    assert not RETIRED_MESSAGE_TYPES & set(HANDLERS)
    # The two doors that remain are the ones this release is built on.
    assert {"permissions_v2_message_search", "permissions_v2_answer_submit", "permissions_v2_answer_fetch"} <= listed


def test_no_product_file_names_a_removed_door_or_routes_a_retired_message():
    found = []
    for path in product_files():
        names, strings = code_tokens(path)
        hits = ((names & RETIRED_IDENTIFIERS) | (strings & RETIRED_MESSAGE_TYPES) | (strings & RETIRED_SWITCHES)
                | (imported_modules(path) & RETIRED_MODULES))
        if hits:
            found.append((str(path.relative_to(ROOT)), sorted(hits)))
    assert found == []


def test_no_retired_switch_is_in_the_table():
    from topos.permissions_v2 import switches
    assert not RETIRED_SWITCHES & set(switches.BY_NAME)
    assert not {"SOURCE_RELEASE", "FACT_RELEASE", "SHADOW_INDEX", "SHADOW_LABELER"} & set(vars(switches))


def test_only_the_reviewed_search_profile_can_answer():
    from topos.permissions_v2.search_contract import (CAPABILITY_KNOWLEDGE_SEARCH, RELEASABLE_SEARCH_CAPABILITIES,
        search_capability_document)
    assert RELEASABLE_SEARCH_CAPABILITIES == (CAPABILITY_KNOWLEDGE_SEARCH,) == ("permissions-beta/p2c-v3",)
    assert search_capability_document()["capabilities"] == ["permissions-beta/p2c-v3"]


def test_the_product_never_imports_the_test_only_door_adapters():
    """`tests/permissions_v2/retired_doors.py` drives shared checks from tests; no node code may reach it."""
    offenders = []
    for path in product_files():
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                       else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
            if any(module == "tests" or module.startswith("tests.") for module in modules):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
