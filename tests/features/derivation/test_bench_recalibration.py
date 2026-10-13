import json
import sqlite3
from datetime import datetime, timezone

from topos.features.derivation.bench_evidence import help_purposes, goal_sources, discussion_candidates
from topos.features.derivation.social_bench import find_candidates

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def db():
    c = sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE user_goals(goal_id TEXT,record_id TEXT,goal_text TEXT);
    CREATE TABLE ai_chat_messages(message_id TEXT,conversation_id TEXT,sender_type TEXT,event_at TEXT,content TEXT);''')
    return c


def goal(c, gid, text, *, speaker='human', date='2026-10-11T15:00:00Z', thread=None):
    c.execute('INSERT INTO user_goals VALUES (?,?,?)',(gid,gid,text))
    c.execute('INSERT INTO ai_chat_messages VALUES (?,?,?,?,?)',(gid,thread or gid,speaker,date,'source statement'))


def test_optional_tables_do_not_hide_ai_goals_and_dates_are_canonical():
    c=db();goal(c,'garden','Find help planning the neighborhood garden')
    purposes=help_purposes(c,now=NOW)
    assert len(purposes)==1
    assert purposes[0]['label']=='Find help planning the neighborhood garden'
    assert purposes[0]['event_at']=='2026-10-11T15:00:00Z'
    assert purposes[0]['need_status']=='inferred_possible_help'
    assert purposes[0]['self_performed_share'] is None
    assert purposes[0]['evidence'][1]['table']=='ai_chat_messages'


def test_assistant_historical_future_and_undated_goals_are_not_current_owner_needs():
    c=db()
    goal(c,'assistant','Hire a studio recording engineer',speaker='assistant')
    goal(c,'old','Find a grant proposal reviewer',date='2025-01-01T00:00:00Z')
    goal(c,'future','Arrange logistics for a festival',date='2027-01-01T00:00:00Z')
    goal(c,'undated','Learn pottery throwing techniques',date=None)
    assert help_purposes(c,now=NOW)==[]


def test_repeated_extraction_and_one_chat_cannot_fill_the_bench():
    c=db()
    for i in range(20):goal(c,str(i),f'Write an alternative product proposal number {i}',thread='one-chat')
    goal(c,'another','Organize a neighborhood garden workshop')
    assert len(help_purposes(c,now=NOW))==2


def test_skill_claim_without_demonstrated_basis_is_not_promoted():
    c=db();c.executescript('''CREATE TABLE signal_objects(object_key TEXT,payload_json TEXT,ontology_id TEXT,valid_to TEXT);
    CREATE TABLE entities(entity_id TEXT,canonical_name TEXT);''')
    c.execute("INSERT INTO entities VALUES ('ent-a','Alex')")
    def add(basis):
        c.execute('INSERT INTO signal_objects VALUES (?,?,?,NULL)',('fact:ent-a:skill',json.dumps({'value_struct':{'skill':'garden planning','basis':basis}}),'net.capability'))
    # Correct predicate, but title/self-claim still is not demonstrated capability.
    for basis in ['stated_by_them',None,'job_title']:
        c.execute('INSERT INTO signal_objects VALUES (?,?,?,NULL)',('fact:ent-a:skill',json.dumps({'predicate':'net.demonstrated_skill','value_struct':{'skill':'garden planning','basis':basis}}),'net.capability'))
    assert find_candidates(c,{'label_terms':['garden','planning']})==[]
    c.execute('INSERT INTO signal_objects VALUES (?,?,?,NULL)',('fact:ent-a:skill',json.dumps({'predicate':'net.demonstrated_skill','value_struct':{'skill':'garden planning','basis':'reviewed_work'}}),'net.capability'))
    person=find_candidates(c,{'label_terms':['garden','planning']})[0]
    assert person['name']=='Alex' and person['readiness_status']=='unknown'
    assert person['evidence']


def test_discussion_is_attributed_to_peer_and_never_promoted_to_skill(monkeypatch):
    c=db();c.executescript('''CREATE TABLE conversation_messages(dataset_id TEXT,message_id TEXT,sender_id TEXT,content TEXT,event_at TEXT,conversation_id TEXT,is_from_self INTEGER);''')
    import topos.analytics.person_graph as graph
    import topos.analytics.messenger_directed as directed
    monkeypatch.setattr(graph,'build_person_nodes',lambda *a:[{'node_id':'a','label':'Alex','messenger_keys':['peer'] }])
    monkeypatch.setattr(directed,'load_messages',lambda *a:[])
    monkeypatch.setattr(directed,'classify_conversations',lambda *a:{'dm':directed.EDGE_KIND_DM})
    for mid,own,ds in [('mine',1,'ds'),('theirs',0,'ds'),('other-dataset',0,'other')]:
        c.execute('INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?)',(ds,mid,'peer','garden planning','2026-10-01','dm',own))
    c.execute("INSERT INTO conversation_messages VALUES ('ds','old','peer','garden planning','2017-05-01','dm',0)")
    people=discussion_candidates(c,'ds',{'label_terms':['garden','planning']},now=NOW)
    assert len(people)==1
    assert people[0]['capability_status']==people[0]['readiness_status']=='unknown'
    assert [e['record_id'] for e in people[0]['evidence']]==['theirs']

def test_generic_words_never_establish_discussion_fit():
    from topos.features.derivation.bench_evidence import terms
    assert not (terms('Support more query quality across domains') & terms('Love to see you out for the show, more details here'))
