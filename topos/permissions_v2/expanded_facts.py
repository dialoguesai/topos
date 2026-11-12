"""Explicit all-ontology consent. v1 policies retain their original meaning.

A stored extraction is labelled inferred unless the original deterministic
statement grammar proves it. Selection never certifies an extraction as truth.
Record protections and qualification of every supporting source remain mandatory.
"""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from .canonical import PolicyError, digest
from .contract import Hash, Identifier, StrictModel
from .fact_catalog_v2 import PACKS

VERSION = 'selective-facts/v2'
VOCABULARY = 'fact-ontology-vocabulary/v2'
# Source-bound legacy FactStore predicates have no pack provenance. Only these
# exact producer/predicate pairs may use this migration mapping.
LEGACY = {'works_at':'work.career','worked_at':'work.career','works_on':'work.career',
          'role_is':'work.career','certified_in':'expertise.knowledge','studied_at':'expertise.knowledge',
          'skilled_in':'expertise.knowledge','prefers':'behavior.preferences',
          'member_of':'community.membership','lives_in':'places.mobility',
          'practices':'health.lifestyle','training_for':'health.lifestyle'}
SEMANTICS = {oid: {**spec, 'predicates': sorted(set(spec['predicates']) |
             {predicate for predicate, pack in LEGACY.items() if pack == oid})} for oid,spec in PACKS.items()}
CATALOG_DIGEST = digest({'vocabulary':VOCABULARY, 'packs':PACKS, 'legacy':LEGACY,
    'projection':'stored-extraction/v1', 'disclosure':'producer-default-is-not-owner-mark/v1',
    'assertions':['owner_stated','inferred']})
Assertion = Literal['owner_stated','inferred']

class OntologySelection(StrictModel):
    ontology_id: Identifier
    revision: Identifier
    predicates: Annotated[list[Identifier],Field(min_length=1,max_length=50)]

    @model_validator(mode='after')
    def exact_semantics(self):
        spec=SEMANTICS.get(self.ontology_id)
        if (spec is None or self.revision!=spec['revision'] or len(set(self.predicates))!=len(self.predicates)
                or self.predicates!=spec['predicates']):
            raise ValueError('unsupported ontology vocabulary')
        return self

class ExpandedFacts(StrictModel):
    version: Literal['selective-facts/v2']
    vocabulary: Literal['fact-ontology-vocabulary/v2']
    catalog_digest: Hash
    ontologies: Annotated[list[OntologySelection],Field(min_length=1,max_length=50)]
    assertions: Annotated[list[Assertion],Field(min_length=1,max_length=2)]
    excluded_fact_ids: Annotated[list[Identifier],Field(max_length=500)]

    @model_validator(mode='after')
    def closed_consent(self):
        if self.catalog_digest!=CATALOG_DIGEST:
            raise ValueError('unsupported fact catalog')
        for values in ([x.ontology_id for x in self.ontologies], self.assertions, self.excluded_fact_ids):
            if len(values)!=len(set(values)):
                raise ValueError('duplicate fact consent')
        return self


def selection(ontology_ids, excluded_fact_ids=(), *, assertions):
    return {'version':VERSION,'vocabulary':VOCABULARY,'catalog_digest':CATALOG_DIGEST,
        'ontologies':[{'ontology_id':oid,'revision':SEMANTICS[oid]['revision'],
                      'predicates':list(SEMANTICS[oid]['predicates'])} for oid in ontology_ids],
        'assertions':list(assertions),'excluded_fact_ids':list(excluded_fact_ids)}


def catalog():
    return {'version':VERSION,'vocabulary':VOCABULARY,'catalog_digest':CATALOG_DIGEST,
        'ontologies':[{'ontology_id':oid,'title':spec['title'],
            'description':'Stated, extracted and inferred facts about you and others. Source permissions and global protections still apply.',
            'available':True,'reason':None,'revision':spec['revision'],'predicates':list(spec['predicates'])}
            for oid,spec in SEMANTICS.items()]}


def ontology_of(row,payload):
    oid,version=row.get('ontology_id'),row.get('ontology_version')
    predicate=payload.get('predicate')
    if not isinstance(predicate,str):
        raise PolicyError('fact_ontology_unknown')
    if oid is None and version is None:
        if (row.get('extractor_version')!='fact_store_v1' or predicate not in LEGACY
            or any(payload.get(k) is not None for k in ('pack','ontology_id','ontology_version'))
            or any(x not in (None,'stated') for x in (row.get('altitude'),payload.get('altitude')))):
            raise PolicyError('fact_ontology_unknown')
        return LEGACY[predicate]
    spec=PACKS.get(oid) if isinstance(oid,str) else None
    pred=spec['predicates'].get(predicate) if spec and isinstance(predicate,str) else None
    extractor=payload.get('extractor')
    if (pred is None or version not in spec['pack_versions'] or payload.get('pack')!=oid
        or payload.get('ontology_id',oid)!=oid or payload.get('ontology_version',version)!=version
        or not isinstance(extractor,dict) or extractor.get('pack_version')!=version
        or row.get('altitude')!=pred['altitude'] or payload.get('altitude')!=pred['altitude']):
        raise PolicyError('fact_ontology_unsupported')
    return oid


def authorize(row,payload,policy):
    declaration=policy.search.selective_facts
    if row['object_id'] in declaration.excluded_fact_ids:
        raise PolicyError('fact_excluded_from_grant')
    oid=ontology_of(row,payload)
    if not any(x.ontology_id==oid and payload['predicate'] in x.predicates for x in declaration.ontologies):
        raise PolicyError('fact_ontology_unselected')
    # The pack writer labels every extraction owner_only by default, including
    # the ones the owner explicitly chooses to share. v2 consent opts those in;
    # independent owner_only_records, Off-limits and opt-outs still veto at read.
    if payload.get('disclosure') not in ('scoped','owner_only'):
        raise PolicyError('fact_disclosure_unknown')
    role=payload.get('actor_role','authored')
    if not ((payload.get('asserted_by')=='owner' and role=='authored')
            or (payload.get('asserted_by')=='extracted:synthesis' and role=='synthesis'
                and row.get('altitude')=='inferred')):
        raise PolicyError('fact_not_owner_stated')
    if payload.get('lineage') is not None and not isinstance(payload['lineage'],dict):
        raise PolicyError('fact_lineage_unsupported')
    if payload.get('dependencies'):
        raise PolicyError('fact_lineage_unsupported')
    # DR-62: a fact read WITH other records (`context_refs`: the turns before the owner's
    # message) may describe what only the other person's turn said. Those records are not
    # in source_refs_json, so nothing on this side qualifies or protects them. Such a fact
    # is not shared at all, as a root or as a dependency, until a ruling says how.
    if payload.get('context_refs') not in (None,[]):
        raise PolicyError('fact_lineage_unsupported')
    return oid
