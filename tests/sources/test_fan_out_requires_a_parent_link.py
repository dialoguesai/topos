"""A self-serve fan-out must declare where each child came from.

``DeclaredFieldMapper._mint`` writes only the declared columns, and
``metadata_json`` — the channel the built-in location fan-out uses for its parent
pointer — is RESERVED. So a declared fan-out produced children with no link of any
kind: strictly worse than the built-in one, on the path the connector catalog
advertises as self-serve.

Both failures this gate closes were made by the built-in Python fan-outs first,
which is the argument for enforcing them in the spec rather than trusting the
registerer:

  * the retired GitHub per-commit fan-out overwrote ``source_record_id`` with a
    synthetic composite, so 0 of its 121 surviving children join back to anything;
  * ``_mint`` validates only that an id resolved to a non-empty string, so a
    record-scoped id template gives every item the SAME id — the upserts overwrite
    each other and N-1 records are discarded in silence.

The validator is the single gate on every declaration and
``DataSourceDefinition.__post_init__`` calls it, so this covers bundled definitions
and runtime-installed ones alike.
"""

from __future__ import annotations

import pytest

from topos.sources.declared_field_map_spec import validate_canonical_field_map


def _fan_out(**field_overrides):
    fields = {
        "entry_id": {"template": "github:{repo.name}:{sha}", "scope": "item"},
        "source_record_id": {"path": "payload.push_id"},
        "content": {"path": "message", "scope": "item"},
    }
    fields.update(field_overrides)
    return {
        "journal_entries": {
            "fan_out": "payload.commits[*]",
            "fields": fields,
        }
    }


# --------------------------------------------------------------- the requirement


def test_a_fan_out_without_a_parent_link_is_rejected():
    declaration = _fan_out()
    del declaration["journal_entries"]["fields"]["source_record_id"]

    with pytest.raises(ValueError, match="source_record_id"):
        validate_canonical_field_map(declaration)


def test_a_record_scoped_parent_link_is_accepted():
    validate_canonical_field_map(_fan_out())


def test_an_item_scoped_parent_link_is_rejected():
    """Reading the parent per-item reproduces the GitHub mistake exactly.

    ``source_record_id`` names the record the children came FROM. Resolving it
    against the item makes it a synthetic per-child value that joins to nothing.
    """
    with pytest.raises(ValueError, match="record-scoped"):
        validate_canonical_field_map(
            _fan_out(source_record_id={"path": "sha", "scope": "item"})
        )


def test_a_record_scoped_id_is_rejected():
    """Every item would mint the same id and all but the last would vanish."""
    with pytest.raises(ValueError, match="item-scoped"):
        validate_canonical_field_map(
            _fan_out(entry_id={"template": "github:{repo.name}", "scope": "record"})
        )


def test_a_bare_path_id_is_rejected_because_it_defaults_to_record_scope():
    """``apply_field_map`` treats a bare string as record-scoped.

    This is the shape a registerer is most likely to write by accident, so the
    default must fail rather than collapse the fan-out silently.
    """
    with pytest.raises(ValueError, match="item-scoped"):
        validate_canonical_field_map(_fan_out(entry_id="payload.push_id"))


# ------------------------------------------------------------------- controls


def test_a_declaration_with_no_fan_out_is_unaffected():
    """The requirement is about SPLITTING. A 1:1 declaration needs no parent link."""
    validate_canonical_field_map(
        {"journal_entries": {"content": {"path": "payload.body"}}}
    )


def test_the_shipped_docstring_example_still_validates():
    """`declared_field_map`'s own worked example must remain legal.

    If the rule rejects the documentation, the rule is wrong.
    """
    validate_canonical_field_map(
        {
            "activity_events": {
                "content": {"path": "payload.commits[*].message", "join": "\n\n"}
            },
            "journal_entries": {
                "fan_out": "payload.commits[*]",
                "where": {"path": "authorship", "in": ["authored"]},
                "fields": {
                    "entry_id": {"template": "github:{repo.name}:{sha}", "scope": "item"},
                    "source_record_id": {"path": "payload.push_id"},
                    "content": {"path": "message", "scope": "item"},
                    "category": {"const": "code"},
                },
            },
        }
    )


def test_every_bundled_source_definition_still_validates():
    """The registry is the real corpus — a rule that breaks it is not shippable."""
    from topos.sources.registry import REGISTRY

    failures = []
    for source in getattr(REGISTRY, "values", lambda: [])() or []:
        declaration = getattr(source, "canonical_field_map", None)
        if declaration is None:
            continue
        try:
            validate_canonical_field_map(declaration)
        except ValueError as exc:
            failures.append(f"{getattr(source, 'source_id', source)}: {exc}")
    assert failures == [], f"bundled definitions rejected by the new rule: {failures}"
