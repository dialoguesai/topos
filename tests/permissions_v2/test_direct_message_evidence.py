"""Synthetic native provenance + explicit whole-message review; no profile fact."""
import pytest
from tests.permissions_v2.test_reconciliation_provenance import legacy, publish
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.message_evidence import (preview_message, record_message_review,
    qualify_message, message_key)


def setup(legacy, **labels):
    service, conn, _, _ = legacy
    publish(legacy)
    resolver = service.resolver
    identity = resolver._identity('conversation_messages', 'imessage:1', 'imessage', 'native-dataset')
    with owner():
        reviews = EvidenceReviewStore(service.root.parent / 'reviews.db', resolver=resolver)
        preview = preview_message(resolver, reviews, identity)
        classification = dict(evidence=preview['snapshot']['message'], domains=['work'], sensitivity='none',
            authorship='owner_authored', speech='original_message', independent_copies='none_known', protected_content='none')
        classification.update(labels)
        record_message_review(resolver, reviews, review_id='message-review-1', expected_snapshot=preview['snapshot'],
            classification=classification, expected_current_review_revision=None, reviewed_at=1)
    return resolver, reviews, identity


def qualify(resolver, reviews, identity):
    with resolver._read() as (conn, floor), reviews._db() as db:
        return qualify_message(resolver, conn, floor, identity, reviews, db)[0]


@pytest.mark.parametrize('legacy', ['visit'], indirect=True)
def test_message_needs_no_fact_and_does_not_claim_residence(legacy):
    resolver, reviews, identity = setup(legacy, domains=['plans'], sensitivity='personal')
    result = qualify(resolver, reviews, identity)
    assert result.family == 'owner_authored_message/v1'
    assert result.snapshot.artifacts == []
    assert legacy[1].execute("SELECT count(*) FROM signal_objects WHERE object_type='fact'").fetchone()[0] == 0
    assert result.classifications[0].domains == ['plans']


@pytest.mark.parametrize('labels,code', [
    ({'sensitivity':'unknown'}, 'classification_unknown_or_mixed'),
    ({'domains':['invented']}, 'classification_unknown_or_mixed'),
    ({'domains':['work','work']}, 'classification_unknown_or_mixed'),
    ({'protected_content':'unknown'}, 'protected_content_unresolved'),
    ({'protected_content':'present'}, 'protected_content_unresolved'),
    ({'speech':'mixed'}, 'not_original_message'),
    ({'speech':'third_party_quote'}, 'not_original_message'),
    ({'authorship':'other'}, 'not_original_message'),
    ({'independent_copies':'unknown'}, 'independent_copy_lineage'),
])
def test_review_cannot_admit_incomplete_or_other_peoples_content(legacy, labels, code):
    args = setup(legacy, **labels)
    with pytest.raises(PolicyError, match=code):
        qualify(*args)


def test_message_optout_and_review_revocation(legacy):
    resolver, reviews, identity = setup(legacy)
    with owner():
        reviews.opt_out(message_key(identity), now=2)
    with pytest.raises(PolicyError, match='owner_opted_out'):
        qualify(resolver, reviews, identity)
    with owner():
        reviews.opt_in(message_key(identity))
    assert qualify(resolver, reviews, identity)
    with owner():
        reviews.revoke_review('message-review-1')
    with pytest.raises(PolicyError, match='message_review_required'):
        qualify(resolver, reviews, identity)


def test_canonical_authorship_flags_are_not_origin(legacy):
    service, conn, _, _ = legacy
    identity = service.resolver._identity('conversation_messages', 'imessage:1', 'imessage', 'native-dataset')
    with owner():
        reviews = EvidenceReviewStore(service.root.parent / 'reviews.db', resolver=service.resolver)
        with pytest.raises(PolicyError, match='native_owner_provenance_unavailable'):
            preview_message(service.resolver, reviews, identity)


def test_revoked_source_proof_overrides_owner_review(legacy):
    resolver, reviews, identity = setup(legacy)
    service, conn, _, enrollment = legacy
    with owner():
        service.revoke(conn, enrollment_id=enrollment)
    with pytest.raises(PolicyError):
        qualify(resolver, reviews, identity)


def test_mutated_content_cannot_reuse_review(legacy):
    args = setup(legacy)
    legacy[1].execute("UPDATE conversation_messages SET content='different content'")
    legacy[1].commit()
    with pytest.raises(PolicyError):
        qualify(*args)


def test_deselected_fact_still_blocks_its_message(legacy):
    from topos.features.facts.store import FactStore
    resolver, reviews, identity = setup(legacy)
    conn = legacy[1]
    FactStore(conn).assert_fact(subject_entity_id='self', predicate='works_on', object_value='Synthetic message',
        confidence=1, source_refs=[{'table':'conversation_messages','record_id':'imessage:1',
                                   'source_id':'imessage','dataset_id':'native-dataset'}],
        disclosure='owner_only', asserted_by='owner')
    conn.commit()
    fact=conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'").fetchone()[0]
    assert qualify(resolver,reviews,identity)
    with owner(): reviews.opt_out(fact,now=2)
    with pytest.raises(PolicyError,match='owner_opted_out'):
        qualify(resolver,reviews,identity)


def test_unrelated_record_restriction_does_not_stale_message_review(legacy):
    resolver,reviews,identity=setup(legacy)
    conn=legacy[1]
    conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('conversation_messages','unrelated-message')")
    conn.commit()
    assert qualify(resolver,reviews,identity)


@pytest.mark.parametrize('protected_name,blocked', [('Synthetic message',True),('Unrelated Private Person',False)])
def test_off_limits_are_rechecked_without_global_message_stop(legacy,protected_name,blocked):
    from topos.storage.canonical.conversations_tables import (ensure_contacts_table,
        ensure_contact_identifiers_table,ensure_conversations_table,ensure_conversation_participants_table)
    resolver,reviews,identity=setup(legacy)
    conn=legacy[1]
    for create in (ensure_contacts_table,ensure_contact_identifiers_table,ensure_conversations_table,ensure_conversation_participants_table):
        create(conn)
    conversation=conn.execute('SELECT conversation_id FROM conversation_messages').fetchone()[0]
    conn.execute("INSERT INTO conversations(conversation_id,dataset_id,source_id) VALUES(?,'native-dataset','imessage')",(conversation,))
    conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) VALUES('protected','',?,?,'complete')",(protected_name,protected_name.lower()))
    conn.commit()
    if blocked:
        with pytest.raises(PolicyError,match='entity_protected'): qualify(resolver,reviews,identity)
    else:
        # BL-107 (the owner's decision of 8 Oct 2026): an entry that reaches nothing of this message keeps its
        # assessment. Until 1.5.1 the human review was out of date here and had to be made again.
        assert qualify(resolver,reviews,identity)


@pytest.mark.parametrize('flag', [0, 1])
def test_a_message_flagged_nsfw_when_it_was_proven_can_never_be_reviewed_into_a_share(legacy, flag):
    """The NSFW hard withhold on the direct-message path, reached past the proof.

    A flag set AFTER the proof is stopped earlier: the row no longer matches what was proven (that case is
    `test_direct_search_twins.py`). Here the row carried the flag when its proof was published, so the proof
    holds and the content rule is the only thing left to stop it. The unflagged twin is reviewed and qualifies.
    """
    _service, conn, _, _ = legacy
    conn.execute('ALTER TABLE conversation_messages ADD COLUMN content_nsfw INTEGER')
    conn.execute('UPDATE conversation_messages SET content_nsfw=?', (flag,))
    conn.commit()
    if flag:
        with pytest.raises(PolicyError, match='unsupported_message_content'):
            setup(legacy)
    else:
        assert type(qualify(*setup(legacy))).__name__ == 'QualifiedMessage'
