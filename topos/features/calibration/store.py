"""Versioned human answers and an indexed current-answer lookup.

The label is also a real FactStore fact, so revisions and owner corrections use
our existing storage. The lookup is rebuildable and never substitutes for a
current, unprotected supporting fact. No model or recent-facts scan is involved.
"""
import json
from datetime import datetime, timezone
from pydantic import Field
from typing import Annotated, Literal
from ...permissions_v2.contract import StrictModel
from ...permissions_v2.truth_contract import PACK, QUESTIONS, TruthIntent, TruthResult, BY_ID
from ...storage.db.write_gate import batched_writes
from ..facts.store import FactStore

class Answer(StrictModel):
    pack_id: Literal['icebreakers-v1']
    question_id: Literal['drink', 'time', 'weekend', 'planning', 'company']
    value: str | None
    expected_revision: Annotated[int, Field(strict=True, ge=0)]

class RevisionConflict(ValueError):
    pass

def exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calibration_answers'").fetchone() is not None

def ensure_schema(conn):
    from ...storage.db.migrations.calibration_answers_v1 import create_calibration_answers
    create_calibration_answers(conn)

def catalog(conn):
    answers = {}
    if exists(conn):
        answers = {r[0]: {'value': r[1], 'revision': r[2]} for r in conn.execute(
            'SELECT question_id,value,revision FROM calibration_answers WHERE pack_id=?', (PACK,))}
    return {'id': PACK, 'title': 'Little things about you', 'kind': 'truth_pack', 'version': 1,
            'questions': [dict(q, answer=answers.get(q['id'], {'value': None, 'revision': 0})) for q in QUESTIONS]}

def save(conn, raw):
    answer = Answer.parse(raw)
    if answer.value is not None:
        TruthIntent.parse({k: v for k, v in answer.model_dump().items() if k != 'expected_revision'})
    with batched_writes(conn):
        ensure_schema(conn)
        old = conn.execute('SELECT revision,fact_id FROM calibration_answers WHERE pack_id=? AND question_id=?',
                           (answer.pack_id, answer.question_id)).fetchone()
        revision = old[0] if old else 0
        if revision != answer.expected_revision:
            raise RevisionConflict('This answer changed elsewhere. Refresh and try again.')
        now = datetime.now(timezone.utc).isoformat()
        fact = None
        if answer.value is not None:
            fact = FactStore(conn).assert_fact(subject_entity_id='self', predicate='calibration_' + answer.question_id,
                object_value=answer.value, confidence=1.0, asserted_by='owner', disclosure='scoped',
                source_refs=[{'type': 'calibration_answer', 'pack_id': PACK, 'question_id': answer.question_id,
                              'revision': revision + 1}], valid_from=now)
            if not fact:
                raise ValueError('This answer is excluded. Remove its exclusion before answering again.')
        elif old and old[1]:
            conn.execute('UPDATE signal_objects SET valid_to=? WHERE object_id=? AND valid_to IS NULL', (now, old[1]))
        conn.execute('''INSERT INTO calibration_answers VALUES (?,?,?,?,?,?)
            ON CONFLICT(pack_id,question_id) DO UPDATE SET value=excluded.value,revision=excluded.revision,
            fact_id=excluded.fact_id,updated_at=excluded.updated_at''',
            (PACK, answer.question_id, answer.value, revision+1, fact['object_id'] if fact else None, now))
    return catalog(conn)

def _direct_answer_refs(raw, *, pack_id, question_id, revision):
    """Complete provenance for a label entered in Calibrate, with no source text.

    Re-saving the same value refreshes one FactStore row and retains its older
    calibration references. Every reference must be for this question, and one
    must bind the current answer revision. Mixed or missing lineage is unknown.
    """
    try:
        refs = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(refs, list) or not refs or type(revision) is not int or revision < 1:
        return False
    current = False
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {'type', 'pack_id', 'question_id', 'revision'}
            or ref['type'] != 'calibration_answer' or ref['pack_id'] != pack_id
            or ref['question_id'] != question_id or type(ref['revision']) is not int
            or not 1 <= ref['revision'] <= revision):
            return False
        current = current or ref['revision'] == revision
    return current

def verify(conn, intent, *, guard=None):
    unknown = TruthResult(verdict='unknown', basis='insufficient_evidence')
    if not exists(conn):
        return unknown, 0
    row = conn.execute('''SELECT a.value,a.revision,a.fact_id,f.payload_json,f.valid_to,
        f.object_type,f.object_key,f.signal_dimension,f.source_refs_json
        FROM calibration_answers a LEFT JOIN signal_objects f ON f.object_id=a.fact_id
        WHERE a.pack_id=? AND a.question_id=?''', (intent.pack_id, intent.question_id)).fetchone()
    if not row:
        return unknown, 0
    value, revision, fact_id, payload, valid_to, object_type, object_key, dimension, refs = row
    if value is None or payload is None or valid_to is not None:
        return unknown, revision
    try:
        payload = json.loads(payload)
    except (TypeError, ValueError):
        return unknown, revision
    # A generic fact edit/rejection cannot leave an authoritative stale label behind.
    if (not isinstance(payload, dict) or object_type != 'fact'
        or object_key != 'fact:self:calibration_' + intent.question_id or dimension != 'profile'
        or not _direct_answer_refs(refs, pack_id=intent.pack_id, question_id=intent.question_id, revision=revision)
        or payload.get('subject_entity_id') != 'self' or payload.get('predicate') != 'calibration_' + intent.question_id
        or payload.get('object_entity_id') is not None
        or payload.get('object_value') != value or payload.get('asserted_by') != 'owner'
        or payload.get('disclosure') != 'scoped'
        or conn.execute("SELECT 1 FROM intelligence_exclusions WHERE artifact_type='fact' AND artifact_key IN (?,?)",
                        (f'self:calibration_{intent.question_id}', f'self:calibration_{intent.question_id}:{value}')).fetchone()):
        return unknown, revision
    # Unlike legacy prose, this certified row depends only on the owner's exact
    # current input above. An unrelated pending rebuild or protected source
    # record cannot taint it. Its own fact, subject, and both finite labels still
    # pass the same privacy guard, including names awaiting their rebuild.
    if guard and (guard.blocks_record_id(fact_id) or guard.blocks_entity_id('self')
                  or guard.text_mentions_blackholed(BY_ID[intent.question_id]['prompt'] + ' ' + value + ' ' + intent.value)):
        return unknown, revision
    return TruthResult(verdict='supports' if value == intent.value else 'contradicts', basis='owner_answer'), revision
