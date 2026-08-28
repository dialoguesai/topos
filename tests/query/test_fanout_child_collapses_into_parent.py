"""A child and its parent are one moment, and must not spend two slots on it.

Once a place hit carries the parent's narrative, a result set holding BOTH shows
that narrative twice. Measured before the narrative was attached: in 55 of 55
query sessions that surfaced a fan-out child, the child's own parent was already
in the same result set — so the duplicate is the common case, not the corner.

Collapsing beats excluding children from retrieval outright. The child is what
carries the place name into the index, so for "where was I on Tuesday" it is the
only row that can match at all — and it now brings the parent's text with it.
Both halves are needed: drop the child when the parent is present, keep it (with
the parent's narrative) when it is not.

Keyed on ``source_record_id`` — written by ingest from the beginning, read by
nothing until this workstream.
"""

from __future__ import annotations

import pytest

from topos.query.retrieval import _collapse_fanout_children


def _item(record_id, *, parent=None, score=1.0):
    item = {"record_id": record_id, "summary_text": f"text of {record_id}",
            "relevance_score": score}
    if parent:
        item["source_record_id"] = parent
    return item


def test_a_child_is_dropped_when_its_parent_is_present(conn=None):
    items = [_item("tl-1"), _item("tl-1-loc", parent="tl-1")]

    kept = _collapse_fanout_children(items)

    assert [i["record_id"] for i in kept] == ["tl-1"]


def test_a_child_survives_without_its_parent():
    """The reason this is a collapse and not an exclusion.

    The child carries the place name into the index; for a place-shaped ask it
    is the only row that matches, and it now brings the parent's narrative.
    """
    items = [_item("tl-1-loc", parent="tl-1")]

    kept = _collapse_fanout_children(items)

    assert [i["record_id"] for i in kept] == ["tl-1-loc"]


def test_a_child_of_a_DIFFERENT_parent_survives():
    items = [_item("tl-1"), _item("tl-2-loc", parent="tl-2")]

    kept = _collapse_fanout_children(items)

    assert {i["record_id"] for i in kept} == {"tl-1", "tl-2-loc"}


def test_rows_with_no_parent_link_are_untouched():
    items = [_item("m-1"), _item("m-2"), _item("m-3")]

    assert len(_collapse_fanout_children(items)) == 3


def test_a_row_that_points_at_itself_is_kept():
    """Self-reference must not delete the row that carries it."""
    items = [_item("tl-1", parent="tl-1")]

    assert len(_collapse_fanout_children(items)) == 1


def test_several_children_of_one_present_parent_all_collapse():
    items = [_item("tl-1"), _item("tl-1-loc", parent="tl-1"), _item("tl-1-b", parent="tl-1")]

    kept = _collapse_fanout_children(items)

    assert [i["record_id"] for i in kept] == ["tl-1"]


def test_an_empty_result_set_is_safe():
    assert _collapse_fanout_children([]) == []


def test_the_item_carries_the_parent_link_at_all():
    """The collapse is keyed on a field the item must actually have.

    Threading it through is half the fix; without it this function is a no-op
    that looks like a guard.
    """
    import inspect

    from topos.query import retrieval

    src = inspect.getsource(retrieval._canonical_row_to_item)
    assert '"source_record_id"' in src
