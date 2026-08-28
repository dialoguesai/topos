"""A fan-out child must never mint a goal, end-to-end through the real job.

This is the invariant behind the 154 fabricated goals found in the fan-out audit
("Watch Northgate- The Foundry", "Seeking information about the book 'The Foundry'
by Northgate", "Search for houses in East Village, NYC"). They were not produced
because a content-length gate was missing — they were produced because the child
was misfiled as a journal entry, which made ``record_role`` return ``authored``
and satisfied the belief-role gate that ``GoalExtractionJob`` already had.

``tests/disclosure/test_fanout_table_stamp.py`` pins the role resolution. This
pins the CONSEQUENCE at the job boundary, so a future refactor that reads a
different field, or drops the gate, turns this red even if role resolution itself
still looks right.

On why there is no minimum-content gate here. It was considered and measured
against the live corpus 2026-08-27, and length turns out to be a bad
discriminator:

  * ``location_events``: 77 of 77 goals came from short sources — all fabricated,
    and all of them blocked by the role gate once the table stamp is correct.
  * ``journal_entries``: only 7 of 1,721 goals came from sources of <=40 chars,
    and they include legitimate ones — "Accomplished: Review Beta specs" (31
    chars) yields "Review Beta specs".
  * verbatim echoes of the source: 3 of 2,019.

A length threshold would therefore delete real goals to catch a residue the role
gate already covers. The gate that works is provenance, not size.
"""

from __future__ import annotations

import pytest

from topos.disclosure.field_registry import stamp_canonical_table
from topos.enrichment.jobs.canonical.brief_fallback import prepare_signal_record
from topos.enrichment.jobs.canonical.goal_extraction_job import GoalExtractionJob
from topos.ingestion.journal_location_fanout import (
    journal_location_event_from_entry,
    journal_location_signal_record,
)

PARENT_ENTRY = {
    "entry_id": "tl-501",
    "place_name": "Northgate- The Foundry",
    "content": (
        "I finished the generative work from this morning, and then began some "
        "deep research into how to develop the eval and query set."
    ),
    "starts_at": "2026-07-06T19:05:00",
    "category": "Topos",
}


def _stamped_child():
    loc = journal_location_event_from_entry(PARENT_ENTRY, source_id="grow_journal")
    rec = prepare_signal_record(journal_location_signal_record(loc))
    rec["message_id"] = rec.get("record_id")
    stamp_canonical_table([rec], source_group="journal")
    return rec


def _stamped_parent():
    rec = prepare_signal_record(dict(PARENT_ENTRY, record_id="tl-501", source_id="grow_journal"))
    rec["message_id"] = "tl-501"
    stamp_canonical_table([rec], source_group="journal")
    return rec


class _ExplodingEngine:
    """Any LLM call at all is a failure: the gate must reject before extraction."""

    def __getattr__(self, name):
        def _boom(*_a, **_k):
            raise AssertionError(
                f"GoalExtractionJob reached the model (.{name}) for a record the "
                "belief-role gate should have declined"
            )

        return _boom


@pytest.mark.asyncio
async def test_a_fanout_child_never_reaches_goal_extraction():
    child = _stamped_child()
    assert child["_table"] == "location_events", "fixture must be correctly stamped"

    out = await GoalExtractionJob(engine=_ExplodingEngine()).enrich([child])

    assert out == [], "a machine-generated place string must not mint goals"


@pytest.mark.asyncio
async def test_the_child_is_rejected_on_provenance_not_on_length():
    """Give the child a long, goal-shaped content and it must STILL be refused.

    This is what separates the fix that works from the one that looks like it
    works. If someone later replaces the role gate with a length threshold, this
    test goes red while the test above would keep passing.
    """
    child = _stamped_child()
    child["content"] = (
        "Plan the next quarter of work at the Convent, including the eval set, "
        "the retrieval overhaul, and onboarding three new data sources before "
        "the end of the month."
    )

    out = await GoalExtractionJob(engine=_ExplodingEngine()).enrich([child])

    assert out == []


@pytest.mark.asyncio
async def test_the_parent_journal_entry_is_still_eligible():
    """Control: the fix must not have switched goal extraction off.

    The parent is the owner's own writing and is exactly what goals SHOULD come
    from. Reaching the engine is success here — the exploding engine proves the
    gate let it through.
    """
    parent = _stamped_parent()
    assert parent["_table"] == "journal_entries"

    with pytest.raises(AssertionError, match="reached the model"):
        await GoalExtractionJob(engine=_ExplodingEngine()).enrich([parent])
