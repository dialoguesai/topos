"""A stored ceiling's spelling survives the engine's own decode.

The query lane reads a grant's saved envelope through
`topos.uma_filters.query_filter_manifest` -> `shared.filtering.FilterManifest`.
Before the port of `normalize_ceiling` (control plane 1dd65995; P0-10 of
BEFORE_PROD_REVIEW_2026-09-22) a row whose ceiling was stored as "Summary",
" summary " or "" raised a ValidationError here, so that grant's reads failed
instead of being clamped to the ceiling the owner chose.
"""
from __future__ import annotations

import pytest

from shared.filtering import FilterManifest, filter_manifest_from_storage
from topos.uma_filters import query_filter_manifest


@pytest.mark.parametrize("stored", ["summary", "Summary", " summary ", "  SUMMARY"])
def test_a_stored_ceiling_reads_as_its_canonical_spelling(stored: str) -> None:
    manifest = query_filter_manifest({"filter_manifest": {"access_mode_ceiling": stored, "filters": []}})
    assert manifest is not None
    assert manifest.access_mode_ceiling == "summary"


def test_an_empty_ceiling_is_no_ceiling_not_an_error() -> None:
    assert query_filter_manifest({"filter_manifest": {"access_mode_ceiling": ""}}).access_mode_ceiling is None


def test_a_ceiling_survives_the_storage_round_trip() -> None:
    stored = FilterManifest(access_mode_ceiling="raw").to_storage_dict()
    assert stored["access_mode_ceiling"] == "raw"
    assert filter_manifest_from_storage(stored).access_mode_ceiling == "raw"


def test_an_unknown_ceiling_is_still_refused() -> None:
    with pytest.raises(Exception):
        query_filter_manifest({"filter_manifest": {"access_mode_ceiling": "unrestricted"}})
