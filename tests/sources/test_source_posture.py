"""P1.1 (PLAN_PROVENANCE_SPLIT): source posture — axis 1, stamped per source.

Registry defaults per plan §3.1: journal/resume/profile-grade sources are
'personal', chat sources 'mixed' (role per-row), browser visits 'ambient',
browser events 'personal'-grade engagement. Every bundled source must carry an
EXPLICIT value; serialized definitions round-trip it; unknown payloads default
to 'mixed' (the safe per-row value).
"""

from __future__ import annotations

import pytest

from topos.sources.definitions import (
    DataSourceDefinition,
    POSTURE_VALUES,
    definition_from_payload,
)
from topos.sources.registry import REGISTRY


EXPECTED_POSTURES = {
    # plan §3.1 explicit defaults
    "chatgpt_file_ingestion": "mixed",
    "chatgpt_ui_conversation": "mixed",
    "imessage": "mixed",
    "signal": "mixed",
    "demo_messenger_file": "mixed",
    "browser_visits": "ambient",
    "browser_events": "personal",  # highlights/stars = personal-grade engagement
    "demo_journal_file": "personal",
    "demo_resume_file": "personal",
    # remaining bundled sources (documented decisions)
    "github_activity": "personal",  # owner-performed deeds, not consumption
    "voxterm_transcripts": "mixed",  # transcripts can carry other speakers
    "calendar_stub": "personal",
    "contacts_enrichment_stub": "personal",
    "demo_email_file": "mixed",
    "demo_calendar_file": "personal",
    "demo_financial_file": "personal",
    "demo_browser_file": "ambient",
    "demo_places_file": "personal",
    "demo_contacts_file": "personal",
}


def test_every_registry_source_has_expected_explicit_posture():
    assert set(EXPECTED_POSTURES) == set(REGISTRY), (
        "registry changed: give the new/removed source an explicit posture here "
        "AND in registry.py (PLAN_PROVENANCE_SPLIT P1.1)"
    )
    for source_id, defn in REGISTRY.items():
        assert defn.posture == EXPECTED_POSTURES[source_id], source_id
        assert defn.posture in POSTURE_VALUES, source_id


def test_posture_serializes_in_to_dict_and_round_trips():
    for defn in REGISTRY.values():
        payload = defn.to_dict()
        assert payload["posture"] == defn.posture
        assert definition_from_payload(payload).posture == defn.posture


def test_payload_without_posture_defaults_to_mixed():
    # Runtime-installed definitions snapshotted before P1.1 carry no posture:
    # they must rehydrate as 'mixed' (per-row role), never crash.
    payload = {
        "source_id": "legacy",
        "display_name": "Legacy",
        "source_type": "file",
        "schema_id": "s.v1",
        "parser_id": "p.v1",
    }
    assert definition_from_payload(payload).posture == "mixed"


def test_invalid_posture_rejected():
    with pytest.raises(ValueError, match="posture"):
        DataSourceDefinition(
            source_id="x",
            display_name="X",
            source_type="file",
            schema_id="s.v1",
            parser_id="p.v1",
            posture="background-noise",
        )
