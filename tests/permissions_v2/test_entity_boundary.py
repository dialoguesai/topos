"""Nonvacuity and canaries for the node's observed Off-limits boundary."""
import json
import sqlite3

import pytest

from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary
from tests.permissions_v2.test_evidence import corpus, decision, edit, payload, attest, owner  # noqa: F401


def install_context(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        conn.executescript("""
            CREATE TABLE contacts(contact_id TEXT PRIMARY KEY,display_name TEXT);
            CREATE TABLE contact_identifiers(contact_id TEXT,identifier TEXT,identifier_type TEXT);
            CREATE TABLE conversations(conversation_id TEXT,dataset_id TEXT,source_id TEXT,title TEXT,metadata_json TEXT);
            CREATE TABLE conversation_participants(conversation_id TEXT,dataset_id TEXT,source_id TEXT,contact_id TEXT);
            ALTER TABLE conversation_messages ADD COLUMN conversation_id TEXT;
            ALTER TABLE conversation_messages ADD COLUMN sender_id TEXT;
            ALTER TABLE conversation_messages ADD COLUMN metadata_json TEXT;
            ALTER TABLE conversation_messages ADD COLUMN reply_to_message_id TEXT;
            UPDATE conversation_messages SET conversation_id='thread-1',sender_id='owner-handle',metadata_json='{}';
            INSERT INTO conversations VALUES('thread-1','dataset-1','source-1','Reading','{}');
            INSERT INTO contacts VALUES('protected-contact','Mara Example');
            INSERT INTO contact_identifiers VALUES('protected-contact','+1 (212) 555-0199','phone');
            INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,aliases_json,contact_id)
                VALUES('protected-entity','person','Mara Example','mara example','["M.E."]','protected-contact');
            INSERT INTO entity_blackholes(blackhole_id,entity_id,normalized_name,canonical_name,aliases_json,rebuild_state)
                VALUES('bh-test','protected-entity','mara example','Mara Example','["M.E."]','complete');
        """)


@pytest.fixture
def protected_corpus(corpus):
    install_context(corpus)
    return corpus


def test_unrelated_fact_qualifies_with_an_off_limits_person(protected_corpus):
    assert decision(protected_corpus).verdict == "qualified"


@pytest.mark.parametrize("text", ["Mara Example called.", "Mara\u200b Example called.",
    "Ｍａｒａ Ｅｘａｍｐｌｅ called.", "Mára Example called.", "Mara.Example@example.org called.",
    "Mara&#32;Example called.", "M.E. called.", "The number is 212-555-0199.", "МАRА Example called."])
def test_protected_text_is_not_released(protected_corpus, text):
    edit(protected_corpus, "UPDATE conversation_messages SET content=?", (text,))
    assert decision(protected_corpus).verdict == "withheld"


@pytest.mark.parametrize("sql,args", [
    ("UPDATE conversation_messages SET metadata_json=?", (json.dumps({"nested": {"ref": "Mara Example"}}),)),
    ("UPDATE conversations SET title='Mara Example'", ()),
    ("UPDATE conversation_messages SET sender_id='2125550199'", ()),
    ("UPDATE conversation_messages SET sender_id='protected-contact'", ()),
    ("UPDATE conversation_messages SET sender_id='protected-entity'", ()),
    ("INSERT INTO conversation_participants VALUES('thread-1','dataset-1','source-1','protected-contact')", ()),
    ("INSERT INTO entity_mentions(mention_id,entity_id,record_id) VALUES('mention','protected-entity','message-1')", ()),
])
def test_metadata_titles_contacts_senders_and_legacy_mentions_veto(protected_corpus, sql, args):
    edit(protected_corpus, sql, args)
    assert decision(protected_corpus).verdict == "withheld"


def test_protected_mention_elsewhere_does_not_poison_independent_message(protected_corpus):
    edit(protected_corpus, "INSERT INTO conversation_messages(message_id,dataset_id,source_id,content,is_from_self,owner_user_id,conversation_id,sender_id) "
        "VALUES('other','dataset-1','source-1','Mara Example called.',1,'owner-1','thread-1','owner-handle')")
    assert decision(protected_corpus).verdict == "qualified"
    edit(protected_corpus, "UPDATE conversation_messages SET reply_to_message_id='other' WHERE message_id='message-1'")
    assert decision(protected_corpus).verdict == "withheld"


@pytest.mark.parametrize("sql", ["DROP TABLE contacts", "DROP TABLE contact_identifiers", "DROP TABLE conversation_participants",
    "UPDATE conversation_messages SET metadata_json='{broken'", "DELETE FROM conversations"])
def test_unavailable_context_never_proves_absence(protected_corpus, sql):
    edit(protected_corpus, sql)
    assert decision(protected_corpus).verdict == "withheld"


def test_empty_ner_does_not_override_a_preemptive_name(protected_corpus):
    edit(protected_corpus, "UPDATE entity_blackholes SET entity_id='' WHERE blackhole_id='bh-test'")
    edit(protected_corpus, "UPDATE conversation_messages SET content='Mara Example called.'")
    assert decision(protected_corpus).verdict == "withheld"


def test_reminted_and_merge_neighbor_ids_are_still_protected(protected_corpus):
    edit(protected_corpus, "INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) VALUES('reminted','person','Mara Example','mara example')")
    edit(protected_corpus, "INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into,canonical_name,aliases_json,identifiers_json) "
        "VALUES('older','reminted','Former Name','[]','[]')")
    edit(protected_corpus, "INSERT INTO entity_mentions(mention_id,entity_id,record_id) VALUES('m','older','message-1')")
    assert decision(protected_corpus).verdict == "withheld"


def test_contact_username_closes_matching_entity_and_observed_mentions(protected_corpus):
    edit(protected_corpus, "ALTER TABLE contacts ADD COLUMN known_usernames_json TEXT")
    edit(protected_corpus, "UPDATE contacts SET known_usernames_json='[\"History Maven\"]'")
    edit(protected_corpus, "INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) VALUES('alias-entity','person','History Maven','history maven')")
    edit(protected_corpus, "INSERT INTO entity_mentions(mention_id,entity_id,record_id) VALUES('alias-mention','alias-entity','message-1')")
    assert decision(protected_corpus).verdict == "withheld"


def test_new_alias_invalidates_entity_boundary_revision(protected_corpus):
    def revision():
        with sqlite3.connect(protected_corpus[0].path) as conn:
            return EntityBoundary(conn).revision
    before = revision()
    edit(protected_corpus, "UPDATE entities SET aliases_json='[\"History Maven\"]' WHERE entity_id='protected-entity'")
    assert revision() != before
    assert decision(protected_corpus).verdict == "qualified"
    edit(protected_corpus, "UPDATE conversation_messages SET content='History Maven called.'")
    assert decision(protected_corpus).verdict == "withheld"


def test_owner_preview_retains_full_contents(protected_corpus):
    edit(protected_corpus, "UPDATE conversation_messages SET content='Mara Example called.'")
    with owner():
        snapshot = protected_corpus[0].inspect_for_review(protected_corpus[2])
    assert len(snapshot.leaves) == 1


def test_fact_object_is_checked_as_well_as_its_message(protected_corpus):
    payload(protected_corpus, object_value="Mara Example")
    assert decision(protected_corpus).verdict == "withheld"


def test_surface_scan_never_returns_protected_details(protected_corpus):
    with sqlite3.connect(protected_corpus[0].path) as conn:
        gate = EntityBoundary(conn)
        with pytest.raises(PolicyError, match="entity_protected") as caught:
            gate.check(table="signal_objects",record_id="test",source_id=None,dataset_id=None,
                row={"payload_json":json.dumps({"object_value":"Mara Example"})})
        assert "Mara" not in str(caught.value)
