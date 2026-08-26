"""`object_value` is what the facts page renders, so it must keep its case."""

import json

from topos.features.derivation.writer import _canon_value, _display_value


def test_the_display_value_keeps_case_and_the_keying_value_does_not():
    value = {"person": "Mike November", "tier": "inner_circle"}
    assert json.loads(_display_value(value))["person"] == "Mike November"
    # equality/keying still folds case, so two spellings of a name still collide
    assert _canon_value(value) == _canon_value({"person": "MIKE NOVEMBER",
                                                "tier": "inner_circle"})


def test_both_forms_sort_keys_so_field_order_never_changes_identity():
    a = {"tier": "close", "person": "K.L. Oscar"}
    b = {"person": "K.L. Oscar", "tier": "close"}
    assert _display_value(a) == _display_value(b)
    assert _canon_value(a) == _canon_value(b)


def test_a_plain_string_value_is_unchanged_by_either():
    assert _display_value("  Qualia   Sleeping Pills ") == "Qualia Sleeping Pills"
    assert _canon_value("Qualia Sleeping Pills") == "qualia sleeping pills"
