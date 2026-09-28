"""Evidence-preserving projections of stored facts, goals and graph relationships.

These adapters do not grant permission. They recover unambiguous legacy source
identities, independently qualify every source, require a common permit clause,
and check the projected assertion. Unsupported inference forms stay withheld.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .canonical import PolicyError, digest
from .evidence import _json, _key
from .entity_boundary import rows_revision
from .fact_eligibility import canonical_utc_microseconds
from .identity import ATTESTED_CONTRACT, permit_subjects
from .message_evidence import qualify_automatic_message
from .native_claim_grounding import explicitly_states_claim
from .opaque_ids import opaque_record_id
from .release import source_message_decision

MAX_SUPPORT = 20
MAX_FACTS = 5000
PREDICATE_TEXT = {'works_at':'works at','worked_at':'worked at','works_on':'works on','role_is':'has the role',
    'certified_in':'is certified in','studied_at':'studied at','skilled_in':'is skilled in',
    'prefers':'prefers','member_of':'is a member of','lives_in':'lives in','practices':'practices','training_for':'is training for'}


@dataclass
class Projection:
    table: str
    record_id: str
    kind: str
    content: str
    fields: dict
    sources: list
    revision: str
    allow_clause_id: str

    def output(self, *, key, grant_id, precision):
        citations, sources, times = [], set(), []
        for qualified, rows in self.sources:
            identity = qualified.snapshot.message.identity
            row = rows[_key(identity)]
            source_id = identity.source_id
            sources.add(source_id)
            citations.append(dict(record_id=opaque_record_id(key,grant_id=grant_id,table=identity.table,
                source_id=source_id,dataset_id=identity.dataset_id,record_id=identity.record_id),
                source_id=source_id,content=row['content']))
            times.append(canonical_utc_microseconds(row['event_at']))
        # Old support cannot gain a fresh date through recent extraction.
        event = min(times)
        return dict(kind=self.kind,record_id=opaque_record_id(key,grant_id=grant_id,table=self.table,
            source_id=None,dataset_id=None,record_id=self.record_id),content=self.content,
            source_ids=sorted(sources),citations=citations,
            event_at=event//1000000 if precision=='second' else event//86400000000*86400 if precision=='day' else None,
            **self.fields)


def resolve_reference(resolver, conn, ref):
    """Fill absent source/dataset from exactly one canonical row, never guesses.

    This does not enroll or edit data. The subsequent qualifier must still prove
    native origin against that exact row and source. Conflicting supplied fields
    and cross-table/dataset ambiguity refuse.
    """
    if not isinstance(ref,dict) or not isinstance(ref.get('record_id'),str):
        raise PolicyError('lineage_identity_incomplete')
    table = ref.get('table')
    if table not in {'conversation_messages','ai_chat_messages'}:
        raise PolicyError('lineage_unsupported')
    dataset = 'dataset_id' if table=='conversation_messages' else 'NULL'
    where,args='message_id=?',[ref['record_id']]
    if ref.get('source_id') is not None:
        where+=' AND source_id=?';args.append(ref['source_id'])
    if ref.get('dataset_id') is not None:
        if table!='conversation_messages': raise PolicyError('lineage_identity_incomplete')
        where+=' AND dataset_id=?';args.append(ref['dataset_id'])
    matches=conn.execute(f'SELECT source_id,{dataset} FROM {table} WHERE {where}',args).fetchmany(2)
    if len(matches)!=1:
        raise PolicyError('lineage_identity_ambiguous')
    return resolver._identity(table,ref['record_id'],matches[0][0],matches[0][1])


def _unrestricted(resolver,conn,reviews,review_db,table,record_id,row):
    from .exclusion_floor import exclusions
    vetoes=exclusions(conn)
    if record_id in vetoes['record'] or record_id in reviews._opt_outs_in(review_db):
        raise PolicyError('owner_opted_out')
    if table=='entities' and record_id in vetoes['entity']:
        raise PolicyError('intelligence_excluded')
    if conn.execute('SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=?',(table,record_id)).fetchone():
        raise PolicyError('owner_only')
    boundary=resolver.entity_boundary(conn)
    if boundary.legacy_veto(table,row):
        raise PolicyError('entity_protected')
    return boundary


def _support(resolver,conn,floor,reviews,review_db,refs,policy,lower_us,upper_us,*,extra_domains=(),extra_sensitivity='none'):
    if not isinstance(refs,list) or not 1<=len(refs)<=MAX_SUPPORT:
        raise PolicyError('lineage_identity_incomplete')
    result,seen,common=[],set(),None
    for ref in refs:
        identity=resolve_reference(resolver,conn,ref)
        if _key(identity) in seen: continue
        seen.add(_key(identity))
        qualified,rows=qualify_automatic_message(resolver,conn,floor,identity,reviews,review_db)
        row=rows[_key(identity)]
        stamp=canonical_utc_microseconds(row.get('event_at'))
        from .reconciliation_provenance import native_time_within
        if stamp is None or not lower_us<=stamp<=upper_us or not native_time_within(row,lower_us,upper_us):
            raise PolicyError('evidence_outside_window')
        if identity.table not in policy.search.tables or len(row['content'])>8000:
            raise PolicyError('evidence_outside_form')
        labels=qualified.classifications[0]
        ranks={'none':0,'personal':1,'special':2,'unknown':3}
        labels=labels.model_copy(update={'domains':sorted(set(labels.domains)|set(extra_domains)),
            'sensitivity':max((labels.sensitivity,extra_sensitivity),key=ranks.__getitem__)})
        checked=qualified.model_copy(update={'classifications':[labels]})
        decision=source_message_decision(policy,checked)
        if decision.verdict!='permit': raise PolicyError('evidence_not_permitted')
        clauses=set(decision.matched_allow_clause_ids)
        common=clauses if common is None else common&clauses
        result.append((qualified,rows))
    if not common: raise PolicyError('cross_rule_derivation')
    return result,sorted(common)[0]


def fact_projection(resolver,conn,floor,reviews,review_db,row,policy,lower_us,upper_us):
    if row.get('object_type')!='fact' or row.get('valid_to') is not None:
        raise PolicyError('fact_not_current')
    payload=_json(row['payload_json'],dict)
    from .evidence import SHAREABLE_DISCLOSURES, implicit_labels
    if payload.get('disclosure') not in SHAREABLE_DISCLOSURES:
        raise PolicyError('fact_disclosure_unknown')
    subject,predicate,value=(payload.get(k) for k in ('subject_entity_id','predicate','object_value'))
    if subject not in permit_subjects(conn,contract=ATTESTED_CONTRACT) or predicate not in PREDICATE_TEXT or not isinstance(value,str):
        raise PolicyError('fact_projection_unsupported')
    _unrestricted(resolver,conn,reviews,review_db,'signal_objects',row['object_id'],row)
    domains,sensitivity=implicit_labels(payload,row.get('signal_dimension'))
    prior=reviews._current_in(review_db,row['object_id'])
    if prior is not None:
        # Preserve explicit fact classifications and their exact revision checks.
        checked,_=resolver._qualified_bundle(conn,floor,row['object_id'],reviews,review_db,
                                             contract=ATTESTED_CONTRACT,discloses_sources=True)
        domains=tuple(set(domains)|{d for c in checked.classifications for d in c.domains})
        ranks={'none':0,'personal':1,'special':2,'unknown':3}
        sensitivity=max([sensitivity,*[c.sensitivity for c in checked.classifications]],key=ranks.__getitem__)
    sources,clause=_support(resolver,conn,floor,reviews,review_db,_json(row['source_refs_json'],list),policy,lower_us,upper_us,
                            extra_domains=domains,extra_sensitivity=sensitivity)
    if not any(explicitly_states_claim(rows[_key(q.snapshot.message.identity)]['content'],predicate,value) for q,rows in sources):
        raise PolicyError('fact_not_grounded')
    return Projection('signal_objects',row['object_id'],'fact',f'Owner {PREDICATE_TEXT[predicate]} {value}.',
        {'assertion':'owner_stated'},sources,rows_revision([[row]]),clause)


def _goal_stated(content, goal):
    if not isinstance(goal,str) or not 6<=len(goal)<=8000: return False
    if re.search(r'[?\n"“”]|\b(?:not|never|if|unless|maybe|perhaps)\b',content,re.I): return False
    forms=r"(?:My goal is to |I want to |I plan to |I intend to |I aim to )"
    return bool(re.fullmatch(forms+re.escape(goal.rstrip('.!'))+r'[.!]?',content,re.I)
                or (content==goal and re.match(forms,content,re.I)))


def goal_projection(resolver,conn,floor,reviews,review_db,row,policy,lower_us,upper_us):
    _unrestricted(resolver,conn,reviews,review_db,'user_goals',row['goal_id'],row)
    # Older goals name source+record but not canonical table. Resolve across both
    # source tables only when exactly one native candidate exists.
    refs=[]
    for table in ('conversation_messages','ai_chat_messages'):
        cols={r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        if not {'message_id','source_id'}<=cols: continue
        if conn.execute(f'SELECT 1 FROM {table} WHERE message_id=? AND source_id=?',
                        (row.get('record_id'),row.get('source_id'))).fetchone():
            refs.append(dict(table=table,record_id=row['record_id'],source_id=row['source_id']))
    if len(refs)!=1: raise PolicyError('lineage_identity_ambiguous')
    sources,clause=_support(resolver,conn,floor,reviews,review_db,refs,policy,lower_us,upper_us,extra_domains=('plans',))
    q,rows=sources[0]
    if not _goal_stated(rows[_key(q.snapshot.message.identity)]['content'],row.get('goal_text')):
        raise PolicyError('goal_not_grounded')
    return Projection('user_goals',row['goal_id'],'goal',row['goal_text'],{'status':'stated_intention'},
                      sources,rows_revision([[row]]),clause)


def relationship_projection(resolver,conn,reviews,review_db,row,source_projection):
    if row.get('valid_to') is not None: raise PolicyError('relationship_not_current')
    _unrestricted(resolver,conn,reviews,review_db,'entity_edges',row['edge_id'],row)
    metadata=_json(row['metadata_json'],dict)
    if metadata.get('source_object_id')!=source_projection.record_id:
        raise PolicyError('relationship_lineage_unknown')
    if row['src_entity_id'] not in permit_subjects(conn,contract=ATTESTED_CONTRACT):
        raise PolicyError('relationship_subject_unknown')
    endpoints=conn.execute('SELECT * FROM entities WHERE entity_id=?',(row['dst_entity_id'],)).fetchmany(2)
    if len(endpoints)!=1: raise PolicyError('relationship_endpoint_unknown')
    endpoint=dict(endpoints[0])
    _unrestricted(resolver,conn,reviews,review_db,'entities',row['dst_entity_id'],endpoint)
    target=endpoint.get('canonical_name')
    if source_projection.kind=='goal':
        if row['edge_type']!='pursues' or target!=source_projection.content:
            raise PolicyError('relationship_not_grounded')
    else:
        raise PolicyError('relationship_projection_unsupported')
    return Projection('entity_edges',row['edge_id'],'relationship',f'Owner intends to {target}',
        {'subject':'Owner','relation':'pursues','object':target},source_projection.sources,
        digest({'rows':rows_revision([[row,endpoint]]),'source':source_projection.revision}),source_projection.allow_clause_id)


def load_projection_row(conn,table,record_id):
    keys={'signal_objects':'object_id','user_goals':'goal_id','entity_edges':'edge_id'}
    if table not in keys: raise PolicyError('projection_table_unsupported')
    rows=conn.execute(f'SELECT * FROM {table} WHERE {keys[table]}=?',(record_id,)).fetchmany(2)
    if len(rows)!=1: raise PolicyError('projection_unavailable')
    return dict(rows[0])


def current_revision(conn,table,record_id):
    row=load_projection_row(conn,table,record_id)
    if table!='entity_edges': return rows_revision([[row]])
    metadata=_json(row['metadata_json'],dict)
    goal=load_projection_row(conn,'user_goals',metadata.get('source_object_id'))
    endpoints=conn.execute('SELECT * FROM entities WHERE entity_id=?',(row['dst_entity_id'],)).fetchmany(2)
    if len(endpoints)!=1: raise PolicyError('relationship_endpoint_unknown')
    return digest({'rows':rows_revision([[row,dict(endpoints[0])]]),'source':rows_revision([[goal]])})


def qualify_projection(resolver,conn,floor,reviews,review_db,table,record_id,policy,lower_us,upper_us):
    row=load_projection_row(conn,table,record_id)
    if table=='signal_objects':
        return fact_projection(resolver,conn,floor,reviews,review_db,row,policy,lower_us,upper_us)
    if table=='user_goals':
        return goal_projection(resolver,conn,floor,reviews,review_db,row,policy,lower_us,upper_us)
    metadata=_json(row['metadata_json'],dict)
    source=qualify_projection(resolver,conn,floor,reviews,review_db,'user_goals',metadata.get('source_object_id'),policy,lower_us,upper_us)
    return relationship_projection(resolver,conn,reviews,review_db,row,source)


def candidates(conn, permitted_messages, result_types):
    """Discovery only. Every returned identifier is independently qualified later.

    Iterate stored rows, without a hidden-universe top-k cutoff. Only rows naming
    an already qualified native source are considered for projection.
    """
    ids={identity.record_id for identity in permitted_messages}
    if not ids: return
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'fact' in result_types:
        for row in conn.execute("SELECT object_id,source_refs_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"):
            try: refs=_json(row[1],list)
            except PolicyError: continue
            if any(isinstance(ref,dict) and ref.get('record_id') in ids for ref in refs):
                yield 'signal_objects',row[0]
    goals=set()
    if {'goal','relationship'}&set(result_types) and 'user_goals' in tables:
        for record_id,message_id in conn.execute('SELECT goal_id,record_id FROM user_goals'):
            if message_id in ids:
                goals.add(record_id)
                if 'goal' in result_types: yield 'user_goals',record_id
    if 'relationship' in result_types and goals and 'entity_edges' in tables:
        for row in conn.execute("SELECT edge_id,metadata_json FROM entity_edges WHERE valid_to IS NULL AND edge_type='pursues'"):
            try: metadata=_json(row[1],dict)
            except PolicyError: continue
            if metadata.get('source_object_id') in goals: yield 'entity_edges',row[0]
