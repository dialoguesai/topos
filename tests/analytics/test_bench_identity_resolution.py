import sqlite3
import pytest
from topos.analytics.messenger_directed import peer_identity_key, resolve_peer_identities

@pytest.fixture
def conn():
    c=sqlite3.connect(':memory:')
    c.executescript('CREATE TABLE contacts(contact_id TEXT, display_name TEXT); CREATE TABLE contact_identifiers(identifier TEXT,contact_id TEXT); CREATE TABLE entities(contact_id TEXT,entity_id TEXT,entity_type TEXT);')
    return c

def contact(c,cid,key,name):
    c.execute('INSERT INTO contacts VALUES (?,?)',(cid,name))
    c.execute('INSERT INTO contact_identifiers VALUES (?,?)',(key,cid))
    c.execute("INSERT INTO entities VALUES (?,?,'person')",(cid,'ent-'+cid))

def test_duplicate_contact_with_the_same_handle_preserves_agreed_label_without_merging_ids(conn):
    contact(conn,'import','+15125551234','Alex Rivera');contact(conn,'sync','+15125551234',None)
    assert resolve_peer_identities(conn,['+15125551234'])['+15125551234']==(None,None,'Alex Rivera')

@pytest.mark.parametrize('one',['5125551234','(512) 555-1234'])
def test_exact_match_keeps_its_ids_beside_a_second_spelling(conn,one):
    # Review of topos#116: folding spellings made this exact match ambiguous and dropped
    # the ids, so the speaker label was lost and one person became two graph nodes.
    contact(conn,'import',one,'Alex Rivera');contact(conn,'sync','+15125551234',None)
    assert resolve_peer_identities(conn,['+15125551234'])['+15125551234']==('sync','ent-sync','Alex Rivera')
    assert resolve_peer_identities(conn,[one])[one]==('import','ent-import','Alex Rivera')

def test_exact_match_keeps_its_own_name_when_spellings_disagree(conn):
    contact(conn,'one','+15125551234','Alex');contact(conn,'two','5125551234','Jamie')
    assert resolve_peer_identities(conn,['+15125551234'])['+15125551234']==('one','ent-one','Alex')

def test_conflicting_names_without_an_exact_match_abstain(conn):
    contact(conn,'one','+15125551234','Alex');contact(conn,'two','5125551234','Jamie')
    assert resolve_peer_identities(conn,['(512) 555-1234'])=={}

@pytest.mark.parametrize('bare',['13812345678','1234567890','5121234567'])
def test_numbers_that_are_not_north_american_are_not_read_as_plus_one(bare):
    assert peer_identity_key(bare)==bare

@pytest.mark.parametrize('spelling',['5125551234','(512) 555-1234','1-512-555-1234','+1 512 555 1234'])
def test_north_american_spellings_fold(spelling):
    assert peer_identity_key(spelling)=='+15125551234'

@pytest.mark.parametrize('wrong',['+445125551234','5125551234 ext 2','151255512345','test5125551234@example.org'])
def test_no_international_suffix_or_extension_matching(conn,wrong):
    contact(conn,'one','+15125551234','Alex')
    assert resolve_peer_identities(conn,[wrong])=={}

def test_unique_identity_retains_unique_ids(conn):
    contact(conn,'one','5125551234','Alex')
    assert resolve_peer_identities(conn,['+15125551234'])['+15125551234']==('one','ent-one','Alex')

def test_multiple_entities_do_not_transfer_facts(conn):
    contact(conn,'one','+15125551234','Alex')
    conn.execute("INSERT INTO entities VALUES ('one','another','person')")
    assert resolve_peer_identities(conn,['+15125551234'])['+15125551234']==('one',None,'Alex')
