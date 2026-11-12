"""Work hopped off the event loop resolves ITS OWN database connection.

A ``sqlite3.Connection`` holds one transaction state, one statement cache and one
last-error code; the per-thread design in ``topos.core.state.get_db_connection``
exists so no two threads share one. On 2026-11-04/05 the query pipeline broke
that rule (tests/query/test_query_offload_event_loop.py), and a sweep on 5 Nov
found the same shape at 24 more ``asyncio.to_thread`` sites: a handle resolved
on the loop — ``get_db_connection()``, ``hub.get_db_connection()``, ``_conn()``,
``_entities_conn()`` — passed as an argument (evaluated before the hop) or
captured by the closure the worker runs.

Two guards. A static one reads every ``to_thread`` / ``run_in_executor`` call in
the package and fails on a hand-off of that shape. A behavioural one drives
representative sites against a file-backed owner connection on the test thread
and checks which handle the worker was given.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import topos

PACKAGE = Path(topos.__file__).parent
RESOLVERS = {"get_db_connection", "_conn", "_entities_conn", "conn_factory"}


# --- the static guard ---------------------------------------------------------


def _is_resolver_call(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        return name in RESOLVERS
    if isinstance(node, ast.IfExp):
        return _is_resolver_call(node.body) or _is_resolver_call(node.orelse)
    return False


def _names_bound_inside(fn: ast.AST) -> set[str]:
    bound = set()
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        bound.update(a.arg for a in fn.args.args + fn.args.kwonlyargs)
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            bound.add(n.id)
    return bound


def _names_used(fn: ast.AST) -> set[str]:
    body = fn.body if isinstance(fn, ast.Lambda) else fn
    return {n.id for n in ast.walk(body) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def loop_handle_hand_offs(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, why)`` for each hop that carries a loop-resolved handle.

    Looks at the enclosing function only: names it binds from a resolver at its
    own level (not inside nested defs), passed to the hop directly or used by a
    local closure/lambda the hop runs — minus names the closure binds itself.
    A hop of a bound method or of a function taking a ``conn`` parameter is not
    judged here; the behavioural tests and review cover those.
    """
    hits: list[tuple[int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nested = [n for n in ast.walk(func) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n is not func]
        inside_nested = {id(x) for n in nested for x in ast.walk(n)}
        local_defs = {n.name: n for n in nested if hasattr(n, "name")}
        conn_names = set()
        for n in ast.walk(func):
            if id(n) in inside_nested or not isinstance(n, ast.Assign) or not _is_resolver_call(n.value):
                continue
            conn_names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        for call in ast.walk(func):
            if id(call) in inside_nested or not isinstance(call, ast.Call):
                continue
            f = call.func
            hop = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if hop not in {"to_thread", "run_in_executor"}:
                continue
            args = list(call.args) + [k.value for k in call.keywords]
            if hop == "run_in_executor":
                args = args[1:]
            for a in args:
                if _is_resolver_call(a):
                    hits.append((call.lineno, f"argument resolves the handle on the loop: {ast.unparse(a)}"))
                elif isinstance(a, ast.Name) and a.id in conn_names:
                    hits.append((call.lineno, f"argument {a.id} is the loop's handle"))
                elif isinstance(a, ast.Name) and a.id in local_defs:
                    leaked = (_names_used(local_defs[a.id]) - _names_bound_inside(local_defs[a.id])) & conn_names
                    if leaked:
                        hits.append((call.lineno, f"closure {a.id} uses the loop's {sorted(leaked)[0]}"))
                elif isinstance(a, ast.Lambda):
                    leaked = (_names_used(a) - _names_bound_inside(a)) & conn_names
                    if leaked:
                        hits.append((call.lineno, f"lambda uses the loop's {sorted(leaked)[0]}"))
    return hits


def test_the_checker_sees_each_shape_of_hand_off():
    """Positive and negative controls, so a silent checker cannot pass the sweep."""
    bad = ast.parse(
        "async def a():\n"
        "    conn = get_db_connection()\n"
        "    await asyncio.to_thread(work, conn)\n"
        "async def b():\n"
        "    await asyncio.to_thread(work, _entities_conn())\n"
        "async def c():\n"
        "    conn = hub.get_db_connection()\n"
        "    def run():\n"
        "        return work(conn)\n"
        "    await asyncio.to_thread(run)\n"
        "async def d():\n"
        "    conn = _conn()\n"
        "    await asyncio.to_thread(lambda: work(conn))\n"
    )
    assert [line for line, _ in loop_handle_hand_offs(bad)] == [3, 5, 10, 13]
    good = ast.parse(
        "async def a():\n"
        "    if get_db_connection() is None:\n"
        "        return None\n"
        "    await asyncio.to_thread(lambda: work(get_db_connection()))\n"
        "async def b():\n"
        "    conn = get_db_connection()\n"
        "    def run():\n"
        "        conn = get_db_connection()\n"
        "        return work(conn)\n"
        "    await asyncio.to_thread(run)\n"
    )
    assert loop_handle_hand_offs(good) == []


def test_no_hop_in_the_package_carries_the_loops_handle():
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(errors="replace"))
        for lineno, why in loop_handle_hand_offs(tree):
            hits.append(f"{path.relative_to(PACKAGE)}:{lineno}  {why}")
    assert hits == [], "a worker was handed the event loop's connection:\n" + "\n".join(hits)


# --- the behavioural guard ----------------------------------------------------


def _owner_connection_on_this_thread(tmp_path, monkeypatch):
    from topos.core import state

    db = tmp_path / "engine.db"
    monkeypatch.setattr(state, "db_conn", None, raising=False)
    monkeypatch.setattr(state, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(state, "_conn_owner_thread", None, raising=False)
    monkeypatch.setattr(state, "_db_conn_described", None, raising=False)
    monkeypatch.setattr(state, "_thread_state", threading.local(), raising=False)
    monkeypatch.setattr(state, "_resolve_database_path_from_settings", lambda: db, raising=False)
    owner = state.get_db_connection()
    if owner is None:
        pytest.skip("database could not be opened in this environment")
    return owner


class _Recorder:
    """Stands in for the hopped function; records which handle it was given."""

    def __init__(self, owner):
        self.owner = owner
        self.calls: list[dict] = []

    def __call__(self, conn, *args, **kwargs):
        from topos.core import state

        self.calls.append(
            {
                "on_owner": conn is self.owner,
                "own": conn is not None and conn is state.get_db_connection(),
                "thread": threading.get_ident(),
            }
        )
        return {}


def _handler(module, name, message):
    async def drive():
        import importlib

        return await getattr(importlib.import_module(module), name)(message)

    return drive


def _route(module, name, **kwargs):
    async def drive():
        import importlib

        return await getattr(importlib.import_module(module), name)(**kwargs)

    return drive


def _job(module, cls):
    async def drive():
        import importlib

        return await getattr(importlib.import_module(module), cls)().enrich([{"id": "m-1", "text": "x"}])

    return drive


SITES = {
    "complexity_summary handler": (
        "topos.features.complexity.engine", "get_complexity_summary",
        _handler("topos.core.handlers.complexity", "handle_complexity_summary", {"id": "r", "payload": {}}),
    ),
    "complexity_timeline handler": (
        "topos.features.complexity.engine", "get_shift_timeline",
        _handler("topos.core.handlers.complexity", "handle_complexity_timeline", {"id": "r", "payload": {}}),
    ),
    "complexity_topics_daily handler": (
        "topos.features.complexity.topics_daily", "topics_daily",
        _handler("topos.core.handlers.complexity", "handle_complexity_topics_daily", {"id": "r", "payload": {}}),
    ),
    "complexity_influence handler": (
        "topos.features.complexity.engine", "get_influence_threads",
        _handler("topos.core.handlers.complexity", "handle_complexity_influence", {"id": "r", "payload": {}}),
    ),
    "graph_cypher_query handler": (
        "topos.features.entities.cypher", "run_cypher",
        _handler("topos.core.handlers.graph_query", "handle_graph_cypher_query",
                 {"id": "r", "payload": {"query": "MATCH (n) RETURN n"}}),
    ),
    "tools_index_status handler": (
        "topos.features.signal.tool_index", "index_status",
        _handler("topos.core.handlers.tool_index", "handle_tools_index_status", {"id": "r"}),
    ),
    "tools_index handler": (
        "topos.features.signal.tool_index", "index_tools",
        _handler("topos.core.handlers.tool_index", "handle_tools_index",
                 {"id": "r", "payload": {"tools": [{"name": "probe", "description": "d"}]}}),
    ),
    "run_pack_backfill handler": (
        "topos.features.derivation.surfaces", "run_pack_backfill",
        _handler("topos.core.handlers.derivation", "handle_run_pack_backfill",
                 {"id": "r", "payload": {"pack_id": "p", "limit": 5}}),
    ),
    "affinity recompute route": (
        "topos.features.entities.affinity_owner", "recompute_affinity_now",
        _route("topos.api.signal", "post_affinity_recompute", _api_key="k"),
    ),
    "job progress route": (
        "topos.api.ingestion_sources", "get_job",
        _route("topos.api.ingestion_sources", "get_job_progress", job_id="j"),
    ),
    "disk status core": (
        "topos.api.disk_space_config", "_status_sync",
        _route("topos.api.disk_space_config", "get_disk_status_core"),
    ),
    "complexity snapshot job": (
        "topos.features.complexity.engine", "get_complexity_summary",
        _job("topos.enrichment.jobs.canonical.complexity_snapshot_job", "ComplexitySnapshotJob"),
    ),
    "derivation job": (
        "topos.enrichment.jobs.canonical.derivation_job", "run_derivation_batch",
        _job("topos.enrichment.jobs.canonical.derivation_job", "DerivationJob"),
    ),
    "fact extraction job": (
        "topos.features.facts.extract", "extract_facts_from_batch",
        _job("topos.enrichment.jobs.canonical.fact_extraction_job", "FactExtractionJob"),
    ),
}


@pytest.mark.parametrize("site", sorted(SITES))
def test_the_worker_is_given_its_own_connection(site, tmp_path, monkeypatch):
    import asyncio
    import importlib

    module, attr, drive = SITES[site]
    owner = _owner_connection_on_this_thread(tmp_path, monkeypatch)
    recorder = _Recorder(owner)
    monkeypatch.setattr(importlib.import_module(module), attr, recorder)
    if site == "fact extraction job":
        # Keep the wallet probe out of it: a hosted provider with no credit defers the batch.
        monkeypatch.setattr("topos.config.facts_llm.resolve_facts_llm_request", lambda s, c: ("ollama", None))

    asyncio.run(drive())

    assert len(recorder.calls) == 1, recorder.calls
    call = recorder.calls[0]
    assert call["thread"] != threading.get_ident(), "the work never left the loop thread"
    assert call["on_owner"] is False, "the worker was handed the event loop's connection"
    assert call["own"] is True, "the worker was handed a connection that is not its own"
