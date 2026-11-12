"""Node-local verification of portable custom fact consent."""
from .canonical import PolicyError
from .custom_facts import CustomFacts
from .fact_catalog_v2 import PACKS
import logging


def manifest(pack):
    from ..features.derivation.registry import definition_sha, bundled_pack_dir
    from ..features.derivation.packs import load_scales
    scales = load_scales(bundled_pack_dir())
    spec = {'title': pack.title, 'revision': definition_sha(pack),
            'definition_digest': definition_sha(pack, stamped=False),
            'pack_versions': [pack.version], 'net_subject': 'deny',
            'domains': pack.raw.get('sharing_domains', []), 'predicates': {}}
    for name, pred in pack.predicates.items():
        item = {'value_type': pred.value_type, 'altitude': pred.altitude,
                'sensitivity': pred.sensitivity or pack.sensitivity_class}
        if pred.value_type == 'structured':
            item['value_schema'] = {field: (kind.strip() if isinstance(kind, str) else
                {'enum': scales[kind['scale']]} if isinstance(kind, dict) and 'scale' in kind else kind)
                for field, kind in pred.value_schema.items()}
        if pred.value_type == 'enum':
            item['values'] = pred.values
        spec['predicates'][name] = item
    return spec


def installed_manifests(*, catalog_limit=False):
    from ..features.derivation.custom_packs import load_custom_packs
    from .custom_facts import validate_custom
    result = {}
    for oid, pack in sorted(load_custom_packs().items()):
        try:
            candidate = {**result, oid: manifest(pack)}
            # The wire catalog has a combined size bound. An existing grant
            # checks its own selected definitions, so another installation
            # filling the picker cannot revoke or hide its runtime schema.
            validate_custom(candidate if catalog_limit else {oid: candidate[oid]})
        except (ValueError, TypeError, KeyError):
            # An unsupported/oversized portable schema cannot break other grants.
            logging.getLogger(__name__).warning('Custom pack %s is outside the sharing catalog limits', oid)
            continue
        result = candidate
    return result


def current_packs():
    return {**PACKS, **installed_manifests()}


def policy_packs(declaration):
    if not isinstance(declaration,CustomFacts):return PACKS
    current=current_packs()
    # Only selected custom packs affect this grant. Installing an unrelated pack
    # cannot invalidate it, nor can it grow its selected ontologies.
    for item in declaration.ontologies:
        if item.ontology_id in declaration.custom_packs:
            if current.get(item.ontology_id)!=declaration.custom_packs[item.ontology_id]:
                raise PolicyError('fact_ontology_unsupported')
    return {**PACKS, **declaration.custom_packs}
