"""Evidence for possible help purposes; no activity count proves a need or readiness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3


def _instant(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


from .goal_evidence import goal_sources


# Generic task vocabulary is not evidence that two people share a specialty.
_STOP = set('a an the and or of to in on for with my our their your is are be have has had i we you they it this that from at as by into about help need want work working create build develop make improve use using project goal system current new more most many some any all not no only also very much each other another out up down over under across through then than there here when where how who what which these those such so too been being do does did doing get got would could should will can may might'.split())

def terms(text):
    return {w for w in re.findall(r"[a-z][a-z0-9_-]{2,}", str(text).lower()) if w not in _STOP}


def help_purposes(conn, *, now=None, lookback_days=60, limit=8):
    """Recent owner-backed goal statements, labelled as possible help, not diagnosed gaps.

    Keep source wording rather than turning term frequency into a job title. Collapse
    repeated extraction and limit a source to one purpose to prevent extractor volume
    from masquerading as importance. Recency is a selection policy, not goal validity.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    rows = [r for r in goal_sources(conn) if r['owner_authored'] and cutoff <= _instant(r['event_at']) <= now]
    rows.sort(key=lambda r: (_instant(r['event_at']), r['goal_id']), reverse=True)
    selected, sources, seen_terms = [], set(), []
    for row in rows:
        source = (row['source_table'], row['source_thread'])
        ts = terms(row['text'])
        if source in sources or len(ts) < 2:
            continue
        if any(len(ts & old) / max(1, len(ts | old)) >= .65 for old in seen_terms):
            continue
        sources.add(source); seen_terms.append(ts)
        selected.append(dict(role_shape_id='purpose:'+row['goal_id'], label=row['text'],
                             label_terms=sorted(ts), recurrence_weeks=1, evidence_count=1,
                             score=0.0, self_performed_share=None, blocking_score=None,
                             purpose_basis='recent_owner_backed_goal', need_status='inferred_possible_help',
                             goal_status='unverified', event_at=row['event_at'],
                             evidence=[{'table':'user_goals','record_id':row['goal_id']},
                                       {'table':row['source_table'],'record_id':row['record_id']}]))
        if len(selected) >= limit:
            break
    return selected


def discussion_candidates(conn, dataset_id, purpose, *, limit=3, now=None):
    """People with source-backed discussion overlap, explicitly not capability findings.

    Only their own messages in the selected dataset qualify. Group co-presence and
    messages written by the owner must not become another person's demonstrated skill.
    """
    if not dataset_id:
        return []
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    from ...analytics.person_graph import build_person_nodes
    from ...analytics.messenger_directed import classify_conversations, load_messages, EDGE_KIND_DM
    try:
        nodes = build_person_nodes(conn, dataset_id)
        kinds = classify_conversations(load_messages(conn, dataset_id))
        rows = conn.execute('SELECT message_id,sender_id,content,event_at,conversation_id FROM conversation_messages WHERE dataset_id=? AND is_from_self=0 AND content IS NOT NULL', (dataset_id,)).fetchall()
    except sqlite3.Error:
        return []
    by_key = {str(key):n for n in nodes if not n.get('is_owner') and not n.get('needs_name') for key in n.get('messenger_keys',[])}
    pt = set(purpose.get('label_terms') or [])
    people = {}
    for mid, sender, content, at, conv in rows:
        node = by_key.get(str(sender))
        if not node or kinds.get(conv) != EDGE_KIND_DM or not _instant(at):
            continue
        if not now - timedelta(days=180) <= _instant(at) <= now:
            continue
        overlap = pt & terms(content)
        if len(overlap) < 2 or len(overlap) / max(1, len(pt)) < .25:
            continue
        nid = str(node['node_id'])
        person = people.setdefault(nid, dict(person_id=nid, name=node.get('label'), skill=None,
            basis='prior_topic_discussion', warmth_band=None, capability_status='unknown',
            readiness_status='unknown', fit_status='inferred_discussion_partner',
            evidence=[], matched_terms=set(), last_evidence_at=str(at)))
        person['matched_terms'].update(overlap)
        person['last_evidence_at'] = max(person['last_evidence_at'],str(at))
        person['evidence'].append({'table':'conversation_messages','record_id':str(mid),'event_at':str(at)})
    out = sorted(people.values(), key=lambda p:(-len(p['matched_terms']), p['person_id']))
    for p in out:
        p['matched_terms'] = sorted(p['matched_terms'])
        p['evidence'] = sorted(p['evidence'], key=lambda r:r['event_at'], reverse=True)[:3]
    return out[:limit]
