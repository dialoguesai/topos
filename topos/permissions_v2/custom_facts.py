"""Portable, owner-selected custom pack manifests. Mirrored by the control plane.

No process-global registration: each signed grant carries its exact vocabulary.
The node additionally checks that custom definitions still match installed packs.
"""
import json
import re
from typing import Annotated, Literal
from pydantic import Field, model_validator
from .canonical import digest
from .contract import Hash, Identifier, StrictModel
from .expanded_facts import Assertion, CATALOG_DIGEST as BASE_DIGEST, SEMANTICS, catalog as base_catalog
from .fact_catalog_v2 import PACKS

VERSION = 'selective-facts/v3'
VOCABULARY = 'fact-ontology-vocabulary/v3'
DOMAINS = {'work','plans','hobbies','health','family','finance','relationships','home'}
TYPES = {'string','text','number','boolean','date','date_or_approx','date_range_or_approx',
         'recurring_date','entity_ref','entity_refs','entity_ref_or_self','entity_ref_or_string',
         'person_ref','person_refs','person_ref_or_self'}
NAME = re.compile(r'^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$')


def validate_custom(packs):
    if not isinstance(packs,dict) or not 1 <= len(packs) <= 20:
        raise ValueError('custom pack count must be 1..20')
    if len(json.dumps(packs,allow_nan=False)) > 128000:
        raise ValueError('custom manifests too large')
    seen = {p for spec in SEMANTICS.values() for p in spec['predicates']}
    for oid,spec in packs.items():
        if not isinstance(oid,str) or not oid.startswith('custom.') or not NAME.fullmatch(oid) or oid in PACKS or not isinstance(spec,dict):
            raise ValueError('custom pack identity collision')
        if set(spec) != {'title','revision','pack_versions','domains','predicates','net_subject','definition_digest'}:
            raise ValueError('unsupported custom manifest')
        if (not isinstance(spec['title'],str) or not 1 <= len(spec['title']) <= 120
                or not spec['title'].isprintable() or spec['net_subject'] != 'deny' or not isinstance(spec['revision'],str)
                or not re.fullmatch(r'[0-9a-f]{64}',spec['revision']) or not isinstance(spec['definition_digest'],str)
                or not re.fullmatch(r'[0-9a-f]{64}',spec['definition_digest'])):
            raise ValueError('invalid custom pack metadata')
        versions=spec['pack_versions']; domains=spec['domains']; predicates=spec['predicates']
        if (not isinstance(versions,list) or len(versions)!=1 or not isinstance(versions[0],str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,39}',versions[0])):
            raise ValueError('invalid custom pack version')
        if (not isinstance(domains,list) or len(domains)>8 or any(not isinstance(d,str) or d not in DOMAINS for d in domains)
                or len(set(domains))!=len(domains)):
            raise ValueError('declare valid sharing_domains')
        if not isinstance(predicates,dict) or not 1 <= len(predicates) <= 50:
            raise ValueError('invalid custom predicates')
        for name,pred in predicates.items():
            if not isinstance(name,str) or not name.startswith('custom.') or not NAME.fullmatch(name) or name in seen:
                raise ValueError('predicate identity collision')
            seen.add(name)
            if (not isinstance(pred,dict) or set(pred)-{'value_type','value_schema','values','altitude','sensitivity'}
                    or not isinstance(pred.get('value_type'),str) or pred.get('value_type') not in TYPES | {'structured','enum'}
                    or not isinstance(pred.get('altitude'),str) or pred.get('altitude') != 'stated'
                    or not isinstance(pred.get('sensitivity'),str) or pred.get('sensitivity') not in {'none','personal','special'}):
                raise ValueError('unsupported custom predicate')
            if pred['value_type']=='structured':
                schema=pred.get('value_schema')
                if not isinstance(schema,dict) or not 1 <= len(schema) <= 50:
                    raise ValueError('custom structured schema required')
                for field,kind in schema.items():
                    if not re.fullmatch(r'[a-z][a-z0-9_]{0,99}',field):
                        raise ValueError('invalid field name')
                    if isinstance(kind,str) and kind in TYPES:continue
                    values=kind.get('enum') if isinstance(kind,dict) and set(kind)=={'enum'} else kind
                    _enum(values)
            elif 'value_schema' in pred:
                raise ValueError('unexpected schema')
            if pred['value_type']=='enum':_enum(pred.get('values'))
            elif 'values' in pred:raise ValueError('unexpected enum')
    return packs


def _enum(values):
    if (not isinstance(values,list) or not 1 <= len(values) <= 100
            or any(not isinstance(v,str) or not 1 <= len(v) <= 200 for v in values)
            or len(set(values))!=len(values)):
        raise ValueError('unsupported enum')


def catalog_digest(packs):
    return digest({'vocabulary':VOCABULARY,'base':BASE_DIGEST,'custom_packs':packs})


class CustomOntologySelection(StrictModel):
    ontology_id: Identifier
    revision: Identifier
    predicates: Annotated[list[Identifier],Field(min_length=1,max_length=50)]


class CustomFacts(StrictModel):
    version: Literal['selective-facts/v3']
    vocabulary: Literal['fact-ontology-vocabulary/v3']
    catalog_digest: Hash
    custom_packs: dict
    ontologies: Annotated[list[CustomOntologySelection],Field(min_length=1,max_length=50)]
    assertions: Annotated[list[Assertion],Field(min_length=1,max_length=2)]
    excluded_fact_ids: Annotated[list[Identifier],Field(max_length=500)]

    @model_validator(mode='after')
    def closed_consent(self):
        validate_custom(self.custom_packs)
        if self.catalog_digest!=catalog_digest(self.custom_packs):
            raise ValueError('custom catalog digest mismatch')
        for values in ([o.ontology_id for o in self.ontologies],self.assertions,self.excluded_fact_ids):
            if len(set(values))!=len(values):raise ValueError('duplicate consent')
        for item in self.ontologies:
            spec=self.custom_packs.get(item.ontology_id) or SEMANTICS.get(item.ontology_id)
            if spec is None or item.revision!=spec['revision'] or item.predicates!=sorted(spec['predicates']):
                raise ValueError('unsupported ontology vocabulary')
        return self


def catalog(packs):
    validate_custom(packs)
    result=base_catalog()
    result.update(version=VERSION,vocabulary=VOCABULARY,catalog_digest=catalog_digest(packs),custom_packs=packs)
    result['ontologies'] += [{'ontology_id':oid,'title':spec['title'],
        'description':'Your custom fact pack. Source permissions and global protections apply.',
        'available':True,'reason':None,'revision':spec['revision'],'predicates':sorted(spec['predicates'])}
        for oid,spec in sorted(packs.items())]
    return result


def selection(data,ontology_ids,excluded_fact_ids=(),*,assertions):
    lookup={item['ontology_id']:item for item in data['ontologies']}
    return {'version':VERSION,'vocabulary':VOCABULARY,'catalog_digest':data['catalog_digest'],
        'custom_packs':data['custom_packs'],
        'ontologies':[{k:lookup[oid][k] for k in ('ontology_id','revision','predicates')} for oid in ontology_ids],
        'assertions':list(assertions),'excluded_fact_ids':list(excluded_fact_ids)}
