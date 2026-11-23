"""Owner-only, forward-only audit of the inbound truth lane.

No claim/note/source text or exception messages. Evidence edges are observations
at persistence time, not proof of causal use or independent corroboration. Erasure
wins over history: deleting either endpoint removes its edges.
"""
from __future__ import annotations
import json
import math

EVENTS = 'truth_events'
EDGES = 'truth_evidence_edges'
DDL = (
    f'''CREATE TABLE IF NOT EXISTS {EVENTS} (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        check_id TEXT NOT NULL, event_kind TEXT NOT NULL, occurred_at TEXT NOT NULL,
        payload_json TEXT NOT NULL)''',
    f'CREATE INDEX IF NOT EXISTS idx_truth_events_check ON {EVENTS}(check_id, sequence)',
    f'''CREATE TABLE IF NOT EXISTS {EDGES} (
        edge_id INTEGER PRIMARY KEY AUTOINCREMENT, event_sequence INTEGER NOT NULL,
        check_id TEXT NOT NULL, parent_check_id TEXT NOT NULL, edge_kind TEXT NOT NULL,
        parent_attempt INTEGER, parent_verdict TEXT, parent_label TEXT,
        parent_checked_at TEXT, parent_labelled_at TEXT,
        UNIQUE(event_sequence, parent_check_id, edge_kind))''',
    f'CREATE INDEX IF NOT EXISTS idx_truth_edges_parent ON {EDGES}(parent_check_id)',
    f'CREATE INDEX IF NOT EXISTS idx_truth_edges_child ON {EDGES}(check_id)',
)


def timings(value):
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in ('gate', 'read', 'retrieve', 'judge', 'check', 'total')
            and type(v) in (int, float) and math.isfinite(v) and v >= 0}


def failure_class(reason, verdict):
    if verdict != 'unable':
        return None
    reason = str(reason or '')
    if reason.startswith('waiting_until:'):
        return 'waiting_until'
    if reason.startswith('model_unavailable:'):
        suffix = reason.partition(':')[2]
        return 'model_unavailable:' + (suffix if suffix in (
            'TimeoutError', 'URLError', 'OSError', 'deadline', 'bad_reply', 'parse') else 'other')
    return reason if reason in ('deadline', 'read_timeout', 'timeout') else 'other'


def append(conn, check_id, kind, at, *, check=None, previous_label=None, label=None,
           reason=None, evidence_ok=None, evidence_opened=None):
    row = conn.execute('SELECT verdict, attempts, model, effort, read_timeout_s, latency_ms, '
                       'engine_version, engine_sha16, source, speaker_kind, claim_text, owner_labels_shown, contradicted '
                       'FROM truth_checks WHERE check_id=?', (check_id,)).fetchone()
    fields = ('verdict', 'attempt', 'model', 'effort', 'read_timeout_s', 'latency_ms',
              'engine_version', 'engine_sha16', 'source', 'speaker_kind', 'claim_bearing', 'owner_labels_shown', 'contradicted')
    payload = dict(zip(fields, row))
    payload['claim_bearing'] = bool(payload['claim_bearing']) and payload['verdict'] != 'not_checkable'
    payload['schema_version'] = 1
    if kind == 'attempt':
        payload.update(failure_class=failure_class(check.get('reason'), payload['verdict']),
                       timings_ms=timings(check.get('timings_ms')), actor='verifier', acceptance='not_measured',
                       cited_reference_count=len(check.get('cited_refs') or []))
    else:
        payload.update(previous_label=previous_label, label=label, actor='owner',
                       acceptance='owner_testimony' if label == 'true' else 'not_accepted' if kind in ('label', 'dismiss') else 'unchanged',
                       reason=reason, evidence_ok=evidence_ok, evidence_opened=evidence_opened)
    sequence = conn.execute(f'INSERT INTO {EVENTS}(check_id,event_kind,occurred_at,payload_json) VALUES (?,?,?,?)',
                            (check_id, kind, at, json.dumps(payload, allow_nan=False))).lastrowid
    if kind == 'attempt':
        _edges(conn, sequence, check_id, check.get('cited_refs') or [])
    return sequence


def _edges(conn, sequence, child, refs):
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        table, record_id = ref.get('table'), ref.get('record_id')
        if not isinstance(record_id, str):
            continue
        kind = 'source_record_reuse'
        if table in ('verified_claim', 'signal_objects'):
            obj = conn.execute("SELECT object_key FROM signal_objects WHERE object_id=? AND object_type='verified_claim' AND valid_to IS NULL", (record_id,)).fetchone()
            if not obj or not str(obj[0]).startswith('truth_check:'):
                continue
            parent = conn.execute('SELECT check_id, attempts, verdict, checked_at FROM truth_checks WHERE check_id=?',
                                  (obj[0][len('truth_check:'):],)).fetchone()
            kind = 'owner_label_citation'
        else:
            parent = conn.execute('SELECT check_id, attempts, verdict, checked_at FROM truth_checks WHERE record_table=? AND record_id=?',
                                  (table, record_id)).fetchone()
        if not parent:
            continue  # An unchecked source is not a previously verified claim.
        label = conn.execute('SELECT label, labelled_at FROM truth_labels WHERE check_id=?', (parent[0],)).fetchone()
        conn.execute(f'INSERT OR IGNORE INTO {EDGES}(event_sequence,check_id,parent_check_id,edge_kind,parent_attempt,'
                     'parent_verdict,parent_label,parent_checked_at,parent_labelled_at) VALUES (?,?,?,?,?,?,?,?,?)',
                     (sequence, child, parent[0], kind, parent[1], parent[2], label[0] if label else None,
                      parent[3], label[1] if label else None))


def forget(conn, checks):
    marks = ','.join('?' for _ in checks)
    for table in (EDGES, EVENTS):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        conn.execute(f'DELETE FROM {table} WHERE check_id IN ({marks})', checks)
        if table == EDGES:
            conn.execute(f'DELETE FROM {table} WHERE parent_check_id IN ({marks})', checks)
