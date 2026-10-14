import copy
import pytest
from topos.features.derivation.bench_lineup import purpose_categories, select_lineup

@pytest.mark.parametrize("text,category", [
    ("Could you review my application?", "feedback"),
    ("Help debug the irrigation controller", "building"),
    ("Introduce me to a local organizer", "introductions"),
    ("Practice a difficult conversation", "practice"),
    ("I need a ride to the clinic", "practical"),
    ("Advance the project", "other"),
])
def test_task_categories(text, category):
    assert category in {c['id'] for c in purpose_categories({'label': text})}

def test_peer_occupation_does_not_define_help_category():
    role={'label':'Discuss my project', 'evidence':[{'speaker':'peer','excerpt':'I teach and review articles'}]}
    assert purpose_categories(role)[0]['id']=='other'

def test_categories_are_tentative_and_multilabel():
    cats=purpose_categories({'label':'Practice how to review a proposal'})
    assert {c['id'] for c in cats}=={'feedback','practice'}
    assert all(c['basis']=='owner_task_wording' and c['matched_cues'] for c in cats)

def role(i, person='p', label='Review my proposal', basis='prior_topic_discussion', ready=False):
    return {'role_shape_id':str(i),'label':label,'candidates':[{'person_id':person,'basis':basis,
        'readiness':{'status':'unknown'},'bench_qualification':{'status':'ready' if ready else 'candidate'}}]}

def test_distinct_people_and_tasks_survive_a_dense_correspondent():
    roles=[role(i) for i in range(10)]+[role(10,'q','Practice my presentation'),role(11,'p','Introduce me to someone')]
    selected,coverage=select_lineup(roles,limit=3)
    assert [r['role_shape_id'] for r in selected]==['0','10','11']
    assert coverage['omitted_purpose_count']==9
    assert coverage['displayed_unique_people']==2

def test_ready_precedes_candidate_and_limitations_are_retained():
    limited=role(2,basis='request_and_reply'); limited['candidates'][0]['response_status']='limited_or_declined'
    selected,_=select_lineup([role(0),limited,role(3,ready=True)],limit=3)
    assert [r['role_shape_id'] for r in selected]==['3','2','0']
    assert selected[1]['candidates'][0]['response_status']=='limited_or_declined'

def test_selection_does_not_promote_claims_or_mutate_inputs():
    roles=[role(0)]; before=copy.deepcopy(roles)
    selected,_=select_lineup(roles)
    assert roles==before
    assert selected[0]['candidates'][0]==before[0]['candidates'][0]

def test_empty_and_omitted_category_counts_are_not_absence_claims():
    selected,coverage=select_lineup([role(0),role(1,'q','Practice my presentation')],limit=1)
    practice=next(c for c in coverage['categories'] if c['id']=='practice')
    assert practice=={'id':'practice','label':'Learning and practice','pool_purposes':1,'displayed_purposes':0,'displayed_people':0}
    assert select_lineup([])[1]['displayed_unique_people']==0
