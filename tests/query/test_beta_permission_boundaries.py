"""Regression canaries at the saved-manifest, SQL, disclosure and model boundaries."""
import itertools
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared.filtering import FilterInstance, FilterManifest, merge_filter_manifests
from topos.uma_filters import apply_filter_manifest, apply_filter_manifest_async, build_sql_constraints
from topos.query.disclosure import DisclosureFilterPipeline
from topos.query.inference import run_query_inference
from topos.query.types import RetrievalBundle


def manifest(fid, values):
    key = "source_ids" if fid == "source_filter" else "fields"
    return FilterManifest(filters=[FilterInstance(filter_id=fid, params={key: values})])


@pytest.mark.parametrize("fid", ["source_filter", "column_allowlist"])
def test_restriction_meet_obeys_empty_absorption_and_associativity(fid):
    key = "source_ids" if fid == "source_filter" else "fields"
    subsets = [[], ["a"], ["b"], ["a", "b"]]
    for a, b, c in itertools.product(subsets, repeat=3):
        ma, mb, mc = [manifest(fid, v) for v in (a, b, c)]
        expected = sorted(set(a) & set(b) & set(c))
        for result in (
            merge_filter_manifests([ma, mb, mc]),
            merge_filter_manifests([mc, mb, ma]),
            merge_filter_manifests([ma, merge_filter_manifests([mb, mc])]),
        ):
            assert result.get_filter(fid).params[key] == expected


@pytest.mark.parametrize("fid", ["source_filter", "column_allowlist"])
@pytest.mark.asyncio
async def test_saved_explicit_empty_never_releases_rows_or_enrichments(fid):
    rows = [{"content": "CANARY_RAW", "source_id": "a", "sender_display_name": "CANARY_NAME", "sender_is_owner": True}]
    fm = FilterManifest.model_validate(manifest(fid, []).to_storage_dict())
    assert apply_filter_manifest(rows, None) == rows  # missing is identity
    assert apply_filter_manifest(rows, fm) == []
    assert await apply_filter_manifest_async(rows, fm) == []


def test_sql_empty_source_constraint_selects_nothing(tmp_path):
    import sqlite3

    with sqlite3.connect(tmp_path / "filter.db") as conn:
        conn.execute("CREATE TABLE records(source_id TEXT, content TEXT)")
        conn.execute("INSERT INTO records VALUES ('a', 'CANARY_RAW')")
        suffix, params = build_sql_constraints(manifest("source_filter", []), "")
        assert conn.execute("SELECT * FROM records WHERE 1=1" + suffix, params).fetchall() == []
        allowed, params = build_sql_constraints(manifest("source_filter", ["a"]), "")
        assert len(conn.execute("SELECT * FROM records WHERE 1=1" + allowed, params).fetchall()) == 1


def test_projection_does_not_reintroduce_unselected_sender_enrichment():
    rows = [{"content": "allowed", "sender_display_name": "CANARY_NAME", "sender_is_owner": True}]
    assert apply_filter_manifest(rows, manifest("column_allowlist", ["content"])) == [{"content": "allowed"}]


def test_scope_projection_metadata_survives_storage_and_meet():
    a = FilterManifest(access_mode_ceiling="raw", scope_table_allowlist={"messages:read": ["a"]})
    b = FilterManifest(access_mode_ceiling="summary", scope_table_allowlist={"messages:read": ["b"]})
    merged = FilterManifest.model_validate(merge_filter_manifests([a, b]).to_storage_dict())
    assert merged.scope_table_allowlist == {"messages:read": []}
    assert merged.access_mode_ceiling == "summary"
    from topos.uma_filters import query_filter_restriction_reason
    assert query_filter_restriction_reason(merged.to_storage_dict(), "raw", "messages:read") == "empty_allowlist"
    assert query_filter_restriction_reason(merged.to_storage_dict(), "summary", "messages:read") == "empty_allowlist"


@pytest.mark.parametrize("envelope", [False, True])
def test_disclosure_decodes_actual_cp_envelope_and_preserves_empty(envelope):
    fm = manifest("source_filter", []).to_storage_dict()
    if envelope:
        fm = {"filter_manifest": fm, "accessible_entity_ids": []}
    result = DisclosureFilterPipeline().apply(
        RetrievalBundle(context_packet={"rows": [{"content": "CANARY_RAW", "source_id": "a"}]}),
        filter_manifest=fm, access_mode="raw",
    )
    assert result.context_packet["rows"] == []


def test_canonical_singular_transform_applied_in_query_disclosure():
    result = DisclosureFilterPipeline().apply(
        RetrievalBundle(context_packet={"rows": [{"event_at": "2026-09-14T12:34:56Z"}]}),
        field_transforms=[{"field": "event_at", "transform_id": "timestamp_to_date"}], access_mode="raw",
    )
    assert result.context_packet["rows"] == [{"event_at": "2026-09-14"}]


@pytest.mark.parametrize("transform,value", [("timestamp_to_date", 123), ("unknown_required", "text")])
def test_mandatory_transform_failures_do_not_release_original_field(transform, value):
    from topos.uma_filters import UMAFilterError
    with pytest.raises(UMAFilterError):
        DisclosureFilterPipeline().apply(
            RetrievalBundle(context_packet={"rows": [{"event_at": value}]}),
            field_transforms=[{"field": "event_at", "transform_id": transform}], access_mode="raw")


@pytest.mark.parametrize("filters", [
    {"filter_manifest": {"filters": [{"filter_id": "column_blocklist", "params": {"fields": ["summary_text"]}}]}},
    {"filter_manifest": {"filters": []}, "field_transforms": [{"field": "summary_text", "transform_id": "timestamp_to_date"}]},
])
def test_unsupported_derived_field_obligations_withhold(filters):
    from topos.uma_filters import query_filter_restriction_reason
    assert query_filter_restriction_reason(filters, "summary") == "derived_filter_lineage_unavailable"


def test_inference_model_never_receives_raw_semantic_or_unknown_fields():
    engine = MagicMock()
    engine.run.return_value = SimpleNamespace(status="completed", output={"answer": "yes", "confidence": .8})
    packet = {
        "scope_id": "work_context:read", "access_mode": "inference",
        "semantic_hits": [{"record_id": "a", "similarity": .9, "source_id": "allowed",
                           "search_text": "CANARY_RAW", "new_future_field": "CANARY_FUTURE"}],
        "scores": [{"value": .8, "confidence": .9, "content": "CANARY_SCORE"}],
        "future_extension": {"raw_payload": "CANARY_EXTENSION"},
    }
    filtered = DisclosureFilterPipeline().apply(RetrievalBundle(context_packet=packet), access_mode="inference")
    run_query_inference(query_text="Any relevant activity?", context_packet=filtered.context_packet,
                        scope_id="work_context:read", engine=engine)
    context = json.loads(engine.run.call_args.args[0].input["context"])
    assert "CANARY_" not in json.dumps(context)
    assert context["semantic_hits"][0]["similarity"] == .9
    assert context["scores"][0]["value"] == .8


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,values,reason", [("raw", [], "empty_allowlist"), ("summary", [], "empty_allowlist"), ("summary", ["a"], "derived_filter_lineage_unavailable")])
async def test_filters_constrain_all_query_paths_before_retrieval(monkeypatch, mode, values, reason):
    from topos.query.pipeline import QueryPipelineOrchestrator
    from topos.query.manifest_validation import resolve_scope_manifest

    orch = QueryPipelineOrchestrator()
    monkeypatch.setattr(orch, "_retrieve_on_calling_thread", lambda _: pytest.fail("restricted evidence reached retrieval"))
    result = await orch.execute(query_text="Show messages", scope_id="messages:read", access_mode=mode,
                                manifest=resolve_scope_manifest("messages:read"),
                                filter_manifest={"filter_manifest": manifest("source_filter", values).to_storage_dict()})
    assert result["deny_reason"] == reason


def test_semantic_retrieval_projection_excludes_raw_and_future_fields(monkeypatch, tmp_path, request):
    import topos.query.retrieval as retrieval
    from topos.query.manifest import ScopeResolutionManifest
    from topos.query.types import RetrievalRequest
    from topos.storage.adapters.factory import AdapterFactory

    adapters = AdapterFactory.from_runtime({"database_hosting_mode": "memory"})
    # A non-owner read needs observed empty protection state, even when the
    # canonical/vector adapter is in memory. Missing context must not authorize.
    import sqlite3
    from topos.storage.db.migrations import apply_all_migrations
    conn = sqlite3.connect(tmp_path / "protection.db")
    apply_all_migrations(conn)
    monkeypatch.setattr(adapters.signal, "_conn", conn, raising=False)
    request.addfinalizer(conn.close)
    monkeypatch.setattr(retrieval, "_bundle_is_global_db", lambda _: True)
    monkeypatch.setattr(retrieval, "_semantic_hits", lambda *a, **kw: ([{
        "record_id": "semantic-positive", "source_id": "synthetic", "similarity": .91,
        "search_text": "CANARY_RAW", "future_extension": "CANARY_FUTURE",
    }], None))
    scope = ScopeResolutionManifest(scope_id="messages:read", primary_dimensions=[], access_mode_ceiling="raw", default_source_ids=["synthetic"])
    bundle = retrieval.DefaultSignalRetrievalAdapter(adapters).retrieve(RetrievalRequest(manifest=scope, access_mode="inference", query_text="messages"))
    assert bundle.context_packet["semantic_hits"][0]["record_id"] == "semantic-positive"
    assert "CANARY_" not in json.dumps(bundle.context_packet)
