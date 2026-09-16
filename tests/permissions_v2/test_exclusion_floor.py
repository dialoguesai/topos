"""Exclusion precedes purge; clock upgrade preserves history and invalidates work."""
from contextlib import ExitStack
import sqlite3
from unittest.mock import patch

import pytest

from tests.permissions_v2.test_evidence import corpus,owner,attest,decision,edit
from tests.permissions_v2.test_fact_release import fact_setup,timed,projection_service,issue,dispatch
from tests.permissions_v2.test_release import release_setup,issue as source_issue,dispatch as source_dispatch
from topos.permissions_v2.canonical import PolicyError,digest
from topos.permissions_v2.protection_clock import (EVENTS,LEDGER,REGISTRY,TABLE,TRIGGERS,V2_TRIGGERS,V3_TRIGGERS,
    LEGACY_TRIGGERS,clock_state,current_protection_revision,ensure_protection_clock,upgrade_protection_clock_v2,
    upgrade_protection_clock_v3,upgrade_protection_clock_v4)
from topos.features.lifecycle.exclusions import ExclusionStore


def add(corpus,kind,key):
    with sqlite3.connect(corpus[0].path) as db:
        ExclusionStore(db)._tombstone(kind,key,None)
        db.commit()


@pytest.mark.parametrize("profile",["fact","source"])
def test_native_record_tombstone_commits_before_purge_and_already_blocks_signed_release(request,profile,monkeypatch):
    setup=request.getfixturevalue("fact_setup" if profile=="fact" else "release_setup")
    envelope,payload=(issue if profile=="fact" else source_issue)(setup)
    def before_purge(conn,ids):
        assert conn.execute("SELECT COUNT(*) FROM intelligence_exclusions WHERE artifact_type='record'").fetchone()[0]==1
        with pytest.raises(PolicyError):
            (dispatch if profile=="fact" else source_dispatch)(setup,envelope,payload,
                send=lambda *_:pytest.fail("excluded output before purge"))
        assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]==1
        return {"probe_stopped_before_purge":True}
    monkeypatch.setattr("topos.features.lifecycle.derived_scrub.purge_derived_for_records",before_purge)
    with owner(),sqlite3.connect(setup[5][0].path) as db:
        ExclusionStore(db).exclude_record(record_id="message-1")


@pytest.mark.parametrize("kind,key,reason",[("record","message-1","intelligence_excluded"),
    ("fact","self:prefers","intelligence_excluded"),("fact","self:prefers:history books","intelligence_excluded"),
    ("fact","owner-entity:prefers","intelligence_excluded"),("entity","unresolved protected name","entity_exclusion_lineage_unavailable")])
def test_current_exclusion_veto_cannot_be_overridden_by_fresh_owner_review(corpus,kind,key,reason):
    add(corpus,kind,key);attest(corpus)
    result=decision(corpus)
    assert result.verdict=="withheld" and result.reason_code==reason
    assert result.evidence is None


def test_record_tombstone_is_checked_before_loading_that_source(corpus,monkeypatch):
    add(corpus,"record","message-1");attest(corpus)
    original=corpus[0]._load
    def load(conn,identity):
        assert identity.record_id!="message-1", "excluded source body loaded"
        return original(conn,identity)
    monkeypatch.setattr(corpus[0],"_load",load)
    assert decision(corpus).reason_code=="intelligence_excluded"


def test_entity_exclusion_withholds_before_any_candidate_load(corpus,monkeypatch):
    add(corpus,"entity","any name")
    monkeypatch.setattr(corpus[0],"_load",lambda *_:pytest.fail("entity-excluded candidate loaded"))
    assert decision(corpus).reason_code=="entity_exclusion_lineage_unavailable"


@pytest.mark.parametrize("kind,key",[("record","unrelated-record"),("fact","self:likes:music"),
    ("fact","someone-else:prefers"),("stat_insight","unrelated:group")])
def test_unrelated_known_nonentity_tombstones_preserve_positive_after_fresh_review(corpus,kind,key):
    add(corpus,kind,key);attest(corpus)
    assert decision(corpus).verdict=="qualified"


@pytest.mark.parametrize("damage",["missing_table","missing_column","unknown_kind","empty_key","invalid_fact_key","malformed_metadata"])
def test_unknown_or_damaged_exclusions_never_mean_empty(corpus,damage):
    with sqlite3.connect(corpus[0].path) as db:
        if damage=="missing_table":db.execute("DROP TABLE intelligence_exclusions")
        elif damage=="missing_column":db.execute("ALTER TABLE intelligence_exclusions RENAME COLUMN artifact_key TO lost_key")
        else:
            db.execute("INSERT INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key,note) VALUES('bad',?,?,?)",
                ("new_kind" if damage=="unknown_kind" else "fact" if damage=="invalid_fact_key" else "record",
                 "" if damage=="empty_key" else "unparseable" if damage=="invalid_fact_key" else "record-1",
                 b"opaque" if damage=="malformed_metadata" else None))
    assert decision(corpus).verdict=="withheld"


@pytest.mark.parametrize("kind,key,review",[("record","unrelated","qualified"),("fact","self:likes","qualified"),
    ("record","message-1","withheld"),("fact","self:prefers","withheld"),("entity","unrelated person","withheld")])
def test_add_remove_aba_without_intervening_read_invalidates_signed_work_and_only_touched_reviews(fact_setup,kind,key,review):
    envelope,payload=issue(fact_setup)
    corpus=fact_setup[5]
    with sqlite3.connect(corpus[0].path) as db:
        before=clock_state(db)
        store=ExclusionStore(db);store._tombstone(kind,key,None);db.commit();store.remove_exclusion(kind,key)
        after=clock_state(db)
    assert after==(before[0],before[1]+2)
    # Signed authority binds the node-wide revision, so every ABA stales it.
    with pytest.raises(PolicyError):dispatch(fact_setup,envelope,payload)
    # A review binds only its closure; entity exclusions stay node-wide.
    assert decision(corpus).verdict==review
    attest(corpus,review_id="fresh-evidence")
    assert decision(corpus).verdict=="qualified"


def make_legacy(corpus):
    """Rebuild the exact former v1 clock: no version column, six triggers, no event log."""
    with sqlite3.connect(corpus[0].path) as db:
        old=clock_state(db)
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchall():
            db.execute(f"DROP TRIGGER {name}")
        db.execute(f"DROP TABLE {EVENTS}")
        # v1 predates the identity ledger and registry entirely.
        db.execute(f"DROP TABLE {LEDGER}");db.execute(f"DROP TABLE {REGISTRY}")
        db.execute(f"CREATE TABLE {TABLE}_v1 (singleton INTEGER PRIMARY KEY CHECK(singleton=1), clock_id TEXT NOT NULL, generation INTEGER NOT NULL)")
        db.execute(f"INSERT INTO {TABLE}_v1 SELECT singleton,clock_id,generation FROM {TABLE}")
        db.execute(f"DROP TABLE {TABLE}");db.execute(f"ALTER TABLE {TABLE}_v1 RENAME TO {TABLE}")
        for sql in LEGACY_TRIGGERS.values():db.execute(sql)
    return old


def upgrade_to_current(path,old):
    upgrade_protection_clock_v2(path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1])
    upgrade_protection_clock_v3(path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1]+1)
    return upgrade_protection_clock_v4(path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1]+2)


def test_upgrade_is_explicit_monotone_and_never_repairs_partial_clock(corpus):
    old=make_legacy(corpus)
    with pytest.raises(PolicyError):ensure_protection_clock(corpus[0].path,owner_id="owner-1")
    result=upgrade_protection_clock_v2(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1])
    assert result=={"contract_version":2,"clock_id":old[0],"generation":old[1]+1,"already_current":False}
    assert upgrade_protection_clock_v2(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1])["already_current"]
    with pytest.raises(PolicyError):ensure_protection_clock(corpus[0].path,owner_id="owner-1")
    with sqlite3.connect(corpus[0].path) as db:db.execute("DROP TRIGGER permissions_v2_intelligence_exclusions_insert")
    with pytest.raises(PolicyError):upgrade_protection_clock_v2(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1])


def test_upgrade_v3_is_explicit_monotone_logs_events_and_never_repairs_partial_clock(corpus):
    old=make_legacy(corpus)
    upgrade_protection_clock_v2(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1])
    with pytest.raises(PolicyError):upgrade_protection_clock_v3(corpus[0].path,owner_id="owner-1",expected_clock_id="a"*64,expected_generation=old[1]+1)
    with pytest.raises(PolicyError):upgrade_protection_clock_v3(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1])
    result=upgrade_protection_clock_v3(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1]+1)
    assert result=={"contract_version":3,"clock_id":old[0],"generation":old[1]+2,"already_current":False}
    assert upgrade_protection_clock_v3(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1]+1)["already_current"]
    # A v3 clock is not the current contract, so the startup check still refuses it.
    with pytest.raises(PolicyError):ensure_protection_clock(corpus[0].path,owner_id="owner-1")
    assert upgrade_protection_clock_v4(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],
        expected_generation=old[1]+2)["generation"]==old[1]+3
    ensure_protection_clock(corpus[0].path,owner_id="owner-1")
    with sqlite3.connect(corpus[0].path) as db:
        assert db.execute(f"SELECT count(*) FROM {EVENTS}").fetchone()[0]==0
        from topos.features.lifecycle.record_protection import RecordProtectionStore
        RecordProtectionStore(db).protect(canonical_table="conversation_messages",record_id="message-1")
        RecordProtectionStore(db).unprotect(canonical_table="conversation_messages",record_id="message-1")
        events=db.execute(f"SELECT generation,source,artifact_key FROM {EVENTS} ORDER BY sequence").fetchall()
        assert events==[(old[1]+4,"owner_only_records","conversation_messages|message-1"),(old[1]+5,"owner_only_records","conversation_messages|message-1")]
        assert clock_state(db)==(old[0],old[1]+5)
        db.execute("DROP TRIGGER permissions_v2_owner_only_records_delete")
    with pytest.raises(PolicyError):upgrade_protection_clock_v3(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1]+1)
    with pytest.raises(PolicyError):ensure_protection_clock(corpus[0].path,owner_id="owner-1")


def test_upgrade_v3_refuses_a_v2_clock_with_a_stray_event_table(corpus):
    old=make_legacy(corpus)
    upgrade_protection_clock_v2(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1])
    with sqlite3.connect(corpus[0].path) as db:db.execute(f"CREATE TABLE {EVENTS}(x)")
    with pytest.raises(PolicyError):upgrade_protection_clock_v3(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1]+1)


@pytest.mark.parametrize("damage",["trigger","clock_id","generation","missing_v2_column"])
def test_upgrade_refuses_unknown_history_or_damaged_v2(corpus,damage):
    old=make_legacy(corpus)
    if damage=="missing_v2_column":
        upgrade_protection_clock_v2(corpus[0].path,owner_id="owner-1",expected_clock_id=old[0],expected_generation=old[1])
        with sqlite3.connect(corpus[0].path) as db:db.execute(f"ALTER TABLE {TABLE} DROP COLUMN contract_version")
    elif damage=="trigger":
        with sqlite3.connect(corpus[0].path) as db:db.execute("DROP TRIGGER permissions_v2_owner_only_records_insert")
    with pytest.raises(PolicyError):
        upgrade_protection_clock_v2(corpus[0].path,owner_id="owner-1",expected_clock_id="a"*64 if damage=="clock_id" else old[0],
            expected_generation=old[1]+1 if damage=="generation" else old[1])


def test_upgrade_invalidates_initialized_v1_ledger_and_reviews_then_fresh_state_restores_unaffected(timed,projection_service,tmp_path):
    from topos.permissions_v2 import protection_clock,evidence,node_protocol
    from topos.features.lifecycle.record_protection import protection_fingerprint
    from topos.permissions_v2.evidence_reviews import EvidenceLookup
    from topos.permissions_v2.projection_reviews import RecordProjectionReview
    from topos.permissions_v2.signing import FactEnvelopeBody,request_digest,sign_envelope
    from topos.permissions_v2.protocol import StatusRequestBody,sign_status_request,verify_ack
    from tests.permissions_v2.test_node_protocol import reopen
    from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
    old=make_legacy(timed)
    def legacy_clock(conn):return tuple(conn.execute(f"SELECT clock_id,generation FROM {TABLE} WHERE singleton=1").fetchone())
    def legacy_revision(conn,*,owner_id):
        clock_id,generation=legacy_clock(conn)
        return digest({"clock_id":clock_id,"generation":generation,"protection":protection_fingerprint(conn)})
    # Only fixture construction uses the exact former-v1 revision formula; the
    # upgrade and all post-upgrade service decisions use the actual new code.
    with ExitStack() as stack:
        for module in (protection_clock,evidence,node_protocol):
            stack.enter_context(patch.object(module,"clock_state",legacy_clock))
            stack.enter_context(patch.object(module,"current_protection_revision",legacy_revision))
        # v1 reviews bound the node-wide revision; the current closure binding
        # did not exist yet and its event log is absent from a v1 database.
        stack.enter_context(patch.object(evidence,"closure_protection_revision",
            lambda conn,*,owner_id,records,fact_prefixes,identity=None:legacy_revision(conn,owner_id=owner_id)))
        # A v1 database has no restriction registry and no identity state at all,
        # so fixture construction uses the sole-self rule that shipped with it.
        stack.enter_context(patch.object(evidence,"restriction_subjects",evidence.legacy_owner_subjects))
        stack.enter_context(patch.object(evidence,"closure_identity",lambda conn,*,subjects,fact_ids:None))
        setup=fact_setup.__wrapped__(timed,projection_service,tmp_path)
        envelope,payload=issue(setup)
    upgrade_to_current(timed[0].path,old)
    service=setup[0]
    service.protocol=reopen((service.protocol,setup[1],setup[2],setup[3]))
    status=sign_status_request(StatusRequestBody.parse({"version":"topos-policy-status-request/v2","kid":"cp-key",
        "issuer_id":"cp-issuer","audience_id":service.protocol.ledger.identity.node_id,"request_id":"post-upgrade-status",
        "binding":setup[1]["binding"],"command_id":None,"command_hash":None,"issued_at":setup[4][0],"expires_at":setup[4][0]+100}),setup[2])
    ack=service.protocol.status(status.model_dump(),now=setup[4][0])
    verified=verify_ack(ack.model_dump(),trusted_keys={"node-key":setup[3].public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)},
        issuer_id=service.protocol.ledger.identity.node_id,audience_id="cp-issuer",request=status,now=setup[4][0])
    assert verified.state.node_epoch==envelope.node_epoch+1
    assert verified.state.protection_revision!=envelope.protection_revision
    with pytest.raises(PolicyError):dispatch(setup,envelope,payload)
    assert decision(timed).verdict=="withheld"
    attest(timed,review_id="post-upgrade-evidence")
    service=setup[0]
    with owner():
        preview=projection_service.preview(EvidenceLookup(fact_id=timed[2]),now=setup[4][0])
        projection_service.record(RecordProjectionReview(review_id="post-upgrade-output",expected_candidate=preview.candidate,
            expected_candidate_hash=preview.candidate_hash,expected_current_review_revision=setup[6].review_revision,
            classification={"domains":["reading"],"sensitivity":"personal","subject":"self","assertion":"explicit_atomic_preference"}),now=setup[4][0])
        authority=service.protocol.ledger.authority_snapshot("grant-1",now=setup[4][0])
    body=FactEnvelopeBody.parse({**authority.model_dump(),"version":"topos-grantee-envelope/v2","kid":"cp-key","request_id":"fresh-read",
        "request_type":"permissions.v2.fact.read","request_hash":request_digest("permissions.v2.fact.read",payload),
        "issued_at":setup[4][0],"expires_at":setup[4][0]+100})
    fresh=sign_envelope(body,setup[2])
    assert dispatch(setup,fresh,payload,request_id="fresh-read")[0][1]["value"]=="history books"


@pytest.mark.parametrize("subject,value",[("self",None),("owner-entity",None),("self","History   Books")])
def test_native_fact_exclusion_normalization_vetoes_legacy_key_not_closed_by_writer(corpus,subject,value):
    # Old/non-FactStore object keys need the tombstone veto even when native
    # soft-close lookup does not find the matching semantic claim.
    edit(corpus,"UPDATE signal_objects SET object_key='legacy-pack-shaped-key'")
    with owner(),sqlite3.connect(corpus[0].path) as db:
        result=ExclusionStore(db).exclude_fact(subject_entity_id=subject,predicate="  PrEfErS  ",object_value=value)
        assert result["facts_closed"]==0
    attest(corpus)
    assert decision(corpus).reason_code=="intelligence_excluded"


def test_record_tombstone_fences_recursive_ancestor_before_terminal_materialization(corpus,monkeypatch):
    import json
    with sqlite3.connect(corpus[0].path) as db:
        db.row_factory=sqlite3.Row
        root=dict(db.execute("SELECT * FROM signal_objects WHERE object_id=?",(corpus[2],)).fetchone())
        root["object_id"]="child-fact";root["object_key"]="child-key"
        payload=json.loads(root["payload_json"]);payload["predicate"]="enjoys";root["payload_json"]=json.dumps(payload)
        columns=list(root)
        db.execute("INSERT INTO signal_objects("+",".join(columns)+") VALUES("+",".join("?" for _ in columns)+")",tuple(root[c] for c in columns))
        db.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?",
            (json.dumps([{"table":"signal_objects","record_id":"child-fact"}]),corpus[2]))
    add(corpus,"record","message-1");attest(corpus)
    original=corpus[0]._load
    loaded=[]
    def load(conn,identity):
        assert identity.record_id!="message-1"
        loaded.append(identity.record_id)
        return original(conn,identity)
    monkeypatch.setattr(corpus[0],"_load",load)
    assert decision(corpus).reason_code=="intelligence_excluded"
    assert loaded==[corpus[2],"child-fact"]


def test_sql_read_error_never_becomes_empty_exclusions(corpus,monkeypatch):
    from topos.permissions_v2.exclusion_floor import exclusions
    class Broken:
        def execute(self,*args):raise sqlite3.OperationalError("private database error")
    with pytest.raises(PolicyError,match="exclusion_schema_unavailable") as caught:exclusions(Broken())
    assert "private" not in str(caught.value)
