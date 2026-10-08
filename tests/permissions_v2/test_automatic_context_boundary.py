"""Graph churn cannot poison independent labels; new protection still takes effect."""
import json
import sqlite3
from types import SimpleNamespace
import pytest
from tests.permissions_v2.test_evidence import corpus
from tests.permissions_v2.test_entity_boundary import protected_corpus
from topos.permissions_v2.automatic_message_review import context_for
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.canonical import PolicyError


@pytest.fixture(autouse=True)
def dated_messages(protected_corpus):
    with sqlite3.connect(protected_corpus[0].path) as db:
        db.execute("ALTER TABLE conversation_messages ADD COLUMN event_at TEXT")
        db.execute("UPDATE conversation_messages SET event_at='2026-09-20T12:00:00+00:00'")


def observed(corpus):
    with sqlite3.connect(corpus[0].path) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        row=dict(db.execute('SELECT * FROM conversation_messages LIMIT 1').fetchone())
        identity=SimpleNamespace(table='conversation_messages',record_id=row['message_id'],
            source_id=row['source_id'],dataset_id=row['dataset_id'])
        boundary=EntityBoundary(db)
        revision,inputs=context_for(db,identity,row,boundary=boundary)
        return revision,inputs,boundary.revision


def test_unrelated_entity_update_does_not_expire_message_assessment(protected_corpus):
    before=observed(protected_corpus)
    with sqlite3.connect(protected_corpus[0].path) as db:
        db.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,aliases_json) VALUES('unrelated-project','project','Compiler','compiler','[]')")
    after=observed(protected_corpus)
    assert before[2]==after[2]  # No protection decision changed in the fresh closure.
    assert before[:2]==after[:2]  # The classifier's entire input did not.


@pytest.mark.parametrize("alias,reaches", [("History Books", True), ("History Maven", False)])
def test_new_protected_alias_invalidates_message_assessment_it_reaches(protected_corpus, alias, reaches):
    """BL-107 (the owner's decision of 8 Oct 2026): a new alias puts out of date the assessment of a message it reaches
    ("History Books" in "I enjoy reading history books."), and keeps one it does not reach ("History Maven": the
    boundary does not read a lower-case "history" in a message as the name). Until 1.5.1 any alias put every
    assessment out of date. The classifier is shown the new alias either way."""
    before=observed(protected_corpus)
    with sqlite3.connect(protected_corpus[0].path) as db:
        db.execute("UPDATE entities SET aliases_json=? WHERE entity_id='protected-entity'", (json.dumps([alias]),))
    after=observed(protected_corpus)
    assert (before[0]!=after[0]) is reaches
    assert alias.lower().replace(' ', '') in after[1]['protected_terms']


def test_new_protected_link_blocks_release_even_without_vocabulary_change(protected_corpus):
    before=observed(protected_corpus)
    with sqlite3.connect(protected_corpus[0].path) as db:
        db.row_factory=sqlite3.Row
        row=dict(db.execute('SELECT * FROM conversation_messages LIMIT 1').fetchone())
        db.execute('INSERT INTO entity_mentions(entity_id,canonical_table,record_id,source_id) VALUES(?,?,?,?)',
                   ('protected-entity','conversation_messages',row['message_id'],row['source_id']))
    after=observed(protected_corpus)
    # BL-107: the link makes the protected person reach this message, so its assessment is withdrawn too (until
    # 1.5.1 only the vocabulary was bound, and a link left the assessment current: the veto alone withheld it).
    assert before[0]!=after[0]
    assert before[2]!=after[2]  # A new protected link still invalidates search indexes.
    with sqlite3.connect(protected_corpus[0].path) as db:
        db.row_factory=sqlite3.Row
        with pytest.raises(PolicyError):
            EntityBoundary(db).check(table='conversation_messages',record_id=row['message_id'],
                source_id=row['source_id'],dataset_id=row['dataset_id'],row=row)
