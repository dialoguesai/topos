"""A journal entry must be dated when it happened, not when it was imported.

Live regression (2026-08-10): 127 grow_journal rows carried
``entry_at == ingested_at == 2026-08-08T03:34:44`` — identical to the second —
while ``starts_at`` held the real session times, spanning 2026-06-28 to
2026-08-08. Another 171 grow_data_file rows shared 2026-06-28T23:28:45 the same
way. The entity graph dates its edges from canonical event time, so a batch of
months-old sessions all looked like they happened at the import instant and
dragged years-old relationships into the "last 6 days" graph view.
"""

from __future__ import annotations

import pytest

from topos.storage.canonical.canonical_store import SQLiteCanonicalStore

pytestmark = pytest.mark.public


def _entry_at(record, ingested_at):
    return SQLiteCanonicalStore._journal_entry_at(record, ingested_at)


def test_ingest_clock_stamp_defers_to_the_session_start():
    """The exact live signature: entry_at == ingested_at to the second."""
    record = {"entry_at": "2026-08-08T03:34:44", "starts_at": "2026-06-28T18:00:00"}
    assert _entry_at(record, "2026-08-08T03:34:44.291428+00:00") == "2026-06-28T18:00:00"


def test_a_real_entry_time_is_left_alone():
    """Only the ingest-clock signature is overridden — never a genuine time."""
    record = {"entry_at": "2026-07-04T09:15:00", "starts_at": "2026-07-04T09:00:00"}
    assert _entry_at(record, "2026-08-08T03:34:44.291428+00:00") == "2026-07-04T09:15:00"


def test_missing_entry_at_falls_back_to_starts_at():
    record = {"entry_at": None, "starts_at": "2026-05-01T08:00:00"}
    assert _entry_at(record, "2026-06-28T23:28:45+00:00") == "2026-05-01T08:00:00"


def test_without_starts_at_nothing_is_invented():
    """No better answer available → keep what the producer said."""
    record = {"entry_at": "2026-08-08T03:34:44", "starts_at": None}
    assert _entry_at(record, "2026-08-08T03:34:44.291428+00:00") == "2026-08-08T03:34:44"


def test_no_times_at_all_stays_none():
    assert _entry_at({"entry_at": None, "starts_at": None}, "2026-08-08T03:34:44") is None


def test_missing_ingested_at_does_not_trigger_the_override():
    """An empty ingest stamp must not match an empty-ish entry_at by accident."""
    record = {"entry_at": "2026-07-04T09:15:00", "starts_at": "2026-01-01T00:00:00"}
    assert _entry_at(record, "") == "2026-07-04T09:15:00"
