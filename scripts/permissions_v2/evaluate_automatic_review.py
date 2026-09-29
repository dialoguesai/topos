"""Synthetic local-model acceptance probe; writes no owner data and changes no grant.

Run with repository PYTHONPATH. Emits only fixture names, labels and failures.
Exit nonzero if any independently authored expected result fails.
"""
import asyncio
import json
from types import SimpleNamespace

from tests.permissions_v2.automatic_review_cases import CASES, verdict
from topos.permissions_v2.automatic_message_review import assess
from topos.permissions_v2.evidence import EvidenceBinding, EvidenceIdentity, EvidenceRevision
from topos.permissions_v2.shadow_labeler_local import open_transport


async def main():
    binding = EvidenceBinding(environment_id='synthetic-eval', node_id='node', resource_id='resource', owner_id='owner')
    evidence = EvidenceRevision(identity=EvidenceIdentity(binding=binding, table='conversation_messages',
        record_id='synthetic', source_id='imessage', dataset_kind='row_dataset', dataset_id='synthetic'), revision='0'*64)
    transport = open_transport(base_url='http://127.0.0.1:11434')
    failures = 0
    try:
        for case in CASES:
            prepared = {'snapshot':SimpleNamespace(message=evidence), 'input':{
                'target':case['target'], 'before':case.get('before',[]), 'after':case.get('after',[]),
                'protected_terms':case.get('protected_terms',[])}}
            try:
                labels = await assess(prepared, transport=transport)
                errors = verdict(case, labels)
                print(json.dumps({'case':case['id'], 'errors':errors, 'labels':labels.model_dump(exclude={'evidence'})}), flush=True)
            except Exception as exc:
                errors = ['unresolved']
                print(json.dumps({'case':case['id'], 'errors':errors, 'exception':type(exc).__name__}), flush=True)
            failures += bool(errors)
    finally:
        await transport.client.aclose()
    print(json.dumps({'cases':len(CASES),'failed':failures}), flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
