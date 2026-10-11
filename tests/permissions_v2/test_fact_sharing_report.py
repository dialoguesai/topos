"""Small matched, synthetic node benchmark; not the production activation report."""
from copy import deepcopy
import json
import os
from pathlib import Path
import statistics
import time

from tests.permissions_v2.test_knowledge_search import add_fact,node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture,owner
from tests.permissions_v2.test_answer_release import _service,_ask,_finished
from topos.permissions_v2.registry import parse_policy
from topos.permissions_v2.selective_facts import selection,catalog
from topos.permissions_v2.share_counts import count


def percentile(values,p):
    return sorted(values)[max(0,int(len(values)*p+.999)-1)]


def test_matched_work_fact_report(legacy,tmp_path,monkeypatch):
    monkeypatch.setenv('TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED','true')
    fid=add_fact(legacy,disclosure='scoped')
    node,_=node_for(legacy,tmp_path,monkeypatch,answers='only',selective_facts=selection(['work.career']))
    node.rebuild()
    with owner():preview=count(node.corpus.resolver,node.corpus.reviews,parse_policy(node.search_raw),now=node.now[0])
    assert preview['fact_preview']['items'][0]['eligible']
    sample={}
    # Same question, native evidence, non-fact outputs, source window, machine review,
    # and deterministic writer; only signed grant-specific fact consent differs.
    original=deepcopy(node.search_raw)
    records=deepcopy(original);records['policy_version_id']='records-version';records['search'].pop('answers')
    records['binding']['grant_id']='grant-records';records['binding']['assignment_id']='assignment-records'
    node.activate(records);node.rebuild()
    for mode in ('facts_on','facts_off'):
        if mode=='facts_off':
            raw=deepcopy(original);raw['policy_version_id']='facts-off-version'
            raw['search']['selective_facts']=selection(['work.career'],[fid]);node.activate(raw,generation=2)
            records['policy_version_id']='records-off-version';records['search']['selective_facts']=selection(['work.career'],[fid])
            node.activate(records,generation=2);node.rebuild()
        service=_service(node,'The owner is working on a synthetic task [1].')
        search_ms=[];answer_ms=[];answered=0;fact_deliveries=0
        try:
            for _ in range(12):
                start=time.perf_counter();result,error=node.search_request('Synthetic message',k=10,grant_id='grant-records')
                search_ms.append((time.perf_counter()-start)*1000)
                assert error is None
                fact_deliveries+=sum(r['kind']=='fact' for r in result['records'])
                start=time.perf_counter();job=_ask(node,service,'Synthetic message');body=_finished(node,service,job)
                answer_ms.append((time.perf_counter()-start)*1000)
                answered+=body.get('outcome')=='answered'
        finally:service.close()
        sample[mode]={'requests':12,'answered':answered,'fact_deliveries':fact_deliveries,
            'search_ms':{'p50':round(statistics.median(search_ms),2),'p95':round(percentile(search_ms,.95),2)},
            'answer_ms':{'p50':round(statistics.median(answer_ms),2),'p95':round(percentile(answer_ms,.95),2)}}
    assert sample['facts_on']['fact_deliveries']==12 and sample['facts_off']['fact_deliveries']==0
    report={'version':'selective-facts-evaluation/v1','activation_verdict':'not_ready',
        'scope':'one synthetic owner, one distinct owner-stated work fact, signed node harness; no deployed CP or hosted model',
        'adjudication':'exact owner-native statement and evidence protections; no independent human review',
        'distinct_candidates':1,'ontology':'work.career','predicate':'works_on','assertion':'owner_stated',
        'support_precision':{'qualified_and_supported':1,'evaluated_distinct_facts':1},
        'unsupported_claims':0,'privacy_leaks':0,'unselected_fact_deliveries':0,
        'matched_writer':'deterministic test writer, identical generation function and response',
        'facts_off_method':'signed grant-specific exclusion of the one candidate; activation remains on for both arms',
        'comparison':sample,'usefulness_delta':sample['facts_on']['answered']-sample['facts_off']['answered'],
        'catalog':catalog(),
        'pending':['six directed pair full privacy release rig','representative per-predicate evaluation',
                   'same production writer matched answer evaluation','independent adjudication','declared workload latency budgets']}
    if os.environ.get('FACT_SHARING_REPORT'):
        path=Path(os.environ['FACT_SHARING_REPORT']);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,indent=2)+'\n')
