"""All-pack fact projection inside an explicitly signed v2 selection.

Stored extraction values, never payload metadata or raw citations, are released.
The label distinguishes deterministic owner statements from extracted/inferred
claims; selecting an ontology is not an assertion that its model was correct.
"""
import json
import math
from .canonical import PolicyError
from .evidence import _deleted, _json, _key
from .expanded_facts import PACKS
from .selective_facts import authorize
from .custom_fact_runtime import policy_packs, current_packs
from .fact_eligibility import (canonical_utc_microseconds, fact_current_from_microseconds,
                               stated_day_elapsed_microseconds)
from .identity import ATTESTED_CONTRACT, permit_subjects
from .selective_facts import enabled_ontologies


def _value(value, schema=None, *, depth=0):
    if depth>8:
        raise PolicyError('fact_projection_unsupported')
    if isinstance(schema,dict) and 'enum' in schema and value not in schema['enum']:
        raise PolicyError('fact_projection_unsupported')
    if isinstance(schema,list) and value not in schema:
        raise PolicyError('fact_projection_unsupported')
    if isinstance(value,dict):
        if not isinstance(schema,dict) or not value or set(value)-set(schema):
            raise PolicyError('fact_projection_unsupported')
        return {k:_value(v,schema[k],depth=depth+1) for k,v in sorted(value.items())}
    if isinstance(value,list):
        if not value or len(value)>50:
            raise PolicyError('fact_projection_unsupported')
        return [_value(v,depth=depth+1) for v in value]
    if type(value) is str:
        if not value.strip() or len(value)>2000 or any(ord(c)<32 and c not in '\n\t' for c in value):
            raise PolicyError('fact_projection_unsupported')
        return value
    if type(value) in (int,float,bool) and (not isinstance(value,float) or math.isfinite(value)):
        return value
    raise PolicyError('fact_projection_unsupported')


REFERENCE_TYPES = frozenset({'entity_ref','entity_refs','entity_ref_or_self','entity_ref_or_string',
                             'person_ref','person_refs','person_ref_or_self'})


def reference_field(schema):
    return isinstance(schema,str) and schema in REFERENCE_TYPES


def value_fields(value,schema):
    # DerivationWriter binds person names beside their resolved ids. Those
    # sidecars participate in protections/revisions, but never leave the node.
    allowed={field+suffix for field,kind in schema.items() if reference_field(kind)
             for suffix in ('_entity_id','_entity_ids')}
    if set(value)-set(schema)-allowed:
        raise PolicyError('fact_projection_unsupported')
    return {key:item for key,item in value.items() if key in schema}


def stored_value(oid,payload,*,packs=None):
    """Only the value schema frozen into this consent contract may leave the node."""
    pred=(PACKS if packs is None else packs)[oid]['predicates'].get(payload.get('predicate'))
    if pred is None:  # exact legacy producer mapping was already checked
        return _value(payload.get('object_value'))
    if pred['value_type']=='structured':
        value=payload.get('value_struct')
        if not isinstance(value,dict):
            raise PolicyError('fact_projection_unsupported')
        schema=pred.get('value_schema',{})
        return _value(value_fields(value,schema),schema)
    value=payload.get('object_value')
    if type(value) is not str or payload.get('value_struct') is not None:
        raise PolicyError('fact_projection_unsupported')
    return _value(value,pred.get('values') if pred['value_type']=='enum' else None)


def project(resolver,conn,floor,reviews,review_db,row,policy,lower_us,upper_us):
    from . import switches
    from .knowledge_projections import Projection, _support, _unrestricted, load_projection_row, rows_revision
    from .native_claim_grounding import explicitly_states_claim
    from .predicate_classes import scalar
    from .knowledge_projections import PREDICATE_TEXT
    from .permitted_derivation import check_lineage
    if not switches.on(switches.SELECTIVE_FACTS):
        raise PolicyError('selective_facts_disabled')
    packs=policy_packs(policy.search.selective_facts)
    enabled=enabled_ontologies(conn)
    subjects=permit_subjects(conn,contract=ATTESTED_CONTRACT)
    artifacts=[]; refs=[]; visiting=set(); visited=set(); domains=set(); sensitivities=['none']; pending_days=[]
    def walk(current,depth):
        if depth>16 or len(visited)>=128 or current['object_id'] in visiting:
            raise PolicyError('fact_lineage_unsupported')
        if current['object_id'] in visited:
            return
        if current.get('object_type')!='fact' or 'valid_to' not in current or current['valid_to'] is not None or _deleted(current):
            raise PolicyError('fact_not_current')
        start=fact_current_from_microseconds(current.get('valid_from'),semantics='stated_day_v1')
        if start is None or start>upper_us:
            day=current.get('valid_from')
            if stated_day_elapsed_microseconds(day) is None:
                raise PolicyError('fact_not_current')
            # The production pack writer records only its evidence's day. An
            # exact, already-permitted source instant on that same day proves
            # it has begun; otherwise retain the conservative elapsed-day rule.
            pending_days.append(day)
        payload=_json(current['payload_json'],dict)
        oid=authorize(current,payload,policy)
        if oid not in enabled:
            raise PolicyError('fact_ontology_off')
        if not isinstance(payload.get('subject_entity_id'),str):
            raise PolicyError('fact_projection_unsupported')
        if oid not in PACKS and payload.get('subject_entity_id') not in subjects:
            raise PolicyError('fact_projection_unsupported')
        if payload.get('subject_entity_id') not in subjects:
            subject_entity=_entity(conn,payload.get('subject_entity_id'))
            if not subject_entity or not subject_entity.get('canonical_name'):
                raise PolicyError('fact_projection_unsupported')
            domains.add('relationships');sensitivities.append('personal')
        for entity in linked_entities(conn,current):
            if _deleted(entity):
                raise PolicyError('entity_protected')
            _value(entity.get('canonical_name'))
            _unrestricted(resolver,conn,reviews,review_db,'entities',entity['entity_id'],entity)
        _unrestricted(resolver,conn,reviews,review_db,'signal_objects',current['object_id'],current)
        stored_value(oid,payload,packs=packs)
        spec=packs[oid];domains.update(spec['domains'])
        pred=spec['predicates'].get(payload['predicate'])
        sensitivity=pred['sensitivity'] if pred else ('special' if oid.startswith('health.') else 'personal')
        # Payload labels and an explicit owner review can only raise the floor.
        ranks={'none':0,'personal':1,'special':2,'unknown':3}
        raw_sensitivity=payload.get('sensitivity',sensitivity)
        if not isinstance(raw_sensitivity,str) or raw_sensitivity not in ranks:
            raise PolicyError('fact_classification_unknown')
        sensitivities.extend((sensitivity,raw_sensitivity))
        prior=reviews._current_in(review_db,current['object_id'])
        if prior is not None:
            from .evidence import OwnerEvidenceReview
            snapshot,_=resolver._snapshot(conn,floor,current['object_id'],enforce_floor=True)
            if not isinstance(prior,OwnerEvidenceReview) or prior.owner_id!=resolver.binding.owner_id or prior.snapshot!=snapshot:
                raise PolicyError('review_stale')
            for classification in prior.classifications:
                if classification.subject_entity_ids!=['self'] or classification.authorship!='owner_authored':
                    raise PolicyError('fact_projection_unsupported')
                domains.update(classification.domains);sensitivities.append(classification.sensitivity)
        visiting.add(current['object_id'])
        children=_json(current['source_refs_json'],list)
        if not children or len(children)>128:
            raise PolicyError('lineage_identity_incomplete')
        for ref in children:
            if not isinstance(ref,dict):
                raise PolicyError('lineage_identity_incomplete')
            if ref.get('table')=='signal_objects':
                if set(ref)!={'table','record_id'} or not isinstance(ref.get('record_id'),str):
                    raise PolicyError('lineage_identity_incomplete')
                walk(load_projection_row(conn,'signal_objects',ref['record_id']),depth+1)
            else:
                refs.append(ref)
        visiting.remove(current['object_id']);visited.add(current['object_id']);artifacts.append(current)
    walk(row,0)
    payload=_json(row['payload_json'],dict);oid=authorize(row,payload,policy)
    sensitivity=max(sensitivities,key={'none':0,'personal':1,'special':2,'unknown':3}.__getitem__)
    if sensitivity=='unknown':
        raise PolicyError('fact_classification_unknown')
    # De-duplicate complete identities only; resolve_reference still checks
    # ambiguity, native authorship, table/source/dataset binding and provenance.
    refs=list({json.dumps(ref,sort_keys=True):ref for ref in refs}.values())
    sources,clause=_support(resolver,conn,floor,reviews,review_db,refs,policy,lower_us,upper_us,
                           extra_domains=sorted(domains),extra_sensitivity=sensitivity,projection_only=True)
    instants=[native[_key(q.snapshot.message.identity)].get('event_at') for q,native in sources]
    if any(not any(isinstance(event,str) and event.startswith(day+'T')
                       and canonical_utc_microseconds(event) is not None for event in instants) for day in pending_days):
        raise PolicyError('fact_not_current')
    for artifact in artifacts:
        check_lineage(_json(artifact['payload_json'],dict),sources)
    predicate=payload['predicate']; value=stored_value(oid,payload,packs=packs)
    atomic=scalar(predicate,payload)
    stated=(payload.get('subject_entity_id') in subjects and not any(a.get('altitude')=='inferred' for a in artifacts) and row.get('altitude') in (None,'stated') and predicate in PREDICATE_TEXT and atomic is not None
        and any(explicitly_states_claim(native[_key(q.snapshot.message.identity)]['content'],predicate,atomic)
                for q,native in sources))
    assertion='owner_stated' if stated else 'inferred'
    if assertion not in policy.search.selective_facts.assertions:
        raise PolicyError('fact_assertion_unselected')
    # Never upgrade a derived dependency to an owner statement just because the
    # root happens to match a literal source sentence.
    if any(a.get('altitude')=='inferred' for a in artifacts) and 'inferred' not in policy.search.selective_facts.assertions:
        raise PolicyError('fact_assertion_unselected')
    subject='owner' if payload.get('subject_entity_id') in subjects else _entity(conn,payload['subject_entity_id'])['canonical_name']
    schema=packs[oid]['predicates'].get(predicate,{}).get('value_schema')
    value=display_value(conn,value,schema,payload.get("value_struct"))
    content=(f'Owner {PREDICATE_TEXT[predicate]} {atomic}.' if stated else
        f'Extracted or inferred fact about {subject} ({packs[oid]["title"]}, {predicate}): '
        + json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(', ', ': ')))
    if len(content)>8000:
        raise PolicyError('fact_projection_unsupported')
    return Projection('signal_objects',row['object_id'],'fact',content,
        {'assertion':assertion,'ontology_id':oid,'predicate':predicate},sources,fact_revision(conn,row),clause,revision_contract='expanded-fact/v1')


def _entity(conn,entity_id):
    if not isinstance(entity_id,str) or not entity_id or entity_id=='self':
        return None
    rows=conn.execute('SELECT * FROM entities WHERE entity_id=?',(entity_id,)).fetchmany(2)
    return dict(rows[0]) if len(rows)==1 else None


def _entity_values(value,schema):
    if reference_field(schema) and isinstance(value,str):
        yield value
    elif reference_field(schema) and isinstance(value,list):
        for item in value:
            if isinstance(item,str):yield item
    elif isinstance(value,dict) and isinstance(schema,dict):
        for key,item in value.items():
            yield from _entity_values(item,schema.get(key))


def linked_entities(conn,row):
    payload=_json(row['payload_json'],dict)
    spec=current_packs().get(row.get('ontology_id'),{}).get('predicates',{}).get(payload.get('predicate'),{})
    identity_values=(payload.get('subject_entity_id'),payload.get('object_entity_id'))
    if any(x is not None and not isinstance(x,str) for x in identity_values):
        raise PolicyError('fact_projection_unsupported')
    ids=set(identity_values)
    ids.update(_entity_values(payload.get('value_struct'),spec.get('value_schema')))
    struct=payload.get('value_struct')
    if isinstance(struct,dict):
        for field,kind in spec.get('value_schema',{}).items():
            if not reference_field(kind):continue
            for suffix in ('_entity_id','_entity_ids'):
                key=field+suffix
                if key not in struct:continue
                bound=struct[key] if suffix=='_entity_ids' else [struct[key]]
                if not isinstance(bound,list) or not bound or any(not _entity(conn,item) for item in bound):
                    raise PolicyError('fact_projection_unsupported')
                ids.update(bound)
    return [found for entity_id in sorted(x for x in ids if isinstance(x,str))
            if (found:=_entity(conn,entity_id.removeprefix('id:'))) is not None]


def fact_revision(conn,row,*,boundary=None):
    """Bind all fact dependencies and named entities, including their current protections."""
    from .knowledge_projections import load_projection_row,rows_revision
    facts={};entities={};visiting=set()
    def walk(current,depth):
        fid=current['object_id']
        if fid in visiting or depth>16 or len(facts)>=128:
            raise PolicyError('fact_lineage_unsupported')
        if fid in facts:return
        visiting.add(fid)
        for ref in _json(current['source_refs_json'],list):
            if isinstance(ref,dict) and ref.get('table')=='signal_objects':
                if set(ref)!={'table','record_id'} or not isinstance(ref.get('record_id'),str):
                    raise PolicyError('lineage_identity_incomplete')
                walk(load_projection_row(conn,'signal_objects',ref.get('record_id')),depth+1)
        visiting.remove(fid);facts[fid]=current
        for entity in linked_entities(conn,current):entities[entity['entity_id']]=entity
    walk(row,0)
    if boundary:
        for table,values in (('signal_objects',facts),('entities',entities)):
            for current in values.values():
                if boundary.legacy_veto(table,current):raise PolicyError('entity_protected')
    groups=[list(facts.values()),list(entities.values())]
    custom_ids=sorted({fact.get('ontology_id') for fact in facts.values()
                       if fact.get('ontology_id') and fact.get('ontology_id') not in PACKS})
    if custom_ids:
        packs=current_packs()
        groups.append([{'ontology_id':oid,'manifest':packs.get(oid)} for oid in custom_ids])
    return rows_revision(groups)


def display_value(conn,value,schema,bindings=None):
    if reference_field(schema) and isinstance(value,str):
        found=_entity(conn,value.removeprefix("id:"))
        return found['canonical_name'] if found and found.get('canonical_name') else value
    if reference_field(schema) and isinstance(value,list):
        return [display_value(conn,item,schema) for item in value]
    if isinstance(value,dict) and isinstance(schema,dict):
        out={}
        for key,item in value.items():
            kind=schema.get(key)
            bound=(bindings or {}).get(key+'_entity_id') if reference_field(kind) else None
            bound_many=(bindings or {}).get(key+'_entity_ids') if reference_field(kind) else None
            if bound:out[key]=_entity(conn,bound)['canonical_name']
            elif bound_many:out[key]=[_entity(conn,eid)['canonical_name'] for eid in bound_many]
            else:out[key]=display_value(conn,item,kind)
        return out
    return value
