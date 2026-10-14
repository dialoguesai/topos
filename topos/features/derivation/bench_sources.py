"""Read-only bench evidence from explicit help exchanges in named human DMs.

This bounded English recognizer retrieves requests and replies, not personal profiles.
It never writes outward facts or calls a model. Unrecognized replies stay unqualified;
quotes, reactions, group messages and another person's offers cannot create readiness.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import re
import sqlite3

from .bench_evidence import terms
from .bench_readiness import _instant, assess_readiness

VERSION = 'bench-source-exchanges-1'
WINDOW_DAYS = 30
_ACTION = r'(?:help|review|check|edit|design|build|write|set up|introduce|connect|teach|advise|take a look|look (?:at|over)|give .{0,30}feedback)'
_REQUEST = re.compile(
    rf'\b(?:(?:can|could|would|will) you (?:please )?{_ACTION}|'
    rf'(?:any interest in|would you be interested in) (?:reviewing|editing|helping|building|designing)|'
    rf'i (?:hope|wish) you (?:could|can|would) {_ACTION}|'
    r'does this (?:look|seem) like .{0,45}(?:missing|need))\b', re.I)
_OFFER = re.compile(rf'\b(?:i (?:can|could|will|am happy to|would be happy to)|i\x27ll|happy to|let me) (?:{_ACTION})\b', re.I)
_LIMIT = re.compile(r"\b(?:i (?:can\x27t|cannot|won\x27t|am unable)|i\x27m (?:not (?:qualified|available|the right person)|unavailable)|not personally qualified|not the right person)\b", re.I)
_CONDITION = re.compile(r"\b(?:if|once|provided|depending|maybe|might|perhaps|possibly|hopefully|could|figure out if|before i (?:can|could)|need .{0,30}first)\b", re.I)
_COMPLETE = re.compile(r"\b(?:thanks? (?:you )?for (?:reviewing|editing|checking|building|writing|helping)|no longer need|don\x27t need .{0,30}anymore|already (?:finished|completed|done))\b", re.I)
_REACTION = re.compile(r'^(?:liked|loved|emphasized|disliked|laughed at|questioned|reacted)\b|^[^\w]{1,8}to [“\"]', re.I)
_UNTIL = re.compile(r'\buntil (\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d))\b', re.I)
_AVAILABLE = re.compile(r"\b(?:i am|i\x27m) available\b", re.I)


def normalized(text):
    return str(text or '').replace('’', "'").strip()


def plain_statement(text):
    text = normalized(text)
    return bool(text) and not _REACTION.search(text) and not any(q in text for q in ('“', '”', '"', '\n>')) and not text.startswith('>')


def is_help_request(text):
    text = normalized(text)
    return plain_statement(text) and len(terms(text)) >= 3 and bool(_REQUEST.search(text))


def response_kind(text):
    """A retrieval label, not a claim of semantic completeness or demonstrated ability."""
    text = normalized(text)
    if not plain_statement(text):
        return None
    if _LIMIT.search(text):
        return 'limited_or_declined'
    if _CONDITION.search(text) and (_OFFER.search(text) or re.search(r'\b(?:take it on|help|review|thoughts)\b', text, re.I)):
        return 'conditional'
    if _OFFER.search(text) and not re.search(r'\b(?:not|never|wish|joking|kidding|pretend|hypothetical|ignore|record that|someone|somebody|said|says|told|imagine)\b|\?', text, re.I):
        return 'offered'
    return None


def evidence_excerpt(text):
    text = str(text or '')
    if len(text) <= 1200:
        return text
    # Preserve the clause that justified selection, not an arbitrary prefix. The
    # canonical record reference remains attached and truncation is explicit.
    match = _LIMIT.search(normalized(text)) or _OFFER.search(normalized(text)) or _REQUEST.search(normalized(text))
    start = max(0, match.start()-200) if match else 0
    return ('[…excerpt…] ' if start else '') + text[start:start+1200] + (' […truncated…]' if start+1200 < len(text) else '')


def _ref(row):
    return {'table': 'conversation_messages', 'record_id': row['message_id'],
            'event_at': row['event_at'], 'excerpt': evidence_excerpt(row['content']),
            'speaker': 'owner' if row['is_from_self'] else 'peer'}


def direct_conversations(conn, dataset_id, now):
    """Classify once in SQL; do not sort/deserialise the full message history per read."""
    direct = {r[0] for r in conn.execute("""SELECT conversation_id FROM conversation_messages
        WHERE dataset_id=? AND is_from_self=0 AND sender_id IS NOT NULL AND sender_id NOT IN ('','self')
        AND julianday(event_at)<=julianday(?) GROUP BY conversation_id HAVING COUNT(DISTINCT sender_id)=1""",
        (dataset_id,now.isoformat()))}
    try:
        groups = {r[0] for r in conn.execute("SELECT conversation_id FROM conversation_participants WHERE dataset_id=? GROUP BY conversation_id HAVING COUNT(DISTINCT contact_id)>2", (dataset_id,))}
        direct -= groups
    except sqlite3.Error:
        pass
    return direct


def eligible_people(conn, dataset_id):
    """Named human graph peers, honoring exclusions across duplicate identity records."""
    from ...analytics.person_graph import build_person_nodes
    from ...analytics.messenger_directed import peer_identity_key
    from .net_subject_policy import subject_policy, DENY
    from ..lifecycle.blackhole import BlackholeStore
    from ..lifecycle.off_limits_view import for_own_processing
    aliases = defaultdict(set)
    try:
        for key, eid in conn.execute("SELECT ci.identifier,e.entity_id FROM contact_identifiers ci JOIN entities e ON e.contact_id=ci.contact_id WHERE e.entity_type='person'"):
            aliases[peer_identity_key(key)].add(eid)
    except sqlite3.Error:
        pass
    people = defaultdict(list)
    for node in build_person_nodes(conn, dataset_id):
        if node.get('is_owner') or node.get('needs_name') or not node.get('label'):
            continue
        ids = {node['entity_id']} if node.get('entity_id') else set()
        keys = node.get('messenger_keys', [])
        for key in keys:
            ids.update(aliases[peer_identity_key(key)])
        if any(subject_policy(conn, eid) == DENY for eid in ids):
            continue
        refs = ids | set(keys) | {node['label']}
        if any(BlackholeStore(conn).is_blackholed(str(ref), view=for_own_processing()) for ref in refs):
            continue
        for key in keys:
            people[str(key)].append(node)
    return {key: nodes[0] for key, nodes in people.items() if len(nodes)==1}


def read_help_exchanges(conn, dataset_id, *, now=None, limit=6):
    """Return concrete requests with attributable replies, plus explicit read coverage.

    Read at most 20,001 recent records. A truncated read is not complete coverage. Only
    unambiguous human DMs are admitted; entity exclusions and source event clocks apply.
    A purpose is the owner's request, never a role fabricated from frequency or job title.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('bench source reference time must include timezone')
    coverage = {'version': VERSION, 'status': 'complete', 'window_days': WINDOW_DAYS,
                'source_family': 'human_direct_messages', 'semantic_scope': 'bounded_english_help_exchanges',
                'excluded': {}, 'messages_read': 0, 'requests_found': 0}
    if not dataset_id:
        return [], {**coverage, 'status': 'unavailable', 'reason': 'no_messaging_dataset'}
    try:
        people = eligible_people(conn, dataset_id)
        direct = direct_conversations(conn, dataset_id, now)
        columns = {r[1] for r in conn.execute('PRAGMA table_info(conversation_messages)')}
        reply = 'reply_to_message_id' if 'reply_to_message_id' in columns else 'NULL'
        rows = conn.execute(f'''SELECT message_id,conversation_id,sender_id,is_from_self,content,event_at,{reply}
            FROM conversation_messages WHERE dataset_id=? AND content IS NOT NULL
            AND julianday(event_at)>=julianday(?) AND julianday(event_at)<=julianday(?)
            ORDER BY julianday(event_at) DESC,message_id DESC LIMIT 20001''',
            (dataset_id, (now-timedelta(days=WINDOW_DAYS)).isoformat(), now.isoformat())).fetchall()
    except sqlite3.Error as exc:
        return [], {**coverage, 'status': 'unavailable', 'reason': 'source_schema_unavailable', 'error_type': type(exc).__name__}
    if len(rows) > 20000:
        coverage['status'] = 'partial'
        rows = rows[:20000]
    coverage['messages_read'] = len(rows)
    excluded = Counter()
    threads = defaultdict(list)
    for raw in reversed(rows):
        row = dict(zip(('message_id','conversation_id','sender_id','is_from_self','content','event_at','reply_to_message_id'), raw))
        if row['conversation_id'] not in direct:
            excluded['not_direct_message'] += 1
            continue
        when = _instant(row['event_at'])
        if when is None:
            excluded['unqualified_event_clock'] += 1
            continue
        row['when'] = when
        if not row['is_from_self'] and str(row['sender_id']) not in people:
            excluded['unknown_or_excluded_person'] += 1
            continue
        threads[str(row['conversation_id'])].append(row)
    output = []
    for thread in threads.values():
        requests = [i for i, row in enumerate(thread) if row['is_from_self'] and is_help_request(row['content'])]
        coverage['requests_found'] += len(requests)
        for request_index, start in enumerate(requests):
            request = thread[start]
            end = requests[request_index + 1] if request_index + 1 < len(requests) else len(thread)
            segment = thread[start+1:end]
            if any(row['is_from_self'] and plain_statement(row['content']) and _COMPLETE.search(normalized(row['content'])) for row in segment):
                excluded['completed_or_cancelled_request'] += 1
                continue
            peer_rows = [row for row in segment if not row['is_from_self'] and plain_statement(row['content'])]
            if not peer_rows:
                continue
            first = peer_rows[0]
            # An explicit reply ID is strongest. Otherwise require a close, same-thread
            # response and retain the context-association uncertainty in fit_status.
            explicit_reply = first['reply_to_message_id'] == request['message_id']
            if first['reply_to_message_id'] and not explicit_reply:
                excluded['reply_to_other_record'] += 1
                continue
            if first['when']-request['when'] > timedelta(days=3):
                excluded['distant_reply'] += 1
                continue
            matching = [row for row in peer_rows if response_kind(row['content'])]
            if not matching:
                continue
            person = people[str(first['sender_id'])]
            matching = [row for row in matching if people[str(row['sender_id'])]['node_id'] == person['node_id']]
            if not matching:
                continue
            latest = matching[-1]
            kind = response_kind(latest['content'])
            purpose_id = 'request:' + str(request['message_id'])
            observations = []
            # Availability requires an explicit time-zone-qualified validity bound in
            # the same direct offer. No inferred expiry, local date or peer timezone.
            until = _UNTIL.search(normalized(latest['content']))
            if kind == 'offered':
                observation = {'person_id': person['node_id'], 'purpose_id': purpose_id,
                    'dataset_id': dataset_id, 'speaker_id': person['node_id'], 'recipient': 'owner',
                    'assertion_mode': 'direct', 'dimension': 'willingness', 'value': 'offered',
                    'event_at': latest['event_at'], 'valid_until': until.group(1) if until else None,
                    'conditions_status': 'none', 'source': {'table':'conversation_messages','record_id':latest['message_id']}}
                observations.append(observation)
                if until and _AVAILABLE.search(normalized(latest['content'])):
                    observations.append({**observation, 'dimension': 'availability', 'value': 'available'})
            # Later unclassified replies may revise an offer. Context association and
            # incomplete parsing cannot establish present readiness.
            purpose_overlap = len(terms(request['content']) & terms(latest['content'])) >= 2
            exact = (explicit_reply and purpose_overlap and latest['message_id'] == first['message_id'] == peer_rows[-1]['message_id']
                     and not any(row['when'] > latest['when'] and plain_statement(row['content']) for row in segment))
            readiness = assess_readiness(person_id=person['node_id'], purpose_id=purpose_id,
                dataset_id=dataset_id, observations=observations, as_of=now,
                coverage='complete' if exact and coverage['status']=='complete' else 'partial')
            candidate = {'person_id': person['node_id'], 'name': person['label'], 'skill': None,
                'basis': 'request_and_reply', 'warmth_band': None, 'capability_status': 'unknown',
                'fit_status': 'supported_purpose_match' if explicit_reply and purpose_overlap else 'inferred_reply_context',
                'readiness': readiness, 'response_status': kind,
                'last_evidence_at': latest['event_at'], 'evidence': [_ref(request), _ref(latest)],
                'next_step': {'offered':'Confirm that this offer still applies and agree on scope and timing.',
                    'conditional':'Clarify the conditions and scope before relying on this help.',
                    'limited_or_declined':'Their reply includes a limitation. Check its scope against your request before relying on this help.'}[kind]}
            # Preserve a later clarification/counterstatement even if its wording is
            # outside this recognizer; the user must be able to inspect what followed.
            later = [row for row in segment if row['when'] > latest['when'] and plain_statement(row['content'])]
            for row in later[-2:]:
                candidate['evidence'].append(_ref(row))
            prior = [row for row in thread[max(0,start-6):start] if row['is_from_self']
                     and plain_statement(row['content']) and len(terms(row['content']))>=3
                     and request['when']-row['when']<=timedelta(days=3)]
            context = [_ref(prior[-1])] if prior else []
            request_end = (_UNTIL.search(normalized(request['content']))
                if re.search(r'\bi (?:still )?need (?:your )?help until\b', normalized(request['content']), re.I) else None)
            current_need = bool(request_end and _instant(request_end.group(1)) and now < _instant(request_end.group(1)))
            output.append({'role_shape_id':purpose_id, 'label':request['content'][:500],
                'label_terms': sorted(terms(request['content'])), 'recurrence_weeks':1,
                'evidence_count':2, 'score':0.0, 'self_performed_share':None, 'blocking_score':None,
                'purpose_basis':'explicit_owner_help_request', 'need_status':'supported_current_need' if current_need else 'observed_help_request',
                'goal_status':'current_status_unverified', 'event_at':request['event_at'],
                'evidence':[_ref(request)] + context, 'candidates':[candidate]})
    # Keep distinct purposes for the same person available to the lineup selector.
    # Bound the pool separately from the final eight-purpose display.
    selected = sorted(output, key=lambda r: (_instant(r['event_at']), r['role_shape_id']), reverse=True)[:limit]
    coverage['excluded'] = dict(excluded)
    coverage['matched_exchanges'] = len(output)
    coverage['displayed_exchanges'] = len(selected)
    return selected, coverage
