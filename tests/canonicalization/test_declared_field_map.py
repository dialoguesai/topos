"""Declarative canonical field mapping (§5a capabilities 2–3).

Pins the rule vocabulary a source definition may use, and the two ways a
declaration composes with a code mapper: overlay onto rows the mapper already
emitted, mint rows for tables it did not.
"""

from __future__ import annotations

import pytest

from topos.canonicalization.declared_field_map import (
    DeclaredFieldMapper,
    build_canonical_mapper,
    evaluate_rule,
    resolve_path,
    validate_canonical_field_map,
)
from topos.canonicalization.mappers.base import CanonicalMapper, CanonicalRecord, MappingMetadata
from topos.ingestion.parsers.base import NormalizedRecord

_RECORD = {
    "id": "evt-1",
    "type": "PushEvent",
    "created_at": "2026-07-01T12:34:56Z",
    "repo": {"name": "dialoguesai/topos"},
    "payload": {
        "size": 2,
        "commits": [
            {"sha": "a" * 40, "message": "fix: tighten retry loop", "authorship": "authored"},
            {"sha": "b" * 40, "message": "docs: add sync notes", "authorship": "bot"},
        ],
    },
}


def _norm(payload=None) -> NormalizedRecord:
    payload = _RECORD if payload is None else payload
    return NormalizedRecord(record_id=str(payload.get("id") or ""), payload=payload)


# --------------------------------------------------------------------------- paths


def test_wildcard_path_expands_list_elements() -> None:
    assert resolve_path(_RECORD, "payload.commits[*].message") == [
        "fix: tighten retry loop",
        "docs: add sync notes",
    ]


def test_plain_path_and_missing_path() -> None:
    assert resolve_path(_RECORD, "repo.name") == ["dialoguesai/topos"]
    assert resolve_path(_RECORD, "repo.nope.deeper") == []


def test_path_descends_into_json_string_columns() -> None:
    # Reload batches carry metadata_json as a JSON string; a declaration must
    # resolve it the same way it resolves a fresh dict payload.
    row = {"metadata_json": '{"repo": "dialoguesai/topos"}'}
    assert resolve_path(row, "metadata_json.repo") == ["dialoguesai/topos"]


# --------------------------------------------------------------------------- rules


def test_bare_string_rule_is_a_path() -> None:
    assert evaluate_rule("repo.name", _RECORD) == "dialoguesai/topos"


def test_multi_value_path_joins_with_declared_separator() -> None:
    rule = {"path": "payload.commits[*].message", "join": "\n\n"}
    assert evaluate_rule(rule, _RECORD) == "fix: tighten retry loop\n\ndocs: add sync notes"


def test_first_of_takes_the_first_non_empty_path() -> None:
    rule = {"first_of": ["payload.html_url", "repo.name"]}
    assert evaluate_rule(rule, _RECORD) == "dialoguesai/topos"


def test_template_substitutes_dotted_paths_and_blanks_misses() -> None:
    rule = {"template": "{repo.name}: pushed {payload.size} commits"}
    assert evaluate_rule(rule, _RECORD) == "dialoguesai/topos: pushed 2 commits"
    assert evaluate_rule({"template": "{nope.gone}!"}, _RECORD) == "!"


def test_map_and_default() -> None:
    rule = {"path": "type", "map": {"PushEvent": "push"}}
    assert evaluate_rule(rule, _RECORD) == "push"
    assert evaluate_rule({"path": "missing", "default": "other"}, _RECORD) == "other"


def test_transform_catalog() -> None:
    assert evaluate_rule({"path": "type", "transform": "strip_event_suffix"}, _RECORD) == "push"
    assert evaluate_rule({"path": "repo.name", "transform": "org_prefix"}, _RECORD) == "dialoguesai"
    assert evaluate_rule({"path": "repo.name", "transform": "basename"}, _RECORD) == "topos"
    assert (
        evaluate_rule(
            {"path": "payload.commits[*].message", "transform": "first_line", "join": " | "},
            _RECORD,
        )
        == "fix: tighten retry loop | docs: add sync notes"
    )


def test_when_gate_suppresses_the_rule() -> None:
    rule = {"const": "code", "when": {"path": "type", "equals": "IssuesEvent"}}
    assert evaluate_rule(rule, _RECORD) == ""
    assert evaluate_rule({**rule, "when": {"path": "type", "equals": "PushEvent"}}, _RECORD) == "code"


# --------------------------------------------------------- overlay onto a code mapper


class _StubActivityMapper(CanonicalMapper):
    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        return CanonicalRecord(
            record_id="stub:1",
            payload={"event_id": "stub:1", "title": "coded title", "activity_type": "push"},
        )

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="stub", mapping_version="v1")


def test_declaration_overlays_the_code_mapper_row() -> None:
    mapper = DeclaredFieldMapper(
        source_id="stub",
        field_map={"activity_events": {"content": "payload.commits[*].message"}},
        default_table="activity_events",
        base=_StubActivityMapper(),
    )
    (record,) = mapper.map_many(_norm())
    assert record.payload["content"] == "fix: tighten retry loop\n\ndocs: add sync notes"
    # Fields the code mapper owns survive the overlay.
    assert record.payload["title"] == "coded title"
    assert record.payload["event_id"] == "stub:1"


def test_overlay_never_blanks_a_computed_value() -> None:
    mapper = DeclaredFieldMapper(
        source_id="stub",
        field_map={"activity_events": {"title": "nowhere.at.all"}},
        default_table="activity_events",
        base=_StubActivityMapper(),
    )
    (record,) = mapper.map_many(_norm())
    assert record.payload["title"] == "coded title"


# ------------------------------------------------------------- standalone (no mapper)


def test_declaration_alone_maps_a_record_with_no_code_mapper() -> None:
    """The self-serve path: a source with no Python in this repo puts a row on
    the activity lane purely from its definition."""
    mapper = DeclaredFieldMapper(
        source_id="acme",
        field_map={
            "activity_events": {
                "event_id": {"template": "acme:{id}"},
                "activity_type": {"path": "type", "transform": "strip_event_suffix"},
                "occurred_at": "created_at",
                "title": {"template": "{repo.name}: pushed {payload.size} commits"},
                "content": {"path": "payload.commits[*].message", "join": "\n\n"},
            }
        },
        default_table="activity_events",
    )
    (record,) = mapper.map_many(_norm())
    assert record.table is None  # default table for the lane
    assert record.record_id == "acme:evt-1"
    assert record.payload == {
        "event_id": "acme:evt-1",
        "activity_type": "push",
        "occurred_at": "2026-07-01T12:34:56Z",
        "title": "dialoguesai/topos: pushed 2 commits",
        "content": "fix: tighten retry loop\n\ndocs: add sync notes",
    }


def test_row_without_its_identity_column_is_dropped_not_written_blind() -> None:
    mapper = DeclaredFieldMapper(
        source_id="acme",
        field_map={"activity_events": {"title": "repo.name"}},
        default_table="activity_events",
    )
    assert mapper.map_many(_norm()) == []


# ------------------------------------------------------------------------- fan-out


def test_fan_out_mints_one_row_per_item_with_a_gate() -> None:
    mapper = DeclaredFieldMapper(
        source_id="acme",
        field_map={
            "journal_entries": {
                "fan_out": "payload.commits[*]",
                "where": {"path": "authorship", "in": ["authored"]},
                "fields": {
                    "entry_id": {"template": "acme:{sha}", "scope": "item"},
                    "content": {"path": "message", "scope": "item"},
                    "entry_at": "created_at",
                    "category": {"const": "code"},
                },
            }
        },
        default_table="activity_events",
    )
    records = mapper.map_many(_norm())
    assert [r.table for r in records] == ["journal_entries"]  # not the default lane
    assert records[0].payload == {
        "entry_id": f"acme:{'a' * 40}",
        "content": "fix: tighten retry loop",
        "entry_at": "2026-07-01T12:34:56Z",
        "category": "code",
    }


def test_fan_out_still_mints_when_the_code_mapper_writes_that_table_too() -> None:
    """A fan-out declaration adds rows; it is never silently suppressed because
    the base mapper already wrote to that table."""

    class _JournalWritingMapper(CanonicalMapper):
        def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
            return CanonicalRecord(
                record_id="coded:1",
                payload={"entry_id": "coded:1", "content": "from the code mapper"},
                table="journal_entries",
            )

        def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
            return MappingMetadata(source_id="stub", mapping_version="v1")

    mapper = DeclaredFieldMapper(
        source_id="stub",
        field_map={
            "journal_entries": {
                "fan_out": "payload.commits[*]",
                "fields": {"entry_id": {"path": "sha", "scope": "item"}},
            }
        },
        default_table="activity_events",
        base=_JournalWritingMapper(),
    )
    assert [r.record_id for r in mapper.map_many(_norm())] == ["coded:1", "a" * 40, "b" * 40]


def test_fan_out_gate_allows_items_missing_the_gated_field() -> None:
    """Legacy payloads that predate a stamped field must not vanish silently."""
    mapper = DeclaredFieldMapper(
        source_id="acme",
        field_map={
            "journal_entries": {
                "fan_out": "payload.commits[*]",
                "where": {"path": "authorship", "in": ["authored"]},
                "fields": {"entry_id": {"path": "sha", "scope": "item"}},
            }
        },
        default_table="activity_events",
    )
    payload = {
        "id": "evt-2",
        "payload": {"commits": [{"sha": "c" * 40, "message": "legacy"}]},
    }
    assert [r.record_id for r in mapper.map_many(_norm(payload))] == ["c" * 40]


# ---------------------------------------------------------------------- validation


def test_validation_rejects_unknown_table_transform_and_reserved_column() -> None:
    with pytest.raises(ValueError, match="unknown canonical table"):
        validate_canonical_field_map({"not_a_table": {"content": "a"}})
    with pytest.raises(ValueError, match="unknown transform"):
        validate_canonical_field_map({"activity_events": {"content": {"path": "a", "transform": "nope"}}})
    with pytest.raises(ValueError, match="reserved"):
        validate_canonical_field_map({"activity_events": {"metadata_json": "a"}})
    with pytest.raises(ValueError, match="unknown rule keys"):
        validate_canonical_field_map({"activity_events": {"content": {"pathh": "a"}}})
    with pytest.raises(ValueError, match="at least one field"):
        validate_canonical_field_map({"activity_events": {}})


def test_validation_accepts_none_and_the_bundled_github_declaration() -> None:
    from topos.sources.registry import GITHUB_ACTIVITY

    validate_canonical_field_map(None)
    validate_canonical_field_map(GITHUB_ACTIVITY.canonical_field_map)


# ------------------------------------------------------------------ mapper builder


def test_builder_returns_fallback_when_nothing_is_declared() -> None:
    from topos.canonicalization.mappers import BrowserActivityCanonicalMapper
    from topos.sources.registry import BROWSER_VISITS

    mapper = build_canonical_mapper(
        BROWSER_VISITS, default_table="activity_events", fallback_mapper_id="browser_activity"
    )
    assert isinstance(mapper, BrowserActivityCanonicalMapper)


def test_builder_wraps_the_code_mapper_when_a_field_map_is_declared() -> None:
    from topos.sources.registry import GITHUB_ACTIVITY

    mapper = build_canonical_mapper(
        GITHUB_ACTIVITY, default_table="activity_events", fallback_mapper_id="browser_activity"
    )
    assert isinstance(mapper, DeclaredFieldMapper)
    assert mapper.base is not None
    assert mapper.mapping_metadata(_norm()).source_id == "github_activity"
