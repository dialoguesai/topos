"""Conservative fact qualification using real canonical rows and owner reviews."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import shutil
import sqlite3

import pytest

from topos.features.facts.store import FactStore
from topos.features.lifecycle.record_protection import RecordProtectionStore
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceBinding, EvidenceResolver, EvidenceReviewStore, ReviewedClassification
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
from topos.storage.db.migrations.owner_only_records_v1 import apply_owner_only_records_v1_up
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up


@contextmanager
def owner(*, actor="owner-1", cls=OWNER_APP, channel="uds"):
    token = set_principal(Principal(cls=cls, channel=channel, acting_user=actor))
    try:
        yield
    finally:
        reset_principal(token)


@pytest.fixture
def corpus(tmp_path):
    canonical = tmp_path / "canonical.db"
    binding = EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1", resource_id="resource-1", owner_id="owner-1")
    with sqlite3.connect(canonical) as conn:
        apply_signal_objects_up(conn)
        apply_owner_only_records_v1_up(conn)
        apply_entity_blackhole_v1_up(conn)
        conn.execute("CREATE TABLE engine_config(key TEXT PRIMARY KEY,value TEXT)")
        conn.execute("INSERT INTO engine_config VALUES('user_id','owner-1')")
        conn.execute("CREATE TABLE entities(entity_id TEXT PRIMARY KEY,is_self INTEGER)")
        conn.execute("INSERT INTO entities VALUES('owner-entity',1)")
        conn.execute("CREATE TABLE conversation_messages(message_id TEXT, dataset_id TEXT, source_id TEXT, content TEXT, is_from_self INTEGER, deleted_at TEXT,owner_user_id TEXT)")
        conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT,source_id TEXT,content TEXT,sender_type TEXT,deleted_at TEXT,conversation_id TEXT)")
        conn.execute("CREATE TABLE ai_chat_conversations(conversation_id TEXT,source_id TEXT,owner_user_id TEXT)")
        conn.execute("INSERT INTO conversation_messages VALUES('message-1','dataset-1','source-1','I enjoy reading history books.',1,NULL,'owner-1')")
        conn.execute("INSERT INTO ai_chat_messages VALUES('ai-message-1','ai-source-1','I attend my reading group.','user',NULL,'ai-conversation-1')")
        conn.execute("INSERT INTO ai_chat_conversations VALUES('ai-conversation-1','ai-source-1','owner-1')")
        conn.commit()
        fact = FactStore(conn).assert_fact(subject_entity_id="self", predicate="prefers", object_value="history books",
            disclosure="scoped", source_refs=[{"table":"conversation_messages","dataset_id":"dataset-1","source_id":"source-1","record_id":"message-1"}], asserted_by="owner")
    ensure_protection_clock(canonical, owner_id="owner-1")
    resolver = EvidenceResolver(canonical, binding=binding)
    with owner():
        reviews = EvidenceReviewStore(tmp_path / "reviews.db", resolver=resolver)
    return resolver, reviews, fact["object_id"]


def edit(corpus, sql, args=()):
    with sqlite3.connect(corpus[0].path) as conn:
        conn.execute(sql, args)


def payload(corpus, **updates):
    with sqlite3.connect(corpus[0].path) as conn:
        raw = json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?", (corpus[2],)).fetchone()[0])
        raw.update(updates)
        conn.execute("UPDATE signal_objects SET payload_json=? WHERE object_id=?", (json.dumps(raw), corpus[2]))


def attest(corpus, *, review_id="review-1", transform=None):
    resolver, reviews, fact_id = corpus
    with owner():
        snapshot = resolver.inspect_for_review(fact_id)
        classifications = [ReviewedClassification(evidence=version, domains=["reading"], sensitivity="personal",
            subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement", independent_copies="none_known")
            for version in snapshot.artifacts + snapshot.leaves]
        if transform:
            classifications = transform(classifications)
        return reviews.record_review(resolver=resolver, review_id=review_id, expected_snapshot=snapshot,
                                     classifications=classifications, reviewed_at=1100)


def decision(corpus):
    return corpus[0].qualify(corpus[2], reviews=corpus[1])


def test_positive_existing_scoped_fact_requires_explicit_revision_bound_owner_review(corpus):
    assert decision(corpus).reason_code == "owner_review_required"
    attest(corpus)
    result = decision(corpus)
    assert result.verdict == "qualified" and result.evidence.execution_enabled is False
    leaf = result.evidence.snapshot.leaves[0].identity
    assert (leaf.table, leaf.source_id, leaf.record_id, leaf.dataset_id) == ("conversation_messages", "source-1", "message-1", "dataset-1")
    assert "history books" not in result.model_dump_json()


def test_datasetless_ai_rows_use_explicit_node_resource_scope(corpus):
    edit(corpus, "UPDATE signal_objects SET source_refs_json=? WHERE object_id=?",
         (json.dumps([{"table":"ai_chat_messages","source_id":"ai-source-1","record_id":"ai-message-1"}]), corpus[2]))
    attest(corpus)
    result = decision(corpus)
    assert result.verdict == "qualified"
    leaf = result.evidence.snapshot.leaves[0].identity
    assert leaf.dataset_kind == "node_resource" and leaf.dataset_id is None
    assert leaf.binding.node_id == "node-1" and leaf.binding.resource_id == "resource-1"


@pytest.mark.parametrize("badref", [
    {"table":"conversation_messages","record_id":"message-1","source_id":"source-1"},
    {"table":"conversation_messages","record_id":"message-1","dataset_id":"dataset-1"},
    {"table":"ai_chat_messages","record_id":"ai-message-1","source_id":"ai-source-1","dataset_id":"borrowed-dataset"},
    {"table":"activity_events","record_id":"event-1","source_id":"source-1"},
    {"table":"conversation_messages","record_id":"message-1","source_id":"source-1","dataset_id":"dataset-1","node_id":"other-node"},
])
def test_incomplete_unsupported_or_cross_node_references_withhold(corpus, badref):
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps([badref]),))
    result = decision(corpus)
    assert result.verdict == "withheld" and result.evidence is None
    assert result.reason_code in {"lineage_identity_incomplete", "lineage_binding"}


@pytest.mark.parametrize("disclosure", ["owner_only", None, "unknown", "public"])
def test_review_and_pack_namespace_never_declassify_owner_only(corpus, disclosure):
    payload(corpus, disclosure=disclosure, pack="interests.taste", verified_by_owner=True, actor_role="authored", altitude="stated")
    attest(corpus)
    assert decision(corpus).reason_code == "owner_only"
    with sqlite3.connect(corpus[0].path) as conn:
        assert json.loads(conn.execute("SELECT payload_json FROM signal_objects").fetchone()[0])["disclosure"] == disclosure


@pytest.mark.parametrize("change", [dict(subject_entity_id="other-person"), dict(asserted_by="contact:other"),
    dict(actor_role="addressed"), dict(object_entity_id="third-person"), dict(altitude="inferred")])
def test_about_owner_and_authored_are_not_inferred_from_a_pack_name(corpus, change):
    payload(corpus, pack="interests.taste", **change)
    attest(corpus)
    assert decision(corpus).verdict == "withheld"


@pytest.mark.parametrize("label", [dict(subject_entity_ids=["self","other-person"]), dict(authorship="other"),
    dict(speech="third_party_quote"), dict(speech="mixed"), dict(speech="unknown"), dict(domains=[]),
    dict(sensitivity="unknown"), dict(independent_copies="present"), dict(independent_copies="unknown")])
def test_mixed_subject_quotes_unknowns_and_copies_withhold(corpus, label):
    attest(corpus, transform=lambda items: [item.model_copy(update=label) for item in items])
    assert decision(corpus).verdict == "withheld"


def test_native_source_role_cannot_be_overridden_by_review(corpus):
    edit(corpus, "UPDATE conversation_messages SET is_from_self=0")
    attest(corpus)
    assert decision(corpus).reason_code == "not_owner_authored"


def test_assistant_source_is_not_owner_authored(corpus):
    edit(corpus, "UPDATE ai_chat_messages SET sender_type='assistant'")
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps([{"table":"ai_chat_messages","record_id":"ai-message-1","source_id":"ai-source-1"}]),))
    attest(corpus)
    assert decision(corpus).reason_code == "not_owner_authored"


@pytest.mark.parametrize("sql", ["UPDATE conversation_messages SET content='Changed claim.'", "UPDATE signal_objects SET updated_at='new-revision'",
    "UPDATE signal_objects SET source_refs_json='[]'", "DELETE FROM conversation_messages", "UPDATE conversation_messages SET deleted_at='now'",
    "UPDATE signal_objects SET valid_to='closed'", "DELETE FROM signal_objects"])
def test_stale_changed_deleted_or_closed_evidence_withholds(corpus, sql):
    attest(corpus)
    edit(corpus, sql)
    assert decision(corpus).verdict == "withheld"


def test_full_identity_ambiguity_is_denied_without_first_row_fallback(corpus):
    edit(corpus, "INSERT INTO conversation_messages SELECT * FROM conversation_messages")
    assert decision(corpus).reason_code == "evidence_ambiguous"


def test_source_and_dataset_identity_do_not_fall_back_to_record_id(corpus):
    edit(corpus, "UPDATE conversation_messages SET source_id='other-source'")
    assert decision(corpus).reason_code == "evidence_missing"


def test_independent_canonical_copy_withholds_even_when_different_family(corpus):
    edit(corpus, "UPDATE ai_chat_messages SET content='I enjoy reading history books.'")
    attest(corpus)
    assert decision(corpus).reason_code == "independent_copy_lineage"


def test_independent_fact_copy_does_not_escape_owner_only(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        row = conn.execute("SELECT * FROM signal_objects").fetchone()
        columns = [item[1] for item in conn.execute("PRAGMA table_info(signal_objects)")]
        values = dict(zip(columns, row))
        values["object_id"], values["object_key"] = "copy-fact", "copy-fact-key"
        copy_payload = json.loads(values["payload_json"])
        copy_payload["disclosure"] = "owner_only"
        values["payload_json"] = json.dumps(copy_payload)
        conn.execute("INSERT INTO signal_objects ("+",".join(columns)+") VALUES ("+",".join("?" for _ in columns)+")", list(values.values()))
    attest(corpus)
    assert decision(corpus).reason_code == "independent_copy_lineage"


def test_recursive_fact_evidence_qualifies_only_when_every_node_is_reviewed(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        child = FactStore(conn).assert_fact(subject_entity_id="self", predicate="member_of", object_value="reading group", disclosure="scoped",
            source_refs=[{"table":"ai_chat_messages","record_id":"ai-message-1","source_id":"ai-source-1"}])
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (json.dumps([{"table":"signal_objects","record_id":child["object_id"]}]), corpus[2]))
    review = attest(corpus)
    result = decision(corpus)
    assert result.verdict == "qualified" and len(result.evidence.snapshot.artifacts) == 2
    assert len(result.evidence.snapshot.leaves) == 1
    attest(corpus, review_id="incomplete-review", transform=lambda items: items[:-1])
    assert decision(corpus).reason_code == "classification_incomplete"


def test_owner_only_derived_child_cannot_be_laundered_by_scoped_parent(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        child = FactStore(conn).assert_fact(subject_entity_id="self", predicate="member_of", object_value="reading group",
            source_refs=[{"table":"ai_chat_messages","record_id":"ai-message-1","source_id":"ai-source-1"}])
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (json.dumps([{"table":"signal_objects","record_id":child["object_id"]}]), corpus[2]))
    attest(corpus)
    assert decision(corpus).reason_code == "owner_only"


def test_recursive_cycle_and_missing_derived_leaf_withhold(corpus):
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps([{"table":"signal_objects","record_id":corpus[2]}]),))
    assert decision(corpus).reason_code == "lineage_cycle"
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps([{"table":"signal_objects","record_id":"missing-fact"}]),))
    assert decision(corpus).reason_code == "evidence_missing"


@pytest.mark.parametrize("lift_again", [False, True])
def test_any_protection_generation_change_invalidates_review(corpus, lift_again):
    attest(corpus)
    with sqlite3.connect(corpus[0].path) as conn:
        RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="message-1")
        if lift_again:
            RecordProtectionStore(conn).unprotect(canonical_table="conversation_messages", record_id="message-1")
    assert decision(corpus).reason_code == ("review_stale" if lift_again else "owner_only")
    attest(corpus, review_id="review-after-change")
    assert decision(corpus).verdict == ("qualified" if lift_again else "withheld")


@pytest.mark.parametrize("actor,cls,channel", [("other-owner",OWNER_APP,"uds"), (None,OWNER_APP,"uds"),
    ("owner-1",THIRD_PARTY,"cp_relay"), ("owner-1",OWNER_APP,"local_http")])
def test_review_creation_requires_actual_owner_mode_and_exact_owner(corpus, actor, cls, channel):
    with owner(actor=actor, cls=cls, channel=channel):
        with pytest.raises(PolicyError, match="owner_authority_required"):
            corpus[0].inspect_for_review(corpus[2])
        with pytest.raises(PolicyError, match="owner_authority_required"):
            corpus[1].revoke_review("review-1")


def test_same_owner_signed_relay_can_review_but_recipient_cannot_submit_flags(corpus):
    with owner(channel="cp_relay"):
        snapshot = corpus[0].inspect_for_review(corpus[2])
    assert snapshot.fact_id == corpus[2]
    with pytest.raises(TypeError):
        corpus[0].qualify(corpus[2], reviews=corpus[1], owner_reviewed=True)


def test_review_revocation_persists_and_cannot_be_replayed(corpus):
    review = attest(corpus)
    with owner():
        corpus[1].revoke_review(review.review_id)
        reopened = EvidenceReviewStore(corpus[1].path, resolver=corpus[0])
        with pytest.raises(PolicyError, match="review_id_conflict"):
            reopened.record_review(resolver=corpus[0], review_id=review.review_id, expected_snapshot=review.snapshot,
                classifications=review.classifications, reviewed_at=review.reviewed_at)
    assert corpus[0].qualify(corpus[2], reviews=reopened).reason_code == "owner_review_required"


def test_copied_database_cannot_reuse_review_even_with_same_labels(corpus, tmp_path):
    attest(corpus)
    copy = tmp_path / "independent-copy.db"
    shutil.copyfile(corpus[0].path, copy)
    resolver = EvidenceResolver(copy, binding=corpus[0].binding)
    assert resolver.qualify(corpus[2], reviews=corpus[1]).reason_code == "review_database_binding"


def test_legacy_verified_flag_and_namespace_are_not_review_authority(corpus):
    payload(corpus, verified_by_owner=True, verified_at="legacy", pack="interests.taste")
    assert decision(corpus).reason_code == "owner_review_required"


def test_unknown_or_multiple_owner_entities_withholds(corpus):
    attest(corpus)
    edit(corpus, "INSERT INTO entities VALUES('second-owner',1)")
    assert decision(corpus).reason_code == "owner_subject_ambiguous"


def test_missing_protection_schema_or_review_store_fails_closed(corpus):
    attest(corpus)
    edit(corpus, "DROP TABLE owner_only_records")
    assert decision(corpus).verdict == "withheld"


@pytest.mark.parametrize("unknown_owner", [None,"different-owner"])
def test_authored_marker_without_same_owner_binding_withholds(corpus, unknown_owner):
    edit(corpus, "UPDATE conversation_messages SET owner_user_id=?", (unknown_owner,))
    assert decision(corpus).reason_code == "evidence_owner_binding"


def test_ai_parent_owner_binding_and_revision_are_required(corpus):
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps([{"table":"ai_chat_messages","source_id":"ai-source-1","record_id":"ai-message-1"}]),))
    attest(corpus)
    edit(corpus, "UPDATE ai_chat_conversations SET owner_user_id='different-owner'")
    assert decision(corpus).reason_code == "evidence_owner_binding"


def test_review_is_stale_when_recursive_leaf_changes(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        child = FactStore(conn).assert_fact(subject_entity_id="self", predicate="member_of", object_value="reading group", disclosure="scoped",
            source_refs=[{"table":"ai_chat_messages","record_id":"ai-message-1","source_id":"ai-source-1"}])
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (json.dumps([{"table":"signal_objects","record_id":child["object_id"]}]), corpus[2]))
    attest(corpus)
    edit(corpus, "UPDATE ai_chat_messages SET content='Changed leaf evidence'")
    assert decision(corpus).reason_code == "review_stale"


def test_new_independent_copy_invalidates_qualification_without_mutating_original(corpus):
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    edit(corpus, "UPDATE ai_chat_messages SET content='I enjoy reading history books.'")
    assert decision(corpus).reason_code == "independent_copy_lineage"


def test_entity_protection_has_no_uncertified_mention_lineage_fallback(corpus):
    edit(corpus, "INSERT INTO entity_blackholes(blackhole_id,normalized_name,entity_id) VALUES('blackhole-1','synthetic entity','entity-2')")
    attest(corpus)
    assert decision(corpus).reason_code == "entity_protection_lineage_unavailable"


def test_corrupted_review_store_is_withheld(corpus):
    attest(corpus)
    with sqlite3.connect(corpus[1].path) as conn:
        conn.execute("DROP TABLE fact_reviews")
    assert decision(corpus).reason_code == "review_storage_unavailable"


def test_qualification_failure_has_no_candidate_values_or_exception_context(corpus):
    marker = 'PRIVATE_CANDIDATE_CANARY'
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", ('{"unexpected":"'+marker+'"}',))
    result = decision(corpus)
    assert result.verdict == "withheld" and marker not in result.model_dump_json()


def test_duplicate_refs_and_node_limit_fail_closed(corpus, monkeypatch):
    import topos.permissions_v2.evidence as module
    with sqlite3.connect(corpus[0].path) as conn:
        original = json.loads(conn.execute("SELECT source_refs_json FROM signal_objects").fetchone()[0])
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps(original*2),))
    assert decision(corpus).reason_code == "lineage_ambiguous"
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps(original),))
    monkeypatch.setattr(module, "MAX_NODES", 1)
    assert decision(corpus).reason_code == "lineage_limit"


def test_recipient_cannot_store_even_complete_forged_owner_review(corpus):
    review = attest(corpus)
    with owner(cls=THIRD_PARTY, channel="cp_relay"):
        with pytest.raises(PolicyError, match="owner_authority_required"):
            corpus[1].record_review(resolver=corpus[0], review_id="recipient-forgery", expected_snapshot=review.snapshot,
                classifications=review.classifications, reviewed_at=1101)
        with pytest.raises(PolicyError, match="owner_authority_required"):
            EvidenceReviewStore(corpus[1].path, resolver=corpus[0])


def test_owner_only_floor_stops_before_recursive_evidence_read(corpus, monkeypatch):
    payload(corpus, disclosure="owner_only")
    resolver = corpus[0]
    original = resolver._load
    observed = []
    def track(conn, identity):
        observed.append(identity.table)
        return original(conn, identity)
    monkeypatch.setattr(resolver, "_load", track)
    assert decision(corpus).reason_code == "owner_only"
    assert observed == ["signal_objects"]


def test_protected_leaf_stops_before_reading_its_content(corpus, monkeypatch):
    with sqlite3.connect(corpus[0].path) as conn:
        RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="message-1")
    resolver = corpus[0]
    original = resolver._load
    observed = []
    def track(conn, identity):
        observed.append(identity.table)
        return original(conn, identity)
    monkeypatch.setattr(resolver, "_load", track)
    assert decision(corpus).reason_code == "owner_only"
    assert observed == ["signal_objects"]


@pytest.mark.parametrize("metadata", [{"quoted_text":"Another person's claim"}, {"is_forwarded":True}, {"quoted_message_id":"foreign-message"}])
def test_known_native_quote_metadata_cannot_be_overridden_by_review(corpus, metadata):
    edit(corpus, "ALTER TABLE conversation_messages ADD COLUMN metadata_json TEXT")
    edit(corpus, "UPDATE conversation_messages SET metadata_json=?", (json.dumps(metadata),))
    attest(corpus)
    assert decision(corpus).reason_code == "not_owner_self_statement"


def test_review_creation_cannot_stamp_a_stale_owner_inspection(corpus):
    review = attest(corpus)
    edit(corpus, "UPDATE conversation_messages SET content='changed after inspection'")
    with owner():
        with pytest.raises(PolicyError, match="review_stale"):
            corpus[1].record_review(resolver=corpus[0], review_id="stale-inspection", expected_snapshot=review.snapshot,
                                   classifications=review.classifications, reviewed_at=1101)


def test_two_fact_cycle_withholds(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        child = FactStore(conn).assert_fact(subject_entity_id="self", predicate="member_of", object_value="reading group", disclosure="scoped",
            source_refs=[{"table":"signal_objects","record_id":corpus[2]}])
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (json.dumps([{"table":"signal_objects","record_id":child["object_id"]}]), corpus[2]))
    assert decision(corpus).reason_code == "lineage_cycle"


def test_legacy_pack_corpus_shape_never_becomes_qualified(corpus):
    # Synthetic reconstruction of the audit's aggregate shape; no owner corpus
    # contents are read or committed. Rich refs do not override owner_only.
    ids = []
    with sqlite3.connect(corpus[0].path) as conn:
        for index in range(200):
            fact = FactStore(conn).assert_fact(subject_entity_id="self", predicate="prefers", object_value=f"synthetic topic {index}",
                source_refs=[{"table":"conversation_messages","dataset_id":"dataset-1","source_id":"source-1","record_id":"message-1"}] if index < 10 else [],
                disclosure="owner_only")
            ids.append(fact["object_id"])
    for fact_id in ids:
        result = corpus[0].qualify(fact_id, reviews=corpus[1])
        assert result.verdict == "withheld" and result.reason_code == "owner_only" and result.evidence is None


@pytest.mark.parametrize("stored,embedded", [("inferred", "stated"), ("stated", "inferred"), ("unknown", "stated"), ("stated", None)])
def test_conflicting_or_unknown_altitude_cannot_be_masked_by_other_representation(corpus, stored, embedded):
    edit(corpus, "ALTER TABLE signal_objects ADD COLUMN altitude TEXT")
    edit(corpus, "UPDATE signal_objects SET altitude=?", (stored,))
    payload(corpus, altitude=embedded)
    attest(corpus)
    assert decision(corpus).reason_code == "unsupported_fact_altitude"


@pytest.mark.parametrize("extractor", ["", "unknown", "pack-extractor-v1"])
def test_absent_altitude_is_not_general_permission_to_treat_extracted_facts_as_stated(corpus, extractor):
    edit(corpus, "UPDATE signal_objects SET extractor_version=?", (extractor,))
    attest(corpus)
    assert decision(corpus).reason_code == "unsupported_fact_altitude"


def test_native_fact_store_null_schema_altitude_has_explicit_reviewed_positive(corpus):
    edit(corpus, "ALTER TABLE signal_objects ADD COLUMN altitude TEXT")
    assert decision(corpus).reason_code == "owner_review_required"
    attest(corpus)
    assert decision(corpus).verdict == "qualified"


def test_explicit_matching_stated_altitudes_have_reviewed_positive(corpus):
    edit(corpus, "ALTER TABLE signal_objects ADD COLUMN altitude TEXT")
    edit(corpus, "UPDATE signal_objects SET altitude='stated',extractor_version='pack-extractor-v1'")
    payload(corpus, altitude="stated", actor_role="authored")
    attest(corpus)
    assert decision(corpus).verdict == "qualified"


def test_review_file_replacement_with_full_snapshot_review_fails_closed(corpus, tmp_path):
    attest(corpus)
    replacement = tmp_path / "replacement.db"
    shutil.copyfile(corpus[1].path, replacement)
    replacement.chmod(0o600)
    os.replace(replacement, corpus[1].path)
    result = decision(corpus)
    assert result.verdict == "withheld" and result.reason_code == "review_database_binding"
    assert result.evidence is None


@pytest.mark.parametrize("assignment", ["binding_json='{}'", "file_revision='forged'", "clock_id='forged'", "singleton=2"])
def test_persisted_review_identity_is_revalidated_every_open(corpus, assignment):
    attest(corpus)
    with sqlite3.connect(corpus[1].path) as conn:
        # Removing the row simulates a corrupted store without defeating its
        # CHECK constraint merely for the test.
        conn.execute("DELETE FROM review_identity" if assignment == "singleton=2" else "UPDATE review_identity SET " + assignment)
    assert decision(corpus).reason_code == "review_database_binding"


def test_review_permissions_are_revalidated_after_construction(corpus):
    attest(corpus)
    corpus[1].path.chmod(0o644)
    assert decision(corpus).reason_code == "review_store_permissions"


def test_review_final_symlink_replacement_is_rejected(corpus, tmp_path):
    attest(corpus)
    original = tmp_path / "original-reviews.db"
    corpus[1].path.rename(original)
    corpus[1].path.symlink_to(original)
    assert decision(corpus).reason_code == "review_database_binding"


def test_review_parent_symlink_is_rejected_at_creation_and_each_open(corpus, tmp_path):
    parent = tmp_path / "private-review-store"
    parent.mkdir()
    with owner():
        reviews = EvidenceReviewStore(parent / "reviews.db", resolver=corpus[0])
    nested = (corpus[0], reviews, corpus[2])
    attest(nested)
    moved = tmp_path / "moved-private-review-store"
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    assert decision(nested).reason_code == "review_database_binding"
    with owner(), pytest.raises(PolicyError, match="review_database_binding"):
        EvidenceReviewStore(parent / "reviews.db", resolver=corpus[0])


def test_canonical_replacement_is_rejected_even_if_content_matches(corpus, tmp_path):
    attest(corpus)
    replacement = tmp_path / "replacement-canonical.db"
    shutil.copyfile(corpus[0].path, replacement)
    os.replace(replacement, corpus[0].path)
    assert decision(corpus).reason_code == "evidence_database_binding"


def test_observed_protection_rollback_cannot_revive_review_across_restart(corpus):
    attest(corpus)
    with sqlite3.connect(corpus[0].path) as conn:
        RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="message-1")
    assert decision(corpus).reason_code == "owner_only"
    edit(corpus, "DELETE FROM owner_only_records")
    edit(corpus, "UPDATE permissions_v2_protection_state SET generation=0")
    assert decision(corpus).reason_code == "review_protection_clock"
    with owner(), pytest.raises(PolicyError, match="review_protection_clock"):
        EvidenceReviewStore(corpus[1].path, resolver=corpus[0])


def test_private_review_clock_high_water_cannot_regress_in_a_running_process(corpus):
    attest(corpus)
    with sqlite3.connect(corpus[0].path) as conn:
        RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="message-1")
    assert decision(corpus).reason_code == "owner_only"
    with sqlite3.connect(corpus[1].path) as conn:
        conn.execute("UPDATE review_identity SET highest_generation=0")
    assert decision(corpus).reason_code == "review_protection_clock"


def test_clock_identity_replacement_is_rejected(corpus):
    attest(corpus)
    edit(corpus, "UPDATE permissions_v2_protection_state SET clock_id=?", ("f" * 64,))
    assert decision(corpus).reason_code == "review_protection_clock"


def test_owner_subject_alias_does_not_hide_an_independent_fact_copy(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        FactStore(conn).assert_fact(subject_entity_id="owner-entity", predicate="prefers", object_value="history books",
            disclosure="owner_only", source_refs=[])
    attest(corpus)
    assert decision(corpus).reason_code == "independent_copy_lineage"


@pytest.mark.parametrize("damage", ["DELETE FROM review_identity", "DROP TABLE review_identity", "DROP TABLE fact_reviews"])
def test_existing_review_store_cannot_reenroll_after_identity_or_schema_loss(corpus, damage):
    attest(corpus)
    with sqlite3.connect(corpus[0].path) as conn:
        RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="message-1")
    assert decision(corpus).reason_code == "owner_only"
    edit(corpus, "DELETE FROM owner_only_records")
    edit(corpus, "UPDATE permissions_v2_protection_state SET generation=0")
    with sqlite3.connect(corpus[1].path) as conn:
        conn.execute(damage)
    with owner(), pytest.raises(PolicyError):
        EvidenceReviewStore(corpus[1].path, resolver=corpus[0])
    assert decision(corpus).verdict == "withheld"


def test_existing_empty_file_is_not_a_first_enrollment(corpus, tmp_path):
    existing = tmp_path / "empty.db"
    existing.touch(mode=0o600)
    with owner(), pytest.raises(PolicyError, match="review_storage_unavailable"):
        EvidenceReviewStore(existing, resolver=corpus[0])


@pytest.mark.parametrize("subject", [[], {}, None, 42])
def test_malformed_native_subject_is_bounded_withholding(corpus, subject):
    payload(corpus, subject_entity_id=subject)
    attest(corpus)
    assert decision(corpus).verdict == "withheld"


def test_intact_review_and_canonical_identity_survive_normal_service_restart(corpus):
    attest(corpus)
    resolver = EvidenceResolver(corpus[0].path, binding=corpus[0].binding)
    with owner():
        reopened = EvidenceReviewStore(corpus[1].path, resolver=resolver)
    assert resolver.qualify(corpus[2], reviews=reopened).verdict == "qualified"


def canonical_role(corpus, table, role):
    edit(corpus, f"ALTER TABLE {table} ADD COLUMN actor_role TEXT")
    edit(corpus, f"UPDATE {table} SET actor_role=?", (role,))


def ai_lineage(corpus):
    edit(corpus, "UPDATE signal_objects SET source_refs_json=?", (json.dumps([
        {"table":"ai_chat_messages","source_id":"ai-source-1","record_id":"ai-message-1"}]),))


def source_settings(corpus, posture, *, dataset="dataset-1", source="source-1"):
    edit(corpus, "CREATE TABLE IF NOT EXISTS user_ingestion_sources(dataset_id TEXT, source_id TEXT, posture TEXT)")
    edit(corpus, "INSERT INTO user_ingestion_sources VALUES (?,?,?)", (dataset,source,posture))


def runtime_source(corpus, posture, *, source="source-1"):
    edit(corpus, "CREATE TABLE IF NOT EXISTS source_runtime_installs(source_id TEXT, is_active INTEGER, status TEXT, source_definition_json TEXT)")
    edit(corpus, "INSERT INTO source_runtime_installs VALUES (?,1,'active',?)", (source,json.dumps({"source_id":source,"posture":posture})))


@pytest.mark.parametrize("table", ["signal_objects","conversation_messages","ai_chat_messages"])
@pytest.mark.parametrize("role", ["addressed","participated","observed","ambient","unknown","inferred",""])
def test_explicit_canonical_role_veto_cannot_be_overridden_by_native_flags_or_review(corpus, table, role):
    if table == "ai_chat_messages": ai_lineage(corpus)
    canonical_role(corpus, table, role)
    payload(corpus, actor_role="authored")
    attest(corpus)
    assert decision(corpus).verdict == "withheld"


@pytest.mark.parametrize("table", ["signal_objects","conversation_messages","ai_chat_messages"])
@pytest.mark.parametrize("role", [None,"authored"])
def test_null_legacy_or_authored_role_still_requires_native_truth_and_review(corpus, table, role):
    if table == "ai_chat_messages": ai_lineage(corpus)
    canonical_role(corpus, table, role)
    assert decision(corpus).reason_code == "owner_review_required"
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    if table == "signal_objects": payload(corpus, asserted_by="another-person")
    elif table == "conversation_messages": edit(corpus,"UPDATE conversation_messages SET is_from_self=0")
    else: edit(corpus,"UPDATE ai_chat_messages SET sender_type='assistant'")
    attest(corpus,review_id="review-2")
    assert decision(corpus).verdict == "withheld"


@pytest.mark.parametrize("kind", ["override","runtime"])
def test_ambient_posture_caps_native_authored_even_after_owner_review(corpus, kind):
    (source_settings if kind == "override" else runtime_source)(corpus,"ambient")
    attest(corpus)
    assert decision(corpus).verdict == "withheld"


@pytest.mark.parametrize("kind", ["override","runtime"])
def test_source_posture_changes_invalidate_existing_owner_review(corpus, kind):
    (source_settings if kind == "override" else runtime_source)(corpus,"mixed")
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    if kind == "override": edit(corpus,"UPDATE user_ingestion_sources SET posture='ambient'")
    else: edit(corpus,"UPDATE source_runtime_installs SET source_definition_json=?",(json.dumps({"posture":"ambient"}),))
    assert decision(corpus).reason_code == "review_stale"
    attest(corpus,review_id="review-2")
    assert decision(corpus).verdict == "withheld"


@pytest.mark.parametrize("posture", ["ambient","unknown","", " AMBIENT "])
def test_datasetless_ai_cannot_borrow_another_datasets_permissive_override(corpus, posture):
    ai_lineage(corpus)
    source_settings(corpus,"personal",dataset="first-dataset",source="ai-source-1")
    source_settings(corpus,posture,dataset="second-dataset",source="ai-source-1")
    if posture == "ambient": attest(corpus)
    assert decision(corpus).verdict == "withheld"


def test_datasetless_ai_posture_change_invalidates_review(corpus):
    ai_lineage(corpus)
    source_settings(corpus,"mixed",source="ai-source-1")
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    edit(corpus,"UPDATE user_ingestion_sources SET posture='ambient'")
    assert decision(corpus).reason_code == "review_stale"


@pytest.mark.parametrize("case", ["invalid_override", "duplicate_override", "invalid_runtime", "runtime_object", "runtime_list",
    "duplicate_runtime", "wrong_source", "broken_json", "duplicate_json", "missing_posture_column", "unknown_active", "unknown_status", "view_instead_of_table"])
def test_unknown_or_ambiguous_posture_never_falls_back_to_authored(corpus, case):
    if case == "invalid_override": source_settings(corpus,"unknown")
    elif case == "duplicate_override": source_settings(corpus,"personal"); source_settings(corpus,"mixed")
    elif case in {"invalid_runtime","runtime_object","runtime_list"}:
        runtime_source(corpus,{"invalid_runtime":"unknown","runtime_object":{},"runtime_list":[]}[case])
    elif case == "duplicate_runtime": runtime_source(corpus,"personal"); runtime_source(corpus,"ambient")
    elif case in {"wrong_source","broken_json","duplicate_json","unknown_active","unknown_status"}:
        runtime_source(corpus,"mixed")
        if case == "unknown_active": edit(corpus,"UPDATE source_runtime_installs SET is_active=2")
        elif case == "unknown_status": edit(corpus,"UPDATE source_runtime_installs SET status='unknown'")
        else:
            raw = {"wrong_source":'{"source_id":"another-source","posture":"personal"}',"broken_json":"{", "duplicate_json":'{"posture":"ambient","posture":"personal"}'}[case]
            edit(corpus,"UPDATE source_runtime_installs SET source_definition_json=?",(raw,))
    elif case == "missing_posture_column": edit(corpus,"CREATE TABLE user_ingestion_sources(dataset_id TEXT,source_id TEXT)")
    else: edit(corpus,"CREATE VIEW user_ingestion_sources AS SELECT 'dataset-1' AS dataset_id,'source-1' AS source_id,'personal' AS posture")
    assert decision(corpus).verdict == "withheld"


@pytest.mark.parametrize("posture", [None,"mixed","personal"])
def test_valid_legacy_and_owner_overrides_preserve_native_authored_availability(corpus, posture):
    source_settings(corpus,posture)
    runtime_source(corpus,None)
    attest(corpus)
    assert decision(corpus).verdict == "qualified"


def test_conversation_uses_exact_dataset_and_source_override(corpus):
    source_settings(corpus,"ambient",dataset="another-dataset")
    source_settings(corpus,"ambient",source="another-source")
    attest(corpus)
    assert decision(corpus).verdict == "qualified"


def test_bundled_ambient_cap_survives_missing_or_mixed_runtime_default(corpus, monkeypatch):
    from types import SimpleNamespace
    from topos.sources import registry
    monkeypatch.setitem(registry.BUNDLED_REGISTRY,"source-1",SimpleNamespace(posture="ambient"))
    runtime_source(corpus,"mixed")
    attest(corpus)
    assert decision(corpus).verdict == "withheld"
    source_settings(corpus,"personal")  # Exact owner override remains authoritative.
    attest(corpus,review_id="review-2")
    assert decision(corpus).verdict == "qualified"


def test_bundled_posture_change_invalidates_review(corpus, monkeypatch):
    from types import SimpleNamespace
    from topos.sources import registry
    attest(corpus)
    monkeypatch.setitem(registry.BUNDLED_REGISTRY,"source-1",SimpleNamespace(posture="ambient"))
    assert decision(corpus).reason_code == "review_stale"


def test_posture_resolution_never_opens_global_database_or_trusts_process_runtime_registry(corpus, monkeypatch):
    from types import SimpleNamespace
    from topos.sources import registry
    import topos.core.state as state
    def forbidden(*args, **kwargs): pytest.fail("global database/posture fallback was used")
    monkeypatch.setattr(state,"get_db_connection",forbidden)
    monkeypatch.setattr(registry,"effective_posture",forbidden)
    monkeypatch.setattr(registry,"_registry_posture_default",forbidden)
    monkeypatch.setitem(registry.REGISTRY,"source-1",SimpleNamespace(posture="personal"))
    runtime_source(corpus,"ambient")
    attest(corpus)
    assert decision(corpus).verdict == "withheld"


@pytest.mark.parametrize("table", ["signal_objects","conversation_messages","ai_chat_messages"])
def test_reserved_source_revision_marker_cannot_be_spoofed_by_canonical_column(corpus, table):
    if table == "ai_chat_messages": ai_lineage(corpus)
    edit(corpus,f"ALTER TABLE {table} ADD COLUMN _p2b_source_revision TEXT")
    assert decision(corpus).reason_code == "evidence_malformed"


@pytest.mark.parametrize("field,value", [("user_id","other-owner"),("topos_id","other-resource"),("dataset_id","other-dataset"),("device_id","unknown-device")])
def test_scoped_runtime_posture_cannot_lend_authority_across_bindings(corpus, field, value):
    runtime_source(corpus,"personal")
    edit(corpus,"ALTER TABLE source_runtime_installs ADD COLUMN scope_key TEXT")
    scope = {"user_id":"owner-1","topos_id":"resource-1","dataset_id":"dataset-1","device_id":"*"}
    scope[field] = value
    edit(corpus,"UPDATE source_runtime_installs SET scope_key=?",(json.dumps(scope),))
    assert decision(corpus).reason_code == "source_posture_unknown"


def test_exact_scoped_runtime_posture_and_revision_are_bound(corpus):
    runtime_source(corpus,"mixed")
    edit(corpus,"ALTER TABLE source_runtime_installs ADD COLUMN scope_key TEXT")
    edit(corpus,"UPDATE source_runtime_installs SET scope_key=?",(json.dumps({"user_id":"owner-1","topos_id":"resource-1","dataset_id":"dataset-1","device_id":"*"}),))
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    edit(corpus,"UPDATE source_runtime_installs SET status='ready'")
    assert decision(corpus).reason_code == "review_stale"
