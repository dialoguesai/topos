"""Declarative canonical field mapping (PLAN_CONNECTOR_CATALOG_ROLLOUT §5a,
capabilities 2–3): a source DECLARES which piece of its records lands in which
canonical column, and where extra rows fan out from.

Why this exists: everything else about adding a source is already self-serve
(definition, ingest chain, raw retention, enrichment, scopes) — the last
bespoke step was the canonical mapper. Without a declaration a runtime-installed
source only gets the *message*-shaped dynamic mapper, so anything landing on the
activity/journal lanes needed Python in this repo. A field map closes that: the
registerer states ``{table: {column: rule}}`` and the engine does the mapping.

Declaration shape (JSON-serializable, lives on ``DataSourceDefinition
.canonical_field_map``)::

    {
      "activity_events": {
        "content": {"path": "payload.commits[*].message", "join": "\\n\\n"}
      },
      "journal_entries": {
        "fan_out": "payload.commits[*]",
        "where": {"path": "authorship", "in": ["authored"]},
        "fields": {
          "entry_id": {"template": "github:{repo.name}:{sha}", "scope": "item"},
          "content": {"path": "message", "scope": "item"},
          "category": {"const": "code"}
        }
      }
    }

Rule vocabulary (a bare string is shorthand for ``{"path": …}``):

  ``path``       dotted path into the normalized record; a ``[*]`` segment
                 expands a list (``payload.commits[*].message`` → every message)
  ``first_of``   list of paths; first non-empty one wins
  ``template``   ``"{a.b}: {c}"`` — dotted paths substituted, missing → ""
  ``const``      literal value
  ``map``        value → value lookup applied after extraction
  ``default``    used when nothing resolved (or the ``map`` missed)
  ``transform``  named transform from TRANSFORMS (closed catalog)
  ``join``       separator for multi-valued paths (default ``"\\n\\n"``)
  ``when``       ``{"path": …, "equals"/"in": …}`` — rule applies only if true
  ``scope``      ``"record"`` (default) or ``"item"`` — inside a ``fan_out``
                 block, whether the rule reads the fanned-out item or the whole
                 record

Semantics against a code mapper (both may be present):

  * a declared table the code mapper already emitted → declared columns are
    OVERLAID onto those rows (a non-empty declared value wins: the declaration
    is the registerer's explicit statement of where a field comes from);
  * a declared table the code mapper did not emit → rows are MINTED from the
    declaration (one, or one per ``fan_out`` item that passes ``where``);
  * rows on tables that were never declared pass through untouched.

Values are TEXT: this maps human-readable content into canonical text columns.
JSON columns (``metadata_json``) are deliberately not declarable — a declared
string would corrupt the column's contract for every reader.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from ..ingestion.parsers.base import NormalizedRecord

# Vocabulary + validation live with the source-definition schema so the
# control-plane bundled mirror can validate a declaration without importing this
# module's engine dependencies (see declared_field_map_spec's docstring).
from ..sources.declared_field_map_spec import (  # noqa: F401 — re-exported for callers
    ID_COLUMNS,
    RESERVED_COLUMNS,
    TRANSFORM_IDS,
    table_block as _table_block,
    validate_canonical_field_map,
)
from .mappers.base import CanonicalMapper, CanonicalRecord, MappingMetadata

DEFAULT_JOIN = "\n\n"


def _strip_event_suffix(value: str) -> str:
    """'PushEvent' → 'push' (GitHub-style type names, generically useful)."""
    stripped = value[: -len("Event")] if value.endswith("Event") else value
    return stripped.lower()


def _org_prefix(value: str) -> str:
    """'dialoguesai/topos' → 'dialoguesai' (owner half of an owner/name pair)."""
    parts = value.split("/")
    return parts[0].strip() if len(parts) >= 2 and parts[0].strip() else ""


def _basename(value: str) -> str:
    """'dialoguesai/topos' → 'topos'; also the last path segment of a URL path."""
    return value.rstrip("/").split("/")[-1].strip()


def _first_line(value: str) -> str:
    """Commit-subject style: the first non-empty line."""
    for line in value.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _hostname(value: str) -> str:
    try:
        return str(urlparse(value).hostname or "")
    except ValueError:
        return ""


TRANSFORMS = {
    "lower": lambda v: v.lower(),
    "upper": lambda v: v.upper(),
    "strip": lambda v: v.strip(),
    "first_line": _first_line,
    "org_prefix": _org_prefix,
    "basename": _basename,
    "strip_event_suffix": _strip_event_suffix,
    "hostname": _hostname,
}
# The declared vocabulary and its implementations are one contract: a name that
# validates but does not run would silently map nothing.
assert set(TRANSFORMS) == set(TRANSFORM_IDS), "TRANSFORMS drifted from TRANSFORM_IDS"


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _descend(value: Any, part: str) -> Any:
    """One path segment. JSON-string values are parsed so metadata_json-shaped
    columns resolve the same way in a reload batch as in a fresh mapping."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict):
        return None
    return value.get(part)


def resolve_path(record: Any, path: str) -> List[Any]:
    """Every value a dotted path resolves to. ``[*]`` expands a list segment;
    without one the result is 0 or 1 values."""
    parts = [part for part in str(path or "").split(".") if part]
    if not parts:
        return []
    current: List[Any] = [record]
    for part in parts:
        expand = part.endswith("[*]")
        key = part[:-3] if expand else part
        nxt: List[Any] = []
        for item in current:
            value = _descend(item, key)
            if value is None:
                continue
            if expand:
                if isinstance(value, list):
                    nxt.extend(v for v in value if v is not None)
            else:
                nxt.append(value)
        current = nxt
        if not current:
            return []
    return current


def _resolve_texts(record: Any, path: str) -> List[str]:
    return [text for text in (_as_text(v) for v in resolve_path(record, path)) if text]


def _condition_holds(record: Any, condition: Any) -> bool:
    """``{"path": …, "equals": …}`` / ``{"path": …, "in": […]}``. A field that is
    ABSENT passes by default (``when_missing: "skip"`` inverts it): the gate this
    models — github's ``authorship`` — has to let legacy payloads that predate
    the stamped field through, exactly as the code mapper's gate does."""
    if not isinstance(condition, dict):
        return True
    path = str(condition.get("path") or condition.get("field") or "").strip()
    if not path:
        return True
    values = _resolve_texts(record, path)
    if not values:
        # Legacy payloads that predate a stamped field must not be silently
        # dropped: the declaration says which way that falls.
        return str(condition.get("when_missing") or "allow").strip().lower() == "allow"
    if "equals" in condition:
        return any(value == _as_text(condition.get("equals")) for value in values)
    if "in" in condition:
        allowed = {_as_text(v) for v in (condition.get("in") or [])}
        return any(value in allowed for value in values)
    return True


def evaluate_rule(rule: Any, record: Any, *, item: Any = None) -> str:
    """Resolve one column rule to text ("" when it yields nothing)."""
    if not isinstance(rule, dict):
        rule = {"path": str(rule or "")}
    scope_item = str(rule.get("scope") or "record").strip().lower() == "item"
    subject = item if (scope_item and item is not None) else record
    if "when" in rule and not _condition_holds(subject, rule.get("when")):
        return ""

    values: List[str] = []
    if "const" in rule:
        values = [_as_text(rule.get("const"))]
    elif rule.get("template"):
        values = [_render_template(str(rule["template"]), subject)]
    elif rule.get("first_of"):
        for path in rule.get("first_of") or []:
            found = _resolve_texts(subject, str(path))
            if found:
                values = found
                break
    elif rule.get("path"):
        values = _resolve_texts(subject, str(rule["path"]))

    transform = str(rule.get("transform") or "").strip()
    if transform:
        fn = TRANSFORMS.get(transform)
        if fn is None:
            return ""  # unknown transform: validation rejects these at definition time
        values = [text for text in (fn(v) for v in values) if text]

    mapping = rule.get("map")
    if isinstance(mapping, dict):
        values = [
            _as_text(mapping.get(v, rule.get("default", "") if "default" in rule else v))
            for v in values
        ]
        values = [v for v in values if v]

    text = str(rule.get("join", DEFAULT_JOIN)).join(v for v in values if v)
    if not text and "default" in rule:
        return _as_text(rule.get("default"))
    return text


def _render_template(template: str, record: Any) -> str:
    """``"{repo.name}: {payload.size} commits"`` — unresolved paths render ''."""
    out: List[str] = []
    rest = template
    while True:
        start = rest.find("{")
        if start < 0:
            out.append(rest)
            break
        end = rest.find("}", start)
        if end < 0:
            out.append(rest)
            break
        out.append(rest[:start])
        path = rest[start + 1 : end].strip()
        found = _resolve_texts(record, path)
        out.append(found[0] if found else "")
        rest = rest[end + 1 :]
    return "".join(out).strip()


def apply_field_map(fields: Dict[str, Any], record: Any, *, item: Any = None) -> Dict[str, str]:
    """Declared columns → resolved text (empty results are dropped, so an
    overlay never blanks a value the code mapper computed)."""
    out: Dict[str, str] = {}
    for column, rule in fields.items():
        text = evaluate_rule(rule, record, item=item)
        if text:
            out[str(column)] = text
    return out


@dataclass
class DeclaredFieldMapper(CanonicalMapper):
    """A code mapper (optional) plus the source's declared field map.

    With a base mapper the declaration overlays/extends its output; without one
    the declaration IS the mapping — which is what makes a non-message lane
    reachable for a runtime-installed source with no Python in this repo.
    """

    source_id: str
    field_map: Dict[str, Any]
    default_table: str = "activity_events"
    base: Optional[CanonicalMapper] = None
    version: str = "declared.v1"
    _blocks: Dict[str, Dict[str, Any]] = dataclass_field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._blocks = {
            str(table): _table_block(declaration)
            for table, declaration in (self.field_map or {}).items()
        }

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        records = self.map_many(normalized)
        if not records:
            raise ValueError(
                f"source_id={self.source_id}: canonical_field_map produced no row for "
                f"record_id={normalized.record_id}"
            )
        return records[0]

    def map_many(self, normalized: NormalizedRecord) -> List[CanonicalRecord]:
        records: List[CanonicalRecord] = list(self.base.map_many(normalized)) if self.base else []
        record = normalized.payload
        out: List[CanonicalRecord] = []
        overlaid: set = set()

        for mapped in records:
            table = mapped.table or self.default_table
            block = self._blocks.get(table)
            if not block or block.get("fan_out"):
                # A fan-out declaration describes ADDITIONAL rows, never an
                # overlay — the base row passes through and the declared rows
                # are still minted below. (Suppressing them because the code
                # mapper also writes this table would make the declaration a
                # silent no-op, the failure mode this capability removes.)
                out.append(mapped)
                continue
            overlaid.add(table)
            declared = apply_field_map(block["fields"], record)
            out.append(
                CanonicalRecord(
                    record_id=mapped.record_id,
                    payload={**mapped.payload, **declared},
                    table=mapped.table,
                )
            )

        for table, block in self._blocks.items():
            if table in overlaid:
                continue
            out.extend(self._mint(table, block, record))
        return out

    def _mint(self, table: str, block: Dict[str, Any], record: Any) -> List[CanonicalRecord]:
        id_column = ID_COLUMNS.get(table)
        fan_out = str(block.get("fan_out") or "").strip()
        items: Sequence[Any] = resolve_path(record, fan_out) if fan_out else [None]
        where = block.get("where")
        out: List[CanonicalRecord] = []
        for item in items:
            if item is not None and where is not None and not _condition_holds(item, where):
                continue
            payload = apply_field_map(block["fields"], record, item=item)
            record_id = payload.get(id_column or "", "")
            if not record_id:
                continue  # no deterministic identity → nothing safe to upsert
            out.append(
                CanonicalRecord(
                    record_id=record_id,
                    payload=payload,
                    table=None if table == self.default_table else table,
                )
            )
        return out

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        if self.base is not None:
            base_meta = self.base.mapping_metadata(normalized)
            return MappingMetadata(
                source_id=base_meta.source_id,
                mapping_version=f"{base_meta.mapping_version}+{self.version}",
            )
        return MappingMetadata(source_id=self.source_id, mapping_version=self.version)


def build_canonical_mapper(
    source_def: Any,
    *,
    default_table: str,
    fallback_mapper_id: Optional[str] = None,
) -> Optional[CanonicalMapper]:
    """The mapper for a source: its registered code mapper, its declared field
    map, or both. ``fallback_mapper_id`` preserves each lane's historical
    default for sources that declare neither."""
    from .mappers import MAPPER_REGISTRY

    field_map = getattr(source_def, "canonical_field_map", None)
    mapper_id = str(getattr(source_def, "canonical_mapper_id", "") or "").strip()
    base_cls = MAPPER_REGISTRY.get(mapper_id) if mapper_id else None
    if base_cls is None and not field_map and fallback_mapper_id:
        base_cls = MAPPER_REGISTRY.get(fallback_mapper_id)
    base = base_cls() if base_cls else None
    if not isinstance(field_map, dict) or not field_map:
        return base
    return DeclaredFieldMapper(
        source_id=str(getattr(source_def, "source_id", "") or ""),
        field_map=field_map,
        default_table=default_table,
        base=base,
    )
