import sqlite3
import pytest
from topos.analytics.messenger_directed import resolve_peer_identities

@pytest.fixture
def conn():
    c=sqlite3.connect(':memory:')
    c.executescript('CREATE TABLE contacts(contact_id TEXT, display_name TEXT); CREATE TABLE contact_identifiers(identifier TEXT,contact_id TEXT); CREATE TABLE entities(contact_id TEXT,entity_id TEXT,entity_type TEXT);')
    return c

def contact(c,cid,key,name):
    c.execute('INSERT INTO contacts VALUES (?,?)',(cid,name))
    c.execute('INSERT INTO contact_identifiers VALUES (?,?)',(key,cid))
    c.execute("INSERT INTO entities VALUES (?,?,'person')",(cid,'ent-'+cid))

@pytest.mark.parametrize('one,two',[
    ('+15125551234','+15125551234'),('5125551234','+15125551234'),('(512) 555-1234','+15125551234')])
def test_duplicate_contact_preserves_agreed_label_without_merging_ids(conn,one,two):
    contact(conn,'import',one,'Alex Rivera');contact(conn,'sync',two,None)
    assert resolve_peer_identities(conn,[two])[two]==(None,None,'Alex Rivera')

def test_conflicting_names_abstain(conn):
    contact(conn,'one','+15125551234','Alex');contact(conn,'two','5125551234','Jamie')
    assert resolve_peer_identities(conn,['+15125551234'])=={}

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
