from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..canonicalization.mappers import MAPPER_REGISTRY
from ..canonicalization.mappers.base import CanonicalMapper as LegacyCanonicalMapper
from ..canonicalization.mappers.base import CanonicalRecord, MappingMetadata
from ..canonicalization.models import CanonicalMessage
from ..ingestion.parsers import PARSER_REGISTRY
from ..ingestion.parsers.base import NormalizedRecord, Parser
from ..ingestion.sources.base import RawRecord
from ..ingestion.validation.base import ValidationResult
from ..sources.definitions import DataSourceDefinition
from ..sources.registry import REGISTRY
from ..storage.canonical.ai_chat.mapper import CanonicalMapper as StorageCanonicalMapper
from ..storage.canonical.ai_chat.mapper import register_mapper
from ..storage.canonical.ai_chat.model import CanonicalAIChatMessage
from ..storage.canonical.ai_chat import mapper as storage_mapper_module

# Snapshot of engine-shipped parser/mapper ids. Registry sources may reference these
# bundled triples without replacing them with generic runtime shims on install.
BUNDLED_PARSER_IDS = frozenset(PARSER_REGISTRY.keys())
BUNDLED_MAPPER_IDS = frozenset(MAPPER_REGISTRY.keys())


def _maybe_parse_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _parse_source_definition_from_version_row(version_row: Dict[str, Any]) -> Dict[str, Any]:
    source_def = _maybe_parse_json(version_row.get("source_definition_json"))
    if not isinstance(source_def, dict):
        raise ValueError("source_definition_json must be a JSON object in the version row")

    compatibility = _maybe_parse_json(version_row.get("compatibility_json"))
    if isinstance(compatibility, dict):
        source_def.setdefault("parser_id", compatibility.get("parser_id"))
        source_def.setdefault("canonical_mapper_id", compatibility.get("canonical_mapper_id"))
        source_def.setdefault("source_type", compatibility.get("source_type"))

    if version_row.get("schema_id") and not source_def.get("schema_id"):
        source_def["schema_id"] = version_row.get("schema_id")
    if version_row.get("source_id") and not source_def.get("source_id"):
        source_def["source_id"] = version_row.get("source_id")
    return source_def


# Enrichment-lane policy fields owned by the bundled registry for bundled
# source ids. A runtime install row snapshots the definition as of install
# time; rehydrated at boot it SHADOWS the current bundled definition, so a
# stale snapshot silently reverts later lane decisions (a 2026-05-29
# browser_visits row with enrichment_trigger='manual' and no jobs turned off
# every browser enrichment lane once the manual gate shipped on 2026-07-06).
# Per-source user toggles belong in source_enrichment_overrides, which the
# lane resolvers apply on top of these values.
_BUNDLED_LANE_POLICY_FIELDS = (
    "enrichment_trigger",
    "raw_enrichment_jobs",
    "canonical_enrichment_jobs",
    "signal_derivation_jobs",
)

# The MAPPING contract of a bundled source is the engine's, not a snapshot's.
# Lane policy (above) answers "which jobs run"; this answers "what does a record
# become" — and a snapshot that predates a declaration silently maps less than
# the shipped engine does. Live case: github_activity's active install was taken
# before canonical_field_map existed, so the engine shipped
# `activity_events.content <- payload.commits[*].message` and the node kept
# mapping without it — a re-map wrote 458 rows and 0 contents, no error anywhere.
# Same failure family as the 2026-05-29 browser_visits snapshot (216ab92): a
# stale install row quietly outranking the build.
_BUNDLED_MAPPING_FIELDS = ("canonical_field_map",)


def _adopt_bundled_lane_policy(payload: Dict[str, Any]) -> Dict[str, Any]:
    from .registry import BUNDLED_REGISTRY

    source_id = str(payload.get("source_id") or "").strip()
    bundled = BUNDLED_REGISTRY.get(source_id)
    if bundled is None:
        return payload
    merged = dict(payload)
    for field_name in (*_BUNDLED_LANE_POLICY_FIELDS, *_BUNDLED_MAPPING_FIELDS):
        value = getattr(bundled, field_name, None)
        if isinstance(value, list):
            merged[field_name] = list(value)
        elif isinstance(value, dict):
            merged[field_name] = dict(value)
        else:
            merged[field_name] = value
    return merged


def _inherit_bundled_posture(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the bundled posture when an install payload does not declare one.

    `install_source_definition` replaces the bundled definition wholesale, and
    `DataSourceDefinition.posture` defaults to `mixed` — so an install payload that simply
    omits the field silently DOWNGRADES a source's posture. Measured on the live node
    2026-08-27: active installs for `browser_visits`, `imessage`, `grow_journal` and
    `github_activity` all carry `posture: null`, so every one of them resolved `mixed` in the
    engine while the bundled registry said `ambient`. Nothing raised; posture simply stopped
    distinguishing anything, and every consumer of it — role gating, the net-subject policy,
    salience — quietly lost its input.

    An omitted field means "unchanged", not "mixed". A payload that DOES declare a posture
    still wins, which is what lets an install deliberately re-posture a source.
    """
    from .registry import BUNDLED_REGISTRY

    if str(payload.get("posture") or "").strip():
        return payload
    source_id = str(payload.get("source_id") or "").strip()
    if not source_id:
        return payload
    bundled = BUNDLED_REGISTRY.get(source_id)
    bundled_posture = str(getattr(bundled, "posture", "") or "").strip()
    if not bundled_posture:
        return payload
    out = dict(payload)
    out["posture"] = bundled_posture
    return out


def _build_source_definition(payload: Dict[str, Any]) -> DataSourceDefinition:
    from .bundled_canonical_triples import apply_bundled_canonical_defaults
    from .definitions import definition_from_payload

    return definition_from_payload(
        _inherit_bundled_posture(
            _adopt_bundled_lane_policy(apply_bundled_canonical_defaults(payload))
        )
    )


def _tokenize(path: str) -> List[str]:
    return [part.strip() for part in str(path).split(".") if part.strip()]


def _walk_step(nodes: List[Any], token: str) -> List[Any]:
    results: List[Any] = []
    if token == "*":
        for node in nodes:
            if isinstance(node, dict):
                results.extend(node.values())
            elif isinstance(node, list):
                results.extend(node)
        return results

    list_mode = token.endswith("[*]")
    key = token[:-3] if list_mode else token
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if key not in node:
            continue
        value = node.get(key)
        if list_mode:
            if isinstance(value, list):
                results.extend(value)
            elif value is not None:
                results.append(value)
        else:
            results.append(value)
    return results


def _extract_path(payload: Dict[str, Any], path: str) -> Any:
    if not path:
        return None
    nodes: List[Any] = [payload]
    for token in _tokenize(path):
        nodes = _walk_step(nodes, token)
        if not nodes:
            return None
    if len(nodes) == 1:
        return nodes[0]
    return nodes


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_coerce_text(item) for item in value]
        return "\n".join([part for part in parts if part])
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True)
    return str(value)


def _normalize_ts(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return _coerce_text(value)


def _derive_parser_extract_map_for_direct_table_passthrough(
    source_def_payload: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    file_ingest_shape = source_def_payload.get("file_ingest_shape")
    parser_extract_map: Dict[str, Any] = {}
    if isinstance(file_ingest_shape, dict):
        maybe_map = file_ingest_shape.get("parser_extract_map")
        if isinstance(maybe_map, dict):
            parser_extract_map = dict(maybe_map)
    if parser_extract_map:
        return parser_extract_map, False

    source_type = str(source_def_payload.get("source_type") or "").strip()
    include_data_table = bool(source_def_payload.get("pipeline_include_data_table"))
    if source_type != "file" or not include_data_table:
        return parser_extract_map, False

    tables = source_def_payload.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError(
            "pipeline_include_data_table=true requires tables when parser_extract_map is empty"
        )

    column_names: List[str] = []
    seen: set[str] = set()
    for table in tables:
        if not isinstance(table, dict):
            continue
        columns = table.get("columns")
        if not isinstance(columns, list):
            continue
        for column in columns:
            if not isinstance(column, dict):
                continue
            col_name = str(column.get("name") or "").strip()
            if not col_name or col_name in seen:
                continue
            seen.add(col_name)
            column_names.append(col_name)

    if not column_names:
        raise ValueError(
            "pipeline_include_data_table=true with empty parser_extract_map requires at least one valid table column name"
        )

    derived_map = {col_name: col_name for col_name in column_names}
    # Common case: table uses record_id but source rows provide id.
    if "record_id" in derived_map and "id" in seen:
        derived_map["record_id"] = "id"
    return derived_map, True


def _build_dynamic_parser_class(
    *,
    source_def: DataSourceDefinition,
    parser_extract_map: Dict[str, Any],
    parser_id: str,
    requires_canonical_contract: bool,
    direct_table_passthrough: bool,
):
    class RuntimeInstalledParser(Parser):
        def __init__(self, dataset_id: str, _schema_id: str = parser_id):
            self.dataset_id = dataset_id
            self._schema_id = _schema_id

        def parse(self, raw: RawRecord) -> NormalizedRecord:
            payload = raw.payload
            extracted: Dict[str, Any] = {}
            for target_key, path in parser_extract_map.items():
                if isinstance(path, str):
                    extracted[target_key] = _extract_path(payload, path)

            if not requires_canonical_contract:
                if (
                    direct_table_passthrough
                    and extracted
                    and isinstance(payload, dict)
                    and payload
                    and all(value is None for value in extracted.values())
                ):
                    keys_preview = sorted([str(key) for key in payload.keys()])[:10]
                    raise ValueError(
                        "Direct table passthrough could not map any table columns from payload keys "
                        f"{keys_preview}. Define file_ingest_shape.parser_extract_map explicitly."
                    )
                if direct_table_passthrough and extracted.get("record_id") is None:
                    extracted["record_id"] = raw.record_id
                record_id = _coerce_text(
                    extracted.get("record_id")
                    or extracted.get("id")
                    or raw.record_id
                )
                return NormalizedRecord(record_id=record_id, payload=extracted)

            message_id = _coerce_text(extracted.get("message_id") or payload.get("id") or raw.record_id)
            conversation_id = _coerce_text(
                extracted.get("conversation_id")
                or extracted.get("thread_id")
                or payload.get("thread_id")
                or payload.get("conversation_id")
                or ""
            )
            sender_hint = _coerce_text(extracted.get("sender_type") or extracted.get("role") or payload.get("role")).lower()
            sender_type = "human" if sender_hint in {"user", "human"} else (sender_hint or "assistant")
            content = _coerce_text(extracted.get("content") or extracted.get("content_rendered") or payload.get("content"))
            ts = _normalize_ts(extracted.get("event_at") or extracted.get("created_at") or payload.get("created_at") or payload.get("ts"))

            normalized_payload: Dict[str, Any] = {
                "message_id": message_id,
                "dataset_id": self.dataset_id,
                "thread_id": conversation_id,
                "ts": ts,
                "sender_type": sender_type,
                "content": content,
            }
            metadata = extracted.get("metadata_json") or payload.get("metadata")
            if metadata is not None:
                normalized_payload["_metadata"] = metadata if isinstance(metadata, dict) else {"metadata": _coerce_text(metadata)}
            return NormalizedRecord(record_id=message_id, payload=normalized_payload)

        def validate(self, record: RawRecord) -> ValidationResult:
            if not isinstance(record.payload, dict):
                return ValidationResult(is_valid=False, errors=["Payload must be an object"], metadata={})
            return ValidationResult(
                is_valid=True,
                errors=[],
                metadata={"dynamic_parser_id": parser_id, "source_id": source_def.source_id},
            )

        def schema_id(self) -> str:
            return self._schema_id

    RuntimeInstalledParser.__name__ = f"RuntimeInstalledParser_{source_def.source_id.replace('-', '_')}"
    return RuntimeInstalledParser


def _build_dynamic_legacy_mapper_class(source_def: DataSourceDefinition, mapper_id: str):
    class RuntimeInstalledLegacyMapper(LegacyCanonicalMapper):
        version: str = "dynamic.v1"

        def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
            payload = normalized.payload
            content = _coerce_text(payload.get("content"))
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            message_id = _coerce_text(payload.get("message_id") or normalized.record_id)
            conversation_id = _coerce_text(payload.get("thread_id") or payload.get("conversation_id") or payload.get("dataset_id"))
            if conversation_id and ":" not in conversation_id:
                conversation_id = f"{source_def.source_id}:{conversation_id}"
            metadata: Dict[str, Any] = {"mapper_version": self.version, "mapper_id": mapper_id}
            if "_metadata" in payload:
                metadata["_metadata"] = payload.get("_metadata")
            canonical = CanonicalMessage(
                message_id=message_id,
                conversation_id=conversation_id,
                sender_type=_coerce_text(payload.get("sender_type")),
                content=content,
                ts=_coerce_text(payload.get("ts")) or None,
                source_id=source_def.source_id,
                content_hash=content_hash,
                metadata=metadata,
            )
            return CanonicalRecord(record_id=canonical.message_id, payload=canonical.__dict__)

        def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
            return MappingMetadata(source_id=source_def.source_id, mapping_version=self.version)

    RuntimeInstalledLegacyMapper.__name__ = f"RuntimeInstalledLegacyMapper_{source_def.source_id.replace('-', '_')}"
    return RuntimeInstalledLegacyMapper


def _build_dynamic_storage_mapper_class(source_def: DataSourceDefinition, mapper_id: str):
    class RuntimeInstalledStorageMapper(StorageCanonicalMapper):
        def map_to_canonical(self, staging_record: Dict[str, Any], source: str) -> List[CanonicalAIChatMessage]:
            conversation_id = self.extract_conversation_id(staging_record)
            actual_source_id = _coerce_text(staging_record.get("source_id") or source_def.source_id)
            message = CanonicalAIChatMessage(
                message_id=_coerce_text(staging_record.get("message_id")),
                conversation_id=conversation_id,
                sender_type=_coerce_text(staging_record.get("sender_type") or "assistant"),
                sender_id=_coerce_text(staging_record.get("sender_id")) or None,
                ts=_coerce_text(staging_record.get("ts")),
                content=_coerce_text(staging_record.get("content")),
                source_id=actual_source_id,
                content_rendered=_coerce_text(staging_record.get("content_rendered")) or None,
                metadata_json={
                    "original_source": source,
                    "mapper_id": mapper_id,
                    "thread_id": staging_record.get("thread_id"),
                    "_metadata": staging_record.get("_metadata"),
                },
            )
            return [message]

        def extract_conversation_id(self, staging_record: Dict[str, Any]) -> str:
            conv = _coerce_text(staging_record.get("thread_id") or staging_record.get("conversation_id") or "")
            if conv and ":" not in conv:
                return f"{source_def.source_id}:{conv}"
            return conv or f"{source_def.source_id}:{_coerce_text(staging_record.get('dataset_id'))}"

    RuntimeInstalledStorageMapper.__name__ = f"RuntimeInstalledStorageMapper_{source_def.source_id.replace('-', '_')}"
    return RuntimeInstalledStorageMapper


@dataclass
class RuntimeInstallHandle:
    source_id: str
    parser_ids: List[str]
    canonical_mapper_id: Optional[str]
    _previous_source: Optional[DataSourceDefinition]
    _previous_parsers: Dict[str, Any]
    _previous_legacy_mapper: Optional[Any]
    _previous_storage_mapper: Optional[Any]

    def uninstall(self) -> None:
        if self._previous_source is None:
            REGISTRY.pop(self.source_id, None)
        else:
            REGISTRY[self.source_id] = self._previous_source

        for parser_id in self.parser_ids:
            previous = self._previous_parsers.get(parser_id)
            if previous is None:
                PARSER_REGISTRY.pop(parser_id, None)
            else:
                PARSER_REGISTRY[parser_id] = previous

        if self.canonical_mapper_id:
            mapper_id = self.canonical_mapper_id
            if self._previous_legacy_mapper is None:
                MAPPER_REGISTRY.pop(mapper_id, None)
            else:
                MAPPER_REGISTRY[mapper_id] = self._previous_legacy_mapper

            if self._previous_storage_mapper is None:
                storage_mapper_module._MAPPER_REGISTRY.pop(mapper_id, None)
            else:
                storage_mapper_module._MAPPER_REGISTRY[mapper_id] = self._previous_storage_mapper


def install_source_definition(source_def_payload: Dict[str, Any]) -> RuntimeInstallHandle:
    source_def = _build_source_definition(source_def_payload)
    source_id = source_def.source_id

    previous_source = REGISTRY.get(source_id)
    REGISTRY[source_id] = source_def

    canonical_mapper_id = str(source_def_payload.get("canonical_mapper_id") or "").strip()
    canonical_group_id = str(source_def_payload.get("canonical_group_id") or "").strip()
    from .bundled_canonical_triples import requires_canonical_contract as _requires_canonical

    requires_canonical_contract = _requires_canonical(source_def_payload)
    file_ingest_shape = source_def_payload.get("file_ingest_shape")
    explicit_parser_extract_map: Dict[str, Any] = {}
    if isinstance(file_ingest_shape, dict):
        maybe_map = file_ingest_shape.get("parser_extract_map")
        if isinstance(maybe_map, dict):
            explicit_parser_extract_map = dict(maybe_map)
    parser_extract_map, direct_table_passthrough = _derive_parser_extract_map_for_direct_table_passthrough(
        source_def_payload
    )
    # Table-column passthrough maps are derived automatically for pipeline_include_data_table
    # sources. Only an explicit file_ingest_shape.parser_extract_map should override bundled parsers.
    has_custom_extract_map = bool(explicit_parser_extract_map)

    parser_ids = sorted(
        {
            item
            for item in [str(source_def.schema_id or "").strip(), str(source_def.parser_id or "").strip()]
            if item
        }
    )
    use_bundled_parser = (
        bool(parser_ids)
        and all(parser_id in BUNDLED_PARSER_IDS for parser_id in parser_ids)
        and not has_custom_extract_map
    )

    mapper_id = str(source_def.canonical_mapper_id or "").strip() or None
    use_bundled_mapper = bool(
        mapper_id and mapper_id in BUNDLED_MAPPER_IDS and not has_custom_extract_map
    )

    previous_parsers: Dict[str, Any] = {}
    installed_parser_ids: List[str] = []
    if not use_bundled_parser:
        parser_cls = _build_dynamic_parser_class(
            source_def=source_def,
            parser_extract_map=parser_extract_map,
            parser_id=str(source_def.schema_id or source_def.parser_id),
            requires_canonical_contract=requires_canonical_contract,
            direct_table_passthrough=direct_table_passthrough,
        )
        for parser_id in parser_ids:
            if parser_id in PARSER_REGISTRY:
                # Reuse an existing shared parser; never clobber another source's registration.
                continue
            previous_parsers[parser_id] = PARSER_REGISTRY.get(parser_id)
            PARSER_REGISTRY[parser_id] = parser_cls
            installed_parser_ids.append(parser_id)

    previous_legacy_mapper = None
    previous_storage_mapper = None
    installed_mapper_id: Optional[str] = None
    if mapper_id and not use_bundled_mapper:
        if mapper_id in MAPPER_REGISTRY:
            # Reuse an existing shared mapper; never clobber another source's registration.
            pass
        else:
            installed_mapper_id = mapper_id
            previous_legacy_mapper = MAPPER_REGISTRY.get(mapper_id)
            legacy_mapper_cls = _build_dynamic_legacy_mapper_class(source_def, mapper_id)
            MAPPER_REGISTRY[mapper_id] = legacy_mapper_cls

            previous_storage_mapper = storage_mapper_module._MAPPER_REGISTRY.get(mapper_id)
            storage_mapper_cls = _build_dynamic_storage_mapper_class(source_def, mapper_id)
            register_mapper(mapper_id, storage_mapper_cls)

    return RuntimeInstallHandle(
        source_id=source_id,
        parser_ids=installed_parser_ids,
        canonical_mapper_id=installed_mapper_id,
        _previous_source=previous_source,
        _previous_parsers=previous_parsers,
        _previous_legacy_mapper=previous_legacy_mapper,
        _previous_storage_mapper=previous_storage_mapper,
    )


def install_source_from_version_row(version_row: Dict[str, Any]) -> Tuple[RuntimeInstallHandle, Dict[str, Any]]:
    source_def_payload = _parse_source_definition_from_version_row(version_row)
    handle = install_source_definition(source_def_payload)
    return handle, source_def_payload
