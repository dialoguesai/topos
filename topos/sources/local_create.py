"""Prepare private source definitions without registry writes or runtime registration.

The owner socket uses this before installation; the CLI also uses the bounded,
stdlib-only sample inference. Samples are data, never Python or executable adapters.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import asdict, fields
from typing import Any

MAX_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 200
ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _bounded(content: str) -> None:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Provide non-empty UTF-8 sample content")
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Sample exceeds 2 MiB; use a representative slice")


def sample_format(file_name: str) -> str:
    suffix = file_name.rsplit(".", 1)[-1].lower()
    if suffix not in {"json", "jsonl", "ndjson", "csv"}:
        raise ValueError("Use a .json, .jsonl, .ndjson or .csv sample")
    return "jsonl" if suffix == "ndjson" else suffix


def _sample_rows(file_name: str, content: str, record_path: str = "") -> tuple[str, list[dict]]:
    _bounded(content)
    fmt = sample_format(file_name)
    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV needs unique column names")
        rows = list(reader)
        if any(None in row for row in rows):
            raise ValueError("CSV row has more fields than its header")
    elif fmt == "jsonl":
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    else:
        value = json.loads(content)
        rows = value if isinstance(value, list) else [value]
    if record_path:
        # The runtime uses dotted paths and [*], relative to each root record.
        from .runtime_install import _extract_path
        expanded = []
        for row in rows:
            value = _extract_path(row, record_path)
            expanded.extend(value if isinstance(value, list) else [value])
        rows = expanded
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Sample must contain objects; select a nested array with --record-path items[*]")
    return fmt, rows


def infer_sample(file_name: str, content: str, *, record_path: str = "") -> dict:
    fmt, all_rows = _sample_rows(file_name, content, record_path)
    rows = all_rows[:MAX_RECORDS]
    columns: dict[str, dict] = {}

    def walk(obj: dict, prefix: str, seen: set[str]) -> None:
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            kind = ("null" if value is None else "boolean" if isinstance(value, bool) else
                    "number" if isinstance(value, (int, float)) else "object" if isinstance(value, dict) else
                    "array" if isinstance(value, list) else "string")
            col = columns.setdefault(path, {"types": set(), "present": 0})
            col["types"].add(kind)
            if path not in seen:
                col["present"] += 1
                seen.add(path)
            if isinstance(value, dict):
                walk(value, path, seen)
    for row in rows:
        walk(row, "", set())
    return {"format": fmt, "record_path": record_path, "records_total": len(all_rows),
            "records_sampled": len(rows), "truncated": len(all_rows) > len(rows),
            "fields": [{"path": key, "types": sorted(col["types"]),
                        "nullable": "null" in col["types"] or col["present"] < len(rows)}
                       for key, col in sorted(columns.items())], "sample_rows": rows[:3]}


def validated_definition(payload: dict) -> tuple[dict, Any]:
    from .definitions import DataSourceDefinition
    from .registry import BUNDLED_REGISTRY, REGISTRY
    from .install_service import _normalize_enrichment_bindings, _validate_source_contract
    from .runtime_install import _build_source_definition, BUNDLED_PARSER_IDS, BUNDLED_MAPPER_IDS

    if not isinstance(payload, dict):
        raise ValueError("source_definition_json must be an object")
    unknown = set(payload) - {f.name for f in fields(DataSourceDefinition)}
    if unknown:
        raise ValueError("Unknown definition fields: " + ", ".join(sorted(unknown)))
    if len(json.dumps(payload).encode()) > MAX_BYTES:
        raise ValueError("Definition exceeds 2 MiB")
    if not isinstance(payload.get("display_name"), str) or not payload["display_name"].strip():
        raise ValueError("display_name must be a non-empty string")
    if payload.get("file_ingest_shape") is not None and not isinstance(payload["file_ingest_shape"], dict):
        raise ValueError("file_ingest_shape must be an object")
    if payload.get("tables") is not None and (not isinstance(payload["tables"], list)
            or any(not isinstance(t, dict) for t in payload["tables"])):
        raise ValueError("tables must be a list of table objects")
    sid = payload.get("source_id")
    if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
        raise ValueError("source_id must be a lowercase identifier of at most 80 characters")
    if sid in BUNDLED_REGISTRY:
        raise ValueError("Use a new source_id; this command does not replace bundled sources")
    if payload.get("source_type") not in {"file", "ui_stream"}:
        raise ValueError("Private definitions support file or ui_stream; a new sync adapter needs code")
    expected_delivery = "owner_upload" if payload["source_type"] == "file" else "client_push"
    if payload.get("delivery", expected_delivery) != expected_delivery:
        raise ValueError("delivery must match file/owner_upload or ui_stream/client_push")
    normalized = _validate_source_contract(_normalize_enrichment_bindings(payload))
    definition = _build_source_definition(normalized)
    if not definition.display_name.strip():
        raise ValueError("display_name is required")
    if definition.ingestion_trigger not in {"automatic", "manual"} or definition.enrichment_trigger not in {"automatic", "manual"}:
        raise ValueError("Triggers must be automatic or manual")
    # Runtime registries share parser and mapper ids. A custom map must not silently reuse another source's code.
    shape = definition.file_ingest_shape or {}
    if (definition.parser_id not in BUNDLED_PARSER_IDS and definition.canonical_group_id
            and definition.canonical_group_id not in {"conversations", "ai_messages"}):
        raise ValueError("Custom JSON parsers currently map chat lanes or raw tables. For another lane, use a compatible bundled parser or add an adapter.")
    # A raw data table must belong to this source; never let a new definition write into system tables.
    table_ids = set()
    prefix = "private_" + re.sub(r"[.-]", "_", sid) + "_"
    for table in definition.tables or []:
        table_id = table.get("table_id", "")
        if not isinstance(table_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", table_id) or not table_id.startswith(prefix):
            raise ValueError(f"Private data table names must start with {prefix} and use lowercase letters, digits or underscores")
        if table_id in table_ids:
            raise ValueError("Duplicate data table name")
        table_ids.add(table_id)
    if shape.get("parser_extract_map") and ({definition.schema_id, definition.parser_id} & BUNDLED_PARSER_IDS):
        raise ValueError("A custom parser_extract_map needs its own schema_id and parser_id")
    for other_id, other in REGISTRY.items():
        if other_id == sid:
            continue
        if table_ids & {t.get("table_id") for t in other.tables or []}:
            raise ValueError("Data table already belongs to another source")
        if ({definition.schema_id, definition.parser_id} & {other.schema_id, other.parser_id}) - BUNDLED_PARSER_IDS:
            raise ValueError("Custom schema_id/parser_id already belongs to another source")
        if (definition.canonical_mapper_id and definition.canonical_mapper_id == other.canonical_mapper_id
                and definition.canonical_mapper_id not in BUNDLED_MAPPER_IDS):
            raise ValueError("Custom canonical_mapper_id already belongs to another source")
    # Use the same defaults on preparation, install, readback and repeat calls.
    return asdict(definition), definition


async def prepare(body: dict) -> dict:
    from .runtime_install import (_build_dynamic_parser_class, _derive_parser_extract_map_for_direct_table_passthrough,
                                  BUNDLED_PARSER_IDS, PARSER_REGISTRY)
    from .bundled_canonical_triples import requires_canonical_contract
    from ..ingestion.parser import parse_file
    from ..ingestion.manager import _expand_file_records
    from ..ingestion.sources.base import RawRecord

    if not isinstance(body, dict) or set(body) - {"source_definition_json", "sample", "plan_sha256"}:
        raise ValueError("Provide source_definition_json and an optional sample; target identity is resolved locally")
    normalized, definition = validated_definition(body.get("source_definition_json"))
    sample = body.get("sample")
    warnings = []
    if not definition.canonical_group_id:
        warnings.append("Raw-only storage: no canonical search or facts. Map a lane for use in chat.")
    if definition.enrichment_trigger == "manual":
        warnings.append("Automatic enrichment is off; derived search and facts may require manual processing.")
    warnings.append("Unregistered definition. Imported data follows this Topos's existing access and sharing rules.")
    receipt = {"status": "ready", "registered": False, "source_id": definition.source_id,
               "definition_sha256": digest(normalized), "source_definition_json": normalized,
               "warnings": warnings, "sample": None}
    if sample is not None:
        if not isinstance(sample, dict) or set(sample) != {"file_name", "content"}:
            raise ValueError("sample needs file_name and content only")
        fmt, _ = _sample_rows(sample["file_name"], sample["content"])
        declared_format = (definition.file_ingest_shape or {}).get("format")
        if declared_format and declared_format != fmt:
            raise ValueError("Sample extension and file_ingest_shape.format disagree")
        # A stream sample is a file of representative records, not an automatic producer grant.
        extract, passthrough = _derive_parser_extract_map_for_direct_table_passthrough(normalized)
        if definition.parser_id in BUNDLED_PARSER_IDS:
            cls = PARSER_REGISTRY[definition.parser_id]
        else:
            cls = _build_dynamic_parser_class(source_def=definition, parser_extract_map=extract,
                    parser_id=definition.parser_id, requires_canonical_contract=requires_canonical_contract(normalized),
                    direct_table_passthrough=passthrough)
        parser = cls(dataset_id="local-source-preview")

        async def stream():
            yield sample["content"].encode("utf-8")
        checked = 0
        seen_ids = set()
        preview = []
        truncated = False
        async for raw in parse_file(stream(), fmt):
            for row in _expand_file_records(raw, definition):
                if checked >= MAX_RECORDS:
                    truncated = True
                    break
                # A stable input id is required: row-number fallbacks collide across later imports.
                record = RawRecord(record_id=str(row.get("id") or ""), payload=row)
                validation = parser.validate(record)
                if not validation.is_valid:
                    raise ValueError(f"Sample record {checked + 1} failed parser validation")
                parsed = parser.parse(record)
                if not parsed.record_id or parsed.record_id in seen_ids:
                    raise ValueError(f"Sample record {checked + 1} needs a non-empty, unique mapped id")
                seen_ids.add(parsed.record_id)
                if definition.canonical_group_id in {"conversations", "ai_messages"} and not str(parsed.payload.get("content") or "").strip():
                    raise ValueError(f"Sample record {checked + 1} has no mapped content")
                if len(preview) < 3:
                    preview.append(parsed.payload)
                checked += 1
            if truncated:
                break
        if not checked:
            raise ValueError("Sample produced no records; check raw_record_path")
        receipt["sample"] = {"file_name": sample["file_name"], "bytes": len(sample["content"].encode()),
                             "records_checked": checked, "truncated": truncated, "normalized_preview": preview,
                             "validation": "parser only; canonical storage and retrieval are verified after import"}
    receipt["plan_sha256"] = digest({"source_definition_json": normalized, "sample": sample})
    return receipt
