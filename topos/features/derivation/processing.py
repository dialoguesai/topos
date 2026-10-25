"""Per-model-call processing metrics, separate from assertions and fact validity.

Latency is client-observed wall time, including model loading and failures. Each
verifier vote is a call. Availability probes are not derivation calls. No prompts,
source text, record identifiers or model replies are retained here.
"""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import logging
import math
import sqlite3
import time
import uuid

log = logging.getLogger(__name__)


def measured_generate(conn, adapter, model, prompt, *, pack_id, phase, mode, n=900):
    started_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    start = time.perf_counter()
    out = None
    failed = True
    try:
        out = adapter._generate(model, prompt, num_predict=n, think=False,
                                temperature=0.0, num_ctx=8192, timeout=180)
        failed = bool(isinstance(out, dict) and (out.get('error') or out.get('status') == 'deferred'))
        return str(out.get('text') or '') if isinstance(out, dict) else str(out or '')
    finally:
        elapsed = max(0, (time.perf_counter() - start) * 1000)
        usage = out.get('usage') if isinstance(out, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        timings = (out.get('timings') or usage.get('timings')) if isinstance(out, dict) else None
        timings = timings if isinstance(timings, dict) else {}
        def number(obj, key):
            value = obj.get(key) if isinstance(obj, dict) else None
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
        try:
            conn.execute('''INSERT INTO derivation_processing
                (call_id, pack_id, model, phase, mode, started_at, elapsed_ms, failed,
                 prompt_tokens, completion_tokens, load_ms, prefill_ms, decode_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (uuid.uuid4().hex, pack_id, model, phase, mode, started_at, elapsed, int(failed),
                 number(usage, 'prompt_tokens'), number(usage, 'completion_tokens'),
                 number(timings, 'load_ms'), number(timings, 'prefill_ms'), number(timings, 'decode_ms')))
        except sqlite3.Error:
            # Measurement must not change whether a fact is accepted or ingest succeeds.
            log.warning('Could not record derivation processing measurement', exc_info=True)


def processing_summary(conn, *, window_start=None, now=None):
    """Bounded response: aggregate calls by pack/model/phase over 30 days.

    Percentiles use the nearest-rank definition. Preserve latency totals and sample
    counts so the client can combine means without averaging averages; p95 is per
    row only and must never be averaged across models.
    """
    now = now or datetime.now(timezone.utc)
    window_start = window_start or now - timedelta(days=30)
    stamp = lambda dt: dt.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    try:
        since = conn.execute('SELECT tracking_since FROM derivation_processing_state WHERE singleton=1').fetchone()
        if not since:
            return {'available': False}
        cursor = conn.execute('''WITH recent AS (
            SELECT * FROM derivation_processing
            WHERE started_at >= ? AND started_at <= ?
        ), expanded AS (
            SELECT pack_id, model, phase, elapsed_ms, failed, prompt_tokens, completion_tokens, load_ms, prefill_ms, decode_ms FROM recent
            UNION ALL SELECT NULL, model, phase, elapsed_ms, failed, prompt_tokens, completion_tokens, load_ms, prefill_ms, decode_ms FROM recent
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY pack_id, model, phase ORDER BY elapsed_ms) AS rank,
                COUNT(*) OVER (PARTITION BY pack_id, model, phase) AS samples FROM expanded
        ) SELECT pack_id, model, phase, COUNT(*) AS calls, SUM(failed) AS errors,
            SUM(elapsed_ms) AS total_latency_ms, AVG(elapsed_ms) AS avg_latency_ms,
            MAX(CASE WHEN rank = (95 * samples + 99) / 100 THEN elapsed_ms END) AS p95_latency_ms,
            SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens,
            COUNT(prompt_tokens) AS token_samples, AVG(load_ms) AS avg_load_ms,
            AVG(prefill_ms) AS avg_prefill_ms, AVG(decode_ms) AS avg_decode_ms,
            COUNT(load_ms) AS timing_samples
            FROM ranked GROUP BY pack_id, model, phase ORDER BY calls DESC, pack_id, model''', (stamp(window_start), stamp(now)))
        keys = [col[0] for col in cursor.description]
        rows = [dict(zip(keys, row)) for row in cursor.fetchall()]
        by_pack = {}
        for row in rows:
            if row['pack_id'] is not None:
                by_pack.setdefault(row['pack_id'], []).append(row)
        return {'available': True, 'tracking_since': since[0], 'window_days': 30,
                'models': [row for row in rows if row['pack_id'] is None], 'by_pack': by_pack}
    except sqlite3.OperationalError:
        return {'available': False}


def processing_windows(conn, time_zone='UTC', *, now=None):
    """Calendar today/week (Monday start) in the owner's timezone; rolling 30 days."""
    try:
        zone = ZoneInfo(str(time_zone))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = ZoneInfo('UTC')
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = {'today': today, 'week': today - timedelta(days=today.weekday()),
              '30d': now - timedelta(days=30)}
    windows = {key: processing_summary(conn, window_start=start, now=now) for key, start in starts.items()}
    return {**windows['30d'], 'time_zone': str(zone), 'windows': windows}
