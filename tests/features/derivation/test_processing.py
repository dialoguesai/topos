import sqlite3
import pytest
from topos.features.derivation.processing import measured_generate, processing_summary
from topos.storage.db.migrations.derivation_processing_v1 import apply_derivation_processing_v1_up


def database():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)')
    apply_derivation_processing_v1_up(conn)
    return conn


class Adapter:
    def _generate(self, *args, **kwargs):
        return {'text': '{}', 'usage': {'prompt_tokens': 12, 'completion_tokens': 3},
                'timings': {'load_ms': 5, 'prefill_ms': 10, 'decode_ms': 20}}


def test_measurement_preserves_output_and_records_model_phase_mode(monkeypatch):
    conn = database()
    times = iter([10, 10.25])
    monkeypatch.setattr('topos.features.derivation.processing.time.perf_counter', lambda: next(times))
    assert measured_generate(conn, Adapter(), 'extract-9b', 'PRIVATE PROMPT', pack_id='p', phase='extraction', mode='backfill') == '{}'
    row = conn.execute('SELECT model, phase, mode, elapsed_ms, failed, prompt_tokens, load_ms FROM derivation_processing').fetchone()
    assert row == ('extract-9b', 'extraction', 'backfill', 250, 0, 12, 5)
    assert 'PRIVATE PROMPT' not in str(conn.execute('SELECT * FROM derivation_processing').fetchall())


def test_failed_call_is_recorded_and_original_error_propagates():
    class Broken:
        def _generate(self, *args, **kwargs):
            raise RuntimeError('model offline')
    conn = database()
    with pytest.raises(RuntimeError, match='model offline'):
        measured_generate(conn, Broken(), 'verify-27b', 'prompt', pack_id='p', phase='verification', mode='ingest')
    assert processing_summary(conn)['models'][0]['errors'] == 1
    assert conn.execute('SELECT COUNT(*) FROM derivation_processing').fetchone()[0] == 1


def test_window_percentiles_pack_grouping_and_migration_are_idempotent():
    conn = database()
    since = processing_summary(conn)['tracking_since']
    for i in range(1, 21):
        conn.execute("INSERT INTO derivation_processing (call_id,pack_id,model,phase,mode,started_at,elapsed_ms,failed) VALUES (?,?,?,'extraction','trial',strftime('%Y-%m-%dT%H:%M:%fZ','now'),?,0)", (str(i), 'p' if i < 11 else 'q', 'm', i * 10))
    conn.execute("INSERT INTO derivation_processing (call_id,pack_id,model,phase,mode,started_at,elapsed_ms,failed) VALUES ('old','p','m','extraction','trial','2020-01-01T00:00:00Z',99999,1)")
    apply_derivation_processing_v1_up(conn)
    data = processing_summary(conn)
    assert data['tracking_since'] == since
    assert data['models'][0]['calls'] == 20
    assert data['models'][0]['avg_latency_ms'] == 105
    assert data['models'][0]['p95_latency_ms'] == 190
    assert data['by_pack']['p'][0]['calls'] == 10
    assert data['by_pack']['p'][0]['p95_latency_ms'] == 100
    assert data['models'][0]['prompt_tokens'] is None


def test_old_node_is_unavailable_not_zero():
    assert processing_summary(sqlite3.connect(':memory:')) == {'available': False}


def test_calendar_windows_use_local_midnight_monday_and_dst():
    from datetime import datetime, timezone
    from topos.features.derivation.processing import processing_windows
    conn = database()
    # Monday 00:30 Chicago, after the DST change. Sunday records remain in 30d only.
    for key, stamp in [('sun', '2026-11-02T05:45:00Z'), ('mon', '2026-11-02T06:15:00Z'), ('future', '2026-11-02T07:00:00Z')]:
        conn.execute("INSERT INTO derivation_processing (call_id,pack_id,model,phase,mode,started_at,elapsed_ms,failed) VALUES (?,'p','m','extraction','ingest',?,100,0)", (key, stamp))
    result = processing_windows(conn, 'America/Chicago', now=datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc))
    assert result['windows']['today']['models'][0]['calls'] == 1
    assert result['windows']['week']['models'][0]['calls'] == 1
    assert result['windows']['30d']['models'][0]['calls'] == 2
    assert processing_windows(conn, 'not/a/timezone')['time_zone'] == 'UTC'
