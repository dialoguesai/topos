"""Private source preparation must not install or write sample records."""
import json
import pytest

pytestmark = pytest.mark.public

from topos.sources import local_create


def definition():
    return {
        "source_id": "personal_notes", "display_name": "Personal notes",
        "source_type": "file", "delivery": "owner_upload",
        "schema_id": "personal.notes.v1", "parser_id": "personal.notes.v1",
        "canonical_mapper_id": "personal_notes", "canonical_group_id": "conversations",
        "posture": "mixed", "default_scope_id": "messages",
        "allowed_scope_ids": ["messages:read"], "enrichment_trigger": "manual",
        "file_ingest_shape": {"format": "json", "parser_extract_map": {
            "message_id": "id", "sender_type": "role", "content": "text", "event_at": "date"}},
    }


def request():
    return {"source_definition_json": definition(), "sample": {
        "file_name": "notes.json", "content": json.dumps([
            {"id": "n1", "role": "user", "text": "A synthetic note", "date": "2026-01-01T12:00:00Z"}])}}


@pytest.mark.asyncio
async def test_prepare_parses_sample_without_mutating_registries(monkeypatch):
    from topos.sources.registry import REGISTRY
    from topos.ingestion.parsers import PARSER_REGISTRY
    before = (dict(REGISTRY), dict(PARSER_REGISTRY))
    result = await local_create.prepare(request())
    assert result["status"] == "ready"
    assert result["registered"] is False
    assert result["sample"]["records_checked"] == 1
    assert result["sample"]["normalized_preview"][0]["content"] == "A synthetic note"
    assert result["definition_sha256"]
    assert (REGISTRY, PARSER_REGISTRY) == before


@pytest.mark.asyncio
async def test_empty_content_mapping_fails_before_install():
    body = request()
    body["source_definition_json"]["file_ingest_shape"]["parser_extract_map"]["content"] = "missing"
    with pytest.raises(ValueError, match="content"):
        await local_create.prepare(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"source_id": "browser_visits"}, {"visibility": "public"},
    {"source_type": "local_sync", "delivery": "local_sync"}, {"enrichment_triger": "automatic"},
])
async def test_unsupported_or_misspelled_definitions_fail(change):
    body = request()
    body["source_definition_json"].update(change)
    with pytest.raises(ValueError):
        await local_create.prepare(body)


@pytest.mark.asyncio
async def test_digest_binds_sample_and_definition():
    body = request()
    first = await local_create.prepare(body)
    body["sample"]["content"] = body["sample"]["content"].replace("synthetic", "different")
    second = await local_create.prepare(body)
    assert first["plan_sha256"] != second["plan_sha256"]
    assert first["definition_sha256"] == second["definition_sha256"]


def test_infer_nested_sample_and_csv_types():
    sample = local_create.infer_sample("items.json", '{"items":[{"id":1,"title":"One"}]}', record_path="items[*]")
    assert {f["path"] for f in sample["fields"]} == {"id", "title"}
    csv = local_create.infer_sample("items.csv", "id,title\n1,One\n2,Two\n")
    assert csv["records_sampled"] == 2
    assert csv["format"] == "csv"


def test_invalid_jsonl_is_not_silently_skipped():
    with pytest.raises(ValueError):
        local_create.infer_sample("bad.jsonl", '{"id":1}\nnot json\n')


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[{"text": "Missing id"}], [{"id": "same", "text": "One"}, {"id": "same", "text": "Two"}]])
async def test_missing_or_duplicate_ids_fail(rows):
    body = request()
    body["sample"]["content"] = json.dumps(rows)
    with pytest.raises(ValueError, match="unique mapped id"):
        await local_create.prepare(body)


def test_raw_tables_cannot_target_a_system_table():
    payload = definition()
    payload["tables"] = [{"table_id": "engine_config", "columns": []}]
    with pytest.raises(ValueError, match="table names"):
        local_create.validated_definition(payload)
