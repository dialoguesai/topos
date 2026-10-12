"""Isolated quality producer for real fusion and read-only owner query replay.

Protocol v1. This exercises internal owner_app, not signed gateway authority.
All input/output artifacts belong to the coordinator's private run directory.
"""
from __future__ import annotations
import asyncio
from datetime import datetime
import inspect
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def expand(lanes):
    out = []
    for lane in lanes:
        items = list(lane.get('items', []))
        if repeated := lane.get('repeat'):
            for i in range(repeated['count']):
                items.append({**repeated['item'], 'record_id': f"{repeated['prefix']}-{i}"})
        out.append((lane['name'], lane['weight'], items))
    return out


def public_items(pr):
    for key in ('summaries', 'summary_items', 'scores', 'items', 'rows', 'raw_rows', 'messages'):
        if isinstance(pr.get(key), list):
            return [i for i in pr[key] if isinstance(i, dict)]
    return []


async def main(request):
    sys.path.insert(0, request['node'])
    import topos.query.retrieval as retrieval
    from topos.query.planner import _relative_time_range
    from topos.principal import Principal, set_principal
    captured = []
    fusion_calls = 0
    original = retrieval._rrf_fuse_summary_lists
    def trace_fusion(lists, **kwargs):
        nonlocal fusion_calls
        fusion_calls += 1
        # Observe the call exactly once; never re-run it with mutated settings.
        output = original(lists, **kwargs)
        captured.extend(item for _, _, items in lists for item in items)
        return output
    retrieval._rrf_fuse_summary_lists = trace_fusion
    adapters = None
    conn = None
    if request.get('snapshot'):
        from topos.storage.adapters.factory import AdapterBundle
        from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore, SQLiteSignalFeatureStore, SQLiteVectorIndex, SQLiteGraphEdgeStore
        from topos.storage.adapters.fakes import InMemoryAuditLogStore, InMemoryQuerySessionStore
        import topos.storage.db.migrations as migrations
        migrations.ensure_migrations_applied = lambda connection: None
        import topos.query.pipeline as pipeline
        pipeline._query_work_stays_on_loop = lambda adapters=None: True
        import topos.core.state as state
        # Frozen, supplied snapshot only. No migrations, WAL creation or database writes.
        conn = sqlite3.connect(Path(request['snapshot']).as_uri() + '?mode=ro&immutable=1', uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        state.get_db_connection = lambda: conn
        adapters = AdapterBundle(SQLiteCanonicalStore(conn), SQLiteSignalFeatureStore(conn), SQLiteVectorIndex(conn), SQLiteGraphEdgeStore(conn), InMemoryAuditLogStore(), InMemoryQuerySessionStore(), 'local_database')
    results = []
    for case in request['pack']['cases']:
        captured.clear()
        fusion_calls = 0
        started = time.monotonic()
        row = {'id': case['id'], 'stages': {}}
        try:
            set_principal(Principal('owner_app', 'internal'))
            now = datetime.fromisoformat(case.get('reference_instant', request['pack']['reference_instant']))
            if case['adapter'] == 'fusion':
                fixture = case['fixture']
                kwargs = {'now': now, 'cap': fixture.get('cap', 25), 'rare_token_groups': fixture.get('rare_token_groups'),
                          'context_sources': frozenset(fixture.get('context_sources', []))}
                bounds = _relative_time_range(case['query'], now) if fixture.get('activity') else None
                if bounds and 'activity_time_range' in inspect.signature(original).parameters:
                    kwargs['activity_time_range'] = bounds
                items = trace_fusion(expand(fixture['lanes']), **kwargs)
                pr = {'summaries': items}
                if fixture.get('activity') and bounds:
                    pr['time_window'] = {'from': bounds[0], 'to': bounds[1]}
            else:
                from topos.query.pipeline import QueryPipelineOrchestrator
                from topos.query.manifest_validation import resolve_scope_manifest
                response = await QueryPipelineOrchestrator(adapters=adapters).execute(
                    query_text=case['query'], scope_id=case['scope'], access_mode=case.get('mode', 'summary'),
                    manifest=resolve_scope_manifest(case['scope']), now=now)
                row['response'] = response
                pr = response.get('public_result') or {}
                items = public_items(pr)
            row['public_result'] = pr
            row['mode'] = case.get('mode', 'summary')
            row['stages'] = {'candidates': list(captured), 'node_packet': items} if fusion_calls else {'node_packet': items}
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
        row['elapsed_ms'] = round((time.monotonic() - started) * 1000, 2)
        results.append(row)
        print(case['id'], 'error' if row.get('error') else 'observed', flush=True)
    if conn:
        conn.rollback(); conn.close()
    path = Path(request['output'])
    with path.open('x') as stream:
        os.chmod(path, 0o600)
        json.dump(results, stream, default=str)


if __name__ == '__main__':
    asyncio.run(main(json.loads(Path(sys.argv[1]).read_text())))
