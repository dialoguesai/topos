"""Legacy live doors use the observed veto without reopening protected rows."""
from types import SimpleNamespace
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import corpus, edit  # noqa: F401
from tests.permissions_v2.test_entity_boundary import protected_corpus  # noqa: F401
from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
from topos.query import retrieval
from topos.query.entity_window import REFUSAL_UNRESOLVED
from topos.query.manifest import ScopeResolutionManifest
from topos.query.types import RetrievalRequest
from topos.uma_contact_enrichment import _apply_blackhole_to_message_rows


def guard(conn, *, owner=False):
    return BlackholeGuard(conn, caller_class=CallerClass.OWNER_UI if owner else CallerClass.GRANTEE)


def test_legacy_uma_nameless_protected_sender_is_withheld_and_owner_keeps_it(protected_corpus):
    edit(protected_corpus, "UPDATE conversation_messages SET sender_id='protected-contact'")
    public_rows = [{"message_id":"message-1","content":"A nameless private message."}]
    with sqlite3.connect(protected_corpus[0].path) as conn:
        assert _apply_blackhole_to_message_rows(public_rows, guard(conn)) == []
        assert _apply_blackhole_to_message_rows(public_rows, guard(conn, owner=True)) == public_rows


def test_legacy_uma_independent_message_remains_available(protected_corpus):
    public_rows = [{"message_id":"message-1","content":"I enjoy reading history books."}]
    with sqlite3.connect(protected_corpus[0].path) as conn:
        assert _apply_blackhole_to_message_rows(public_rows, guard(conn)) == public_rows


@pytest.mark.parametrize("table,blocked,permitted", [
    ("entities", {"entity_id":"protected-entity","canonical_name":"Old label"}, {"entity_id":"independent","canonical_name":"Kepler"}),
    ("entity_edges", {"subject_entity_id":"other","object_entity_id":"protected-entity"}, {"subject_entity_id":"other","object_entity_id":"independent"}),
    ("contacts", {"contact_id":"protected-contact","display_name":"Old label"}, {"contact_id":"independent","display_name":"Kepler"}),
    ("contact_identifiers", {"contact_id":"protected-contact","identifier":"2125550199"}, {"contact_id":"independent","identifier":"6465550110"}),
])
def test_legacy_graph_contacts_and_identifiers_have_protected_holes(protected_corpus,table,blocked,permitted):
    with sqlite3.connect(protected_corpus[0].path) as conn:
        assert guard(conn).filter_observed_canonical_rows([blocked,permitted], canonical_table=table) == [permitted]
        assert guard(conn,owner=True).filter_observed_canonical_rows([blocked,permitted], canonical_table=table) == [blocked,permitted]


def test_raw_retrieval_filters_before_redaction(protected_corpus,monkeypatch):
    rows = [{"entity_id":"protected-entity","canonical_name":"Old label"},
            {"entity_id":"independent","canonical_name":"Kepler"}]
    monkeypatch.setattr(retrieval,"_route_canonical_rows",lambda *a,**k: rows)
    projected = []
    def redact(scope,table,row):
        projected.append(row)
        return row
    monkeypatch.setattr(retrieval,"_redact_row_for_scope",redact)
    with sqlite3.connect(protected_corpus[0].path) as conn:
        adapter = retrieval.DefaultSignalRetrievalAdapter(SimpleNamespace(signal=SimpleNamespace(_conn=conn)))
        manifest = ScopeResolutionManifest(scope_id="graph:read",primary_dimensions=[],canonical_tables=["entities"],access_mode_ceiling="raw")
        result = adapter._retrieve_bundle(RetrievalRequest(manifest=manifest,access_mode="raw",disclosure_tier="scoped"))
    assert result.context_packet["rows"] == [{"_table":"entities",**rows[1]}]
    assert projected == [rows[1]]


@pytest.mark.parametrize("scope", ["attention:read","availability:read","complexity:read"])
def test_unproven_legacy_summary_addons_never_load_for_nonowner_with_protection(protected_corpus,monkeypatch,scope):
    def leak(*a,**k):
        raise AssertionError("Unproven derived data was loaded")
    for name in ("_load_attention_summary_items","_load_time_summary_items","_load_complexity_summary_items"):
        monkeypatch.setattr(retrieval,name,leak)
    with sqlite3.connect(protected_corpus[0].path) as conn:
        adapter = retrieval.DefaultSignalRetrievalAdapter(SimpleNamespace(signal=SimpleNamespace(_conn=conn)))
        manifest = ScopeResolutionManifest(scope_id=scope,primary_dimensions=[],access_mode_ceiling="raw")
        result = adapter._retrieve_bundle(RetrievalRequest(manifest=manifest,access_mode="summary",disclosure_tier="scoped"))
    assert result.context_packet == {"scope_id":scope,"access_mode":"summary","answer_type":"summary","summaries":[]}


def test_protected_and_nonexistent_entity_window_are_identical(protected_corpus,monkeypatch):
    from topos.features.entities import linking
    manifest = ScopeResolutionManifest(scope_id="messages:read",primary_dimensions=[],canonical_tables=["conversation_messages"],access_mode_ceiling="raw")
    with sqlite3.connect(protected_corpus[0].path) as conn:
        results = []
        for linked in ([{"entity_id":"protected-entity","canonical_name":"Mara Example"}], []):
            monkeypatch.setattr(linking,"link_query_entities",lambda *a, values=linked,**k: values)
            result = retrieval._derive_entity_anchored_window(manifest=manifest,conn=conn,query_text="What did I miss while working on Mara Example?",source_ids=["source-1"],disclosure_tier="scoped")
            results.append(result.as_packet())
    assert results[0] == results[1]
    assert results[0]["empty_reason"] == REFUSAL_UNRESOLVED


def test_legacy_uma_ai_prompt_uses_its_own_canonical_parent(protected_corpus):
    public_rows = [{"message_id":"ai-message-1","content":"I attend my reading group."}]
    with sqlite3.connect(protected_corpus[0].path) as conn:
        assert _apply_blackhole_to_message_rows(public_rows, guard(conn)) == public_rows


def test_legacy_egress_does_not_reuse_cached_identity_terms(protected_corpus):
    with sqlite3.connect(protected_corpus[0].path) as conn:
        cached = guard(conn)
        assert cached.active
        assert not cached.text_mentions_blackholed("Fresh synthetic alias")
        conn.execute("UPDATE entities SET aliases_json='[\"Fresh synthetic alias\"]' WHERE entity_id='protected-entity'")
        conn.commit()
        rows = [{"contact_id":"unlinked","display_name":"Fresh synthetic alias"}]
        assert cached.filter_observed_canonical_rows(rows,canonical_table="contacts") == []
