import sqlite3
from topos.analytics import luck_surface as L


def test_population_counts_aliases_once_and_keeps_unclustered_people(monkeypatch):
    import topos.analytics.messenger_directed as d
    monkeypatch.setattr(d,'resolve_peer_identities',lambda c,keys:{'phone':('alex',None,None),'email':('alex',None,None)})
    x=L.communication_coverage(sqlite3.connect(':memory:'),['email','outside'],{'phone':'c1','email':'c1','unclustered':''})
    assert len(x['population'])==2
    assert len(x['unique_recipient_keys'])==2
    assert x['people_without_recorded_telling']==1
    assert x['recipients_outside_population']==1
    assert x['communities_reached']=={'c1'}


def test_zero_gap_never_produces_a_reach_suggestion(monkeypatch):
    monkeypatch.setattr(L,'rollup',lambda *a:{'work_items':[{'matchable':True,'people_without_recorded_telling':0,'reachable_people':4,'communities_unreached':20}],'coverage':{}})
    assert not any(m['kind']=='tell_unexposed_community' for m in L.build_moves(sqlite3.connect(':memory:'),'ds'))


def test_gap_alone_is_not_an_opportunity(monkeypatch):
    monkeypatch.setattr(L,'rollup',lambda *a:{'work_items':[{'work_item_id':'w','label':'Garden','doing_events':5,'matchable':True,'people_without_recorded_telling':2,'reachable_people':4}],'coverage':{}})
    assert not any(m['kind'] in ('tell_unexposed_community','discuss_work_with_contact') for m in L.build_moves(sqlite3.connect(':memory:'),'ds'))


def test_context_backed_move_names_person_without_claiming_readiness(monkeypatch):
    monkeypatch.setattr(L,'rollup',lambda *a:{'work_items':[],'coverage':{}})
    monkeypatch.setattr(L,'work_conversation_openings',lambda *a:[{'work_item_id':'w','work_label':'Garden', 'peer_key':'p','label':'Morgan','record_id':'m1','event_at':'2026-10-01','last_work_event_at':'2026-10-10'}])
    move=next(m for m in L.build_moves(sqlite3.connect(':memory:'),'ds') if m['kind']=='discuss_work_with_contact')
    assert 'Morgan' in move['title'] and 'Garden' in move['title']
    assert move['marginal_benefit']==0
    assert 'does not establish present willingness' in move['why']
    assert move['evidence']['record_id']=='m1'


def test_missing_community_does_not_make_a_human_contact_disappear(monkeypatch):
    import topos.analytics.person_graph as graph
    monkeypatch.setattr(graph,'build_person_nodes',lambda *a:[{'node_id':'peer','messenger_keys':['peer']}, {'is_owner':True,'messenger_keys':['self']}])
    assert L.messaging_population(sqlite3.connect(':memory:'),'ds',{})=={'peer':''}


def test_work_openings_exclude_automated_peers_and_vendor_mentions(monkeypatch):
    from datetime import datetime, timezone
    import topos.analytics.messenger_directed as directed
    c=sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE conversation_messages(message_id TEXT,sender_id TEXT,content TEXT,event_at TEXT,conversation_id TEXT,dataset_id TEXT,is_from_self INTEGER);
    CREATE TABLE messenger_dyad_stats(dataset_id TEXT,a_key TEXT,b_key TEXT,involves_self INTEGER,peer_class TEXT);''')
    for peer,kind in [('human','human'),('bot','automated')]:
        c.execute("INSERT INTO messenger_dyad_stats VALUES ('ds','self',?,1,?)",(peer,kind))
        c.execute("INSERT INTO conversation_messages VALUES (?,?,?,'2026-10-01','dm','ds',0)",(peer,peer,'I can help with the Garden project'))
    monkeypatch.setattr(directed,'load_messages',lambda *a:[])
    monkeypatch.setattr(directed,'classify_conversations',lambda *a:{'dm':directed.EDGE_KIND_DM})
    monkeypatch.setattr(L,'build_work_items',lambda *a:[])
    monkeypatch.setattr(L,'compile_surfaces',lambda *a:[])
    monkeypatch.setattr(L,'resolve_message_to_work_item',lambda *a:['garden','vendor'])
    monkeypatch.setattr(L,'_peer_labels',lambda *a:{'human':'Morgan','bot':'Automated service'})
    items=[{'work_item_id':'garden','label':'Garden','entity_types':['project'],'last_work_event_at':'2026-10-10'},
           {'work_item_id':'vendor','label':'Vendor','entity_types':['org'],'last_work_event_at':'2026-10-10'}]
    result=L.work_conversation_openings(c,'ds',items,now=datetime(2026,10,13,tzinfo=timezone.utc))
    assert [(x['peer_key'],x['work_item_id']) for x in result]==[('human','garden')]


def test_topic_mentions_and_requests_for_help_are_not_offers():
    assert not L._possible_help_offer('Can you help me with the Garden project?')
    assert not L._possible_help_offer('I cannot help with the Garden project')
    assert not L._possible_help_offer('Garden project tickets are on sale, RSVP now')
    assert L._possible_help_offer('I can help with the Garden project')
    assert L._possible_help_offer('Let me introduce you to the organizers')
